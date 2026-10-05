#!/usr/bin/env python3
"""
Image rendering for the Zade Meadows monitor.

Two jobs:
  * render_profile_card()  - the Instagram mini frame: round avatar, handle,
    verified rosette, posts / followers / following, and small state pills.
  * animated_logo()        - your logo with a soft light sweeping across it,
    as a looping GIF for the embed thumbnail.

The logo artwork itself is never redrawn or recoloured. It is loaded from
assets/logo.png exactly as supplied; the animation is a highlight layered on
top and thrown away afterwards.

Everything renders offline. If Pillow is missing, the bot still runs and simply
posts embeds without pictures.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Iterable, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - the bot degrades to text-only embeds
    PIL_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
LOGO_FILE = ASSETS_DIR / "logo.png"

# --------------------------------------------------------------------------
# Palette - Instagram's dark mode, tuned to sit beside the bot's house style.
# --------------------------------------------------------------------------

BG = (13, 15, 16)
CARD_EDGE = (32, 35, 38)
TEXT = (245, 246, 247)
MUTED = (145, 152, 160)
DIM = (96, 103, 110)
IG_BLUE = (0, 149, 246)
GREEN = (87, 201, 139)
AMBER = (226, 176, 74)
RED = (224, 115, 107)
SLATE = (138, 148, 166)

CARD_W, CARD_H = 880, 412

# Windows first - that is where this bot actually runs - then Linux, so the
# same file renders identically on a laptop and on a VPS.
_BOLD_FONTS = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_REGULAR_FONTS = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def _font(candidates: Iterable[str], size: int):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    # No system font found. Pillow >= 10.1 ships a scalable default; older
    # versions only have a tiny bitmap font, which has no .size and cannot
    # anchor text - _size() and _centred_text() below cope with both.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _size(font, fallback: int = 11) -> int:
    return int(getattr(font, "size", fallback))


def bold(size: int):
    return _font(_BOLD_FONTS, size)


def regular(size: int):
    return _font(_REGULAR_FONTS, size)


def human_count(value: Optional[int]) -> str:
    """12 345 678 -> 12.3M, the way Instagram shortens it."""
    if value is None:
        return "—"
    value = int(value)
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        trimmed = value / 1_000
        return f"{trimmed:.1f}K".replace(".0K", "K")
    trimmed = value / 1_000_000
    return f"{trimmed:.1f}M".replace(".0M", "M")


# --------------------------------------------------------------------------
# Small drawing helpers
# --------------------------------------------------------------------------

def _circle_mask(size: int, supersample: int = 4) -> "Image.Image":
    """Antialiased circle: draw big, shrink down."""
    big = Image.new("L", (size * supersample, size * supersample), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * supersample - 1, size * supersample - 1), fill=255)
    return big.resize((size, size), Image.LANCZOS)


def _avatar(avatar_bytes: Optional[bytes], size: int) -> "Image.Image":
    """Round avatar, or a quiet placeholder disc when we have no picture."""
    canvas = Image.new("RGB", (size, size), (26, 29, 32))
    if avatar_bytes:
        try:
            source = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
            shortest = min(source.size)
            left = (source.width - shortest) // 2
            top = (source.height - shortest) // 2
            source = source.crop((left, top, left + shortest, top + shortest))
            canvas = source.resize((size, size), Image.LANCZOS)
        except Exception:
            pass
    else:
        draw = ImageDraw.Draw(canvas)
        glyph = bold(int(size * 0.42))
        try:
            draw.text((size / 2, size / 2), "?", font=glyph, fill=(70, 77, 84), anchor="mm")
        except ValueError:  # bitmap fallback font: no anchors
            draw.text((size / 2, size / 2), "?", font=glyph, fill=(70, 77, 84))

    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(canvas, (0, 0), _circle_mask(size))
    return rounded


def _verified_badge(image: "Image.Image", cx: int, cy: int, radius: int,
                    colour=IG_BLUE) -> None:
    """Instagram's scalloped blue tick, drawn rather than shipped as an asset."""
    scale = 4
    size = radius * 2 * scale
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    points = []
    lobes = 11
    for step in range(lobes * 2):
        angle = math.pi * step / lobes
        reach = radius * scale if step % 2 == 0 else radius * scale * 0.84
        points.append((size / 2 + reach * math.cos(angle),
                       size / 2 + reach * math.sin(angle)))
    draw.polygon(points, fill=colour + (255,))

    tick = radius * scale
    draw.line(
        [(size / 2 - tick * 0.40, size / 2 + tick * 0.02),
         (size / 2 - tick * 0.10, size / 2 + tick * 0.32),
         (size / 2 + tick * 0.42, size / 2 - tick * 0.32)],
        fill=(255, 255, 255, 255), width=max(2, int(tick * 0.20)), joint="curve",
    )

    layer = layer.resize((radius * 2, radius * 2), Image.LANCZOS)
    image.paste(layer, (cx - radius, cy - radius), layer)


