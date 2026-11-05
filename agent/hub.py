from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketState

log = logging.getLogger("landeck.hub")

# delivered to every authenticated socket, no subscription needed
BROADCAST_TOPICS = {"notify", "power", "clipboard", "files", "features", "actions"}


class Conn:
    def __init__(self, ws: WebSocket, device: dict[str, Any], ip: str | None):
        self.ws = ws
        self.device = device
        self.ip = ip
        self.topics: set[str] = set()
        self.last_seen = time.monotonic()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.extra: dict[str, Any] = {}

    async def send(self, msg: dict[str, Any]) -> bool:
        return await self._send(text=json.dumps(msg, separators=(",", ":"), default=str))

    async def send_bytes(self, data: bytes) -> bool:
        return await self._send(data=data)

    async def _send(self, text: str | None = None, data: bytes | None = None) -> bool:
        if self.closed:
            return False
        try:
            async with self.send_lock:
                if self.ws.application_state != WebSocketState.CONNECTED:
                    self.closed = True
                    return False
                if text is not None:
                    await self.ws.send_text(text)
                else:
                    await self.ws.send_bytes(data or b"")
            return True
        except Exception:
            self.closed = True
            return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass


class Hub:
    def __init__(self) -> None:
        self.conns: set[Conn] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._listeners: list[Callable[[], None]] = []

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("hub listener failed")

    def add(self, conn: Conn) -> None:
        self.conns.add(conn)
        self._changed()

    def remove(self, conn: Conn) -> None:
        self.conns.discard(conn)
        self._changed()

    def subscribers(self, topic: str) -> list[Conn]:
        return [c for c in self.conns if topic in c.topics and not c.closed]

    def has_subscribers(self, topic: str) -> bool:
        return any(topic in c.topics for c in self.conns)

    def online_devices(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.conns:
            out[c.device["id"]] = out.get(c.device["id"], 0) + 1
        return out

    async def emit(self, topic: str, data: Any, device_id: str | None = None) -> None:
        msg = {"type": topic, "data": data}
        targets = [
            c for c in list(self.conns)
            if (device_id is None or c.device["id"] == device_id)
            and (topic in BROADCAST_TOPICS or topic in c.topics)
        ]
        if targets:
            await asyncio.gather(*(c.send(msg) for c in targets))

    def publish(self, topic: str, data: Any, device_id: str | None = None) -> None:
        """Thread-safe fire-and-forget version of emit()."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self.emit(topic, data, device_id))
        else:
            asyncio.run_coroutine_threadsafe(self.emit(topic, data, device_id), loop)

    async def drop_device(self, device_id: str, code: int = 4401, reason: str = "revoked") -> None:
        for c in [c for c in self.conns if c.device["id"] == device_id]:
            await c.send({"type": "revoked"})
            await c.close(code, reason)
            self.conns.discard(c)
        self._changed()

    async def close_all(self, code: int = 1001) -> None:
        for c in list(self.conns):
            await c.close(code, "agent stopping")
        self.conns.clear()
