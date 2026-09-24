(function () {
  "use strict";

  // Always written to a brand-new file; the source PDF is only ever read.
  const OUTPUT_NAME = "NRO-Conversion-Form-FILLED.pdf";
  // bumped with the field layout so a saved draft can never land on renumbered fields
  const DRAFT_KEY = "nro_form_draft_v3";

  function selfCheck() {
    const problems = [];
    const broken = [...document.querySelectorAll(".page img")].filter((i) => !i.naturalWidth).length;
    if (broken) problems.push(broken + " page image(s) did not load from the assets folder");
    if (!window.FIELDS_DATA || !window.PDF_BASE64) problems.push("the form data files did not load");
    if (!problems.length) return;
    const el = document.getElementById("loadWarn");
    el.textContent = "This page is not loading correctly: " + problems.join("; ")
      + ". Keep index.html and the assets folder together, then reload with Ctrl+Shift+R.";
    el.hidden = false;
  }

  function base64ToBytes(b64) {
    const binStr = atob(b64);
    const bytes = new Uint8Array(binStr.length);
    for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
    return bytes;
  }

  function toast(msg, kind) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast" + (kind ? " " + kind : "");
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 4000);
  }

  function allFieldEls() {
    return Array.from(document.querySelectorAll(".fld"));
  }

  function saveDraft() {
    const data = {};
    allFieldEls().forEach((el) => {
      data[el.id] = el.type === "checkbox" ? el.checked : el.value;
    });
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(data));
    } catch (e) {
      /* storage unavailable or full; ignore */
    }
  }

  function loadDraft() {
    let data;
    try {
      data = JSON.parse(localStorage.getItem(DRAFT_KEY) || "{}");
    } catch (e) {
      data = {};
    }
    Object.keys(data).forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === "checkbox") el.checked = !!data[id];
      else el.value = data[id];
    });
  }

  function clearAll() {
    if (!confirm("Clear all entered data on this form?")) return;
    allFieldEls().forEach((el) => {
      if (el.type === "checkbox") el.checked = false;
      else el.value = "";
    });
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
    toast("All fields cleared.");
  }

  function ptBox(f) {
    return { x0: f.x0, x1: f.x1, top: f.top, bottom: f.bottom, w: f.x1 - f.x0, h: f.bottom - f.top };
  }

  function safeDrawText(page, text, x, y, size, font) {
    try {
      page.drawText(text, { x, y, size, font });
    } catch (e) {
      const cleaned = text.replace(/[^\x00-\x7E]/g, "");
      if (cleaned) {
        try {
          page.drawText(cleaned, { x, y, size, font });
        } catch (e2) {
          /* give up on this fragment rather than aborting the whole fill */
        }
      }
    }
  }

  function drawTextInBox(page, text, box, pageHeight, font, opts) {
    opts = opts || {};
    if (!text) return;
    let size = Math.min(box.h * (opts.heightRatio || 0.66), opts.maxSize || 10.5);
    const maxWidth = Math.max(box.w - 3, 3);
    while (size > 5 && font.widthOfTextAtSize(text, size) > maxWidth) {
      size -= 0.5;
    }
    // never let a long entry bleed outside its box
    while (text.length > 1 && font.widthOfTextAtSize(text, size) > maxWidth) {
      text = text.slice(0, -1);
    }
    const yBottomEdge = pageHeight - box.bottom;
    const y = yBottomEdge + (box.h - size) / 2 + size * 0.12;
    let x;
    if (opts.align === "center") {
      const tw = font.widthOfTextAtSize(text, size);
      x = box.x0 + (box.w - tw) / 2;
    } else {
      x = box.x0 + 2;
    }
    safeDrawText(page, text, x, y, size, font);
  }

  async function fillPdf() {
    const btn = document.getElementById("fillBtn");
    const fab = document.getElementById("fabBtn");
    btn.disabled = true;
    fab.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = "Filling PDF...";
    try {
      const data = window.FIELDS_DATA;
      if (!data) throw new Error("Form field data did not load (assets/fields-data.js missing?)");
      if (!window.PDF_BASE64) throw new Error("Source PDF data did not load (assets/pdf-data.js missing?)");
      const pdfBytes = base64ToBytes(window.PDF_BASE64);

      const { PDFDocument, StandardFonts } = window.PDFLib;
      const pdfDoc = await PDFDocument.load(pdfBytes);
      const font = await pdfDoc.embedFont(StandardFonts.Helvetica);
      const fontBold = await pdfDoc.embedFont(StandardFonts.HelveticaBold);
      const pages = pdfDoc.getPages();

      Object.keys(data).forEach((pn) => {
        const pageInfo = data[pn];
        const page = pages[Number(pn) - 1];
        if (!page) return;
        const pageHeight = pageInfo.height;

        pageInfo.fields.forEach((f) => {
          const el = document.getElementById(f.id);
          if (!el) return;

          if (f.type === "checkbox") {
            if (el.checked) {
              const box = ptBox(f);
              const size = Math.min(box.w, box.h) * 0.9;
              const tw = fontBold.widthOfTextAtSize("X", size);
              const x = box.x0 + (box.w - tw) / 2;
              const yBottomEdge = pageHeight - box.bottom;
              const y = yBottomEdge + (box.h - size) / 2 + size * 0.08;
              safeDrawText(page, "X", x, y, size, fontBold);
            }
          } else if (f.type === "comb") {
            const val = (el.value || "").toUpperCase();
            const cells = f.cells || [];
            for (let i = 0; i < Math.min(val.length, cells.length); i++) {
              const c = cells[i];
              const cbox = { x0: c.x0, x1: c.x1, top: c.top, bottom: c.bottom, w: c.x1 - c.x0, h: c.bottom - c.top };
              drawTextInBox(page, val[i], cbox, pageHeight, font, { align: "center" });
            }
          } else {
            const val = (el.value || "").trim();
            // write-on rules are shallow, so let their text use most of the height
            const opts = f.line ? { align: "left", maxSize: 9.5, heightRatio: 0.85 }
                                : { align: "left" };
            if (val) drawTextInBox(page, val, ptBox(f), pageHeight, font, opts);
          }
        });
      });

      const outBytes = await pdfDoc.save();
      const blob = new Blob([outBytes], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = OUTPUT_NAME;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast("PDF filled and downloaded.", "success");
    } catch (err) {
      console.error(err);
      toast("Could not fill PDF: " + err.message, "error");
    } finally {
      btn.disabled = false;
      fab.disabled = false;
      btn.textContent = originalLabel;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    selfCheck();
    loadDraft();
    document.getElementById("fillBtn").addEventListener("click", fillPdf);
    document.getElementById("fabBtn").addEventListener("click", fillPdf);
    document.getElementById("clearBtn").addEventListener("click", clearAll);

    let saveTimer = null;
    document.addEventListener("input", (e) => {
      if (!e.target.classList || !e.target.classList.contains("fld")) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveDraft, 400);
    });
    document.addEventListener("change", (e) => {
      if (e.target.classList && e.target.classList.contains("chk")) saveDraft();
    });
  });
})();
