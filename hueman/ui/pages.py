"""Right-hand detail pages, one per kind of entity."""

from __future__ import annotations

import copy
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHeaderView, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSpinBox, QTableWidget, QVBoxLayout,
    QWidget,
)

from ..colors import action_rgb, light_rgb, mirek_to_kelvin, kelvin_to_mirek
from .widgets import (
    FIELD_WIDTH, LIGHT_ARCHETYPES, LIGHT_FUNCTION_HELP, POWERUP_HELP, ROOM_ARCHETYPES, ColorButton, Collapsible, EnumCombo, JsonView, LightControls, NameEdit,
    Section, SelectableLabel, confirm, dot_icon, explained, hbox, pretty, set_checked,
)


class Page(QWidget):
    """Base class. Subclasses build widgets once and update them in ``refresh``."""

    def __init__(self, ctx, rid: str | None):
        super().__init__()
        self.ctx, self.rid = ctx, rid
        self.store, self.session = ctx.store, ctx.session
        self.layout_ = QVBoxLayout(self)
        self.layout_.setAlignment(Qt.AlignTop)
        self._refreshers: list = []

    @property
    def res(self) -> dict:
        return self.store.get(self.rid) or {}

    def watched(self) -> set[str]:
        """Resource ids whose change should refresh this page."""
        return {self.rid} if self.rid else set()

    def add(self, w: QWidget) -> QWidget:
        self.layout_.addWidget(w)
        return w

    def on_refresh(self, fn) -> None:
        self._refreshers.append(fn)

    def refresh(self) -> None:
        for fn in self._refreshers:
            fn()

    def title(self, text: str, subtitle: str = "") -> None:
        t = QLabel(f"<h2 style='margin:0'>{_esc(text)}</h2>" + (f"<span>{_esc(subtitle)}</span>" if subtitle else ""))
        t.setTextFormat(Qt.RichText)
        self.add(t)

    def raw_json(self, ids_fn) -> None:
        view = JsonView()
        self.add(Collapsible("Raw data", view))
        self.on_refresh(lambda: view.set_data([self.store.get(i) for i in ids_fn() if self.store.get(i)]))

    def delete_button(self, label: str, rtype: str, what: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setStyleSheet("QPushButton { color: #c0392b; }")

        def go():
            if confirm(self, label, f"{what}\n\nThis is applied to the bridge immediately and cannot be undone."):
                self.session.delete(rtype, self.rid, on_done=lambda _: self.ctx.status(f"Deleted {self.store.name(self.res) or rtype}"))

        btn.clicked.connect(go)
        return btn


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    return iso.replace("T", " ").split(".")[0].removesuffix("Z") + " UTC"


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class DevicePage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        dev = self.res
        pd = dev.get("product_data") or {}
        is_bridge = "bridge" in self.store.service_types(dev)
        self.title(self.store.name(dev), f"{pd.get('product_name', '')} · {pd.get('manufacturer_name', '')}")

        # -- general
        sec = self.add(Section("Device"))
        name = NameEdit(self.store.name(dev))
        name.committed.connect(lambda v: self.session.put("device", rid, {"metadata": {"name": v}}))
        sec.row("Name", name)
        self.on_refresh(lambda: name.set_value(self.store.name(self.res)))

        if not is_bridge and self.store.services(dev, "light"):
            arche = EnumCombo(LIGHT_ARCHETYPES, (dev.get("metadata") or {}).get("archetype"))
            arche.chosen.connect(lambda v: self.session.put("device", rid, {"metadata": {"archetype": v}}))
            sec.row("Type", arche)
            self.on_refresh(lambda: arche.set_value((self.res.get("metadata") or {}).get("archetype")))

        if not is_bridge:
            self.room_combo = QComboBox()
            self.room_combo.setFixedWidth(FIELD_WIDTH)
            self.room_combo.activated.connect(self._move_to_room)
            sec.row("Room", self.room_combo)
            self.on_refresh(self._fill_rooms)

        sec.row("Model", f"{pd.get('product_name', '?')} ({pd.get('model_id', '?')})")
        sec.row("Firmware", pd.get("software_version", "—"))
        status = sec.row("Status", SelectableLabel())
        self.on_refresh(lambda: status.setText(self._status_text()))

        buttons = []
        if "identify" in dev:
            ident = QPushButton("Identify (blink)")
            ident.clicked.connect(lambda: self.session.put("device", rid, {"identify": {"action": "identify"}},
                                                           optimistic=False))
            buttons.append(ident)
        if not is_bridge:
            buttons.append(self.delete_button(
                "Delete device", "device",
                f"Remove “{self.store.name(dev)}” from the bridge?\n\nIt will disappear from all rooms, zones "
                "and scenes. To use it again you will have to reset and pair it."))
        if buttons:
            self.add(hbox(*buttons, stretch=True))

        # -- one section per service
        for light in self.store.services(dev, "light"):
            self._light_section(light)
        for motion in self.store.services(dev, "motion", "camera_motion"):
            self._motion_section(motion)
        for svc in self.store.services(dev, "temperature"):
            self._sensor_section(svc, "Temperature",
                                 lambda r: f"{(r.get('temperature') or {}).get('temperature', '?')} °C",
                                 lambda r: ((r.get("temperature") or {}).get("temperature_report") or {}).get("changed"))
        for svc in self.store.services(dev, "light_level"):
            self._sensor_section(svc, "Light level", _lux_text,
                                 lambda r: ((r.get("light") or {}).get("light_level_report") or {}).get("changed"))
        for svc in self.store.services(dev, "contact"):
            self._sensor_section(svc, "Contact",
                                 lambda r: pretty(((r.get("contact_report") or {}).get("state"))),
                                 lambda r: (r.get("contact_report") or {}).get("changed"))
        for svc in self.store.services(dev, "tamper"):
            self._sensor_section(svc, "Tamper",
                                 lambda r: ", ".join(pretty(t.get("state")) for t in r.get("tamper_reports", [])) or "—",
                                 lambda r: None, toggle=False)
        buttons_svc = self.store.services(dev, "button")
        if buttons_svc:
            self._buttons_section(buttons_svc)
        for svc in self.store.services(dev, "relative_rotary"):
            self._sensor_section(svc, "Rotary dial",
                                 lambda r: pretty(((r.get("relative_rotary") or {}).get("rotary_report") or {})
                                                  .get("action")), lambda r: None, toggle=False)

        self.raw_json(lambda: [rid] + [s["rid"] for s in self.res.get("services", [])])
        self.refresh()

    def watched(self) -> set[str]:
        ids = {self.rid} | {s["rid"] for s in self.res.get("services", [])}
        ids |= {r["id"] for r in self.store.of_type("room")}
        return ids

    def _status_text(self) -> str:
        dev = self.res
        parts = []
        for z in self.store.services(dev, "zigbee_connectivity", "zgp_connectivity", "wifi_connectivity"):
            parts.append(pretty(z.get("status")) + (f"  ({z['mac_address']})" if z.get("mac_address") else ""))
        for p in self.store.services(dev, "device_power"):
            ps = p.get("power_state") or {}
            if "battery_level" in ps:
                parts.append(f"Battery {ps['battery_level']} % ({pretty(ps.get('battery_state'))})")
        for u in self.store.services(dev, "device_software_update"):
            if u.get("state") and u["state"] != "no_update":
                parts.append(f"Firmware: {pretty(u['state'])}")
        return "\n".join(parts) or "—"

    def _fill_rooms(self):
        combo = self.room_combo
        current = self.store.room_of_device(self.rid)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(no room)", "")
        for room in sorted(self.store.of_type("room"), key=self.store.name):
            combo.addItem(self.store.name(room), room["id"])
        combo.setCurrentIndex(combo.findData(current["id"]) if current else 0)
        combo.blockSignals(False)

    def _move_to_room(self, _index: int):
        new_id = self.room_combo.currentData()
        old = self.store.room_of_device(self.rid)
        if old and old["id"] == new_id:
            return
        if old:
            children = [c for c in old.get("children", []) if c.get("rid") != self.rid]
            self.session.put("room", old["id"], {"children": children})
        if new_id:
            new = self.store.get(new_id)
            children = list(new.get("children", [])) + [{"rid": self.rid, "rtype": "device"}]
            self.session.put("room", new_id, {"children": children})

    def _light_section(self, light: dict):
        lid = light["id"]
        title = "Light" if len(self.store.services(self.res, "light")) == 1 else f"Light: {self.store.name(light)}"
        sec = self.add(Section(title))
        controls = LightControls(self.ctx, "light", lid, sec)
        self.on_refresh(controls.refresh)

        func = EnumCombo(["functional", "decorative", "mixed"], (light.get("metadata") or {}).get("function"))
        func.chosen.connect(lambda v: self.session.put("light", lid, {"metadata": {"function": v}}))
        sec.row("Function", explained(func, *LIGHT_FUNCTION_HELP))
        self.on_refresh(lambda: func.set_value(((self.store.get(lid) or {}).get("metadata") or {}).get("function")))

        if "powerup" in light:
            pu = EnumCombo(["safety", "powerfail", "last_on_state"], (light.get("powerup") or {}).get("preset"))
            pu.chosen.connect(lambda v: self.session.put("light", lid, {"powerup": {"preset": v}}))
            sec.row("After power loss", explained(pu, *POWERUP_HELP))
            self.on_refresh(lambda: pu.set_value(((self.store.get(lid) or {}).get("powerup") or {}).get("preset")))

    def _motion_section(self, motion: dict):
        mid, rtype = motion["id"], motion["type"]
        sec = self.add(Section("Motion sensor"))
        enabled = QCheckBox("Enabled")
        enabled.clicked.connect(lambda v: self.session.put(rtype, mid, {"enabled": bool(v)}))
        sec.row("Sensor", enabled)
        sens = None
        if motion.get("sensitivity"):
            sens = QSlider(Qt.Horizontal)
            sens.setRange(0, (motion["sensitivity"].get("sensitivity_max") or 4))
            sens.setPageStep(1)
            sens.setTickPosition(QSlider.TicksBelow)
            sens_label = QLabel()

            def sens_changed(v):
                sens_label.setText(str(v))
                self.session.put(rtype, mid, {"sensitivity": {"sensitivity": v}})

            sens.valueChanged.connect(sens_changed)
            sec.row("Sensitivity", hbox(QLabel("low"), sens, QLabel("high"), sens_label))
        state = sec.row("Motion", SelectableLabel())

        def refresh():
            m = self.store.get(mid) or {}
            set_checked(enabled, m.get("enabled", True))
            if sens is not None and not sens.isSliderDown():
                v = (m.get("sensitivity") or {}).get("sensitivity", 0)
                sens.blockSignals(True)
                sens.setValue(v)
                sens.blockSignals(False)
                sens_label.setText(str(v))
            rep = (m.get("motion") or {}).get("motion_report") or {}
            moving = rep.get("motion", (m.get("motion") or {}).get("motion"))
            state.setText(("Motion detected" if moving else "No motion") + f" · since {_fmt_time(rep.get('changed'))}")

        self.on_refresh(refresh)

    def _sensor_section(self, svc: dict, title: str, value_fn, changed_fn, toggle: bool = True):
        sid, rtype = svc["id"], svc["type"]
        sec = self.add(Section(title))
        enabled = None
        if toggle and "enabled" in svc:
            enabled = QCheckBox("Enabled")
            enabled.clicked.connect(lambda v: self.session.put(rtype, sid, {"enabled": bool(v)}))
            sec.row("Sensor", enabled)
        value = sec.row("Value", SelectableLabel())

        def refresh():
            r = self.store.get(sid) or {}
            if enabled is not None:
                set_checked(enabled, r.get("enabled", True))
            changed = changed_fn(r)
            value.setText(value_fn(r) + (f"  (updated {_fmt_time(changed)})" if changed else ""))

        self.on_refresh(refresh)

    def _buttons_section(self, buttons: list[dict]):
        sec = self.add(Section("Buttons"))
        table = QTableWidget(len(buttons), 3)
        table.setHorizontalHeaderLabels(["Button", "Last event", "When"])
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setMaximumHeight(40 + 30 * len(buttons))
        sec.form.addRow(table)
        ids = [b["id"] for b in sorted(buttons, key=lambda b: (b.get("metadata") or {}).get("control_id", 0))]

        def refresh():
            from PySide6.QtWidgets import QTableWidgetItem
            for row, bid in enumerate(ids):
                b = self.store.get(bid) or {}
                rep = (b.get("button") or {}).get("button_report") or {}
                table.setItem(row, 0, QTableWidgetItem(str((b.get("metadata") or {}).get("control_id", row + 1))))
                table.setItem(row, 1, QTableWidgetItem(pretty(rep.get("event") or (b.get("button") or {}).get("last_event"))))
                table.setItem(row, 2, QTableWidgetItem(_fmt_time(rep.get("updated"))))

        self.on_refresh(refresh)
        note = QLabel("Button actions are configured as automations in the Hue app; "
                      "they appear under “Automations” when exposed by the bridge.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        sec.form.addRow(note)


def _lux_text(r: dict) -> str:
    lvl = (r.get("light") or {}).get("light_level")
    if lvl is None:
        lvl = ((r.get("light") or {}).get("light_level_report") or {}).get("light_level")
    if lvl is None:
        return "?"
    lux = 10 ** ((lvl - 1) / 10000)
    return f"{lux:.0f} lx"


# ---------------------------------------------------------------------------
# Rooms, zones, all lights
# ---------------------------------------------------------------------------

class GroupPage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        g = self.res
        rtype = g["type"]
        self.rtype = rtype
        self.title(self.store.name(g), {"room": "Room", "zone": "Zone", "bridge_home": "Every light on the bridge"}[rtype])

        if rtype in ("room", "zone"):
            sec = self.add(Section("General"))
            name = NameEdit(self.store.name(g))
            name.committed.connect(lambda v: self.session.put(rtype, rid, {"metadata": {"name": v}}))
            sec.row("Name", name)
            arche = EnumCombo(ROOM_ARCHETYPES, (g.get("metadata") or {}).get("archetype"))
            arche.chosen.connect(lambda v: self.session.put(rtype, rid, {"metadata": {"archetype": v}}))
            sec.row("Type", arche)
            self.on_refresh(lambda: name.set_value(self.store.name(self.res)))
            self.on_refresh(lambda: arche.set_value((self.res.get("metadata") or {}).get("archetype")))

        gl = self.store.grouped_light(g)
        if gl:
            sec = self.add(Section("Lights"))
            controls = LightControls(ctx, "grouped_light", gl["id"], sec, self.store.lights_in_group(g))
            note = QLabel("Controls every light in the group. The bridge accepts at most one group command per second.")
            note.setStyleSheet("color: gray")
            note.setWordWrap(True)
            sec.form.addRow(note)
            self.on_refresh(controls.refresh)

        if rtype in ("room", "zone"):
            self._members_section()
            self._scenes_section()
            self.add(hbox(self.delete_button(
                f"Delete {rtype}", rtype,
                f"Delete the {rtype} “{self.store.name(g)}”?\n\nIts devices stay paired, but scenes that belong "
                f"to this {rtype} are deleted too."), stretch=True))
        self.raw_json(lambda: [rid] + ([gl["id"]] if gl else []))
        self.refresh()

    def watched(self) -> set[str]:
        g = self.res
        ids = {self.rid} | {s["rid"] for s in g.get("services", [])}
        ids |= {light["id"] for light in self.store.lights_in_group(g)}
        ids |= {s["id"] for s in self.store.scenes_for_group(self.rid)}
        return ids

    def _members_section(self):
        sec = self.add(Section("Devices in this room" if self.rtype == "room" else "Lights in this zone"))
        lst = QListWidget()
        lst.setMinimumHeight(160)
        sec.form.addRow(lst)
        hint = QLabel("A device can only be in one room; ticking a device that is elsewhere moves it here."
                      if self.rtype == "room" else "A light can be in any number of zones.")
        hint.setStyleSheet("color: gray")
        hint.setWordWrap(True)
        sec.form.addRow(hint)

        def refresh():
            lst.blockSignals(True)
            lst.clear()
            g = self.res
            member_ids = {c.get("rid") for c in g.get("children", [])}
            if self.rtype == "room":
                candidates = [d for d in self.store.of_type("device") if "bridge" not in self.store.service_types(d)]
            else:
                candidates = self.store.of_type("light")
            for c in sorted(candidates, key=lambda x: (x["id"] not in member_ids, self.store.name(x).lower())):
                label = self.store.name(c)
                if self.rtype == "room":
                    other = self.store.room_of_device(c["id"])
                    if other and other["id"] != self.rid:
                        label += f"   (in {self.store.name(other)})"
                    lights = self.store.services(c, "light")
                    icon = dot_icon(light_rgb(lights[0])) if lights else dot_icon(None)
                else:
                    other = self.store.room_of_device((c.get("owner") or {}).get("rid", ""))
                    if other:
                        label += f"   ({self.store.name(other)})"
                    icon = dot_icon(light_rgb(c))
                item = QListWidgetItem(icon, label)
                item.setData(Qt.UserRole, c["id"])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if c["id"] in member_ids else Qt.Unchecked)
                lst.addItem(item)
            lst.blockSignals(False)

        def changed(item: QListWidgetItem):
            cid = item.data(Qt.UserRole)
            checked = item.checkState() == Qt.Checked
            children = [c for c in self.res.get("children", []) if c.get("rid") != cid]
            if checked:
                if self.rtype == "room":
                    other = self.store.room_of_device(cid)
                    if other and other["id"] != self.rid:
                        rest = [c for c in other.get("children", []) if c.get("rid") != cid]
                        self.session.put("room", other["id"], {"children": rest})
                children.append({"rid": cid, "rtype": "device" if self.rtype == "room" else "light"})
            self.session.put(self.rtype, self.rid, {"children": children})

        lst.itemChanged.connect(changed)
        self.on_refresh(refresh)

    def _scenes_section(self):
        sec = self.add(Section("Scenes"))
        lst = QListWidget()
        lst.setMinimumHeight(120)
        sec.form.addRow(lst)
        recall = QPushButton("Activate")
        open_ = QPushButton("Edit…")
        new = QPushButton("New scene from current lights…")
        sec.form.addRow(hbox(recall, open_, new, stretch=True))

        def refresh():
            sel = lst.currentItem().data(Qt.UserRole) if lst.currentItem() else None
            lst.clear()
            for s in sorted(self.store.scenes_for_group(self.rid), key=self.store.name):
                active = (s.get("status") or {}).get("active", "inactive") not in ("inactive", None)
                if s["type"] == "smart_scene":
                    active = s.get("state") == "active"
                item = QListWidgetItem(("● " if active else "   ") + self.store.name(s))
                item.setData(Qt.UserRole, s["id"])
                lst.addItem(item)
                if s["id"] == sel:
                    lst.setCurrentItem(item)

        def do_recall():
            item = lst.currentItem()
            if item:
                recall_scene(self.ctx, self.store.get(item.data(Qt.UserRole)))

        recall.clicked.connect(do_recall)
        lst.itemDoubleClicked.connect(lambda _: do_recall())
        open_.clicked.connect(lambda: lst.currentItem() and self.ctx.select(lst.currentItem().data(Qt.UserRole)))
        new.clicked.connect(lambda: create_scene_from_current(self.ctx, self, self.res))
        self.on_refresh(refresh)


def recall_scene(ctx, scene: dict | None) -> None:
    if not scene:
        return
    if scene["type"] == "smart_scene":
        ctx.session.put("smart_scene", scene["id"], {"recall": {"action": "activate"}}, optimistic=False)
    else:
        ctx.session.put("scene", scene["id"], {"recall": {"action": "active"}}, optimistic=False)
    ctx.status(f"Activated scene “{ctx.store.name(scene)}”")


def current_light_actions(store, group: dict) -> list[dict]:
    actions = []
    for light in store.lights_in_group(group):
        action: dict = {"on": {"on": bool((light.get("on") or {}).get("on"))}}
        if action["on"]["on"]:
            if "dimming" in light and light["dimming"].get("brightness") is not None:
                action["dimming"] = {"brightness": light["dimming"]["brightness"]}
            ct = light.get("color_temperature") or {}
            xy = (light.get("color") or {}).get("xy")
            if ct.get("mirek_valid") and ct.get("mirek"):
                action["color_temperature"] = {"mirek": ct["mirek"]}
            elif xy:
                action["color"] = {"xy": xy}
        actions.append({"target": {"rid": light["id"], "rtype": "light"}, "action": action})
    return actions


def create_scene_from_current(ctx, parent: QWidget, group: dict) -> None:
    name, ok = QInputDialog.getText(parent, "New scene",
                                    f"Name for a scene that stores the current state of the lights in "
                                    f"“{ctx.store.name(group)}”:")
    name = name.strip()[:32]
    if not ok or not name:
        return
    actions = current_light_actions(ctx.store, group)
    if not actions:
        QMessageBox.information(parent, "New scene", "This group has no lights.")
        return
    ctx.session.post("scene", {"type": "scene", "metadata": {"name": name},
                               "group": {"rid": group["id"], "rtype": group["type"]}, "actions": actions},
                     on_done=lambda data: ctx.select_when_available(data[0]["rid"] if data else None))


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

class ScenePage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        s = self.res
        group = self.store.resolve(s.get("group"))
        self.title(self.store.name(s), f"Scene in {self.store.name(group)}")

        sec = self.add(Section("General"))
        name = NameEdit(self.store.name(s))
        name.committed.connect(lambda v: self.session.put("scene", rid, {"metadata": {"name": v}}))
        sec.row("Name", name)
        self.on_refresh(lambda: name.set_value(self.store.name(self.res)))
        goto = QPushButton(self.store.name(group))
        goto.setFlat(True)
        goto.setStyleSheet("text-align: left; text-decoration: underline;")
        goto.clicked.connect(lambda: group and self.ctx.select(group["id"]))
        sec.row("Belongs to", hbox(goto, stretch=True))
        state = sec.row("State", SelectableLabel())
        self.on_refresh(lambda: state.setText(pretty((self.res.get("status") or {}).get("active"))))

        has_palette = bool(((s.get("palette") or {}).get("color")))
        speed = QSlider(Qt.Horizontal)
        speed.setRange(0, 100)
        speed.valueChanged.connect(lambda v: self.session.put("scene", rid, {"speed": v / 100.0}))
        sec.row("Dynamic speed", speed)
        auto_dyn = QCheckBox("Start dynamically when activated")
        auto_dyn.clicked.connect(lambda v: self.session.put("scene", rid, {"auto_dynamic": bool(v)}))
        sec.row("", auto_dyn)
        speed.setEnabled(has_palette)
        auto_dyn.setEnabled(has_palette)

        def refresh_general():
            if not speed.isSliderDown():
                speed.blockSignals(True)
                speed.setValue(round((self.res.get("speed") or 0) * 100))
                speed.blockSignals(False)
            set_checked(auto_dyn, self.res.get("auto_dynamic", False))

        self.on_refresh(refresh_general)

        recall = QPushButton("Activate")
        recall.clicked.connect(lambda: recall_scene(self.ctx, self.res))
        dyn = QPushButton("Activate dynamic")
        dyn.setEnabled(has_palette)
        dyn.clicked.connect(lambda: self.session.put("scene", rid, {"recall": {"action": "dynamic_palette"}},
                                                     optimistic=False))
        capture = QPushButton("Store current light states")
        capture.setToolTip("Overwrite this scene with what the lights in the group are doing right now")
        capture.clicked.connect(self._capture)
        self.add(hbox(recall, dyn, capture, stretch=True))

        self._actions_section()
        self.add(hbox(self.delete_button("Delete scene", "scene", f"Delete the scene “{self.store.name(s)}”?"),
                      stretch=True))
        self.raw_json(lambda: [rid])
        self.refresh()

    def watched(self) -> set[str]:
        return {self.rid} | {a["target"]["rid"] for a in self.res.get("actions", [])}

    def _capture(self):
        group = self.store.resolve(self.res.get("group"))
        if group and confirm(self, "Store current light states",
                             f"Replace the stored settings of “{self.store.name(self.res)}” with the current state "
                             f"of the lights in “{self.store.name(group)}”?", "Store"):
            self.session.put("scene", self.rid, {"actions": current_light_actions(self.store, group)})

    def _put_actions(self, mutate):
        actions = copy.deepcopy(self.res.get("actions", []))
        mutate(actions)
        self.session.put("scene", self.rid, {"actions": actions})

    def _actions_section(self):
        sec = self.add(Section("Lights in this scene"))
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Light", "On", "Brightness", "Colour / white"])
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setMinimumHeight(200)
        sec.form.addRow(table)
        self._table = table
        self._rows_for: list[str] = []
        self.on_refresh(self._refresh_actions)

    def _refresh_actions(self):
        from PySide6.QtWidgets import QTableWidgetItem
        table = self._table
        actions = self.res.get("actions", [])
        targets = [a["target"]["rid"] for a in actions]
        rebuild = targets != self._rows_for
        if rebuild:
            table.setRowCount(len(actions))
            self._rows_for = targets
        for row, a in enumerate(actions):
            light = self.store.get(a["target"]["rid"]) or {}
            act = a.get("action") or {}
            on = (act.get("on") or {}).get("on", True)
            if rebuild:
                item = QTableWidgetItem(self.store.name(light))
                item.setFlags(Qt.ItemIsEnabled)
                table.setItem(row, 0, item)
                box = QCheckBox()
                box.clicked.connect(lambda v, r=row: self._put_actions(
                    lambda acts: acts[r]["action"].__setitem__("on", {"on": bool(v)})))
                table.setCellWidget(row, 1, hbox(box))
                if "dimming" in light:
                    spin = QDoubleSpinBox()
                    spin.setRange(0, 100)
                    spin.setDecimals(0)
                    spin.setSuffix(" %")
                    spin.editingFinished.connect(lambda r=row, s=spin: self._put_actions(
                        lambda acts: acts[r]["action"].__setitem__("dimming", {"brightness": s.value()})))
                    table.setCellWidget(row, 2, spin)
                else:
                    table.setCellWidget(row, 2, QLabel("—", alignment=Qt.AlignCenter))
                table.setCellWidget(row, 3, self._color_cell(row, light))
            table.item(row, 0).setIcon(dot_icon(action_rgb(act)))
            box = table.cellWidget(row, 1).findChild(QCheckBox)
            set_checked(box, on)
            spin = table.cellWidget(row, 2)
            if isinstance(spin, QDoubleSpinBox) and not spin.hasFocus():
                spin.blockSignals(True)
                spin.setValue((act.get("dimming") or {}).get("brightness", 100))
                spin.blockSignals(False)
            btn = table.cellWidget(row, 3).findChild(ColorButton)
            if btn:
                btn.set_rgb(action_rgb({**act, "on": {"on": True}}))
            kspin = table.cellWidget(row, 3).findChild(QSpinBox)
            if kspin and not kspin.hasFocus():
                mirek = (act.get("color_temperature") or {}).get("mirek")
                kspin.blockSignals(True)
                kspin.setValue(mirek_to_kelvin(mirek) if mirek else kspin.minimum())
                kspin.setSpecialValueText("—")
                kspin.blockSignals(False)

    def _color_cell(self, row: int, light: dict) -> QWidget:
        widgets = []
        if "color" in light:
            btn = ColorButton()

            def set_xy(x, y, r=row):
                def mutate(acts):
                    acts[r]["action"].pop("color_temperature", None)
                    acts[r]["action"]["color"] = {"xy": {"x": x, "y": y}}
                self._put_actions(mutate)

            btn.picked.connect(set_xy)
            widgets.append(btn)
        if "color_temperature" in light:
            schema = (light["color_temperature"].get("mirek_schema") or {})
            kspin = QSpinBox()
            kspin.setRange(mirek_to_kelvin(schema.get("mirek_maximum", 500)),
                           mirek_to_kelvin(schema.get("mirek_minimum", 153)))
            kspin.setSingleStep(100)
            kspin.setSuffix(" K")

            def set_ct(r=row, s=kspin):
                def mutate(acts):
                    acts[r]["action"].pop("color", None)
                    acts[r]["action"]["color_temperature"] = {"mirek": kelvin_to_mirek(s.value())}
                self._put_actions(mutate)

            kspin.editingFinished.connect(set_ct)
            widgets.append(kspin)
        if not widgets:
            widgets.append(QLabel("—"))
        return hbox(*widgets)


class SmartScenePage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        s = self.res
        group = self.store.resolve(s.get("group"))
        self.title(self.store.name(s), f"Smart scene in {self.store.name(group)}")
        sec = self.add(Section("General"))
        name = NameEdit(self.store.name(s))
        name.committed.connect(lambda v: self.session.put("smart_scene", rid, {"metadata": {"name": v}}))
        sec.row("Name", name)
        state = sec.row("State", SelectableLabel())
        self.on_refresh(lambda: state.setText(pretty(self.res.get("state"))))
        on = QPushButton("Activate")
        on.clicked.connect(lambda: self.session.put("smart_scene", rid, {"recall": {"action": "activate"}},
                                                    optimistic=False))
        off = QPushButton("Deactivate")
        off.clicked.connect(lambda: self.session.put("smart_scene", rid, {"recall": {"action": "deactivate"}},
                                                     optimistic=False))
        self.add(hbox(on, off, stretch=True))
        note = QLabel("The time-slot schedule of smart scenes is shown below as raw data; edit it in the Hue app.")
        note.setStyleSheet("color: gray")
        self.add(note)
        self.add(hbox(self.delete_button("Delete smart scene", "smart_scene",
                                         f"Delete the smart scene “{self.store.name(s)}”?"), stretch=True))
        self.raw_json(lambda: [rid])
        self.refresh()


# ---------------------------------------------------------------------------
# Automations
# ---------------------------------------------------------------------------

def _refs_in(obj) -> list[dict]:
    out = []
    if isinstance(obj, dict):
        if "rid" in obj and "rtype" in obj:
            out.append(obj)
        for v in obj.values():
            out += _refs_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _refs_in(v)
    return out


class AutomationPage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        b = self.res
        script = self.store.get(b.get("script_id"))
        self.title(self.store.name(b), f"Automation · {self.store.name(script) if script else 'unknown script'}")

        sec = self.add(Section("General"))
        name = NameEdit(self.store.name(b))
        name.committed.connect(lambda v: self.session.put("behavior_instance", rid, {"metadata": {"name": v}}))
        sec.row("Name", name)
        self.on_refresh(lambda: name.set_value(self.store.name(self.res)))
        enabled = QCheckBox("Enabled")
        enabled.clicked.connect(lambda v: self.session.put("behavior_instance", rid, {"enabled": bool(v)}))
        sec.row("", enabled)
        if script:
            sec.row("Script", f"{self.store.name(script)} — {script.get('description', '')}")
        status = sec.row("Status", SelectableLabel())
        refs = sec.row("Affects", SelectableLabel())

        def refresh_general():
            r = self.res
            set_checked(enabled, r.get("enabled", False))
            err = r.get("last_error")
            status.setText(pretty(r.get("status")) + (f" — {err}" if err else ""))
            names = []
            for ref in _refs_in(r.get("configuration")):
                target = self.store.get(ref["rid"])
                label = f"{self.store.name(target)} ({pretty(ref['rtype'])})" if target else f"missing {ref['rtype']}"
                if label not in names:
                    names.append(label)
            refs.setText(", ".join(names) or "—")

        self.on_refresh(refresh_general)

        cfg = self.add(Section("Configuration (JSON)"))
        self.editor = QPlainTextEdit()
        f = self.editor.font()
        f.setFamily("monospace")
        self.editor.setFont(f)
        self.editor.setMinimumHeight(260)
        self.editor.setPlainText(json.dumps(b.get("configuration", {}), indent=2, ensure_ascii=False))
        cfg.form.addRow(self.editor)
        self.dirty = QLabel("")
        apply = QPushButton("Apply configuration")
        revert = QPushButton("Revert")
        apply.clicked.connect(self._apply)
        revert.clicked.connect(self._revert)
        cfg.form.addRow(hbox(apply, revert, self.dirty, stretch=True))
        note = QLabel("The configuration is validated by the bridge against the script's schema. "
                      "Unlike other fields it is sent only when you press Apply, because partial JSON is not valid.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        cfg.form.addRow(note)
        self._shown_cfg = b.get("configuration", {})
        self.editor.textChanged.connect(lambda: self.dirty.setText("● unsaved changes"))

        def refresh_cfg():
            new = self.res.get("configuration", {})
            if new != self._shown_cfg and not self.dirty.text():
                self._revert()

        self.on_refresh(refresh_cfg)
        self.add(hbox(self.delete_button("Delete automation", "behavior_instance",
                                         f"Delete the automation “{self.store.name(b)}”?"), stretch=True))
        self.raw_json(lambda: [rid] + ([script["id"]] if script else []))
        self.refresh()

    def _revert(self):
        self._shown_cfg = self.res.get("configuration", {})
        self.editor.blockSignals(True)
        self.editor.setPlainText(json.dumps(self._shown_cfg, indent=2, ensure_ascii=False))
        self.editor.blockSignals(False)
        self.dirty.setText("")

    def _apply(self):
        try:
            cfg = json.loads(self.editor.toPlainText())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid JSON", str(e))
            return
        if not isinstance(cfg, dict):
            QMessageBox.warning(self, "Invalid configuration", "The configuration must be a JSON object.")
            return

        def done(_):
            self.dirty.setText("")
            self._shown_cfg = cfg
            self.ctx.status("Automation configuration saved")

        self.session.put("behavior_instance", self.rid, {"configuration": cfg}, on_done=done)


# ---------------------------------------------------------------------------
# Bridge & fallback
# ---------------------------------------------------------------------------

class BridgePage(Page):
    def __init__(self, ctx, rid: str | None):
        super().__init__(ctx, rid)
        bridge = next(iter(self.store.of_type("bridge")), {})
        dev = self.store.owner(bridge) if bridge else None
        cfg = ctx.bridge_config
        self.title(cfg.name, f"{cfg.host} · {bridge.get('bridge_id', cfg.bridge_id)}")

        sec = self.add(Section("Bridge"))
        sec.row("Bridge ID", bridge.get("bridge_id", "—"))
        sec.row("Address", cfg.host + ("" if cfg.port == 443 else f":{cfg.port}"))
        if dev:
            pd = dev.get("product_data") or {}
            sec.row("Model", f"{pd.get('product_name', '?')} ({pd.get('model_id', '?')})")
            sec.row("Firmware", pd.get("software_version", "—"))
        sec.row("Time zone", (bridge.get("time_zone") or {}).get("time_zone", "—"))
        for z in (self.store.services(dev, "zigbee_connectivity") if dev else []):
            sec.row("Zigbee channel", pretty((z.get("channel") or {}).get("value")))
        for hk in self.store.of_type("homekit"):
            sec.row("HomeKit", pretty(hk.get("status")))
        for m in self.store.of_type("matter"):
            sec.row("Matter", f"{m.get('max_fabrics', '?')} fabrics max, software {m.get('software_version_string', '?')}")
        fp = cfg.cert_sha256
        sec.row("TLS certificate", f"pinned, SHA-256\n{fp[:32]}\n{fp[32:]}" if fp else "not pinned yet")

        counts = Section("Contents")
        for label, types in (("Devices", ("device",)), ("Lights", ("light",)), ("Rooms", ("room",)),
                             ("Zones", ("zone",)), ("Scenes", ("scene", "smart_scene")),
                             ("Automations", ("behavior_instance",)),
                             ("Entertainment areas", ("entertainment_configuration",))):
            counts.row(label, str(len(self.store.of_type(*types))))
        self.add(counts)

        pair = QPushButton("Search for new devices…")
        pair.clicked.connect(ctx.open_device_pairing)
        self.add(hbox(pair, stretch=True))
        if dev:
            self.raw_json(lambda: [dev["id"]] + [s["rid"] for s in dev.get("services", [])])
        self.refresh()


class GenericPage(Page):
    def __init__(self, ctx, rid: str):
        super().__init__(ctx, rid)
        r = self.res
        self.title(self.store.name(r), pretty(r.get("type")))
        view = JsonView(r)
        view.setMinimumHeight(400)
        self.add(view)
        self.on_refresh(lambda: view.set_data(self.res))


def page_for(ctx, rid: str | None) -> Page:
    if rid is None or rid == "__bridge__":
        return BridgePage(ctx, None)
    r = ctx.store.get(rid) or {}
    return {
        "device": DevicePage,
        "room": GroupPage, "zone": GroupPage, "bridge_home": GroupPage,
        "scene": ScenePage, "smart_scene": SmartScenePage,
        "behavior_instance": AutomationPage,
    }.get(r.get("type"), GenericPage)(ctx, rid)
