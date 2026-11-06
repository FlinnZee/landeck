from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import config as cfg
from .discovery import Advertiser, lan_ips
from .hub import Conn, Hub
from .security import Audit, DeviceStore, LinkSigner, Pairing, RateLimiter
from .tls import CertManager

log = logging.getLogger("landeck")

WsHandler = Callable[[Conn, dict[str, Any]], Awaitable[None]]


class Context:
    """Everything the server and the feature modules share."""

    def __init__(self, home: Path | None = None, dry_run: bool | None = None, dev: bool = False,
                 confirm_device: Callable[[str, str | None], bool] | None = None,
                 discover: bool = True):
        self.home = home or cfg.data_dir()
        first_run = not self.home.exists()
        self.home.mkdir(parents=True, exist_ok=True)
        if first_run and home is None:
            cfg.lock_down(self.home)

        self.dry_run = cfg.dry_run() if dry_run is None else dry_run
        self.dev = dev
        self.version = cfg.VERSION
        self.hostname = cfg.hostname()
        self.started = time.time()
        self.config = cfg.Config(self.home)
        self.devices = DeviceStore(self.home / "devices.json")
        self.audit = Audit(self.home / "audit.jsonl")
        self.limiter = RateLimiter()
        self.pin_limiter = RateLimiter(max_failures=5, window=300, block_for=900)
        self.pairing = Pairing()
        self.signer = LinkSigner()
        self.hub = Hub()
        self.certs = CertManager(self.home / "certs", self.hostname)
        self.port = self.config.port
        self.ips: list[str] = lan_ips() if discover else []
        self.advertiser = Advertiser(self.config.name, self.port, self.version)
        self.ssl_context = None
        self.ws_handlers: dict[str, WsHandler] = {}
        self.disconnect_hooks: list[Callable[[Conn], Awaitable[None]]] = []
        self.subscribe_hooks: dict[str, Callable[[Conn], Awaitable[None]]] = {}
        self.services: dict[str, Any] = {}
        self.background: list[Callable[[], Awaitable[None]]] = []
        self.shutdown_hooks: list[Callable[[], None]] = []
        self.confirm_device = confirm_device or self._native_confirm
        self.request_restart: Callable[[], None] | None = None
        self.on_viewing_changed: Callable[[bool], None] | None = None

    # --- helpers used all over -------------------------------------------

    @staticmethod
    def _native_confirm(name: str, ip: str | None) -> bool:
        from .native import ask_yes_no

        return ask_yes_no(
            "LanDeck - new device",
            f"Allow \"{name}\" ({ip or 'unknown address'}) to control this PC?\n\n"
            "Only say Yes if you just scanned the pairing code yourself.",
        )

    def ws(self, msg_type: str) -> Callable[[WsHandler], WsHandler]:
        def deco(fn: WsHandler) -> WsHandler:
            self.ws_handlers[msg_type] = fn
            return fn
        return deco

    def trusted_hosts(self) -> set[str]:
        sans = self.certs.sans()
        hosts = {h.lower() for h in sans["names"] + sans["ips"]}
        hosts.update(self.ips)
        hosts.update({"localhost", "127.0.0.1", "landeck.local"})
        return hosts

    def origin_ok(self, origin: str | None) -> bool:
        if not origin:
            return True
        from urllib.parse import urlsplit

        from .security import is_lan

        try:
            parts = urlsplit(origin)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return False
        if parts.scheme != "https" or port != self.port:
            return False
        host = (parts.hostname or "").lower()
        # a known name/IP, or any private-LAN address on our port: the latter keeps a phone
        # that was added to the Home Screen on one IP working after the PC's IP changes.
        return host in self.trusted_hosts() or is_lan(host)

    def server_info(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "hostname": self.hostname,
            "version": self.version,
            "port": self.port,
            "ips": self.ips,
            "dry_run": self.dry_run,
        }

    def refresh_network(self) -> bool:
        """Re-read LAN addresses, re-issue the cert and re-announce if they moved."""
        ips = lan_ips()
        if ips == self.ips and self.certs.cert_path.exists():
            return False
        old = self.ips
        self.ips = ips
        reissued = self.certs.ensure(ips)
        if reissued and self.ssl_context is not None:
            try:
                self.ssl_context.load_cert_chain(str(self.certs.cert_path), str(self.certs.key_path))
            except Exception:
                log.exception("could not reload certificate")
        if ips != old:
            log.info("LAN addresses changed: %s -> %s", old, ips)
            self.advertiser.update(ips)
            self.hub.publish("system.net", {"ips": ips})
        return True
