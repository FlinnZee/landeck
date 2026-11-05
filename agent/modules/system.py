"""Dashboard: live stats sampler, threshold alerts and static PC info."""
from __future__ import annotations

import getpass
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

import psutil
from fastapi import APIRouter, Depends

from ..api import feature_dep
from ..config import CREATE_NO_WINDOW
from ..discovery import mac_addresses

if TYPE_CHECKING:
    from ..context import Context
    from ..hub import Conn

log = logging.getLogger("landeck.system")


def os_info() -> dict[str, Any]:
    info = {"name": platform.system(), "release": platform.release(), "build": platform.version()}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            def val(name: str, default: Any = "") -> Any:
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return default
            build = int(val("CurrentBuild", "0") or 0)
            product = str(val("ProductName", "Windows"))
            # ProductName still says "Windows 10" on 11
            if build >= 22000:
                product = product.replace("Windows 10", "Windows 11")
            info.update(name=product, release=str(val("DisplayVersion", "")),
                        build=f"{build}.{val('UBR', 0)}")
    except Exception:
        pass
    return info


def cpu_model() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor()


def gpu_stats() -> list[dict[str, Any]] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue

        def num(s: str) -> float | None:
            try:
                return float(s)
            except ValueError:
                return None
        gpus.append({"name": parts[0], "util": num(parts[1]), "mem_used": num(parts[2]),
                     "mem_total": num(parts[3]), "temp": num(parts[4])})
    return gpus or None


def active_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "")


class Sampler:
    INTERVAL = 1.0

    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.latest: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._procs: dict[int, psutil.Process] = {}
        self._last_net = psutil.net_io_counters()
        self._last_disk = psutil.disk_io_counters()
        self._last_t = time.monotonic()
        self._disks: list[dict[str, Any]] = []
        self._disks_at = 0.0
        self._top: dict[str, list] = {"cpu": [], "mem": []}
        self._top_at = 0.0
        self._gpu: list | None = None
        self._gpu_at = 0.0
        self._gpu_ok = shutil.which("nvidia-smi") is not None
        self._alerts = Alerts(ctx)
        self._thread: threading.Thread | None = None
        psutil.cpu_percent(percpu=True)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="stats", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            try:
                watching = self.ctx.hub.has_subscribers("stats")
                snap = self.sample(full=watching)
                self.latest = snap
                self._alerts.check(snap)
                if watching:
                    self.ctx.hub.publish("stats", snap)
            except Exception:
                log.exception("sampling failed")

    def sample(self, full: bool = True) -> dict[str, Any]:
        now = time.monotonic()
        dt = max(now - self._last_t, 0.001)
        self._last_t = now

        per_core = psutil.cpu_percent(percpu=True)
        total = sum(per_core) / len(per_core) if per_core else 0.0
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        net = psutil.net_io_counters()
        up = (net.bytes_sent - self._last_net.bytes_sent) / dt
        down = (net.bytes_recv - self._last_net.bytes_recv) / dt
        self._last_net = net

        dio = psutil.disk_io_counters()
        if dio and self._last_disk:
            rd = (dio.read_bytes - self._last_disk.read_bytes) / dt
            wr = (dio.write_bytes - self._last_disk.write_bytes) / dt
        else:
            rd = wr = 0.0
        self._last_disk = dio

        if now - self._disks_at > 5 or not self._disks:
            self._disks = self._disk_usage()
            self._disks_at = now

        snap: dict[str, Any] = {
            "t": time.time(),
            "cpu": {"total": round(total, 1), "cores": [round(c, 1) for c in per_core]},
            "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
            "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
            "disks": self._disks,
            "disk_io": {"read": max(rd, 0), "write": max(wr, 0)},
            "net": {"up": max(up, 0), "down": max(down, 0)},
            "uptime": time.time() - psutil.boot_time(),
            "battery": self._battery(),
        }
        if full:
            if now - self._top_at > 2:
                self._top = self._top_processes()
                self._top_at = now
            if self._gpu_ok and now - self._gpu_at > 2:
                self._gpu = gpu_stats()
                self._gpu_ok = self._gpu is not None
                self._gpu_at = now
            from ..native import window_title

            snap.update(top=self._top, gpu=self._gpu, window=window_title())
        return snap

    @staticmethod
    def _battery() -> dict[str, Any] | None:
        try:
            b = psutil.sensors_battery()
        except Exception:
            return None
        if b is None:
            return None
        left = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 else None
        return {"percent": round(b.percent, 1), "plugged": bool(b.power_plugged), "secs_left": left}

    @staticmethod
    def _disk_usage() -> list[dict[str, Any]]:
        out = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            out.append({"mount": part.mountpoint.rstrip("\\"), "fs": part.fstype, "total": u.total,
                        "used": u.used, "free": u.free, "percent": u.percent})
        return out

    def _top_processes(self) -> dict[str, list]:
        cores = psutil.cpu_count() or 1
        rows = []
        alive: set[int] = set()
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            pid = p.info["pid"]
            if pid == 0:
                continue
            alive.add(pid)
            proc = self._procs.get(pid)
            if proc is None:
                self._procs[pid] = p
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
                continue
            try:
                cpu = proc.cpu_percent(None) / cores
            except psutil.Error:
                continue
            mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            rows.append({"pid": pid, "name": p.info["name"], "cpu": round(cpu, 1), "mem": mem})
        for pid in list(self._procs):
            if pid not in alive:
                del self._procs[pid]
        return {
            "cpu": sorted(rows, key=lambda r: r["cpu"], reverse=True)[:5],
            "mem": sorted(rows, key=lambda r: r["mem"], reverse=True)[:5],
        }


