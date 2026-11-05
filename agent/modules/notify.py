"""Messages both ways: phone -> Windows toast, PC events -> notification center on the phone."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, Request

from ..api import ApiError, client_ip, device_dep, feature_dep
from ..config import resource_dir

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("landeck.notify")


class Toaster:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.sent: list[dict[str, Any]] = []

    def show(self, title: str, body: str, sound: bool = True) -> None:
        if self.dry_run:
            self.sent.append({"title": title, "body": body, "sound": sound})
            return
        threading.Thread(target=self._show, args=(title, body, sound), daemon=True).start()

    def _show(self, title: str, body: str, sound: bool) -> None:
        try:
            from winotify import Notification, audio

            icon = resource_dir() / "assets" / "icon.png"
            toast = Notification(app_id="LanDeck", title=title, msg=body,
                                 icon=str(icon) if icon.exists() else "")
            toast.set_audio(audio.Default if sound else audio.Silent, loop=False)
            toast.show()
        except Exception:
            log.exception("toast failed")


class NotificationCenter:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx
        self.items: deque[dict[str, Any]] = deque(maxlen=100)
        self._lock = threading.Lock()

    def push(self, kind: str, title: str, body: str = "", level: str = "info", **data: Any) -> dict[str, Any]:
        item = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "kind": kind, "level": level,
                "title": title, "body": body, **data}
        with self._lock:
            self.items.appendleft(item)
        self.ctx.hub.publish("notify", item)
        return item

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.items)

    def clear(self) -> None:
        with self._lock:
            self.items.clear()


def build(ctx: "Context") -> APIRouter:
    center = NotificationCenter(ctx)
    toaster = Toaster(ctx.dry_run)
    ctx.services["notify"] = center
    ctx.services["toaster"] = toaster
    r = APIRouter()
    gate = feature_dep(ctx, "notify")
    device = device_dep(ctx)

    @r.get("/notifications")
    async def list_items(dev: dict = Depends(device)):
        return {"items": center.list()}

    @r.delete("/notifications")
    async def clear(dev: dict = Depends(device)):
        center.clear()
        return {"ok": True}

    @r.post("/notify")
    async def send_to_pc(request: Request, body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        title = str(body.get("title") or "").strip()[:120]
        text = str(body.get("body") or "").strip()[:1000]
        if not title and not text:
            raise ApiError(400, "empty", "Write something first.")
        toaster.show(title or f"Message from {dev['name']}", text, bool(body.get("sound", True)))
        ctx.audit("notify.toast", title, device=dev, ip=client_ip(request))
        return {"ok": True}

    return r
