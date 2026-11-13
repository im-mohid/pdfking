"""OCR engine: Tesseract (eng+ben) with OpenCV preprocessing.

- Page-by-page processing (low RAM), pages rendered at config.OCR_DPI
- Deskew (projection-profile search) + optional median denoise
- Output: searchable PDF (invisible text layer), TXT, DOCX (any combination)
- Pages that already have real text are copied as-is (hybrid PDFs)
- A failed/timed-out page is kept as the original page and reported as a warning
- Single OCR pass per page: TXT/DOCX text is read back from the searchable PDF layer
"""
import re
from pathlib import Path
from typing import Callable

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from docx import Document
from docx.oxml.ns import qn
from PIL import Image

from app import config
from app.services.tables import _parse_pages
from app.utils import PDFError

PAGE_TIMEOUT = 180  # seconds per page
_LANG_RE = re.compile(r"^[a-z_]{3,12}(\+[a-z_]{3,12})*$")
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------------------------------------------------------------- helpers
def _check_langs(langs: str | None) -> str:
    langs = (langs or config.OCR_LANGS).strip().lower()
    if not _LANG_RE.match(langs):
        raise PDFError("Invalid OCR language code.")
    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception:
        raise PDFError("Tesseract OCR engine is not available on the server.")
    missing = [l for l in langs.split("+") if l not in installed]
    if missing:
        raise PDFError(f"OCR language not installed: {', '.join(missing)}")
    return langs


def _render(page, dpi: int, color: bool) -> np.ndarray:
    """Render a page to a numpy array (gray HxW or RGB HxWx3)."""
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB if color else fitz.csGRAY, alpha=False)
    raw = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
    arr = raw[:, : pix.width * pix.n].reshape(pix.height, pix.width, pix.n)
    return arr[:, :, 0].copy() if pix.n == 1 else arr.copy()


def _skew_angle(gray: np.ndarray) -> float:
    """Estimate skew (degrees) by maximising horizontal-projection sharpness."""
    h, w = gray.shape
    s = 800 / max(h, w)
    small = cv2.resize(gray, (max(1, int(w * s)), max(1, int(h * s))),
                       interpolation=cv2.INTER_AREA) if s < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if cv2.countNonZero(bw) < 0.0005 * bw.size:  # blank page
        return 0.0
    hh, ww = bw.shape
    center = (ww / 2, hh / 2)

    def score(a: float) -> float:
        M = cv2.getRotationMatrix2D(center, a, 1.0)
        rot = cv2.warpAffine(bw, M, (ww, hh), flags=cv2.INTER_NEAREST)
        prof = rot.sum(axis=1, dtype=np.float64)
        return float(np.sum(np.diff(prof) ** 2))

    best = max(np.arange(-10, 10.01, 1.0), key=score)          # coarse
    best = max(np.arange(best - 1, best + 1.01, 0.2), key=score)  # fine
    return float(best)


