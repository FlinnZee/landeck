"""Master volume, per-app mixer, media keys, now playing (SMTC) and output device switching."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, feature_dep

if TYPE_CHECKING:
    from ..context import Context
    from ..hub import Conn

log = logging.getLogger("landeck.media")

VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
      "vol_up": 0xAF, "vol_down": 0xAE, "mute": 0xAD}
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
PLAYBACK = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


def press_vk(vk: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


class WinAudio:
    """pycaw wrapper. Every call runs on one COM-initialised thread."""

    def __init__(self) -> None:
        import comtypes

        self.pool = ThreadPoolExecutor(1, thread_name_prefix="com-audio", initializer=comtypes.CoInitialize)
        self._names: dict[str, str] = {}
        self._icons: dict[str, str | None] = {}

    async def call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    def _endpoint(self):
        from pycaw.pycaw import AudioUtilities

        return AudioUtilities.GetSpeakers().EndpointVolume

    def master(self) -> dict[str, Any]:
        ep = self._endpoint()
        return {"volume": round(ep.GetMasterVolumeLevelScalar() * 100), "muted": bool(ep.GetMute())}

    def set_volume(self, value: int) -> None:
        ep = self._endpoint()
        ep.SetMasterVolumeLevelScalar(max(0, min(100, value)) / 100, None)
        if value > 0 and ep.GetMute():
            ep.SetMute(0, None)

    def set_mute(self, muted: bool) -> None:
        self._endpoint().SetMute(1 if muted else 0, None)

    def _app_name(self, exe: str, fallback: str) -> str:
        if exe not in self._names:
            from ..winicons import file_description

            self._names[exe] = file_description(exe) or fallback
        return self._names[exe]

    def _app_icon(self, exe: str) -> str | None:
        if exe not in self._icons:
            from ..winicons import icon_png

            png = icon_png(exe, 48)
            self._icons[exe] = "data:image/png;base64," + base64.b64encode(png).decode() if png else None
        return self._icons[exe]

    def sessions(self) -> list[dict[str, Any]]:
        from pycaw.pycaw import AudioUtilities

        out: dict[str, dict[str, Any]] = {}
        for s in AudioUtilities.GetAllSessions():
            try:
                vol = s.SimpleAudioVolume
                proc = s.Process
                if proc is None:
                    key, name, icon = "system", "System sounds", None
                else:
                    exe = proc.exe()
                    key = os.path.normcase(exe)
                    name = self._app_name(exe, proc.name().removesuffix(".exe"))
                    icon = self._app_icon(exe)
                item = out.get(key)
                if item is None:
                    out[key] = {"id": key, "pids": [s.ProcessId], "name": name, "icon": icon,
                                "volume": round(vol.GetMasterVolume() * 100), "muted": bool(vol.GetMute()),
                                "active": s.State == 1}
                else:
                    item["pids"].append(s.ProcessId)
                    item["active"] = item["active"] or s.State == 1
            except Exception:
                continue
        return sorted(out.values(), key=lambda i: (i["id"] != "system", not i["active"], i["name"].lower()))

    def set_session(self, key: str, volume: int | None, muted: bool | None) -> None:
        from pycaw.pycaw import AudioUtilities

        hit = False
        for s in AudioUtilities.GetAllSessions():
            proc = s.Process
            skey = "system" if proc is None else os.path.normcase(proc.exe())
            if skey != key:
                continue
            hit = True
            vol = s.SimpleAudioVolume
            if volume is not None:
                vol.SetMasterVolume(max(0, min(100, volume)) / 100, None)
            if muted is not None:
                vol.SetMute(1 if muted else 0, None)
        if not hit:
            raise ApiError(404, "no_session", "That app isn't playing audio anymore.")

    def devices(self) -> list[dict[str, Any]]:
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities

        default = AudioUtilities.GetSpeakers().id
        devs = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eRender.value,
                                            device_state=DEVICE_STATE.ACTIVE.value)
        return [{"id": d.id, "name": d.FriendlyName or "Output", "default": d.id == default} for d in devs]

    def set_device(self, dev_id: str) -> None:
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities

        if dev_id not in {d["id"] for d in self.devices()}:
            raise ApiError(404, "no_device", "That output isn't available.")
        AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])

    def key(self, name: str) -> None:
        press_vk(VK[name])


class FakeAudio:
    def __init__(self) -> None:
        self.state = {"volume": 40, "muted": False}
        self.keys: list[str] = []
        self.apps = [{"id": "system", "pids": [0], "name": "System sounds", "icon": None,
                      "volume": 100, "muted": False, "active": False}]
        self.outs = [{"id": "spk", "name": "Speakers", "default": True},
                     {"id": "hp", "name": "Headphones", "default": False}]

    async def call(self, fn, *args):
        return fn(*args)

    def master(self):
        return dict(self.state)

    def set_volume(self, value):
        self.state["volume"] = max(0, min(100, value))

    def set_mute(self, muted):
        self.state["muted"] = bool(muted)

    def sessions(self):
        return [dict(a) for a in self.apps]

    def set_session(self, key, volume, muted):
        for a in self.apps:
            if a["id"] == key:
                if volume is not None:
                    a["volume"] = volume
                if muted is not None:
                    a["muted"] = muted
                return
        raise ApiError(404, "no_session", "That app isn't playing audio anymore.")

    def devices(self):
        return [dict(d) for d in self.outs]

    def set_device(self, dev_id):
        for d in self.outs:
            d["default"] = d["id"] == dev_id

    def key(self, name):
        self.keys.append(name)


class NowPlaying:
    """Windows System Media Transport Controls via winsdk, on its own asyncio loop."""

    def __init__(self) -> None:
        self.available = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._art: dict[str, str] = {}
        try:
            from winsdk.windows.media.control import (  # noqa: F401
                GlobalSystemMediaTransportControlsSessionManager,
            )
        except Exception:
            return
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="smtc", daemon=True).start()
        self.available = True

    async def _run(self, coro):
        if not self._loop:
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), 4)
        except Exception:
            log.debug("smtc call failed", exc_info=True)
            return None

    async def _session(self):
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

        mgr = await M.request_async()
        return mgr.get_current_session()

    async def _info(self) -> dict[str, Any] | None:
        s = await self._session()
        if s is None:
            return None
        props = await s.try_get_media_properties_async()
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        info = {
            "title": props.title or "",
            "artist": props.artist or props.album_artist or "",
            "album": props.album_title or "",
            "app": _app_label(s.source_app_user_model_id or ""),
            "status": PLAYBACK.get(int(pb.playback_status), "unknown"),
            "position": tl.position.total_seconds() if tl and tl.position else None,
            "duration": tl.end_time.total_seconds() if tl and tl.end_time else None,
            "art": None,
        }
        key = f"{info['app']}|{info['title']}|{info['artist']}"
        if key in self._art:
            info["art"] = self._art[key]
        elif props.thumbnail is not None:
            info["art"] = await self._thumb(props.thumbnail)
            self._art = {key: info["art"]}
        return info

    async def _thumb(self, ref) -> str | None:
        try:
            from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

            stream = await ref.open_read_async()
            size = int(stream.size)
            if not size or size > 4 * 1024 * 1024:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(buf)
            data = bytearray(buf.length)
            reader.read_bytes(data)
            mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(bytes(data)).decode()
        except Exception:
            return None

    async def info(self) -> dict[str, Any] | None:
        return await self._run(self._info())

    async def control(self, action: str) -> bool:
        async def go() -> bool:
            s = await self._session()
            if s is None:
                return False
            fn = {"play_pause": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                  "prev": s.try_skip_previous_async, "stop": s.try_stop_async}.get(action)
            return bool(fn and await fn())
        return bool(await self._run(go()))


def _app_label(aumid: str) -> str:
    low = aumid.lower()
    for needle, label in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"),
                          ("firefox", "Firefox"), ("zunemusic", "Media Player"), ("vlc", "VLC"),
                          ("brave", "Brave"), ("opera", "Opera"), ("foobar", "foobar2000"),
                          ("applemusic", "Apple Music"), ("itunes", "iTunes")):
        if needle in low:
            return label
    tail = aumid.split("!")[-1].split("\\")[-1]
    return tail.removesuffix(".exe") or "Media"


def build(ctx: "Context") -> APIRouter:
    audio: Any = FakeAudio() if ctx.dry_run else WinAudio()
    now = NowPlaying() if not ctx.dry_run else None
    ctx.services["media"] = audio
    gate = feature_dep(ctx, "media")
    r = APIRouter()
    last: dict[str, Any] = {}

    async def snapshot(full: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"t": time.time(), **await audio.call(audio.master)}
        if full:
            try:
                out["sessions"] = await audio.call(audio.sessions)
            except Exception:
                log.debug("sessions failed", exc_info=True)
                out["sessions"] = []
            try:
                out["devices"] = await audio.call(audio.devices)
            except Exception:
                out["devices"] = []
        out["now"] = await now.info() if now and now.available else None
        out["can"] = {"now_playing": bool(now and now.available), "devices": len(out.get("devices", [])) > 0}
        return out

    async def watcher() -> None:
        tick = 0
        while True:
            await asyncio.sleep(1.5)
            if not ctx.hub.has_subscribers("media") or not ctx.config.feature("media"):
                continue
            tick += 1
            try:
                snap = await snapshot(full=tick % 3 == 0)
            except Exception:
                continue
            compare = {k: snap.get(k) for k in ("volume", "muted", "sessions", "devices")}
            now_key = snap["now"] and {k: snap["now"].get(k) for k in ("title", "artist", "status", "app")}
            if compare != {k: last.get(k) for k in compare} or now_key != last.get("now_key"):
                last.update(compare, now_key=now_key)
                ctx.hub.publish("media", snap)

    ctx.background.append(watcher)

    async def on_sub(conn: "Conn") -> None:
        if ctx.config.feature("media"):
            await conn.send({"type": "media", "data": await snapshot()})

    ctx.subscribe_hooks["media"] = on_sub

    @r.get("/media")
    async def state(dev: dict = Depends(gate)):
        return await snapshot()

    @r.post("/media/volume")
    async def volume(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        try:
            value = int(body["value"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "bad_value", "value must be 0-100.")
        await audio.call(audio.set_volume, value)
        return await audio.call(audio.master)

    @r.post("/media/mute")
    async def mute(body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        muted = body.get("muted")
        if muted is None:
            muted = not (await audio.call(audio.master))["muted"]
        await audio.call(audio.set_mute, bool(muted))
        return await audio.call(audio.master)

    @r.post("/media/session")
    async def session(body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        vol = body.get("volume")
        await audio.call(audio.set_session, str(body.get("id", "")),
                         int(vol) if vol is not None else None,
                         bool(body["muted"]) if "muted" in body else None)
        return {"ok": True}

    @r.post("/media/key")
    async def key(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        name = body.get("key")
        if name not in VK:
            raise ApiError(400, "bad_key", f"Use one of {', '.join(VK)}.")
        handled = False
        if now and now.available and name in ("play_pause", "next", "prev", "stop"):
            handled = await now.control(name)
        if not handled:
            await audio.call(audio.key, name)
        return {"ok": True}

    @r.post("/media/device")
    async def device(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        dev_id = str(body.get("id", ""))
        try:
            await audio.call(audio.set_device, dev_id)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(500, "switch_failed", f"Windows refused to switch output: {exc}")
        ctx.audit("media.device", dev_id, device=dev, ip=client_ip(request))
        return {"devices": await audio.call(audio.devices)}

    return r
