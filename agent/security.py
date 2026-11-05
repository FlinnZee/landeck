from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LAN_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "::1/128",
    "fe80::/10",
)]


def parse_ip(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_loopback(host: str | None) -> bool:
    ip = parse_ip(host)
    return bool(ip and ip.is_loopback)


def is_lan(host: str | None) -> bool:
    ip = parse_ip(host)
    if ip is None:
        return False
    return any(ip in net for net in _LAN_NETS if net.version == ip.version)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> float:
    return time.time()


class DeviceStore:
    """Paired devices. Only the SHA-256 of each token is ever written to disk."""

    TOUCH_EVERY = 60

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._devices: list[dict[str, Any]] = []
        self._dirty_at = 0.0
        if path.exists():
            try:
                self._devices = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                self._devices = []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._devices, indent=2), "utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def public(dev: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in dev.items() if k != "hash"}

    def create(self, name: str, ip: str | None) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(32)
        dev = {
            "id": uuid.uuid4().hex[:12],
            "name": name.strip()[:48] or "Phone",
            "hash": hash_token(token),
            "created": _now(),
            "last_seen": _now(),
            "last_ip": ip,
        }
        with self._lock:
            self._devices.append(dev)
            self._save()
        return self.public(dev), token

    def verify(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        digest = hash_token(token)
        found = None
        with self._lock:
            for dev in self._devices:
                # keep looping so timing doesn't depend on position
                if hmac.compare_digest(digest, dev["hash"]):
                    found = dev
        return self.public(found) if found else None

    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            for dev in self._devices:
                if dev["id"] == device_id:
                    return self.public(dev)
        return None

    def touch(self, device_id: str, ip: str | None) -> None:
        with self._lock:
            for dev in self._devices:
                if dev["id"] == device_id:
                    dev["last_seen"] = _now()
                    dev["last_ip"] = ip
            if _now() - self._dirty_at > self.TOUCH_EVERY:
                self._dirty_at = _now()
                self._save()

    def rename(self, device_id: str, name: str) -> bool:
        with self._lock:
            for dev in self._devices:
                if dev["id"] == device_id:
                    dev["name"] = name.strip()[:48] or dev["name"]
                    self._save()
                    return True
        return False

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            before = len(self._devices)
            self._devices = [d for d in self._devices if d["id"] != device_id]
            if len(self._devices) != before:
                self._save()
                return True
        return False

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._save()  # flush pending last_seen updates
            return [self.public(d) for d in self._devices]


class RateLimiter:
    def __init__(self, max_failures: int = 5, window: float = 60, block_for: float = 300,
                 clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.window = window
        self.block_for = block_for
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> float:
        """Seconds left on a block, 0 when the IP is free to try."""
        with self._lock:
            until = self._blocked.get(ip)
            if until is None:
                return 0
            left = until - self.clock()
            if left <= 0:
                del self._blocked[ip]
                return 0
            return left

    def fail(self, ip: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._fails.setdefault(ip, deque())
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max_failures:
                self._blocked[ip] = now + self.block_for
                q.clear()
                return True
        return False

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


class Pairing:
    """One pairing window at a time: a URL secret plus a 6-digit code, both single use."""

    TTL = 120

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._session: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            self._session = {
                "secret": secrets.token_urlsafe(24),
                "code": f"{secrets.randbelow(1_000_000):06d}",
                "expires": self.clock() + self.TTL,
                "used": False,
            }
            return dict(self._session)

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            s = self._session
            if not s or s["used"] or s["expires"] < self.clock():
                return None
            return dict(s)

    def cancel(self) -> None:
        with self._lock:
            self._session = None

    def consume(self, secret: str | None = None, code: str | None = None) -> str:
        """Returns 'ok', 'expired' or 'invalid'. Any match burns the session."""
        with self._lock:
            s = self._session
            if not s or s["used"]:
                return "invalid"
            matched = False
            if secret:
                matched = hmac.compare_digest(secret, s["secret"])
            elif code:
                matched = hmac.compare_digest(code.strip(), s["code"])
            if not matched:
                return "invalid"
            s["used"] = True
            if s["expires"] < self.clock():
                return "expired"
            return "ok"


class LinkSigner:
    """Short-lived signed URLs so <img>/<video>/<a download> work without exposing the token."""

    def __init__(self, key: bytes | None = None):
        self.key = key or secrets.token_bytes(32)

    def sign(self, device_id: str, kind: str, data: dict[str, Any], ttl: int = 3600) -> str:
        body = json.dumps({"d": device_id, "k": kind, "x": int(_now()) + ttl, **data},
                          separators=(",", ":")).encode()
        mac = hmac.new(self.key, body, hashlib.sha256).digest()[:18]
        return _b64(body) + "." + _b64(mac)

    def verify(self, token: str, kind: str) -> dict[str, Any] | None:
        try:
            body_s, mac_s = token.split(".", 1)
            body = _unb64(body_s)
            mac = _unb64(mac_s)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.key, body, hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(mac, expected):
            return None
        data = json.loads(body)
        if data.get("k") != kind or data.get("x", 0) < _now():
            return None
        return data


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_pin(pin: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return {"salt": salt.hex(), "hash": digest.hex(), "n": n, "r": r, "p": p}


def check_pin(pin: str, stored: dict[str, Any] | None) -> bool:
    if not stored or not pin:
        return False
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(stored["salt"]),
                            n=stored["n"], r=stored["r"], p=stored["p"], dklen=32)
    return hmac.compare_digest(digest.hex(), stored["hash"])


class Audit:
    def __init__(self, path: Path):
        self.path = path
        self.log_ = logging.getLogger(f"landeck.audit.{id(self)}")
        self.log_.propagate = False
        self.log_.setLevel(logging.INFO)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.log_.addHandler(handler)
        self._handler = handler

    def __call__(self, action: str, target: Any = "", result: str = "ok",
                 device: dict[str, Any] | None = None, ip: str | None = None, **extra: Any) -> None:
        entry = {
            "ts": round(_now(), 3),
            "device": device.get("name") if device else None,
            "device_id": device.get("id") if device else None,
            "ip": ip,
            "action": action,
            "target": str(target)[:500],
            "result": result,
        }
        if extra:
            entry["extra"] = extra
        self.log_.info(json.dumps(entry, ensure_ascii=False))

    def tail(self, limit: int = 500) -> list[dict[str, Any]]:
        self._handler.flush()
        files = [self.path] + [self.path.with_name(f"{self.path.name}.{i}") for i in range(1, 4)]
        out: list[dict[str, Any]] = []
        for f in files:
            if not f.exists():
                continue
            lines = f.read_text("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
                if len(out) >= limit:
                    return out
        return out

    def close(self) -> None:
        self.log_.removeHandler(self._handler)
        self._handler.close()
