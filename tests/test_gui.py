"""End-to-end: the real main window driving the mock bridge."""

import pytest
from conftest import first, writes
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QListWidget, QPushButton, QSlider

from hueman.config import AppConfig
from hueman.ui import pages, widgets
from hueman.ui.main_window import MainWindow


@pytest.fixture
def window(qtbot, paired, mock_bridge, monkeypatch):
    # Destructive actions ask first; tests always say yes.
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(pages, "confirm", lambda *a, **k: True)
    w = MainWindow(AppConfig.load())
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.session is not None and w.session.connected and w.store.all(), timeout=5000)
    # Let the event stream attach before we start changing things.
    qtbot.waitUntil(lambda: mock_bridge.world.subscribers, timeout=3000)
    qtbot.waitUntil(lambda: w.nav._items, timeout=3000)
    yield w
    w.close()


def light_device(world, kind="color"):
    def has(d):
        lights = [world.res[s["rid"]] for s in d["services"] if s["rtype"] == "light"]
        return lights and (kind != "color" or "color" in lights[0])
    return first(world, "device", has)


def test_tree_lists_everything(window, mock_bridge):
    for r in mock_bridge.world.res.values():
        if r["type"] in ("room", "zone", "scene", "behavior_instance"):
            assert window.nav.select(r["id"]), r["type"]
        if r["type"] == "device" and not any(s["rtype"] == "bridge" for s in r["services"]):
            assert window.nav.select(r["id"]), window.store.name(r)


def test_light_controls_write_to_bridge(window, qtbot, mock_bridge):
    world = mock_bridge.world
    dev = light_device(world)
    light_id = next(s["rid"] for s in dev["services"] if s["rtype"] == "light")
    window.select(dev["id"])
    page = window.page
    assert isinstance(page, pages.DevicePage)
    controls = page.findChild(widgets.LightControls)
    controls.on.click()
    qtbot.waitUntil(lambda: world.res[light_id]["on"]["on"] is True, timeout=3000)
    controls.bri.setValue(25)
    qtbot.waitUntil(lambda: world.res[light_id]["dimming"]["brightness"] == 25.0, timeout=3000)
    controls.color.picked.emit(0.2, 0.3)
    qtbot.waitUntil(lambda: world.res[light_id]["color"]["xy"] == {"x": 0.2, "y": 0.3}, timeout=3000)


def test_rapid_slider_moves_are_throttled(window, qtbot, mock_bridge):
    world = mock_bridge.world
    dev = light_device(world)
    light_id = next(s["rid"] for s in dev["services"] if s["rtype"] == "light")
    window.select(dev["id"])
    slider = window.page.findChild(widgets.LightControls).bri
    before = len(writes(world))
    for v in range(1, 101):
        slider.setValue(v)
    qtbot.waitUntil(lambda: world.res[light_id]["dimming"]["brightness"] == 100.0, timeout=3000)
    sent = len(writes(world)) - before
    assert sent <= 5, f"{sent} PUTs for one slider sweep"


def test_external_change_updates_open_page(window, qtbot, mock_bridge):
    world = mock_bridge.world
    dev = light_device(world)
    light_id = next(s["rid"] for s in dev["services"] if s["rtype"] == "light")
    window.select(dev["id"])
    controls = window.page.findChild(widgets.LightControls)
    with world.lock:  # someone else (e.g. the Hue app) changes the light
        world.put("light", light_id, {"dimming": {"brightness": 33.0}, "on": {"on": True}})
    qtbot.waitUntil(lambda: controls.bri.value() == 33 and controls.on.isChecked(), timeout=3000)


def test_rename_device(window, qtbot, mock_bridge):
    dev = light_device(mock_bridge.world)
    window.select(dev["id"])
    name = window.page.findChild(widgets.NameEdit)
    name.setText("Reading lamp")
    name.editingFinished.emit()
    qtbot.waitUntil(lambda: mock_bridge.world.res[dev["id"]]["metadata"]["name"] == "Reading lamp", timeout=3000)
    qtbot.waitUntil(lambda: window.nav.tree.findItems("Reading lamp", Qt.MatchRecursive), timeout=3000)


def test_move_device_between_rooms(window, qtbot, mock_bridge):
    world = mock_bridge.world
    bedroom = first(world, "room", lambda r: r["metadata"]["name"] == "Bedroom")
    dev = light_device(world)  # in the living room
    window.select(dev["id"])
    combo = window.page.room_combo
    combo.setCurrentIndex(combo.findData(bedroom["id"]))
    combo.activated.emit(combo.currentIndex())
    qtbot.waitUntil(lambda: any(c["rid"] == dev["id"] for c in world.res[bedroom["id"]]["children"]), timeout=3000)
    rooms_with_dev = [r for r in world.res.values() if r["type"] == "room"
                      and any(c["rid"] == dev["id"] for c in r["children"])]
    assert rooms_with_dev == [bedroom]


