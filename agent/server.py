from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import admin
from .api import ApiError, client_ip, device_dep
from .config import resource_dir
from .context import Context
from .hub import Conn
from .modules import MODULES
from .security import is_lan, is_loopback

log = logging.getLogger("landeck.server")

APP_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "connect-src 'self' https: wss:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
CORS_HEADERS = "Authorization, Content-Type, X-LanDeck, X-Terminal-Session"


class GuardMiddleware:
    """LAN-only filter, security headers and CORS between our own hostnames."""

    def __init__(self, app: ASGIApp, ctx: Context):
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        ip = (scope.get("client") or ("", 0))[0]
        path = scope.get("path", "")
        if not is_lan(ip) or (path.startswith("/admin") and not is_loopback(ip)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            body = json.dumps({"error": "forbidden", "detail": "LAN access only."}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return

        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        cors = origin if origin and self.ctx.origin_ok(origin) else None

        if scope["method"] == "OPTIONS" and cors:
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", cors.encode()),
                (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, DELETE"),
                (b"access-control-allow-headers", CORS_HEADERS.encode()),
                (b"access-control-max-age", b"600"),
                (b"vary", b"Origin"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        dev = self.ctx.dev

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers", []))
                present = {k.lower() for k, _ in hdrs}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if b"content-security-policy" not in present:
                    extra.append((b"content-security-policy", APP_CSP.encode()))
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store" if dev or path.startswith("/api")
                                  else b"no-cache"))
                if cors:
                    extra += [(b"access-control-allow-origin", cors.encode()), (b"vary", b"Origin")]
                message["headers"] = hdrs + [h for h in extra if h[0] not in present]
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(ctx: Context) -> FastAPI:
    web = resource_dir() / "web"

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.hub.attach(asyncio.get_running_loop())
        tasks = [asyncio.create_task(job()) for job in ctx.background]
        tasks.append(asyncio.create_task(_network_watch(ctx)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await ctx.hub.close_all()
            for hook in ctx.shutdown_hooks:
                try:
                    hook()
                except Exception:
                    log.exception("shutdown hook failed")

    app = FastAPI(title="LanDeck", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": exc.error, "detail": exc.detail, **exc.extra}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return JSONResponse({"error": "invalid_request", "detail": f"{where}: {first.get('msg', 'invalid')}"},
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": "http_error", "detail": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"error": "internal", "detail": "Something went wrong on the PC."}, status_code=500)

    device = device_dep(ctx)

    # --- unauthenticated --------------------------------------------------

    @app.get("/api/v1/health")
    async def health():
        return {"ok": True, "name": ctx.config.name, "version": ctx.version}

    @app.post("/api/v1/pair")
    async def pair(request: Request, body: dict[str, Any] = Body(...)):
        ip = client_ip(request)
        left = ctx.limiter.blocked(ip)
        if left:
            raise ApiError(429, "rate_limited", f"Too many attempts. Try again in {int(left) + 1} s.")
        name = str(body.get("name") or "").strip()[:48]
        if not name:
            raise ApiError(400, "name_required", "Give this device a name.")
        secret = body.get("secret")
        code = body.get("code")
        result = ctx.pairing.consume(secret=str(secret) if secret else None,
                                     code=str(code) if code else None)
        if result != "ok":
            ctx.limiter.fail(ip)
            ctx.audit("pair", name, result=result, ip=ip)
            if result == "expired":
                raise ApiError(410, "pairing_expired", "That pairing code expired. Start pairing again on the PC.")
            raise ApiError(401, "pairing_invalid", "That pairing code isn't valid. Start pairing again on the PC.")

        allowed = await asyncio.to_thread(ctx.confirm_device, name, ip)
        if not allowed:
            ctx.audit("pair", name, result="denied", ip=ip)
            raise ApiError(403, "pairing_denied", "The request was declined on the PC.")

        dev, token = ctx.devices.create(name, ip)
        ctx.limiter.reset(ip)
        ctx.audit("pair", name, result="ok", device=dev, ip=ip)
        ctx.hub.publish("notify", {"kind": "system", "title": "Device paired", "body": name})
        admin.notify_admin_changed(ctx)
        return {"token": token, "device": dev, "server": ctx.server_info()}

    @app.get("/ca.mobileconfig")
    async def mobileconfig():
        data = ctx.certs.mobileconfig(f"LanDeck ({ctx.config.name})")
        return Response(data, media_type="application/x-apple-aspen-config",
                        headers={"content-disposition": 'attachment; filename="LanDeck.mobileconfig"'})

    @app.get("/ca.cer")
    async def ca_cer():
        return Response(ctx.certs.ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"content-disposition": 'attachment; filename="LanDeck-CA.cer"'})

    # --- this device ------------------------------------------------------

    @app.get("/api/v1/me")
    async def me(dev: dict = Depends(device)):
        return {"device": dev, "features": ctx.config.features(), "server": ctx.server_info()}

    @app.post("/api/v1/me/rename")
    async def rename(body: dict[str, Any] = Body(...), dev: dict = Depends(device)):
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "name_required", "Name can't be empty.")
        ctx.devices.rename(dev["id"], name)
        admin.notify_admin_changed(ctx)
        return {"device": ctx.devices.get(dev["id"])}

    @app.post("/api/v1/me/disconnect")
    async def disconnect(request: Request, dev: dict = Depends(device)):
        ctx.devices.revoke(dev["id"])
        ctx.audit("device.disconnect", dev["name"], device=dev, ip=client_ip(request))
        await ctx.hub.drop_device(dev["id"], reason="disconnected")
        admin.notify_admin_changed(ctx)
        return {"ok": True}

    for mod in MODULES:
        app.include_router(mod.build(ctx), prefix="/api/v1")

    app.include_router(admin.build(ctx))

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await _ws_session(ctx, ws)

    # --- static shell -----------------------------------------------------

    index = web / "index.html"

    @app.get("/")
    @app.get("/pair")
    async def shell():
        return FileResponse(index, media_type="text/html")

    @app.get("/admin")
    async def admin_page(request: Request):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ApiError(403, "bad_host", "Open the admin page via https://localhost.")
        return FileResponse(web / "admin.html", media_type="text/html")

    app.mount("/", StaticFiles(directory=web), name="web")
    return GuardMiddleware(app, ctx)  # type: ignore[return-value]


async def _network_watch(ctx: Context) -> None:
    while True:
        await asyncio.sleep(20)
        try:
            await asyncio.to_thread(ctx.refresh_network)
        except Exception:
            log.exception("network refresh failed")


async def _ws_session(ctx: Context, ws: WebSocket) -> None:
    ip = ws.client.host if ws.client else ""
    if not ctx.origin_ok(ws.headers.get("origin")):
        await ws.close(code=4403)
        return
    if ctx.limiter.blocked(ip):
        await ws.close(code=4429)
        return
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=3)
    except Exception:
        await _close(ws, 4408, "auth timeout")
        return
    dev = ctx.devices.verify(first.get("token")) if isinstance(first, dict) and first.get("type") == "auth" else None
    if dev is None:
        if ctx.limiter.fail(ip):
            ctx.audit("auth.blocked", result="blocked", ip=ip)
        await _close(ws, 4401, "unauthorized")
        return

    ctx.devices.touch(dev["id"], ip)
    conn = Conn(ws, dev, ip)
    ctx.hub.add(conn)
    await conn.send({"type": "hello", "data": {
        "device": dev, "features": ctx.config.features(), "server": ctx.server_info(), "t": time.time(),
    }})
    beat = asyncio.create_task(_heartbeat(conn))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            conn.last_seen = time.monotonic()
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            await _dispatch(ctx, conn, data)
    except Exception as exc:
        if not conn.closed:
            log.debug("socket error: %s", exc)
    finally:
        beat.cancel()
        conn.closed = True
        ctx.hub.remove(conn)
        for hook in ctx.disconnect_hooks:
            try:
                await hook(conn)
            except Exception:
                log.exception("disconnect hook failed")


async def _dispatch(ctx: Context, conn: Conn, data: dict[str, Any]) -> None:
    kind = data.get("type")
    if kind == "pong":
        return
    if kind in ("sub", "unsub"):
        topics = {t for t in data.get("topics", []) if isinstance(t, str)}
        if kind == "sub":
            new = topics - conn.topics
            conn.topics |= topics
            for t in new:
                hook = ctx.subscribe_hooks.get(t)
                if hook:
                    await hook(conn)
        else:
            conn.topics -= topics
        return
    handler = ctx.ws_handlers.get(str(kind))
    if handler is None:
        return
    # devices can be revoked between messages; the hub drops them, but be strict anyway
    if ctx.devices.get(conn.device["id"]) is None:
        await conn.close(4401, "revoked")
        return
    try:
        await handler(conn, data)
    except ApiError as exc:
        await conn.send({"type": "error", "data": {"error": exc.error, "detail": exc.detail, "for": kind}})
    except Exception:
        log.exception("ws handler %s failed", kind)


async def _heartbeat(conn: Conn) -> None:
    while not conn.closed:
        await asyncio.sleep(5)
        if time.monotonic() - conn.last_seen > 15:
            await conn.close(4000, "heartbeat timeout")
            return
        await conn.send({"type": "ping", "t": time.time()})


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


def static_root() -> Path:
    return resource_dir() / "web"
