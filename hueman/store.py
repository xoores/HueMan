"""In-memory mirror of the bridge's resources, kept current by the event stream."""

from __future__ import annotations

import copy
from typing import Iterable

from PySide6.QtCore import QObject, Signal

# Keys whose change affects how the resource is listed (names, membership, grouping).
STRUCTURAL_KEYS = {"metadata", "children", "services", "group", "owner", "script_id"}


# Objects the bridge replaces wholesale rather than merging (automation configuration).
REPLACED_KEYS = {"configuration"}


def deep_merge(target: dict, patch: dict, _top: bool = True) -> None:
    """Merge ``patch`` into ``target`` in place. Lists are replaced, dicts merged."""
    for k, v in patch.items():
        if _top and k in REPLACED_KEYS:
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            deep_merge(target[k], v, False)
        else:
            target[k] = copy.deepcopy(v)


class ResourceStore(QObject):
    reset = Signal()
    changed = Signal(str)  # resource id
    added = Signal(str)
    removed = Signal(str, str)  # id, rtype
    structure_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._by_id: dict[str, dict] = {}

    # -- loading / events --------------------------------------------------

    def load(self, resources: Iterable[dict]) -> None:
        self._by_id = {r["id"]: r for r in resources if "id" in r}
        self.reset.emit()
        self.structure_changed.emit()

    def clear(self) -> None:
        self._by_id = {}
        self.reset.emit()
        self.structure_changed.emit()

    def apply_event(self, event: dict) -> None:
        etype = event.get("type")
        structural = False
        for item in event.get("data", []):
            rid = item.get("id")
            if not rid:
                continue
            if etype == "add":
                self._by_id[rid] = copy.deepcopy(item)
                structural = True
                self.added.emit(rid)
            elif etype == "delete":
                old = self._by_id.pop(rid, None)
                structural = True
                self.removed.emit(rid, (old or item).get("type", ""))
            elif etype == "update":
                res = self._by_id.get(rid)
                if res is None:
                    continue
                deep_merge(res, item)
                if STRUCTURAL_KEYS & item.keys():
                    structural = True
                self.changed.emit(rid)
        if structural:
            self.structure_changed.emit()

    def merge_local(self, rid: str, patch: dict) -> None:
        """Optimistically apply a change we just sent to the bridge."""
        res = self._by_id.get(rid)
        if res is None:
            return
        deep_merge(res, patch)
        self.changed.emit(rid)
        if STRUCTURAL_KEYS & patch.keys():
            self.structure_changed.emit()

    def drop_local(self, rid: str) -> None:
        old = self._by_id.pop(rid, None)
        if old is not None:
            self.removed.emit(rid, old.get("type", ""))
            self.structure_changed.emit()

    # -- queries -----------------------------------------------------------

    def get(self, rid: str | None) -> dict | None:
        return self._by_id.get(rid) if rid else None

    def resolve(self, ref: dict | None) -> dict | None:
        return self.get(ref.get("rid")) if ref else None

    def of_type(self, *rtypes: str) -> list[dict]:
        return [r for r in self._by_id.values() if r.get("type") in rtypes]

    def all(self) -> list[dict]:
        return list(self._by_id.values())

    def bridge_device(self) -> dict | None:
        bridge = next(iter(self.of_type("bridge")), None)
        return self.resolve(bridge.get("owner")) if bridge else None

    def owner(self, res: dict) -> dict | None:
        return self.resolve(res.get("owner"))

    def services(self, device: dict, *rtypes: str) -> list[dict]:
        out = []
        for ref in device.get("services", []):
            if not rtypes or ref.get("rtype") in rtypes:
                svc = self.get(ref.get("rid"))
                if svc:
                    out.append(svc)
        return out

    def service_types(self, device: dict) -> set[str]:
        return {ref.get("rtype") for ref in device.get("services", [])}

    def name(self, res: dict | None) -> str:
        if not res:
            return "?"
        md = res.get("metadata") or {}
        if md.get("name"):
            return md["name"]
        if res.get("name"):
            return res["name"]
        owner = self.owner(res)
        if owner and owner is not res:
            return self.name(owner)
        if res.get("type") == "bridge_home":
            return "All lights"
        return f"{res.get('type', '?')} {res.get('id', '')[:8]}"

    def room_of_device(self, device_id: str) -> dict | None:
        for room in self.of_type("room"):
            if any(c.get("rid") == device_id for c in room.get("children", [])):
                return room
        return None

    def grouped_light(self, group: dict) -> dict | None:
        return next(iter(self.services(group, "grouped_light")), None)

    def lights_in_group(self, group: dict) -> list[dict]:
        if group.get("type") == "bridge_home":
            return self.of_type("light")
        lights: list[dict] = []
        for ref in group.get("children", []):
            child = self.get(ref.get("rid"))
            if not child:
                continue
            if child.get("type") == "light":
                lights.append(child)
            elif child.get("type") == "device":
                lights.extend(self.services(child, "light"))
        return lights

    def scenes_for_group(self, group_id: str) -> list[dict]:
        return [s for s in self.of_type("scene", "smart_scene") if (s.get("group") or {}).get("rid") == group_id]

    def device_of_service(self, rid: str) -> dict | None:
        svc = self.get(rid)
        if svc and svc.get("type") == "device":
            return svc
        return self.owner(svc) if svc else None
