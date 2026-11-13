#!/usr/bin/env python3
"""A fake Hue Bridge speaking enough of the CLIP v2 API to develop and test hueman against.

    python tools/mock_bridge.py --port 8443                # synthetic home
    python tools/mock_bridge.py --snapshot snapshot.json   # seed from `GET /clip/v2/resource` output

The link button is "pressed" automatically unless --manual-link is given; then
`curl -k -X POST https://127.0.0.1:8443/mock/press-link` presses it for 30 seconds.
"""

from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UUID_RE = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ref(res: dict) -> dict:
    return {"rid": res["id"], "rtype": res["type"]}


def merge(target: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            merge(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Synthetic home
# ---------------------------------------------------------------------------

def _light(owner: dict, name: str, kind: str, archetype: str) -> dict:
    light = {
        "id": new_id(), "type": "light", "owner": ref(owner), "service_id": 0,
        "metadata": {"name": name, "archetype": archetype, "function": "mixed"},
        "identify": {}, "on": {"on": False},
        "mode": "normal",
        "powerup": {"preset": "safety", "configured": True, "on": {"mode": "on", "on": {"on": True}},
                    "dimming": {"mode": "dimming", "dimming": {"brightness": 100.0}},
                    "color": {"mode": "color_temperature", "color_temperature": {"mirek": 366}}},
    }
    if kind != "plug":
        light["dimming"] = {"brightness": 100.0, "min_dim_level": 0.2}
    if kind in ("ambiance", "color"):
        light["color_temperature"] = {"mirek": 366, "mirek_valid": True,
                                      "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 454}}
    if kind == "color":
        light["color"] = {"xy": {"x": 0.4573, "y": 0.41},
                          "gamut": {"red": {"x": 0.6915, "y": 0.3083}, "green": {"x": 0.17, "y": 0.7},
                                    "blue": {"x": 0.1532, "y": 0.0475}}, "gamut_type": "C"}
        effects = ["no_effect", "candle", "fire", "prism", "sparkle", "opal", "glisten"]
        light["effects_v2"] = {"action": {"effect_values": effects},
                               "status": {"effect": "no_effect", "effect_values": effects}}
    return light


def _device(name: str, model: str, product: str, archetype: str) -> dict:
    return {
        "id": new_id(), "type": "device",
        "product_data": {"model_id": model, "manufacturer_name": "Signify Netherlands B.V.",
                         "product_name": product, "product_archetype": archetype, "certified": True,
                         "software_version": "1.122.2"},
        "metadata": {"name": name, "archetype": archetype}, "identify": {}, "services": [],
    }


class World:
    """All resources plus the bookkeeping the fake bridge needs."""

    def __init__(self):
        self.res: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.subscribers: list[queue.Queue] = []
        self.app_keys: set[str] = set()
        self.link_pressed_until = 0.0
        self.auto_link = True
        self.request_log: list[tuple[str, str, object]] = []
        self.pending_new_devices = 1
        self.discovery_delay = 3.0

    # -- building ----------------------------------------------------------

    def add(self, r: dict) -> dict:
        self.res[r["id"]] = r
        return r

    def attach(self, device: dict, svc: dict) -> dict:
        self.add(svc)
        device["services"].append(ref(svc))
        return svc

    def add_light_device(self, name: str, kind: str = "color", model: str = "LCA001",
                         product: str = "Hue color lamp", archetype: str = "sultan_bulb") -> dict:
        dev = self.add(_device(name, model, product, archetype))
        self.attach(dev, {"id": new_id(), "type": "zigbee_connectivity", "owner": ref(dev),
                          "status": "connected", "mac_address": "00:17:88:01:%02x:%02x:%02x:%02x" % tuple(
                              uuid.uuid4().bytes[:4])})
        self.attach(dev, _light(dev, name, kind, archetype))
        self.attach(dev, {"id": new_id(), "type": "device_software_update", "owner": ref(dev),
                          "state": "no_update", "problems": []})
        return dev

    def add_group(self, rtype: str, name: str, archetype: str, children: list[dict]) -> dict:
        group = self.add({"id": new_id(), "type": rtype, "children": [ref(c) for c in children],
                          "services": [], "metadata": {"name": name, "archetype": archetype}})
        gl = self.add({"id": new_id(), "type": "grouped_light", "owner": ref(group), "on": {"on": False},
                       "dimming": {"brightness": 100.0}, "dimming_delta": {}, "color_temperature": {},
                       "color_temperature_delta": {}, "color": {}, "alert": {"action_values": ["breathe"]},
                       "signaling": {"signal_values": ["no_signal", "on_off"]}, "dynamics": {}})
        group["services"].append(ref(gl))
        return group

    def lights_of_group(self, group: dict) -> list[dict]:
        if group["type"] == "bridge_home":
            return [r for r in self.res.values() if r["type"] == "light"]
        out = []
        for c in group.get("children", []):
            child = self.res.get(c["rid"])
            if not child:
                continue
            if child["type"] == "light":
                out.append(child)
            elif child["type"] == "device":
                out += [self.res[s["rid"]] for s in child["services"]
                        if s["rtype"] == "light" and s["rid"] in self.res]
        return out

    @classmethod
    def synthetic(cls) -> "World":
        w = cls()
        bridge_dev = w.add(_device("Mock Bridge", "BSB002", "Hue Bridge", "bridge_v2"))
        w.attach(bridge_dev, {"id": new_id(), "type": "bridge", "owner": ref(bridge_dev),
                              "bridge_id": "001788fffe000001", "time_zone": {"time_zone": "Europe/Prague"}})
        w.attach(bridge_dev, {"id": new_id(), "type": "zigbee_connectivity", "owner": ref(bridge_dev),
                              "status": "connected", "mac_address": "00:17:88:01:00:00:00:01",
                              "channel": {"status": "set", "value": "channel_25"}})
        w.attach(bridge_dev, {"id": new_id(), "type": "zigbee_device_discovery", "owner": ref(bridge_dev),
                              "status": "ready", "action": {"action_type_values": ["search"]}})

        sofa = w.add_light_device("Sofa lamp", "color", "LCA001", "Hue color lamp", "sultan_bulb")
        ceiling = w.add_light_device("Ceiling", "ambiance", "LTW013", "Hue ambiance spot", "spot_bulb")
        strip = w.add_light_device("TV strip", "color", "LST002", "Hue lightstrip plus", "hue_lightstrip")
        bedside = w.add_light_device("Bedside", "white", "LWA001", "Hue white lamp", "classic_bulb")
        plug = w.add_light_device("Christmas tree", "plug", "LOM002", "Hue smart plug", "plug")

        sensor = w.add(_device("Hallway sensor", "SML001", "Hue motion sensor", "unknown_archetype"))
        w.attach(sensor, {"id": new_id(), "type": "motion", "owner": ref(sensor), "enabled": True,
                          "motion": {"motion": False, "motion_valid": True,
                                     "motion_report": {"changed": now_iso(), "motion": False}},
                          "sensitivity": {"status": "set", "sensitivity": 2, "sensitivity_max": 4}})
        w.attach(sensor, {"id": new_id(), "type": "temperature", "owner": ref(sensor), "enabled": True,
                          "temperature": {"temperature": 21.5, "temperature_valid": True,
                                          "temperature_report": {"changed": now_iso(), "temperature": 21.5}}})
        w.attach(sensor, {"id": new_id(), "type": "light_level", "owner": ref(sensor), "enabled": True,
                          "light": {"light_level": 18000, "light_level_valid": True,
                                    "light_level_report": {"changed": now_iso(), "light_level": 18000}}})
        w.attach(sensor, {"id": new_id(), "type": "device_power", "owner": ref(sensor),
                          "power_state": {"battery_state": "normal", "battery_level": 87}})
        w.attach(sensor, {"id": new_id(), "type": "zigbee_connectivity", "owner": ref(sensor),
                          "status": "connected", "mac_address": "00:17:88:01:02:00:00:01"})

        switch = w.add(_device("Living room switch", "RWL021", "Hue dimmer switch", "unknown_archetype"))
        for cid, ev in enumerate(["initial_press", "short_release", "long_press", "repeat"], start=1):
            w.attach(switch, {"id": new_id(), "type": "button", "owner": ref(switch),
                              "metadata": {"control_id": cid},
                              "button": {"last_event": ev, "button_report": {"updated": now_iso(), "event": ev},
                                         "repeat_interval": 800,
                                         "event_values": ["initial_press", "repeat", "short_release",
                                                          "long_release", "long_press"]}})
        w.attach(switch, {"id": new_id(), "type": "device_power", "owner": ref(switch),
                          "power_state": {"battery_state": "low", "battery_level": 9}})
        w.attach(switch, {"id": new_id(), "type": "zigbee_connectivity", "owner": ref(switch),
                          "status": "connected", "mac_address": "00:17:88:01:03:00:00:01"})

        living = w.add_group("room", "Living room", "living_room", [sofa, ceiling, strip, plug, switch])
        bedroom = w.add_group("room", "Bedroom", "bedroom", [bedside])
        w.add_group("room", "Hallway", "hallway", [sensor])
        light_of = lambda d: next(w.res[s["rid"]] for s in d["services"] if s["rtype"] == "light")  # noqa: E731
        w.add_group("zone", "TV area", "tv", [light_of(sofa), light_of(strip)])

        home = w.add({"id": new_id(), "type": "bridge_home", "children": [], "services": []})
        home["children"] = [ref(r) for r in w.res.values() if r["type"] == "device"]
        ghome = w.add({"id": new_id(), "type": "grouped_light", "owner": ref(home), "on": {"on": False},
                       "dimming": {"brightness": 100.0}})
        home["services"].append(ref(ghome))

        def scene(name, group, actions):
            w.add({"id": new_id(), "type": "scene", "group": ref(group), "metadata": {"name": name},
                   "actions": actions, "palette": {"color": [], "dimming": [], "color_temperature": [],
                                                   "effects_v2": []},
                   "speed": 0.5, "auto_dynamic": False, "recall": {}, "status": {"active": "inactive"}})

        scene("Relax", living, [
            {"target": ref(light_of(d)), "action": {"on": {"on": True}, "dimming": {"brightness": 56.0},
                                                    "color_temperature": {"mirek": 447}}}
            for d in (ceiling,)] + [
            {"target": ref(light_of(sofa)), "action": {"on": {"on": True}, "dimming": {"brightness": 40.0},
                                                       "color": {"xy": {"x": 0.5, "y": 0.41}}}},
            {"target": ref(light_of(strip)), "action": {"on": {"on": True}, "dimming": {"brightness": 30.0},
                                                        "color": {"xy": {"x": 0.21, "y": 0.1}}}},
            {"target": ref(light_of(plug)), "action": {"on": {"on": False}}},
        ])
        scene("Read", bedroom, [{"target": ref(light_of(bedside)),
                                 "action": {"on": {"on": True}, "dimming": {"brightness": 100.0}}}])

        wake = w.add({"id": new_id(), "type": "behavior_script", "description": "Get ready for the day",
                      "configuration_schema": {"$ref": "wake_up_config.json#"}, "trigger_schema": {},
                      "state_schema": {}, "version": "0.0.1",
                      "metadata": {"name": "Wake up", "category": "automation"}, "supported_features": []})
        w.add({"id": new_id(), "type": "behavior_script", "description": "Turn lights on at a set time",
               "configuration_schema": {"$ref": "timer_config.json#"}, "trigger_schema": {},
               "state_schema": {}, "version": "0.0.1",
               "metadata": {"name": "Timer", "category": "automation"}, "supported_features": []})
        w.add({"id": new_id(), "type": "behavior_instance", "script_id": wake["id"], "enabled": False,
               "state": None, "dependees": [], "status": "disabled", "last_error": "",
               "metadata": {"name": "Weekday wake-up"},
               "configuration": {"fade_in_duration": {"seconds": 1800},
                                 "when": {"recurrence_days": ["monday", "tuesday", "wednesday", "thursday",
                                                              "friday"],
                                          "time_point": {"time": {"hour": 6, "minute": 45}, "type": "time"}},
                                 "where": [{"group": ref(bedroom)}]}})
        return w

    @classmethod
    def from_snapshot(cls, path: Path) -> "World":
        w = cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        for r in data.get("data", data if isinstance(data, list) else []):
            w.add(r)
        return w

    # -- events ------------------------------------------------------------

    def emit(self, etype: str, items: list[dict]) -> None:
        if not items:
            return
        container = [{"creationtime": now_iso(), "id": new_id(), "type": etype, "data": items}]
        for q in list(self.subscribers):
            q.put(container)

    def _update_event(self, res: dict, patch: dict) -> dict:
        item = {"id": res["id"], "type": res["type"], **copy.deepcopy(patch)}
        if "owner" in res:
            item["owner"] = res["owner"]
        return item

    # -- request handling --------------------------------------------------

    def handle(self, method: str, rtype: str | None, rid: str | None, body) -> tuple[int, list]:
        with self.lock:
            self.request_log.append((method, f"{rtype}/{rid}" if rid else str(rtype), body))
            if method == "GET":
                if rtype is None:
                    return 200, list(self.res.values())
                items = [r for r in self.res.values() if r["type"] == rtype]
                if rid:
                    items = [r for r in items if r["id"] == rid]
                    if not items:
                        raise ApiError(404, "Not Found")
                return 200, items
            if not isinstance(body, dict):
                if method != "DELETE":
                    raise ApiError(400, "body must be a JSON object")
            if method == "PUT":
                return 200, self.put(rtype, rid, body)
            if method == "POST":
                return 200, self.post(rtype, body)
            if method == "DELETE":
                return 200, self.delete(rtype, rid)
            raise ApiError(405, "Method Not Allowed")

    def _get(self, rtype: str, rid: str) -> dict:
        r = self.res.get(rid)
        if not r or r["type"] != rtype:
            raise ApiError(404, "Not Found")
        return r

    def put(self, rtype: str, rid: str, body: dict) -> list:
        r = self._get(rtype, rid)
        body = {k: v for k, v in body.items() if k not in ("id", "type")}
        if "metadata" in body and "name" in body["metadata"]:
            name = body["metadata"]["name"]
            if not isinstance(name, str) or not 1 <= len(name) <= 32:
                raise ApiError(400, "invalid value for metadata.name")
        if rtype == "light":
            self._apply_light(r, body)
        elif rtype == "grouped_light":
            owner = self.res.get(r["owner"]["rid"])
            light_patch = {k: v for k, v in body.items() if k in ("on", "dimming", "color", "color_temperature")}
            for light in self.lights_of_group(owner) if owner else []:
                self._apply_light(light, copy.deepcopy(light_patch))
            merge(r, light_patch)
            self.emit("update", [self._update_event(r, light_patch)])
        elif rtype == "scene":
            recall = body.pop("recall", None)
            if body:
                merge(r, body)
                self.emit("update", [self._update_event(r, body)])
            if recall:
                for a in r.get("actions", []):
                    light = self.res.get(a["target"]["rid"])
                    if light:
                        action = copy.deepcopy(a["action"])
                        if "dimming" in recall:
                            action.setdefault("dimming", {})["brightness"] = recall["dimming"]["brightness"]
                        self._apply_light(light, action)
                for s in self.res.values():
                    if s["type"] == "scene" and s["group"]["rid"] == r["group"]["rid"] and s is not r \
                            and s.get("status", {}).get("active") != "inactive":
                        s["status"] = {"active": "inactive"}
                        self.emit("update", [self._update_event(s, {"status": s["status"]})])
                r["status"] = {"active": "static" if recall.get("action") == "static" else "static"}
                self.emit("update", [self._update_event(r, {"status": r["status"]})])
        elif rtype in ("room", "zone"):
            if "children" in body and rtype == "room":
                for c in body["children"]:
                    if c.get("rtype") != "device":
                        raise ApiError(400, "room children must be devices")
                    for other in self.res.values():
                        if other["type"] == "room" and other is not r and \
                                any(x["rid"] == c["rid"] for x in other["children"]):
                            raise ApiError(400, "device is already part of another room")
            if "children" in body and rtype == "zone":
                for c in body["children"]:
                    if c.get("rtype") != "light":
                        raise ApiError(400, "zone children must be light services")
            merge(r, body)
            self.emit("update", [self._update_event(r, body)])
        elif rtype == "zigbee_device_discovery":
            action = (body.get("action") or {}).get("action_type")
            if action not in ("search", "search_allow_default_link_key"):
                raise ApiError(400, "invalid action_type")
            if r["status"] == "active":
                raise ApiError(409, "search already active")
            r["status"] = "active"
            self.emit("update", [self._update_event(r, {"status": "active"})])
            threading.Thread(target=self._finish_search, args=(r,), daemon=True).start()
        else:
            patch = {k: v for k, v in body.items() if k not in ("identify",)}
            if "configuration" in patch:
                r["configuration"] = copy.deepcopy(patch.pop("configuration"))
                self.emit("update", [self._update_event(r, {"configuration": r["configuration"]})])
            merge(r, patch)
            if rtype == "behavior_instance" and "enabled" in patch:
                r["status"] = "running" if patch["enabled"] else "disabled"
                patch["status"] = r["status"]
            if patch:
                self.emit("update", [self._update_event(r, patch)])
        return [ref(r)]

    def _apply_light(self, light: dict, body: dict) -> None:
        patch: dict = {}
        if "on" in body:
            patch["on"] = {"on": bool(body["on"]["on"])}
        if "dimming" in body and "dimming" in light:
            b = float(body["dimming"]["brightness"])
            if not 0 <= b <= 100:
                raise ApiError(400, "brightness out of range")
            patch["dimming"] = {"brightness": max(b, light["dimming"].get("min_dim_level", 0.0))}
        if "color_temperature" in body and "color_temperature" in light:
            m = body["color_temperature"].get("mirek")
            schema = light["color_temperature"]["mirek_schema"]
            m = min(max(int(m), schema["mirek_minimum"]), schema["mirek_maximum"])
            patch["color_temperature"] = {"mirek": m, "mirek_valid": True}
        if "color" in body and "color" in light:
            patch["color"] = {"xy": body["color"]["xy"]}
            if "color_temperature" in light and "color_temperature" not in body:
                patch["color_temperature"] = {"mirek": None, "mirek_valid": False}
        if "effects_v2" in body and "effects_v2" in light:
            eff = body["effects_v2"]["action"]["effect"]
            patch["effects_v2"] = {"status": {"effect": eff}}
        for k in ("metadata", "powerup", "dimming_configuration"):
            if k in body:
                patch[k] = body[k]
        if patch:
            merge(light, patch)
            self.emit("update", [self._update_event(light, patch)])

    def _finish_search(self, disc: dict) -> None:
        time.sleep(self.discovery_delay)
        with self.lock:
            added = []
            for _ in range(self.pending_new_devices):
                n = sum(1 for r in self.res.values() if r["type"] == "device")
                dev = self.add_light_device(f"Hue color lamp {n}", "color")
                added.append(dev)
            self.pending_new_devices = 0
            for dev in added:
                self.emit("add", [copy.deepcopy(self.res[s["rid"]]) for s in dev["services"]])
                self.emit("add", [copy.deepcopy(dev)])
            disc["status"] = "ready"
            self.emit("update", [self._update_event(disc, {"status": "ready"})])

    def post(self, rtype: str, body: dict) -> list:
        if rtype in ("room", "zone"):
            md = body.get("metadata") or {}
            if not md.get("name") or not md.get("archetype"):
                raise ApiError(400, "metadata.name and metadata.archetype are required")
            children = [self.res[c["rid"]] for c in body.get("children", []) if c.get("rid") in self.res]
            g = self.add_group(rtype, md["name"], md["archetype"], children)
            gl = self.res[g["services"][0]["rid"]]
            self.emit("add", [copy.deepcopy(gl)])
            self.emit("add", [copy.deepcopy(g)])
            return [ref(g)]
        if rtype == "scene":
            md = body.get("metadata") or {}
            if not md.get("name") or "group" not in body or "actions" not in body:
                raise ApiError(400, "metadata.name, group and actions are required")
            s = self.add({"id": new_id(), "type": "scene", "group": body["group"], "metadata": md,
                          "actions": body["actions"], "palette": body.get("palette", {
                              "color": [], "dimming": [], "color_temperature": [], "effects_v2": []}),
                          "speed": body.get("speed", 0.5), "auto_dynamic": body.get("auto_dynamic", False),
                          "recall": {}, "status": {"active": "inactive"}})
            self.emit("add", [copy.deepcopy(s)])
            return [ref(s)]
        if rtype == "behavior_instance":
            if body.get("script_id") not in self.res or "configuration" not in body:
                raise ApiError(400, "script_id and configuration are required")
            b = self.add({"id": new_id(), "type": "behavior_instance", "script_id": body["script_id"],
                          "enabled": bool(body.get("enabled")), "state": None, "dependees": [],
                          "status": "running" if body.get("enabled") else "disabled", "last_error": "",
                          "configuration": body["configuration"],
                          "metadata": body.get("metadata") or {"name": "Automation"}})
            self.emit("add", [copy.deepcopy(b)])
            return [ref(b)]
        raise ApiError(405, "Method Not Allowed")

    def delete(self, rtype: str, rid: str) -> list:
        r = self._get(rtype, rid)
        removed = [r]
        if rtype == "device":
            if any(s["rtype"] == "bridge" for s in r["services"]):
                raise ApiError(403, "the bridge device cannot be deleted")
            svc_ids = {s["rid"] for s in r["services"]}
            removed += [self.res[s] for s in svc_ids if s in self.res]
            for g in self.res.values():
                if g["type"] in ("room", "zone", "bridge_home"):
                    before = len(g.get("children", []))
                    g["children"] = [c for c in g["children"] if c["rid"] != rid and c["rid"] not in svc_ids]
                    if len(g["children"]) != before:
                        self.emit("update", [self._update_event(g, {"children": g["children"]})])
                if g["type"] == "scene":
                    before = len(g["actions"])
                    g["actions"] = [a for a in g["actions"] if a["target"]["rid"] not in svc_ids]
                    if len(g["actions"]) != before:
                        self.emit("update", [self._update_event(g, {"actions": g["actions"]})])
        elif rtype in ("room", "zone"):
            removed += [self.res[s["rid"]] for s in r["services"] if s["rid"] in self.res]
            removed += [s for s in self.res.values() if s["type"] == "scene" and s["group"]["rid"] == rid]
        elif rtype not in ("scene", "behavior_instance", "smart_scene"):
            raise ApiError(405, "Method Not Allowed")
        for x in removed:
            self.res.pop(x["id"], None)
        self.emit("delete", [{"id": x["id"], "type": x["type"]} for x in removed])
        return [ref(r)]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def make_handler(world: World):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            if getattr(self.server, "verbose", False):
                super().log_message(fmt, *args)

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return None
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return "invalid"

        def _authorized(self) -> bool:
            return self.headers.get("hue-application-key") in world.app_keys

        def _dispatch(self, method: str):
            path = self.path.split("?")[0]
            body = self._body() if method in ("PUT", "POST") else None
            if path == "/api/0/config" and method == "GET":
                return self._send(200, {"name": "Mock Bridge", "datastoreversion": "187", "swversion": "1977090000",
                                        "apiversion": "1.77.0", "mac": "00:17:88:00:00:01",
                                        "bridgeid": "001788FFFE000001", "factorynew": False,
                                        "modelid": "BSB002"})
            if path == "/api" and method == "POST":
                if world.auto_link or time.time() < world.link_pressed_until:
                    key = uuid.uuid4().hex
                    world.app_keys.add(key)
                    return self._send(200, [{"success": {"username": key, "clientkey": uuid.uuid4().hex.upper()}}])
                return self._send(200, [{"error": {"type": 101, "address": "",
                                                   "description": "link button not pressed"}}])
            if path == "/mock/press-link" and method == "POST":
                world.link_pressed_until = time.time() + 30
                return self._send(200, {"ok": True})
            if path == "/eventstream/clip/v2" and method == "GET":
                if not self._authorized():
                    return self._send(403, {"errors": [{"description": "Forbidden"}], "data": []})
                return self._stream()
            m = re.fullmatch(rf"/clip/v2/resource(?:/([a-z_]+))?(?:/({UUID_RE}))?/?", path)
            if not m:
                return self._send(404, {"errors": [{"description": "Not Found"}], "data": []})
            if not self._authorized():
                return self._send(403, {"errors": [{"description": "Forbidden"}], "data": []})
            if body == "invalid":
                return self._send(400, {"errors": [{"description": "Body contains invalid JSON"}], "data": []})
            rtype, rid = m.group(1), m.group(2)
            if method in ("PUT", "DELETE") and not (rtype and rid):
                return self._send(405, {"errors": [{"description": "Method Not Allowed"}], "data": []})
            if method == "POST" and (not rtype or rid):
                return self._send(405, {"errors": [{"description": "Method Not Allowed"}], "data": []})
            try:
                status, data = world.handle(method, rtype, rid, body)
            except ApiError as e:
                return self._send(e.status, {"errors": [{"description": str(e)}], "data": []})
            except (KeyError, TypeError, ValueError) as e:
                return self._send(400, {"errors": [{"description": f"invalid body: {e}"}], "data": []})
            return self._send(status, {"errors": [], "data": data})

        def _stream(self):
            q: queue.Queue = queue.Queue()
            world.subscribers.append(q)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b": hi\n\n")
                self.wfile.flush()
                seq = 0
                while not getattr(self.server, "stopping", False):
                    try:
                        container = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    seq += 1
                    payload = json.dumps(container, ensure_ascii=False)
                    self.wfile.write(f"id: {int(time.time())}:{seq}\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass
            finally:
                if q in world.subscribers:
                    world.subscribers.remove(q)
                self.close_connection = True

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def self_signed_context() -> ssl.SSLContext:
    d = Path(tempfile.mkdtemp(prefix="hueman-mock-"))
    key, crt = d / "key.pem", d / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=001788fffe000001", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    return ctx


class _QuietServer(ThreadingHTTPServer):
    # Don't wait for idle keep-alive connections on shutdown.
    block_on_close = False

    def handle_error(self, request, client_address):
        # Clients (TLS probes, closed event streams) hang up all the time; that's not interesting.
        pass


class MockBridge:
    """Run a mock bridge in a background thread (used by tests)."""

    def __init__(self, world: World | None = None, host: str = "127.0.0.1", port: int = 0):
        self.world = world or World.synthetic()
        self.server = _QuietServer((host, port), make_handler(self.world))
        self.server.daemon_threads = True
        self.server.socket = self_signed_context().wrap_socket(self.server.socket, server_side=True)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "MockBridge":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.stopping = True
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--snapshot", type=Path, help="seed resources from a saved GET /clip/v2/resource response")
    ap.add_argument("--manual-link", action="store_true", help="require POST /mock/press-link before pairing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    world = World.from_snapshot(args.snapshot) if args.snapshot else World.synthetic()
    world.auto_link = not args.manual_link
    bridge = MockBridge(world, args.host, args.port)
    bridge.server.verbose = args.verbose
    print(f"Mock Hue bridge on https://{bridge.host}:{bridge.port} ({len(world.res)} resources)", flush=True)
    try:
        bridge.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
