"""Reusable widgets for the detail pages."""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from ..colors import light_rgb, mirek_to_kelvin, rgb_to_xy, xy_to_rgb

ROOM_ARCHETYPES = [
    "living_room", "kitchen", "dining", "bedroom", "kids_bedroom", "bathroom", "nursery", "recreation", "office",
    "gym", "hallway", "toilet", "front_door", "garage", "terrace", "garden", "driveway", "carport", "home",
    "downstairs", "upstairs", "top_floor", "attic", "guest_room", "staircase", "lounge", "man_cave", "computer",
    "studio", "music", "tv", "reading", "closet", "storage", "laundry_room", "balcony", "porch", "barbecue",
    "pool", "other",
]

LIGHT_ARCHETYPES = [
    "unknown_archetype", "classic_bulb", "sultan_bulb", "flood_bulb", "spot_bulb", "candle_bulb", "luster_bulb",
    "pendant_round", "pendant_long", "ceiling_round", "ceiling_square", "floor_shade", "floor_lantern",
    "table_shade", "recessed_ceiling", "recessed_floor", "single_spot", "double_spot", "table_wash",
    "wall_lantern", "wall_shade", "flexible_lamp", "ground_spot", "wall_spot", "plug", "hue_go", "hue_lightstrip",
    "hue_iris", "hue_bloom", "bollard", "wall_washer", "hue_play", "vintage_bulb", "vintage_candle_bulb",
    "ellipse_bulb", "triangle_bulb", "small_globe_bulb", "large_globe_bulb", "edison_bulb", "christmas_tree",
    "string_light", "hue_centris", "hue_lightstrip_tv", "hue_lightstrip_pc", "hue_tube", "hue_signe",
    "pendant_spot", "ceiling_horizontal", "ceiling_tube", "up_and_down", "up_and_down_up", "up_and_down_down",
    "hue_floodlight_camera", "twilight", "twilight_front", "twilight_back", "hue_play_wallwasher",
    "hue_omniglow", "hue_neon", "string_globe", "string_permanent", "hue_go_xxl", "rigid_tube", "flexible_tube",
    "panels", "pendant_tube", "vertical_tube", "recessed_ceiling_tube", "wall_rectangle", "string_icicle",
    "string_grid", "string_net", "hue_omniglow_arc", "hue_flux_arc",
]


def pretty(value: str | None) -> str:
    return (value or "").replace("_", " ").strip().capitalize() or "—"


def dot_icon(rgb: tuple[int, int, int] | None, size: int = 14, ring: QColor | None = None) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if rgb is None:
        p.setPen(QColor(128, 128, 128))
        p.setBrush(Qt.NoBrush)
    else:
        p.setPen(ring or QColor(0, 0, 0, 90))
        p.setBrush(QColor(*rgb))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


def confirm(parent: QWidget, title: str, text: str, destructive_label: str = "Delete") -> bool:
    box = QMessageBox(QMessageBox.Warning, title, text, parent=parent)
    yes = box.addButton(destructive_label, QMessageBox.DestructiveRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    box.exec()
    return box.clickedButton() is yes


# Every section uses the same label and dropdown widths, so the page reads as one table.
LABEL_WIDTH = 130
FIELD_WIDTH = 200


class Section(QGroupBox):
    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        self.form = QFormLayout(self)
        self.form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def row(self, label: str, widget: QWidget | str) -> QWidget:
        if isinstance(widget, str):
            widget = SelectableLabel(widget)
        lbl = QLabel(label)
        lbl.setFixedWidth(LABEL_WIDTH)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.form.addRow(lbl, widget)
        return widget


class SelectableLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)


class NameEdit(QLineEdit):
    """Line edit that commits on Enter/focus-out, with Hue's 1–32 character limit."""

    committed = Signal(str)

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setMaxLength(32)
        self._value = text
        self.editingFinished.connect(self._commit)

    def _commit(self):
        text = self.text().strip()
        if not text:
            self.setText(self._value)
            return
        if text != self._value:
            self._value = text
            self.committed.emit(text)

    def set_value(self, text: str) -> None:
        self._value = text
        if not self.hasFocus():
            self.setText(text)


class EnumCombo(QComboBox):
    """Combo box over API enum values that emits only on user changes."""

    chosen = Signal(str)

    def __init__(self, values: list[str], current: str | None = None, parent=None):
        super().__init__(parent)
        self.setFixedWidth(FIELD_WIDTH)
        self._listeners: list = []
        self._set_items(values, current)
        self.activated.connect(lambda _: self._user_chose())

    def _user_chose(self):
        self._notify()
        self.chosen.emit(self.currentData())

    def on_value(self, fn) -> None:
        """Call ``fn(value)`` now and whenever the shown value changes (by the user or a refresh)."""
        self._listeners.append(fn)
        fn(self.currentData())

    def _notify(self):
        for fn in self._listeners:
            fn(self.currentData())

    def _set_items(self, values, current):
        self.blockSignals(True)
        self.clear()
        vals = list(values)
        if current and current not in vals:
            vals.insert(0, current)
        for v in vals:
            self.addItem(pretty(v), v)
        if current:
            self.setCurrentIndex(self.findData(current))
        self.blockSignals(False)

    def set_value(self, value: str | None) -> None:
        if value is None:
            return
        if self.findData(value) < 0:
            self.addItem(pretty(value), value)
        self.blockSignals(True)
        self.setCurrentIndex(self.findData(value))
        self.blockSignals(False)
        self._notify()


