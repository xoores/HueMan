import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mock_bridge import MockBridge, World  # noqa: E402

from hueman import api  # noqa: E402
from hueman.config import AppConfig, BridgeConfig  # noqa: E402


@pytest.fixture
def mock_bridge():
    b = MockBridge(World.synthetic()).start()
    b.world.discovery_delay = 0.5
    yield b
    b.stop()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HUEMAN_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


@pytest.fixture
def paired(mock_bridge, config_dir):
    key, ck = api.create_app_key(mock_bridge.host, mock_bridge.port)
    fp = api.fetch_cert_fingerprint(mock_bridge.host, mock_bridge.port)
    bridge = BridgeConfig("001788fffe000001", "Mock", mock_bridge.host, key, ck, fp, mock_bridge.port)
    cfg = AppConfig(bridges=[bridge], last_bridge=bridge.bridge_id)
    cfg.save()
    return cfg


def first(world, rtype, pred=lambda r: True):
    return next(r for r in world.res.values() if r["type"] == rtype and pred(r))


def writes(world):
    return [entry for entry in world.request_log if entry[0] != "GET"]


@pytest.fixture(autouse=True)
def _truthy_wait_until(monkeypatch):
    """Let ``qtbot.waitUntil`` predicates return any truthy value, not just bools."""
    from pytestqt.qtbot import QtBot
    orig = QtBot.waitUntil
    monkeypatch.setattr(QtBot, "waitUntil", lambda self, cb, **kw: orig(self, lambda: bool(cb()), **kw))
