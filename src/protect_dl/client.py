"""Minimal client for the UniFi OS / Protect private API: login, cameras and video export."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .timeparse import to_ms

# Export query parameters for each --channel choice. "package" is the second, downward-facing
# lens on cameras such as the G4 Doorbell Pro.
CHANNELS: dict[str, dict[str, int]] = {
    "high": {"channel": 0},
    "medium": {"channel": 1},
    "low": {"channel": 2},
    "package": {"lens": 2},
}
PROTECT_API = "/proxy/protect/api"
BLOCK_SIZE = 256 * 1024


class ProtectError(Exception):
    pass


class AuthError(ProtectError):
    pass


class CameraNotFound(ProtectError, LookupError):
    pass


class ExportTruncated(ProtectError):
    """The console closed the export before sending the promised Content-Length."""


@dataclass
class Channel:
    name: str
    width: int
    height: int
    enabled: bool


@dataclass
class Camera:
    id: str
    name: str
    model: str
    state: str
    channels: list[Channel]
    has_package: bool = False

    def supports(self, channel: str) -> bool:
        if channel == "package":
            return self.has_package
        return not self.channels or list(CHANNELS).index(channel) < len(self.channels)

    def channel_summary(self) -> str:
        names = [f"{c.name} {c.width}x{c.height}" for c in self.channels[:3] if c.enabled]
        if self.has_package:
            names.append("package lens")
        return ", ".join(names)

    @classmethod
    def from_json(cls, d: dict) -> Camera:
        return cls(
            id=d["id"],
            name=d.get("name") or d["id"],
            model=d.get("marketName") or d.get("type") or "",
            state=d.get("state", ""),
            channels=[
                Channel(c.get("name", ""), c.get("width", 0), c.get("height", 0), c.get("enabled", True))
                for c in d.get("channels", [])
            ],
            has_package=bool(d.get("featureFlags", {}).get("hasPackageCamera"))
            or len(d.get("channels", [])) > 3,
        )


def base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    return host if host.startswith(("https://", "http://")) else f"https://{host}"


def _raise_for_status(r: httpx.Response, what: str) -> None:
    if r.status_code in (401, 403):
        raise AuthError(f"{what}: HTTP {r.status_code} (not logged in or no Protect access)")
    if r.status_code >= 400:
        raise ProtectError(f"{what}: HTTP {r.status_code} {r.reason_phrase}")


class ProtectClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool,
        transport: httpx.AsyncBaseTransport | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.username, self.password = username, password
        self.log = log
        hooks = {"request": [self._log_request], "response": [self._log_response]} if log else {}
        self.http = httpx.AsyncClient(
            event_hooks=hooks,
            base_url=base_url(host),
            verify=verify_ssl,
            transport=transport,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "protect-dl"},
        )
        self._cameras: dict[str, Camera] = {}

    # ---- debug logging (never logs request bodies, cookies or tokens) ------------

    async def _log_request(self, request: httpx.Request) -> None:
        request.extensions["protect_dl_t0"] = time.monotonic()
        query = f"?{request.url.query.decode()}" if request.url.query else ""
        self.log(f"→ {request.method} {request.url.path}{query}")

    async def _log_response(self, response: httpx.Response) -> None:
        request = response.request
        took = time.monotonic() - request.extensions.get("protect_dl_t0", time.monotonic())
        details = [f"{took:.2f}s"]
        for header in ("content-type", "content-length", "transfer-encoding"):
            if value := response.headers.get(header):
                details.append(f"{header}={value}")
        self.log(f"← {response.status_code} {request.method} {request.url.path} ({', '.join(details)})")

    async def log_console_info(self) -> None:
        """Log the Protect version, which is what bug reports most need."""
        try:
            r = await self.http.get(f"{PROTECT_API}/nvr")
            _raise_for_status(r, "reading console info")
            nvr = r.json()
            self.log(
                f"Protect {nvr.get('version', '?')} on {nvr.get('marketName') or nvr.get('type', '?')}"
                f" (UniFi OS {nvr.get('firmwareVersion', '?')})"
            )
        except (ProtectError, httpx.HTTPError, ValueError) as e:
            self.log(f"could not read console info: {e}")

    async def __aenter__(self) -> ProtectClient:
        try:
            await self.login()
            if self.log:
                await self.log_console_info()
            await self.refresh_cameras()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self.http.aclose()

    async def login(self) -> None:
        self.http.cookies.clear()
        r = await self.http.post(
            "/api/auth/login",
            json={"username": self.username, "password": self.password, "rememberMe": False},
        )
        if r.status_code in (400, 401, 403):
            raise AuthError(f"login rejected (HTTP {r.status_code}); check the username and password")
        _raise_for_status(r, "login")
        if csrf := r.headers.get("x-csrf-token"):
            self.http.headers["x-csrf-token"] = csrf

    relogin = login

    async def refresh_cameras(self) -> None:
        r = await self.http.get(f"{PROTECT_API}/cameras")
        _raise_for_status(r, "listing cameras")
        self._cameras = {
            d["id"]: Camera.from_json(d)
            for d in r.json()
            if d.get("id") and d.get("isAdopted", True) and not d.get("isAdoptedByOther")
        }

    def cameras(self) -> list[Camera]:
        return sorted(self._cameras.values(), key=lambda c: c.name.lower())

    def find_camera(self, name: str) -> Camera:
        for cam in self.cameras():
            if cam.name.lower() == name.lower():
                return cam
        names = ", ".join(repr(c.name) for c in self.cameras()) or "(none)"
        raise CameraNotFound(f"No camera named {name!r}. Available: {names}")

    def camera_by_id(self, camera_id: str) -> Camera:
        cam = self._cameras.get(camera_id)
        if cam is None:
            raise CameraNotFound(f"Camera id {camera_id} no longer exists on this console")
        return cam

    async def download_chunk(
        self,
        camera_id: str,
        start: datetime,
        end: datetime,
        channel: str,
        dest: Path,
        on_bytes: Callable[[int, int], None],
    ) -> int:
        """Stream one MP4 export to ``dest``, reporting (bytes_in_block, content_length) per
        block. Returns the number of bytes written."""
        params = {"camera": camera_id, "start": to_ms(start), "end": to_ms(end), **CHANNELS[channel]}
        # No read timeout: the stall watchdog in the downloader decides when to give up.
        timeout = httpx.Timeout(30.0, read=None)
        written = 0
        async with self.http.stream(
            "GET", f"{PROTECT_API}/video/export", params=params, timeout=timeout
        ) as r:
            _raise_for_status(r, "video export")
            total = int(r.headers.get("content-length") or 0)
            on_bytes(0, total)
            with dest.open("wb") as f:
                try:
                    async for block in r.aiter_raw(BLOCK_SIZE):
                        f.write(block)
                        written += len(block)
                        on_bytes(len(block), total)
                except httpx.RemoteProtocolError as e:
                    if total and written < total:
                        raise ExportTruncated(
                            f"console closed the export early ({written} of {total} bytes)"
                        ) from e
                    raise
        if total and written < total:
            raise ExportTruncated(f"console closed the export early ({written} of {total} bytes)")
        return written
