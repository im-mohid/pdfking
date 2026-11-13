"""Smart compression: downsample + recompress raster images; text/vectors stay untouched."""
from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF
from PIL import Image

from app.utils import PDFError, human_size, validate_pdf

# level -> (target DPI, JPEG quality)
PRESETS = {"light": (150, 80), "balanced": (110, 65), "strong": (72, 45)}
MIN_IMG_BYTES = 20_000        # smaller images are not worth touching
MAX_PIXELS = 60_000_000       # skip monsters (RAM safety)


def _recompress(doc: fitz.Document, page: fitz.Page, xref: int, meta: tuple,
                width_in: float, dpi: int, quality: int, gray: bool) -> bool:
    """Recompress one image. Returns True if replaced, False if skipped/kept."""
    smask, w, h, bpc = meta
    # keep: transparent images, 1-bit/mask images, huge or tiny images
    if smask or bpc == 1 or w * h > MAX_PIXELS or w * h < 10_000:
        return False
    raw_len = len(doc.xref_stream_raw(xref))
    if raw_len < MIN_IMG_BYTES:
        return False

    pix = fitz.Pixmap(doc, xref)
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n not in (1, 3):   # CMYK / other -> RGB
        pix = fitz.Pixmap(fitz.csRGB, pix)
    im = Image.frombytes("L" if pix.n == 1 else "RGB", (pix.width, pix.height), pix.samples)
    del pix

    # flat graphics (<=256 colors: logos, charts) stay lossless
    if im.mode == "RGB" and im.getcolors(256):
        return False

    # downsample only if effective DPI on the page is clearly above target
    if width_in and width_in > 0:
        eff = im.width / width_in
        if eff > dpi * 1.1:
            s = dpi / eff
            im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.LANCZOS)
    if gray and im.mode != "L":
        im = im.convert("L")

    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    data = buf.getvalue()
    if len(data) >= raw_len * 0.92:          # not worth it
        return False
    page.replace_image(xref, stream=data)
    return True


def compress_pdf(pdf_path: Path, out_path: Path, *, level: str = "balanced",
                 image_dpi: int | None = None, jpeg_quality: int | None = None,
                 grayscale: bool = False, strip_metadata: bool = True,
                 on_progress: Callable[[int, int], None] | None = None) -> dict:
    """
    level: "light" | "balanced" | "strong" (image_dpi / jpeg_quality override the preset).
    Returns {level, dpi, quality, original_size, original_human, compressed_size,
             compressed_human, saved_bytes, saved_percent, pages, images_total,
             images_recompressed, images_kept, warnings[], output}.
    """
    if level not in PRESETS:
        raise PDFError(f"Unknown level '{level}'. Use: {', '.join(PRESETS)}")
    dpi, quality = PRESETS[level]
    dpi = image_dpi if image_dpi is not None else dpi
    quality = jpeg_quality if jpeg_quality is not None else quality
    if not 36 <= dpi <= 600:
        raise PDFError("image_dpi must be between 36 and 600")
    if not 10 <= quality <= 95:
        raise PDFError("jpeg_quality must be between 10 and 95")

    validate_pdf(pdf_path)
    original = pdf_path.stat().st_size
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise PDFError(f"Cannot open PDF: {exc}") from exc

    warnings: list[str] = []
    try:
        if doc.needs_pass:
            raise PDFError("PDF is password-protected")
        pages = len(doc)

        # pass 1 (metadata only): which images exist, on which page, how large when displayed
        meta: dict[int, tuple] = {}
        first_page: dict[int, int] = {}
        max_w: dict[int, float] = {}
        for pno in range(pages):
            page = doc[pno]
            for img in page.get_images(full=True):
                xref = img[0]
                meta.setdefault(xref, (img[1], img[2], img[3], img[4]))
                first_page.setdefault(xref, pno)
                try:
                    for r in page.get_image_rects(xref):
                        max_w[xref] = max(max_w.get(xref, 0.0), r.width / 72.0)
                except Exception:
                    pass

        # pass 2: one image at a time (bounded RAM)
        todo = sorted(meta)
        done_n = kept = 0
        for k, xref in enumerate(todo, 1):
            try:
                if _recompress(doc, doc[first_page[xref]], xref, meta[xref],
                               max_w.get(xref, 0.0), dpi, quality, grayscale):
                    done_n += 1
                else:
                    kept += 1
            except Exception as exc:
                kept += 1
                if len(warnings) < 5:
                    warnings.append(f"Image {xref} kept as-is ({exc})")
            if on_progress:
                on_progress(k, len(todo))
        if not todo:
            warnings.append("No raster images found; only PDF structure was optimized")

        if strip_metadata:
            doc.set_metadata({})
            doc.del_xml_metadata()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc.save(out_path, garbage=4, deflate=True, deflate_fonts=True, clean=True, use_objstms=1)
        except TypeError:   # older PyMuPDF without use_objstms
            doc.save(out_path, garbage=4, deflate=True, clean=True)
    except PDFError:
        raise
    except Exception as exc:
        raise PDFError(f"Compression failed: {exc}") from exc
    finally:
        doc.close()

    new = out_path.stat().st_size
    if new >= original:     # never hand back a bigger file
        shutil.copyfile(pdf_path, out_path)
        new = original
        warnings.append("File is already well optimized; original kept")
    saved = original - new
    return {
        "level": level, "dpi": dpi, "quality": quality, "pages": pages,
        "original_size": original, "original_human": human_size(original),
        "compressed_size": new, "compressed_human": human_size(new),
        "saved_bytes": saved, "saved_percent": round(saved / original * 100, 1) if original else 0.0,
        "images_total": len(todo), "images_recompressed": done_n, "images_kept": kept,
        "warnings": warnings, "output": out_path.name,
          }
