"""Persistent application configuration (paired bridges)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("HUEMAN_CONFIG_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "hueman"


@dataclass
class BridgeConfig:
    bridge_id: str
    name: str
    host: str
    app_key: str
    client_key: str = ""
    # SHA-256 fingerprint of the bridge TLS certificate, pinned on first pairing.
    cert_sha256: str = ""
    port: int = 443

    @property
    def label(self) -> str:
        return f"{self.name} ({self.host})"


@dataclass
class AppConfig:
    bridges: list[BridgeConfig] = field(default_factory=list)
    last_bridge: str = ""

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "bridges.json"

    @classmethod
    def load(cls) -> "AppConfig":
        p = cls.path()
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        known = set(BridgeConfig.__dataclass_fields__)
        bridges = [BridgeConfig(**{k: v for k, v in b.items() if k in known}) for b in raw.get("bridges", [])]
        return cls(bridges=bridges, last_bridge=raw.get("last_bridge", ""))

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        data = {"bridges": [asdict(b) for b in self.bridges], "last_bridge": self.last_bridge}
        # The file holds application keys; keep it private.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)

    def find(self, bridge_id: str) -> BridgeConfig | None:
        bid = bridge_id.lower()
        return next((b for b in self.bridges if b.bridge_id.lower() == bid), None)

    def upsert(self, bridge: BridgeConfig) -> None:
        existing = self.find(bridge.bridge_id)
        if existing:
            self.bridges[self.bridges.index(existing)] = bridge
        else:
            self.bridges.append(bridge)

    def remove(self, bridge_id: str) -> None:
        self.bridges = [b for b in self.bridges if b.bridge_id.lower() != bridge_id.lower()]
        if self.last_bridge.lower() == bridge_id.lower():
            self.last_bridge = self.bridges[0].bridge_id if self.bridges else ""
