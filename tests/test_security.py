from __future__ import annotations

import os
import subprocess
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent.api import ApiError
from agent.security import LinkSigner, Pairing, RateLimiter, is_lan, is_loopback

from .conftest import PHONE_IP, Env


async def test_health_is_public(env: Env):
    async with env.client() as c:
        res = await c.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and "version" in body
    assert set(body) == {"ok", "name", "version"}


async def test_no_token_is_401(env: Env):
    async with env.client() as c:
        res = await c.get("/api/v1/me")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthorized"


async def test_wrong_token_is_401(env: Env, token: str):
    async with env.client(token="x" * 43) as c:
        res = await c.get("/api/v1/me")
    assert res.status_code == 401


async def test_good_token_works(env: Env, token: str):
    async with env.client(token=token) as c:
        res = await c.get("/api/v1/me")
    assert res.status_code == 200
    assert res.json()["device"]["name"] == "Test iPhone"
    assert "hash" not in res.json()["device"]


async def test_missing_custom_header_rejected(env: Env, token: str):
    async with env.client(token=token) as c:
        res = await c.get("/api/v1/me", headers={"X-LanDeck": ""})
    assert res.status_code == 403


async def test_sixth_failure_blocks_ip(env: Env, token: str):
    async with env.client(ip="192.168.1.77", token="nope") as c:
        codes = [(await c.get("/api/v1/me")).status_code for _ in range(5)]
        assert codes == [401] * 5
        res = await c.get("/api/v1/me")
        assert res.status_code == 429
    # even a valid token is refused while the IP is blocked
    async with env.client(ip="192.168.1.77", token=token) as c:
        assert (await c.get("/api/v1/me")).status_code == 429
    # other IPs are unaffected
    async with env.client(token=token) as c:
        assert (await c.get("/api/v1/me")).status_code == 200
    assert any(e["action"] == "auth.blocked" for e in env.ctx.audit.tail())


def test_rate_limiter_window_and_expiry():
    now = [1000.0]
    rl = RateLimiter(clock=lambda: now[0])
    for _ in range(4):
        assert rl.fail("a") is False
    now[0] += 61  # failures outside the window don't count
    assert rl.fail("a") is False
    for _ in range(3):
        rl.fail("a")
    assert rl.fail("a") is True
    assert rl.blocked("a") > 0
    now[0] += 301
    assert rl.blocked("a") == 0


async def test_non_lan_source_rejected(env: Env, token: str):
    for ip in ("8.8.8.8", "100.64.1.1", "2001:db8::1"):
        async with env.client(ip=ip, token=token) as c:
            res = await c.get("/api/v1/health")
        assert res.status_code == 403, ip


def test_lan_classification():
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.254", "192.168.0.9", "169.254.3.3",
               "::1", "fe80::1", "::ffff:192.168.1.2"):
        assert is_lan(ip), ip
    for ip in ("8.8.8.8", "172.32.0.1", "100.64.0.1", "2001:db8::1", "", "testclient", None):
        assert not is_lan(ip), ip
    assert is_loopback("::1") and is_loopback("127.0.0.1") and not is_loopback("192.168.1.1")


async def test_pairing_code_single_use(env: Env):
    async with env.local() as admin:
        code = (await admin.post("/admin/api/pair")).json()["code"]
    async with env.client() as phone:
        ok = await phone.post("/api/v1/pair", json={"code": code, "name": "A"})
        again = await phone.post("/api/v1/pair", json={"code": code, "name": "B"})
    assert ok.status_code == 200
    assert again.status_code == 401
    assert len(env.ctx.devices.all()) == 1


async def test_pairing_secret_from_qr(env: Env):
    async with env.local() as admin:
        data = (await admin.post("/admin/api/pair")).json()
    secret = data["urls"]["ip"].split("#", 1)[1]
    assert data["qr"].startswith("<?xml") or "<svg" in data["qr"]
    async with env.client() as phone:
        res = await phone.post("/api/v1/pair", json={"secret": secret, "name": "QR phone"})
    assert res.status_code == 200
    assert env.prompts[-1] == ("QR phone", PHONE_IP)


async def test_pairing_expires(env: Env):
    async with env.local() as admin:
        code = (await admin.post("/admin/api/pair")).json()["code"]
    env.ctx.pairing._session["expires"] = time.time() - 1
    async with env.client() as phone:
        res = await phone.post("/api/v1/pair", json={"code": code, "name": "Late"})
    assert res.status_code == 410
    assert env.ctx.devices.all() == []


async def test_pairing_denied_on_pc(env: Env):
    env.approve = False
    async with env.local() as admin:
        code = (await admin.post("/admin/api/pair")).json()["code"]
    async with env.client() as phone:
        res = await phone.post("/api/v1/pair", json={"code": code, "name": "Sneaky"})
    assert res.status_code == 403
    assert env.ctx.devices.all() == []


