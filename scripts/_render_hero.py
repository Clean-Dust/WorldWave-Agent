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
    crest_lines = []  # remember top wave ridges for foam
    for phase, alpha, amp in [(0, 55, 20), (1.3, 40, 14), (2.5, 28, 10)]:
        pts = []
        ridge = []
        for x in range(0, w + 8, 8):
            yy = h - 70 + amp * math.sin(x / 100 + phase) + phase * 8
            pts.append((x, yy))
            ridge.append((x, yy))
        pts += [(w, h), (0, h)]
        draw.polygon(pts, fill=(*WAVE, alpha))
        crest_lines.append(ridge)

    # White sea foam along wave crests — broken / fragmented whitecaps
    foam = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fd = ImageDraw.Draw(foam)
    for i, ridge in enumerate(crest_lines):
        # broken crest dashes (not a continuous ribbon)
        if len(ridge) >= 4:
            for j in range(0, len(ridge) - 1, 3):
                # skip some segments for broken look
                if (j + i) % 5 == 0:
                    continue
                x0, y0 = ridge[j]
                x1, y1 = ridge[min(j + 2, len(ridge) - 1)]
                fd.line(
                    [(x0, y0), (x1, y1)],
                    fill=(255, 255, 255, 100 - i * 18),
                    width=1 + (j % 2),
                )
        # dense flecks / spray clusters along ridge
        for j in range(0, len(ridge), 2):
            x, yy = ridge[j]
            # local crest preference + some random-ish scatter via hash
            hsh = (x * 73856093 + int(yy) * 19349663 + i * 83492791) & 0xFFFFFFFF
            if hsh % 3 == 0:
                continue
            # cluster of tiny white dots (broken foam)
            n_dots = 2 + (hsh % 4)
            for k in range(n_dots):
                h2 = (hsh + k * 2654435761) & 0xFFFFFFFF
                ox = (h2 % 11) - 5
                oy = ((h2 >> 4) % 7) - 2
                rr = 1 + ((h2 >> 8) % 3)
                alpha = 70 + ((h2 >> 12) % 90)
                fd.ellipse(
                    [x + ox - rr, yy + oy - rr, x + ox + rr, yy + oy + rr],
                    fill=(255, 255, 255, min(200, alpha)),
                )
            # occasional larger clump
            if hsh % 7 == 0:
                fd.ellipse(
                    [x - 4, yy - 2, x + 6, yy + 3],
                    fill=(255, 255, 255, 55),
                )
            # spray up
            if hsh % 5 == 0:
                for s in range(3):
                    fd.point(
                        (x + s * 2 - 2, yy - 4 - s * 2),
                        fill=(255, 255, 255, 120),
                    )
        # secondary broken foam band slightly below crest
        for j in range(1, len(ridge), 4):
            x, yy = ridge[j]
            hsh = (x * 97 + i * 13) & 255
            if hsh % 2:
                fd.ellipse(
                    [x - 3, yy + 3, x + 5, yy + 7],
                    fill=(255, 255, 255, 40 + (hsh % 40)),
                )
    # light blur so flecks feel soft, still fragmented
    foam = foam.filter(ImageFilter.GaussianBlur(0.6))
    base = Image.alpha_composite(base, foam)

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


def draw_text_outlined(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fnt,
    fill,
    outline=(0, 0, 0, 255),
    stroke: int = 1,
) -> None:
    """Draw text with a tight black outline hugging the glyph."""
    x, y = xy
    # 8-neighborhood offsets for a crisp, tight stroke (no soft blur)
    if stroke <= 0:
        draw.text((x, y), text, font=fnt, fill=fill)
        return
    offs = []
    for dx in range(-stroke, stroke + 1):
        for dy in range(-stroke, stroke + 1):
            if dx == 0 and dy == 0:
                continue
            # ring only (tight hull), skip far corners when stroke>1 for slightly cleaner edge
            if max(abs(dx), abs(dy)) == stroke or (abs(dx) + abs(dy) <= stroke + (stroke > 1)):
                offs.append((dx, dy))
    # denser fill of stroke disk for solid outline
    offs = []
    for dx in range(-stroke, stroke + 1):
        for dy in range(-stroke, stroke + 1):
            if dx * dx + dy * dy <= stroke * stroke + stroke:  # slight bias so 1px is full 3x3-ish
                if dx or dy:
                    offs.append((dx, dy))
    for dx, dy in offs:
        draw.text((x + dx, y + dy), text, font=fnt, fill=outline)
    draw.text((x, y), text, font=fnt, fill=fill)


