"""Left-hand navigation tree."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ..colors import light_rgb
from ..store import ResourceStore
from .widgets import dot_icon

SENSOR_TYPES = {"motion", "camera_motion", "temperature", "light_level", "contact", "tamper"}
SWITCH_TYPES = {"button", "relative_rotary", "bell_button"}
ID_ROLE = Qt.UserRole
BRIDGE_ITEM = "__bridge__"


class NavTree(QWidget):
    selected = Signal(object)  # resource id, BRIDGE_ITEM, or None

    def __init__(self, store: ResourceStore, parent=None):
        super().__init__(parent)
        self.store = store
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit(placeholderText="Filter…", clearButtonEnabled=True)
        self.filter.textChanged.connect(self._apply_filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._current_changed)
        lay.addWidget(self.filter)
        lay.addWidget(self.tree)
        self._items: dict[str, list[QTreeWidgetItem]] = {}
        self._collapsed: set[str] = set()
        self._rebuild_timer = QTimer(self, singleShot=True, interval=150)
        self._rebuild_timer.timeout.connect(self.rebuild)
        store.structure_changed.connect(self._rebuild_timer.start)
        store.changed.connect(self._resource_changed)
        self._warn_icon = QApplication.style().standardIcon(QStyle.SP_MessageBoxWarning)

    # -- building ----------------------------------------------------------

    def current_id(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, ID_ROLE) if item else None

    def rebuild(self) -> None:
        current = self.current_id()
        for i in range(self.tree.topLevelItemCount()):
            self._remember_collapsed(self.tree.topLevelItem(i))
        self.tree.blockSignals(True)
        self.tree.clear()
        self._items.clear()
        s = self.store
        if not s.all():
            self.tree.blockSignals(False)
            return

        devices = [d for d in s.of_type("device") if "bridge" not in s.service_types(d)]
        by_name = lambda r: s.name(r).lower()  # noqa: E731

        groups = self._category("Rooms & zones")
        for home in s.of_type("bridge_home"):
            self._leaf(groups, home, "All lights")
        for room in sorted(s.of_type("room"), key=by_name):
            self._leaf(groups, room)
        for zone in sorted(s.of_type("zone"), key=by_name):
            self._leaf(groups, zone, s.name(zone) + "  (zone)")

        lights_cat = self._category("Lights")
        light_devs = [d for d in devices if "light" in s.service_types(d)]
        rooms: dict[str, list[dict]] = {}
        for d in light_devs:
            room = s.room_of_device(d["id"])
            rooms.setdefault(s.name(room) if room else "No room", []).append(d)
        for room_name in sorted(rooms, key=lambda n: (n == "No room", n.lower())):
            folder = self._folder(lights_cat, room_name, "lights/" + room_name)
            for d in sorted(rooms[room_name], key=by_name):
                self._leaf(folder, d)

        scenes_cat = self._category("Scenes")
        by_group: dict[str, list[dict]] = {}
        for sc in s.of_type("scene", "smart_scene"):
            by_group.setdefault((sc.get("group") or {}).get("rid", ""), []).append(sc)
        for gid in sorted(by_group, key=lambda g: s.name(s.get(g)).lower()):
            folder = self._folder(scenes_cat, s.name(s.get(gid)) if s.get(gid) else "Other", "scenes/" + gid)
            for sc in sorted(by_group[gid], key=by_name):
                self._leaf(folder, sc)

        sensors = [d for d in devices if s.service_types(d) & SENSOR_TYPES and d not in light_devs]
        switches = [d for d in devices if s.service_types(d) & SWITCH_TYPES and d not in light_devs + sensors]
        others = [d for d in devices if d not in light_devs + sensors + switches]
        for title, items in (("Sensors", sensors), ("Switches", switches), ("Other devices", others)):
            if items:
                cat = self._category(title)
                for d in sorted(items, key=by_name):
                    self._leaf(cat, d)

        autos = s.of_type("behavior_instance")
        cat = self._category("Automations")
        for b in sorted(autos, key=by_name):
            self._leaf(cat, b)

        bridge = QTreeWidgetItem(self.tree, ["Bridge"])
        bridge.setData(0, ID_ROLE, BRIDGE_ITEM)
        self._items.setdefault(BRIDGE_ITEM, []).append(bridge)

        for i in range(self.tree.topLevelItemCount()):
            self._restore_collapsed(self.tree.topLevelItem(i))
        self._apply_filter(self.filter.text())
        self.tree.blockSignals(False)
        if current and current in self._items:
            self.tree.setCurrentItem(self._items[current][0])
        elif current:
            # The selected resource is gone (deleted elsewhere).
            self.selected.emit(None)

    def _category(self, title: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self.tree, [title])
        item.setFlags(Qt.ItemIsEnabled)
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        item.setData(0, Qt.UserRole + 1, "cat/" + title)
        item.setExpanded(True)
        return item

    def _folder(self, parent: QTreeWidgetItem, title: str, key: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [title])
        item.setFlags(Qt.ItemIsEnabled)
        item.setData(0, Qt.UserRole + 1, key)
        item.setExpanded(True)
        return item

    def _leaf(self, parent: QTreeWidgetItem, res: dict, label: str | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [label or self.store.name(res)])
        item.setData(0, ID_ROLE, res["id"])
        self._items.setdefault(res["id"], []).append(item)
        self._decorate(item, res)
        return item

    def _remember_collapsed(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.UserRole + 1)
        if key:
            (self._collapsed.discard if item.isExpanded() else self._collapsed.add)(key)
        for i in range(item.childCount()):
            self._remember_collapsed(item.child(i))

    def _restore_collapsed(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.UserRole + 1)
        if key:
            item.setExpanded(key not in self._collapsed)
        for i in range(item.childCount()):
            self._restore_collapsed(item.child(i))

    # -- decoration --------------------------------------------------------

    def _decorate(self, item: QTreeWidgetItem, res: dict) -> None:
        s = self.store
        rtype = res.get("type")
        tip = []
        icon = None
        if rtype == "device":
            lights = s.services(res, "light")
            problems = []
            for z in s.services(res, "zigbee_connectivity", "zgp_connectivity"):
                if z.get("status") not in ("connected", "unidirectional_incoming", None):
                    problems.append(f"Connectivity: {z['status'].replace('_', ' ')}")
            for p in s.services(res, "device_power"):
                ps = p.get("power_state") or {}
                if ps.get("battery_state") in ("low", "critical"):
                    problems.append(f"Battery {ps.get('battery_level', '?')} %")
            if problems:
                icon = self._warn_icon
                tip += problems
            elif lights:
                icon = dot_icon(light_rgb(lights[0]))
            room = s.room_of_device(res["id"])
            pd = res.get("product_data") or {}
            tip.insert(0, f"{pd.get('product_name', '')} — {s.name(room) if room else 'no room'}")
        elif rtype in ("room", "zone", "bridge_home"):
            gl = s.grouped_light(res)
            if gl:
                icon = dot_icon(light_rgb(gl) if (gl.get("on") or {}).get("on") else None)
        elif rtype == "scene":
            active = (res.get("status") or {}).get("active", "inactive") != "inactive"
            icon = dot_icon((255, 200, 80) if active else None, 10)
        elif rtype == "behavior_instance":
            icon = dot_icon((90, 180, 90) if res.get("enabled") else None, 10)
        if icon is not None:
            item.setIcon(0, icon)
        item.setToolTip(0, "\n".join(t for t in tip if t))

    def _resource_changed(self, rid: str) -> None:
        res = self.store.get(rid)
        if not res:
            return
        targets = {rid}
        owner = (res.get("owner") or {}).get("rid")
        if owner:
            targets.add(owner)
        for t in targets:
            for item in self._items.get(t, []):
                r = self.store.get(t)
                if r:
                    self._decorate(item, r)

    # -- selection & filter --------------------------------------------------

    def select(self, rid: str) -> bool:
        items = self._items.get(rid)
        if not items:
            return False
        self.tree.setCurrentItem(items[0])
        self.tree.scrollToItem(items[0])
        return True

    def _current_changed(self, cur: QTreeWidgetItem | None, _prev) -> None:
        rid = cur.data(0, ID_ROLE) if cur else None
        if rid:
            self.selected.emit(rid)

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            own = not text or text in item.text(0).lower()
            child_match = False
            for i in range(item.childCount()):
                child_match |= visit(item.child(i))
            leaf = item.data(0, ID_ROLE) is not None
            show = own if leaf else (child_match or (own and bool(text)) or not text)
            if not leaf and text and own:
                # Category/folder matched: show everything inside.
                for i in range(item.childCount()):
                    _show_all(item.child(i))
                show = True
            item.setHidden(not show)
            if text and show and not leaf:
                item.setExpanded(True)
            return show

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))


def _show_all(item: QTreeWidgetItem) -> None:
    item.setHidden(False)
    for i in range(item.childCount()):
        _show_all(item.child(i))
