"""Search for and pair new Zigbee devices (zigbee_device_discovery)."""

from __future__ import annotations

import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QVBoxLayout,
)

SERIAL_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


class DevicePairingDialog(QDialog):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.store = ctx.store
        self.setWindowTitle("Add devices")
        self.resize(520, 460)
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Make sure new lights are powered on and new sensors or switches are in pairing mode, "
            "then start the search. The bridge searches for about a minute.<br><br>"
            "Devices that were paired to another bridge need a reset first. If a device isn't found, "
            "enter the 6-character serial number printed on it (comma-separated, up to 10).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Serial numbers:"))
        self.serials = QLineEdit(placeholderText="optional, e.g. 1A2B3C, 4D5E6F")
        row.addWidget(self.serials)
        lay.addLayout(row)
        self.start_btn = QPushButton("Start search")
        self.start_btn.clicked.connect(self._start)
        lay.addWidget(self.start_btn)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.progress = QProgressBar(maximum=0, visible=False, textVisible=False)
        lay.addWidget(self.progress)
        lay.addWidget(QLabel("New devices:"))
        self.found = QListWidget()
        self.found.itemDoubleClicked.connect(lambda it: self.ctx.select(it.data(Qt.UserRole)))
        lay.addWidget(self.found)
        hint = QLabel("Double-click a device to rename it or put it in a room.")
        hint.setStyleSheet("color: gray")
        lay.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        lay.addWidget(buttons)

        self._started_at: float | None = None
        self._seen_active = False
        self.store.added.connect(self._added)
        self.store.changed.connect(self._changed)
        self._elapsed = QTimer(self, interval=1000)
        self._elapsed.timeout.connect(self._update_state)
        self._update_state()

    def _discovery(self) -> dict | None:
        return next(iter(self.store.of_type("zigbee_device_discovery")), None)

    def _start(self):
        disc = self._discovery()
        if not disc:
            self.state.setText("This bridge does not offer device discovery through the v2 API (firmware too old?).")
            return
        codes = [c.strip() for c in self.serials.text().replace(";", ",").split(",") if c.strip()]
        bad = [c for c in codes if not SERIAL_RE.match(c)]
        if bad or len(codes) > 10:
            self.state.setText("Serial numbers are 6 hexadecimal characters each, at most 10: "
                               + ", ".join(bad or codes[10:]))
            return
        action: dict = {"action_type": "search"}
        if codes:
            action["search_codes"] = [c.upper() for c in codes]
        self._started_at = time.monotonic()
        self._seen_active = False
        self.ctx.session.put("zigbee_device_discovery", disc["id"], {"action": action}, optimistic=False)
        self.start_btn.setEnabled(False)
        self.progress.setVisible(True)
        self._elapsed.start()
        self._update_state()

    def _update_state(self):
        disc = self._discovery()
        status = (disc or {}).get("status")
        if status == "active":
            self._seen_active = True
        searching = status == "active" or (self._started_at is not None and not self._seen_active
                                            and time.monotonic() - self._started_at < 10)
        if searching:
            secs = int(time.monotonic() - (self._started_at or time.monotonic()))
            self.state.setText(f"Searching… {secs} s")
            self.progress.setVisible(True)
            self.start_btn.setEnabled(False)
        else:
            if self._started_at is not None:
                n = self.found.count()
                self.state.setText(f"Search finished. {n} new device{'s' if n != 1 else ''} found."
                                   if n else "Search finished. No new devices found.")
                self._started_at = None
            self._elapsed.stop()
            self.progress.setVisible(False)
            self.start_btn.setEnabled(disc is not None)

    def _changed(self, rid: str):
        disc = self._discovery()
        if disc and disc["id"] == rid:
            self._update_state()

    def _added(self, rid: str):
        res = self.store.get(rid)
        if not res or res.get("type") != "device" or self._started_at is None:
            return
        pd = res.get("product_data") or {}
        item = QListWidgetItem(f"{self.store.name(res)} — {pd.get('product_name', '')}")
        item.setData(Qt.UserRole, rid)
        self.found.addItem(item)

    def closeEvent(self, e):
        self.store.added.disconnect(self._added)
        self.store.changed.disconnect(self._changed)
        self._elapsed.stop()
        super().closeEvent(e)
