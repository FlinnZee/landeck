"""Localhost-only admin API backing web/admin.html."""
from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import subprocess
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response

from . import startup
from .api import ApiError, local_dep
from .config import CREATE_NO_WINDOW, FEATURES
from .native import is_elevated
from .security import hash_pin

if TYPE_CHECKING:
    from .context import Context


def notify_admin_changed(ctx: "Context") -> None:
    ctx.hub.publish("admin", {"changed": time.time()})


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def pair_urls(ctx: "Context", secret: str) -> dict[str, str]:
    host = ctx.ips[0] if ctx.ips else "landeck.local"
    return {
        "ip": f"https://{host}:{ctx.port}/pair#{secret}",
        "mdns": f"https://landeck.local:{ctx.port}/pair#{secret}",
        "host": f"https://{host}:{ctx.port}",
    }


def build(ctx: "Context") -> APIRouter:
    r = APIRouter(prefix="/admin/api", dependencies=[Depends(local_dep(ctx))])

    @r.get("/overview")
    async def overview():
        online = ctx.hub.online_devices()
        devices = []
        for d in ctx.devices.all():
            devices.append({**d, "online": online.get(d["id"], 0)})
        try:
            fp = ctx.certs.fingerprint()
            ca_fp = ctx.certs.ca_fingerprint()
        except OSError:
            fp = ca_fp = None
        pairing = ctx.pairing.active()
        return {
            "server": ctx.server_info(),
            "elevated": is_elevated(),
            "devices": devices,
            "features": ctx.config.features(),
            "feature_names": list(FEATURES),
            "settings": {k: ctx.config.get(k) for k in ("name", "upload_dir", "clipboard_persist", "screen_toast",
                                                         "autostart", "port")},
            "upload_dir_effective": str(ctx.services["files"].upload_dir()),
            "roots": ctx.services["files"].roots(),
            "pin_set": bool(ctx.config.get("terminal_pin")),
            "cert": {"fingerprint": fp, "ca_fingerprint": ca_fp, **ctx.certs.sans()},
            "pairing": {"active": bool(pairing), "expires": pairing["expires"] if pairing else None},
            "viewing": bool(ctx.services["screen"].streams),
            "data_dir": str(ctx.home),
            "dry_run": ctx.dry_run,
        }

    @r.get("/startup")
    async def startup_status():
        state = await asyncio.to_thread(startup.task_state)
        profiles = await asyncio.to_thread(startup.network_profiles)
        return {"task": state, "autostart": ctx.config.get("autostart"), "networks": profiles}

    @r.post("/startup")
    async def set_startup(body: dict[str, Any] = Body(...)):
        enabled = bool(body.get("enabled"))
        if not is_elevated():
            raise ApiError(403, "not_elevated", "Run LanDeck as administrator to change startup.")
        try:
            if enabled:
                await asyncio.to_thread(startup.install_task)
                await asyncio.to_thread(startup.install_firewall, ctx.port)
            else:
                await asyncio.to_thread(startup.remove_task)
        except RuntimeError as exc:
            raise ApiError(500, "schtasks_failed", str(exc))
        ctx.config.set("autostart", enabled)
        ctx.audit("admin.startup", "on" if enabled else "off", ip="127.0.0.1")
        return {"task": await asyncio.to_thread(startup.task_state), "autostart": enabled}

    @r.post("/pair")
    async def start_pair():
        s = ctx.pairing.start()
        urls = pair_urls(ctx, s["secret"])
        ctx.audit("admin.pair_start", ip="127.0.0.1")
        return {"code": s["code"], "expires": s["expires"], "ttl": ctx.pairing.TTL, "urls": urls,
                "qr": qr_svg(urls["ip"]), "qr_mdns": qr_svg(urls["mdns"])}

    @r.delete("/pair")
    async def cancel_pair():
        ctx.pairing.cancel()
        return {"ok": True}

    @r.delete("/devices/{device_id}")
    async def revoke(device_id: str):
        dev = ctx.devices.get(device_id)
        if not ctx.devices.revoke(device_id):
            raise ApiError(404, "no_device", "Unknown device.")
        await ctx.hub.drop_device(device_id)
        ctx.audit("admin.revoke", dev["name"] if dev else device_id, ip="127.0.0.1")
        return {"ok": True}

    @r.patch("/devices/{device_id}")
    async def rename(device_id: str, body: dict[str, Any] = Body(...)):
        if not ctx.devices.rename(device_id, str(body.get("name", ""))):
            raise ApiError(404, "no_device", "Unknown device.")
        return {"ok": True}

    @r.put("/features")
    async def features(body: dict[str, Any] = Body(...)):
        for name, enabled in body.items():
            if name not in FEATURES:
                raise ApiError(400, "unknown_feature", name)
            ctx.config.set_feature(name, bool(enabled))
            ctx.audit("admin.feature", f"{name}={'on' if enabled else 'off'}", ip="127.0.0.1")
        ctx.hub.publish("features", ctx.config.features())
        if not ctx.config.feature("screen"):
            for st in list(ctx.services["screen"].streams.values()):
                await ctx.services["screen"].stop(st.conn)
        return {"features": ctx.config.features()}

    @r.put("/settings")
    async def settings(body: dict[str, Any] = Body(...)):
        allowed = {"name": str, "upload_dir": str, "clipboard_persist": bool, "screen_toast": bool, "port": int}
        for key, value in body.items():
            if key not in allowed:
                raise ApiError(400, "unknown_setting", key)
            value = allowed[key](value)
            if key == "upload_dir" and value and not os.path.isabs(value):
                raise ApiError(400, "bad_path", "Use a full path like D:\\Inbox.")
            if key == "port" and not 1024 <= value <= 65535:
                raise ApiError(400, "bad_port", "Pick a port between 1024 and 65535.")
            ctx.config.set(key, value)
            ctx.audit("admin.setting", f"{key}", ip="127.0.0.1")
        return {"ok": True, "restart_needed": "port" in body}

    @r.post("/roots")
    async def add_root(body: dict[str, Any] = Body(...)):
        path = str(body.get("path", "")).strip().strip('"')
        if not path or not os.path.isdir(path):
            raise ApiError(400, "bad_path", "That folder doesn't exist.")
        path = os.path.realpath(path)
        roots = ctx.config.get("roots", []) or []
        if any(os.path.normcase(r["path"]) == os.path.normcase(path) for r in roots):
            raise ApiError(409, "exists", "Already shared.")
        roots.append({"name": str(body.get("name") or os.path.basename(path) or path), "path": path})
        ctx.config.set("roots", roots)
        ctx.audit("admin.root_add", path, ip="127.0.0.1")
        return {"roots": ctx.services["files"].roots()}

    @r.delete("/roots")
    async def remove_root(body: dict[str, Any] = Body(...)):
        path = os.path.normcase(str(body.get("path", "")))
        roots = [x for x in (ctx.config.get("roots", []) or []) if os.path.normcase(x["path"]) != path]
        ctx.config.set("roots", roots)
        ctx.audit("admin.root_remove", body.get("path", ""), ip="127.0.0.1")
        return {"roots": ctx.services["files"].roots()}

    @r.put("/default-roots")
    async def default_roots(body: dict[str, Any] = Body(...)):
        ctx.config.set("default_roots", bool(body.get("enabled", True)))
        return {"roots": ctx.services["files"].roots()}

    @r.get("/actions")
    async def get_actions():
        store = ctx.services["actions"]
        return {"items": store.load(), "scripts": sorted(p.name for p in store.scripts.glob("*.ps1")),
                "scripts_dir": str(store.scripts)}

    @r.put("/actions")
    async def put_actions(body: dict[str, Any] = Body(...)):
        items = ctx.services["actions"].save(body.get("items", []))
        ctx.audit("admin.actions", f"{len(items)} actions", ip="127.0.0.1")
        ctx.hub.publish("actions", {"changed": True})
        return {"items": items}

    @r.post("/pin")
    async def set_pin(body: dict[str, Any] = Body(...)):
        pin = str(body.get("pin", ""))
        if len(pin) < 6 or not pin.isdigit():
            raise ApiError(400, "weak_pin", "Use at least 6 digits.")
        ctx.config.set("terminal_pin", await asyncio.to_thread(hash_pin, pin))
        ctx.services["terminal"].sessions.clear()
        ctx.audit("admin.pin_set", ip="127.0.0.1")
        return {"ok": True}

    @r.delete("/pin")
    async def clear_pin():
        ctx.config.set("terminal_pin", None)
        ctx.services["terminal"].sessions.clear()
        ctx.audit("admin.pin_clear", ip="127.0.0.1")
        return {"ok": True}

    @r.get("/audit")
    async def audit(limit: int = 500, q: str = "", action: str = ""):
        rows = await asyncio.to_thread(ctx.audit.tail, min(max(limit, 1), 5000))
        if action:
            rows = [x for x in rows if str(x.get("action", "")).startswith(action)]
        if q:
            ql = q.lower()
            rows = [x for x in rows if ql in json.dumps(x, ensure_ascii=False).lower()]
        return {"items": rows}

    @r.post("/restart")
    async def restart():
        if not ctx.request_restart:
            raise ApiError(409, "no_restart", "Restart isn't available in this mode.")
        ctx.audit("admin.restart", ip="127.0.0.1")
        asyncio.get_running_loop().call_later(0.5, ctx.request_restart)
        return {"ok": True}

    @r.post("/trust-cert")
    async def trust_cert():
        if not is_elevated():
            raise ApiError(403, "not_elevated", "Needs administrator rights.")
        res = await asyncio.to_thread(subprocess.run, ["certutil", "-addstore", "Root", str(ctx.certs.ca_cert_path)],
                                      capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0:
            raise ApiError(500, "certutil_failed", (res.stdout or res.stderr).strip()[-300:])
        ctx.audit("admin.trust_cert", ip="127.0.0.1")
        return {"ok": True}

    @r.get("/diagnostics")
    async def diagnostics():
        data = await asyncio.to_thread(_diagnostics_zip, ctx)
        name = f"landeck-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(data, media_type="application/zip",
                        headers={"content-disposition": f'attachment; filename="{name}"'})

    return r


def _diagnostics_zip(ctx: "Context") -> bytes:
    cfg = json.loads(json.dumps(ctx.config.data))
    if cfg.get("terminal_pin"):
        cfg["terminal_pin"] = "(set)"
    info = {
        "version": ctx.version,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "elevated": is_elevated(),
        "ips": ctx.ips,
        "port": ctx.port,
        "certs": ctx.certs.sans(),
        "task": startup.task_state(),
        "uptime": time.time() - ctx.started,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info.json", json.dumps(info, indent=2))
        zf.writestr("config.json", json.dumps(cfg, indent=2))
        zf.writestr("devices.json", json.dumps(ctx.devices.all(), indent=2))  # hashes are stripped
        zf.writestr("audit.jsonl", "\n".join(json.dumps(x) for x in reversed(ctx.audit.tail(2000))))
        log = Path(ctx.home) / "landeck.log"
        if log.exists():
            zf.writestr("landeck.log", log.read_text("utf-8", errors="replace")[-500_000:])
    return buf.getvalue()
