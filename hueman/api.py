"""Thin, synchronous client for the Hue CLIP v2 API.

All methods block; the GUI calls them from worker threads (see ``hueman.tasks``).
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
from typing import Any, Iterator

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 10
DEVICE_TYPE = "hueman#" + (socket.gethostname()[:19] or "desktop")


class HueError(Exception):
    """Error reported by the bridge (or a transport failure)."""

    def __init__(self, message: str, status: int | None = None, errors: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []


class LinkButtonNotPressed(HueError):
    pass


class CertificateMismatch(HueError):
    pass


def _url_host(host: str, port: int = 443) -> str:
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return h if port == 443 else f"{h}:{port}"


def fetch_cert_fingerprint(host: str, port: int = 443) -> str:
    """Return the SHA-256 fingerprint (hex) of the certificate the bridge presents."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


def get_public_config(host: str, port: int = 443) -> dict:
    """Unauthenticated bridge info (name, bridgeid, modelid, swversion...)."""
    r = requests.get(f"https://{_url_host(host, port)}/api/0/config", verify=False, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def create_app_key(host: str, port: int = 443, devicetype: str = DEVICE_TYPE) -> tuple[str, str]:
    """Register this application with the bridge. Requires the link button to be pressed.

    Returns ``(app_key, client_key)``.
    """
    r = requests.post(
        f"https://{_url_host(host, port)}/api",
        json={"devicetype": devicetype, "generateclientkey": True},
        verify=False,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    item = body[0] if isinstance(body, list) and body else {}
    if "success" in item:
        return item["success"]["username"], item["success"].get("clientkey", "")
    err = item.get("error", {})
    if err.get("type") == 101:
        raise LinkButtonNotPressed(err.get("description", "link button not pressed"))
    raise HueError(err.get("description", f"unexpected response: {body!r}"))


class _PinnedAdapter(HTTPAdapter):
    """Accepts the bridge's self-signed certificate only if it matches the pinned fingerprint."""

    def __init__(self, fingerprint: str, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        if self._fingerprint:
            kw["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(connections, maxsize, block=block, **kw)


class HueClient:
    def __init__(self, host: str, app_key: str, cert_sha256: str = "", port: int = 443):
        self.host = host
        self.port = port
        self.base = f"https://{_url_host(host, port)}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers["hue-application-key"] = app_key
        self.session.mount("https://", _PinnedAdapter(cert_sha256))

    # -- low level ---------------------------------------------------------

    def _request(self, method: str, path: str, payload: Any = None) -> list[dict]:
        url = f"{self.base}/clip/v2{path}"
        try:
            r = self.session.request(method, url, json=payload, timeout=TIMEOUT)
        except requests.exceptions.SSLError as e:
            if "fingerprint" in str(e).lower():
                raise CertificateMismatch(
                    "The bridge presented a different TLS certificate than the one pinned when it was paired."
                ) from e
            raise HueError(f"TLS error: {e}") from e
        except requests.RequestException as e:
            raise HueError(f"Cannot reach bridge at {self.host}: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        errors = [e.get("description", str(e)) for e in body.get("errors", [])] if isinstance(body, dict) else []
        if r.status_code >= 400:
            msg = "; ".join(errors) or f"HTTP {r.status_code} {r.reason}"
            if r.status_code in (401, 403):
                msg = f"Not authorized ({msg}). The application key may have been revoked; pair the bridge again."
            raise HueError(msg, r.status_code, errors)
        if errors and r.status_code == 207:
            raise HueError("Partially applied: " + "; ".join(errors), r.status_code, errors)
        return body.get("data", []) if isinstance(body, dict) else []

    # -- resources ---------------------------------------------------------

    def get_all(self) -> list[dict]:
        return self._request("GET", "/resource")

    def get(self, rtype: str, rid: str | None = None) -> list[dict]:
        return self._request("GET", f"/resource/{rtype}" + (f"/{rid}" if rid else ""))

    def put(self, rtype: str, rid: str, payload: dict) -> list[dict]:
        return self._request("PUT", f"/resource/{rtype}/{rid}", payload)

    def post(self, rtype: str, payload: dict) -> list[dict]:
        return self._request("POST", f"/resource/{rtype}", payload)

    def delete(self, rtype: str, rid: str) -> list[dict]:
        return self._request("DELETE", f"/resource/{rtype}/{rid}")

    # -- events ------------------------------------------------------------

    def open_event_stream(self) -> requests.Response:
        try:
            r = self.session.get(
                f"{self.base}/eventstream/clip/v2",
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=(TIMEOUT, 120),
            )
        except requests.exceptions.SSLError as e:
            raise CertificateMismatch(str(e)) from e
        except requests.RequestException as e:
            raise HueError(f"Cannot open event stream: {e}") from e
        if r.status_code != 200:
            r.close()
            raise HueError(f"Event stream refused: HTTP {r.status_code}", r.status_code)
        # text/event-stream has no charset parameter; requests would fall back to latin-1.
        r.encoding = "utf-8"
        return r

    @staticmethod
    def iter_events(response: requests.Response) -> Iterator[dict]:
        """Yield individual events (``{"type": "update", "data": [...]}``) from an SSE response."""
        data_lines: list[str] = []
        for line in _sse_lines(response):
            if line == "":
                if data_lines:
                    try:
                        containers = json.loads("\n".join(data_lines))
                    except ValueError:
                        containers = []
                    data_lines = []
                    for ev in containers if isinstance(containers, list) else []:
                        yield ev
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())


def _sse_lines(response: requests.Response) -> Iterator[str]:
    """Yield decoded lines as soon as they arrive.

    ``Response.iter_lines`` waits for a full 512-byte chunk, which would delay small events.
    """
    raw = response.raw
    buf = b""
    while True:
        chunk = raw.read1(65536) if hasattr(raw, "read1") else raw.read(1)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.rstrip(b"\r").decode("utf-8", errors="replace")


def abort_stream(response: requests.Response) -> None:
    """Interrupt a streaming response that another thread is reading."""
    conn = getattr(response.raw, "connection", None) or getattr(response.raw, "_connection", None)
    sock = getattr(conn, "sock", None)
    try:
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
