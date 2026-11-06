from __future__ import annotations

import hashlib
import io
import os
import sys
import time
import zipfile

import pytest
from PIL import Image
from starlette.testclient import TestClient

from agent.modules.actions import run_process
from agent.modules.clipboard import looks_secret

from .conftest import PHONE_IP, Env


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-LanDeck": "1"}


# --- dashboard ---------------------------------------------------------------------

async def test_stats_snapshot(env: Env, token: str):
    async with env.client(token=token) as c:
        res = await c.get("/api/v1/system/stats")
        info = await c.get("/api/v1/system/info")
    s = res.json()
    assert 0 <= s["cpu"]["total"] <= 100
    assert s["mem"]["total"] > 0 and s["disks"]
    assert info.json()["cpu"]["logical"] >= 1


def test_stats_stream_over_ws(env: Env, token: str):
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53000)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"type": "sub", "topics": ["stats"]})
            got = []
            deadline = time.time() + 5
            while len(got) < 2 and time.time() < deadline:
                msg = ws.receive_json()
                if msg["type"] == "stats":
                    got.append(msg["data"])
            assert len(got) >= 2
            assert "cpu" in got[-1] and "net" in got[-1]


# --- power -----------------------------------------------------------------------------

async def test_power_requires_confirm_and_is_dry(env: Env, token: str):
    backend = env.ctx.services["power"].backend
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/power/shutdown", json={})).status_code == 400
        assert (await c.post("/api/v1/power/nuke", json={"confirm": True})).status_code == 404
        res = await c.post("/api/v1/power/restart", json={"confirm": True})
        assert res.status_code == 200
        assert (await c.post("/api/v1/power/display-off")).status_code == 200
    assert ("run", "restart", 0) in backend.calls
    assert ("display_off",) in backend.calls
    assert any(e["action"] == "power.restart" for e in env.ctx.audit.tail())


async def test_power_schedule_and_cancel(env: Env, token: str):
    backend = env.ctx.services["power"].backend
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/power/schedule", json={"action": "shutdown", "in_minutes": 10, "confirm": True})
        assert res.status_code == 200
        due = res.json()["schedule"]["due"]
        assert 590 < due - time.time() <= 600
        info = (await c.get("/api/v1/power/info")).json()
        assert info["schedule"]["action"] == "shutdown"
        res = await c.delete("/api/v1/power/schedule")
        assert res.json()["cancelled"] is True
        assert (await c.get("/api/v1/power/info")).json()["schedule"] is None
    assert backend.calls[0][0:2] == ("run", "shutdown") and 595 <= backend.calls[0][2] <= 600
    assert ("abort",) in backend.calls


def test_power_announces_going_offline(env: Env, token: str):
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53001)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_json()
            tc.post("/api/v1/power/sleep", json={"confirm": True}, headers=auth(token))
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "power":
                    assert msg["data"]["state"] == "going_offline"
                    break
            else:
                pytest.fail("no power event")


# --- clipboard ---------------------------------------------------------------------------

def test_clipboard_both_ways(env: Env, token: str):
    svc = env.ctx.services["clipboard"]
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53002)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_json()
            res = tc.post("/api/v1/clipboard", json={"text": "hello from phone"}, headers=auth(token))
            assert res.status_code == 200
            assert svc.backend.read() == ("text", "hello from phone")
            msg = next(m for m in iter(ws.receive_json, None) if m["type"] == "clipboard")
            assert msg["data"]["origin"] == "phone"

            # now the PC copies something
            svc.backend.write_text("copied on the PC")
            svc.on_pc_change()
            msg = next(m for m in iter(ws.receive_json, None) if m["type"] == "clipboard")
            assert msg["data"]["origin"] == "pc"
            assert msg["data"]["item"]["preview"] == "copied on the PC"

            items = tc.get("/api/v1/clipboard", headers=auth(token)).json()["items"]
            assert [i["preview"] for i in items[:2]] == ["copied on the PC", "hello from phone"]


