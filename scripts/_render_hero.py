#!/usr/bin/env python3
"""Render Worldwave README hero banner + social preview.

Layout zones (user annotated):
  LEFT   — small blue shark (mascot)
  RIGHT  — WORLDWAVE title (bold cyan, vertical stretch only)
  BELOW  — promo line, letter-spaced to equal WORLDWAVE width

Title must NOT invade the shark column.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

ROOT = Path(__file__).resolve().parents[1]
assets = ROOT / "docs" / "assets"
shark = Image.open(assets / "shark-mascot.png").convert("RGBA")
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
if not Path(BOLD).exists():
    BOLD = "/mnt/c/Windows/Fonts/arialbd.ttf"
if not Path(REG).exists():
    REG = "/mnt/c/Windows/Fonts/arial.ttf"

# Approved soft cyan palette
TITLE_FILL = (49, 209, 247, 255)
TITLE_GLOW = (25, 150, 210)
SUB_FILL = (240, 248, 255, 255)
BG_TOP = (0, 21, 73)
WAVE = (15, 77, 131)
V_STRETCH = 1.20  # 只上下拉长，不整体放大


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
    gx, gy = int(w * 0.18), int(h * 0.58)
    for r, a in [(160, 26), (100, 38), (60, 48)]:
        gd.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(30, 95, 155, a))
    for r, a in [(140, 14), (90, 20)]:
        gd.ellipse(
            [int(w * 0.58) - r, int(h * 0.36) - r, int(w * 0.58) + r, int(h * 0.36) + r],
            fill=(20, 130, 200, a),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, glow)


def render_title_layer(text: str, fnt, v_stretch: float = 1.20) -> tuple[Image.Image, int, int]:
    tw, th = text_size(fnt, text)
    pad = 14
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=fnt)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    glow_r = 4
    for r in range(glow_r, 0, -1):
        a = int(16 + (glow_r - r) * 9)
        for dx, dy in [(-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, r), (-r, r), (r, -r)]:
            d.text((ox + dx, oy + dy), text, font=fnt, fill=(*TITLE_GLOW, min(255, a)))
    d.text((ox, oy), text, font=fnt, fill=TITLE_FILL)
    content_w, content_h = tw, th
    if abs(v_stretch - 1.0) > 1e-3:
        layer = layer.resize(
            (layer.width, max(1, int(round(layer.height * v_stretch)))),
            Image.Resampling.LANCZOS,
        )
        content_h = max(1, int(round(th * v_stretch)))
    return layer, content_w, content_h


def fit_sub_font(text: str, target_w: int, max_size: int, min_size: int = 12):
    best = font(REG, min_size)
    for size in range(max_size, min_size - 1, -1):
        f = font(REG, size)
        w, _ = text_size(f, text)
        if w <= target_w:
            return f
    return best


def draw_text_exact_width(draw, xy, text, fnt, target_w, fill):
    x, y = xy
    if not text:
        return
    widths = [max(text_size(fnt, ch)[0], 1) for ch in text]
    natural = sum(widths)
    if natural <= 0:
        return
    if len(text) == 1 or natural >= target_w:
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

    # ── PURPLE ZONE: shark left column ──
    sh = shark.copy()
    target_h = int(h * 0.72)
    ratio = target_h / sh.height
    sh = sh.resize((max(1, int(sh.width * ratio)), target_h), Image.Resampling.LANCZOS)
    sx = int(w * 0.03)
    sy = h - sh.height - int(h * 0.015)
    bg.paste(sh, (sx, sy), sh)
    shark_right = sx + sh.width

    draw = ImageDraw.Draw(bg)
    title = "WORLDWAVE"
    sub = "Persistent memory · Persistent autonomy · Persistent session"
    margin_r = 36
    pad = 14

    # ── RED ZONE: WORLDWAVE to the RIGHT of shark (clear gap) ──
    text_left = shark_right + int(w * 0.025)
    max_w = w - text_left - margin_r

    # Base size fit to red zone width only; vertical stretch later (no overall scale-up)
    size = int(h * 0.28)
    while size >= 40:
        f = font(BOLD, size)
        tw, th = text_size(f, title)
        if tw <= max_w and th <= int(h * 0.36):
            break
        size -= 2
    title_font = font(BOLD, size)
    natural_w, natural_h = text_size(title_font, title)

    title_layer, content_w, content_h = render_title_layer(title, title_font, v_stretch=V_STRETCH)
    tx = text_left - pad
    ty = int(h * 0.22)
    # keep title block in upper/mid red zone
    if ty + title_layer.height > int(h * 0.72):
        ty = max(8, int(h * 0.72) - title_layer.height)
    bg.paste(title_layer, (tx, ty), title_layer)

    # ── GREEN ZONE: promo under WORLDWAVE, same width as title ──
    sub_font = fit_sub_font(sub, content_w, max_size=max(12, int(h * 0.046)), min_size=11)
    natural_sw, _ = text_size(sub_font, sub)
    sty = ty + pad + content_h + int(h * 0.04)
    draw_text_exact_width(draw, (text_left, sty), sub, sub_font, content_w, SUB_FILL)

    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path} zones: shark=[{sx}-{shark_right}] "
        f"title_left={text_left}({text_left/w:.0%}) size={size} "
        f"natural=({natural_w}x{natural_h}) v_h={content_h} sub_nat={natural_sw}"
    )


if __name__ == "__main__":
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
