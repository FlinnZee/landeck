"""System tray icon and menu."""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from . import startup

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("landeck.tray")

ACCENT = (34, 211, 238)
VIEWING = (251, 113, 133)


def make_icon(size: int = 64, accent: tuple[int, int, int] = ACCENT, dot: bool = False) -> Image.Image:
    """Rounded tile with a stylised deck: three bars, the middle one lit."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.24), fill=(13, 17, 23, 255),
                        outline=(*accent, 255), width=max(1, s // 28))
    pad = s * 0.24
    bar_h = s * 0.1
    gap = s * 0.08
    top = s / 2 - bar_h * 1.5 - gap
    widths = (0.52, 0.38, 0.52)
    for i, w in enumerate(widths):
        y = top + i * (bar_h + gap)
        colour = (*accent, 255) if i == 1 else (230, 237, 243, 255)
        d.rounded_rectangle((pad, y, pad + (s - 2 * pad) * (w / 0.52), y + bar_h), radius=bar_h / 2, fill=colour)
    if dot:
        r = s * 0.17
        d.ellipse((s - r * 2 - s * 0.04, s * 0.04, s - s * 0.04, s * 0.04 + r * 2), fill=(*VIEWING, 255),
                  outline=(13, 17, 23, 255), width=max(1, s // 32))
    return img


class Tray:
    def __init__(self, ctx: "Context", admin_url: str, on_quit: Callable[[], None],
                 on_restart: Callable[[], None]):
        self.ctx = ctx
        self.admin_url = admin_url
        self.on_quit = on_quit
        self.on_restart = on_restart
        self.icon = None
        self._normal = make_icon()
        self._viewing = make_icon(accent=VIEWING, dot=True)

    def _status(self, _item=None) -> str:
        n = len(self.ctx.hub.online_devices())
        viewing = " - screen viewed" if self.ctx.services.get("screen") and self.ctx.services["screen"].streams else ""
        return f"Port {self.ctx.port} - {n} device{'s' if n != 1 else ''} online{viewing}"

    def _autostart(self, _item=None) -> bool:
        return bool(self.ctx.config.get("autostart"))

    def _toggle_autostart(self, icon, item) -> None:
        def work():
            try:
                if self.ctx.config.get("autostart"):
                    startup.remove_task()
                    self.ctx.config.set("autostart", False)
                else:
                    startup.install_task()
                    startup.install_firewall(self.ctx.port)
                    self.ctx.config.set("autostart", True)
            except Exception as exc:
                log.error("startup toggle failed: %s", exc)
                self.notify("Couldn't change startup", str(exc))
            if self.icon:
                self.icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    def open_admin(self, fragment: str = "") -> None:
        webbrowser.open(self.admin_url + (f"#{fragment}" if fragment else ""))

    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception:
            pass

    def set_viewing(self, viewing: bool) -> None:
        if self.icon:
            self.icon.icon = self._viewing if viewing else self._normal
            self.icon.title = "LanDeck - screen is being viewed" if viewing else "LanDeck"
            self.icon.update_menu()

    def refresh(self) -> None:
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def run(self) -> None:
        import pystray
        from pystray import Menu
        from pystray import MenuItem as Item

        menu = Menu(
            Item(self._status, None, enabled=False),
            Menu.SEPARATOR,
            Item("Open admin", lambda: self.open_admin(), default=True),
            Item("Pair new device", lambda: self.open_admin("pair")),
            Item("Show QR", lambda: self.open_admin("pair")),
            Menu.SEPARATOR,
            Item("Start with Windows", self._toggle_autostart, checked=self._autostart),
            Item("Open config folder", lambda: os.startfile(str(self.ctx.home))),  # type: ignore[attr-defined]
            Item("View audit log", lambda: self.open_admin("audit")),
            Menu.SEPARATOR,
            Item("Restart agent", lambda: self.on_restart()),
            Item("Quit", lambda: self.on_quit()),
        )
        self.icon = pystray.Icon("LanDeck", self._normal, "LanDeck", menu)
        self.ctx.hub.on_change(self.refresh)
        self.icon.run()

    def stop(self) -> None:
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
