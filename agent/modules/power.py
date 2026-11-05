"""Shutdown, restart, sleep, lock, display off and scheduled power actions."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import subprocess
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, feature_dep, require_confirm
from ..config import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.power")

ACTIONS = ("shutdown", "restart", "sleep", "hibernate", "lock", "signout")
GOES_OFFLINE = {"shutdown", "restart", "sleep", "hibernate", "signout"}
TIMER_NATIVE = {"shutdown", "restart"}  # Windows' own shutdown timer handles these


def capabilities() -> dict[str, bool]:
    buf = (ctypes.c_ubyte * 256)()
    try:
        ok = ctypes.windll.powrprof.GetPwrCapabilities(ctypes.byref(buf))
    except (AttributeError, OSError):
        ok = 0
    if not ok:
        return {"sleep": True, "hibernate": False, "modern_standby": False}
    s3, s4, hiber_file, aoac = buf[5], buf[6], buf[8], buf[20]
    return {"sleep": bool(s3 or aoac), "hibernate": bool(s4 and hiber_file), "modern_standby": bool(aoac)}


class RealPower:
    def __init__(self) -> None:
        self.caps = capabilities()

    def _shutdown_exe(self, *args: str) -> None:
        res = subprocess.run(["shutdown", *args], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW)
        if res.returncode not in (0, 1116):  # 1116 = no shutdown was in progress
            raise ApiError(500, "power_failed", (res.stderr or res.stdout).strip() or f"exit {res.returncode}")

    def run(self, action: str, delay: int = 0) -> None:
        if action == "shutdown":
            self._shutdown_exe("/s", "/t", str(delay))
        elif action == "restart":
            self._shutdown_exe("/r", "/t", str(delay))
        elif action == "signout":
            self._shutdown_exe("/l")
        elif action == "lock":
            ctypes.windll.user32.LockWorkStation()
        elif action == "sleep":
            if self.caps["modern_standby"] and not self.caps["sleep"]:
                self.display_off()
            elif not ctypes.windll.powrprof.SetSuspendState(False, False, False):
                # Modern Standby machines refuse S3; turning the display off is how they sleep
                self.display_off()
        elif action == "hibernate":
            if not self.caps["hibernate"]:
                raise ApiError(409, "hibernate_off", "Hibernation is disabled on this PC (powercfg /h on).")
            ctypes.windll.powrprof.SetSuspendState(True, False, False)
        else:
            raise ApiError(400, "unknown_action", action)

    def display_off(self) -> None:
        HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER = 0xFFFF, 0x0112, 0xF170
        ctypes.windll.user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)

    def abort(self) -> None:
        self._shutdown_exe("/a")


class DryPower:
    """Records what would have happened. Used by tests and LANDECK_DRY_RUN=1."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.caps = {"sleep": True, "hibernate": True, "modern_standby": False}

    def run(self, action: str, delay: int = 0) -> None:
        self.calls.append(("run", action, delay))

    def display_off(self) -> None:
        self.calls.append(("display_off",))

    def abort(self) -> None:
        self.calls.append(("abort",))


