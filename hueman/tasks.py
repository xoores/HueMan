"""Run blocking calls off the GUI thread and get the result back on it."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class _Invoker(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


_invoker: _Invoker | None = None


def _get_invoker() -> _Invoker:
    global _invoker
    if _invoker is None:  # created lazily, on the GUI thread
        _invoker = _Invoker()
    return _invoker


def in_background(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
                  on_error: Callable[[Exception], None] | None = None) -> threading.Thread:
    inv = _get_invoker()

    def run():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            if on_error:
                inv.call.emit(lambda e=e: on_error(e))
            return
        if on_done:
            inv.call.emit(lambda: on_done(result))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
