"""Dialogs for creating rooms, zones and automations."""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit,
)

from .widgets import ROOM_ARCHETYPES, EnumCombo, NameEdit


class NewGroupDialog(QDialog):
    def __init__(self, ctx, rtype: str, parent=None):
        super().__init__(parent)
        self.store, self.rtype = ctx.store, rtype
        self.setWindowTitle(f"New {rtype}")
        self.resize(420, 500)
        form = QFormLayout(self)
        self.name = NameEdit()
        form.addRow("Name", self.name)
        self.archetype = EnumCombo(ROOM_ARCHETYPES, "living_room" if rtype == "room" else "other")
        form.addRow("Type", self.archetype)
        self.members = QListWidget()
        if rtype == "room":
            form.addRow(QLabel("Devices (ones already in a room will be moved here):"))
            candidates = [d for d in self.store.of_type("device") if "bridge" not in self.store.service_types(d)]
        else:
            form.addRow(QLabel("Lights:"))
            candidates = self.store.of_type("light")
        for c in sorted(candidates, key=lambda r: self.store.name(r).lower()):
            label = self.store.name(c)
            room = self.store.room_of_device(c["id"] if rtype == "room" else (c.get("owner") or {}).get("rid", ""))
            if room:
                label += f"   ({self.store.name(room)})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, c["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.members.addItem(item)
        form.addRow(self.members)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def checked(self) -> list[str]:
        return [self.members.item(i).data(Qt.UserRole) for i in range(self.members.count())
                if self.members.item(i).checkState() == Qt.Checked]

    def _accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, f"New {self.rtype}", "Please enter a name.")
            return
        self.accept()

    def payload(self) -> dict:
        ids = self.checked()
        child_type = "device" if self.rtype == "room" else "light"
        return {"type": self.rtype, "metadata": {"name": self.name.text().strip(),
                                                 "archetype": self.archetype.currentData()},
                "children": [{"rid": i, "rtype": child_type} for i in ids]}

    def moves(self) -> list[tuple[dict, list]]:
        """For rooms: (old room, remaining children) for each room losing a device."""
        out = []
        if self.rtype != "room":
            return out
        ids = set(self.checked())
        for room in self.store.of_type("room"):
            rest = [c for c in room.get("children", []) if c.get("rid") not in ids]
            if len(rest) != len(room.get("children", [])):
                out.append((room, rest))
        return out


def pick_group(parent, store) -> dict | None:
    groups = sorted(store.of_type("room", "zone"), key=lambda g: store.name(g).lower())
    if not groups:
        QMessageBox.information(parent, "New scene", "Create a room or zone first.")
        return None
    labels = [f"{store.name(g)} ({g['type']})" for g in groups]
    choice, ok = QInputDialog.getItem(parent, "New scene", "Room or zone for the scene:", labels, 0, False)
    return groups[labels.index(choice)] if ok else None


class NewAutomationDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("New automation")
        self.resize(560, 520)
        form = QFormLayout(self)
        self.name = NameEdit()
        form.addRow("Name", self.name)
        self.script = QComboBox()
        for s in sorted(store.of_type("behavior_script"), key=lambda s: store.name(s).lower()):
            if (s.get("metadata") or {}).get("category") in ("automation", "other", None):
                self.script.addItem(f"{store.name(s)} — {s.get('description', '')}", s["id"])
        self.script.currentIndexChanged.connect(self._template)
        form.addRow("Script", self.script)
        self.enabled = QCheckBox("Enable immediately")
        form.addRow("", self.enabled)
        self.config = QPlainTextEdit()
        f = self.config.font()
        f.setFamily("monospace")
        self.config.setFont(f)
        form.addRow(QLabel("Configuration (JSON):"))
        form.addRow(self.config)
        self.hint = QLabel(wordWrap=True)
        self.hint.setStyleSheet("color: gray")
        form.addRow(self.hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._template()

    def _template(self):
        sid = self.script.currentData()
        existing = next((b for b in self.store.of_type("behavior_instance") if b.get("script_id") == sid), None)
        if existing:
            self.config.setPlainText(json.dumps(existing.get("configuration", {}), indent=2, ensure_ascii=False))
            self.hint.setText(f"Pre-filled from the existing automation “{self.store.name(existing)}”. "
                              "Adjust it; the bridge validates the result.")
        else:
            script = self.store.get(sid) or {}
            schema = script.get("configuration_schema", {})
            self.config.setPlainText("{\n}")
            self.hint.setText("The bridge documents this script's configuration schema as "
                              f"{json.dumps(schema)}. There is no existing automation to copy from; "
                              "the bridge will report which fields are missing.")

    def _accept(self):
        if not self.script.currentData():
            QMessageBox.warning(self, "New automation", "The bridge offers no automation scripts.")
            return
        try:
            cfg = json.loads(self.config.toPlainText())
        except ValueError as e:
            QMessageBox.warning(self, "Invalid JSON", str(e))
            return
        if not isinstance(cfg, dict):
            QMessageBox.warning(self, "Invalid JSON", "The configuration must be a JSON object.")
            return
        self._cfg = cfg
        self.accept()

    def payload(self) -> dict:
        p = {"type": "behavior_instance", "script_id": self.script.currentData(),
             "enabled": self.enabled.isChecked(), "configuration": self._cfg}
        if self.name.text().strip():
            p["metadata"] = {"name": self.name.text().strip()}
        return p

