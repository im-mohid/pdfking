"""Processing engines. Every tool: fn(items, opts) -> (filename, bytes). items = [(name, bytes)]."""
import difflib, hashlib, io, os, re, shutil, subprocess, tempfile, zipfile
import xml.etree.ElementTree as ET
import pandas as pd
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ExifTags
from pypdf import PdfReader, PdfWriter

try:
    import pillow_heif; pillow_heif.register_heif_opener(); HEIC = True
except Exception: HEIC = False
try: import pyzipper
except Exception: pyzipper = None
try: from pdf2docx import Converter
except Exception: Converter = None
try: import pdfplumber
except Exception: pdfplumber = None
try: import docx
except Exception: docx = None

Image.MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
MB = 1024 * 1024
TOOLS = {}


def O(name, label, type="text", default="", choices=None):
    return dict(name=name, label=label, type=type, default=default, choices=choices or [])

def tool(slug, name, cat, desc, accept, multi=False, opts=(), rank=0, avail=True, note=""):
    def deco(fn):
        TOOLS[slug] = dict(slug=slug, name=name, cat=cat, desc=desc, accept=accept, multi=multi,
                           opts=list(opts), rank=rank, avail=avail, note=note, fn=fn)
        return fn
    return deco

IMG = ".jpg,.jpeg,.png,.webp" + (",.heic,.heif" if HEIC else "")
YN = ["yes", "no"]

# ---------- helpers ----------
def ext(n): return os.path.splitext(n)[1].lower()
def _safe(n): return re.sub(r"[^\w.\- ]", "_", os.path.basename(n or "")) or "file"
def _stem(n): return os.path.splitext(_safe(n))[0]
def _txt(name, text): return name, text.encode("utf-8")

def _zip(outs):
    buf, seen = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in outs:
            n = _safe(n)
            while n in seen: n = "x_" + n
            seen.add(n); z.writestr(n, b)
    return buf.getvalue()

def _each(items, fn):
    outs = [fn(n, b) for n, b in items]
    return outs[0] if len(outs) == 1 else ("results.zip", _zip(outs))

def _need(items, k):
    if len(items) != k: raise ValueError(f"Select exactly {k} files.")

def sig_ok(name, b):
    e = ext(name)
    if e == ".pdf": return b[:1024].find(b"%PDF-") >= 0
    if e in (".jpg", ".jpeg"): return b[:3] == b"\xff\xd8\xff"
    if e == ".png": return b[:8] == b"\x89PNG\r\n\x1a\n"
    if e == ".webp": return b[:4] == b"RIFF" and b[8:12] == b"WEBP"
    if e in (".heic", ".heif"): return b[4:8] == b"ftyp"
    if e in (".docx", ".xlsx", ".pptx", ".zip"): return b[:2] == b"PK"
    if e in (".csv", ".txt"): return b"\x00" not in b[:4096]
    return True

def _pdf(b, pw=None):
    if b[:1024].find(b"%PDF-") < 0: raise ValueError("This is not a valid PDF file.")
    try:
        r = PdfReader(io.BytesIO(b))
        if r.is_encrypted and not r.decrypt(pw or ""): raise ValueError("This PDF is password protected.")
        len(r.pages); return r
    except ValueError: raise
    except Exception: raise ValueError("This PDF is damaged. Try the Repair PDF tool.")

def _fdoc(b):
    _pdf(b); return fitz.open(stream=b, filetype="pdf")

def _pdfbytes(w):
    buf = io.BytesIO(); w.write(buf); return buf.getvalue()

def _img(b):
    try: im = Image.open(io.BytesIO(b)); im.load(); return im
    except Exception: raise ValueError("This is not a valid image file.")

def _pages(s, n):
    s = (s or "").replace(" ", "")
    if s.lower() in ("", "all"): return list(range(n))
    out = []
    try:
        for part in s.split(","):
            if "-" in part:
                a, b = part.split("-", 1); out += range(int(a or 1) - 1, int(b or n))
            elif part: out.append(int(part) - 1)
    except ValueError: raise ValueError("Page format example: 1-3,5,8")
    if not out or any(p < 0 or p >= n for p in out): raise ValueError(f"Pages must be between 1 and {n}.")
    return out

def _encode(im, fmt, q=85):
    fmt = (fmt or "png").upper(); buf = io.BytesIO()
    if fmt in ("JPG", "JPEG", "MPO", "HEIF", "HEIC"):
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA"); bg = Image.new("RGB", im.size, "white"); bg.paste(im, mask=im.split()[-1]); im = bg
        im.convert("RGB").save(buf, "JPEG", quality=q, optimize=True); return buf.getvalue(), "jpg"
    if fmt == "WEBP": im.save(buf, "WEBP", quality=q); return buf.getvalue(), "webp"
    im.save(buf, "PNG", optimize=True); return buf.getvalue(), "png"

def _fmt(im): return (im.format or "PNG")

def _df(n, b, sheet=0):
    try:
        if ext(n) == ".csv":
            try: return pd.read_csv(io.BytesIO(b), encoding="utf-8-sig")
            except UnicodeDecodeError: return pd.read_csv(io.BytesIO(b), encoding="latin-1")
        return pd.read_excel(io.BytesIO(b), engine="openpyxl", sheet_name=sheet)
    except Exception: raise ValueError(f"Could not read {_safe(n)}.")

def _uniq(name, used):
    base = (str(name) or "Sheet")[:28] or "Sheet"; n, i = base, 1
    while n in used: i += 1; n = f"{base}_{i}"
    used.add(n); return n

def _xlsx(sheets):
    buf, used = io.BytesIO(), set()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for k, d in sheets.items(): d.to_excel(w, sheet_name=_uniq(k, used), index=False)
    return buf.getvalue()

def _like(n, df, tag):
    if ext(n) == ".csv": return f"{_stem(n)}_{tag}.csv", df.to_csv(index=False).encode("utf-8-sig")
    return f"{_stem(n)}_{tag}.xlsx", _xlsx({"Sheet1": df})

