"""Process list with kill protection, Start Menu app launcher and open-URL."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import psutil
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from ..api import ApiError, client_ip, feature_dep, require_confirm

if TYPE_CHECKING:
    from ..context import Context
    from ..hub import Conn

log = logging.getLogger("landeck.processes")

PROTECTED = {"system", "system idle process", "idle", "registry", "memory compression", "secure system",
             "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "smss.exe",
             "lsaiso.exe", "fontdrvhost.exe"}


def own_pids() -> set[int]:
    """This process and, for PyInstaller onefile builds, its bootloader parent."""
    me = psutil.Process()
    pids = {me.pid}
    try:
        parent = me.parent()
        if parent and parent.name().lower() == me.name().lower():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


class ProcessKiller:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.killed: list[int] = []

    def kill(self, procs: list[psutil.Process]) -> None:
        if self.dry_run:
            self.killed += [p.pid for p in procs]
            return
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=3)


class ProcessService:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.killer = ProcessKiller(ctx.dry_run)
        self._cache: dict[int, psutil.Process] = {}
        self._lock = threading.Lock()
        self.cores = psutil.cpu_count() or 1
        self.mine = own_pids()

    def snapshot(self) -> list[dict[str, Any]]:
        rows = []
        alive = set()
        with self._lock:
            for p in psutil.process_iter(["pid", "name", "username", "memory_info", "create_time", "ppid"]):
                info = p.info
                pid = info["pid"]
                alive.add(pid)
                cached = self._cache.get(pid)
                if cached is None or cached.create_time() != info.get("create_time"):
                    self._cache[pid] = p
                    cached = p
                    try:
                        p.cpu_percent(None)
                    except psutil.Error:
                        pass
                    cpu = 0.0
                else:
                    try:
                        cpu = cached.cpu_percent(None) / self.cores
                    except psutil.Error:
                        cpu = 0.0
                name = info.get("name") or ""
                user = (info.get("username") or "").split("\\")[-1]
                rows.append({
                    "pid": pid, "ppid": info.get("ppid"), "name": name, "user": user,
                    "cpu": round(cpu, 1),
                    "mem": info["memory_info"].rss if info.get("memory_info") else 0,
                    "protected": self.is_protected(pid, name),
                })
            for pid in list(self._cache):
                if pid not in alive:
                    del self._cache[pid]
        return rows

    def is_protected(self, pid: int, name: str) -> bool:
        return pid in (0, 4) or pid in self.mine or name.lower() in PROTECTED

    def kill(self, pid: int, tree: bool) -> list[dict[str, Any]]:
        try:
            proc = psutil.Process(pid)
            targets = [proc]
            if tree:
                targets = proc.children(recursive=True) + [proc]
            names = [(p.pid, p.name()) for p in targets]
        except psutil.NoSuchProcess:
            raise ApiError(404, "no_process", "That process already exited.")
        except psutil.AccessDenied:
            raise ApiError(403, "access_denied", "Windows denied access to that process.")
        for tpid, tname in names:
            if self.is_protected(tpid, tname):
                raise ApiError(403, "protected", f"{tname} is protected; killing it would crash Windows or LanDeck.")
        try:
            self.killer.kill(targets)
        except psutil.AccessDenied:
            raise ApiError(403, "access_denied", "Windows denied access to that process.")
        return [{"pid": p, "name": n} for p, n in names]


class AppIndex:
    """Start Menu shortcuts from both the per-user and all-users folders."""

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.apps: dict[str, dict[str, Any]] = {}
        self.scanned_at = 0.0
        self.icon_dir = ctx.home / "icons"
        self.icon_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def folders() -> list[Path]:
        out = []
        for env in ("APPDATA", "PROGRAMDATA"):
            base = os.environ.get(env)
            if base:
                out.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        return out

    def scan(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if self.apps and not force and time.time() - self.scanned_at < 300:
                return list(self.apps.values())
            found: dict[str, dict[str, Any]] = {}
            skip = ("uninstall", "readme", "help", "documentation", "release notes", "website", "license")
            for folder in self.folders():
                if not folder.exists():
                    continue
                for lnk in folder.rglob("*"):
                    if lnk.suffix.lower() not in (".lnk", ".url", ".appref-ms"):
                        continue
                    name = lnk.stem
                    if any(s in name.lower() for s in skip):
                        continue
                    key = name.lower()
                    if key in found:
                        continue
                    app_id = hashlib.sha1(str(lnk).lower().encode()).hexdigest()[:12]
                    group = lnk.parent.name if lnk.parent != folder else ""
                    found[key] = {"id": app_id, "name": name, "group": group, "path": str(lnk)}
            self.apps = {a["id"]: a for a in sorted(found.values(), key=lambda a: a["name"].lower())}
            self.scanned_at = time.time()
            return list(self.apps.values())

    def icon(self, app_id: str) -> bytes | None:
        app = self.apps.get(app_id)
        if not app:
            return None
        cached = self.icon_dir / f"{app_id}.png"
        if cached.exists():
            return cached.read_bytes() or None
        from ..winicons import icon_png

        png = icon_png(_icon_source(app["path"]), 64)
        try:
            cached.write_bytes(png or b"")
        except OSError:
            pass
        return png


def _icon_source(lnk: str) -> str:
    """Where a shortcut's icon really lives (custom icon location, else its target)."""
    if not lnk.lower().endswith(".lnk"):
        return lnk
    try:
        import pythoncom
        from win32com.shell import shell

        pythoncom.CoInitialize()
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(lnk)
        icon_path, _ = link.GetIconLocation()
        icon_path = os.path.expandvars(icon_path or "")
        if icon_path and os.path.exists(icon_path):
            return icon_path
        target = link.GetPath(shell.SLGP_RAWPATH)[0]
        target = os.path.expandvars(target or "")
        if target and os.path.exists(target):
            return target
    except Exception:
        pass
    return lnk


