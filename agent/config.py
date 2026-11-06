from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import __version__

APP_NAME = "LanDeck"
VERSION = __version__
DEFAULT_PORT = 8765

# terminal and remote action editing stay off until someone turns them on at the PC
FEATURES: dict[str, bool] = {
    "dashboard": True,
    "power": True,
    "clipboard": True,
    "files": True,
    "media": True,
    "input": True,
    "screen": True,
    "processes": True,
    "actions": True,
    "notify": True,
    "ram": True,
    "terminal": False,
    "remote_action_editing": False,
}

DEFAULTS: dict[str, Any] = {
    "port": DEFAULT_PORT,
    "name": "",
    "features": FEATURES,
    "roots": [],
    "default_roots": True,
    "show_drives": True,
    "upload_dir": "",
    "clipboard_persist": False,
    "screen_toast": True,
    "autostart": True,
    "terminal_pin": None,
}

CREATE_NO_WINDOW = 0x08000000


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Where web/ and assets/ live. PyInstaller unpacks bundled data to _MEIPASS."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("LANDECK_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def dry_run() -> bool:
    return os.environ.get("LANDECK_DRY_RUN") == "1"


def hostname() -> str:
    return socket.gethostname()


def lock_down(folder: Path) -> None:
    """Strip inherited ACLs so only the current user and SYSTEM can read the data dir."""
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        sid_str = win32security.ConvertSidToStringSid(sid)
    except Exception:
        return
    subprocess.run(
        ["icacls", str(folder), "/inheritance:r",
         "/grant:r", f"*{sid_str}:(OI)(CI)F",
         "/grant:r", "*S-1-5-18:(OI)(CI)F"],
        capture_output=True, creationflags=CREATE_NO_WINDOW, check=False,
    )


class Config:
    def __init__(self, folder: Path):
        self.folder = folder
        self.path = folder / "config.json"
        self._lock = threading.RLock()
        self.data: dict[str, Any] = deepcopy(DEFAULTS)
        self.data["name"] = hostname()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            # keep the broken file around for inspection, start fresh
            self.path.replace(self.path.with_suffix(".bad.json"))
            self.save()
            return
        features = {**FEATURES, **stored.get("features", {})}
        self.data.update(stored)
        self.data["features"] = {k: bool(v) for k, v in features.items() if k in FEATURES}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self.data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def feature(self, name: str) -> bool:
        return bool(self.data["features"].get(name, False))

    def features(self) -> dict[str, bool]:
        return dict(self.data["features"])

    def set_feature(self, name: str, enabled: bool) -> None:
        if name not in FEATURES:
            raise KeyError(name)
        with self._lock:
            self.data["features"][name] = bool(enabled)
            self.save()

    @property
    def port(self) -> int:
        return int(self.data.get("port") or DEFAULT_PORT)

    @property
    def name(self) -> str:
        return self.data.get("name") or hostname()
