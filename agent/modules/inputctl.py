"""Remote mouse and keyboard through SendInput."""
from __future__ import annotations

import asyncio
import ctypes
import logging
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends

from ..api import ApiError, feature_dep

if TYPE_CHECKING:
    from ..context import Context
    from ..hub import Conn

log = logging.getLogger("landeck.input")

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

KEYS: dict[str, int] = {
    "esc": 0x1B, "tab": 0x09, "enter": 0x0D, "backspace": 0x08, "space": 0x20, "del": 0x2E, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "win": 0x5B, "ctrl": 0xA2, "alt": 0xA4, "shift": 0xA0, "altgr": 0xA5,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D, "pause": 0x13, "numlock": 0x90,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
    "browser_back": 0xA6, "browser_forward": 0xA7,
}
KEYS.update({f"f{i}": 0x6F + i for i in range(1, 25)})
KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(d): ord(str(d)) for d in range(10)})
KEYS.update({";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
             "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE})

EXTENDED = {"ins", "del", "home", "end", "pgup", "pgdn", "up", "down", "left", "right", "win", "menu",
            "printscreen", "altgr", "numlock"} | {k for k in KEYS if k.startswith(("volume_", "media_", "browser_"))}
MODIFIERS = {"ctrl", "alt", "shift", "win", "altgr"}
BLOCKED_COMBOS = [{"win", "l"}]  # locking from here leaves you stuck at a screen the agent can't touch


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def check_combo(names: list[str]) -> list[str]:
    keys = [str(n).lower() for n in names]
    for k in keys:
        if k not in KEYS:
            raise ApiError(400, "bad_key", f"Unknown key '{k}'.")
    if any(b <= set(keys) for b in BLOCKED_COMBOS):
        raise ApiError(403, "blocked_combo", "Win+L is blocked. Use Lock in the Power tab instead.")
    return keys


class SendInputBackend:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def _send(self, *inputs: INPUT) -> None:
        arr = (INPUT * len(inputs))(*inputs)
        self.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        i = INPUT(type=INPUT_MOUSE)
        i.mi = MOUSEINPUT(dx, dy, ctypes.c_uint32(data).value, flags, 0, 0)
        return i

    def _key(self, vk: int, up: bool, extended: bool) -> INPUT:
        scan = self.user32.MapVirtualKeyW(vk, 0)
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        i = INPUT(type=INPUT_KEYBOARD)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def move(self, dx: int, dy: int) -> None:
        # Position absolutely from the current cursor so Windows' own pointer acceleration
        # doesn't stack on top of the phone's curve. Relative input is the fallback.
        pt = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(pt)):
            self._send(self._mouse(MOUSEEVENTF_MOVE, dx, dy))
            return
        sm = self.user32.GetSystemMetrics
        vx, vy, vw, vh = sm(76), sm(77), sm(78), sm(79)
        x = min(max(pt.x + dx, vx), vx + vw - 1)
        y = min(max(pt.y + dy, vy), vy + vh - 1)
        self.move_abs(x, y)

    def move_abs(self, x: int, y: int) -> None:
        sm = self.user32.GetSystemMetrics
        vx, vy, vw, vh = sm(76), sm(77), sm(78), sm(79)
        nx = int((x - vx) * 65535 / max(vw - 1, 1))
        ny = int((y - vy) * 65535 / max(vh - 1, 1))
        self._send(self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))

    def button(self, btn: str, down: bool) -> None:
        d, u = BUTTONS[btn]
        self._send(self._mouse(d if down else u))

    def scroll(self, dx: int, dy: int) -> None:
        events = []
        if dy:
            events.append(self._mouse(MOUSEEVENTF_WHEEL, data=dy))
        if dx:
            events.append(self._mouse(MOUSEEVENTF_HWHEEL, data=dx))
        if events:
            self._send(*events)

    def key(self, name: str, down: bool) -> None:
        self._send(self._key(KEYS[name], not down, name in EXTENDED))

    def text(self, text: str) -> None:
        events: list[INPUT] = []
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                events += [self._key(0x0D, False, False), self._key(0x0D, True, False)]
                continue
            raw = ch.encode("utf-16-le")
            for k in range(0, len(raw), 2):
                unit = int.from_bytes(raw[k:k + 2], "little")
                for up in (False, True):
                    i = INPUT(type=INPUT_KEYBOARD)
                    i.ki = KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
                    events.append(i)
        for k in range(0, len(events), 200):
            self._send(*events[k:k + 200])


class DryBackend:
    def __init__(self) -> None:
        self.log: list[tuple] = []

    def move(self, dx, dy):
        self.log.append(("move", dx, dy))

    def move_abs(self, x, y):
        self.log.append(("move_abs", x, y))

    def button(self, btn, down):
        self.log.append(("button", btn, down))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))

    def key(self, name, down):
        self.log.append(("key", name, down))

    def text(self, text):
        self.log.append(("text", text))