async def test_clipboard_masks_secrets_and_pins(env: Env, token: str):
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/clipboard", json={"text": "password: hunter2hunter2"})
        item = res.json()["item"]
        assert item["secret"] is True and item["preview"] == ""
        full = await c.get(f"/api/v1/clipboard/item/{item['id']}")
        assert full.json()["text"] == "password: hunter2hunter2"
        await c.post(f"/api/v1/clipboard/item/{item['id']}/pin", json={"pinned": True})
        await c.delete("/api/v1/clipboard")
        items = (await c.get("/api/v1/clipboard")).json()["items"]
        assert [i["id"] for i in items] == [item["id"]]


def test_secret_heuristics():
    assert looks_secret("ghp_" + "a" * 36)
    assert looks_secret("sk-proj-Abc123Def456Ghi789Jkl")
    assert looks_secret("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123")
    assert looks_secret("Xk9#mQ2$vL7pR4wZ")
    assert not looks_secret("just a normal sentence about lunch")
    assert not looks_secret("https://example.com/some/long/path/here")


async def test_clipboard_image(env: Env, token: str):
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 30, 90)).save(buf, "JPEG")
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/clipboard/image", content=buf.getvalue())
        assert res.status_code == 200
        item = res.json()["item"]
        thumb = await c.get(f"/api/v1/clipboard/item/{item['id']}/image?thumb=1")
    assert thumb.headers["content-type"] == "image/png"
    assert max(Image.open(io.BytesIO(thumb.content)).size) <= 320
    kind, png = env.ctx.services["clipboard"].backend.read()
    assert kind == "image" and png[:4] == b"\x89PNG"


# --- files ----------------------------------------------------------------------------

async def upload(c, data: bytes, name: str, chunk: int = 4 * 1024 * 1024, **target) -> dict:
    res = await c.post("/api/v1/files/upload", json={"name": name, "size": len(data), **target})
    assert res.status_code == 200, res.text
    uid = res.json()["id"]
    off = 0
    while off < len(data):
        r = await c.put(f"/api/v1/files/upload/{uid}", params={"offset": off}, content=data[off:off + chunk])
        assert r.status_code == 200, r.text
        off = r.json()["received"]
    done = await c.post(f"/api/v1/files/upload/{uid}/complete")
    assert done.status_code == 200, done.text
    return done.json()


async def test_chunked_upload_and_resume(env: Env, token: str):
    data = os.urandom(9 * 1024 * 1024 + 123)
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/files/upload", json={"name": "big.bin", "size": len(data)})
        uid = res.json()["id"]
        r = await c.put(f"/api/v1/files/upload/{uid}", params={"offset": 0}, content=data[:4 * 1024 * 1024])
        assert r.json()["received"] == 4 * 1024 * 1024
        # wrong offset: server tells us where to continue
        r = await c.put(f"/api/v1/files/upload/{uid}", params={"offset": 123}, content=b"x")
        assert r.status_code == 409 and r.json()["received"] == 4 * 1024 * 1024
        # "connection dropped": ask for status and resume from there
        status = (await c.get(f"/api/v1/files/upload/{uid}")).json()
        off = status["received"]
        while off < len(data):
            r = await c.put(f"/api/v1/files/upload/{uid}", params={"offset": off}, content=data[off:off + 4 * 1024 * 1024])
            off = r.json()["received"]
        done = (await c.post(f"/api/v1/files/upload/{uid}/complete")).json()
    assert done["sha256"] == hashlib.sha256(data).hexdigest()
    saved = env.inbox / "big.bin"
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == done["sha256"]


async def test_upload_conflict_renames(env: Env, token: str):
    async with env.client(token=token) as c:
        a = await upload(c, b"one", "same.txt", root=env.root_id(), path="")
        b = await upload(c, b"two", "same.txt", root=env.root_id(), path="")
    assert a["name"] == "same.txt" and b["name"] == "same (1).txt"
    assert (env.share / "same (1).txt").read_bytes() == b"two"