def test_pairing_unit():
    now = [0.0]
    p = Pairing(clock=lambda: now[0])
    s = p.start()
    assert p.consume(code="000000" if s["code"] != "000000" else "111111") == "invalid"
    assert p.consume(code=s["code"]) == "ok"
    assert p.consume(code=s["code"]) == "invalid"
    s = p.start()
    now[0] += 121
    assert p.consume(secret=s["secret"]) == "expired"


async def test_tokens_stored_hashed(env: Env, token: str):
    raw = (env.ctx.home / "devices.json").read_text()
    assert token not in raw
    import hashlib

    assert hashlib.sha256(token.encode()).hexdigest() in raw


async def test_revoked_token_401_and_ws_closed(env: Env, token: str):
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 52000)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            dev_id = hello["data"]["device"]["id"]
            res = tc.delete(f"/admin/api/devices/{dev_id}", headers={"X-LanDeck": "1"})
            # the admin call comes from a LAN IP here, so it must be refused
            assert res.status_code == 403

        with TestClient(env.app, base_url="https://localhost:8765", client=("127.0.0.1", 52001)) as admin:
            with tc.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth", "token": token})
                assert ws.receive_json()["type"] == "hello"
                res = admin.delete(f"/admin/api/devices/{dev_id}", headers={"X-LanDeck": "1"})
                assert res.status_code == 200
                msg = ws.receive_json()
                assert msg["type"] == "revoked"
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4401
        res = tc.get("/api/v1/me", headers={"Authorization": f"Bearer {token}", "X-LanDeck": "1"})
        assert res.status_code == 401


def test_ws_rejects_bad_token_and_bad_origin(env: Env):
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 52002)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "garbage"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
        assert exc.value.code == 4403


def test_ws_auth_timeout(env: Env):
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 52003)) as tc:
        with tc.websocket_connect("/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4408


async def test_admin_is_loopback_only(env: Env):
    async with env.client() as phone:
        assert (await phone.get("/admin/api/overview")).status_code == 403
        assert (await phone.post("/admin/api/pair")).status_code == 403
        assert (await phone.get("/admin")).status_code == 403
        assert (await phone.get("/admin.html")).status_code == 403
    async with env.local() as admin:
        assert (await admin.get("/admin/api/overview")).status_code == 200
        # DNS rebinding: right IP, wrong Host header
        res = await admin.get("/admin/api/overview", headers={"host": "attacker.example:8765"})
        assert res.status_code == 403
        # writes need the custom header (blocks plain cross-site form posts)
        res = await admin.post("/admin/api/pair", headers={"X-LanDeck": ""})
        assert res.status_code == 403


async def test_remote_cannot_change_features(env: Env, token: str):
    async with env.client(token=token) as phone:
        res = await phone.put("/admin/api/features", json={"terminal": True})
    assert res.status_code == 403
    assert env.ctx.config.feature("terminal") is False


def test_signed_links():
    s = LinkSigner()
    tok = s.sign("dev1", "file", {"r": "share", "p": "a.txt"})
    assert s.verify(tok, "file")["p"] == "a.txt"
    assert s.verify(tok, "thumb") is None
    body, mac = tok.split(".")
    assert s.verify(body + "." + mac[:-2] + "AA", "file") is None
    assert LinkSigner().verify(tok, "file") is None
    expired = s.sign("dev1", "file", {}, ttl=-5)
    assert s.verify(expired, "file") is None


class TestPathSafety:
    def resolve(self, env: Env, rel: str):
        return env.ctx.services["files"].resolve(env.root_id(), rel, must_exist=False)

    async def test_traversal(self, env: Env):
        for rel in ("..", "../x", "a/../../x", "..\\..\\Windows", "a/..", "C:/Windows", "c:\\windows",
                    "file.txt:stream", "a/\x01b"):
            with pytest.raises(ApiError):
                self.resolve(env, rel)
        # leading slashes are stripped, so a UNC-looking path stays inside the root
        assert str(self.resolve(env, "//server/share")).startswith(str(env.share))

    async def test_inside_is_fine(self, env: Env):
        (env.share / "sub").mkdir()
        assert self.resolve(env, "sub") == env.share / "sub"
        assert self.resolve(env, "") == env.share

    async def test_junction_escape(self, env: Env, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        link = env.share / "escape"
        res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if res.returncode != 0:
            pytest.skip("could not create junction")
        with pytest.raises(ApiError) as exc:
            self.resolve(env, "escape/secret.txt")
        assert exc.value.error == "outside_root"
        os.rmdir(link)

    async def test_api_rejects_traversal(self, env: Env, token: str):
        async with env.client(token=token) as c:
            for rel in ("..", "../..", "..\\"):
                res = await c.get("/api/v1/files/list", params={"root": env.root_id(), "path": rel})
                assert res.status_code in (400, 403), rel
            res = await c.get("/api/v1/files/list", params={"root": "nope", "path": ""})
            assert res.status_code == 404
