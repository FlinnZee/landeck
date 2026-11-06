"""Quick actions: buttons defined on the PC in actions.json that the phone can only trigger by id."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, feature_dep, require_confirm
from ..config import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.actions")

TYPES = {"run", "open_url", "open_path", "keys", "power", "script", "builtin"}
BUILTINS = {"volume", "mute_toggle", "screenshot", "display_off", "display_on", "clean_ram"}
POWER = {"lock", "sleep", "hibernate", "shutdown", "restart", "signout"}
COLORS = {"cyan", "violet", "green", "rose", "amber", "slate"}
OUTPUT_LIMIT = 2048

DEFAULT_ACTIONS: list[dict[str, Any]] = [
    {"id": "lock", "label": "Lock PC", "icon": "lock", "color": "violet", "type": "power", "action": "lock"},
    {"id": "display-off", "label": "Display off", "icon": "monitor-off", "color": "slate",
     "type": "builtin", "name": "display_off"},
    {"id": "mute", "label": "Mute toggle", "icon": "volume-x", "color": "rose", "type": "builtin",
     "name": "mute_toggle"},
    {"id": "volume-30", "label": "Volume 30%", "icon": "volume-1", "color": "cyan", "type": "builtin",
     "name": "volume", "value": 30},
    {"id": "play-pause", "label": "Play / Pause", "icon": "play", "color": "green", "type": "keys",
     "keys": ["media_play_pause"]},
    {"id": "show-desktop", "label": "Show desktop", "icon": "layout", "color": "slate", "type": "keys",
     "keys": ["win", "d"]},
    {"id": "task-manager", "label": "Task Manager", "icon": "activity", "color": "amber", "type": "run",
     "exe": "taskmgr.exe", "args": []},
    {"id": "clean-ram", "label": "Clean RAM", "icon": "zap", "color": "green", "type": "builtin",
     "name": "clean_ram"},
    {"id": "wake-display", "label": "Wake display", "icon": "sun", "color": "amber", "type": "builtin",
     "name": "display_on"},
    {"id": "screenshot", "label": "Screenshot to Downloads", "icon": "camera", "color": "cyan",
     "type": "builtin", "name": "screenshot"},
    {"id": "downloads", "label": "Open Downloads", "icon": "download", "color": "green", "type": "open_path",
     "path": "%USERPROFILE%\\Downloads"},
    {"id": "flush-dns", "label": "Flush DNS", "icon": "refresh", "color": "violet", "type": "run",
     "exe": "ipconfig.exe", "args": ["/flushdns"], "wait": True},
    {"id": "night-light", "label": "Night light settings", "icon": "moon", "color": "amber",
     "type": "open_url", "url": "ms-settings:nightlight"},
    {"id": "sleep", "label": "Sleep", "icon": "moon", "color": "slate", "type": "power", "action": "sleep",
     "confirm": True},
]

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def validate(action: dict[str, Any]) -> dict[str, Any]:
    a = dict(action)
    if not _ID.match(str(a.get("id", ""))):
        raise ApiError(400, "bad_id", "Action ids are lowercase letters, digits, - and _.")
    if not str(a.get("label", "")).strip():
        raise ApiError(400, "bad_label", "Every action needs a label.")
    t = a.get("type")
    if t not in TYPES:
        raise ApiError(400, "bad_type", f"type must be one of {', '.join(sorted(TYPES))}.")
    if t == "run":
        if not isinstance(a.get("exe"), str) or not a["exe"].strip():
            raise ApiError(400, "bad_run", "run actions need an 'exe'.")
        args = a.get("args", [])
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise ApiError(400, "bad_run", "'args' must be a list of strings.")
    elif t == "open_url" and not isinstance(a.get("url"), str):
        raise ApiError(400, "bad_url", "open_url actions need a 'url'.")
    elif t == "open_path" and not isinstance(a.get("path"), str):
        raise ApiError(400, "bad_path", "open_path actions need a 'path'.")
    elif t == "keys":
        from .inputctl import check_combo

        if not isinstance(a.get("keys"), list) or not a["keys"]:
            raise ApiError(400, "bad_keys", "keys actions need a 'keys' list.")
        check_combo(a["keys"])
    elif t == "power" and a.get("action") not in POWER:
        raise ApiError(400, "bad_power", f"power action must be one of {', '.join(sorted(POWER))}.")
    elif t == "script":
        name = str(a.get("file", ""))
        if not name.lower().endswith(".ps1") or Path(name).name != name:
            raise ApiError(400, "bad_script", "script must be a .ps1 file name inside the scripts folder.")
    elif t == "builtin" and a.get("name") not in BUILTINS:
        raise ApiError(400, "bad_builtin", f"builtin name must be one of {', '.join(sorted(BUILTINS))}.")
    if a.get("color") not in COLORS:
        a["color"] = "cyan"
    a["label"] = str(a["label"]).strip()[:40]
    if t == "power" and a["action"] in {"shutdown", "restart", "signout", "hibernate"}:
        a["confirm"] = True
    return a


class ActionStore:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.path = ctx.home / "actions.json"
        self.scripts = ctx.home / "scripts"
        self.scripts.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self.last: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            self.save(DEFAULT_ACTIONS)

    def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return [a for a in data if isinstance(a, dict)]
        except (OSError, ValueError):
            log.warning("actions.json unreadable, using defaults")
            return [dict(a) for a in DEFAULT_ACTIONS]

    def save(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean = [validate(a) for a in actions]
        ids = [a["id"] for a in clean]
        if len(ids) != len(set(ids)):
            raise ApiError(400, "duplicate_id", "Two actions share the same id.")
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2), "utf-8")
            os.replace(tmp, self.path)
        return clean

    def get(self, action_id: str) -> dict[str, Any]:
        for a in self.load():
            if a.get("id") == action_id:
                return validate(a)
        raise ApiError(404, "no_action", "That action doesn't exist on the PC.")

    @staticmethod
    def public(a: dict[str, Any]) -> dict[str, Any]:
        # the phone only needs what it renders; commands stay on the PC
        return {k: a.get(k) for k in ("id", "label", "icon", "color", "type", "confirm") if k in a}


def run_process(argv: list[str], wait: bool, timeout: int = 30, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"exit_code": 0, "output": "(dry run) " + " ".join(argv), "duration": 0.0}
    started = time.monotonic()
    if not wait:
        subprocess.Popen(argv, shell=False, creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"exit_code": None, "output": "Started.", "duration": 0.0}
    try:
        res = subprocess.run(argv, shell=False, capture_output=True, timeout=timeout,
                             creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        return {"exit_code": None, "output": _decode(out)[:OUTPUT_LIMIT] + "\n[timed out]",
                "duration": round(time.monotonic() - started, 2)}
    out = _decode(res.stdout + (b"\n" + res.stderr if res.stderr else b""))
    return {"exit_code": res.returncode, "output": out.strip()[:OUTPUT_LIMIT],
            "duration": round(time.monotonic() - started, 2)}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs", "cp437"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class ActionRunner:
    def __init__(self, ctx: "Context", store: ActionStore):
        self.ctx = ctx
        self.store = store

    async def run(self, a: dict[str, Any]) -> dict[str, Any]:
        t = a["type"]
        ctx = self.ctx
        if t == "run":
            argv = [os.path.expandvars(a["exe"])] + [os.path.expandvars(x) for x in a.get("args", [])]
            return await asyncio.to_thread(run_process, argv, bool(a.get("wait", True)),
                                           int(a.get("timeout", 30)), ctx.dry_run)
        if t == "script":
            script = self.store.scripts / a["file"]
            if not script.is_file():
                raise ApiError(404, "no_script", f"{a['file']} isn't in the scripts folder.")
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(script)]
            return await asyncio.to_thread(run_process, argv, True, int(a.get("timeout", 60)), ctx.dry_run)
        if t in ("open_url", "open_path"):
            target = os.path.expandvars(a["url"] if t == "open_url" else a["path"])
            if not ctx.dry_run:
                await asyncio.to_thread(os.startfile, target)  # type: ignore[attr-defined]
            return {"exit_code": 0, "output": f"Opened {target}"}
        if t == "keys":
            svc = ctx.services["input"]
            await asyncio.to_thread(svc.combo, a["keys"])
            return {"exit_code": 0, "output": "Sent " + "+".join(a["keys"])}
        if t == "power":
            await ctx.services["power"].now(a["action"])
            return {"exit_code": 0, "output": a["action"].capitalize()}
        if t == "builtin":
            return await self._builtin(a)
        raise ApiError(400, "bad_type", t)

    async def _builtin(self, a: dict[str, Any]) -> dict[str, Any]:
        ctx = self.ctx
        name = a["name"]
        if name == "volume":
            audio = ctx.services["media"]
            value = int(a.get("value", 30))
            await audio.call(audio.set_volume, value)
            return {"exit_code": 0, "output": f"Volume {value}%"}
        if name == "mute_toggle":
            audio = ctx.services["media"]
            state = await audio.call(audio.master)
            await audio.call(audio.set_mute, not state["muted"])
            return {"exit_code": 0, "output": "Unmuted" if state["muted"] else "Muted"}
        if name == "display_off":
            await asyncio.to_thread(ctx.services["power"].backend.display_off)
            return {"exit_code": 0, "output": "Display off"}
        if name == "display_on":
            await asyncio.to_thread(ctx.services["power"].backend.display_on)
            return {"exit_code": 0, "output": "Display on"}
        if name == "clean_ram":
            cleaner = ctx.services.get("ram")
            if not cleaner:
                raise ApiError(409, "ram_off", "The RAM cleaner is turned off on the PC.")
            res = await asyncio.to_thread(cleaner.clean, None)
            return {"exit_code": 0, "output": f"Freed {res['freed'] // (1024 * 1024)} MB"}
        if name == "screenshot":
            from .screen import save_screenshot

            path = await save_screenshot(ctx, int(a.get("monitor", 1)))
            return {"exit_code": 0, "output": f"Saved {path}"}
        raise ApiError(400, "bad_builtin", name)


def build(ctx: "Context") -> APIRouter:
    store = ActionStore(ctx)
    runner = ActionRunner(ctx, store)
    ctx.services["actions"] = store
    gate = feature_dep(ctx, "actions")
    r = APIRouter()

    def editing_allowed() -> None:
        if not ctx.config.feature("remote_action_editing"):
            raise ApiError(403, "editing_disabled", "Actions can only be edited on the PC (admin page).")

    @r.get("/actions")
    async def list_actions(dev: dict = Depends(gate)):
        editable = ctx.config.feature("remote_action_editing")
        items = [a if editable else store.public(a) for a in store.load()]
        return {"items": items, "editable": editable, "last": store.last}

    @r.post("/actions/{action_id}/run")
    async def run(action_id: str, request: Request, body: dict[str, Any] | None = Body(None),
                  dev: dict = Depends(gate)):
        a = store.get(action_id)
        if a.get("confirm"):
            require_confirm(body)
        try:
            result = await runner.run(a)
            status = "ok" if result.get("exit_code") in (0, None) else f"exit {result.get('exit_code')}"
        except ApiError as exc:
            result = {"exit_code": None, "output": exc.detail, "error": exc.error}
            status = exc.error
        except Exception as exc:
            log.exception("action %s failed", action_id)
            result = {"exit_code": None, "output": str(exc)[:OUTPUT_LIMIT], "error": "failed"}
            status = "failed"
        result.update(id=action_id, label=a["label"], ts=time.time())
        store.last[action_id] = result
        ctx.audit("action.run", f"{action_id} ({a['type']})", result=status, device=dev, ip=client_ip(request))
        return result

    @r.put("/actions")
    async def replace(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        editing_allowed()
        items = store.save(body.get("items", []))
        ctx.audit("action.edit", f"{len(items)} actions", device=dev, ip=client_ip(request))
        ctx.hub.publish("actions", {"changed": True})
        return {"items": items}

    return r
