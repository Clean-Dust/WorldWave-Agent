#!/usr/bin/env python3
"""Render Worldwave README hero — sizes locked to user's annotation circles.

Measured from img_c03b2e573d40.jpg (1280x560):
  PURPLE shark: x=96-447 (7.5%-34.9%)  y=96-489 (17.1%-87.3%)  ~351x393
  RED    title: x=506-1220 (39.5%-95.3%) y=108-376 (19.3%-67.1%)  ~714x268
  GREEN  sub:   x=530-1194 (41.4%-93.3%) y=410-530 (73.2%-94.6%)  ~664x120
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

# Annotation fractions (from pen strokes)
SHARK = dict(l=0.0750, r=0.3492, t=0.1714, b=0.8732)
TITLE = dict(l=0.3953, r=0.9531, t=0.1929, b=0.6714)
SUB = dict(l=0.4141, r=0.9336, t=0.7321, b=0.9464)


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
    # glow under shark zone + title zone
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


def render_title_to_box(text: str, box_w: int, box_h: int) -> Image.Image:
    """Render WORLDWAVE so final bitmap fills box_w x box_h (may non-uniform scale)."""
    # Find large bold size that fits width at natural aspect, then scale to exact box
    size = 200
    while size >= 20:
        f = font(BOLD, size)
        tw, th = text_size(f, text)
        if tw <= box_w * 1.05:  # slightly over ok before scale
            break
        size -= 2
    f = font(BOLD, size)
    tw, th = text_size(f, text)
    pad = 20
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=f)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    glow_r = 5
    for r in range(glow_r, 0, -1):
        a = int(18 + (glow_r - r) * 10)
        for dx, dy in [(-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, r)]:
            d.text((ox + dx, oy + dy), text, font=f, fill=(*TITLE_GLOW, min(255, a)))
    d.text((ox, oy), text, font=f, fill=TITLE_FILL)
    # Scale to exact annotation box (hits BOTH width and height of red circle)
    return layer.resize((max(1, box_w), max(1, box_h)), Image.Resampling.LANCZOS)


def draw_text_exact_width(draw, xy, text, fnt, target_w, fill):
    x, y = xy
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


def fit_sub_font(text: str, target_w: int, max_size: int, min_size: int = 14):
    for size in range(max_size, min_size - 1, -1):
        f = font(REG, size)
        if text_size(f, text)[0] <= target_w:
            return f
    return font(REG, min_size)


def compose(w: int, h: int, out_path: Path) -> None:
    bg = make_bg(w, h)

    # ── PURPLE: shark fills annotation box ──
    sx0, sx1 = int(w * SHARK["l"]), int(w * SHARK["r"])
    sy0, sy1 = int(h * SHARK["t"]), int(h * SHARK["b"])
    sw, sh = sx1 - sx0, sy1 - sy0
    # fit shark into box preserving aspect, centered
    src = shark_src.copy()
    scale = min(sw / src.width, sh / src.height)
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    src = src.resize((nw, nh), Image.Resampling.LANCZOS)
    # if still short of box height, allow slight vertical fill (user wants circle size)
    # prefer filling the box more aggressively: scale to cover min dimension then center-crop? 
    # User said hit circle SIZE — use contain then optional upscale to cover 95% of box
    cover = 0.95
    scale2 = min((sw * cover) / src.width, (sh * cover) / max(src.height, 1))
    # recompute from original for quality
    src = shark_src.copy()
    scale = min(sw / src.width, sh / src.height) * 0.98
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    src = src.resize((nw, nh), Image.Resampling.LANCZOS)
    px = sx0 + (sw - nw) // 2
    py = sy0 + (sh - nh) // 2
    bg.paste(src, (px, py), src)

    draw = ImageDraw.Draw(bg)

    # ── RED: WORLDWAVE fills annotation box exactly ──
    tx0, tx1 = int(w * TITLE["l"]), int(w * TITLE["r"])
    ty0, ty1 = int(h * TITLE["t"]), int(h * TITLE["b"])
    tw, th = tx1 - tx0, ty1 - ty0
    title_img = render_title_to_box("WORLDWAVE", tw, th)
    bg.paste(title_img, (tx0, ty0), title_img)

    # ── GREEN: promo inside green bar, width = green width ──
    gx0, gx1 = int(w * SUB["l"]), int(w * SUB["r"])
    gy0, gy1 = int(h * SUB["t"]), int(h * SUB["b"])
    gw, gh = gx1 - gx0, gy1 - gy0
    sub = "Persistent memory · Persistent autonomy · Persistent session"
    # font height ~ 45% of green box height
    sub_max = max(14, int(gh * 0.55))
    sub_font = fit_sub_font(sub, gw, max_size=sub_max, min_size=12)
    _, sh_h = text_size(sub_font, sub)
    sty = gy0 + max(0, (gh - sh_h) // 2)
    draw_text_exact_width(draw, (gx0, sty), sub, sub_font, gw, SUB_FILL)

    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path}\n"
        f"  shark box {sw}x{sh} @({sx0},{sy0}) sprite {nw}x{nh}\n"
        f"  title box {tw}x{th} @({tx0},{ty0})\n"
        f"  sub   box {gw}x{gh} @({gx0},{gy0}) font~{sub_max}"
    )


if __name__ == "__main__":
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