def test_pair_new_device(window, qtbot, mock_bridge):
    world = mock_bridge.world
    devices_before = sum(1 for r in world.res.values() if r["type"] == "device")
    window.open_device_pairing()
    dlg = window._pairing_dialog
    dlg.start_btn.click()
    qtbot.waitUntil(lambda: dlg.found.count() == 1, timeout=5000)
    qtbot.waitUntil(lambda: "finished" in dlg.state.text(), timeout=5000)
    assert sum(1 for r in world.res.values() if r["type"] == "device") == devices_before + 1
    new_id = dlg.found.item(0).data(Qt.UserRole)
    assert window.store.get(new_id)
    dlg.close()
    # The new device is in the tree (under "No room") and can be opened.
    qtbot.waitUntil(lambda: window.nav.select(new_id), timeout=3000)


def test_pairing_rejects_bad_serials(window, mock_bridge):
    window.open_device_pairing()
    dlg = window._pairing_dialog
    dlg.serials.setText("12345, ZZZZZZ")
    dlg.start_btn.click()
    assert "6 hexadecimal" in dlg.state.text()
    assert not [w for w in writes(mock_bridge.world) if "zigbee_device_discovery" in w[1]]
    dlg.close()


def test_delete_device(window, qtbot, mock_bridge):
    world = mock_bridge.world
    dev = light_device(world)
    light_id = next(s["rid"] for s in dev["services"] if s["rtype"] == "light")
    window.select(dev["id"])
    btn = next(b for b in window.page.findChildren(QPushButton) if b.text() == "Delete device")
    btn.click()
    qtbot.waitUntil(lambda: dev["id"] not in world.res, timeout=3000)
    assert light_id not in world.res
    qtbot.waitUntil(lambda: window.store.get(dev["id"]) is None, timeout=3000)
    qtbot.waitUntil(lambda: not window.nav.select(dev["id"]), timeout=3000)
    for scene in (r for r in world.res.values() if r["type"] == "scene"):
        assert all(a["target"]["rid"] != light_id for a in scene["actions"])


def test_bridge_device_cannot_be_deleted(window, mock_bridge):
    bridge_dev = first(mock_bridge.world, "device", lambda d: any(s["rtype"] == "bridge" for s in d["services"]))
    window.select(bridge_dev["id"])
    labels = [b.text() for b in window.page.findChildren(QPushButton)]
    assert "Delete device" not in labels


def test_room_membership_checklist(window, qtbot, mock_bridge):
    world = mock_bridge.world
    hallway = first(world, "room", lambda r: r["metadata"]["name"] == "Hallway")
    window.select(hallway["id"])
    lst = window.page.findChild(QListWidget)
    item = next(lst.item(i) for i in range(lst.count()) if lst.item(i).text().startswith("Bedside"))
    item.setCheckState(Qt.Checked)
    bedside = first(world, "device", lambda d: d["metadata"]["name"] == "Bedside")
    qtbot.waitUntil(lambda: any(c["rid"] == bedside["id"] for c in world.res[hallway["id"]]["children"]),
                    timeout=3000)
    bedroom = first(world, "room", lambda r: r["metadata"]["name"] == "Bedroom")
    assert not any(c["rid"] == bedside["id"] for c in world.res[bedroom["id"]]["children"])


def test_group_controls(window, qtbot, mock_bridge):
    world = mock_bridge.world
    living = first(world, "room", lambda r: r["metadata"]["name"] == "Living room")
    window.select(living["id"])
    window.page.findChild(widgets.LightControls).on.click()
    lights = [world.res[s["rid"]] for c in living["children"] for s in world.res[c["rid"]]["services"]
              if s["rtype"] == "light"]
    qtbot.waitUntil(lambda: all(light["on"]["on"] for light in lights), timeout=3000)


def test_create_room_and_scene(window, qtbot, mock_bridge, monkeypatch):
    world = mock_bridge.world
    sofa = first(world, "device", lambda d: d["metadata"]["name"] == "Sofa lamp")
    window.session.post("room", {"type": "room", "metadata": {"name": "Office", "archetype": "office"},
                                 "children": []},
                        on_done=lambda data: window.select_when_available(data[0]["rid"]))
    qtbot.waitUntil(lambda: isinstance(window.page, pages.GroupPage) and window.store.name(window.page.res) == "Office",
                    timeout=3000)
    office_id = window.page.rid
    # Move the sofa lamp in via the device page, then store a scene from current state.
    window.select(sofa["id"])
    combo = window.page.room_combo
    combo.setCurrentIndex(combo.findData(office_id))
    combo.activated.emit(combo.currentIndex())
    qtbot.waitUntil(lambda: world.res[office_id]["children"], timeout=3000)
    monkeypatch.setattr(pages.QInputDialog, "getText", lambda *a, **k: ("Evening", True))
    pages.create_scene_from_current(window, window, window.store.get(office_id))
    qtbot.waitUntil(lambda: any(r["type"] == "scene" and r["metadata"]["name"] == "Evening"
                                for r in world.res.values()), timeout=3000)
    qtbot.waitUntil(lambda: isinstance(window.page, pages.ScenePage), timeout=3000)
    scene = window.page.res
    assert scene["group"]["rid"] == office_id and len(scene["actions"]) == 1