def _lo(n, b, target):
    if not SOFFICE: raise ValueError("Office engine is not installed on this server.")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "input" + ext(n)); open(src, "wb").write(b)
        try:
            subprocess.run([SOFFICE, "--headless", "--norestore", f"-env:UserInstallation=file://{d}/profile",
                            "--convert-to", target, "--outdir", d, src], timeout=110, check=True, capture_output=True)
        except Exception: raise ValueError("Conversion failed. The file may be damaged or unsupported.")
        out = os.path.join(d, "input." + target)
        if not os.path.exists(out): raise ValueError("Conversion failed.")
        return f"{_stem(n)}.{target}", open(out, "rb").read()

def _zguard(z, max_total=int(os.getenv("ZIP_MAX_MB", "500")) * MB):
    infos = z.infolist()
    if len(infos) > 5000: raise ValueError("Archive has too many entries.")
    if sum(i.file_size for i in infos) > max_total: raise ValueError("Archive expands to too much data (possible ZIP bomb).")
    for i in infos:
        if i.file_size > MB and i.file_size / max(i.compress_size, 1) > 200: raise ValueError("Suspicious compression ratio (possible ZIP bomb).")
    return infos

def _safe_arc(name):
    p = os.path.normpath(name.replace("\\", "/")).lstrip("/")
    return None if p.startswith("..") or ":" in p.split("/")[0] or p in ("", ".") else p

def _openzip(b):
    try: return zipfile.ZipFile(io.BytesIO(b))
    except Exception: raise ValueError("Not a valid ZIP-based file.")

def validate_output(name, data):
    if not data: raise ValueError("Output is empty.")
    e = ext(name)
    try:
        if e == ".pdf":
            r = PdfReader(io.BytesIO(data))
            if not r.is_encrypted and len(r.pages) == 0: raise ValueError("empty")
        elif e in (".jpg", ".png", ".webp"): Image.open(io.BytesIO(data)).verify()
        elif e in (".zip", ".xlsx", ".docx"):
            z = zipfile.ZipFile(io.BytesIO(data))
            if not any(i.flag_bits & 1 for i in z.infolist()) and z.testzip(): raise ValueError("corrupt")  # encrypted entries can't be CRC-tested without the password
    except Exception: raise ValueError("Generated output failed validation. Please try another file.")


# =============== PDF ===============
@tool("merge-pdf", "Merge PDF", "PDF", "Combine multiple PDF files into one document.", ".pdf", True)
def merge_pdf(items, o):
    if len(items) < 2: raise ValueError("Select at least 2 PDF files.")
    w = PdfWriter()
    for _, b in items:
        for p in _pdf(b).pages: w.add_page(p)
    return "merged.pdf", _pdfbytes(w)

@tool("split-pdf", "Split PDF", "PDF", "Split into single pages or custom ranges (example: 1-3,4-6).", ".pdf",
      opts=[O("mode", "Mode", "select", "every page", ["every page", "ranges"]), O("ranges", "Ranges (ranges mode)", "text", "1-3,4-6")])
def split_pdf(items, o):
    n, b = items[0]; r = _pdf(b); N = len(r.pages)
    groups = [[i] for i in range(N)] if o.get("mode") == "every page" else [_pages(g, N) for g in o.get("ranges", "").replace(" ", "").split(",") if g]
    if not groups or len(groups) > 200: raise ValueError("Enter 1-200 ranges.")
    outs = []
    for k, g in enumerate(groups, 1):
        w = PdfWriter()
        for i in g: w.add_page(r.pages[i])
        outs.append((f"{_stem(n)}_part{k}.pdf", _pdfbytes(w)))
    return outs[0] if len(outs) == 1 else (_stem(n) + "_split.zip", _zip(outs))

@tool("extract-pdf-pages", "Extract PDF Pages", "PDF", "Keep only the pages you choose (example: 1-3,5).", ".pdf",
      opts=[O("pages", "Pages to keep", "text", "1")])
def extract_pages(items, o):
    n, b = items[0]; r = _pdf(b); w = PdfWriter()
    for i in _pages(o.get("pages"), len(r.pages)): w.add_page(r.pages[i])
    return _stem(n) + "_extracted.pdf", _pdfbytes(w)

@tool("delete-pdf-pages", "Delete PDF Pages", "PDF", "Remove unwanted pages from a PDF.", ".pdf",
      opts=[O("pages", "Pages to delete", "text", "1")])
def delete_pages(items, o):
    n, b = items[0]; r = _pdf(b); drop = set(_pages(o.get("pages"), len(r.pages))); w = PdfWriter()
    for i, p in enumerate(r.pages):
        if i not in drop: w.add_page(p)
    if not len(w.pages): raise ValueError("You cannot delete every page.")
    return _stem(n) + "_edited.pdf", _pdfbytes(w)

@tool("reorder-pdf", "Reorder PDF Pages", "PDF", "Rearrange pages with a new order (example: 3,1,2).", ".pdf", rank=1,
      opts=[O("order", "New order (all pages once)", "text", "")])
def reorder_pdf(items, o):
    n, b = items[0]; r = _pdf(b); N = len(r.pages)
    try: order = [int(x) for x in re.split(r"[,\s]+", o.get("order", "").strip()) if x]
    except ValueError: order = []
    if sorted(order) != list(range(1, N + 1)): raise ValueError(f"Enter every page 1-{N} exactly once, e.g. 3,1,2.")
    w = PdfWriter()
    for i in order: w.add_page(r.pages[i - 1])
    return _stem(n) + "_reordered.pdf", _pdfbytes(w)

@tool("rotate-pdf", "Rotate PDF", "PDF", "Rotate all or selected pages.", ".pdf",
      opts=[O("angle", "Angle", "select", "90", ["90", "180", "270"]), O("pages", "Pages (or all)", "text", "all")])
def rotate_pdf(items, o):
    n, b = items[0]; r = _pdf(b); sel = set(_pages(o.get("pages"), len(r.pages))); w = PdfWriter()
    for i, p in enumerate(r.pages):
        if i in sel: p.rotate(int(o.get("angle", 90)))
        w.add_page(p)
    return _stem(n) + "_rotated.pdf", _pdfbytes(w)

@tool("compress-pdf", "Compress PDF", "PDF", "Reduce PDF size. If it cannot be made smaller, the original is returned unchanged.", ".pdf", True,
      [O("level", "Compression", "select", "medium", ["low", "medium", "high"])])
