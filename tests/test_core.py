"""Store, API client and config, without the GUI."""

import os
import stat
import threading
import time

import pytest
from conftest import first

from hueman import api
from hueman.colors import kelvin_to_mirek, mirek_to_kelvin, rgb_to_xy, xy_to_rgb
from hueman.config import AppConfig, BridgeConfig
from hueman.store import ResourceStore


def test_store_applies_add_update_delete(qtbot):
    store = ResourceStore()
    store.load([{"id": "a", "type": "light", "on": {"on": False}, "dimming": {"brightness": 10.0}}])
    changed, removed = [], []
    store.changed.connect(changed.append)
    store.removed.connect(lambda rid, rtype: removed.append((rid, rtype)))

    store.apply_event({"type": "update", "data": [{"id": "a", "type": "light", "on": {"on": True}}]})
    assert store.get("a")["on"] == {"on": True}
    assert store.get("a")["dimming"] == {"brightness": 10.0}, "partial updates must merge, not replace"
    store.apply_event({"type": "add", "data": [{"id": "b", "type": "scene", "metadata": {"name": "X"}}]})
    assert store.name(store.get("b")) == "X"
    store.apply_event({"type": "delete", "data": [{"id": "a", "type": "light"}]})
    assert store.get("a") is None
    assert changed == ["a"] and removed == [("a", "light")]


def test_store_name_falls_back_to_owner():
    store = ResourceStore()
    store.load([{"id": "d", "type": "device", "metadata": {"name": "Lamp"}, "services": []},
                {"id": "m", "type": "motion", "owner": {"rid": "d", "rtype": "device"}}])
    assert store.name(store.get("m")) == "Lamp"


def test_config_roundtrip_is_private(config_dir):
    cfg = AppConfig(bridges=[BridgeConfig("abc", "Home", "10.0.0.2", "key")], last_bridge="abc")
    cfg.save()
    mode = stat.S_IMODE(os.stat(AppConfig.path()).st_mode)
    assert mode == 0o600
    loaded = AppConfig.load()
    assert loaded.find("ABC").host == "10.0.0.2"
    loaded.remove("abc")
    assert loaded.bridges == [] and loaded.last_bridge == ""


def test_colour_conversions_roundtrip():
    x, y = rgb_to_xy(255, 0, 0)
    r, g, b = xy_to_rgb(x, y)
    assert r > 200 and g < 60 and b < 60
    assert mirek_to_kelvin(kelvin_to_mirek(2700)) in range(2690, 2711)


def test_link_button_required(mock_bridge):
    mock_bridge.world.auto_link = False
    with pytest.raises(api.LinkButtonNotPressed):
        api.create_app_key(mock_bridge.host, mock_bridge.port)
    mock_bridge.world.link_pressed_until = time.time() + 5
    key, client_key = api.create_app_key(mock_bridge.host, mock_bridge.port)
    assert key and client_key


def test_client_reads_writes_and_reports_errors(paired, mock_bridge):
    b = paired.bridges[0]
    c = api.HueClient(b.host, b.app_key, b.cert_sha256, b.port)
    assert any(r["type"] == "device" for r in c.get_all())
    light = first(mock_bridge.world, "light")
    c.put("light", light["id"], {"dimming": {"brightness": 42.0}})
    assert mock_bridge.world.res[light["id"]]["dimming"]["brightness"] == 42.0
    with pytest.raises(api.HueError) as e:
        c.put("light", light["id"], {"metadata": {"name": "x" * 40}})
    assert e.value.status == 400


def test_wrong_key_is_rejected(paired, mock_bridge):
    b = paired.bridges[0]
    with pytest.raises(api.HueError) as e:
        api.HueClient(b.host, "nope", b.cert_sha256, b.port).get_all()
    assert "Not authorized" in str(e.value)


def test_certificate_pinning(paired, mock_bridge):
    b = paired.bridges[0]
    with pytest.raises(api.CertificateMismatch):
        api.HueClient(b.host, b.app_key, "ab" * 32, b.port).get_all()


def test_event_stream_delivers_utf8_events(paired, mock_bridge):
    b = paired.bridges[0]
    c = api.HueClient(b.host, b.app_key, b.cert_sha256, b.port)
    events = []
    resp = c.open_event_stream()
    t = threading.Thread(target=lambda: events.extend(c.iter_events(resp)), daemon=True)
    t.start()
    time.sleep(0.3)
    light = first(mock_bridge.world, "light")
    c.put("light", light["id"], {"metadata": {"name": "Kuchyň střed"}})
    deadline = time.time() + 3
    while not events and time.time() < deadline:
        time.sleep(0.05)
    api.abort_stream(resp)
    assert events and events[0]["data"][0]["metadata"]["name"] == "Kuchyň střed"