def test_scene_edit_and_recall(window, qtbot, mock_bridge):
    world = mock_bridge.world
    relax = first(world, "scene", lambda s: s["metadata"]["name"] == "Relax")
    window.select(relax["id"])
    page = window.page
    row_box = page._table.cellWidget(0, 1).findChild(QCheckBox)
    row_box.click()  # turn the first light off in the scene
    qtbot.waitUntil(lambda: world.res[relax["id"]]["actions"][0]["action"]["on"]["on"] is False, timeout=3000)
    next(b for b in page.findChildren(QPushButton) if b.text() == "Activate").click()
    target = world.res[relax["actions"][1]["target"]["rid"]]
    qtbot.waitUntil(lambda: target["on"]["on"] is True, timeout=3000)
    qtbot.waitUntil(lambda: (window.store.get(relax["id"])["status"]["active"]) != "inactive", timeout=3000)


def test_automation_toggle_and_config(window, qtbot, mock_bridge):
    world = mock_bridge.world
    auto = first(world, "behavior_instance")
    window.select(auto["id"])
    page = window.page
    page.findChild(QCheckBox).click()
    qtbot.waitUntil(lambda: world.res[auto["id"]]["enabled"] is True, timeout=3000)
    page.editor.setPlainText('{"when": {}, "where": []}')
    page._apply()
    qtbot.waitUntil(lambda: world.res[auto["id"]]["configuration"] == {"when": {}, "where": []}, timeout=3000)
    qtbot.waitUntil(lambda: page.dirty.text() == "", timeout=3000)


def test_bridge_error_is_reported_and_resynced(window, qtbot, mock_bridge):
    dev = light_device(mock_bridge.world)
    light_id = next(s["rid"] for s in dev["services"] if s["rtype"] == "light")
    window.session.put("light", light_id, {"dimming": {"brightness": 500.0}})
    qtbot.waitUntil(lambda: "failed" in window.statusBar().currentMessage(), timeout=3000)
    # The optimistic value is rolled back by the refresh that follows the failure.
    qtbot.waitUntil(lambda: window.store.get(light_id)["dimming"]["brightness"] <= 100, timeout=3000)


def test_motion_sensor_settings(window, qtbot, mock_bridge):
    world = mock_bridge.world
    motion = first(world, "motion")
    window.select(motion["owner"]["rid"])
    page = window.page
    page.findChild(QCheckBox).click()
    qtbot.waitUntil(lambda: world.res[motion["id"]]["enabled"] is False, timeout=3000)
    page.findChild(QSlider).setValue(4)
    qtbot.waitUntil(lambda: world.res[motion["id"]]["sensitivity"]["sensitivity"] == 4, timeout=3000)


def test_bridges_dialog_pairs_new_bridge(qtbot, mock_bridge, config_dir, monkeypatch):
    from hueman.ui import bridges_dialog
    monkeypatch.setattr(bridges_dialog, "discover", lambda: [])
    monkeypatch.setattr(bridges_dialog, "confirm", lambda *a, **k: True)
    mock_bridge.world.auto_link = False
    cfg = AppConfig()
    dlg = bridges_dialog.BridgesDialog(cfg)
    qtbot.addWidget(dlg)
    dlg.manual.setText(f"{mock_bridge.host}:{mock_bridge.port}")
    dlg.pair_btn.click()
    qtbot.waitUntil(lambda: "link button" in dlg.pair_status.text(), timeout=5000)
    assert AppConfig.load().bridges == []
    import time
    mock_bridge.world.link_pressed_until = time.time() + 30  # user presses the button
    qtbot.waitUntil(lambda: dlg.connect_to is not None, timeout=8000)
    saved = AppConfig.load().find("001788fffe000001")
    assert saved and saved.app_key and len(saved.cert_sha256) == 64 and saved.port == mock_bridge.port


def test_no_bridges_shows_setup(qtbot, config_dir, monkeypatch):
    from hueman.ui import main_window
    opened = []
    monkeypatch.setattr(main_window.MainWindow, "open_bridges", lambda self: opened.append(True))
    w = MainWindow(AppConfig.load())
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: opened, timeout=2000)
    assert not w.act_pair.isEnabled()

