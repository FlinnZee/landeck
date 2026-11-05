"""Bits of Win32 that don't belong to a single feature module."""
from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

log = logging.getLogger("landeck.native")

user32 = ctypes.WinDLL("user32", use_last_error=True)

MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_DEFBUTTON2 = 0x100
MB_SYSTEMMODAL = 0x1000
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6

WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_CLIPBOARDUPDATE = 0x031D
WM_DISPLAYCHANGE = 0x007E
WM_CLOSE = 0x0010
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012


def set_dpi_awareness() -> None:
    # per-monitor v2 so screen grabs and SendInput agree on physical pixels
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def ask_yes_no(title: str, text: str, timeout_s: int = 60) -> bool:
    """Topmost Yes/No box on the PC. "No" is the default button and also what a timeout means."""
    flags = MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
    try:
        fn = user32.MessageBoxTimeoutW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
                       wintypes.WORD, wintypes.DWORD]
        fn.restype = ctypes.c_int
        res = fn(None, text, title, flags, 0, timeout_s * 1000)
    except AttributeError:
        res = user32.MessageBoxW(None, text, title, flags)
    return res == IDYES


def window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


class MessageWindow:
    """Hidden top-level window that receives clipboard, power and session messages.

    A message-only window would be lighter, but those never see broadcasts such
    as WM_POWERBROADCAST, so this is a real (never shown) window.
    """

    def __init__(self) -> None:
        self.on_clipboard: Callable[[], None] | None = None
        self.on_resume: Callable[[], None] | None = None
        self.on_suspend: Callable[[], None] | None = None
        self.on_session_end: Callable[[], None] | None = None
        self.on_display_change: Callable[[], None] | None = None
        self.clipboard_listener = False
        self.hwnd = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="msg-window", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self.hwnd:
            import win32gui
            try:
                win32gui.PostMessage(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass

    def _fire(self, fn: Callable[[], None] | None) -> None:
        if fn is None:
            return
        try:
            fn()
        except Exception:
            log.exception("message window callback failed")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        import win32gui

        if msg == WM_CLIPBOARDUPDATE:
            self._fire(self.on_clipboard)
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self._fire(self.on_resume)
            elif wparam == PBT_APMSUSPEND:
                self._fire(self.on_suspend)
            return 1
        if msg == WM_QUERYENDSESSION:
            return 1
        if msg == WM_ENDSESSION:
            if wparam:
                self._fire(self.on_session_end)
            return 0
        if msg == WM_DISPLAYCHANGE:
            self._fire(self.on_display_change)
            return 0
        if msg == WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        try:
            import win32api
            import win32gui

            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.lpszClassName = "LanDeckMessageWindow"
            wc.hInstance = win32api.GetModuleHandle(None)
            try:
                atom = win32gui.RegisterClass(wc)
            except win32gui.error:
                atom = wc.lpszClassName
            self.hwnd = win32gui.CreateWindow(atom, "LanDeck", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
            self.clipboard_listener = bool(user32.AddClipboardFormatListener(self.hwnd))
        except Exception:
            log.exception("could not create message window")
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()
        if self.clipboard_listener:
            user32.RemoveClipboardFormatListener(self.hwnd)