def _pill(draw: "ImageDraw.ImageDraw", x: int, y: int, label: str,
          colour, font) -> int:
    """Small outlined tag. Returns the x where the next pill can start."""
    pad_x, pad_y = 16, 9
    width = int(draw.textlength(label, font=font))
    height = _size(font) + pad_y * 2
    box = (x, y, x + width + pad_x * 2, y + height)
    draw.rounded_rectangle(box, radius=height // 2, fill=(colour[0] // 7 + 14,
                                                          colour[1] // 7 + 15,
                                                          colour[2] // 7 + 16),
                           outline=colour, width=2)
    draw.text((x + pad_x, y + pad_y - 1), label, font=font, fill=colour)
    return box[2] + 10


def _wrap(draw: "ImageDraw.ImageDraw", text: str, font, max_width: int,
          max_lines: int = 2) -> list[str]:
    """Word-wrap to a pixel width. Long status notes used to run off the edge
    of the card, which looked broken rather than informative."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines:
        while lines and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        consumed = sum(len(line.split()) for line in lines)
        if consumed < len(words):
            lines[-1] = lines[-1].rstrip(" ,-") + "…"
    return lines


# --------------------------------------------------------------------------
# The Instagram mini frame
# --------------------------------------------------------------------------

def render_profile_card(
    username: str,
    *,
    full_name: Optional[str] = None,
    followers: Optional[int] = None,
    following: Optional[int] = None,
    posts: Optional[int] = None,
    verified: bool = False,
    private: Optional[bool] = False,
    avatar_bytes: Optional[bytes] = None,
    state: str = "ok",          # ok | gone | unknown
    note: Optional[str] = None,
) -> Optional[bytes]:
    """Draw one account as a PNG. Returns None if Pillow is unavailable."""
    if not PIL_AVAILABLE:
        return None

    card = Image.new("RGB", (CARD_W, CARD_H), BG)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((2, 2, CARD_W - 3, CARD_H - 3), radius=26,
                           outline=CARD_EDGE, width=3)

    accent = {"ok": GREEN, "gone": RED, "unknown": SLATE}.get(state, SLATE)

    # avatar, with a thin ring in the state colour
    av_size, av_x, av_y = 210, 56, 74
    ring = av_size + 16
    draw.ellipse((av_x - 8, av_y - 8, av_x - 8 + ring, av_y - 8 + ring),
                 outline=accent, width=3)
    # Rendered once and used as its own mask - decoding the photo twice here
    # doubled the work on every single card for no visible difference.
    face = _avatar(avatar_bytes, av_size)
    card.paste(face, (av_x, av_y), face)

    text_x = av_x + av_size + 56

    # handle + verified tick
    handle = f"@{username}"[:32]
    handle_font = bold(46)
    draw.text((text_x, 84), handle, font=handle_font, fill=TEXT)
    if verified:
        tick_x = text_x + int(draw.textlength(handle, font=handle_font)) + 26
        _verified_badge(card, tick_x, 84 + _size(handle_font, 46) // 2 + 2, 18)

    # display name
    if full_name:
        draw.text((text_x, 146), full_name[:36], font=regular(32), fill=MUTED)

    # counts row - posts / followers / following, Instagram's order
    row_y = 214
    columns = (
        ("posts", posts),
        ("followers", followers),
        ("following", following),
    )
    col_x = text_x
    number_font, label_font = bold(40), regular(26)
    for label, value in columns:
        shown = human_count(value) if state == "ok" else "—"
        draw.text((col_x, row_y), shown, font=number_font, fill=TEXT)
        draw.text((col_x, row_y + 50), label, font=label_font, fill=DIM)
        col_x += 200

    # state pills along the bottom
    pill_font = regular(24)
    pill_x = text_x
    if state == "ok":
        if private is not None:  # None: the source did not say, so no pill
            pill_x = _pill(draw, pill_x, 300, "Private" if private else "Public",
                           AMBER if private else GREEN, pill_font)
        if verified:
            pill_x = _pill(draw, pill_x, 300, "Verified", IG_BLUE, pill_font)
    elif state == "gone":
        pill_x = _pill(draw, pill_x, 300, "Not reachable", RED, pill_font)
    else:
        pill_x = _pill(draw, pill_x, 300, "Could not check", SLATE, pill_font)

    if note:
        note_font = regular(21)
        room = CARD_W - text_x - 40
        for index, line in enumerate(_wrap(draw, note, note_font, room)):
            draw.text((text_x, 348 + index * 26), line, font=note_font, fill=DIM)

    buffer = io.BytesIO()
    card.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# The animated logo
# --------------------------------------------------------------------------

_logo_cache: Optional[bytes] = None


def animated_logo(size: int = 160, sweep_frames: int = 12,
                  hold_frames: int = 10, strength: int = 96) -> Optional[bytes]:
    """Your logo, untouched, with a light sweeping diagonally across it.

    The sweep runs once, then the logo rests for a moment before repeating -
    constant motion in a channel gets tiring fast. Result is cached, so this
    only renders on the first call after start-up, and the frames share one
    small palette so the file stays light enough to re-upload freely."""
    global _logo_cache
    if _logo_cache is not None:
        return _logo_cache
    if not PIL_AVAILABLE or not LOGO_FILE.exists():
        return None

    try:
        base = Image.open(LOGO_FILE).convert("RGB").resize((size, size), Image.LANCZOS)
    except Exception:
        return None

    white = Image.new("RGB", (size, size), (255, 255, 255))
    frames = []
    sigma = 0.11

    for step in range(sweep_frames):
        position = -0.25 + (1.5 * step / max(sweep_frames - 1, 1))
        mask = Image.new("L", (size, size))
        pixels = mask.load()
        for y in range(size):
            for x in range(size):
                # diagonal coordinate, 0 at top-left, 1 at bottom-right
                u = (x + y) / (2.0 * size)
                falloff = math.exp(-((u - position) ** 2) / (2 * sigma * sigma))
                pixels[x, y] = int(strength * falloff)
        frame = base.copy()
        frame.paste(white, (0, 0), mask)
        frames.append(frame)

    frames.extend([base.copy() for _ in range(hold_frames)])

    # One shared 64-colour palette: the logo is two flat colours plus the
    # highlight, so this is invisible to the eye but a fraction of the size.
    palette = base.quantize(colors=64, method=Image.MEDIANCUT)
    frames = [frame.quantize(palette=palette, dither=Image.NONE) for frame in frames]

    buffer = io.BytesIO()
    frames[0].save(
        buffer, format="GIF", save_all=True, append_images=frames[1:],
        duration=70, loop=0, optimize=True,
    )
    _logo_cache = buffer.getvalue()
    return _logo_cache


if __name__ == "__main__":  # quick local render for eyeballing
    out = BASE_DIR / "assets"
    out.mkdir(exist_ok=True)
    gif = animated_logo()
    if gif:
        (out / "logo_shine.gif").write_bytes(gif)
        print("logo_shine.gif", len(gif), "bytes")
    png = render_profile_card("kushina.uzk", full_name="Amardeep Banarjee",
                              posts=0, followers=0, following=2,
                              verified=True, private=True)
    if png:
        (out / "sample_card.png").write_bytes(png)
        print("sample_card.png", len(png), "bytes")