class InputService:
    def __init__(self, backend: Any):
        self.b = backend
        self.held: dict[int, dict[str, set[str]]] = {}
        self.carry: dict[int, list[float]] = {}

    def _held(self, owner: int) -> dict[str, set[str]]:
        return self.held.setdefault(owner, {"buttons": set(), "keys": set()})

    def apply(self, events: list[Any], owner: int = 0) -> int:
        """Run a batch of compact events. Returns how many were applied."""
        held = self._held(owner)
        done = 0
        for ev in events[:500]:
            if not isinstance(ev, list) or not ev:
                continue
            op, args = ev[0], ev[1:]
            try:
                if op == "m":
                    cx, cy = self.carry.get(owner, [0.0, 0.0])
                    fx, fy = float(args[0]) + cx, float(args[1]) + cy
                    ix, iy = int(fx), int(fy)
                    self.carry[owner] = [fx - ix, fy - iy]
                    if ix or iy:
                        self.b.move(max(-4000, min(4000, ix)), max(-4000, min(4000, iy)))
                elif op == "a":
                    self.b.move_abs(int(args[0]), int(args[1]))
                elif op == "b":
                    btn, act = str(args[0]), str(args[1] if len(args) > 1 else "click")
                    if btn not in BUTTONS:
                        continue
                    if act == "down":
                        self.b.button(btn, True)
                        held["buttons"].add(btn)
                    elif act == "up":
                        self.b.button(btn, False)
                        held["buttons"].discard(btn)
                    else:
                        for _ in range(2 if act == "dbl" else 1):
                            self.b.button(btn, True)
                            self.b.button(btn, False)
                elif op == "s":
                    self.b.scroll(int(max(-2400, min(2400, args[0]))), int(max(-2400, min(2400, args[1]))))
                elif op == "k":
                    name, act = str(args[0]).lower(), str(args[1] if len(args) > 1 else "press")
                    if name not in KEYS:
                        continue
                    if act == "down":
                        if name == "l" and "win" in held["keys"]:
                            continue
                        self.b.key(name, True)
                        held["keys"].add(name)
                    elif act == "up":
                        self.b.key(name, False)
                        held["keys"].discard(name)
                    else:
                        if name == "l" and "win" in held["keys"]:
                            continue
                        self.b.key(name, True)
                        self.b.key(name, False)
                elif op == "c":
                    self.combo(list(args[0]))
                elif op == "t":
                    text = str(args[0])[:5000]
                    if text:
                        self.b.text(text)
                else:
                    continue
                done += 1
            except ApiError:
                raise
            except (IndexError, TypeError, ValueError):
                continue
        return done

    def combo(self, names: list[str]) -> None:
        keys = check_combo(names)
        mods = [k for k in keys if k in MODIFIERS]
        rest = [k for k in keys if k not in MODIFIERS]
        for k in mods:
            self.b.key(k, True)
        for k in rest:
            self.b.key(k, True)
            self.b.key(k, False)
        for k in reversed(mods):
            self.b.key(k, False)

    def release(self, owner: int) -> None:
        held = self.held.pop(owner, None)
        self.carry.pop(owner, None)
        if not held:
            return
        for btn in held["buttons"]:
            self.b.button(btn, False)
        for key in held["keys"]:
            self.b.key(key, False)


def build(ctx: "Context") -> APIRouter:
    svc = InputService(DryBackend() if ctx.dry_run else SendInputBackend())
    ctx.services["input"] = svc
    gate = feature_dep(ctx, "input")

    @ctx.ws("input")
    async def ws_input(conn: "Conn", msg: dict[str, Any]) -> None:
        if not ctx.config.feature("input"):
            raise ApiError(403, "feature_disabled", "Remote input is turned off on the PC.")
        events = msg.get("ev")
        if isinstance(events, list):
            await asyncio.to_thread(svc.apply, events, id(conn))

    async def on_close(conn: "Conn") -> None:
        # synchronous on purpose: this runs in a finally block that may be getting cancelled
        svc.release(id(conn))

    ctx.disconnect_hooks.append(on_close)

    r = APIRouter()

    @r.post("/input")
    async def rest_input(body: dict[str, Any] = Body(...), dev: dict = Depends(gate)):
        events = body.get("ev")
        if not isinstance(events, list):
            raise ApiError(400, "bad_events", "Send {\"ev\": [[op, ...], ...]}.")
        n = await asyncio.to_thread(svc.apply, events, hash(dev["id"]))
        await asyncio.to_thread(svc.release, hash(dev["id"]))
        return {"applied": n}

    @r.get("/input/keys")
    async def keys(dev: dict = Depends(gate)):
        return {"keys": sorted(KEYS), "blocked": [sorted(b) for b in BLOCKED_COMBOS]}

    return r
