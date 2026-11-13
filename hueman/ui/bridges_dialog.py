"""Configuration window: pair new bridges, edit or remove known ones."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout,
)

from .. import api
from ..config import AppConfig, BridgeConfig
from ..discovery import FoundBridge, discover
from ..tasks import in_background
from .widgets import confirm

LINK_TIMEOUT_S = 60


def _parse_host(text: str) -> tuple[str, int]:
    text = text.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if text.startswith("["):  # [ipv6]:port
        host, _, rest = text[1:].partition("]")
        return host, int(rest.lstrip(":") or 443)
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port)
    return text, 443


class BridgesDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hue Bridges")
        self.resize(560, 560)
        self.config = config
        self.connect_to: str | None = None
        lay = QVBoxLayout(self)

        known = QGroupBox("Paired bridges")
        kl = QVBoxLayout(known)
        self.known = QListWidget()
        self.known.itemDoubleClicked.connect(lambda _: self._use())
        kl.addWidget(self.known)
        row = QHBoxLayout()
        for title, fn in (("Connect", self._use), ("Rename…", self._rename), ("Change address…", self._edit_host),
                          ("Remove", self._remove)):
            b = QPushButton(title)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        kl.addLayout(row)
        lay.addWidget(known)

        add = QGroupBox("Add a bridge")
        al = QVBoxLayout(add)
        self.found = QListWidget()
        self.found.setMaximumHeight(120)
        self.found.currentItemChanged.connect(
            lambda cur, _: cur and self.manual.setText(cur.data(Qt.UserRole).host if cur.data(Qt.UserRole) else ""))
        self.scan_btn = QPushButton("Search network (mDNS)")
        self.scan_btn.clicked.connect(self._scan)
        al.addWidget(self.scan_btn)
        al.addWidget(self.found)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("IP address:"))
        self.manual = QLineEdit(placeholderText="e.g. 192.168.1.20")
        self.manual.returnPressed.connect(self._pair)
        mrow.addWidget(self.manual)
        self.pair_btn = QPushButton("Pair…")
        self.pair_btn.clicked.connect(self._pair)
        mrow.addWidget(self.pair_btn)
        al.addLayout(mrow)
        self.pair_status = QLabel(wordWrap=True)
        self.progress = QProgressBar(visible=False, maximum=LINK_TIMEOUT_S, textVisible=False)
        al.addWidget(self.pair_status)
        al.addWidget(self.progress)
        lay.addWidget(add)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._cancel_pairing = False
        self._fill_known()
        QTimer.singleShot(0, self._scan)

    # -- known bridges -----------------------------------------------------

    def _fill_known(self):
        self.known.clear()
        for b in self.config.bridges:
            item = QListWidgetItem(f"{b.name}\n{b.host} · {b.bridge_id}")
            item.setData(Qt.UserRole, b.bridge_id)
            self.known.addItem(item)

    def _selected(self) -> BridgeConfig | None:
        item = self.known.currentItem()
        return self.config.find(item.data(Qt.UserRole)) if item else None

    def _use(self):
        b = self._selected()
        if b:
            self.connect_to = b.bridge_id
            self.accept()

    def _rename(self):
        b = self._selected()
        if not b:
            return
        name, ok = QInputDialog.getText(self, "Rename bridge", "Name shown in hueman:", text=b.name)
        if ok and name.strip():
            b.name = name.strip()
            self.config.save()
            self._fill_known()

    def _edit_host(self):
        b = self._selected()
        if not b:
            return
        text, ok = QInputDialog.getText(self, "Change address", "IP address or host name of the bridge:",
                                        text=b.host if b.port == 443 else f"{b.host}:{b.port}")
        if ok and text.strip():
            b.host, b.port = _parse_host(text)
            self.config.save()
            self._fill_known()

    def _remove(self):
        b = self._selected()
        if b and confirm(self, "Remove bridge",
                         f"Forget “{b.name}” ({b.host})?\n\nThis only removes it from hueman. The bridge and its "
                         "devices are not changed. The application key stays registered on the bridge; you can "
                         "revoke it in the Hue app under Settings → Hue Bridges → Connected apps (if available).",
                         "Remove"):
            self.config.remove(b.bridge_id)
            self.config.save()
            self._fill_known()

    # -- discovery & pairing ---------------------------------------------

    def _scan(self):
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Searching…")
        self.found.clear()

        def done(bridges: list[FoundBridge]):
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("Search network (mDNS)")
            for fb in bridges:
                paired = self.config.find(fb.bridge_id) if fb.bridge_id else None
                label = f"{fb.name} — {fb.host}" + ("   (already paired)" if paired else "")
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, fb)
                self.found.addItem(item)
            if not bridges:
                item = QListWidgetItem("No bridges found. Enter the IP address below.")
                item.setFlags(Qt.NoItemFlags)
                self.found.addItem(item)
            # Update addresses of known bridges whose IP changed (DHCP).
            changed = False
            for fb in bridges:
                known = self.config.find(fb.bridge_id) if fb.bridge_id else None
                if known and (known.host, known.port) != (fb.host, fb.port):
                    known.host, known.port = fb.host, fb.port
                    changed = True
            if changed:
                self.config.save()
                self._fill_known()

        def failed(e: Exception):
            self.scan_btn.setEnabled(True)
            self.scan_btn.setText("Search network (mDNS)")
            self.pair_status.setText(f"mDNS search failed: {e}. Enter the IP address manually.")

        in_background(discover, done, failed)

    def _pair(self):
        text = self.manual.text().strip()
        if not text:
            self.pair_status.setText("Pick a bridge from the list or enter its IP address.")
            return
        host, port = _parse_host(text)
        self.pair_btn.setEnabled(False)
        self.manual.setEnabled(False)
        self.pair_status.setText(f"Contacting {host}…")

        def got_info(info: dict):
            bid = (info.get("bridgeid") or "").lower()
            name = info.get("name") or "Hue Bridge"
            existing = self.config.find(bid) if bid else None
            if existing and not confirm(self, "Already paired",
                                        f"{name} ({bid}) is already paired. Pair again and replace its key?", "Pair"):
                self._pair_finished()
                return
            self._wait_for_link(host, port, bid, name)

        in_background(lambda: api.get_public_config(host, port), got_info,
                      lambda e: self._pair_finished(f"{host} does not look like a Hue Bridge: {e}"))

    def _wait_for_link(self, host: str, port: int, bid: str, name: str):
        self.pair_status.setText(f"<b>Press the round link button on top of “{name}”.</b><br>"
                                 f"Waiting up to {LINK_TIMEOUT_S} seconds…")
        self.progress.setValue(0)
        self.progress.setVisible(True)
        started = time.monotonic()

        def attempt():
            fp = api.fetch_cert_fingerprint(host, port)
            while time.monotonic() - started < LINK_TIMEOUT_S:
                if self._cancel_pairing:
                    raise api.HueError("cancelled")
                try:
                    key, client_key = api.create_app_key(host, port)
                    return key, client_key, fp
                except api.LinkButtonNotPressed:
                    time.sleep(2)
            raise api.HueError("Timed out waiting for the link button.")

        tick = QTimer(self, interval=500)
        tick.timeout.connect(lambda: self.progress.setValue(int(time.monotonic() - started)))
        tick.start()

        def ok(result):
            tick.stop()
            key, client_key, fp = result
            bridge = BridgeConfig(bridge_id=bid or host, name=name, host=host, app_key=key, client_key=client_key,
                                  cert_sha256=fp, port=port)
            self.config.upsert(bridge)
            self.config.save()
            self._fill_known()
            self._pair_finished(f"Paired with {name}.")
            if confirm(self, "Bridge paired", f"{name} is paired. Connect to it now?", "Connect"):
                self.connect_to = bridge.bridge_id
                self.accept()

        def fail(e):
            tick.stop()
            self._pair_finished(f"Pairing failed: {e}")

        in_background(attempt, ok, fail)

    def _pair_finished(self, msg: str = ""):
        self.progress.setVisible(False)
        self.pair_btn.setEnabled(True)
        self.manual.setEnabled(True)
        self.pair_status.setText(msg)

    def reject(self):
        self._cancel_pairing = True
        super().reject()

    def accept(self):
        self._cancel_pairing = True
        super().accept()