async def test_upload_rejects_bad_names_and_oversize(env: Env, token: str):
    async with env.client(token=token) as c:
        for bad in ("..", "a/b.txt", "con.txt", "x:y", "trail."):
            res = await c.post("/api/v1/files/upload", json={"name": bad, "size": 1})
            assert res.status_code == 400, bad
        res = await c.post("/api/v1/files/upload", json={"name": "ok.txt", "size": 3})
        uid = res.json()["id"]
        r = await c.put(f"/api/v1/files/upload/{uid}", params={"offset": 0}, content=b"too long")
        assert r.status_code == 413


async def test_list_download_range_and_zip(env: Env, token: str):
    (env.share / "docs").mkdir()
    payload = bytes(range(256)) * 400
    (env.share / "docs" / "data.bin").write_bytes(payload)
    (env.share / "docs" / "note.html").write_text("<script>alert(1)</script>")
    Image.new("RGB", (800, 600), (10, 200, 120)).save(env.share / "docs" / "pic.jpg")
    async with env.client(token=token) as c:
        listing = (await c.get("/api/v1/files/list", params={"root": env.root_id(), "path": "docs"})).json()
        by = {i["name"]: i for i in listing["items"]}
        assert set(by) == {"data.bin", "note.html", "pic.jpg"}
        url = by["data.bin"]["url"]
        # signed link works without the bearer header (for <video>/<img>)
        async with env.client() as anon:
            full = await anon.get(url)
            assert full.status_code == 200 and full.content == payload
            part = await anon.get(url, headers={"Range": "bytes=1000-1999"})
            assert part.status_code == 206 and part.content == payload[1000:2000]
            html = await anon.get(by["note.html"]["url"])
            assert html.headers["content-type"].startswith("text/plain")
            assert "sandbox" in html.headers["content-security-policy"]
            thumb = await anon.get(by["pic.jpg"]["thumb"])
            assert thumb.status_code == 200 and max(Image.open(io.BytesIO(thumb.content)).size) <= 200
            z = await anon.get(listing["zip"])
            assert z.status_code == 200
            names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
            assert sorted(names) == ["data.bin", "note.html", "pic.jpg"]
            # tampered link is refused
            bad = url.replace(url.split("/")[4][:6], "AAAAAA")
            assert (await anon.get(bad)).status_code == 403


async def test_link_dies_with_device(env: Env, token: str):
    (env.share / "f.txt").write_text("x")
    async with env.client(token=token) as c:
        item = (await c.get("/api/v1/files/list", params={"root": env.root_id()})).json()["items"][0]
        dev = (await c.get("/api/v1/me")).json()["device"]
    env.ctx.devices.revoke(dev["id"])
    async with env.client() as anon:
        assert (await anon.get(item["url"])).status_code == 401


async def test_mkdir_rename_search_delete(env: Env, token: str):
    rid = env.root_id()
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/files/mkdir", json={"root": rid, "path": "", "name": "Inbox"})).status_code == 200
        (env.share / "Inbox" / "report final.txt").write_text("x")
        res = await c.post("/api/v1/files/rename", json={"root": rid, "path": "Inbox/report final.txt", "name": "report.txt"})
        assert res.status_code == 200
        found = (await c.get("/api/v1/files/search", params={"root": rid, "q": "repo"})).json()
        assert [i["path"] for i in found["items"]] == ["Inbox/report.txt"]
        assert (await c.post("/api/v1/files/delete", json={"root": rid, "path": "Inbox/report.txt"})).status_code == 400
        res = await c.post("/api/v1/files/delete", json={"root": rid, "path": "Inbox/report.txt", "confirm": True})
        assert res.status_code == 200
        assert (await c.post("/api/v1/files/delete", json={"root": rid, "path": "", "confirm": True})).status_code == 400


