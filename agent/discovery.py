from __future__ import annotations

import ipaddress
import logging
import socket
import threading

import psutil

log = logging.getLogger("landeck.discovery")

SERVICE_TYPE = "_landeck._tcp.local."
MDNS_HOST = "landeck.local."

_SKIP_IFACE = ("vethernet", "virtualbox", "vmware", "loopback", "wsl", "hyper-v", "docker", "npcap")


def _is_private_v4(addr: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def primary_ip() -> str | None:
    """The address Windows would use for outbound traffic. No packet is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if _is_private_v4(ip) else None


def lan_ips() -> list[str]:
    """Private IPv4 addresses on interfaces that are up, primary first."""
    stats = psutil.net_if_stats()
    found: list[str] = []
    virtual: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        for a in addrs:
            if a.family == socket.AF_INET and _is_private_v4(a.address):
                bucket = virtual if any(k in name.lower() for k in _SKIP_IFACE) else found
                bucket.append(a.address)
    primary = primary_ip()
    ordered = ([primary] if primary else []) + found + virtual
    seen: set[str] = set()
    return [ip for ip in ordered if not (ip in seen or seen.add(ip))]


def mac_addresses() -> list[dict[str, str]]:
    out = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        if any(k in name.lower() for k in _SKIP_IFACE):
            continue
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        mac = next((a.address for a in addrs if a.family == psutil.AF_LINK), None)
        has_ip = any(a.family == socket.AF_INET and _is_private_v4(a.address) for a in addrs)
        if mac and has_ip:
            out.append({"iface": name, "mac": mac.replace("-", ":").upper()})
    return out


class Advertiser:
    """Announces the agent over mDNS as landeck.local and _landeck._tcp."""

    def __init__(self, instance: str, port: int, version: str):
        self.instance = instance
        self.port = port
        self.version = version
        self._zc = None
        self._info = None
        self._lock = threading.Lock()

    def _build_info(self, ips: list[str]):
        from zeroconf import ServiceInfo

        safe = "".join(c for c in self.instance if c.isalnum() or c in " -_") or "PC"
        return ServiceInfo(
            SERVICE_TYPE,
            f"LanDeck on {safe}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=self.port,
            properties={"version": self.version, "path": "/"},
            server=MDNS_HOST,
        )

    def start(self, ips: list[str]) -> None:
        if not ips:
            return
        try:
            from zeroconf import IPVersion, Zeroconf

            with self._lock:
                self._zc = Zeroconf(ip_version=IPVersion.V4Only)
                self._info = self._build_info(ips)
                self._zc.register_service(self._info, allow_name_change=True)
            log.info("mDNS: advertising %s on %s", MDNS_HOST, ", ".join(ips))
        except Exception as exc:  # mDNS is a convenience, never fatal
            log.warning("mDNS unavailable: %s", exc)
            self._zc = None

    def update(self, ips: list[str]) -> None:
        self.stop()
        self.start(ips)

    def stop(self) -> None:
        with self._lock:
            if self._zc is None:
                return
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._info = None

    def alive(self) -> bool:
        with self._lock:
            return self._zc is not None
