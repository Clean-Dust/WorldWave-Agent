#!/usr/bin/env python3
"""Render Worldwave README hero banner + social preview.

Layout (user direction 2026-07-16):
  - Left: fat shark
  - Right: WORLDWAVE (bold cyan)
  - Below title, CENTERED under WORLDWAVE: three promo lines stacked
  - Normal letter-spacing (NO width-match-to-title rule)
  - Tight gap under title (not low on the canvas)
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

ROOT = Path(__file__).resolve().parents[1]
assets = ROOT / "docs" / "assets"
shark_src = Image.open(assets / "shark-mascot.png").convert("RGBA")
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not Path(BOLD).exists():
    BOLD = "/mnt/c/Windows/Fonts/arialbd.ttf"
if not Path(REG).exists():
    REG = "/mnt/c/Windows/Fonts/arial.ttf"

TITLE_FILL = (49, 209, 247, 255)
TITLE_GLOW = (25, 150, 210)
SUB_FILL = (200, 230, 245, 255)
BG_TOP = (0, 21, 73)
WAVE = (15, 77, 131)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def text_size(fnt, text: str) -> tuple[int, int]:
    d = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    bb = d.textbbox((0, 0), text, font=fnt)
    return int(bb[2] - bb[0]), int(bb[3] - bb[1])


def make_bg(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_TOP)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for x in range(w):
            r = int(0 + t * 10)
            g = int(21 + t * 30)
            b = int(73 + t * 35)
            cx = abs(x - w / 2) / (w / 2)
            r = max(0, min(255, int(r * (1 - 0.10 * cx))))
            g = max(0, min(255, int(g * (1 - 0.08 * cx))))
            b = max(0, min(255, int(b * (1 - 0.05 * cx))))
            lx = max(0, 1 - abs(x - w * 0.18) / (w * 0.35))
            g = min(255, int(g + 12 * lx * (1 - t * 0.35)))
            b = min(255, int(b + 22 * lx * (1 - t * 0.25)))
            px[x, y] = (r, g, b)
    base = img.convert("RGBA")
    draw = ImageDraw.Draw(base, "RGBA")
    for phase, alpha, amp in [(0, 55, 20), (1.3, 40, 14), (2.5, 28, 10)]:
        pts = []
        for x in range(0, w + 8, 8):
            yy = h - 70 + amp * math.sin(x / 100 + phase) + phase * 8
            pts.append((x, yy))
        pts += [(w, h), (0, h)]
        draw.polygon(pts, fill=(*WAVE, alpha))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for rad, a in [(180, 30), (110, 42), (70, 52)]:
        gd.ellipse(
            [int(w * 0.18) - rad, int(h * 0.55) - rad, int(w * 0.18) + rad, int(h * 0.55) + rad],
            fill=(30, 95, 155, a),
        )
    for rad, a in [(160, 16), (100, 22)]:
        gd.ellipse(
            [int(w * 0.62) - rad, int(h * 0.32) - rad, int(w * 0.62) + rad, int(h * 0.32) + rad],
            fill=(20, 130, 200, a),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, glow)


def render_title(text: str, max_w: int, max_h: int) -> tuple[Image.Image, int, int, int]:
    """Largest bold title that fits max_w x max_h (uniform). Returns layer, content_w, content_h, pad."""
    lo, hi = 24, 360
    best_f, best_tw, best_th = font(BOLD, 48), 0, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        f = font(BOLD, mid)
        tw, th = text_size(f, text)
        if tw <= max_w and th <= max_h:
            best_f, best_tw, best_th = f, tw, th
            lo = mid + 1
        else:
            hi = mid - 1

    pad = 16
    layer = Image.new("RGBA", (best_tw + pad * 2, best_th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=best_f)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    for r in range(4, 0, -1):
        a = int(16 + (4 - r) * 10)
        for dx, dy in [(-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, r)]:
            d.text((ox + dx, oy + dy), text, font=best_f, fill=(*TITLE_GLOW, min(255, a)))
    d.text((ox, oy), text, font=best_f, fill=TITLE_FILL)
    return layer, best_tw, best_th, pad


def compose(w: int, h: int, out_path: Path) -> None:
    bg = make_bg(w, h)
    draw = ImageDraw.Draw(bg)

    # Shared vertical center so mascot + type sit on one horizontal band
    band_cy = int(h * 0.48)

    # Shark left — raised (centered on band, not glued to bottom)
    sh = shark_src.copy()
    target_h = int(h * 0.70)
    ratio = target_h / sh.height
    sh = sh.resize((max(1, int(sh.width * ratio)), target_h), Image.Resampling.LANCZOS)
    sx = int(w * 0.03)
    sy = band_cy - sh.height // 2
    sy = max(int(h * 0.04), min(sy, h - sh.height - int(h * 0.06)))
    bg.paste(sh, (sx, sy), sh)
    shark_right = sx + sh.width

    # WORLDWAVE + promos as one column, centered on same band_cy
    margin_r = int(w * 0.04)
    text_left = shark_right + int(w * 0.03)
    max_title_w = w - text_left - margin_r
    max_title_h = int(h * 0.28)
    title_layer, tw, th, pad = render_title("WORLDWAVE", max_title_w, max_title_h)
    col_w = max_title_w
    title_x = text_left + (col_w - title_layer.width) // 2

    lines = [
        "Persistent memory",
        "Persistent autonomy",
        "Persistent session",
    ]
    sub_size = max(20, int(h * 0.055))
    sub_font = font(REG, sub_size)
    while sub_size >= 12:
        sub_font = font(REG, sub_size)
        if max(text_size(sub_font, ln)[0] for ln in lines) <= col_w:
            break
        sub_size -= 1
        sub_font = font(REG, sub_size)
    lh = text_size(sub_font, "Hg")[1]
    gap = max(4, int(lh * 0.22))
    sub_block_h = 3 * lh + 2 * gap
    title_to_sub = int(h * 0.028)
    # total text column height ≈ title_layer + gap + sub block
    text_block_h = title_layer.height + title_to_sub + sub_block_h
    title_y = band_cy - text_block_h // 2
    title_y = max(int(h * 0.06), min(title_y, h - text_block_h - int(h * 0.06)))

    bg.paste(title_layer, (title_x, title_y), title_layer)

    glyph_left = title_x + (title_layer.width - tw) // 2
    glyph_right = glyph_left + tw
    glyph_cx = (glyph_left + glyph_right) // 2
    glyph_bottom = title_y + (title_layer.height + th) // 2

    y = glyph_bottom + title_to_sub
    for line in lines:
        lw, _ = text_size(sub_font, line)
        x = glyph_cx - lw // 2
        draw.text((x, y), line, font=sub_font, fill=SUB_FILL)
        y += lh + gap

    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path} shark_y={sy} title_y={title_y} band_cy={band_cy} "
        f"shark_mid={sy + sh.height//2} title_mid={title_y + text_block_h//2}"
    )


if __name__ == "__main__":
    # fix return type unpack
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