async def test_open_executable_needs_confirm(env: Env, token: str):
    (env.share / "setup.exe").write_bytes(b"MZ")
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/files/open", json={"root": env.root_id(), "path": "setup.exe"})
        assert res.status_code == 400 and res.json()["error"] == "confirm_required"
        res = await c.post("/api/v1/files/open", json={"root": env.root_id(), "path": "setup.exe", "confirm": True})
        assert res.status_code == 200


# --- media -----------------------------------------------------------------------------------

async def test_media_volume_and_keys(env: Env, token: str):
    audio = env.ctx.services["media"]
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/media/volume", json={"value": 63})).json()["volume"] == 63
        assert (await c.post("/api/v1/media/mute", json={"muted": True})).json()["muted"] is True
        assert (await c.post("/api/v1/media/key", json={"key": "next"})).status_code == 200
        assert (await c.post("/api/v1/media/key", json={"key": "launch_missiles"})).status_code == 400
        await c.post("/api/v1/media/device", json={"id": "hp"})
        state = (await c.get("/api/v1/media")).json()
    assert audio.keys == ["next"]
    assert state["volume"] == 63 and [d["id"] for d in state["devices"] if d["default"]] == ["hp"]


# --- input -----------------------------------------------------------------------------------

async def test_input_rest_dry_run(env: Env, token: str):
    backend = env.ctx.services["input"].b
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/input", json={"ev": [
            ["m", 10.6, -3.2], ["m", 0.6, 0.4], ["b", "left", "click"], ["s", 0, -120],
            ["t", "héllo \U0001f600"], ["c", ["ctrl", "c"]], ["k", "f5", "press"],
        ]})
        assert res.json()["applied"] == 7
        blocked = await c.post("/api/v1/input", json={"ev": [["c", ["win", "l"]]]})
        assert blocked.status_code == 403
    assert ("move", 10, -3) in backend.log and ("move", 1, 0) in backend.log  # fractions carry over
    assert ("text", "héllo \U0001f600") in backend.log
    assert backend.log.index(("key", "ctrl", True)) < backend.log.index(("key", "c", True)) \
        < backend.log.index(("key", "ctrl", False))


def test_input_released_when_socket_drops(env: Env, token: str):
    backend = env.ctx.services["input"].b
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53003)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_json()
            ws.send_json({"type": "input", "ev": [["b", "left", "down"], ["k", "shift", "down"]]})
            deadline = time.time() + 3
            while ("key", "shift", True) not in backend.log and time.time() < deadline:
                time.sleep(0.02)
        # the socket is gone: whatever was held must be let go
        deadline = time.time() + 3
        while ("key", "shift", False) not in backend.log and time.time() < deadline:
            time.sleep(0.02)
    assert ("button", "left", False) in backend.log
    assert ("key", "shift", False) in backend.log


def test_unicode_sendinput_structs():
    from agent.modules.inputctl import INPUT

    # 64-bit INPUT is 40 bytes; a wrong size makes SendInput silently do nothing
    assert ctypes_size(INPUT) == (40 if sys.maxsize > 2**32 else 28)


def ctypes_size(t) -> int:
    import ctypes

    return ctypes.sizeof(t)


# --- screen -----------------------------------------------------------------------------------

async def test_screenshot_is_jpeg(env: Env, token: str):
    async with env.client(token=token) as c:
        mons = (await c.get("/api/v1/screen/monitors")).json()["monitors"]
        assert mons
        res = await c.get("/api/v1/screen/shot", params={"monitor": 1, "q": 70, "scale": 0.5})
    assert res.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(res.content))
    assert img.format == "JPEG" and img.width > 100


