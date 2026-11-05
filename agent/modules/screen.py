"""Screenshots and a live JPEG stream over the WebSocket."""
from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from ..api import ApiError, client_ip, feature_dep

if TYPE_CHECKING:
    from ..context import Context
    from ..hub import Conn

log = logging.getLogger("landeck.screen")

# scale relative to native, longest-edge cap, JPEG quality
PRESETS = {
    "saver": (0.5, 1280, 45),
    "balanced": (0.75, 1600, 62),
    "sharp": (1.0, 2560, 80),
}
FRAME_MAGIC = b"LDF1"
HEADER = struct.Struct("<4sIdHH")  # magic, seq, server time ms, width, height


class Grabber:
    """mss instances are per-thread, so each worker thread lazily owns one."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            sct = mss.mss()
            self._local.sct = sct
        return sct

    def monitors(self) -> list[dict[str, Any]]:
        mons = self._sct().monitors
        out = []
        for i, m in enumerate(mons[1:], start=1):
            out.append({"index": i, "left": m["left"], "top": m["top"], "width": m["width"],
                        "height": m["height"], "primary": m["left"] == 0 and m["top"] == 0})
        return out

    def grab(self, index: int):
        from PIL import Image

        mons = self._sct().monitors
        if index < 1 or index >= len(mons):
            raise ApiError(404, "no_monitor", "That monitor isn't connected.")
        mon = mons[index]
        shot = self._sct().grab(mon)
        img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)
        return img, mon


def _cursor_pos() -> tuple[int, int] | None:
    pt = wintypes.POINT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return None


def draw_cursor(img, mon: dict[str, Any]) -> None:
    pos = _cursor_pos()
    if not pos:
        return
    x, y = pos[0] - mon["left"], pos[1] - mon["top"]
    if not (0 <= x < img.width and 0 <= y < img.height):
        return
    from PIL import ImageDraw

    s = max(1.0, img.width / 1920)
    pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10, 19), (7, 12.5), (12.5, 12.5)]
    poly = [(x + px * s, y + py * s) for px, py in pts]
    d = ImageDraw.Draw(img)
    d.polygon(poly, fill=(255, 255, 255), outline=(0, 0, 0))


def encode(img, preset: str, scale_override: float | None = None) -> tuple[bytes, int, int]:
    from PIL import Image

    scale, cap, quality = PRESETS.get(preset, PRESETS["balanced"])
    if scale_override:
        scale = scale_override
    w, h = img.size
    factor = min(scale, cap / max(w, h))
    if factor < 0.999:
        img = img.resize((max(1, int(w * factor)), max(1, int(h * factor))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=False, subsampling=0 if preset == "sharp" else 2)
    return buf.getvalue(), img.width, img.height


class Stream:
    def __init__(self, conn: "Conn", monitor: int, fps: int, preset: str):
        self.conn = conn
        self.monitor = monitor
        self.fps = max(1, min(15, fps))
        self.preset = preset if preset in PRESETS else "balanced"
        self.seq = 0
        self.acked = 0
        self.last_ack = time.monotonic()
        self.rtts: list[float] = []
        self.sent_times: list[float] = []
        self.task: asyncio.Task | None = None
        self.cursor = True


class ScreenService:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.grabber = Grabber()
        self.pool = ThreadPoolExecutor(2, thread_name_prefix="screen")
        self.streams: dict[int, Stream] = {}

    async def run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    async def monitors(self) -> list[dict[str, Any]]:
        return await self.run(self.grabber.monitors)

    def _shot(self, monitor: int, quality: int, scale: float, fmt: str = "JPEG") -> bytes:
        img, mon = self.grabber.grab(monitor)
        if scale < 0.999:
            from PIL import Image

            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "PNG":
            img.save(buf, "PNG", optimize=False, compress_level=3)
        else:
            img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    async def shot(self, monitor: int = 1, quality: int = 80, scale: float = 1.0, fmt: str = "JPEG") -> bytes:
        return await self.run(self._shot, monitor, max(10, min(95, quality)), max(0.1, min(1.0, scale)), fmt)

    def _frame(self, st: Stream) -> tuple[bytes, int, int]:
        img, mon = self.grabber.grab(st.monitor)
        if st.cursor:
            draw_cursor(img, mon)
        return encode(img, st.preset)

    async def start(self, conn: "Conn", monitor: int, fps: int, preset: str) -> Stream:
        old = self.streams.get(id(conn))
        if old:
            old.monitor = monitor
            old.fps = max(1, min(15, fps))
            old.preset = preset if preset in PRESETS else "balanced"
            return old
        st = Stream(conn, monitor, fps, preset)
        self.streams[id(conn)] = st
        st.task = asyncio.create_task(self._loop(st))
        self._viewing_changed()
        return st

    async def stop(self, conn: "Conn") -> None:
        st = self.streams.pop(id(conn), None)
        if st and st.task:
            st.task.cancel()
        self._viewing_changed()

    def _viewing_changed(self) -> None:
        cb = self.ctx.on_viewing_changed
        if cb:
            try:
                cb(bool(self.streams))
            except Exception:
                log.exception("viewing callback failed")

    async def _loop(self, st: Stream) -> None:
        conn = st.conn
        last_stats = time.monotonic()
        try:
            while not conn.closed:
                started = time.monotonic()
                # flow control: at most two frames in flight, otherwise wait for the phone to catch up
                if st.seq - st.acked >= 2:
                    if started - st.last_ack > 10:
                        log.info("screen stream stalled, stopping")
                        break
                    await asyncio.sleep(0.01)
                    continue
                try:
                    jpeg, w, h = await self.run(self._frame, st)
                except ApiError as exc:
                    await conn.send({"type": "screen.error", "data": {"error": exc.error, "detail": exc.detail}})
                    break
                st.seq += 1
                header = HEADER.pack(FRAME_MAGIC, st.seq, time.time() * 1000, w, h)
                st.sent_times.append(time.monotonic())
                if not await conn.send_bytes(header + jpeg):
                    break
                now = time.monotonic()
                st.sent_times = [t for t in st.sent_times if now - t < 2]
                if now - last_stats >= 1:
                    last_stats = now
                    rtt = sum(st.rtts) / len(st.rtts) if st.rtts else None
                    st.rtts = st.rtts[-10:]
                    await conn.send({"type": "screen.stats", "data": {
                        "fps": round(len([t for t in st.sent_times if now - t < 1]), 1),
                        "rtt": round(rtt, 1) if rtt else None, "bytes": len(jpeg), "w": w, "h": h}})
                await asyncio.sleep(max(0, 1 / st.fps - (time.monotonic() - started)))
        except asyncio.CancelledError:
            pass
        finally:
            if self.streams.get(id(conn)) is st:
                self.streams.pop(id(conn), None)
                self._viewing_changed()
                await conn.send({"type": "screen.stopped", "data": {}})

    def ack(self, conn: "Conn", seq: int, t: float | None) -> None:
        st = self.streams.get(id(conn))
        if not st:
            return
        st.acked = max(st.acked, seq)
        st.last_ack = time.monotonic()
        if t:
            st.rtts.append(max(0.0, time.time() * 1000 - t))


def build(ctx: "Context") -> APIRouter:
    svc = ScreenService(ctx)
    ctx.services["screen"] = svc
    gate = feature_dep(ctx, "screen")

    def check() -> None:
        if not ctx.config.feature("screen"):
            raise ApiError(403, "feature_disabled", "Screen sharing is turned off on the PC.")

    @ctx.ws("screen.start")
    async def ws_start(conn: "Conn", msg: dict[str, Any]) -> None:
        check()
        first = id(conn) not in svc.streams
        st = await svc.start(conn, int(msg.get("monitor", 1)), int(msg.get("fps", 8)),
                             str(msg.get("preset", "balanced")))
        st.cursor = bool(msg.get("cursor", True))
        if first:
            ctx.audit("screen.view_start", f"monitor {st.monitor}", device=conn.device, ip=conn.ip)
            if ctx.config.get("screen_toast", True):
                toaster = ctx.services.get("toaster")
                if toaster:
                    toaster.show("Screen is being viewed", f"{conn.device['name']} is watching this PC.", sound=False)

    @ctx.ws("screen.stop")
    async def ws_stop(conn: "Conn", msg: dict[str, Any]) -> None:
        if id(conn) in svc.streams:
            await svc.stop(conn)
            ctx.audit("screen.view_stop", device=conn.device, ip=conn.ip)

    @ctx.ws("screen.ack")
    async def ws_ack(conn: "Conn", msg: dict[str, Any]) -> None:
        svc.ack(conn, int(msg.get("seq", 0)), msg.get("t"))

    async def on_close(conn: "Conn") -> None:
        if id(conn) in svc.streams:
            await svc.stop(conn)

    ctx.disconnect_hooks.append(on_close)

    r = APIRouter()

    @r.get("/screen/monitors")
    async def monitors(dev: dict = Depends(gate)):
        return {"monitors": await svc.monitors(), "presets": list(PRESETS)}

    @r.get("/screen/shot")
    async def shot(request: Request, monitor: int = 1, q: int = 80, scale: float = 1.0, dev: dict = Depends(gate)):
        data = await svc.shot(monitor, q, scale)
        ctx.audit("screen.shot", f"monitor {monitor}", device=dev, ip=client_ip(request))
        return Response(data, media_type="image/jpeg", headers={"cache-control": "no-store"})

    @r.post("/screen/shot/save")
    async def save(request: Request, body: dict[str, Any] | None = Body(None), dev: dict = Depends(gate)):
        monitor = int((body or {}).get("monitor", 1))
        path = await save_screenshot(ctx, monitor)
        ctx.audit("screen.save", path, device=dev, ip=client_ip(request))
        return {"ok": True, "path": str(path), "name": path.name}

    return r


async def save_screenshot(ctx: "Context", monitor: int = 1) -> Path:
    from .files import KNOWN_FOLDERS, known_folder

    svc: ScreenService = ctx.services["screen"]
    if ctx.config.get("upload_dir"):
        folder = ctx.services["files"].upload_dir()
    else:
        folder = Path(known_folder(KNOWN_FOLDERS["downloads"][1]) or Path.home() / "Downloads")
    folder.mkdir(parents=True, exist_ok=True)
    data = await svc.shot(monitor, fmt="PNG")
    path = folder / f"Screenshot {datetime.now():%Y-%m-%d %H-%M-%S}.png"
    path.write_bytes(data)
    return path