def compress_pdf(items, o):
    cfg = {"low": (200, 150, 80), "medium": (150, 110, 60), "high": (110, 80, 40)}[o.get("level", "medium")]
    def one(n, b):
        doc = _fdoc(b)
        try: doc.rewrite_images(dpi_threshold=cfg[0], dpi_target=cfg[1], quality=cfg[2])
        except Exception: pass
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
        return _stem(n) + "_compressed.pdf", (out if len(out) < len(b) else b)
    return _each(items, one)

@tool("pdf-to-image", "PDF to JPG / PNG", "PDF", "Convert each PDF page into an image.", ".pdf",
      opts=[O("format", "Format", "select", "jpg", ["jpg", "png"]), O("dpi", "Quality (DPI)", "select", "150", ["100", "150", "200"])])
def pdf_to_image(items, o):
    n, b = items[0]; doc = _fdoc(b)
    if doc.page_count > 100: raise ValueError("Maximum 100 pages per conversion.")
    f = o.get("format", "jpg")
    outs = [(f"{_stem(n)}_{i+1}.{f}", p.get_pixmap(dpi=int(o.get("dpi", 150))).tobytes(f)) for i, p in enumerate(doc)]
    return outs[0] if len(outs) == 1 else (_stem(n) + "_images.zip", _zip(outs))

@tool("images-to-pdf", "Images to PDF", "PDF", "Combine JPG, PNG or WebP images into one PDF.", IMG, True)
def images_to_pdf(items, o):
    ims = [_img(b).convert("RGB") for _, b in items]
    buf = io.BytesIO(); ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=100.0)
    return "images.pdf", buf.getvalue()

@tool("protect-pdf", "Protect PDF", "PDF", "Encrypt a PDF with a password.", ".pdf", rank=1, opts=[O("password", "Password", "password")])
def protect_pdf(items, o):
    pw = o.get("password", "")
    if len(pw) < 4: raise ValueError("Password must be at least 4 characters.")
    n, b = items[0]; r = _pdf(b); w = PdfWriter()
    for p in r.pages: w.add_page(p)
    w.encrypt(pw); return _stem(n) + "_protected.pdf", _pdfbytes(w)

@tool("unlock-pdf", "Unlock PDF", "PDF", "Remove the password from a PDF you own (current password required).", ".pdf", rank=1,
      opts=[O("password", "Current password", "password")], note="Works only with the correct password. No password cracking.")
def unlock_pdf(items, o):
    n, b = items[0]; r = _pdf(b, o.get("password")); w = PdfWriter()
    for p in r.pages: w.add_page(p)
    return _stem(n) + "_unlocked.pdf", _pdfbytes(w)

@tool("pdf-to-text", "PDF to Text", "PDF", "Extract text from text-based PDFs.", ".pdf", note="Scanned PDFs have no text layer; OCR is not included.")
def pdf_text(items, o):
    n, b = items[0]
    t = "\n\n".join(p.get_text() for p in _fdoc(b)).strip()
    if not t: raise ValueError("No text found. This PDF may be a scanned image.")
    return _txt(_stem(n) + ".txt", t)

@tool("pdf-to-html", "PDF to HTML", "PDF", "Convert PDF text and layout to an HTML page.", ".pdf", rank=1)
def pdf_html(items, o):
    n, b = items[0]; doc = _fdoc(b)
    body = "\n<hr>\n".join(p.get_text("html") for p in doc)
    return _txt(_stem(n) + ".html", f'<!doctype html><meta charset="utf-8"><title>{_stem(n)}</title>{body}')

@tool("pdf-metadata", "PDF Metadata Viewer / Editor", "PDF", "Leave fields empty to view metadata; fill fields to edit them.", ".pdf", rank=1,
      opts=[O("title", "Title"), O("author", "Author"), O("subject", "Subject"), O("keywords", "Keywords")])
def pdf_meta(items, o):
    n, b = items[0]; r = _pdf(b); edits = {"/" + k.capitalize(): o[k] for k in ("title", "author", "subject", "keywords") if o.get(k)}
    if not edits:
        m = r.metadata or {}
        lines = [f"Pages: {len(r.pages)}", f"Encrypted: {r.is_encrypted}", f"PDF version: {r.pdf_header}"] + [f"{k.strip('/')}: {v}" for k, v in m.items()]
        return _txt(_stem(n) + "_metadata.txt", "\n".join(lines))
    w = PdfWriter(clone_from=r); w.add_metadata(edits); return _stem(n) + "_meta.pdf", _pdfbytes(w)

@tool("watermark-pdf", "Add PDF Watermark", "PDF", "Stamp diagonal text on every page.", ".pdf", rank=2,
      opts=[O("text", "Watermark text", "text", "CONFIDENTIAL"), O("size", "Font size", "number", "48"), O("opacity", "Opacity (10-100)", "number", "30")],
      note="Text watermark supports Latin characters only.")
def watermark_pdf(items, o):
    n, b = items[0]; doc = _fdoc(b); t = (o.get("text") or "").strip()
    if not t: raise ValueError("Enter watermark text.")
    size = max(10, min(150, int(o.get("size") or 48))); op = max(10, min(100, int(o.get("opacity") or 30))) / 100
    for p in doc:
        r = p.rect; pt = fitz.Point(r.width * 0.15, r.height * 0.65)
        p.insert_text(pt, t, fontsize=size, color=(0.5, 0.5, 0.5), fill_opacity=op, morph=(pt, fitz.Matrix(-35)), overlay=True)
    return _stem(n) + "_watermarked.pdf", doc.tobytes(garbage=3, deflate=True)

@tool("pdf-page-numbers", "Add Page Numbers", "PDF", "Add page numbers to every page.", ".pdf", rank=1,
      opts=[O("position", "Position", "select", "bottom-center", ["bottom-center", "bottom-right", "top-right"]), O("start", "Start number", "number", "1")])
