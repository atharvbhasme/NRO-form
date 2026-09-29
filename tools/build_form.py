"""Build <form>-index.html from data/<form>-fields.json + assets/style.css.

    python3 tools/build_form.py            # rebuild every known form
    python3 tools/build_form.py axis       # rebuild one

The stylesheet is inlined and every input carries its own absolute position, so
the page still lines up if it is opened without the stylesheet beside it.
"""
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPLAY_WIDTH = 1080
BUILD = time.strftime("%Y%m%d-%H%M")

FORMS = {
    "hdfc": {
        "title": "HDFC Bank - Resident to NRO Account Conversion Form",
        "short": "HDFC - Resident to NRO Conversion",
        "output": "HDFC-NRO-Conversion-Form-FILLED.pdf",
        "other": ("axis-index.html", "Axis form"),
        "legacy_key": "nro_form_draft_v3",   # drafts saved before the two-form split
    },
    "axis": {
        "title": "Axis Bank - NRI Scheme CIF Conversion cum ReKYC Form",
        "short": "Axis - NRI Scheme CIF Conversion cum ReKYC",
        "output": "Axis-NRI-ReKYC-Form-FILLED.pdf",
        "other": ("hdfc-index.html", "HDFC form"),
    },
}


def build(form):
    cfg = FORMS[form]
    fields = json.load(open(os.path.join(ROOT, "data", f"{form}-fields.json")))
    css = open(os.path.join(ROOT, "assets/style.css")).read()

    pages_html, counts = [], {"text": 0, "comb": 0, "checkbox": 0}
    page_nums = sorted(fields, key=int)

    for pn in page_nums:
        page = fields[pn]
        scale = DISPLAY_WIDTH / page["width"]
        disp_h = page["height"] * scale
        divs = []

        for f in page["fields"]:
            counts[f["type"]] += 1
            hit = f.get("hit", f)          # the full white box when we know it
            l, t = hit["x0"] * scale, hit["top"] * scale
            w = (hit["x1"] - hit["x0"]) * scale
            h = (hit["bottom"] - hit["top"]) * scale
            pos = f"position:absolute;left:{l:.2f}px;top:{t:.2f}px;width:{w:.2f}px;height:{h:.2f}px;"

            if f["type"] == "checkbox":
                divs.append(f'<input type="checkbox" id="{f["id"]}" class="fld chk" '
                            f'style="{pos}font-size:{min(w, h) * 1.02:.2f}px;">')
                continue

            classes, style = "fld txt", pos
            if f["type"] == "comb":
                classes += " comb"
                maxlen = len(f["cells"])
                style += f"padding-left:{max((f['cells'][0]['x0'] - hit['x0']) * scale, 0):.2f}px;"
                fs = max(min(h * 0.66, 13.5), 8)
            elif f.get("line"):
                classes += " ln"
                maxlen = 200
                fs = max(min(h * 0.82, 12.5), 8)
            else:
                maxlen = 300
                fs = max(min(h * 0.66, 13.5), 8)
            divs.append(f'<input type="text" id="{f["id"]}" class="{classes}" maxlength="{maxlen}" '
                        f'style="{style}font-size:{fs:.2f}px;">')

        pages_html.append(f'''
  <section class="page" id="page{pn}" style="position:relative;width:{DISPLAY_WIDTH}px;height:{disp_h:.2f}px;margin:0 auto 28px;">
    <img src="assets/{form}/page{pn}.png?v={BUILD}" alt="Form page {pn}" draggable="false"
         style="display:block;width:{DISPLAY_WIDTH}px;height:{disp_h:.2f}px;">
    {''.join(divs)}
  </section>''')

    nav = " ".join(f'<a href="#page{pn}">Page {pn}</a>' for pn in page_nums)
    other_href, other_label = cfg["other"]
    total = sum(counts.values())

    # A draft is keyed to the field layout, not to the build: rebuilding keeps
    # entries, while a changed layout (renumbered ids) starts a fresh draft
    # instead of restoring values onto the wrong boxes.
    ids = "|".join(f["id"] for pn in page_nums for f in fields[pn]["fields"])
    fingerprint = hashlib.sha1(ids.encode()).hexdigest()[:10]
    legacy = f', legacyKey: "{cfg["legacy_key"]}"' if cfg.get("legacy_key") else ""

    html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{cfg["title"]} - Fillable</title>
<style>
{css}
</style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <strong>{cfg["short"]}</strong>
    <span class="hint">Type into the boxes and tick the checkboxes, then press
      <b>Fill PDF &amp; Download</b> at the bottom. A new PDF is created &mdash; the original file is never changed.
      Signature boxes stay blank for signing by hand.</span>
  </div>
  <nav class="pagenav">{nav}<a class="swap" href="{other_href}">Open {other_label}</a><span class="build">build {BUILD}</span></nav>
</header>
<div id="loadWarn" class="loadwarn" hidden></div>

<main class="page-wrap">
{''.join(pages_html)}

  <div class="footer-actions">
    <button id="fillBtn" class="btn btn-primary" type="button">Fill PDF &amp; Download</button>
    <button id="resetBtn" class="btn btn-secondary" type="button">Reset Form</button>
  </div>
</main>

<button id="fabBtn" class="fab" type="button" title="Fill PDF &amp; Download">Fill PDF</button>
<div id="toast" class="toast" hidden></div>

<script>window.FORM_CONFIG = {{outputName: "{cfg["output"]}", draftKey: "{form}_draft_{fingerprint}"{legacy}}};</script>
<script src="assets/pdf-lib.min.js?v={BUILD}"></script>
<script src="assets/{form}/fields-data.js?v={BUILD}"></script>
<script src="assets/{form}/pdf-data.js?v={BUILD}"></script>
<script src="assets/fill.js?v={BUILD}"></script>
</body>
</html>
'''

    open(os.path.join(ROOT, f"{form}-index.html"), "w").write(html)
    with open(os.path.join(ROOT, "assets", form, "fields-data.js"), "w") as fh:
        fh.write("window.FIELDS_DATA = ")
        json.dump(fields, fh)
        fh.write(";\n")

    print(f"{form}-index.html: {len(page_nums)} pages, {total} fields "
          f"(text={counts['text']} comb={counts['comb']} checkbox={counts['checkbox']}) build {BUILD}")


if __name__ == "__main__":
    targets = sys.argv[1:] or list(FORMS)
    for name in targets:
        build(name)
