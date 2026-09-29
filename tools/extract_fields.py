"""Find every writable spot on a printed (non-AcroForm) PDF form.

    python3 tools/extract_fields.py <form-name> <source.pdf>

Writes assets/<form-name>/pageN.png, assets/<form-name>/pdf-data.js and
data/<form-name>-fields.json.

Three sources are combined so nothing writable is missed:
  * vector rectangles  -> tick boxes, per-character comb grids, boxed inputs
  * white rectangles found in the rendered page -> the true extent of each box,
    plus boxes the vector pass misses
  * underscore runs and drawn rules -> "Name ______" style write-on lines

Signature and stamp areas are deliberately left without inputs, and solid
corner registration marks are rejected by checking that a tick box is empty.
"""
import base64
import json
import os
import sys

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDER_SCALE = 2.0
WHITE = 238
SIGN_WORDS = ("signat", "stamp", "thumb")


# ------------------------------------------------------------- geometry ----
def dedupe(rects):
    seen, out = set(), []
    for r in rects:
        key = (r["x0"], r["x1"], r["top"], r["bottom"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def contains(a, b, tol=0.6):
    return (a["x0"] - tol <= b["x0"] and a["x1"] + tol >= b["x1"]
            and a["top"] - tol <= b["top"] and a["bottom"] + tol >= b["bottom"])


def container_counts(rects):
    counts = [0] * len(rects)
    for i, a in enumerate(rects):
        area_a = a["w"] * a["h"]
        for j, b in enumerate(rects):
            if i != j and b["w"] * b["h"] < area_a * 0.9 and contains(a, b):
                counts[i] += 1
    return counts


def overlap(a, b):
    ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def extract_combs(leaves, is_empty):
    """Group a row of equal, touching cells into one per-character field."""
    leaves = sorted(leaves, key=lambda r: (round(r["top"], 1), r["x0"]))
    used = [False] * len(leaves)
    combs = []
    for i, r in enumerate(leaves):
        if used[i] or not (5.5 <= r["w"] <= 20 and 5.5 <= r["h"] <= 20):
            continue
        if not (0.4 <= r["w"] / r["h"] <= 2.6):
            continue
        run, cur, max_gap = [i], r, 0.0
        for j in range(i + 1, len(leaves)):
            if used[j]:
                continue
            c = leaves[j]
            if abs(c["top"] - r["top"]) > 0.7 or abs(c["bottom"] - r["bottom"]) > 0.7:
                continue
            gap = c["x0"] - cur["x1"]
            if -0.6 <= gap <= 3.0 and abs(c["w"] - r["w"]) < r["w"] * 0.5:
                run.append(j)
                cur = c
                max_gap = max(max_gap, gap)
        # 3+ cells is a character grid; a touching pair is a two-digit entry
        # only when both cells are blank - a printed pair like [Y][N] is two tick boxes
        pair_is_entry = (len(run) == 2 and max_gap <= 1.5
                         and all(is_empty(leaves[k]) for k in run))
        if len(run) >= 3 or pair_is_entry:
            for k in run:
                used[k] = True
            cells = sorted((leaves[k] for k in run), key=lambda c: c["x0"])
            combs.append({
                "x0": cells[0]["x0"], "x1": cells[-1]["x1"],
                "top": min(c["top"] for c in cells),
                "bottom": max(c["bottom"] for c in cells),
                "cells": [{"x0": c["x0"], "x1": c["x1"], "top": c["top"], "bottom": c["bottom"]}
                          for c in cells],
            })
    return combs, [leaves[i] for i in range(len(leaves)) if not used[i]]


def white_rects(mask, sx, sy, min_w_pt=12, min_h_pt=6.5, tol=3):
    """Axis-aligned white rectangles, grown from per-row runs of white pixels."""
    h, w = mask.shape
    min_w, min_h = int(min_w_pt * sx), int(min_h_pt * sy)
    active, done = [], []
    for y in range(h):
        row = mask[y].view(np.int8)
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row, [0]))))
        runs = [(idx[i], idx[i + 1] - 1) for i in range(0, len(idx), 2)
                if idx[i + 1] - idx[i] >= min_w]
        used = [False] * len(runs)
        still = []
        for rect in active:
            match = None
            for i, (x0, x1) in enumerate(runs):
                if not used[i] and abs(x0 - rect["x0"]) <= tol and abs(x1 - rect["x1"]) <= tol:
                    match = i
                    break
            if match is None:
                done.append(rect)
            else:
                used[match] = True
                rect["y1"] = y
                still.append(rect)
        for i, (x0, x1) in enumerate(runs):
            if not used[i]:
                still.append({"x0": int(x0), "x1": int(x1), "y0": y, "y1": y})
        active = still
    done.extend(active)
    return [{"x0": r["x0"] / sx, "x1": (r["x1"] + 1) / sx,
             "top": r["y0"] / sy, "bottom": (r["y1"] + 1) / sy}
            for r in done if r["y1"] - r["y0"] + 1 >= min_h]


