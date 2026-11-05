"""Two-way clipboard sync with history, pins and secret masking."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from ..api import ApiError, client_ip, feature_dep

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.clipboard")

MAX_ITEMS = 50
MAX_IMAGES = 10
MAX_TEXT = 1_000_000
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_SECRET_HINT = re.compile(r"(pass(word|wd)?|pwd|secret|token|api[_-]?key|private[_ ]key|bearer)\s*[:=]",
                          re.IGNORECASE)
_SECRET_SHAPES = [
    re.compile(r"^(sk|pk|rk)[-_][A-Za-z0-9_\-]{16,}$"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{30,}$"),
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    re.compile(r"^xox[abpr]-[A-Za-z0-9-]{10,}$"),
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def looks_secret(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if _SECRET_HINT.search(t[:2000]):
        return True
    if any(p.search(t) for p in _SECRET_SHAPES):
        return True
    if 16 <= len(t) <= 200 and " " not in t and "\n" not in t and not t.startswith(("http://", "https://")):
        classes = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
        if classes >= 3 and _entropy(t) >= 3.5:
            return True
    return False


class WinClipboard:
    def _open(self) -> None:
        import win32clipboard

        for _ in range(10):
            try:
                win32clipboard.OpenClipboard()
                return
            except Exception:
                time.sleep(0.05)
        raise ApiError(503, "clipboard_busy", "Another app is holding the clipboard. Try again.")

    def sequence(self) -> int:
        import win32clipboard

        return win32clipboard.GetClipboardSequenceNumber()

    def read(self) -> tuple[str, Any] | None:
        import win32clipboard
        import win32con

        self._open()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return "text", win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            png_fmt = win32clipboard.RegisterClipboardFormat("PNG")
            if win32clipboard.IsClipboardFormatAvailable(png_fmt):
                return "image", bytes(win32clipboard.GetClipboardData(png_fmt))
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
                dib = win32clipboard.GetClipboardData(win32con.CF_DIB)
                return "image", _dib_to_png(dib)
        finally:
            win32clipboard.CloseClipboard()
        return None

    def write_text(self, text: str) -> None:
        import win32clipboard
        import win32con

        self._open()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

    def write_image(self, png: bytes) -> None:
        import win32clipboard
        import win32con
        from PIL import Image

        img = Image.open(io.BytesIO(png))
        img.load()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        dib = buf.getvalue()[14:]  # drop BITMAPFILEHEADER
        self._open()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
            # keep transparency for apps that understand the PNG format
            win32clipboard.SetClipboardData(win32clipboard.RegisterClipboardFormat("PNG"), png)
        finally:
            win32clipboard.CloseClipboard()


class MemoryClipboard:
    """Stand-in used by tests and dry runs."""

    def __init__(self) -> None:
        self.value: tuple[str, Any] | None = None
        self.seq = 0

    def sequence(self) -> int:
        return self.seq

    def read(self) -> tuple[str, Any] | None:
        return self.value

    def write_text(self, text: str) -> None:
        self.value = ("text", text)
        self.seq += 1

    def write_image(self, png: bytes) -> None:
        self.value = ("image", png)
        self.seq += 1


def _dib_to_png(dib: bytes) -> bytes:
    from PIL import Image

    header_size = int.from_bytes(dib[0:4], "little")
    bit_count = int.from_bytes(dib[14:16], "little")
    colors = int.from_bytes(dib[32:36], "little")
    compression = int.from_bytes(dib[16:20], "little")
    palette = (colors or (1 << bit_count if bit_count <= 8 else 0)) * 4
    if compression == 3 and header_size == 40:  # BI_BITFIELDS masks follow the header
        palette += 12
    offset = 14 + header_size + palette
    bmp = b"BM" + (14 + len(dib)).to_bytes(4, "little") + b"\0\0\0\0" + offset.to_bytes(4, "little") + dib
    out = io.BytesIO()
    Image.open(io.BytesIO(bmp)).save(out, "PNG")
    return out.getvalue()


def _thumb(png: bytes, size: int = 320) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    img.thumbnail((size, size))
    out = io.BytesIO()
    img.convert("RGBA").save(out, "PNG", optimize=True)
    return out.getvalue()


class ClipboardService:
    def __init__(self, ctx: "Context", backend: Any):
        self.ctx = ctx
        self.backend = backend
        self.items: list[dict[str, Any]] = []
        self.images: dict[str, bytes] = {}
        self._lock = threading.RLock()
        self._ignore_seq: int | None = None
        self._last_seq = -1
        self._stop = threading.Event()
        self.persist_path = ctx.home / "clipboard.json"
        if ctx.config.get("clipboard_persist"):
            self._load()

    # --- history ----------------------------------------------------------

    def _load(self) -> None:
        try:
            self.items = [i for i in json.loads(self.persist_path.read_text("utf-8")) if i["kind"] == "text"]
        except (OSError, ValueError, KeyError):
            self.items = []

    def _persist(self) -> None:
        if not self.ctx.config.get("clipboard_persist"):
            return
        texts = [i for i in self.items if i["kind"] == "text"]
        try:
            self.persist_path.write_text(json.dumps(texts), "utf-8")
        except OSError:
            log.warning("could not persist clipboard history")

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in item.items() if k != "text"}
        if item["kind"] == "text":
            text = item["text"]
            out["length"] = len(text)
            out["preview"] = "" if item["secret"] else text[:400]
        return out

    def _add(self, kind: str, value: Any, source: str) -> dict[str, Any]:
        with self._lock:
            if kind == "text" and self.items and self.items[0]["kind"] == "text" \
                    and self.items[0].get("text") == value:
                self.items[0]["ts"] = time.time()
                return self.items[0]
            item: dict[str, Any] = {"id": uuid.uuid4().hex[:10], "kind": kind, "ts": time.time(),
                                    "source": source, "pinned": False}
            if kind == "text":
                item["text"] = value
                item["secret"] = looks_secret(value)
            else:
                item["secret"] = False
                item["size"] = len(value)
                self.images[item["id"]] = value
            self.items.insert(0, item)
            self._trim()
            self._persist()
            return item

    def _trim(self) -> None:
        imgs = [i for i in self.items if i["kind"] == "image" and not i["pinned"]]
        for old in imgs[MAX_IMAGES:]:
            self.items.remove(old)
        while len(self.items) > MAX_ITEMS:
            victim = next((i for i in reversed(self.items) if not i["pinned"]), None)
            if victim is None:
                break
            self.items.remove(victim)
        live = {i["id"] for i in self.items}
        for key in list(self.images):
            if key not in live:
                del self.images[key]

    def get(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            for i in self.items:
                if i["id"] == item_id:
                    return i
        raise ApiError(404, "not_found", "That clipboard item is gone.")

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.public(i) for i in self.items]

    def pin(self, item_id: str, pinned: bool) -> dict[str, Any]:
        with self._lock:
            item = self.get(item_id)
            item["pinned"] = pinned
            self._persist()
            return self.public(item)

    def delete(self, item_id: str) -> None:
        with self._lock:
            item = self.get(item_id)
            self.items.remove(item)
            self.images.pop(item_id, None)
            self._persist()

    def clear(self) -> None:
        with self._lock:
            self.items = [i for i in self.items if i["pinned"]]
            self._trim()
            self._persist()

    # --- PC clipboard ------------------------------------------------------

    def set_text(self, text: str, source: str = "phone") -> dict[str, Any]:
        self.backend.write_text(text)
        self._ignore_seq = self.backend.sequence()
        item = self._add("text", text, source)
        self.ctx.hub.publish("clipboard", {"item": self.public(item), "origin": source})
        return item

    def set_image(self, png: bytes, source: str = "phone") -> dict[str, Any]:
        self.backend.write_image(png)
        self._ignore_seq = self.backend.sequence()
        item = self._add("image", png, source)
        self.ctx.hub.publish("clipboard", {"item": self.public(item), "origin": source})
        return item

    def on_pc_change(self) -> None:
        """Called from the message window (or the poller) when Windows says the clipboard changed."""
        try:
            seq = self.backend.sequence()
            if seq == self._last_seq:
                return
            self._last_seq = seq
            if self._ignore_seq is not None and seq == self._ignore_seq:
                return
            got = self.backend.read()
        except ApiError:
            return
        except Exception:
            log.exception("reading clipboard failed")
            return
        if not got:
            return
        kind, value = got
        if kind == "text":
            if not value or len(value) > MAX_TEXT:
                return
        elif len(value) > MAX_IMAGE_BYTES:
            return
        item = self._add(kind, value, "pc")
        self.ctx.hub.publish("clipboard", {"item": self.public(item), "origin": "pc"})

    def poll_forever(self) -> None:
        while not self._stop.wait(0.5):
            try:
                if self.backend.sequence() != self._last_seq:
                    self.on_pc_change()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()


def build(ctx: "Context") -> APIRouter:
    svc = ClipboardService(ctx, MemoryClipboard() if ctx.dry_run else WinClipboard())
    ctx.services["clipboard"] = svc
    ctx.shutdown_hooks.append(svc.stop)
    gate = feature_dep(ctx, "clipboard")
    r = APIRouter()

    @r.get("/clipboard")
    async def history(dev: dict = Depends(gate)):
        return {"items": svc.history(), "persist": bool(ctx.config.get("clipboard_persist"))}

    @r.post("/clipboard")
    async def set_text(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise ApiError(400, "text_required", "Nothing to send.")
        if len(text) > MAX_TEXT:
            raise ApiError(413, "too_large", "That's over 1 MB of text.")
        item = svc.set_text(text)
        ctx.audit("clipboard.set", f"{len(text)} chars", device=dev, ip=client_ip(request))
        return {"item": svc.public(item)}

    @r.post("/clipboard/image")
    async def set_image(request: Request, dev: dict = Depends(gate)):
        data = await request.body()
        if not data:
            raise ApiError(400, "image_required", "No image received.")
        if len(data) > MAX_IMAGE_BYTES:
            raise ApiError(413, "too_large", "Images up to 12 MB.")

        def to_png(raw: bytes) -> bytes:
            from PIL import Image, ImageOps

            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            out = io.BytesIO()
            img.save(out, "PNG")
            return out.getvalue()

        try:
            png = await asyncio.to_thread(to_png, data)
        except Exception:
            raise ApiError(415, "bad_image", "Couldn't read that image (HEIC isn't supported, try JPEG/PNG).")
        item = svc.set_image(png)
        ctx.audit("clipboard.set_image", f"{len(png)} bytes", device=dev, ip=client_ip(request))
        return {"item": svc.public(item)}

    @r.get("/clipboard/item/{item_id}")
    async def reveal(item_id: str, dev: dict = Depends(gate)):
        item = svc.get(item_id)
        if item["kind"] != "text":
            raise ApiError(400, "not_text", "That item is an image.")
        return {"id": item_id, "text": item["text"]}

    @r.get("/clipboard/item/{item_id}/image")
    async def image(item_id: str, thumb: int = 0, dev: dict = Depends(gate)):
        svc.get(item_id)
        png = svc.images.get(item_id)
        if png is None:
            raise ApiError(404, "not_found", "Image no longer in memory.")
        if thumb:
            png = await asyncio.to_thread(_thumb, png)
        return Response(png, media_type="image/png", headers={"cache-control": "private, max-age=3600"})

    @r.post("/clipboard/item/{item_id}/apply")
    async def apply(item_id: str, request: Request, dev: dict = Depends(gate)):
        item = svc.get(item_id)
        if item["kind"] == "text":
            svc.backend.write_text(item["text"])
        else:
            svc.backend.write_image(svc.images[item_id])
        svc._ignore_seq = svc.backend.sequence()
        ctx.audit("clipboard.apply", item_id, device=dev, ip=client_ip(request))
        return {"ok": True}

    @r.post("/clipboard/item/{item_id}/pin")
    async def pin(item_id: str, body: dict[str, Any] | None = Body(None), dev: dict = Depends(gate)):
        pinned = True if body is None else bool(body.get("pinned", True))
        return {"item": svc.pin(item_id, pinned)}

    @r.delete("/clipboard/item/{item_id}")
    async def delete(item_id: str, dev: dict = Depends(gate)):
        svc.delete(item_id)
        return {"ok": True}

    @r.delete("/clipboard")
    async def clear(dev: dict = Depends(gate)):
        svc.clear()
        return {"ok": True}

    return r