def build(ctx: "Context") -> APIRouter:
    svc = ProcessService(ctx)
    apps = AppIndex(ctx)
    ctx.services["processes"] = svc
    ctx.services["apps"] = apps
    gate = feature_dep(ctx, "processes")

    async def watcher() -> None:
        while True:
            await asyncio.sleep(2)
            if ctx.hub.has_subscribers("processes") and ctx.config.feature("processes"):
                rows = await asyncio.to_thread(svc.snapshot)
                ctx.hub.publish("processes", {"items": rows, "t": time.time()})

    async def warm() -> None:
        await asyncio.to_thread(apps.scan)

    ctx.background += [watcher, warm]

    async def on_sub(conn: "Conn") -> None:
        if ctx.config.feature("processes"):
            await conn.send({"type": "processes", "data": {"items": await asyncio.to_thread(svc.snapshot),
                                                            "t": time.time()}})

    ctx.subscribe_hooks["processes"] = on_sub

    r = APIRouter()

    @r.get("/processes")
    async def processes(dev: dict = Depends(gate)):
        return {"items": await asyncio.to_thread(svc.snapshot), "cores": svc.cores}

    @r.post("/processes/{pid}/kill")
    async def kill(pid: int, request: Request, body: dict[str, Any] | None = Body(None), dev: dict = Depends(gate)):
        require_confirm(body)
        tree = bool((body or {}).get("tree"))
        try:
            killed = await asyncio.to_thread(svc.kill, pid, tree)
        except ApiError as exc:
            ctx.audit("process.kill", f"pid {pid}", result=exc.error, device=dev, ip=client_ip(request))
            raise
        ctx.audit("process.kill", ", ".join(f"{k['name']}({k['pid']})" for k in killed),
                  device=dev, ip=client_ip(request), tree=tree)
        return {"ok": True, "killed": killed}

    @r.get("/apps")
    async def list_apps(refresh: int = 0, dev: dict = Depends(gate)):
        items = await asyncio.to_thread(apps.scan, bool(refresh))
        out = []
        for a in items:
            tok = ctx.signer.sign(dev["id"], "icon", {"a": a["id"]}, ttl=86400)
            out.append({"id": a["id"], "name": a["name"], "group": a["group"], "icon": f"/api/v1/appicon/{tok}"})
        return {"items": out}

    @r.get("/appicon/{token}")
    async def app_icon(token: str):
        data = ctx.signer.verify(token, "icon")
        if not data or not ctx.devices.get(data["d"]):
            raise ApiError(403, "link_expired", "Icon link expired.")
        png = await asyncio.to_thread(apps.icon, data["a"])
        if not png:
            raise ApiError(404, "no_icon", "No icon.")
        return Response(png, media_type="image/png", headers={"cache-control": "private, max-age=86400"})

    @r.post("/apps/{app_id}/launch")
    async def launch(app_id: str, request: Request, dev: dict = Depends(gate)):
        app = apps.apps.get(app_id)
        if not app:
            await asyncio.to_thread(apps.scan, True)
            app = apps.apps.get(app_id)
        if not app:
            raise ApiError(404, "no_app", "That app isn't in the Start Menu anymore.")
        if not ctx.dry_run:
            await asyncio.to_thread(os.startfile, app["path"])  # type: ignore[attr-defined]
        ctx.audit("apps.launch", app["name"], device=dev, ip=client_ip(request))
        return {"ok": True}

    @r.post("/apps/open-url")
    async def open_url(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        url = str(body.get("url") or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ApiError(400, "bad_url", "Only http(s) links can be opened.")
        if not ctx.dry_run:
            import webbrowser

            await asyncio.to_thread(webbrowser.open, url)
        ctx.audit("apps.open_url", url, device=dev, ip=client_ip(request))
        return {"ok": True, "url": url}

    return r