class HelpIcon(QLabel):
    """A small "?" that explains something in its tooltip."""

    def __init__(self, tooltip: str, parent=None):
        super().__init__("?", parent)
        self.setToolTip(tooltip)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.WhatsThisCursor)
        self.setStyleSheet("QLabel { border: 1px solid palette(text); border-radius: 9px;"
                           " color: palette(text); font-weight: bold; font-size: 11px; }")


def explained(combo: EnumCombo, intro: str, options: dict[str, str]) -> QWidget:
    """Combo + "?" tooltip listing every option + a grey line describing the selected one."""
    tip = f"<p>{intro}</p>" + "".join(
        f"<p><b>{pretty(k)}</b> — {v}</p>" for k, v in options.items())
    combo.setToolTip(tip)
    note = QLabel()
    note.setWordWrap(True)
    note.setStyleSheet("color: gray")
    combo.on_value(lambda v: note.setText(options.get(v, "")))
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(combo)
    lay.addWidget(HelpIcon(tip))
    lay.addSpacing(6)
    lay.addWidget(note, 1)
    return w


LIGHT_FUNCTION_HELP = (
    "What the light is mainly used for. This is a hint for the Hue apps and features (scenes, automations), "
    "so they can give practical lights usable white light and leave the colourful parts of a scene to "
    "mood lights. It does not change how the light itself behaves.",
    {
        "functional": "Mainly for seeing and doing things: ceiling lights, spots, task and reading lamps.",
        "decorative": "Mainly for atmosphere: lightstrips, accent and mood lamps.",
        "mixed": "Used for both seeing and atmosphere.",
        "unknown": "Not specified.",
    },
)

POWERUP_HELP = (
    "What the light does when its power comes back: after a power cut, or when it is switched on with an "
    "ordinary wall switch.",
    {
        "safety": "Always turns on at full brightness in warm white (about 2700 K), like a normal bulb. "
                  "Hue's factory default.",
        "powerfail": "Returns to exactly what it was doing before the power went off, including staying off "
                     "if it was off.",
        "last_on_state": "Always turns on, with the brightness and colour it last had while it was on.",
        "custom": "Custom settings made elsewhere (e.g. in the Hue app). Choosing another option replaces them.",
    },
)


class JsonView(QPlainTextEdit):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = self.font()
        f.setFamily("monospace")
        self.setFont(f)
        self.setMinimumHeight(180)
        if data is not None:
            self.set_data(data)

    def set_data(self, data) -> None:
        bar = self.verticalScrollBar().value()
        self.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
        self.verticalScrollBar().setValue(bar)


