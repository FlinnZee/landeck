"""Turn exe/lnk icons into small PNGs for the phone UI."""
from __future__ import annotations

import ctypes
import io
import logging
from ctypes import wintypes

log = logging.getLogger("landeck.icons")


def _icon_handle(path: str, size: int) -> int | None:
    hicon = wintypes.HICON()
    icon_id = wintypes.UINT()
    try:
        got = ctypes.windll.user32.PrivateExtractIconsW(
            path, 0, size, size, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
    except (AttributeError, OSError):
        got = 0
    if got and hicon.value:
        return hicon.value
    try:
        from win32com.shell import shell, shellcon

        flags = shellcon.SHGFI_ICON | shellcon.SHGFI_LARGEICON
        ret, info = shell.SHGetFileInfo(path, 0, flags)
        if ret and info[0]:
            return int(info[0])
    except Exception:
        pass
    return None


def _render(hicon: int, size: int):
    import win32con
    import win32gui
    import win32ui
    from PIL import Image, ImageChops

    def draw(bg: int):
        screen = win32gui.GetDC(0)
        dc = win32ui.CreateDCFromHandle(screen)
        mem = dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(dc, size, size)
        old = mem.SelectObject(bmp)
        brush = win32gui.CreateSolidBrush(bg)
        win32gui.FillRect(mem.GetSafeHdc(), (0, 0, size, size), brush)
        win32gui.DeleteObject(brush)
        win32gui.DrawIconEx(mem.GetSafeHdc(), 0, 0, hicon, size, size, 0, None, win32con.DI_NORMAL)
        bits = bmp.GetBitmapBits(True)
        mem.SelectObject(old)
        img = Image.frombuffer("RGBA", (size, size), bits, "raw", "BGRA", 0, 1).convert("RGB")
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        dc.DeleteDC()
        win32gui.ReleaseDC(0, screen)
        return img

    # draw on black and on white, the difference gives us alpha for any icon format
    on_black = draw(0x000000)
    on_white = draw(0xFFFFFF)
    r, g, b = ImageChops.subtract(on_white, on_black).split()
    alpha = ImageChops.invert(ImageChops.lighter(ImageChops.lighter(r, g), b))
    out = on_black.convert("RGBA")
    out.putalpha(alpha)
    # un-premultiply the colour that was drawn over black
    px = out.load()
    for y in range(size):
        for x in range(size):
            cr, cg, cb, a = px[x, y]
            if 0 < a < 255:
                px[x, y] = (min(255, cr * 255 // a), min(255, cg * 255 // a), min(255, cb * 255 // a), a)
    return out


def icon_png(path: str, size: int = 64) -> bytes | None:
    hicon = _icon_handle(path, size)
    if not hicon:
        return None
    try:
        img = _render(hicon, size)
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        log.debug("icon render failed for %s", path, exc_info=True)
        return None
    finally:
        ctypes.windll.user32.DestroyIcon(wintypes.HICON(hicon))


def file_description(exe: str) -> str | None:
    try:
        import win32api

        langs = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")
        if not langs:
            return None
        lang, cp = langs[0]
        desc = win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription")
        return str(desc).strip() or None
    except Exception:
        return None
