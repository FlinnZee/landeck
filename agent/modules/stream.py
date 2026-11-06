"""Watch movies on the phone: stream video from the PC as HLS.

For H.264/HEVC the video is copied, not re-encoded, so a 4K HEVC HDR movie reaches the
phone at its original quality with HDR intact and starts instantly. Only the audio is
converted to AAC when it's a format iPhones can't decode (DTS, TrueHD, ...). Codecs the
phone can't decode at all are transcoded with the GPU (NVENC) as a fallback.

Segments are fragmented-MP4, generated on demand and aligned to keyframes, so seeking
anywhere in a movie works without pre-processing the whole file. Needs ffmpeg on the PC.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from ..api import ApiError, client_ip, feature_dep
from ..config import CREATE_NO_WINDOW

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.stream")

# codecs an iPhone decodes in hardware -> we can copy the video stream untouched
DIRECT_VIDEO = {"h264", "hevc"}
HDR_TRC = {"smpte2084", "arib-std-b67"}  # PQ (HDR10/DV) and HLG
IOS_AUDIO = {"aac"}  # copied as-is; everything else -> AAC
TARGET_SEG = 6.0  # seconds per HLS segment (aligned up to the next keyframe)
SESSION_IDLE = 600  # drop a playback session after 10 min with no requests
SEEK_KF_LIMIT = 200_000  # cap the keyframe index so a huge file can't blow up memory


def _find_tool(name: str, configured: str | None) -> str | None:
    if configured:
        cand = Path(configured)
        exe = cand / f"{name}.exe" if cand.is_dir() else cand
        if exe.exists():
            return str(exe)
    found = shutil.which(name)
    if found:
        return found
    # common winget install locations
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.is_dir():
        for hit in base.glob(f"*FFmpeg*/**/{name}.exe"):
            return str(hit)
    return None


class FFmpeg:
    def __init__(self, ctx: "Context"):
        configured = ctx.config.get("ffmpeg_path") or None
        self.ffmpeg = _find_tool("ffmpeg", configured)
        self.ffprobe = _find_tool("ffprobe", configured)
        self.available = bool(self.ffmpeg and self.ffprobe)
        self.nvenc = self._has_nvenc() if self.available else False

    def _has_nvenc(self) -> bool:
        try:
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                 text=True, timeout=10, creationflags=CREATE_NO_WINDOW).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "hevc_nvenc" in out or "h264_nvenc" in out

    def probe(self, path: Path) -> dict[str, Any]:
        res = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        if res.returncode != 0:
            raise ApiError(422, "probe_failed", "ffmpeg couldn't read that file.")
        data = json.loads(res.stdout or "{}")
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and s.get("disposition", {}).get("attached_pic") != 1), None)
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        subs = [s for s in streams if s.get("codec_type") == "subtitle"]
        if not video:
            raise ApiError(415, "no_video", "No video track in that file.")
        trc = (video.get("color_transfer") or "").lower()
        dovi = any(sd.get("side_data_type") == "DOVI configuration record"
                   for sd in video.get("side_data_list", []))
        pix = video.get("pix_fmt", "")
        vcodec = (video.get("codec_name") or "").lower()
        try:
            duration = float(fmt.get("duration") or video.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return {
            "duration": round(duration, 3),
            "container": fmt.get("format_name", ""),
            "size": int(fmt.get("size") or 0),
            "video": {
                "codec": vcodec,
                "width": video.get("width"),
                "height": video.get("height"),
                "bit_depth": 10 if "10" in pix or "p010" in pix else 8,
                "hdr": trc in HDR_TRC or dovi,
                "dolby_vision": dovi,
                "transfer": trc,
            },
            "audios": [{
                "index": s.get("index"),
                "codec": (s.get("codec_name") or "").lower(),
                "channels": s.get("channels"),
                "lang": s.get("tags", {}).get("language", ""),
                "title": s.get("tags", {}).get("title", ""),
            } for s in audios],
            "subs": [{
                "index": s.get("index"),
                "codec": (s.get("codec_name") or "").lower(),
                "lang": s.get("tags", {}).get("language", ""),
                "title": s.get("tags", {}).get("title", ""),
            } for s in subs],
        }

    def keyframes(self, path: Path) -> list[float]:
        """Video keyframe timestamps, used to cut segments on clean boundaries."""
        res = subprocess.run(
            [self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        out = []
        for line in res.stdout.splitlines():
            parts = line.split(",")
            if len(parts) >= 2 and "K" in parts[1]:
                try:
                    out.append(round(float(parts[0]), 3))
                except ValueError:
                    continue
                if len(out) >= SEEK_KF_LIMIT:
                    break
        return sorted(set(out))


def plan_segments(keyframes: list[float], duration: float) -> list[tuple[float, float]]:
    """Group keyframes into ~TARGET_SEG chunks; returns (start, dur) per segment."""
    if not keyframes or keyframes[0] > 0.1:
        keyframes = [0.0] + keyframes
    bounds = [keyframes[0]]
    for kf in keyframes[1:]:
        if kf - bounds[-1] >= TARGET_SEG:
            bounds.append(kf)
    if duration > bounds[-1] + 0.1:
        bounds.append(round(duration, 3))
    return [(bounds[i], round(bounds[i + 1] - bounds[i], 3)) for i in range(len(bounds) - 1)]


class Session:
    def __init__(self, sid: str, path: Path, info: dict[str, Any], segments: list[tuple[float, float]],
                 audio_index: int | None, transcode: bool, work: Path):
        self.sid = sid
        self.path = path
        self.info = info
        self.segments = segments
        self.audio_index = audio_index
        self.transcode = transcode
        self.work = work
        # capability token: the iPhone's native HLS player can't send our auth header, so the
        # playlist, init and segment URLs carry this instead (same idea as the file download links).
        self.token = secrets.token_urlsafe(24)
        self.device_id = ""
        self.touched = time.monotonic()
        self.init_lock = threading.Lock()
        self.seg_locks: dict[int, threading.Lock] = {}
        self._dlock = threading.Lock()

    def seg_lock(self, n: int) -> threading.Lock:
        with self._dlock:
            return self.seg_locks.setdefault(n, threading.Lock())

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.touched > SESSION_IDLE

    def playlist(self) -> str:
        lines = ["#EXTM3U", "#EXT-X-VERSION:7",
                 f"#EXT-X-TARGETDURATION:{int(max((d for _, d in self.segments), default=TARGET_SEG)) + 1}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                 "#EXT-X-INDEPENDENT-SEGMENTS", '#EXT-X-MAP:URI="init.mp4"']
        lines[-1] = f'#EXT-X-MAP:URI="init.mp4?k={self.token}"'
        for i, (_, dur) in enumerate(self.segments):
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"s{i}.m4s?k={self.token}")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


class StreamService:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.ff = FFmpeg(ctx)
        self.sessions: dict[str, Session] = {}
        self.kf_cache: dict[str, tuple[float, list[float]]] = {}
        self.root = ctx.home / "stream"
        self.root.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        for old in self.root.glob("*"):  # clear any leftovers from a previous run
            shutil.rmtree(old, ignore_errors=True)

    # --- decisions -------------------------------------------------------

    def direct_play(self, info: dict[str, Any]) -> tuple[bool, str]:
        v = info["video"]
        if v["codec"] not in DIRECT_VIDEO:
            return False, f"{v['codec'].upper()} video isn't supported on iPhone; it will be converted."
        if v["dolby_vision"] and v["transfer"] not in HDR_TRC:
            return False, "Dolby Vision profile needs converting; HDR may become SDR."
        return True, "Streams at original quality."

    # --- sessions --------------------------------------------------------

    def _kf(self, path: Path) -> list[float]:
        st = path.stat()
        key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
        cached = self.kf_cache.get(key)
        if cached:
            return cached[1]
        kf = self.ff.keyframes(path)
        self.kf_cache[key] = (time.monotonic(), kf)
        if len(self.kf_cache) > 64:
            oldest = min(self.kf_cache, key=lambda k: self.kf_cache[k][0])
            del self.kf_cache[oldest]
        return kf

    def open(self, path: Path, audio_index: int | None, force_transcode: bool) -> Session:
        if not self.ff.available:
            raise ApiError(503, "no_ffmpeg", "ffmpeg isn't installed on the PC. Install it to stream video.")
        info = self.ff.probe(path)
        direct, _ = self.direct_play(info)
        transcode = force_transcode or not direct
        if transcode and not self.ff.nvenc:
            # still possible with libx264, just slower; allow it
            log.info("transcoding without NVENC (CPU)")
        keyframes = self._kf(path)
        segments = plan_segments(keyframes, info["duration"])
        if not segments:
            raise ApiError(422, "no_segments", "Couldn't work out how to segment that file.")
        sid = uuid.uuid4().hex[:16]
        work = self.root / sid
        work.mkdir()
        if audio_index is None and info["audios"]:
            audio_index = info["audios"][0]["index"]
        sess = Session(sid, path, info, segments, audio_index, transcode, work)
        with self._lock:
            self._reap_locked()
            self.sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Session:
        sess = self.sessions.get(sid)
        if not sess:
            raise ApiError(404, "no_session", "That playback session ended. Start the video again.")
        sess.touched = time.monotonic()
        return sess

    def close(self, sid: str) -> None:
        with self._lock:
            sess = self.sessions.pop(sid, None)
        if sess:
            shutil.rmtree(sess.work, ignore_errors=True)

    def _reap_locked(self) -> None:
        for sid in [s for s, v in self.sessions.items() if v.expired]:
            sess = self.sessions.pop(sid, None)
            if sess:
                shutil.rmtree(sess.work, ignore_errors=True)

    def reap(self) -> None:
        with self._lock:
            self._reap_locked()

    # --- ffmpeg output ---------------------------------------------------

    def _audio_args(self, sess: Session) -> list[str]:
        audios = sess.info["audios"]
        chosen = next((a for a in audios if a["index"] == sess.audio_index), audios[0] if audios else None)
        if chosen is None:
            return ["-an"]
        amap = ["-map", f"0:{chosen['index']}"]
        if chosen["codec"] in IOS_AUDIO and not sess.transcode:
            return [*amap, "-c:a", "copy"]
        return [*amap, "-c:a", "aac", "-ac", "2", "-b:a", "256k"]

    def _video_args(self, sess: Session) -> list[str]:
        if not sess.transcode:
            args = ["-map", "0:v:0", "-c:v", "copy"]
            if sess.info["video"]["codec"] == "hevc":
                args += ["-tag:v", "hvc1"]  # Apple needs this tag for HEVC; it's invalid for H.264
            return args
        height = min(sess.info["video"].get("height") or 1080, 1080)
        if self.ff.nvenc:
            enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "23"]
        else:
            enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]
        return ["-map", "0:v:0", "-vf", f"scale=-2:{height}:flags=bicubic,format=yuv420p", *enc,
                "-profile:v", "high", "-g", "48", "-force_key_frames", "expr:gte(t,n_forced*6)"]

    def build_init(self, sess: Session) -> Path:
        init = sess.work / "init.mp4"
        if init.exists():
            return init
        with sess.init_lock:
            if init.exists():
                return init
            # the HLS init filename is written relative to the process cwd, so run inside the session dir
            cmd = [self.ff.ffmpeg, "-y", "-v", "error", "-i", str(sess.path),
                   *self._video_args(sess), *self._audio_args(sess), "-t", "0.1",
                   "-f", "hls", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
                   "-hls_segment_filename", "_init_%d.m4s", "_init.m3u8"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(sess.work),
                                 creationflags=CREATE_NO_WINDOW)
            for junk in sess.work.glob("_init*"):
                junk.unlink(missing_ok=True)
            if not init.exists():
                raise ApiError(500, "init_failed", (res.stderr or "ffmpeg init failed")[-300:])
        return init

    def build_segment(self, sess: Session, n: int) -> Path:
        if n < 0 or n >= len(sess.segments):
            raise ApiError(404, "no_segment", "That part of the video doesn't exist.")
        seg = sess.work / f"s{n}.m4s"
        if seg.exists():
            return seg
        with sess.seg_lock(n):
            if seg.exists():
                return seg
            start, dur = sess.segments[n]
            tmp = sess.work / f"s{n}.tmp"
            # -copyts keeps the original timestamps (so the segment lands at the right place on the
            # timeline); with it, -to is absolute, which is what bounds the segment correctly.
            cmd = [self.ff.ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-copyts",
                   "-i", str(sess.path), "-to", f"{start + dur:.3f}",
                   *self._video_args(sess), *self._audio_args(sess),
                   "-muxdelay", "0", "-muxpreload", "0",
                   "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof+dash", str(tmp)]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=CREATE_NO_WINDOW)
            if res.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                raise ApiError(500, "segment_failed", (res.stderr or "ffmpeg segment failed")[-300:])
            os.replace(tmp, seg)
            self._evict(sess)
        return seg

    def _evict(self, sess: Session, keep: int = 24) -> None:
        segs = sorted(sess.work.glob("s*.m4s"), key=lambda p: p.stat().st_mtime)
        for old in segs[:-keep]:
            old.unlink(missing_ok=True)


def build(ctx: "Context") -> APIRouter:
    svc = StreamService(ctx)
    ctx.services["stream"] = svc
    gate = feature_dep(ctx, "stream")
    r = APIRouter()

    async def reaper() -> None:
        while True:
            await asyncio.sleep(120)
            svc.reap()

    ctx.background.append(reaper)
    ctx.shutdown_hooks.append(lambda: shutil.rmtree(svc.root, ignore_errors=True))

    def resolve(root: str, path: str) -> Path:
        return ctx.services["files"].resolve(root, path)

    @r.get("/stream/info")
    async def info(root: str, path: str, dev: dict = Depends(gate)):
        if not svc.ff.available:
            return {"available": False, "reason": "ffmpeg isn't installed on the PC."}
        p = resolve(root, path)
        data = await asyncio.to_thread(svc.ff.probe, p)
        direct, note = svc.direct_play(data)
        return {"available": True, "nvenc": svc.ff.nvenc, "direct_play": direct, "note": note, **data}

    @r.post("/stream/open")
    async def open_stream(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        p = resolve(str(body.get("root", "")), str(body.get("path", "")))
        audio = body.get("audio")
        sess = await asyncio.to_thread(svc.open, p, int(audio) if audio is not None else None,
                                       bool(body.get("transcode")))
        sess.device_id = dev["id"]
        ctx.audit("stream.open", p, device=dev, ip=client_ip(request),
                  mode="transcode" if sess.transcode else "direct")
        return {"session": sess.sid, "m3u8": f"/api/v1/stream/{sess.sid}/index.m3u8?k={sess.token}",
                "duration": sess.info["duration"], "segments": len(sess.segments),
                "transcode": sess.transcode, "video": sess.info["video"], "audios": sess.info["audios"]}

    def authed(sid: str, k: str) -> Session:
        sess = svc.get(sid)
        if not k or not hmac.compare_digest(k, sess.token):
            raise ApiError(403, "bad_token", "This stream link isn't valid.")
        if not ctx.config.feature("stream") or (sess.device_id and not ctx.devices.get(sess.device_id)):
            raise ApiError(403, "forbidden", "Streaming is off or this device was removed.")
        return sess

    @r.get("/stream/{sid}/index.m3u8")
    async def playlist(sid: str, k: str = ""):
        sess = authed(sid, k)
        return Response(sess.playlist(), media_type="application/vnd.apple.mpegurl",
                        headers={"cache-control": "no-store"})

    @r.get("/stream/{sid}/init.mp4")
    async def init_seg(sid: str, k: str = ""):
        sess = authed(sid, k)
        path = await asyncio.to_thread(svc.build_init, sess)
        return Response(path.read_bytes(), media_type="video/mp4",
                        headers={"cache-control": "private, max-age=3600"})

    @r.get("/stream/{sid}/s{n}.m4s")
    async def segment(sid: str, n: int, k: str = ""):
        sess = authed(sid, k)
        await asyncio.to_thread(svc.build_init, sess)
        path = await asyncio.to_thread(svc.build_segment, sess, n)
        return Response(path.read_bytes(), media_type="video/iso.segment",
                        headers={"cache-control": "private, max-age=3600"})

    @r.delete("/stream/{sid}")
    async def close_stream(sid: str, dev: dict = Depends(gate)):
        svc.close(sid)
        return {"ok": True}

    return r