def page_numbers(items, o):
    n, b = items[0]; doc = _fdoc(b); st = int(o.get("start") or 1); N = doc.page_count
    for i, p in enumerate(doc):
        t = f"{st + i} / {N + st - 1}"; w = fitz.get_text_length(t, fontsize=10); r = p.rect; pos = o.get("position")
        x = r.width - w - 36 if pos != "bottom-center" else (r.width - w) / 2
        y = 28 if pos == "top-right" else r.height - 24
        p.insert_text(fitz.Point(x, y), t, fontsize=10, color=(0.2, 0.2, 0.2))
    return _stem(n) + "_numbered.pdf", doc.tobytes(garbage=3, deflate=True)

@tool("crop-pdf", "Crop PDF", "PDF", "Trim margins from every page (millimetres).", ".pdf", rank=1,
      opts=[O(k, f"{k.capitalize()} (mm)", "number", "10") for k in ("left", "top", "right", "bottom")])
def crop_pdf(items, o):
    n, b = items[0]; doc = _fdoc(b); mm = 72 / 25.4
    l, t, r_, bt = (float(o.get(k) or 0) * mm for k in ("left", "top", "right", "bottom"))
    for p in doc:
        R = p.rect; box = fitz.Rect(R.x0 + l, R.y0 + t, R.x1 - r_, R.y1 - bt)
        if box.is_empty or box.width < 20 or box.height < 20: raise ValueError("Margins are too large.")
        p.set_cropbox(box)
    return _stem(n) + "_cropped.pdf", doc.tobytes(garbage=3, deflate=True)

@tool("redact-pdf", "Redact PDF", "PDF", "Permanently black out words or emails (text is removed, not just covered).", ".pdf", rank=2,
      opts=[O("terms", "Words/phrases (comma separated)", "text", ""), O("emails", "Also redact email addresses", "select", "no", YN)],
      note="Works on selectable text only. Scanned images are not searched.")
def redact_pdf(items, o):
    n, b = items[0]; doc = _fdoc(b); terms = [t.strip() for t in re.split(r"[\n,;]+", o.get("terms", "")) if t.strip()]
    if not terms and o.get("emails") != "yes": raise ValueError("Enter at least one term or enable email redaction.")
    cnt = 0
    for p in doc:
        tl = list(terms)
        if o.get("emails") == "yes": tl += set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", p.get_text()))
        for t in tl:
            for rect in p.search_for(t): p.add_redact_annot(rect, fill=(0, 0, 0)); cnt += 1
        p.apply_redactions()
    if not cnt: raise ValueError("No matches found; nothing was redacted.")
    return _stem(n) + "_redacted.pdf", doc.tobytes(garbage=4, deflate=True, clean=True)

@tool("compare-pdf", "Compare PDFs", "PDF", "Show text differences between two PDFs.", ".pdf", True, rank=2)
def compare_pdf(items, o):
    _need(items, 2); t = ["\n".join(p.get_text() for p in _fdoc(b)).splitlines() for _, b in items]
    d = list(difflib.unified_diff(t[0], t[1], items[0][0], items[1][0], lineterm="", n=1))
    return _txt("pdf_comparison.txt", "\n".join(d) if d else "No text differences found.")

@tool("sign-pdf", "Sign PDF (image signature)", "PDF", "Place a signature image on a PDF. Upload the PDF and a PNG/JPG signature.", ".pdf,.png,.jpg,.jpeg", True, rank=2,
      opts=[O("page", "Page (number or last)", "text", "last"), O("position", "Position", "select", "bottom-right", ["bottom-right", "bottom-left", "bottom-center"]), O("width", "Width (mm)", "number", "45")],
      note="Visual signature only; not a cryptographic digital signature.")
def sign_pdf(items, o):
    pdfs = [x for x in items if ext(x[0]) == ".pdf"]; ims = [x for x in items if ext(x[0]) != ".pdf"]
    if len(pdfs) != 1 or len(ims) != 1: raise ValueError("Upload exactly one PDF and one signature image.")
    _img(ims[0][1]); doc = _fdoc(pdfs[0][1]); pg = o.get("page", "last")
    idx = doc.page_count - 1 if pg == "last" else int(pg) - 1
    if not 0 <= idx < doc.page_count: raise ValueError("Page out of range.")
    p = doc[idx]; w = float(o.get("width") or 45) * 72 / 25.4; im = _img(ims[0][1]); h = w * im.height / im.width; R = p.rect
    x = {"bottom-right": R.width - w - 40, "bottom-left": 40, "bottom-center": (R.width - w) / 2}[o.get("position", "bottom-right")]
    p.insert_image(fitz.Rect(x, R.height - h - 40, x + w, R.height - 40), stream=ims[0][1])
    return _stem(pdfs[0][0]) + "_signed.pdf", doc.tobytes(garbage=3, deflate=True)

@tool("extract-pdf-images", "Extract PDF Images", "PDF", "Pull out all embedded images from a PDF.", ".pdf", rank=1)
def pdf_images(items, o):
    n, b = items[0]; doc = _fdoc(b); outs = []
    for pi, p in enumerate(doc):
        for k, im in enumerate(p.get_images(full=True)):
            try: x = doc.extract_image(im[0]); outs.append((f"p{pi+1}_{k+1}.{x['ext']}", x["image"]))
            except Exception: continue
            if len(outs) > 300: raise ValueError("Too many images (limit 300).")
    if not outs: raise ValueError("No embedded images found.")
    return _stem(n) + "_images.zip", _zip(outs)

@tool("pdf-tables-to-excel", "PDF Tables to Excel", "PDF", "Detect tables in a PDF and save them as Excel sheets.", ".pdf", rank=2,
      note="Works on text-based PDFs with clear table lines; complex layouts may need manual cleanup.")
def pdf_tables(items, o):
    n, b = items[0]; _pdf(b); sheets = {}
    with pdfplumber.open(io.BytesIO(b)) as pdf:
        for i, p in enumerate(pdf.pages[:100]):
            for k, t in enumerate(p.extract_tables()):
                if t and len(t) > 0: sheets[f"p{i+1}_t{k+1}"] = pd.DataFrame(t)
    if not sheets: raise ValueError("No tables detected in this PDF.")
    buf = io.BytesIO(); used = set()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for k, d in sheets.items(): d.to_excel(w, sheet_name=_uniq(k, used), index=False, header=False)
    return _stem(n) + "_tables.xlsx", buf.getvalue()