def underscore_runs(page):
    runs, cur = [], None
    chars = sorted((c for c in page.chars if c["text"] == "_"),
                   key=lambda c: (round(c["top"], 1), c["x0"]))
    for c in chars:
        if cur and abs(c["top"] - cur["top"]) < 0.8 and (c["x0"] - cur["x1"]) < 2.5:
            cur["x1"] = c["x1"]
            cur["n"] += 1
        else:
            if cur and cur["n"] >= 3:
                runs.append(cur)
            cur = {"x0": c["x0"], "x1": c["x1"], "top": c["top"], "bottom": c["bottom"], "n": 1}
    if cur and cur["n"] >= 3:
        runs.append(cur)
    return runs


# ---------------------------------------------------------------- build ----
def build_page(page, img, page_no):
    pw, ph = page.width, page.height

    def is_placeholder_colour(col):
        """Forms print their hint text ("First Name", "Prefix") in light grey."""
        if not col or not isinstance(col, (list, tuple)):
            return False
        vals = [v for v in col if isinstance(v, (int, float))]
        if len(vals) == 1:
            return 0.45 <= vals[0] <= 0.82
        if len(vals) >= 3:
            r, g, b = vals[:3]
            return min(r, g, b) >= 0.45 and max(r, g, b) <= 0.82 and (max(r, g, b) - min(r, g, b)) <= 0.15
        return False

    words = []
    for w in page.extract_words(extra_attrs=["non_stroking_color"]):
        words.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"],
                      "top": w["top"], "bottom": w["bottom"],
                      "hint": is_placeholder_colour(w.get("non_stroking_color"))})
    arr = np.asarray(img)
    mask = (arr[:, :, 0] >= WHITE) & (arr[:, :, 1] >= WHITE) & (arr[:, :, 2] >= WHITE)
    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    ink = lum < 120          # real content; grey hint text (~148) is not ink
    sx, sy = img.width / pw, img.height / ph

    def is_heading(text):
        letters = "".join(c for c in text if c.isalpha())
        return len(letters) >= 8 and letters.isupper()

    sign_words = [w for w in words
                  if any(k in w["text"].lower() for k in SIGN_WORDS) and not is_heading(w["text"])]

    def words_inside(r, include_hints=True):
        out = []
        for w in words:
            if not include_hints and w["hint"]:
                continue
            cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
            if r["x0"] - 0.5 <= cx <= r["x1"] + 0.5 and r["top"] - 0.5 <= cy <= r["bottom"] + 0.5:
                out.append(w["text"])
        return out

    def is_caption_box(r):
        """Printed wording inside a box means it is a label, not an input -
        but grey hint text ("First Name") belongs to an input."""
        return sum(1 for t in words_inside(r, include_hints=False)
                   if len("".join(c for c in t if c.isalpha())) >= 2) >= 2

    def in_signature_zone(r):
        h = r["bottom"] - r["top"]
        for sw in sign_words:
            gap = sw["top"] - r["bottom"]
            if not (-1 <= gap <= 14):
                continue
            if min(r["x1"], sw["x1"]) - max(r["x0"], sw["x0"]) <= 0:
                continue
            if h >= 18 or (r["x1"] - r["x0"]) <= 3.0 * (sw["x1"] - sw["x0"]):
                return True
        return False

    def in_header_zone(r):
        return r["top"] < 40        # masthead / logo band

    def in_barcode_zone(r):
        return r["x0"] > 440 and r["top"] < 46

    def excluded(r):
        return (in_header_zone(r) or in_barcode_zone(r)
                or in_signature_zone(r) or is_caption_box(r))

    rects = dedupe([{"x0": r["x0"], "x1": r["x1"], "top": r["top"], "bottom": r["bottom"],
                     "w": r["width"], "h": r["height"]} for r in page.rects])
    counts = container_counts(rects)
    leaves = [r for i, r in enumerate(rects) if r["w"] >= 2 and r["h"] >= 2 and counts[i] < 2]

    # some forms draw their tick boxes and cells as rounded paths rather than
    # rectangles; keep only curves that are already box-sized
    curve_boxes = [{"x0": c["x0"], "x1": c["x1"], "top": c["top"], "bottom": c["bottom"],
                    "w": c["width"], "h": c["height"]} for c in page.curves
                   if ((5.5 <= c["width"] <= 20 and 5.5 <= c["height"] <= 20
                        and 0.4 <= c["width"] / max(c["height"], 0.01) <= 2.6)
                       or (15 <= c["width"] <= 545 and 8 <= c["height"] <= 22))]
    for c in dedupe(curve_boxes):
        cb = (c["x0"], c["top"], c["x1"], c["bottom"])
        area = max(c["w"] * c["h"], 0.01)
        # the same box is often drawn as both a rect and a path - keep one
        if any(overlap(cb, (r["x0"], r["top"], r["x1"], r["bottom"])) / area > 0.5 for r in leaves):
            continue
        leaves.append(c)

    def cell_is_empty(r):
        return not words_inside(r)

    combs, rest = extract_combs(leaves, cell_is_empty)

    boxes, vector_texts = [], []
    for r in rest:
        if 5.8 <= r["w"] <= 16 and 5.8 <= r["h"] <= 16 and 0.5 <= r["w"] / r["h"] <= 2.0:
            boxes.append(r)
        elif r["h"] <= 22 and r["w"] >= 15:
            vector_texts.append(r)

    # a real tick box is empty; a solid registration mark is not
    ticks, dropped = [], 0
    for r in boxes:
        if excluded(r):
            continue
        x0, x1 = int(r["x0"] * sx) + 3, int(r["x1"] * sx) - 3
        y0, y1 = int(r["top"] * sy) + 3, int(r["bottom"] * sy) - 3
        inner = mask[y0:y1, x0:x1]
        if inner.size and inner.mean() < 0.45:
            dropped += 1
            continue
        ticks.append(r)

    whites = white_rects(mask, sx, sy)
    fields = []

    def add(kind, r, **extra):
        f = {"type": kind, "x0": round(r["x0"], 2), "x1": round(r["x1"], 2),
             "top": round(r["top"], 2), "bottom": round(r["bottom"], 2)}
        f.update(extra)
        fields.append(f)

    # a comb is a row of equal cells - it is structurally an input, so the
    # "printed wording means it is a label" rule does not apply to it
    def comb_excluded(r):
        return in_header_zone(r) or in_barcode_zone(r) or in_signature_zone(r)

    primary = ([{"kind": "comb", "r": c} for c in combs if not comb_excluded(c)]
               + [{"kind": "text", "r": t} for t in vector_texts if not excluded(t)])

    def white_for(r):
        """The white box holding this vector box, when it holds only this one."""
        rb = (r["x0"], r["top"], r["x1"], r["bottom"])
        area = max((rb[2] - rb[0]) * (rb[3] - rb[1]), 0.01)
        best, best_ov = None, 0.0
        for wr in whites:
            ov = overlap(rb, (wr["x0"], wr["top"], wr["x1"], wr["bottom"])) / area
            if ov > best_ov:
                best_ov, best = ov, wr
        if best is None or best_ov < 0.55:
            return None
        wb = (best["x0"], best["top"], best["x1"], best["bottom"])
        holders = 0
        for other in primary:
            o = other["r"]
            ob = (o["x0"], o["top"], o["x1"], o["bottom"])
            oarea = max((ob[2] - ob[0]) * (ob[3] - ob[1]), 0.01)
            if overlap(ob, wb) / oarea > 0.55:
                holders += 1
        return best if holders == 1 else None

    claimed = set()
    for item in primary:
        r, wr = item["r"], white_for(item["r"])
        if wr is not None:
            claimed.add(id(wr))
        if item["kind"] == "comb":
            extra = {"cells": [{k: round(v, 2) for k, v in c.items()} for c in r["cells"]]}
            if wr is not None:
                extra["hit"] = {"x0": round(wr["x0"], 2), "x1": round(wr["x1"], 2),
                                "top": round(wr["top"], 2), "bottom": round(wr["bottom"], 2)}
            add("comb", r, **extra)
        else:
            add("text", wr if wr is not None else r)

    existing = [(f["x0"], f["top"], f["x1"], f["bottom"]) for f in fields]
    existing += [(t["x0"], t["top"], t["x1"], t["bottom"]) for t in ticks]
    rows = [f for f in fields if f["type"] in ("text", "comb")]

    def white_bounded(r):
        """Reject white bands that bleed sideways into a text area."""
        X0, X1 = int(r["x0"] * sx), int(r["x1"] * sx)
        Y0, Y1 = int(r["top"] * sy), int(r["bottom"] * sy)
        left = mask[Y0:Y1, max(X0 - 4, 0):max(X0 - 1, 1)]
        right = mask[Y0:Y1, X1 + 2:X1 + 5]
        lf = float(left.mean()) if left.size else 0.0
        rf = float(right.mean()) if right.size else 0.0
        return lf < 0.5 and rf < 0.5

    added_white = 0
    for wr in whites:
        if id(wr) in claimed:
            continue
        h_pt, w_pt = wr["bottom"] - wr["top"], wr["x1"] - wr["x0"]
        if not (9 <= h_pt <= 26 and 14 <= w_pt <= 545):
            continue
        if excluded(wr) or words_inside(wr) or not white_bounded(wr):
            continue
        wb = (wr["x0"], wr["top"], wr["x1"], wr["bottom"])
        area = (wb[2] - wb[0]) * (wb[3] - wb[1])
        if any(overlap(wb, e) / area > 0.15 for e in existing):
            continue
        aligned = False
        for f in rows:
            x_ov = min(wr["x1"], f["x1"]) - max(wr["x0"], f["x0"])
            if x_ov > 0.7 * min(w_pt, f["x1"] - f["x0"]) and abs(wr["top"] - f["bottom"]) < 30:
                aligned = True
                break
            if (abs(wr["top"] - f["top"]) < 2 and abs(wr["bottom"] - f["bottom"]) < 2
                    and min(abs(wr["x0"] - f["x1"]), abs(f["x0"] - wr["x1"])) < 220):
                aligned = True
                break
        if not aligned:
            continue
        add("text", wr)
        existing.append(wb)
        added_white += 1

    # write-on rules: underscore runs and drawn horizontal lines
    rules = [{"x0": u["x0"], "x1": u["x1"], "base": u["bottom"], "top": u["top"]}
             for u in underscore_runs(page)]
    for ln in page.lines:
        if abs(ln["top"] - ln["bottom"]) > 2.5:
            continue
        w = ln["x1"] - ln["x0"]
        if not (25 <= w <= 430) or ln["x0"] < 5 or ln["x1"] > pw - 5:
            continue
        rules.append({"x0": ln["x0"], "x1": ln["x1"], "base": ln["bottom"], "top": ln["top"] - 6})

    def writing_space_is_clear(r):
        """You write above a rule, so that strip must be empty ink-wise.
        Table borders have the cell's text sitting right above them."""
        X0, X1 = int(r["x0"] * sx), int(r["x1"] * sx)
        Y0, Y1 = int(r["top"] * sy), int((r["bottom"] - 2.2) * sy)   # skip the rule itself
        strip = ink[Y0:Y1, X0:X1]
        return strip.size == 0 or float(strip.mean()) < 0.04

    def has_label_before(run):
        """A write-on rule either follows a caption on the same line
        ("Name ______") or carries grey placeholder text of its own."""
        for w in words:
            if abs(w["bottom"] - run["base"]) <= 3.5 and w["x0"] < run["x0"] \
                    and run["x0"] - w["x1"] <= 40:
                return True
            if w["hint"] and abs(w["bottom"] - run["base"]) <= 6 \
                    and min(w["x1"], run["x1"]) - max(w["x0"], run["x0"]) > 0:
                return True
        return False

    added_lines = 0
    for run in rules:
        base = run["base"]
        r = {"x0": run["x0"], "x1": run["x1"], "top": base - 9.5, "bottom": base - 0.3}
        if not has_label_before(run) or not writing_space_is_clear(r):
            continue
        skip = False
        for sw in sign_words:
            same_line = abs(sw["top"] - run["top"]) < 7 and 0 <= run["x0"] - sw["x1"] <= 25
            span = r["x1"] - r["x0"]
            mid = (sw["x0"] + sw["x1"]) / 2
            below = (-2 <= sw["top"] - base <= 14 and span >= 60
                     and r["x0"] + span / 3 <= mid <= r["x1"] - span / 3)
            if same_line or below:
                skip = True
                break
        if skip or in_barcode_zone(r) or in_header_zone(r):
            continue
        rb = (r["x0"], r["top"], r["x1"], r["bottom"])
        area = (rb[2] - rb[0]) * (rb[3] - rb[1])
        if any(overlap(rb, e) / area > 0.4 for e in existing):
            continue
        add("text", r, line=True)
        existing.append(rb)
        added_lines += 1

    for r in ticks:
        add("checkbox", r)

    for i, f in enumerate(fields, 1):
        f["id"] = f"p{page_no}_{f['type'][0]}{i}"
        f["page"] = page_no

    n = lambda t: sum(1 for f in fields if f["type"] == t)
    print(f"page {page_no}: text={n('text')} comb={n('comb')} checkbox={n('checkbox')}"
          f"  | print-marks dropped={dropped} extra white boxes={added_white} write-on lines={added_lines}")
    return {"width": pw, "height": ph, "fields": fields}


def main(form, pdf_path):
    asset_dir = os.path.join(ROOT, "assets", form)
    os.makedirs(asset_dir, exist_ok=True)

    doc = pdfium.PdfDocument(pdf_path)
    images = []
    for i in range(len(doc)):
        img = doc[i].render(scale=RENDER_SCALE).to_pil().convert("RGB")
        img.save(os.path.join(asset_dir, f"page{i + 1}.png"))
        images.append(img)

    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            out[str(i + 1)] = build_page(page, images[i], i + 1)

    fields_path = os.path.join(ROOT, "data", f"{form}-fields.json")
    json.dump(out, open(fields_path, "w"))

    with open(pdf_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    with open(os.path.join(asset_dir, "pdf-data.js"), "w") as f:
        f.write('window.PDF_BASE64 = "' + b64 + '";\n')

    total = sum(len(p["fields"]) for p in out.values())
    print(f"\n{form}: {len(out)} pages, {total} fields -> {fields_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