def test_screen_stream_fps_and_stop(env: Env, token: str):
    svc = env.ctx.services["screen"]
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53004)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_json()
            ws.send_json({"type": "screen.start", "monitor": 1, "fps": 10, "preset": "balanced"})
            frames = 0
            t0 = time.time()
            while time.time() - t0 < 3:
                msg = ws.receive()
                if msg.get("bytes"):
                    data = msg["bytes"]
                    assert data[:4] == b"LDF1" and data[20:22] == b"\xff\xd8"
                    seq = int.from_bytes(data[4:8], "little")
                    ws.send_json({"type": "screen.ack", "seq": seq})
                    frames += 1
            fps = frames / (time.time() - t0)
            assert fps >= 5, fps
            assert len(svc.streams) == 1
        time.sleep(0.5)
        assert svc.streams == {}


# --- processes -------------------------------------------------------------------------------------

async def test_kill_protection(env: Env, token: str):
    import psutil

    lsass = next((p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "lsass.exe"), None)
    killer = env.ctx.services["processes"].killer
    async with env.client(token=token) as c:
        rows = (await c.get("/api/v1/processes")).json()["items"]
        assert any(r["protected"] for r in rows)
        if lsass:
            res = await c.post(f"/api/v1/processes/{lsass}/kill", json={"confirm": True})
            assert res.status_code == 403 and res.json()["error"] in ("protected", "access_denied")
        res = await c.post(f"/api/v1/processes/{os.getpid()}/kill", json={"confirm": True})
        assert res.status_code == 403
        assert (await c.post(f"/api/v1/processes/{os.getpid()}/kill", json={})).status_code == 400
    assert killer.killed == []


async def test_kill_dry_run(env: Env, token: str):
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        async with env.client(token=token) as c:
            res = await c.post(f"/api/v1/processes/{child.pid}/kill", json={"confirm": True, "tree": True})
        assert res.status_code == 200
        assert child.pid in env.ctx.services["processes"].killer.killed
        assert child.poll() is None  # dry run: still alive
    finally:
        child.kill()


async def test_open_url_only_http(env: Env, token: str):
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/apps/open-url", json={"url": "example.com"})).json()["url"] == "https://example.com"
        for bad in ("file:///C:/Windows", "javascript:alert(1)", "ms-settings:privacy"):
            assert (await c.post("/api/v1/apps/open-url", json={"url": bad})).status_code == 400, bad


# --- quick actions -------------------------------------------------------------------------------------

async def test_actions_by_id_only(env: Env, token: str):
    async with env.client(token=token) as c:
        items = (await c.get("/api/v1/actions")).json()["items"]
        assert items and all(set(a) <= {"id", "label", "icon", "color", "type", "confirm"} for a in items)
        res = await c.post("/api/v1/actions/flush-dns/run")
        assert res.status_code == 200 and res.json()["exit_code"] == 0
        assert (await c.post("/api/v1/actions/does-not-exist/run")).status_code == 404
        # sleep is marked confirm
        assert (await c.post("/api/v1/actions/sleep/run", json={})).status_code == 400
        # phones can't redefine actions unless the PC allows it
        res = await c.put("/api/v1/actions", json={"items": [{"id": "x", "label": "x", "type": "run", "exe": "calc.exe"}]})
        assert res.status_code == 403
    async with env.local() as admin:
        res = await admin.put("/admin/api/actions", json={"items": [
            {"id": "evil", "label": "Evil", "type": "run", "exe": "cmd.exe", "args": "/c del *"}]})
        assert res.status_code == 400  # args must be a list


def test_run_never_uses_a_shell():
    out = run_process([sys.executable, "-c", "import sys; print(sys.argv[1])", "x & echo PWNED"], wait=True)
    assert out["exit_code"] == 0
    assert out["output"].strip() == "x & echo PWNED"


# --- notifications ------------------------------------------------------------------------------------

async def test_message_to_pc(env: Env, token: str):
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/notify", json={"title": "", "body": ""})).status_code == 400
        assert (await c.post("/api/v1/notify", json={"title": "Dinner", "body": "Food's ready"})).status_code == 200
        env.ctx.services["notify"].push("alert", "CPU is pegged", "test")
        items = (await c.get("/api/v1/notifications")).json()["items"]
    assert env.ctx.services["toaster"].sent[-1]["title"] == "Dinner"
    assert items[0]["title"] == "CPU is pegged"


