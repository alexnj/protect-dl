import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from protect_dl.client import AuthError, CameraNotFound, ExportTruncated, ProtectClient

CAMERAS = [
    {"id": "a1", "name": "Front Door", "marketName": "G5 Pro", "state": "CONNECTED", "isAdopted": True,
     "channels": [{"name": "High", "width": 2688, "height": 1512, "enabled": True}]},
    {"id": "b2", "name": "garage", "type": "UVC G4 Bullet", "state": "DISCONNECTED", "isAdopted": True},
    {"id": "c3", "name": "New Cam", "state": "CONNECTED", "isAdopted": False},
    {"id": "d4", "name": "Doorbell", "marketName": "G4 Doorbell Pro", "state": "CONNECTED", "isAdopted": True,
     "featureFlags": {"hasPackageCamera": True},
     "channels": [{"name": n, "width": 1600, "height": 1200, "enabled": True} for n in ("High", "Medium", "Low")]},
]


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        for i in range(0, len(self.body), 300):
            yield self.body[i:i + 300]


class FakeConsole:
    def __init__(self, body=b"v" * 1000, content_length=None, password="pw"):
        self.body, self.content_length, self.password = body, content_length, password
        self.logins = 0
        self.expire_session = False
        self.export_params = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            self.logins += 1
            import json
            if json.loads(request.content)["password"] != self.password:
                return httpx.Response(401)
            return httpx.Response(200, headers={"set-cookie": "TOKEN=abc; Path=/", "x-csrf-token": "csrf1"})
        if request.headers.get("cookie") != "TOKEN=abc" or self.expire_session:
            return httpx.Response(401)
        if path == "/proxy/protect/api/nvr":
            return httpx.Response(200, json={"version": "6.1.79", "marketName": "UNVR", "firmwareVersion": "4.3.6"})
        if path == "/proxy/protect/api/cameras":
            return httpx.Response(200, json=CAMERAS)
        if path == "/proxy/protect/api/video/export":
            self.export_params = dict(request.url.params)
            headers = {"content-length": str(self.content_length or len(self.body))}
            return httpx.Response(200, stream=BodyStream(self.body), headers=headers)
        return httpx.Response(404)


def client(console):
    return ProtectClient("console.local", "u", "pw", verify_ssl=True, transport=httpx.MockTransport(console))


def test_login_and_cameras():
    async def go():
        async with client(FakeConsole()) as c:
            assert c.http.headers["x-csrf-token"] == "csrf1"
            assert [cam.name for cam in c.cameras()] == ["Doorbell", "Front Door", "garage"]  # unadopted hidden
            assert c.find_camera("GARAGE").model == "UVC G4 Bullet"
            assert c.find_camera("front door").channels[0].width == 2688
            with pytest.raises(CameraNotFound, match="Available: 'Doorbell', 'Front Door', 'garage'"):
                c.find_camera("Back")
    asyncio.run(go())


def test_bad_password():
    async def go():
        with pytest.raises(AuthError, match="login rejected"):
            async with ProtectClient("console.local", "u", "wrong", True,
                                     transport=httpx.MockTransport(FakeConsole())):
                pass
    asyncio.run(go())


def test_export_streams_to_file_with_progress(tmp_path):
    console = FakeConsole()
    seen = []
    start = datetime(2026, 9, 19, 2, tzinfo=UTC)

    async def go():
        async with client(console) as c:
            return await c.download_chunk("a1", start, start + timedelta(hours=1), "medium",
                                          tmp_path / "x.part", lambda n, total: seen.append((n, total)))
    assert asyncio.run(go()) == 1000
    assert (tmp_path / "x.part").read_bytes() == b"v" * 1000
    assert console.export_params == {"camera": "a1", "start": str(int(start.timestamp() * 1000)),
                                     "end": str(int(start.timestamp() * 1000) + 3_600_000), "channel": "1"}
    assert sum(n for n, _ in seen) == 1000 and all(t == 1000 for _, t in seen)


def test_export_truncated(tmp_path):
    console = FakeConsole(body=b"v" * 900, content_length=1000)

    async def go():
        async with client(console) as c:
            await c.download_chunk("a1", datetime.now(UTC), datetime.now(UTC),
                                   "high", tmp_path / "x.part", lambda n, t: None)
    with pytest.raises(ExportTruncated, match="900 of 1000"):
        asyncio.run(go())


def test_expired_session_raises_auth_error_and_relogin_recovers(tmp_path):
    console = FakeConsole()

    async def go():
        async with client(console) as c:
            console.expire_session = True
            with pytest.raises(AuthError):
                await c.download_chunk("a1", datetime.now(UTC), datetime.now(UTC),
                                       "high", tmp_path / "x.part", lambda n, t: None)
            console.expire_session = False
            await c.relogin()
            await c.refresh_cameras()
    asyncio.run(go())
    assert console.logins == 2


def test_channel_support():
    async def go():
        async with client(FakeConsole()) as c:
            door, front, garage = c.find_camera("Doorbell"), c.find_camera("Front Door"), c.find_camera("garage")
            assert door.supports("package") and door.supports("low")
            assert "package lens" in door.channel_summary()
            assert front.supports("high") and not front.supports("medium") and not front.supports("package")
            assert garage.supports("low")  # no channel list reported: don't block
    asyncio.run(go())


def test_package_lens_export_params(tmp_path):
    console = FakeConsole()
    start = datetime(2026, 9, 19, 2, tzinfo=UTC)

    async def go():
        async with client(console) as c:
            await c.download_chunk("d4", start, start + timedelta(minutes=1), "package",
                                   tmp_path / "x.part", lambda n, t: None)
    asyncio.run(go())
    assert console.export_params["lens"] == "2" and "channel" not in console.export_params


def test_debug_log_has_versions_and_requests_but_no_secrets(tmp_path):
    lines = []

    async def go():
        async with ProtectClient("console.local", "u", "pw", True,
                                 transport=httpx.MockTransport(FakeConsole()), log=lines.append) as c:
            await c.download_chunk("a1", datetime.now(UTC), datetime.now(UTC),
                                   "high", tmp_path / "x.part", lambda n, t: None)
    asyncio.run(go())
    text = "\n".join(lines)
    assert "Protect 6.1.79 on UNVR (UniFi OS 4.3.6)" in text
    assert "→ POST /api/auth/login" in text and "← 200 POST /api/auth/login" in text
    assert "← 200 GET /proxy/protect/api/video/export" in text and "content-length=1000" in text
    for secret in ("pw", "abc", "csrf1"):
        assert secret not in text.replace("/proxy/", "")
