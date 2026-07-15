#!/usr/bin/env python3
"""Render Worldwave README hero — annotation zones + rules.

Zones (from user pen strokes on 1280x560 mock):
  PURPLE shark: 7.5%-34.9% x, 17.1%-87.3% y
  RED    title: 39.5%-95.3% x, 19.3%-67.1% y
  GREEN  sub:   under title; three lines stacked

Rules:
  - Shark / WORLDWAVE sized to their boxes with UNIFORM scale (no squash)
  - Three promo lines stacked; EACH line letter-spaced to WORLDWAVE width
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
SUB_FILL = (240, 248, 255, 255)
BG_TOP = (0, 21, 73)
WAVE = (15, 77, 131)

SHARK = dict(l=0.0750, r=0.3492, t=0.1714, b=0.8732)
TITLE = dict(l=0.3953, r=0.9531, t=0.1929, b=0.6714)
# Green zone top from annotation; left/width follow title
SUB_T = 0.7321
SUB_B = 0.9464


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
    gx = int(w * (SHARK["l"] + SHARK["r"]) / 2)
    gy = int(h * (SHARK["t"] + SHARK["b"]) / 2)
    for rad, a in [(200, 28), (120, 40), (70, 50)]:
        gd.ellipse([gx - rad, gy - rad, gx + rad, gy + rad], fill=(30, 95, 155, a))
    tx = int(w * (TITLE["l"] + TITLE["r"]) / 2)
    ty = int(h * (TITLE["t"] + TITLE["b"]) / 2)
    for rad, a in [(180, 16), (110, 22)]:
        gd.ellipse([tx - rad, ty - rad, tx + rad, ty + rad], fill=(20, 130, 200, a))
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, glow)


def fit_uniform(img: Image.Image, box_w: int, box_h: int, fill: float = 0.98) -> Image.Image:
    """Uniform scale to fit inside box (contain), using fill fraction of box."""
    scale = min(box_w / img.width, box_h / img.height) * fill
    nw = max(1, int(img.width * scale))
    nh = max(1, int(img.height * scale))
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def render_title_uniform(text: str, box_w: int, box_h: int) -> tuple[Image.Image, int, int, int]:
    """
    Render WORLDWAVE with uniform scale to fill the red box as much as possible.
    Prefer filling WIDTH of red box (user wants big title), height may be less than box.
    Returns (layer, content_w, content_h, pad).
    """
    # Binary search font size so natural width ≈ box_w (uniform, no Y squash)
    lo, hi = 20, 400
    best = font(BOLD, 40)
    best_tw = best_th = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        f = font(BOLD, mid)
        tw, th = text_size(f, text)
        if tw <= box_w and th <= box_h:
            best, best_tw, best_th = f, tw, th
            lo = mid + 1
        else:
            hi = mid - 1

    pad = 18
    layer = Image.new("RGBA", (best_tw + pad * 2, best_th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=best)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    glow_r = 5
    for r in range(glow_r, 0, -1):
        a = int(18 + (glow_r - r) * 10)
        for dx, dy in [(-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, r)]:
            d.text((ox + dx, oy + dy), text, font=best, fill=(*TITLE_GLOW, min(255, a)))
    d.text((ox, oy), text, font=best, fill=TITLE_FILL)

    # If still smaller than box, uniform upscale to max fit
    scale = min(box_w / layer.width, box_h / layer.height)
    if scale > 1.01:
        nw = max(1, int(layer.width * scale))
        nh = max(1, int(layer.height * scale))
        layer = layer.resize((nw, nh), Image.Resampling.LANCZOS)
        # content scales same (pad included in layer; content ≈ layer minus pad*scale)
        content_w = int(best_tw * scale)
        content_h = int(best_th * scale)
    else:
        content_w, content_h = best_tw, best_th
    return layer, content_w, content_h, pad


def draw_text_exact_width(draw, xy, text, fnt, target_w, fill):
    """Letter-space glyphs so total width == target_w (WORLDWAVE rule)."""
    x, y = xy
    if not text:
        return
    widths = [max(text_size(fnt, ch)[0], 1) for ch in text]
    natural = sum(widths)
    if natural <= 0:
        return
    if len(text) == 1:
        draw.text((x, y), text, font=fnt, fill=fill)
        return
    if natural >= target_w:
        # shrink tracking: still draw natural (overflow slightly ok) or scale font externally
        draw.text((x, y), text, font=fnt, fill=fill)
        return
    gaps = len(text) - 1
    leftover = target_w - natural
    base, rem = leftover // gaps, leftover % gaps
    cursor = x
    for i, ch in enumerate(text):
        draw.text((cursor, y), ch, font=fnt, fill=fill)
        cursor += widths[i]
        if i < gaps:
            cursor += base + (1 if i < rem else 0)


def compose(w: int, h: int, out_path: Path) -> None:
    bg = make_bg(w, h)

    # ── PURPLE: shark — uniform fit into annotation box, centered ──
    sx0, sx1 = int(w * SHARK["l"]), int(w * SHARK["r"])
    sy0, sy1 = int(h * SHARK["t"]), int(h * SHARK["b"])
    sw, sh = sx1 - sx0, sy1 - sy0
    sprite = fit_uniform(shark_src, sw, sh, fill=0.98)
    nw, nh = sprite.size
    px = sx0 + (sw - nw) // 2
    py = sy0 + (sh - nh) // 2
    bg.paste(sprite, (px, py), sprite)

    draw = ImageDraw.Draw(bg)

    # ── RED: WORLDWAVE — uniform fit into red box, centered ──
    tx0, tx1 = int(w * TITLE["l"]), int(w * TITLE["r"])
    ty0, ty1 = int(h * TITLE["t"]), int(h * TITLE["b"])
    tw, th = tx1 - tx0, ty1 - ty0
    title_layer, content_w, content_h, pad = render_title_uniform("WORLDWAVE", tw, th)
    # Center title layer in red box
    tpx = tx0 + (tw - title_layer.width) // 2
    tpy = ty0 + (th - title_layer.height) // 2
    bg.paste(title_layer, (tpx, tpy), title_layer)
    # WORLDWAVE left edge of glyphs (approx): layer left + pad scaled
    # After possible upscale, pad in layer coords:
    scale_pad = title_layer.width / max(1, content_w + 2 * pad) if content_w else 1
    # Simpler: content is centered in layer; glyph left = tpx + (layer_w - content_w)//2
    title_glyph_left = tpx + (title_layer.width - content_w) // 2
    title_glyph_bottom = tpy + (title_layer.height + content_h) // 2

    # ── GREEN / under title: 3 lines stacked, EACH width == WORLDWAVE ──
    lines = [
        "Persistent memory",
        "Persistent autonomy",
        "Persistent session",
    ]
    target_w = content_w  # same length rule as WORLDWAVE
    gx0 = title_glyph_left
    # vertical band from annotation green top/bottom
    gy0, gy1 = int(h * SUB_T), int(h * SUB_B)
    # if title bottom is lower, start below title with gap
    gy0 = max(gy0, title_glyph_bottom + int(h * 0.025))
    gh = max(40, gy1 - gy0)

    n = len(lines)
    gap_ratio = 0.28
    size = max(14, int(gh / (n + (n - 1) * gap_ratio) * 0.90))
    while size >= 10:
        f = font(REG, size)
        # natural widths must be < target_w so we can letter-space out
        max_lw = max(text_size(f, line)[0] for line in lines)
        lh = text_size(f, "Hg")[1]
        gap = max(3, int(lh * gap_ratio))
        total_h = n * lh + (n - 1) * gap
        if max_lw <= target_w and total_h <= gh:
            break
        size -= 1
    sub_font = font(REG, size)
    lh = text_size(sub_font, "Hg")[1]
    gap = max(3, int(lh * gap_ratio))
    total_h = n * lh + (n - 1) * gap
    y = gy0 + max(0, (gh - total_h) // 2)
    for line in lines:
        draw_text_exact_width(draw, (gx0, y), line, sub_font, target_w, SUB_FILL)
        y += lh + gap

    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path}\n"
        f"  shark box {sw}x{sh} sprite {nw}x{nh} @({px},{py})\n"
        f"  title box {tw}x{th} layer {title_layer.size} content_w={content_w}\n"
        f"  sub x3 each width={target_w} font={size} @x={gx0}"
    )


if __name__ == "__main__":
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
