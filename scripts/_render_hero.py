#!/usr/bin/env python3
"""Render Worldwave README hero banner + social preview.

Color reference: user-approved soft cyan title (~#2ED5FB, sampled 46,213,251)
on deep navy (bg ~#00103D). Bold DejaVu + light glow. Optional v-stretch.
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

# Sampled from approved banner (img_2de0cd4db89f)
TITLE_FILL = (46, 213, 251, 255)  # soft electric cyan
TITLE_GLOW = (30, 170, 230)  # softer glow than neon white-cyan
SUB_FILL = (235, 245, 255, 255)
BG_TOP = (0, 16, 61)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def make_bg(w: int, h: int) -> Image.Image:
    # Match approved deep navy → mid blue (no over-boosted cyan haze)
    img = Image.new("RGB", (w, h), BG_TOP)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for x in range(w):
            # top (0,16,61) → lower (8, 40, 90)
            r = int(0 + t * 12)
            g = int(16 + t * 28)
            b = int(61 + t * 40)
            cx = abs(x - w / 2) / (w / 2)
            r = max(0, min(255, int(r * (1 - 0.10 * cx))))
            g = max(0, min(255, int(g * (1 - 0.08 * cx))))
            b = max(0, min(255, int(b * (1 - 0.05 * cx))))
            # soft pool left for shark
            lx = max(0, 1 - abs(x - w * 0.20) / (w * 0.40))
            g = min(255, int(g + 14 * lx * (1 - t * 0.35)))
            b = min(255, int(b + 28 * lx * (1 - t * 0.25)))
            px[x, y] = (r, g, b)
    base = img.convert("RGBA")
    draw = ImageDraw.Draw(base, "RGBA")
    # waves ~ sampled (15, 79, 134)
    for phase, alpha, amp in [(0, 55, 20), (1.3, 40, 14), (2.5, 28, 10)]:
        pts = []
        for x in range(0, w + 8, 8):
            yy = h - 70 + amp * math.sin(x / 100 + phase) + phase * 8
            pts.append((x, yy))
        pts += [(w, h), (0, h)]
        draw.polygon(pts, fill=(15, 79, 134, alpha))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gx, gy = int(w * 0.20), int(h * 0.55)
    for r, a in [(180, 28), (110, 40), (70, 50)]:
        gd.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(30, 100, 160, a))
    # subtle title-zone glow (not neon wash)
    for r, a in [(160, 18), (100, 24)]:
        gd.ellipse(
            [int(w * 0.52) - r, int(h * 0.34) - r, int(w * 0.52) + r, int(h * 0.34) + r],
            fill=(20, 140, 210, a),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    return Image.alpha_composite(base, glow)


def render_title_layer(text: str, fnt, v_stretch: float = 1.20) -> Image.Image:
    """Render title with soft glow, then stretch vertically."""
    dummy = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    bb = dummy.textbbox((0, 0), text, font=fnt)
    pad = 14
    tw, th = int(bb[2] - bb[0]), int(bb[3] - bb[1])
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    # softer multi-pass glow (approved look, not blown-out white cyan)
    glow_r = 4
    for r in range(glow_r, 0, -1):
        a = int(18 + (glow_r - r) * 10)
        for dx, dy in [(-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, r), (-r, r), (r, -r)]:
            d.text((ox + dx, oy + dy), text, font=fnt, fill=(*TITLE_GLOW, min(255, a)))
    d.text((ox, oy), text, font=fnt, fill=TITLE_FILL)
    if abs(v_stretch - 1.0) > 1e-3:
        nw, nh = layer.width, max(1, int(round(layer.height * v_stretch)))
        layer = layer.resize((nw, nh), Image.Resampling.LANCZOS)
    return layer


def compose(w: int, h: int, out_path: Path) -> None:
    bg = make_bg(w, h)
    sh = shark.copy()
    target_h = int(h * 0.66)
    ratio = target_h / sh.height
    sh = sh.resize((max(1, int(sh.width * ratio)), target_h), Image.Resampling.LANCZOS)
    sx = int(w * 0.02)
    sy = h - sh.height - int(h * 0.03)
    bg.paste(sh, (sx, sy), sh)

    draw = ImageDraw.Draw(bg)
    title = "WORLDWAVE"
    margin_r = 28
    V_STRETCH = 1.20
    base_left = max(int(w * 0.36), sx + sh.width - 10)
    text_left = max(sx + sh.width + int(w * 0.01), int(base_left - 0.10 * w))
    max_w = w - text_left - margin_r

    size = int(h * 0.28)
    dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    while size >= 40:
        f = font(BOLD, size)
        bb = dummy.textbbox((0, 0), title, font=f)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        if tw <= max_w and int(th * V_STRETCH) <= int(h * 0.52):
            break
        size -= 2
    title_font = font(BOLD, size)
    title_layer = render_title_layer(title, title_font, v_stretch=V_STRETCH)
    tw, th = title_layer.width, title_layer.height
    tx = text_left
    ty = int(h * 0.18)
    if tx + tw > w - 4:
        tx = max(0, w - 4 - tw)
    if ty + th > h - 80:
        ty = max(8, h - 80 - th)
    bg.paste(title_layer, (tx, ty), title_layer)

    sub = "Persistent memory · Persistent autonomy · Persistent session"
    sub_size = max(16, int(h * 0.048))
    while sub_size >= 12:
        sf = font(REG, sub_size)
        sw = dummy.textbbox((0, 0), sub, font=sf)[2] - dummy.textbbox((0, 0), sub, font=sf)[0]
        if sw <= max_w:
            break
        sub_size -= 1
    sub_font = font(REG, sub_size)
    sw = dummy.textbbox((0, 0), sub, font=sub_font)[2] - dummy.textbbox((0, 0), sub, font=sub_font)[0]
    sty = ty + th + int(h * 0.02)
    draw.text((text_left, sty), sub, font=sub_font, fill=SUB_FILL)

    assert text_left + sw <= w - 4, (text_left, sw, w)

    # No contrast/color boost — keep sampled palette clean
    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path} size={size} v_stretch={V_STRETCH} "
        f"title_fill=#2ED5FB title_px=({tw}x{th}) tx={tx}"
    )


if __name__ == "__main__":
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
