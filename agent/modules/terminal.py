"""PowerShell runner. Off by default, needs a PIN per session, every command is audited."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, feature_dep, require_confirm
from ..config import CREATE_NO_WINDOW
from ..security import check_pin

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.terminal")

IDLE_TIMEOUT = 30 * 60
CWD_MARK = "__LANDECK_CWD__="


class Session:
    def __init__(self, device_id: str):
        self.token = secrets.token_urlsafe(24)
        self.device_id = device_id
        self.cwd = os.path.expanduser("~")
        self.touched = time.monotonic()
        self.history: list[str] = []
        self.running: dict[str, subprocess.Popen] = {}

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.touched > IDLE_TIMEOUT


def wrap(command: str, cwd: str) -> str:
    safe_cwd = cwd.replace("'", "''")
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
        "$OutputEncoding = [Text.Encoding]::UTF8\n"
        f"Set-Location -LiteralPath '{safe_cwd}' -ErrorAction SilentlyContinue\n"
        f"{command}\n"
        f"Write-Output \"`n{CWD_MARK}$((Get-Location).ProviderPath)\"\n"
    )


class TerminalService:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def unlock(self, device: dict[str, Any], pin: str, ip: str) -> Session:
        stored = self.ctx.config.get("terminal_pin")
        if not stored:
            raise ApiError(409, "no_pin", "Set a terminal PIN in the admin page on the PC first.")
        left = self.ctx.pin_limiter.blocked(ip)
        if left:
            raise ApiError(429, "rate_limited", f"Too many wrong PINs. Try again in {int(left) + 1} s.")
        if not check_pin(pin, stored):
            self.ctx.pin_limiter.fail(ip)
            self.ctx.audit("terminal.unlock", result="bad_pin", device=device, ip=ip)
            raise ApiError(401, "bad_pin", "Wrong PIN.")
        self.ctx.pin_limiter.reset(ip)
        s = Session(device["id"])
        with self._lock:
            self.sessions = {k: v for k, v in self.sessions.items() if not v.expired}
            self.sessions[s.token] = s
        self.ctx.audit("terminal.unlock", device=device, ip=ip)
        return s

    def session(self, request: Request, device: dict[str, Any]) -> Session:
        token = request.headers.get("x-terminal-session", "")
        s = self.sessions.get(token)
        if not s or s.device_id != device["id"]:
            raise ApiError(401, "locked", "Terminal is locked. Enter the PIN.")
        if s.expired:
            self.sessions.pop(token, None)
            raise ApiError(401, "locked", "Terminal locked after 30 minutes idle.")
        s.touched = time.monotonic()
        return s

    def lock(self, token: str) -> None:
        s = self.sessions.pop(token, None)
        if s:
            for run_id in list(s.running):
                self.cancel(s, run_id)

    def start(self, s: Session, command: str) -> str:
        run_id = uuid.uuid4().hex[:10]
        script = wrap(command, s.cwd)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded]
        if self.ctx.dry_run:
            self._emit(s, run_id, "out", f"(dry run) {command}\n")
            self._emit(s, run_id, "exit", "", code=0)
            return run_id
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW, cwd=s.cwd
                                if os.path.isdir(s.cwd) else None)
        s.running[run_id] = proc
        threading.Thread(target=self._pump, args=(s, run_id, proc), daemon=True).start()
        return run_id

    def _emit(self, s: Session, run_id: str, kind: str, data: str, **extra: Any) -> None:
        self.ctx.hub.publish("terminal", {"run": run_id, "kind": kind, "data": data, "cwd": s.cwd, **extra},
                             device_id=s.device_id)

    def _pump(self, s: Session, run_id: str, proc: subprocess.Popen) -> None:
        pending: list[str] = []
        last_flush = time.monotonic()
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            if line.startswith(CWD_MARK):
                s.cwd = line[len(CWD_MARK):].strip() or s.cwd
                continue
            pending.append(line)
            if len(pending) > 50 or time.monotonic() - last_flush > 0.1:
                self._emit(s, run_id, "out", "".join(pending))
                pending.clear()
                last_flush = time.monotonic()
        if pending:
            text = "".join(pending)
            if text.endswith("\r\n\r\n") or text.endswith("\n\n"):
                text = text[:-1]
            self._emit(s, run_id, "out", text)
        code = proc.wait()
        s.running.pop(run_id, None)
        self._emit(s, run_id, "exit", "", code=code)

    def cancel(self, s: Session, run_id: str) -> bool:
        proc = s.running.get(run_id)
        if not proc:
            return False
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.Error:
            pass
        return True


def build(ctx: "Context") -> APIRouter:
    svc = TerminalService(ctx)
    ctx.services["terminal"] = svc
    gate = feature_dep(ctx, "terminal")
    r = APIRouter()

    @r.get("/terminal")
    async def status(request: Request, dev: dict = Depends(gate)):
        token = request.headers.get("x-terminal-session", "")
        s = svc.sessions.get(token)
        unlocked = bool(s and s.device_id == dev["id"] and not s.expired)
        return {"unlocked": unlocked, "pin_set": bool(ctx.config.get("terminal_pin")),
                "cwd": s.cwd if unlocked and s else None, "history": s.history[-50:] if unlocked and s else []}

    @r.post("/terminal/unlock")
    async def unlock(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        s = await asyncio.to_thread(svc.unlock, dev, str(body.get("pin", "")), client_ip(request))
        return {"session": s.token, "cwd": s.cwd, "idle_timeout": IDLE_TIMEOUT}

    @r.post("/terminal/lock")
    async def lock(request: Request, dev: dict = Depends(gate)):
        svc.lock(request.headers.get("x-terminal-session", ""))
        return {"ok": True}

    @r.post("/terminal/run")
    async def run(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        s = svc.session(request, dev)
        require_confirm(body)
        command = str(body.get("command", "")).strip()
        if not command:
            raise ApiError(400, "empty", "Type a command.")
        if len(command) > 8000:
            raise ApiError(413, "too_long", "Command is too long.")
        run_id = svc.start(s, command)
        s.history.append(command)
        ctx.audit("terminal.run", command, device=dev, ip=client_ip(request), run=run_id, cwd=s.cwd)
        return {"run": run_id, "cwd": s.cwd}

    @r.post("/terminal/cancel")
    async def cancel(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        s = svc.session(request, dev)
        ok = svc.cancel(s, str(body.get("run", "")))
        ctx.audit("terminal.cancel", body.get("run", ""), device=dev, ip=client_ip(request))
        return {"ok": ok}

    return r