# --- terminal ---------------------------------------------------------------------------------------------

async def test_terminal_gated(env: Env, token: str):
    async with env.client(token=token) as c:
        assert (await c.get("/api/v1/terminal")).status_code == 403
        assert (await c.post("/api/v1/terminal/run", json={"command": "whoami", "confirm": True})).status_code == 403
    async with env.local() as admin:
        await admin.put("/admin/api/features", json={"terminal": True})
    async with env.client(token=token) as c:
        res = await c.post("/api/v1/terminal/unlock", json={"pin": "123456"})
        assert res.status_code == 409  # no PIN yet
    async with env.local() as admin:
        assert (await admin.post("/admin/api/pin", json={"pin": "12"})).status_code == 400
        assert (await admin.post("/admin/api/pin", json={"pin": "246810"})).status_code == 200
    stored = env.ctx.config.get("terminal_pin")
    assert "246810" not in str(stored) and stored["salt"]
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/terminal/unlock", json={"pin": "000000"})).status_code == 401
        assert (await c.post("/api/v1/terminal/run", json={"command": "whoami", "confirm": True})).status_code == 401
        session = (await c.post("/api/v1/terminal/unlock", json={"pin": "246810"})).json()["session"]
        h = {"X-Terminal-Session": session}
        assert (await c.post("/api/v1/terminal/run", json={"command": "whoami"}, headers=h)).status_code == 400
        res = await c.post("/api/v1/terminal/run", json={"command": "whoami", "confirm": True}, headers=h)
        assert res.status_code == 200
    log = [e for e in env.ctx.audit.tail() if e["action"] == "terminal.run"]
    assert log and log[0]["target"] == "whoami"


def test_terminal_streams_real_output(env: Env, token: str):
    env.ctx.config.set_feature("terminal", True)
    from agent.security import hash_pin

    env.ctx.config.set("terminal_pin", hash_pin("135790"))
    with TestClient(env.app, base_url="https://192.168.1.10:8765", client=(PHONE_IP, 53005)) as tc:
        with tc.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.receive_json()
            session = tc.post("/api/v1/terminal/unlock", json={"pin": "135790"}, headers=auth(token)).json()["session"]
            env.ctx.dry_run = False  # really run PowerShell for this one
            try:
                res = tc.post("/api/v1/terminal/run", json={"command": "Write-Output 'landeck-ok'; 6*7", "confirm": True},
                              headers={**auth(token), "X-Terminal-Session": session})
                run_id = res.json()["run"]
                out = ""
                code = None
                deadline = time.time() + 20
                while code is None and time.time() < deadline:
                    msg = ws.receive_json()
                    if msg["type"] == "terminal" and msg["data"]["run"] == run_id:
                        if msg["data"]["kind"] == "out":
                            out += msg["data"]["data"]
                        else:
                            code = msg["data"]["code"]
            finally:
                env.ctx.dry_run = True
    assert "landeck-ok" in out and "42" in out
    assert code == 0


# --- RAM cleaner ---------------------------------------------------------------------------

async def test_ram_info_and_clean(env: Env, token: str):
    cleaner = env.ctx.services["ram"]
    async with env.client(token=token) as c:
        info = (await c.get("/api/v1/memory")).json()
        assert info["total"] > 0 and info["can_clean"] is True
        ids = {r["id"] for r in info["regions"]}
        assert {"working_sets", "standby"} <= ids
        res = (await c.post("/api/v1/memory/clean")).json()
        assert "freed" in res and res["before"]["total"] == res["after"]["total"]
        # dry-run runs the default region set without touching real memory
        assert res["regions"] and not res["failed"]
    assert cleaner.calls  # recorded, not executed
    assert any(e["action"] == "ram.clean" for e in env.ctx.audit.tail())


