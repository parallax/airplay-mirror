"""Render the add-on icon and logo.

    python tools/make_icons.py

Writes airplay_mirror/icon.png (square) and airplay_mirror/logo.png (wide). Everything is drawn
with Pillow at 4x and downsampled, so the result is crisp with no external assets.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1] / "airplay_mirror"
S = 4  # supersampling

BG_TOP = (30, 41, 82)
BG_BOTTOM = (17, 24, 52)
SPEAKER = (236, 239, 246)
SPEAKER_SHADOW = (18, 24, 48)
CONE = (52, 63, 104)
WAVE = (96, 165, 250)
WAVE_SOFT = (96, 165, 250, 110)


def gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return m


def draw_speaker(d: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float) -> None:
    """A cabinet with a tweeter and a woofer, drawn at (x, y) top-left with size w x h."""
    r = w * 0.18
    d.rounded_rectangle((x + w * 0.04, y + h * 0.04, x + w * 1.04, y + h * 1.04), radius=r, fill=SPEAKER_SHADOW)
    d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=SPEAKER)
    cx = x + w / 2
    tw = w * 0.22
    d.ellipse((cx - tw / 2, y + h * 0.16, cx + tw / 2, y + h * 0.16 + tw), fill=CONE)
    ww = w * 0.58
    wy = y + h * 0.42
    d.ellipse((cx - ww / 2, wy, cx + ww / 2, wy + ww), fill=CONE)
    iw = ww * 0.42
    d.ellipse((cx - iw / 2, wy + (ww - iw) / 2, cx + iw / 2, wy + (ww + iw) / 2), fill=SPEAKER)


def draw_waves(img: Image.Image, cx: float, cy: float, radii: list[float], width: float, span: int = 62) -> None:
    """Concentric arcs opening to the right and to the left of (cx, cy) - the 'mirror'."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, r in enumerate(radii):
        colour = WAVE if i == 0 else WAVE_SOFT if i == 2 else (*WAVE, 180)
        box = (cx - r, cy - r, cx + r, cy + r)
        d.arc(box, start=-span, end=span, fill=colour, width=round(width))
        d.arc(box, start=180 - span, end=180 + span, fill=colour, width=round(width))
    img.alpha_composite(layer)


def glyph(size: int) -> Image.Image:
    """The badge: rounded gradient square, two speakers, waves between them."""
    n = size * S
    img = gradient(n, BG_TOP, BG_BOTTOM).convert("RGBA")
    img.putalpha(rounded_mask(n, round(n * 0.22)))
    d = ImageDraw.Draw(img)
    sw, sh = n * 0.22, n * 0.46
    top = n * 0.29
    draw_speaker(d, n * 0.12, top, sw, sh)
    draw_speaker(d, n * 0.66, top, sw, sh)
    cx, cy = n / 2, top + sh * 0.52
    draw_waves(img, cx, cy, [n * 0.06, n * 0.11, n * 0.16], width=n * 0.022)
    d = ImageDraw.Draw(img)
    dot = n * 0.026
    d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=WAVE)
    return img.resize((size, size), Image.LANCZOS)


def font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in (
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),  # Neue Bold
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size)


def logo(width: int = 250, height: int = 100) -> Image.Image:
    """Wide logo for the add-on page: a dark pill holding the badge and the name, readable on any theme."""
    W, H = width * S, height * S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pill = gradient(W, BG_TOP, BG_BOTTOM).crop((0, 0, W, H)).convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), radius=round(H * 0.24), fill=255)
    pill.putalpha(mask)
    img.alpha_composite(pill)
    inner = height - 20
    badge = glyph(inner).resize((inner * S, inner * S), Image.LANCZOS)
    img.alpha_composite(badge, (10 * S, 10 * S))
    d = ImageDraw.Draw(img)
    f = font(round(H * 0.30))
    x = (inner + 24) * S
    d.text((x, H * 0.19), "AirPlay", font=f, fill=SPEAKER)
    d.text((x, H * 0.50), "Mirror", font=f, fill=WAVE)
    return img.resize((width, height), Image.LANCZOS)


def main() -> None:
    glyph(256).save(ROOT / "icon.png", optimize=True)
    logo().save(ROOT / "logo.png", optimize=True)
    print("wrote", ROOT / "icon.png", "and", ROOT / "logo.png")


if __name__ == "__main__":
    main()
