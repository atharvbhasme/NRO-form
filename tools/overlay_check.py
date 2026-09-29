"""Draw the detected fields over a rendered page, for visual checking.

    python3 tools/overlay_check.py <form> <page> [out.png] [x0 top x1 bottom]
"""
import json
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLOURS = {"checkbox": (255, 0, 170), "text": (0, 140, 255), "comb": (255, 140, 0)}


def main():
    form, page_no = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else f"/tmp/{form}_p{page_no}.png"
    crop_pt = [float(v) for v in sys.argv[4:8]] if len(sys.argv) > 7 else None

    data = json.load(open(os.path.join(ROOT, "data", f"{form}-fields.json")))[page_no]
    img = Image.open(os.path.join(ROOT, "assets", form, f"page{page_no}.png")).convert("RGB")
    sx, sy = img.width / data["width"], img.height / data["height"]
    d = ImageDraw.Draw(img)

    for f in data["fields"]:
        colour = (0, 190, 90) if f.get("line") else COLOURS[f["type"]]
        if "hit" in f:
            h = f["hit"]
            d.rectangle([h["x0"] * sx, h["top"] * sy, h["x1"] * sx, h["bottom"] * sy],
                        outline=(150, 0, 255), width=2)
        d.rectangle([f["x0"] * sx, f["top"] * sy, f["x1"] * sx, f["bottom"] * sy],
                    outline=colour, width=2)

    if crop_pt:
        box = (int(crop_pt[0] * sx), int(crop_pt[1] * sy), int(crop_pt[2] * sx), int(crop_pt[3] * sy))
        img = img.crop(box)
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img.save(out)
    print(out, img.size)


if __name__ == "__main__":
    main()