async def test_ram_specific_regions(env: Env, token: str):
    async with env.client(token=token) as c:
        res = (await c.post("/api/v1/memory/clean", json={"regions": ["standby"]})).json()
        assert res["regions"] == ["standby"]
        bad = await c.post("/api/v1/memory/clean", json={"regions": "nope"})
        assert bad.status_code == 400


async def test_ram_feature_gate(env: Env, token: str):
    env.ctx.config.set_feature("ram", False)
    async with env.client(token=token) as c:
        assert (await c.get("/api/v1/memory")).status_code == 403
        assert (await c.post("/api/v1/memory/clean")).status_code == 403


def test_ram_privilege_constant():
    # SeProfileSingleProcessPrivilege is LUID 13 (not 29 = SeImpersonate); the purge calls need it
    from agent.modules import ram
    assert ram.SE_PROFILE_SINGLE_PROCESS_PRIVILEGE == 13


# --- display on/off + wake action ----------------------------------------------------------

async def test_display_on_off(env: Env, token: str):
    backend = env.ctx.services["power"].backend
    async with env.client(token=token) as c:
        assert (await c.post("/api/v1/power/display-off")).status_code == 200
        assert (await c.post("/api/v1/power/display-on")).status_code == 200
    assert ("display_off",) in backend.calls and ("display_on",) in backend.calls


async def test_builtin_actions_wake_and_ram(env: Env, token: str):
    async with env.client(token=token) as c:
        items = {a["id"] for a in (await c.get("/api/v1/actions")).json()["items"]}
        assert {"clean-ram", "wake-display"} <= items
        assert (await c.post("/api/v1/actions/wake-display/run")).json()["exit_code"] == 0
        ram = await c.post("/api/v1/actions/clean-ram/run")
        assert ram.json()["exit_code"] == 0 and "MB" in ram.json()["output"]
    assert ("display_on",) in env.ctx.services["power"].backend.calls


# --- connectivity: pairing prefers landeck.local -------------------------------------------

async def test_pairing_urls_prefer_mdns(env: Env):
    async with env.local() as admin:
        data = (await admin.post("/admin/api/pair")).json()
    assert data["urls"]["host"].startswith("https://landeck.local:")
    assert "landeck.local" in data["urls"]["mdns"]
    assert data["qr_mdns"]


async def test_origin_accepts_any_lan_ip(env: Env):
    # a phone added to the Home Screen on an old IP keeps working after the PC's IP changes
    assert env.ctx.origin_ok("https://192.168.5.23:8765") is True
    assert env.ctx.origin_ok("https://10.1.2.3:8765") is True
    assert env.ctx.origin_ok("https://8.8.8.8:8765") is False
    assert env.ctx.origin_ok("https://evil.example:8765") is False
    assert env.ctx.origin_ok("http://192.168.1.4:8765") is False


async def test_shell_versions_assets(env: Env):
    # a browser must never reuse a stale stylesheet after an update: every css/js link is stamped
    async with env.client() as c:
        res = await c.get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    body = res.text
    # every css/js link carries a version stamp (dev adds an mtime suffix, so match the prefix)
    assert f'href="/css/base.css?v={env.ctx.version}' in body
    assert f'src="/js/app.js?v={env.ctx.version}' in body
    assert "/css/tokens.css?v=" in body


async def test_drives_as_roots(env: Env, token: str):
    env.ctx.config.set("show_drives", True)
    svc = env.ctx.services["files"]
    drives = svc.drives()
    assert drives, "expected at least one drive"
    letters = {d["id"] for d in drives}
    assert "drive-C" in letters
    for d in drives:
        assert d["drive"] is True and d["path"].endswith(":\\") and d["id"].startswith("drive-")
    async with env.client(token=token) as c:
        roots = (await c.get("/api/v1/files/roots")).json()["roots"]
        cdrive = next(r for r in roots if r["id"] == "drive-C")
        assert cdrive["drive"] is True and cdrive["disk"]["total"] > 0
        # can browse the drive root, and traversal out of it is still refused
        listing = await c.get("/api/v1/files/list", params={"root": "drive-C", "path": ""})
        assert listing.status_code == 200
        esc = await c.get("/api/v1/files/list", params={"root": "drive-C", "path": "../.."})
        assert esc.status_code in (400, 403)


