"""Main window: bridge picker, navigation tree, detail pane."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from .. import api
from ..config import AppConfig, BridgeConfig
from ..session import BridgeSession
from ..store import ResourceStore
from ..tasks import in_background
from .nav import BRIDGE_ITEM, NavTree
from .pages import Page, create_scene_from_current, page_for


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.setWindowTitle("Hueman")
        self.resize(1150, 760)
        self.config = config
        self.store = ResourceStore(self)
        self.session: BridgeSession | None = None
        self.bridge_config: BridgeConfig | None = None
        self.page: Page | None = None
        self._pending_select: str | None = None
        self._pairing_dialog = None

        # -- toolbar
        tb = QToolBar("Main", movable=False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Bridge: "))
        self.bridge_combo = QComboBox(minimumWidth=240)
        self.bridge_combo.activated.connect(lambda _: self._connect(self.bridge_combo.currentData()))
        tb.addWidget(self.bridge_combo)
        self.conn_label = QLabel()
        self.conn_label.setContentsMargins(8, 0, 8, 0)
        tb.addWidget(self.conn_label)
        tb.addSeparator()

        self.act_refresh = QAction("Reload", self, shortcut=QKeySequence.Refresh,
                                   toolTip="Reload everything from the bridge")
        self.act_refresh.triggered.connect(lambda: self.session and self.session.refresh())
        tb.addAction(self.act_refresh)
        self.act_pair = QAction("Add devices…", self, toolTip="Search for new Zigbee devices to pair")
        self.act_pair.triggered.connect(self.open_device_pairing)
        tb.addAction(self.act_pair)

        new_menu = QMenu(self)
        new_menu.addAction("Room…", lambda: self._new_group("room"))
        new_menu.addAction("Zone…", lambda: self._new_group("zone"))
        new_menu.addAction("Scene from current lights…", self._new_scene)
        new_menu.addAction("Automation…", self._new_automation)
        self.new_btn = QToolButton(text="New", popupMode=QToolButton.InstantPopup)
        self.new_btn.setMenu(new_menu)
        tb.addWidget(self.new_btn)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        act_bridges = QAction("Bridges…", self, toolTip="Pair, edit and remove Hue Bridges")
        act_bridges.triggered.connect(self.open_bridges)
        tb.addAction(act_bridges)

        # -- body
        self.nav = NavTree(self.store)
        self.nav.selected.connect(self._show)
        self.scroll = QScrollArea(widgetResizable=True)
        splitter = QSplitter()
        splitter.addWidget(self.nav)
        splitter.addWidget(self.scroll)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 830])
        self.setCentralWidget(splitter)

        self._refresh_timer = QTimer(self, singleShot=True, interval=60)
        self._refresh_timer.timeout.connect(lambda: self.page and self.page.refresh())
        self.store.changed.connect(self._on_changed)
        self.store.removed.connect(self._on_removed)
        self.store.reset.connect(self._on_reset)
        self.store.added.connect(self._on_added)

        self._set_actions_enabled(False)
        self.fill_bridge_combo()
        start = self.config.find(self.config.last_bridge) or (self.config.bridges[0] if self.config.bridges else None)
        if start:
            self._connect(start.bridge_id)
        else:
            self._placeholder("No Hue Bridge is set up yet.", ("Add a bridge…", self.open_bridges))
            QTimer.singleShot(0, self.open_bridges)

    # -- context API used by pages ----------------------------------------

    def status(self, msg: str, error: bool = False) -> None:
        bar = self.statusBar()
        bar.setStyleSheet("color: #c0392b;" if error else "")
        bar.showMessage(msg, 15000 if error else 5000)

    def select(self, rid: str) -> None:
        if not self.nav.select(rid):
            self._show(rid)

    def select_when_available(self, rid: str | None) -> None:
        if not rid:
            return
        if self.store.get(rid):
            QTimer.singleShot(200, lambda: self.select(rid))
        else:
            self._pending_select = rid

    def open_device_pairing(self) -> None:
        if not self.session or not self.session.connected:
            return
        from .pairing_dialog import DevicePairingDialog
        if self._pairing_dialog is None or not self._pairing_dialog.isVisible():
            self._pairing_dialog = DevicePairingDialog(self, self)
        self._pairing_dialog.show()
        self._pairing_dialog.raise_()

    def open_bridges(self) -> None:
        from .bridges_dialog import BridgesDialog
        dlg = BridgesDialog(self.config, self)
        dlg.exec()
        self.config = AppConfig.load()
        self.fill_bridge_combo()
        current = self.bridge_config.bridge_id if self.bridge_config else None
        if dlg.connect_to:
            self._connect(dlg.connect_to)
        elif current and not self.config.find(current):
            # The active bridge was removed.
            self._disconnect()
            if self.config.bridges:
                self._connect(self.config.bridges[0].bridge_id)
            else:
                self._placeholder("No Hue Bridge is set up yet.", ("Add a bridge…", self.open_bridges))
        elif current:
            self.bridge_config = self.config.find(current)

    # -- connection --------------------------------------------------------

    def fill_bridge_combo(self) -> None:
        self.bridge_combo.clear()
        for b in self.config.bridges:
            self.bridge_combo.addItem(b.label, b.bridge_id)
        if self.bridge_config:
            self.bridge_combo.setCurrentIndex(self.bridge_combo.findData(self.bridge_config.bridge_id))
        self.bridge_combo.setEnabled(bool(self.config.bridges))

    def _disconnect(self) -> None:
        if self.session:
            self.session.stop()
            self.session.deleteLater()
        self.session = None
        self.store.clear()
        self._set_actions_enabled(False)

    def _connect(self, bridge_id: str) -> None:
        bridge = self.config.find(bridge_id)
        if not bridge:
            return
        self._disconnect()
        self.bridge_config = bridge
        self.config.last_bridge = bridge.bridge_id
        self.config.save()
        self.bridge_combo.setCurrentIndex(self.bridge_combo.findData(bridge.bridge_id))
        self._placeholder(f"Connecting to {bridge.label}…")
        if not bridge.cert_sha256:
            self._set_conn("connecting", "Checking certificate…")
            in_background(lambda: api.fetch_cert_fingerprint(bridge.host, bridge.port),
                          lambda fp: self._pin_and_start(bridge, fp),
                          lambda e: self._on_status("error", f"Cannot reach {bridge.host}: {e}"))
        else:
            self._start_session(bridge)

    def _pin_and_start(self, bridge: BridgeConfig, fp: str) -> None:
        if self.bridge_config is not bridge:
            return
        bridge.cert_sha256 = fp
        self.config.upsert(bridge)
        self.config.save()
        self._start_session(bridge)

    def _start_session(self, bridge: BridgeConfig) -> None:
        self.session = BridgeSession(bridge, self.store, self)
        self.session.status.connect(self._on_status)
        self.session.error.connect(lambda m: self.status(m, error=True))
        self.session.start()

    def _set_conn(self, state: str, msg: str) -> None:
        color = {"connected": "#27ae60", "connecting": "#f39c12", "reconnecting": "#f39c12"}.get(state, "#c0392b")
        self.conn_label.setText(f"<span style='color:{color}'>●</span> {state.capitalize()}")
        self.conn_label.setToolTip(msg)

    def _on_status(self, state: str, msg: str) -> None:
        self._set_conn(state, msg)
        if state == "connected":
            self._set_actions_enabled(True)
            if self.page is None:
                self._show(self.nav.current_id() or BRIDGE_ITEM)
        elif state == "error":
            self._set_actions_enabled(False)
            actions = [("Retry", lambda: self._connect(self.bridge_config.bridge_id))]
            if "certificate" in msg.lower():
                actions.append(("Trust new certificate…", self._retrust_certificate))
            if "not authorized" in msg.lower() or "pair" in msg.lower():
                actions.append(("Bridges…", self.open_bridges))
            self._placeholder(f"Could not connect to {self.bridge_config.label}.\n\n{msg}", *actions)
        elif state == "reconnecting":
            self.status(msg)

    def _retrust_certificate(self) -> None:
        bridge = self.bridge_config

        def got(fp: str):
            box = QMessageBox(QMessageBox.Warning, "Certificate changed",
                              f"{bridge.label} now presents a different TLS certificate.\n\n"
                              f"Pinned:  {bridge.cert_sha256}\nNew:     {fp}\n\n"
                              "This is expected after a bridge firmware update or factory reset. If neither "
                              "happened, someone may be intercepting traffic. Trust the new certificate?",
                              QMessageBox.Yes | QMessageBox.No, self)
            if box.exec() == QMessageBox.Yes:
                self._pin_and_start(bridge, fp)

        in_background(lambda: api.fetch_cert_fingerprint(bridge.host, bridge.port), got,
                      lambda e: self.status(str(e), error=True))

    def _set_actions_enabled(self, on: bool) -> None:
        for w in (self.act_refresh, self.act_pair, self.new_btn):
            w.setEnabled(on)

    # -- detail pane -------------------------------------------------------

    def _placeholder(self, text: str, *buttons) -> None:
        self.page = None
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        label = QLabel(text, alignment=Qt.AlignCenter, wordWrap=True)
        lay.addWidget(label)
        for title, fn in buttons:
            b = QPushButton(title)
            b.clicked.connect(fn)
            lay.addWidget(b, alignment=Qt.AlignCenter)
        self.scroll.setWidget(w)

    def _show(self, rid) -> None:
        if not self.session or not self.store.all():
            return
        if rid is None:
            self._placeholder("Select something on the left.")
            return
        if rid != BRIDGE_ITEM and not self.store.get(rid):
            return
        self.page = page_for(self, rid)
        self.scroll.setWidget(self.page)

    def _on_changed(self, rid: str) -> None:
        if self.page and rid in self.page.watched():
            self._refresh_timer.start()

    def _on_removed(self, rid: str, _rtype: str) -> None:
        if self.page and self.page.rid == rid:
            self._placeholder("This item was deleted.")
        elif self.page and rid in self.page.watched():
            self._show(self.page.rid)

    def _on_added(self, rid: str) -> None:
        if self._pending_select == rid:
            self._pending_select = None
            QTimer.singleShot(300, lambda: self.select(rid))

    def _on_reset(self) -> None:
        if self.page and self.store.all():
            if self.page.rid and not self.store.get(self.page.rid):
                self._placeholder("This item no longer exists.")
            else:
                self._show(self.page.rid or BRIDGE_ITEM)

    # -- creation ----------------------------------------------------------

    def _new_group(self, rtype: str) -> None:
        from .create_dialogs import NewGroupDialog
        dlg = NewGroupDialog(self, rtype, self)
        if dlg.exec():
            # A device can only be in one room: take chosen devices out of their current rooms first.
            for old, rest in dlg.moves():
                self.session.put("room", old["id"], {"children": rest})
            self.session.post(rtype, dlg.payload(),
                              on_done=lambda data: self.select_when_available(data[0]["rid"] if data else None))

    def _new_scene(self) -> None:
        from .create_dialogs import pick_group
        group = pick_group(self, self.store)
        if group:
            create_scene_from_current(self, self, group)

    def _new_automation(self) -> None:
        from .create_dialogs import NewAutomationDialog
        dlg = NewAutomationDialog(self, self.store)
        if dlg.exec():
            self.session.post("behavior_instance", dlg.payload(),
                              on_done=lambda data: self.select_when_available(data[0]["rid"] if data else None))

    def closeEvent(self, e):
        if self.session:
            self.session.stop()
        super().closeEvent(e)
