"""core/computer_use/som.py — Set-of-Mark (SoM) numbering

Draws numbered bounding boxes on screenshots using element data from
UIAutomation (Tier 2). The numbered screenshot is sent to the vision
model, which only needs to answer "click [5]" instead of guessing
absolute pixel coordinates.

This is the key innovation from Tier 3: the model reasons about labelled
elements, not raw pixels. Accuracy jumps from ~60% to ~95%.

Pipeline:
  1. capture.screenshot() → raw image
  2. uia.extract_elements() → element list with coordinates
  3. som.annotate() → draw number boxes on image, produce lookup table
  4. Send annotated image to vision model → model replies with target_id
  5. Look up target_id in the table → get real coordinates → execute click
"""

from __future__ import annotations
import json
import os
from typing import Optional

# Will use PIL at runtime — already available on system Python


def annotate(
    screenshot_path: str,
    elements: list[dict],
    output_path: Optional[str] = None,
    font_size: int = 14,
    box_color: str = "#00FF00",
    text_bg: str = "#000000",
) -> str:
    """Draw numbered bounding boxes on a screenshot image.

    Each interactive element gets a visible number label so the vision
    model can refer to elements by ID instead of guessing coordinates.

    Args:
        screenshot_path: Path to the raw screenshot
        elements: List of elements from uia.extract_elements()
        output_path: Where to save the annotated image (default: overwrite input)
        font_size: Size of the number labels
        box_color: Hex color for bounding box outline
        text_bg: Hex color for number label background

    Returns:
        Path to the annotated image
    """
    from PIL import Image, ImageDraw, ImageFont

    if output_path is None:
        output_path = screenshot_path

    img = Image.open(screenshot_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Try to load a nice font, fall back to default
    font = None
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            pass

    for el in elements:
        el_id = el.get("id")
        x = el.get("x", 0)
        y = el.get("y", 0)
        w = el.get("width", 0)
        h = el.get("height", 0)

        if w <= 0 or h <= 0:
            continue

        # Draw bounding box
        draw.rectangle([x, y, x + w, y + h], outline=box_color, width=2)

        # Draw number label (top-left of element)
        label = str(el_id)
        if font:
            bbox = draw.textbbox((0, 0), label, font=font)
            tw = bbox[2] - bbox[0] + 6
            th = bbox[3] - bbox[1] + 4
        else:
            tw, th = len(label) * 9, 16

        # Background rectangle for text
        draw.rectangle([x, y - th - 2, x + tw, y], fill=text_bg)
        # Text
        draw.text((x + 3, y - th - 1), label, fill=box_color, font=font)

    img.save(output_path, "PNG")
    return output_path


def build_lookup(elements: list[dict]) -> dict:
    """Build ID→coordinate lookup table from element list.

    Returns:
        { id: { "x": ..., "y": ..., "width": ..., "height": ..., "label": ..., "type": ... } }
    """
    table = {}
    for el in elements:
        eid = el.get("id")
        if eid is not None:
            table[str(eid)] = {
                "x": int(el.get("x", 0)),
                "y": int(el.get("y", 0)),
                "width": int(el.get("width", 0)),
                "height": int(el.get("height", 0)),
                "label": el.get("label", ""),
                "type": el.get("type", ""),
            }
    return table


def save_lookup(elements: list[dict], path: str):
    """Save lookup table to JSON file (for debugging / agent context)."""
    table = build_lookup(elements)
    with open(path, "w") as f:
        json.dump(table, f, indent=2)
    return path

