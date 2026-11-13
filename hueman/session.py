"""Connection to one bridge: initial load, event stream, and ordered, throttled writes."""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .api import CertificateMismatch, HueClient, HueError, abort_stream
from .config import BridgeConfig
from .store import ResourceStore, deep_merge

# Minimum spacing between PUTs to the same resource (Hue guidance: ~10/s for lights, 1/s for groups).
THROTTLE_MS = {"grouped_light": 1000, "light": 100}
DEFAULT_THROTTLE_MS = 100


class _Invoker(QObject):
    """Delivers callbacks from worker threads onto the GUI thread."""

    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


class BridgeSession(QObject):
    status = Signal(str, str)  # state ("connecting", "connected", "reconnecting", "error", "disconnected"), message
    error = Signal(str)  # user-facing write/read failure
    events = Signal(list)  # raw events, after they were applied to the store

    def __init__(self, bridge: BridgeConfig, store: ResourceStore, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.store = store
        self.client = HueClient(bridge.host, bridge.app_key, bridge.cert_sha256, bridge.port)
        self._invoker = _Invoker()
        self._reader = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hue-read")
        # A single writer keeps writes in the order the user made them.
        self._writes: queue.Queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, name="hue-write", daemon=True)
        self._stop = threading.Event()
        self._sse_thread: threading.Thread | None = None
        self._sse_response = None
        self._pending: dict[tuple[str, str], dict] = {}
        self._last_sent: dict[tuple[str, str], float] = {}
        self._timers: dict[tuple[str, str], QTimer] = {}
        self.connected = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.status.emit("connecting", f"Connecting to {self.bridge.host}…")
        self._writer.start()
        self.run(self.client.get_all, self._on_loaded, self._on_load_failed)

    def stop(self) -> None:
        self._stop.set()
        self._writes.put(None)
        resp = self._sse_response
        if resp is not None:
            # Closing the response while the event thread is blocked reading it deadlocks;
            # shutting the socket down makes that read return instead.
            abort_stream(resp)
        self._reader.shutdown(wait=False, cancel_futures=True)
        for t in self._timers.values():
            t.stop()
        self.connected = False

    def refresh(self) -> None:
        self.run(self.client.get_all, self.store.load, lambda e: self.error.emit(f"Refresh failed: {e}"))

    def _on_loaded(self, data: list[dict]) -> None:
        if self._stop.is_set():
            return
        self.store.load(data)
        self.connected = True
        self.status.emit("connected", f"Connected to {self.bridge.name}")
        if self._sse_thread is None:
            self._sse_thread = threading.Thread(target=self._sse_loop, name="hue-events", daemon=True)
            self._sse_thread.start()

    def _on_load_failed(self, exc: Exception) -> None:
        if self._stop.is_set():
            return
        self.status.emit("error", str(exc))

    # -- background helpers ------------------------------------------------

    def _post(self, fn: Callable[[], None]) -> None:
        if not self._stop.is_set():
            self._invoker.call.emit(fn)

    def run(self, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[Exception], None] | None = None) -> None:
        """Run a blocking read on a worker thread; callbacks run on the GUI thread."""

        def job():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 - surfaced to the user
                if on_error:
                    self._post(lambda e=e: on_error(e))
                else:
                    self._post(lambda m=str(e): self.error.emit(m))
                return
            if on_done:
                self._post(lambda: on_done(result))

        if not self._stop.is_set():
            self._reader.submit(job)

    # -- writes ------------------------------------------------------------

    def put(self, rtype: str, rid: str, payload: dict, optimistic: bool = True,
            on_done: Callable[[Any], None] | None = None) -> None:
        """Send a change. Rapid successive changes to one resource are coalesced and rate-limited."""
        if optimistic:
            self.store.merge_local(rid, payload)
        key = (rtype, rid)
        pending = self._pending.setdefault(key, {})
        deep_merge(pending, payload)
        if on_done:
            pending.setdefault("__callbacks__", []).append(on_done)
        interval = THROTTLE_MS.get(rtype, DEFAULT_THROTTLE_MS) / 1000.0
        wait = self._last_sent.get(key, 0.0) + interval - time.monotonic()
        if wait <= 0:
            self._flush(key)
            return
        timer = self._timers.get(key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda k=key: self._flush(k))
            self._timers[key] = timer
        if not timer.isActive():
            timer.start(int(wait * 1000) + 1)

    def _flush(self, key: tuple[str, str]) -> None:
        payload = self._pending.pop(key, None)
        if not payload:
            return
        callbacks = payload.pop("__callbacks__", [])
        self._last_sent[key] = time.monotonic()
        rtype, rid = key
        self._enqueue(lambda: self.client.put(rtype, rid, payload), callbacks,
                      f"Updating {rtype} failed")

    def post(self, rtype: str, payload: dict, on_done: Callable[[Any], None] | None = None) -> None:
        self._enqueue(lambda: self.client.post(rtype, payload), [on_done] if on_done else [],
                      f"Creating {rtype} failed")

    def delete(self, rtype: str, rid: str, on_done: Callable[[Any], None] | None = None) -> None:
        def done(result):
            self.store.drop_local(rid)
            if on_done:
                on_done(result)

        self._enqueue(lambda: self.client.delete(rtype, rid), [done], f"Deleting {rtype} failed")

    def _enqueue(self, fn: Callable[[], Any], callbacks: list, what: str) -> None:
        self._writes.put((fn, callbacks, what))

    def _write_loop(self) -> None:
        while True:
            item = self._writes.get()
            if item is None or self._stop.is_set():
                return
            fn, callbacks, what = item
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                msg = f"{what}: {e}"
                self._post(lambda m=msg: self.error.emit(m))
                # The optimistic update may now be wrong; resynchronise.
                self._post(self.refresh)
                continue
            for cb in callbacks:
                self._post(lambda cb=cb, r=result: cb(r))

    # -- event stream ------------------------------------------------------

    def _sse_loop(self) -> None:
        backoff = 1.0
        first = True
        while not self._stop.is_set():
            try:
                resp = self.client.open_event_stream()
                self._sse_response = resp
                if not first:
                    # We may have missed events while disconnected.
                    self._post(self.refresh)
                    self._post(lambda: self.status.emit("connected", f"Connected to {self.bridge.name}"))
                first = False
                backoff = 1.0
                for ev in self.client.iter_events(resp):
                    if self._stop.is_set():
                        return
                    self._post(lambda ev=ev: self._apply(ev))
                # Stream ended normally (idle timeout on the bridge side); reconnect quietly.
            except CertificateMismatch as e:
                self._post(lambda m=str(e): self.status.emit("error", m))
                return
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    return
                if "Read timed out" in str(e):
                    continue
                msg = f"Event stream lost ({e}); reconnecting…"
                self._post(lambda m=msg: self.status.emit("reconnecting", m))
                first = False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._sse_response = None

    def _apply(self, ev: dict) -> None:
        self.store.apply_event(ev)
        self.events.emit([ev])


__all__ = ["BridgeSession", "HueError"]
