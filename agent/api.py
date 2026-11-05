"""Shared request plumbing: errors, auth and feature gates."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request

from .security import is_loopback

if TYPE_CHECKING:
    from .context import Context


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail: str = "", **extra: Any):
        self.status = status
        self.error = error
        self.detail = detail
        self.extra = extra


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return None


def device_dep(ctx: "Context") -> Callable[..., Any]:
    async def dep(request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        left = ctx.limiter.blocked(ip)
        if left:
            raise ApiError(429, "rate_limited", f"Too many failed attempts. Try again in {int(left) + 1} s.")
        dev = ctx.devices.verify(bearer(request))
        if dev is None:
            if ctx.limiter.fail(ip):
                ctx.audit("auth.blocked", result="blocked", ip=ip)
            raise ApiError(401, "unauthorized", "This device is not paired with the PC.")
        if request.headers.get("x-landeck") != "1":
            raise ApiError(403, "bad_request", "Missing X-LanDeck header.")
        ctx.devices.touch(dev["id"], ip)
        request.state.device = dev
        return dev
    return dep


def feature_dep(ctx: "Context", name: str) -> Callable[..., Any]:
    device = device_dep(ctx)

    async def dep(dev: dict[str, Any] = Depends(device)) -> dict[str, Any]:
        if not ctx.config.feature(name):
            raise ApiError(403, "feature_disabled",
                           f"'{name}' is turned off on the PC. Enable it in the LanDeck admin page.")
        return dev
    return dep


def local_dep(ctx: "Context") -> Callable[..., Any]:
    """Admin surface: loopback only, sane Host header (DNS rebinding) and the custom header on writes."""
    async def dep(request: Request) -> None:
        if not is_loopback(client_ip(request)):
            raise ApiError(403, "local_only", "The admin page is only available on the PC itself.")
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise ApiError(403, "bad_host", "Open the admin page via https://localhost.")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-landeck") != "1":
            raise ApiError(403, "bad_request", "Missing X-LanDeck header.")
    return dep


def require_confirm(body: dict[str, Any] | None) -> None:
    if not body or body.get("confirm") is not True:
        raise ApiError(400, "confirm_required", "This action needs {\"confirm\": true}.")
