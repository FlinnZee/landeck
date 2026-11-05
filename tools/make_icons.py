"""Draws the LanDeck icons with Pillow: tray/exe icon, PWA icons, apple-touch-icon.

Run from the repo root:  python tools/make_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BG = (11, 15, 21, 255)
TILE = (19, 26, 36, 255)
INK = (230, 237, 243, 255)
ACCENT = (34, 211, 238, 255)

S = 1024  # draw big, scale down


def mark(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float]) -> None:
    """Three bars, the middle one shorter and lit."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    bar = h * 0.15
    gap = h * 0.13
    top = y0 + (h - (bar * 3 + gap * 2)) / 2
    for i, (frac, colour) in enumerate(((1.0, INK), (0.7, ACCENT), (1.0, INK))):
        y = top + i * (bar + gap)
        draw.rounded_rectangle((x0, y, x0 + w * frac, y + bar), radius=bar / 2, fill=colour)


def glow_layer(size: int, box: tuple[float, float, float, float]) -> Image.Image:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = box
    h = y1 - y0
    bar = h * 0.15
    gap = h * 0.13
    top = y0 + (h - (bar * 3 + gap * 2)) / 2 + bar + gap
    d.rounded_rectangle((x0, top, x0 + (x1 - x0) * 0.7, top + bar), radius=bar / 2, fill=(*ACCENT[:3], 170))
    return layer.filter(ImageFilter.GaussianBlur(size * 0.03))


def tile_icon(transparent_corners: bool = True, inset: float = 0.0) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0) if transparent_corners else BG)
    d = ImageDraw.Draw(img)
    pad = S * inset
    if transparent_corners:
        d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=S * 0.23, fill=TILE,
                            outline=(*ACCENT[:3], 105), width=int(S * 0.011))
    inner = S * (0.27 + inset * 0.6)
    box = (inner, inner, S - inner, S - inner)
    img.alpha_composite(glow_layer(S, box))
    mark(ImageDraw.Draw(img), box)
    return img


def full_bleed(scale: float = 0.46) -> Image.Image:
    img = Image.new("RGBA", (S, S), BG)
    d = ImageDraw.Draw(img)
    # subtle accent edge so it reads as a tile on the home screen
    d.rounded_rectangle((S * 0.06, S * 0.06, S * 0.94, S * 0.94), radius=S * 0.2, fill=TILE)
    m = S * (1 - scale) / 2
    box = (m, m, S - m, S - m)
    img.alpha_composite(glow_layer(S, box))
    mark(ImageDraw.Draw(img), box)
    return img


def save(img: Image.Image, path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.resize((size, size), Image.LANCZOS).save(path)
    print(f"  {path.relative_to(ROOT)}")


def main() -> int:
    print("Generating icons:")
    tile = tile_icon()
    save(tile, ROOT / "assets" / "icon.png", 256)
    ico = ROOT / "assets" / "icon.ico"
    sizes = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]
    tile.resize((256, 256), Image.LANCZOS).save(ico, sizes=sizes)
    print(f"  {ico.relative_to(ROOT)}")
    save(tile, ROOT / "web" / "icons" / "icon-192.png", 192)
    save(tile, ROOT / "web" / "icons" / "icon-512.png", 512)
    save(full_bleed(0.42), ROOT / "web" / "icons" / "icon-512-maskable.png", 512)
    save(full_bleed(0.5), ROOT / "web" / "icons" / "apple-touch-icon.png", 180)
    return 0


if __name__ == "__main__":
    sys.exit(main())