class Alerts:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self._cpu_high_since: float | None = None
        self._last: dict[str, float] = {}

    def _fire(self, key: str, cooldown: float, title: str, body: str) -> None:
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < cooldown:
            return
        self._last[key] = now
        center = self.ctx.services.get("notify")
        if center:
            center.push("alert", title, body, level="warn")

    def check(self, s: dict[str, Any]) -> None:
        now = time.monotonic()
        cpu = s["cpu"]["total"]
        if cpu > 90:
            self._cpu_high_since = self._cpu_high_since or now
            if now - self._cpu_high_since >= 30:
                self._fire("cpu", 600, "CPU is pegged", f"Above 90% for 30 s (now {cpu:.0f}%).")
        else:
            self._cpu_high_since = None
        if s["mem"]["percent"] > 90:
            self._fire("mem", 600, "Memory almost full", f"RAM at {s['mem']['percent']:.0f}%.")
        for d in s.get("disks", []):
            if d["total"] and d["free"] / d["total"] < 0.10:
                self._fire(f"disk:{d['mount']}", 6 * 3600, f"Drive {d['mount']} is nearly full",
                           f"{100 - d['percent']:.0f}% free.")
        b = s.get("battery")
        if b and not b["plugged"] and b["percent"] < 15:
            self._fire("battery", 1800, "Battery low", f"{b['percent']:.0f}% left.")


def build(ctx: "Context") -> APIRouter:
    sampler = Sampler(ctx)
    ctx.services["system"] = sampler
    gate = feature_dep(ctx, "dashboard")

    async def start_sampler() -> None:
        sampler.start()

    ctx.background.append(start_sampler)
    ctx.shutdown_hooks.append(sampler.stop)

    async def on_sub(conn: "Conn") -> None:
        if not ctx.config.feature("dashboard"):
            return
        if sampler.latest:
            await conn.send({"type": "stats", "data": sampler.latest})

    ctx.subscribe_hooks["stats"] = on_sub

    r = APIRouter()

    @r.get("/system/info")
    async def info(dev: dict = Depends(gate)):
        vm = psutil.virtual_memory()
        return {
            "hostname": ctx.hostname,
            "name": ctx.config.name,
            "os": os_info(),
            "cpu": {"model": cpu_model(), "logical": psutil.cpu_count(),
                    "physical": psutil.cpu_count(logical=False)},
            "ram": vm.total,
            "user": active_user(),
            "boot": psutil.boot_time(),
            "ips": ctx.ips,
            "macs": mac_addresses(),
            "gpu": sampler._gpu_ok,
            "agent": {"version": ctx.version, "pid": os.getpid(), "started": ctx.started},
        }

    @r.get("/system/stats")
    async def stats(dev: dict = Depends(gate)):
        return sampler.latest or sampler.sample(full=True)

    return r