@tool("pdf-to-docx", "PDF to Word", "PDF", "Convert a text-based PDF to an editable DOCX.", ".pdf", rank=2, avail=Converter is not None,
      note="Layout fidelity varies: complex tables, scanned pages and unusual fonts may not convert perfectly.")
def pdf_to_docx(items, o):
    n, b = items[0]; _pdf(b)
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "in.pdf"), os.path.join(d, "out.docx"); open(src, "wb").write(b)
        try:
            c = Converter(src); c.convert(out); c.close()
        except Exception: raise ValueError("Could not convert this PDF.")
        return _stem(n) + ".docx", open(out, "rb").read()

@tool("repair-pdf", "Repair PDF", "PDF", "Try to rebuild a damaged PDF.", ".pdf", rank=1, note="Recovery is not guaranteed for severely corrupted files.")
def repair_pdf(items, o):
    n, b = items[0]
    try: doc = fitz.open(stream=b, filetype="pdf"); out = doc.tobytes(garbage=4, clean=True, deflate=True)
    except Exception: raise ValueError("This PDF could not be repaired.")
    if not doc.page_count: raise ValueError("No recoverable pages found.")
    return _stem(n) + "_repaired.pdf", out


# =============== WORD / OFFICE ===============
@tool("docx-to-pdf", "Word to PDF", "Word", "Convert DOCX to PDF.", ".docx", True, avail=bool(SOFFICE))
def docx_to_pdf(items, o): return _each(items, lambda n, b: _lo(n, b, "pdf"))

@tool("docx-to-text", "Word to Text", "Word", "Extract all text (paragraphs and tables) from DOCX.", ".docx", True)
def docx_to_text(items, o):
    def one(n, b):
        try: d = docx.Document(io.BytesIO(b))
        except Exception: raise ValueError(f"{_safe(n)} is not a valid DOCX file.")
        parts = [p.text for p in d.paragraphs] + ["\t".join(c.text for c in r.cells) for t in d.tables for r in t.rows]
        return _txt(_stem(n) + ".txt", "\n".join(parts))
    return _each(items, one)

@tool("text-to-docx", "Text to Word", "Word", "Convert plain text files to DOCX.", ".txt", True)
def text_to_docx(items, o):
    def one(n, b):
        d = docx.Document()
        for line in b.decode("utf-8", "replace").splitlines(): d.add_paragraph(line)
        buf = io.BytesIO(); d.save(buf); return _stem(n) + ".docx", buf.getvalue()
    return _each(items, one)

@tool("office-metadata", "Office Metadata Viewer", "Word", "View author, dates and properties of DOCX, XLSX or PPTX files.", ".docx,.xlsx,.pptx", True, rank=1)
def office_meta(items, o):
    out = []
    for n, b in items:
        z = _openzip(b); lines = [f"== {_safe(n)} =="]
        for part in ("docProps/core.xml", "docProps/app.xml"):
            if part in z.namelist():
                try:
                    for el in ET.fromstring(z.read(part)): lines.append(f"{el.tag.split('}')[-1]}: {(el.text or '').strip()}")
                except ET.ParseError: pass
        out.append("\n".join(lines))
    return _txt("office_metadata.txt", "\n\n".join(out))

@tool("extract-office-images", "Extract Images from Word/Excel/PowerPoint", "Word", "Download all embedded images from DOCX, XLSX or PPTX.", ".docx,.xlsx,.pptx", rank=1)
def office_images(items, o):
    n, b = items[0]; z = _openzip(b); _zguard(z)
    outs = [(os.path.basename(i.filename), z.read(i)) for i in z.infolist() if "/media/" in i.filename and not i.is_dir()]
    if not outs: raise ValueError("No images found in this file.")
    return _stem(n) + "_images.zip", _zip(outs)

@tool("pptx-to-pdf", "PowerPoint to PDF", "PowerPoint", "Convert PPTX to PDF.", ".pptx", True, avail=bool(SOFFICE), note="Fonts missing on the server may change appearance.")
def pptx_to_pdf(items, o): return _each(items, lambda n, b: _lo(n, b, "pdf"))


# =============== EXCEL / CSV ===============
@tool("csv-to-xlsx", "CSV to Excel", "Excel", "Convert CSV files to XLSX.", ".csv", True)
def csv_to_xlsx(items, o): return _each(items, lambda n, b: (_stem(n) + ".xlsx", _xlsx({"Sheet1": _df(n, b)})))

@tool("xlsx-to-csv", "Excel to CSV", "Excel", "Convert every sheet of an XLSX file to CSV.", ".xlsx", True)
def xlsx_to_csv(items, o):
    def one(n, b):
        try: sh = pd.read_excel(io.BytesIO(b), sheet_name=None, engine="openpyxl")
        except Exception: raise ValueError(f"Could not read {_safe(n)}.")
        outs = [(f"{_stem(n)}_{s}.csv", d.to_csv(index=False).encode("utf-8-sig")) for s, d in sh.items()]
        return outs[0] if len(outs) == 1 else (_stem(n) + "_csv.zip", _zip(outs))
    return _each(items, one)

@tool("excel-to-pdf", "Excel to PDF", "Excel", "Convert XLSX to PDF.", ".xlsx", True, avail=bool(SOFFICE))
def excel_to_pdf(items, o): return _each(items, lambda n, b: _lo(n, b, "pdf"))

@tool("clean-spreadsheet", "Clean Spreadsheet", "Excel", "Remove duplicates, empty rows/columns and extra spaces.", ".csv,.xlsx", True,
      [O("dups", "Remove duplicate rows", "select", "yes", YN), O("empty", "Remove empty rows & columns", "select", "yes", YN), O("trim", "Trim whitespace", "select", "yes", YN)])
def clean_sheet(items, o):
    def one(n, b):
        df = _df(n, b)
        if o.get("trim") == "yes": df = df.map(lambda v: v.strip() if isinstance(v, str) else v)
        if o.get("empty") == "yes": df = df.replace("", pd.NA).dropna(how="all").dropna(axis=1, how="all")
        if o.get("dups") == "yes": df = df.drop_duplicates()
        return _like(n, df, "clean")
    return _each(items, one)