class Collapsible(QWidget):
    def __init__(self, title: str, content: QWidget, expanded: bool = False, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.button = QToolButton(text=title, checkable=True, checked=expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content = content
        content.setVisible(expanded)
        self.button.toggled.connect(self._toggle)
        lay.addWidget(self.button)
        lay.addWidget(content)

    def _toggle(self, on: bool):
        self.button.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)


class ColorButton(QPushButton):
    """Shows a colour swatch; opens a live-updating colour dialog."""

    picked = Signal(float, float)  # CIE x, y

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(90)
        self._rgb: tuple[int, int, int] = (255, 255, 255)
        self.clicked.connect(self._open)

    def set_rgb(self, rgb: tuple[int, int, int] | None) -> None:
        self._rgb = rgb or (60, 60, 60)
        self.setIcon(dot_icon(rgb, 16))
        self.setText("#%02x%02x%02x" % self._rgb if rgb else "—")

    def _open(self):
        dlg = QColorDialog(QColor(*self._rgb), self)
        dlg.setOption(QColorDialog.NoButtons, False)
        dlg.currentColorChanged.connect(self._emit)
        original = self._rgb
        if dlg.exec() == QColorDialog.Accepted:
            self._emit(dlg.currentColor())
        else:
            self._emit(QColor(*original))

    def _emit(self, c: QColor):
        if c.isValid():
            self.picked.emit(*rgb_to_xy(c.red(), c.green(), c.blue()))


class LightControls(QObject):
    """On/off, brightness, colour temperature, colour and effect controls for a light or grouped_light.

    The rows are added to ``section`` so they share its label column with the section's other rows.
    """

    def __init__(self, ctx, rtype: str, rid: str, section: Section, lights_for_caps: list[dict] | None = None):
        super().__init__(section)
        self.ctx, self.rtype, self.rid = ctx, rtype, rid
        res = ctx.store.get(rid) or {}
        caps = lights_for_caps if lights_for_caps is not None else [res]
        self.has_dim = any("dimming" in light for light in caps)
        ct_schemas = [light["color_temperature"].get("mirek_schema") for light in caps
                      if isinstance(light.get("color_temperature"), dict)
                      and light["color_temperature"].get("mirek_schema")]
        self.has_color = any("xy" in (light.get("color") or {}) or "gamut_type" in (light.get("color") or {})
                             for light in caps)
        self.effects = ((res.get("effects_v2") or {}).get("action") or {}).get("effect_values") or []

        row = section.row
        self.on = QCheckBox("On")
        self.on.clicked.connect(lambda v: self._put({"on": {"on": bool(v)}}))
        row("Power", self.on)

        self.bri = self.bri_label = None
        if self.has_dim:
            self.bri = QSlider(Qt.Horizontal)
            self.bri.setRange(1, 100)
            self.bri_label = QLabel()
            self.bri_label.setMinimumWidth(40)
            self.bri.valueChanged.connect(self._on_bri)
            row("Brightness", hbox(self.bri, self.bri_label))

        self.ct = self.ct_label = None
        if ct_schemas:
            lo = min(s["mirek_minimum"] for s in ct_schemas)
            hi = max(s["mirek_maximum"] for s in ct_schemas)
            self.ct = QSlider(Qt.Horizontal)
            # Left = warm, right = cool, like the Hue app.
            self.ct.setRange(-hi, -lo)
            self.ct_label = QLabel()
            self.ct_label.setMinimumWidth(60)
            self.ct.valueChanged.connect(self._on_ct)
            row("White", hbox(QLabel("warm"), self.ct, QLabel("cool"), self.ct_label))

        self.color = None
        if self.has_color:
            self.color = ColorButton()
            self.color.picked.connect(lambda x, y: self._put({"color": {"xy": {"x": x, "y": y}}}))
            row("Colour", hbox(self.color, stretch=True))

        self.effect = None
        if self.effects:
            self.effect = EnumCombo(self.effects)
            self.effect.chosen.connect(lambda v: self._put({"effects_v2": {"action": {"effect": v}}}))
            row("Effect", hbox(self.effect, stretch=True))
        self.refresh()

    def _put(self, payload: dict) -> None:
        self.ctx.session.put(self.rtype, self.rid, payload)

    def _on_bri(self, v: int):
        self.bri_label.setText(f"{v} %")
        self._put({"dimming": {"brightness": float(v)}})

    def _on_ct(self, v: int):
        mirek = -v
        self.ct_label.setText(f"{mirek_to_kelvin(mirek)} K")
        self._put({"color_temperature": {"mirek": mirek}})

    def refresh(self) -> None:
        res = self.ctx.store.get(self.rid)
        if not res:
            return
        set_checked(self.on, (res.get("on") or {}).get("on", False))
        if self.bri and not self.bri.isSliderDown():
            b = (res.get("dimming") or {}).get("brightness")
            if b is not None:
                self.bri.blockSignals(True)
                self.bri.setValue(max(1, round(b)))
                self.bri.blockSignals(False)
                self.bri_label.setText(f"{round(b)} %")
        if self.ct and not self.ct.isSliderDown():
            ct = res.get("color_temperature") or {}
            mirek = ct.get("mirek")
            self.ct.blockSignals(True)
            if mirek and ct.get("mirek_valid", True):
                self.ct.setValue(-mirek)
                self.ct_label.setText(f"{mirek_to_kelvin(mirek)} K")
            else:
                self.ct_label.setText("colour")
            self.ct.blockSignals(False)
        if self.color:
            xy = (res.get("color") or {}).get("xy")
            if self.rtype == "light":
                self.color.set_rgb(light_rgb({**res, "on": {"on": True}}))
            elif xy:
                self.color.set_rgb(xy_to_rgb(xy["x"], xy["y"]))
            else:
                self.color.set_rgb(None)
        if self.effect:
            self.effect.set_value(((res.get("effects_v2") or {}).get("status") or {}).get("effect"))


def set_checked(box: QCheckBox, value: bool) -> None:
    box.blockSignals(True)
    box.setChecked(bool(value))
    box.blockSignals(False)


def hbox(*widgets, stretch: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        lay.addWidget(x)
    if stretch:
        lay.addStretch(1)
    return w


def kelvin_mirek_range(light: dict) -> tuple[int, int]:
    schema = (light.get("color_temperature") or {}).get("mirek_schema") or {}
    return schema.get("mirek_minimum", 153), schema.get("mirek_maximum", 500)