class PowerService:
    def __init__(self, ctx: "Context", backend: Any):
        self.ctx = ctx
        self.backend = backend
        self.schedule: dict[str, Any] | None = None
        self._timer: asyncio.Task | None = None

    def _announce(self, action: str, seconds: float) -> None:
        self.ctx.hub.publish("power", {"state": "going_offline", "action": action, "in": seconds})

    async def now(self, action: str, delay: int = 0) -> None:
        if action in GOES_OFFLINE and delay <= 0:
            self._announce(action, 0)
            await asyncio.sleep(0.6)  # let the event reach the phone first
        await asyncio.to_thread(self.backend.run, action, delay)

    async def set_schedule(self, action: str, due: float) -> dict[str, Any]:
        await self.cancel_schedule(quiet=True)
        seconds = max(0, int(round(due - time.time())))
        if action in TIMER_NATIVE:
            await asyncio.to_thread(self.backend.run, action, seconds)
        self.schedule = {"action": action, "due": due, "native": action in TIMER_NATIVE}
        self._timer = asyncio.create_task(self._wait(action, due))
        self.ctx.hub.publish("power", {"state": "scheduled", "schedule": self.schedule})
        return self.schedule

    async def _wait(self, action: str, due: float) -> None:
        try:
            await asyncio.sleep(max(0, due - time.time() - 3))
            self._announce(action, max(0, due - time.time()))
            await asyncio.sleep(max(0, due - time.time()))
            self.schedule = None
            if action not in TIMER_NATIVE:
                await asyncio.to_thread(self.backend.run, action, 0)
        except asyncio.CancelledError:
            pass

    async def cancel_schedule(self, quiet: bool = False) -> bool:
        had = self.schedule is not None
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self.schedule and self.schedule.get("native"):
            await asyncio.to_thread(self.backend.abort)
        self.schedule = None
        if had and not quiet:
            self.ctx.hub.publish("power", {"state": "schedule_cancelled"})
        return had


def build(ctx: "Context") -> APIRouter:
    svc = PowerService(ctx, DryPower() if ctx.dry_run else RealPower())
    ctx.services["power"] = svc
    gate = feature_dep(ctx, "power")
    r = APIRouter()

    @r.get("/power/info")
    async def info(dev: dict = Depends(gate)):
        return {"capabilities": svc.backend.caps, "schedule": svc.schedule, "now": time.time()}

    @r.post("/power/display-off")
    async def display_off(request: Request, dev: dict = Depends(gate)):
        await asyncio.to_thread(svc.backend.display_off)
        ctx.audit("power.display_off", device=dev, ip=client_ip(request))
        return {"ok": True}

    @r.post("/power/schedule")
    async def schedule(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        require_confirm(body)
        action = body.get("action")
        if action not in ACTIONS:
            raise ApiError(400, "unknown_action", f"Pick one of {', '.join(ACTIONS)}.")
        if body.get("at") is not None:
            due = float(body["at"])
        elif body.get("in_minutes") is not None:
            due = time.time() + float(body["in_minutes"]) * 60
        else:
            raise ApiError(400, "when_required", "Send 'at' (unix time) or 'in_minutes'.")
        if due < time.time() + 5 or due > time.time() + 7 * 86400:
            raise ApiError(400, "bad_time", "Schedule between 5 seconds and 7 days from now.")
        result = await svc.set_schedule(action, due)
        ctx.audit("power.schedule", f"{action} at {int(due)}", device=dev, ip=client_ip(request))
        return {"schedule": result}

    @r.delete("/power/schedule")
    async def cancel(request: Request, dev: dict = Depends(gate)):
        had = await svc.cancel_schedule()
        if not had:
            # also catch timers started outside LanDeck
            await asyncio.to_thread(svc.backend.abort)
        ctx.audit("power.schedule_cancel", device=dev, ip=client_ip(request))
        return {"ok": True, "cancelled": had}

    @r.post("/power/{action}")
    async def act(action: str, request: Request, body: dict[str, Any] | None = Body(None),
                  dev: dict = Depends(gate)):
        if action not in ACTIONS:
            raise ApiError(404, "unknown_action", f"Unknown power action '{action}'.")
        require_confirm(body)
        delay = int((body or {}).get("delay_seconds") or 0)
        if delay < 0 or delay > 86400:
            raise ApiError(400, "bad_delay", "delay_seconds must be between 0 and 86400.")
        ctx.audit(f"power.{action}", f"delay={delay}", device=dev, ip=client_ip(request))
        if delay and action in TIMER_NATIVE:
            await svc.set_schedule(action, time.time() + delay)
        else:
            await svc.now(action)
        return {"ok": True, "action": action, "delay_seconds": delay}

    return r