@tool("merge-excel", "Merge Excel / CSV Files", "Excel", "Combine files as separate sheets or stack all rows.", ".csv,.xlsx", True, rank=1,
      opts=[O("mode", "Mode", "select", "separate sheets", ["separate sheets", "stack rows"])])
def merge_excel(items, o):
    if len(items) < 2: raise ValueError("Select at least 2 files.")
    if o.get("mode") == "stack":
        return "merged.xlsx", _xlsx({"Merged": pd.concat([_df(n, b) for n, b in items], ignore_index=True)})
    sheets = {}
    for n, b in items:
        if ext(n) == ".csv": sheets[_stem(n)] = _df(n, b)
        else:
            try:
                for s, d in pd.read_excel(io.BytesIO(b), sheet_name=None, engine="openpyxl").items(): sheets[f"{_stem(n)}_{s}"] = d
            except Exception: raise ValueError(f"Could not read {_safe(n)}.")
    return "merged.xlsx", _xlsx(sheets)

@tool("split-sheets", "Split Excel Sheets", "Excel", "Save every sheet of a workbook as its own file.", ".xlsx", rank=1)
def split_sheets(items, o):
    n, b = items[0]
    try: sh = pd.read_excel(io.BytesIO(b), sheet_name=None, engine="openpyxl")
    except Exception: raise ValueError("Could not read this Excel file.")
    return _stem(n) + "_sheets.zip", _zip([(f"{_stem(n)}_{s}.xlsx", _xlsx({s: d})) for s, d in sh.items()])

@tool("find-replace-sheet", "Find & Replace in Spreadsheet", "Excel", "Replace text in all text cells.", ".csv,.xlsx", rank=1,
      opts=[O("find", "Find"), O("replace", "Replace with")])
def find_replace(items, o):
    n, b = items[0]; f = o.get("find", "")
    if not f: raise ValueError("Enter text to find.")
    return _like(n, _df(n, b).map(lambda v: v.replace(f, o.get("replace", "")) if isinstance(v, str) else v), "replaced")

def _col(df, c):
    if c not in df.columns: raise ValueError("Column not found. Available: " + ", ".join(map(str, df.columns))[:300])
    return c

@tool("sort-sheet", "Sort Spreadsheet", "Excel", "Sort rows by a column.", ".csv,.xlsx", rank=1,
      opts=[O("column", "Column name"), O("order", "Order", "select", "ascending", ["ascending", "descending"])])
def sort_sheet(items, o):
    n, b = items[0]; df = _df(n, b)
    return _like(n, df.sort_values(_col(df, o.get("column", "")), ascending=o.get("order") == "ascending"), "sorted")

@tool("filter-sheet", "Filter Spreadsheet", "Excel", "Keep rows matching a condition.", ".csv,.xlsx", rank=1,
      opts=[O("column", "Column name"), O("op", "Condition", "select", "contains", ["contains", "equals", ">", "<"]), O("value", "Value")])
def filter_sheet(items, o):
    n, b = items[0]; df = _df(n, b); c = _col(df, o.get("column", "")); v = o.get("value", ""); op = o.get("op")
    if op == "contains": m = df[c].astype(str).str.contains(v, case=False, regex=False)
    elif op == "equals": m = df[c].astype(str) == v
    else:
        try: x = pd.to_numeric(df[c], errors="coerce"); m = x > float(v) if op == ">" else x < float(v)
        except ValueError: raise ValueError("Value must be a number for > and <.")
    if not m.any(): raise ValueError("No rows match this condition.")
    return _like(n, df[m], "filtered")

@tool("spreadsheet-stats", "Spreadsheet Statistics", "Excel", "Row/column counts, missing values, min/max/mean per column.", ".csv,.xlsx", rank=1)
def sheet_stats(items, o):
    n, b = items[0]; df = _df(n, b); s = df.describe(include="all").T; s["missing"] = df.isna().sum()
    return _stem(n) + "_stats.csv", f"rows,{len(df)}\ncolumns,{df.shape[1]}\n\n".encode() + s.to_csv().encode("utf-8-sig")

@tool("compare-excel", "Compare Excel Files", "Excel", "List cell differences between two spreadsheets.", ".csv,.xlsx", True, rank=2)
def compare_excel(items, o):
    _need(items, 2); a, b = _df(*items[0]).astype(str), _df(*items[1]).astype(str); from openpyxl.utils import get_column_letter as L
    diffs = []
    for r in range(max(len(a), len(b))):
        for c in range(max(a.shape[1], b.shape[1])):
            x = a.iat[r, c] if r < len(a) and c < a.shape[1] else "<empty>"; y = b.iat[r, c] if r < len(b) and c < b.shape[1] else "<empty>"
            if x != y: diffs.append(f"{L(c+1)}{r+2}: {x} -> {y}")
            if len(diffs) >= 2000: break
        if len(diffs) >= 2000: break
    return _txt("excel_comparison.txt", f"{len(diffs)} difference(s)\n" + "\n".join(diffs) if diffs else "Files are identical.")