async def test_show_drives_toggle(env: Env, token: str):
    env.ctx.config.set("show_drives", False)
    async with env.client(token=token) as c:
        ids = {r["id"] for r in (await c.get("/api/v1/files/roots")).json()["roots"]}
        assert not any(i.startswith("drive-") for i in ids)
    async with env.local() as admin:
        await admin.put("/admin/api/show-drives", json={"enabled": True})
    async with env.client(token=token) as c:
        ids = {r["id"] for r in (await c.get("/api/v1/files/roots")).json()["roots"]}
        assert any(i.startswith("drive-") for i in ids)


# --- video streaming -----------------------------------------------------------------------

import shutil as _shutil  # noqa: E402

HAS_FFMPEG = _shutil.which("ffmpeg") is not None


def test_plan_segments():
    from agent.modules.stream import plan_segments
    kf = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    segs = plan_segments(kf, 10.0)
    assert segs[0][0] == 0.0
    assert abs(sum(d for _, d in segs) - 10.0) < 0.01  # segments cover the whole duration
    assert all(d > 0 for _, d in segs)


def test_direct_play_decision(env: Env):
    svc = env.ctx.services["stream"]
    assert svc.direct_play({"video": {"codec": "hevc", "dolby_vision": False, "transfer": "smpte2084"}})[0] is True
    assert svc.direct_play({"video": {"codec": "h264", "dolby_vision": False, "transfer": ""}})[0] is True
    assert svc.direct_play({"video": {"codec": "av1", "dolby_vision": False, "transfer": ""}})[0] is False


async def test_stream_feature_gate(env: Env, token: str):
    env.ctx.config.set_feature("stream", False)
    async with env.client(token=token) as c:
        assert (await c.get(f"/api/v1/stream/info?root={env.root_id()}&path=x.mp4")).status_code == 403


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_stream_end_to_end(env: Env, token: str):
    import subprocess as sp
    clip = env.share / "clip.mp4"
    sp.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2",
            "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-g", "24", "-keyint_min", "24",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
    rid = env.root_id()
    async with env.client(token=token) as c:
        info = (await c.get(f"/api/v1/stream/info?root={rid}&path=clip.mp4")).json()
        assert info["available"] and info["video"]["codec"] == "h264" and info["direct_play"] is True
        op = (await c.post("/api/v1/stream/open", json={"root": rid, "path": "clip.mp4"})).json()
        sid, m3u8 = op["session"], op["m3u8"]
        assert op["segments"] >= 1 and op["transcode"] is False
        k = m3u8.split("k=")[1]
        pl = await c.get(f"/api/v1/stream/{sid}/index.m3u8?k={k}")
        assert pl.status_code == 200 and f"init.mp4?k={k}" in pl.text and f"s0.m4s?k={k}" in pl.text
        # wrong token refused
        assert (await c.get(f"/api/v1/stream/{sid}/index.m3u8?k=wrong")).status_code == 403
        init = await c.get(f"/api/v1/stream/{sid}/init.mp4?k={k}")
        assert init.status_code == 200 and init.content[:4] and len(init.content) > 100
        seg = await c.get(f"/api/v1/stream/{sid}/s0.m4s?k={k}")
        assert seg.status_code == 200 and len(seg.content) > 1000
        assert (await c.delete(f"/api/v1/stream/{sid}")).status_code == 200
        # session gone after close
        assert (await c.get(f"/api/v1/stream/{sid}/index.m3u8?k={k}")).status_code == 404
