"""Find Hue bridges on the local network via mDNS (_hue._tcp)."""

from __future__ import annotations

import time
from dataclasses import dataclass

SERVICE = "_hue._tcp.local."


@dataclass
class FoundBridge:
    name: str
    host: str
    port: int
    bridge_id: str
    model: str


def discover(timeout: float = 4.0) -> list[FoundBridge]:
    """Browse mDNS for ``timeout`` seconds and return the bridges that answered."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, FoundBridge] = {}

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            addrs = info.parsed_addresses()
            # Prefer IPv4: the bridge certificate and most routers behave better with it.
            addrs.sort(key=lambda a: ":" in a)
            if not addrs:
                return
            props = {k.decode(): (v.decode() if v else "") for k, v in (info.properties or {}).items()}
            bid = props.get("bridgeid", "").lower()
            found[bid or name] = FoundBridge(
                name=name.removesuffix("." + SERVICE).replace("\\032", " "),
                host=addrs[0],
                port=info.port or 443,
                bridge_id=bid,
                model=props.get("modelid", ""),
            )

        update_service = add_service

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return sorted(found.values(), key=lambda b: b.name)