@tool("excel-formula-viewer", "Excel Formula Viewer", "Excel", "List every formula in a workbook.", ".xlsx", rank=1)
def formula_viewer(items, o):
    from openpyxl import load_workbook
    n, b = items[0]
    try: wb = load_workbook(io.BytesIO(b), data_only=False)
    except Exception: raise ValueError("Could not read this workbook.")
    out = [f"{ws.title}!{c.coordinate}: {c.value}" for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"][:5000]
    return _txt(_stem(n) + "_formulas.txt", "\n".join(out) if out else "No formulas found.")


# =============== IMAGES ===============
@tool("compress-image", "Image Compressor", "Image", "Shrink JPG, PNG and WebP images.", IMG, True, [O("quality", "Quality (30-95)", "number", "75")])
def compress_image(items, o):
    q = max(30, min(95, int(o.get("quality") or 75)))
    def one(n, b):
        im = _img(b); data, e = _encode(im, _fmt(im), q); return f"{_stem(n)}_compressed.{e}", (data if len(data) < len(b) else b)
    return _each(items, one)

@tool("resize-image", "Image Resizer", "Image", "Resize by width and/or height.", IMG, True, [O("width", "Width px", "number", ""), O("height", "Height px", "number", "")])
def resize_image(items, o):
    w, h = int(o.get("width") or 0), int(o.get("height") or 0)
    if not (w or h) or max(w, h) > 8000: raise ValueError("Enter a width or height (max 8000).")
    def one(n, b):
        im = _img(b); W, H = im.size
        nw, nh = (w, int(H * w / W)) if w and not h else (int(W * h / H), h) if h and not w else (w, h)
        data, e = _encode(im.resize((max(1, nw), max(1, nh)), Image.LANCZOS), _fmt(im)); return f"{_stem(n)}_{nw}x{nh}.{e}", data
    return _each(items, one)

@tool("crop-image", "Image Cropper", "Image", "Crop using pixel coordinates.", IMG, rank=1,
      opts=[O("left", "Left px", "number", "0"), O("top", "Top px", "number", "0"), O("width", "Width px", "number", ""), O("height", "Height px", "number", "")])
def crop_image(items, o):
    n, b = items[0]; im = _img(b); l, t = int(o.get("left") or 0), int(o.get("top") or 0)
    w, h = int(o.get("width") or im.width - l), int(o.get("height") or im.height - t)
    if l < 0 or t < 0 or w < 1 or h < 1 or l + w > im.width or t + h > im.height: raise ValueError(f"Crop area must fit inside {im.width}x{im.height}px.")
    data, e = _encode(im.crop((l, t, l + w, t + h)), _fmt(im)); return f"{_stem(n)}_cropped.{e}", data

@tool("convert-image", "Image Converter (JPG/PNG/WebP/HEIC)", "Image", "Convert between JPG, PNG, WebP. HEIC input supported.", IMG, True,
      [O("format", "Convert to", "select", "webp", ["jpg", "png", "webp"])])
def convert_image(items, o):
    def one(n, b): data, e = _encode(_img(b), o.get("format", "webp")); return f"{_stem(n)}.{e}", data
    return _each(items, one)

@tool("rotate-flip-image", "Rotate / Flip Image", "Image", "Rotate or mirror images.", IMG, True, rank=1,
      opts=[O("angle", "Rotate", "select", "90", ["0", "90", "180", "270"]), O("flip", "Flip", "select", "none", ["none", "horizontal", "vertical"])])
def rotate_flip(items, o):
    def one(n, b):
        im = _img(b); fm = _fmt(im)
        if o.get("angle") != "0": im = im.rotate(-int(o["angle"]), expand=True)
        if o.get("flip") == "horizontal": im = im.transpose(Image.FLIP_LEFT_RIGHT)
        if o.get("flip") == "vertical": im = im.transpose(Image.FLIP_TOP_BOTTOM)
        data, e = _encode(im, fm); return f"{_stem(n)}_edited.{e}", data
    return _each(items, one)

@tool("watermark-image", "Image Watermark", "Image", "Add a text watermark to images.", IMG, True, rank=2,
      opts=[O("text", "Text", "text", "© Your Name"), O("position", "Position", "select", "bottom-right", ["bottom-right", "bottom-left", "center"]), O("opacity", "Opacity (10-100)", "number", "50")],
      note="Latin characters only.")
def watermark_image(items, o):
    t = (o.get("text") or "").strip()
    if not t: raise ValueError("Enter watermark text.")
    op = int(max(10, min(100, int(o.get("opacity") or 50))) * 2.55)
    def one(n, b):
        im = _img(b); fm = _fmt(im); base = im.convert("RGBA"); lay = Image.new("RGBA", base.size, (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
        sz = max(14, base.width // 18)
        try: f = ImageFont.truetype("DejaVuSans.ttf", sz)
        except Exception: f = ImageFont.load_default(size=sz)
        bb = d.textbbox((0, 0), t, font=f); tw, th = bb[2] - bb[0], bb[3] - bb[1]; m = sz // 2
        pos = {"bottom-right": (base.width - tw - m, base.height - th - m * 2), "bottom-left": (m, base.height - th - m * 2), "center": ((base.width - tw) // 2, (base.height - th) // 2)}[o.get("position", "bottom-right")]
        d.text(pos, t, font=f, fill=(255, 255, 255, op), stroke_width=max(1, sz // 14), stroke_fill=(0, 0, 0, op))
        data, e = _encode(Image.alpha_composite(base, lay), fm); return f"{_stem(n)}_watermarked.{e}", data
    return _each(items, one)

@tool("image-info", "Image Metadata & Dimensions", "Image", "Show format, dimensions, mode, DPI and EXIF data.", IMG, True, rank=1)
def image_info(items, o):
    out = []
    for n, b in items:
        im = _img(b); L = [f"== {_safe(n)} ==", f"Format: {im.format}", f"Size: {im.width}x{im.height}px", f"Mode: {im.mode}", f"File size: {len(b)} bytes", f"DPI: {im.info.get('dpi', 'n/a')}"]
        ex = im.getexif()
        for k, v in ex.items(): L.append(f"EXIF {ExifTags.TAGS.get(k, k)}: {str(v)[:100]}")
        if ex.get_ifd(0x8825): L.append("WARNING: contains GPS location data")
        out.append("\n".join(L))
    return _txt("image_info.txt", "\n\n".join(out))

@tool("remove-exif", "Remove EXIF Metadata", "Image", "Strip location, camera and other hidden metadata.", IMG, True)
def remove_exif(items, o):
    def one(n, b):
        im = _img(b); c = Image.new(im.mode, im.size); c.putdata(list(im.getdata())); data, e = _encode(c, _fmt(im), 95); return f"{_stem(n)}_clean.{e}", data
    return _each(items, one)


# =============== ARCHIVE / FILES ===============
@tool("create-zip", "ZIP Creator", "Archive", "Put multiple files into one ZIP.", "*", True)
def create_zip(items, o): return "files.zip", _zip(items)

@tool("zip-protect", "Password-Protect ZIP (AES-256)", "Archive", "Create an AES-256 encrypted ZIP.", "*", True, rank=1, avail=pyzipper is not None,
      opts=[O("password", "Password", "password")])
def zip_protect(items, o):
    pw = o.get("password", "")
    if len(pw) < 6: raise ValueError("Password must be at least 6 characters.")
    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as z:
        z.setpassword(pw.encode())
        for n, b in items: z.writestr(_safe(n), b)
    return "protected.zip", buf.getvalue()

@tool("zip-info", "Archive Information Viewer", "Archive", "List ZIP contents, sizes, ratios and unsafe paths.", ".zip", rank=1)
def zip_info(items, o):
    n, b = items[0]; z = _openzip(b); infos = _zguard(z)
    L = [f"{_safe(n)}: {len(infos)} entries, {sum(i.file_size for i in infos)} bytes uncompressed"]
    for i in infos:
        flag = " [UNSAFE PATH]" if _safe_arc(i.filename) is None else ""; flag += " [ENCRYPTED]" if i.flag_bits & 1 else ""
        L.append(f"{i.file_size:>12}  {i.compress_size:>12}  {i.filename}{flag}")
    return _txt(_stem(n) + "_info.txt", "\n".join(L))

@tool("unlock-zip", "Safe ZIP Re-pack / Unlock", "Archive", "Validate a ZIP, drop unsafe paths and remove the password (if you know it).", ".zip", rank=1, avail=pyzipper is not None,
      opts=[O("password", "Password (if encrypted)", "password")], note="Entries with unsafe paths are removed. Wrong passwords are rejected; no cracking.")
def unlock_zip(items, o):
    n, b = items[0]
    try: z = pyzipper.AESZipFile(io.BytesIO(b))
    except Exception: raise ValueError("Not a valid ZIP file.")
    _zguard(z)
    if o.get("password"): z.setpassword(o["password"].encode())
    outs = []
    for i in z.infolist():
        p = _safe_arc(i.filename)
        if p is None or i.is_dir(): continue
        try: outs.append((p, z.read(i)))
        except RuntimeError: raise ValueError("Wrong or missing password.")
        except Exception: raise ValueError("Could not read an entry (corrupt archive?).")
    if not outs: raise ValueError("Nothing safe to extract.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        for p, d in outs: zo.writestr(p, d)
    return _stem(n) + "_safe.zip", buf.getvalue()

@tool("file-checksum", "File Checksum Generator", "Archive", "MD5, SHA-1 and SHA-256 for any files (integrity check).", "*", True)
def checksum(items, o):
    L = []
    for n, b in items: L.append(f"{_safe(n)}  ({len(b)} bytes)\n  MD5:    {hashlib.md5(b).hexdigest()}\n  SHA1:   {hashlib.sha1(b).hexdigest()}\n  SHA256: {hashlib.sha256(b).hexdigest()}")
    return _txt("checksums.txt", "\n".join(L))

_MAGIC = [(b"%PDF-", "PDF document"), (b"\x89PNG", "PNG image"), (b"\xff\xd8\xff", "JPEG image"), (b"GIF8", "GIF image"), (b"RIFF", "RIFF (WebP/WAV/AVI)"),
          (b"PK\x03\x04", "ZIP-based (zip/docx/xlsx/pptx/apk)"), (b"Rar!", "RAR archive"), (b"7z\xbc\xaf", "7-Zip archive"), (b"\x1f\x8b", "GZIP"), (b"MZ", "Windows executable"),
          (b"\x7fELF", "Linux executable"), (b"SQLite format", "SQLite database"), (b"\xd0\xcf\x11\xe0", "Legacy Office (doc/xls/ppt)")]

@tool("file-type-detector", "File Type Detector", "Archive", "Detect real file type by signature (not just extension).", "*", True)
def file_type(items, o):
    L = []
    for n, b in items:
        t = next((d for s, d in _MAGIC if b.startswith(s)), "Text/unknown" if b"\x00" not in b[:2048] else "Binary/unknown")
        if b[4:8] == b"ftyp": t = "ISO media (MP4/HEIC)"
        L.append(f"{_safe(n)}: {t} | extension {ext(n) or 'none'} | {len(b)} bytes")
    return _txt("file_types.txt", "\n".join(L))

@tool("find-duplicates", "Duplicate File Detector", "Archive", "Find identical files by content hash.", "*", True, rank=1)
def find_dups(items, o):
    g = {}
    for n, b in items: g.setdefault(hashlib.sha256(b).hexdigest(), []).append(_safe(n))
    d = [v for v in g.values() if len(v) > 1]
    return _txt("duplicates.txt", "\n".join(" = ".join(v) for v in d) if d else "No duplicates found.")

@tool("batch-rename", "Batch File Renamer", "Archive", "Rename many files with a prefix and numbering; download as ZIP.", "*", True, rank=1,
      opts=[O("prefix", "Prefix", "text", "file_"), O("start", "Start number", "number", "1"), O("lower", "Lowercase extension", "select", "yes", YN)])
def batch_rename(items, o):
    s = int(o.get("start") or 1); pre = _safe(o.get("prefix", ""))
    return "renamed.zip", _zip([(f"{pre}{s+i:03d}{ext(n).lower() if o.get('lower') == 'yes' else os.path.splitext(n)[1]}", b) for i, (n, b) in enumerate(items)])

@tool("split-file", "File Splitter", "Archive", "Split a large file into parts (rejoin with cat or copy /b).", "*", rank=1, opts=[O("mb", "Part size (MB)", "number", "5")])
def split_file(items, o):
    n, b = items[0]; sz = max(1, int(o.get("mb") or 5)) * MB
    parts = [b[i:i + sz] for i in range(0, len(b), sz)]
    if len(parts) < 2: raise ValueError("File is smaller than the part size.")
    if len(parts) > 500: raise ValueError("Too many parts; increase part size.")
    return _stem(n) + "_parts.zip", _zip([(f"{_safe(n)}.part{i+1:03d}", p) for i, p in enumerate(parts)])

@tool("compare-text", "Text File Comparison", "Archive", "Line-by-line diff of two text files.", ".txt,.csv", True)
def compare_text(items, o):
    _need(items, 2); t = [b.decode("utf-8", "replace").splitlines() for _, b in items]
    d = list(difflib.unified_diff(t[0], t[1], items[0][0], items[1][0], lineterm="", n=2))
    return _txt("text_comparison.txt", "\n".join(d) if d else "Files are identical.")