def _rotate(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def _preprocess(img: np.ndarray, deskew: bool, denoise: bool) -> tuple[np.ndarray, float]:
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    angle = _skew_angle(gray) if deskew else 0.0
    if abs(angle) >= 0.2:
        img = _rotate(img, angle)
    if denoise:
        img = cv2.medianBlur(img, 3)
    return img, angle


def _write_docx(path: Path, texts: list[str], page_nums: list[int]) -> None:
    doc = Document()
    # Complex-script font so Bengali renders properly in Word
    fonts = doc.styles["Normal"].element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:cs"), "Nirmala UI")
    for i, text in enumerate(texts):
        if i:
            doc.add_page_break()
        paras, cur = [], []
        for line in _XML_BAD.sub("", text).splitlines():
            line = line.strip()
            if not line:
                if cur:
                    paras.append(" ".join(cur))
                    cur = []
                continue
            cur.append(line)
            if line[-1] in ".?!।:;":  # sentence end -> paragraph break
                paras.append(" ".join(cur))
                cur = []
        if cur:
            paras.append(" ".join(cur))
        for p in paras or [""]:
            doc.add_paragraph(p)
    doc.save(path)


# --------------------------------------------------------------- public API
def ocr_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    langs: str | None = None,          # default config.OCR_LANGS ("eng+ben")
    pages: str | None = None,          # "1-3,5"; empty = all
    outputs: tuple[str, ...] = ("pdf",),  # any of: pdf, txt, docx, all
    deskew: bool = True,
    denoise: bool = True,
    color: bool = False,               # keep colour in output PDF (bigger file)
    skip_text_pages: bool = True,      # copy pages that already contain text
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """OCR a PDF. Returns {pages_total, pages_ocr, pages_native, words, langs, files[], warnings[]}."""
    langs = _check_langs(langs)
    outs = set(outputs)
    if "all" in outs:
        outs = {"pdf", "txt", "docx"}
    if not outs or outs - {"pdf", "txt", "docx"}:
        raise PDFError("outputs must be any of: pdf, txt, docx, all")

    try:
        src = fitz.open(pdf_path)
    except Exception:
        raise PDFError("PDF could not be opened (corrupted or unsupported).")

    dpi = config.OCR_DPI
    warnings: list[str] = []
    texts: list[str] = []
    n_ocr = n_native = 0
    tmp = out_dir / "_ocr_tmp.jpg"

    with src:
        wanted = _parse_pages(pages, src.page_count)
        if len(wanted) > config.MAX_PAGES_OCR:
            raise PDFError(f"Too many pages for OCR ({len(wanted)}). Limit is {config.MAX_PAGES_OCR} - use a page range.")
        out = fitz.open()
        try:
            for step, idx in enumerate(wanted, 1):
                try:
                    native = src[idx].get_text("text").strip() if skip_text_pages else ""
                    if len(native) >= 30:
                        out.insert_pdf(src, from_page=idx, to_page=idx)
                        texts.append(native)
                        n_native += 1
                    else:
                        img, _angle = _preprocess(_render(src[idx], dpi, color), deskew, denoise)
                        # JPEG on disk -> Tesseract embeds it as-is (small output PDF)
                        Image.fromarray(img).save(tmp, "JPEG", quality=75, dpi=(dpi, dpi))
                        pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                            str(tmp), lang=langs, extension="pdf",
                            config=f"--psm 3 --oem 1 --dpi {dpi}", timeout=PAGE_TIMEOUT)
                        with fitz.open(stream=pdf_bytes, filetype="pdf") as one:
                            texts.append(one[0].get_text("text").strip())
                            out.insert_pdf(one)
                        n_ocr += 1
                except Exception as e:  # keep original page, continue
                    warnings.append(f"Page {idx + 1}: OCR failed ({type(e).__name__})")
                    out.insert_pdf(src, from_page=idx, to_page=idx)
                    texts.append("")
                if on_progress:
                    on_progress(step, len(wanted))

            if n_ocr + n_native == 0:
                raise PDFError("OCR failed on every page.")

            stem = re.sub(r"[^\w\-]+", "_", Path(pdf_path).stem, flags=re.UNICODE)[:60] or "document"
            nums = [i + 1 for i in wanted]
            files: list[str] = []

            if "pdf" in outs:
                p = out_dir / f"{stem}_ocr.pdf"
                out.save(p, garbage=3, deflate=True)
                files.append(p.name)
            if "txt" in outs:
                p = out_dir / f"{stem}_ocr.txt"
                p.write_text("\n\n".join(f"--- Page {n} ---\n{t}" for n, t in zip(nums, texts)),
                             encoding="utf-8")
                files.append(p.name)
            if "docx" in outs:
                p = out_dir / f"{stem}_ocr.docx"
                _write_docx(p, texts, nums)
                files.append(p.name)
        finally:
            out.close()
            tmp.unlink(missing_ok=True)

    return {
        "pages_total": len(wanted),
        "pages_ocr": n_ocr,
        "pages_native": n_native,
        "words": sum(len(t.split()) for t in texts),
        "langs": langs,
        "files": files,
        "warnings": warnings,
  }