def render_title(
    text: str, max_w: int, max_h: int, stroke: int = 2
) -> tuple[Image.Image, int, int, int]:
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

    stroke = max(1, int(stroke))
    pad = int(8 + stroke * 2)
    layer = Image.new("RGBA", (best_tw + pad * 2, best_th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = ImageDraw.Draw(Image.new("RGBA", (8, 8))).textbbox((0, 0), text, font=best_f)
    ox, oy = int(pad - bb[0]), int(pad - bb[1])
    draw_text_outlined(
        d,
        (ox, oy),
        text,
        best_f,
        fill=TITLE_FILL,
        outline=(0, 0, 0, 255),
        stroke=stroke,
    )
    return layer, best_tw, best_th, pad


def outline_rgba(img: Image.Image, stroke: int = 3, color=(0, 0, 0, 255)) -> Image.Image:
    """Add a tight opaque outline around a transparent RGBA sprite (by dilating alpha)."""
    if stroke <= 0:
        return img
    img = img.convert("RGBA")
    pad = stroke + 1
    big = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    big.paste(img, (pad, pad), img)
    alpha = big.split()[-1]
    out_a = alpha
    for _ in range(stroke):
        out_a = out_a.filter(ImageFilter.MaxFilter(3))
    solid = Image.new("RGBA", big.size, color)
    solid.putalpha(out_a)
    canvas = Image.alpha_composite(solid, big)
    return canvas


def compose(w: int, h: int, out_path: Path) -> None:
    bg = make_bg(w, h)
    draw = ImageDraw.Draw(bg)

    # Shared vertical center so mascot + type sit on one horizontal band
    band_cy = int(h * 0.48)

    # Shared base outline; shark slightly thinner than type
    target_h = int(h * 0.70)
    outline_stroke = max(2, min(4, target_h // 90))
    shark_stroke = max(1, outline_stroke - 2)  # thinner mascot outline

    # Shark left — raised + black outline
    sh = shark_src.copy()
    ratio = target_h / sh.height
    sh = sh.resize((max(1, int(sh.width * ratio)), target_h), Image.Resampling.LANCZOS)
    sh = outline_rgba(sh, stroke=shark_stroke, color=(0, 0, 0, 255))
    sx = int(w * 0.03)
    sy = band_cy - sh.height // 2
    sy = max(int(h * 0.04), min(sy, h - sh.height - int(h * 0.06)))
    bg.paste(sh, (sx, sy), sh)
    shark_right = sx + sh.width

    # WORLDWAVE + promos — same outline_stroke as shark
    margin_r = int(w * 0.04)
    text_left = shark_right + int(w * 0.03)
    max_title_w = w - text_left - margin_r
    max_title_h = int(h * 0.28)
    title_layer, tw, th, pad = render_title(
        "WORLDWAVE", max_title_w, max_title_h, stroke=outline_stroke
    )
    col_w = max_title_w
    title_x = text_left + (col_w - title_layer.width) // 2

    lines = [
        "Persistent memory",
        "Persistent autonomy",
        "Persistent session",
    ]
    sub_size = max(24, int(h * 0.070))
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
    text_block_h = title_layer.height + title_to_sub + sub_block_h
    title_y = band_cy - text_block_h // 2
    title_y += int(h * 0.05)
    title_y = max(int(h * 0.06), min(title_y, h - text_block_h - int(h * 0.04)))

    bg.paste(title_layer, (title_x, title_y), title_layer)

    glyph_left = title_x + (title_layer.width - tw) // 2
    glyph_right = glyph_left + tw
    glyph_cx = (glyph_left + glyph_right) // 2
    glyph_bottom = title_y + (title_layer.height + th) // 2

    y = glyph_bottom + title_to_sub
    for line in lines:
        lw, _ = text_size(sub_font, line)
        x = glyph_cx - lw // 2
        draw_text_outlined(
            draw,
            (x, y),
            line,
            sub_font,
            fill=SUB_FILL,
            outline=(0, 0, 0, 255),
            stroke=outline_stroke,
        )
        y += lh + gap

    out = bg.convert("RGB")
    out.save(out_path, "PNG", optimize=True)
    print(
        f"wrote {out_path} type_stroke={outline_stroke} shark_stroke={shark_stroke} "
        f"shark_y={sy} title_y={title_y} band_cy={band_cy} "
        f"shark_mid={sy + sh.height//2} title_mid={title_y + text_block_h//2}"
    )


if __name__ == "__main__":
    # fix return type unpack
    compose(1280, 560, assets / "banner.png")
    compose(1280, 640, assets / "social-preview.png")
    Image.open(assets / "banner.png").save("/tmp/ww-banner-preview.png")
    print("ok")
