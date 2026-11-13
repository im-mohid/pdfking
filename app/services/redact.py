"""PII auto-redaction (true redaction: text/pixels are removed, not just covered).

- Regex PII types from config.PII_PATTERNS + custom regex + literal terms (names, addresses)
- Bengali digits (০-৯) are normalised for matching, so Bengali phone/NID numbers are caught
- Credit cards are Luhn-validated (fewer false positives)
- Scanned pages (no text layer) are OCR'd to locate PII, then image pixels are erased
- Modes: "black" (solid box, most secure) | "blur" (mosaic over erased area)
- Metadata / embedded files / thumbnails scrubbed; mailto/URL links containing PII removed
- Any page failure ABORTS the job (never returns a half-redacted file)
"""
import io
import re
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from PIL import Image, ImageFilter

from app import config
from app.services.ocr import _check_langs
from app.utils import PDFError

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")  # 1:1 -> offsets stay aligned
_DIGIT_GUARD = {"bd_phone", "intl_phone"}  # must not be glued to other digits
_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")  # crude ReDoS guard
_OCR_DPI = 200


# ------------------------------------------------------------------ matching
def _compile(types: list[str], custom_regex: str | None) -> dict[str, re.Pattern]:
    pats: dict[str, re.Pattern] = {}
    for t in types:
        p = config.PII_PATTERNS[t]
        if t in _DIGIT_GUARD:
            p = rf"(?<!\d)(?:{p})(?!\d)"
        pats[t] = re.compile(p)
    if custom_regex:
        if len(custom_regex) > 200 or _NESTED_QUANT.search(custom_regex):
            raise PDFError("Custom pattern is too long or too complex.")
        try:
            pats["custom"] = re.compile(custom_regex, re.IGNORECASE)
        except re.error:
            raise PDFError("Custom regex is invalid.")
    return pats


def _luhn(num: str) -> bool:
    d = [int(c) for c in num][::-1]
    return (sum(d[0::2]) + sum(sum(divmod(x * 2, 10)) for x in d[1::2])) % 10 == 0


def _scan(text: str, pats: dict[str, re.Pattern]):
    """Yield (kind, start, end, original_text) for every PII match."""
    norm = text.translate(BN_DIGITS)
    for kind, rx in pats.items():
        for m in rx.finditer(norm):
            digits = re.sub(r"\D", "", m.group())
            if kind == "credit_card" and not (13 <= len(digits) <= 19 and _luhn(digits)):
                continue
            if kind == "nid" and len(set(digits)) == 1:  # 0000000000 etc.
                continue
            raw = text[m.start():m.end()].strip()
            if raw:
                yield kind, m.start(), m.end(), raw


def _mask(s: str) -> str:
    return "*" * len(s) if len(s) <= 4 else s[:2] + "*" * (len(s) - 4) + s[-2:]


# ----------------------------------------------------------------------- OCR
def _ocr_words(page, langs: str) -> list[tuple[str, fitz.Rect]]:
    """OCR a scanned page -> [(word, rect in unrotated page coordinates)]."""
    pix = page.get_pixmap(dpi=_OCR_DPI, colorspace=fitz.csGRAY, alpha=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    d = pytesseract.image_to_data(img, lang=langs, config="--psm 3",
                                  output_type=pytesseract.Output.DICT, timeout=180)
    k = 72 / _OCR_DPI
    words = []
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if t and float(d["conf"][i]) >= 0:
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            r = fitz.Rect(x * k, y * k, (x + w) * k, (y + h) * k)
            words.append((t, r * page.derotation_matrix))  # visual -> unrotated coords
    return words


def _join_words(words):
    text, spans = "", []
    for t, r in words:
        spans.append((len(text), len(text) + len(t), r))
        text += t + " "
    return text, spans


def _span_rects(spans, s: int, e: int) -> list[fitz.Rect]:
    return [fitz.Rect(r) + (-1.5, -1.5, 1.5, 1.5) for a, b, r in spans if a < e and b > s]


# ------------------------------------------------------------------ applying
def _pixelate(page, rect: fitz.Rect) -> bytes | None:
    """Mosaic image of a region (taken BEFORE the text is erased)."""
    try:
        pix = page.get_pixmap(clip=rect, dpi=150, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        blk = max(6, int(pix.height * 0.6))  # ~1-2 blocks per text line -> unreadable
        small = img.resize((max(1, pix.width // blk), max(1, pix.height // blk)), Image.BOX)
        out = small.resize(img.size, Image.NEAREST).filter(ImageFilter.GaussianBlur(2))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def _apply(page, rects: list[fitz.Rect], mode: str) -> None:
    plugs = []
    use_blur = mode == "blur" and page.rotation == 0  # rotated pages fall back to black
    for r in rects:
        png = _pixelate(page, r) if use_blur else None
        page.add_redact_annot(r, fill=(1, 1, 1) if png else (0, 0, 0))
        if png:
            plugs.append((r, png))
    try:  # erase text + image pixels, keep table borders / line art
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    except (TypeError, AttributeError):
        page.apply_redactions()
    for r, png in plugs:
        page.insert_image(r, stream=png)


def _strip_links(page, strings: set[str]) -> None:
    """Remove links (mailto:, tel:, URLs) that contain redacted values."""
    keys = {s.replace(" ", "") for s in strings if len(s) >= 4}
    for ln in page.get_links():
        uri = (ln.get("uri") or "").replace(" ", "")
        if uri and any(k in uri for k in keys):
            page.delete_link(ln)


# ---------------------------------------------------------------- public API
def redact_pdf(
    pdf_path: Path,
    out_path: Path,
    *,
    types: list[str] | None = None,     # None = all of config.PII_PATTERNS; [] = none
    custom_regex: str | None = None,
    terms: list[str] | None = None,     # literal words/phrases (case-insensitive)
    mode: str = "black",                # "black" | "blur"
    ocr_scanned: bool = True,
    langs: str | None = None,
) -> dict:
    """Redact PII. Returns {redactions, by_type, pages_affected, ocr_pages, samples(masked), warnings, output}."""
    if mode not in ("black", "blur"):
        raise PDFError("mode must be 'black' or 'blur'.")
    types = list(config.PII_PATTERNS) if types is None else list(types)
    unknown = [t for t in types if t not in config.PII_PATTERNS]
    if unknown:
        raise PDFError(f"Unknown data type: {', '.join(unknown)}")
    pats = _compile(types, custom_regex)
    terms = list(dict.fromkeys(t.strip() for t in (terms or []) if t and len(t.strip()) >= 2))[:100]
    if not pats and not terms:
        raise PDFError("Select at least one data type or add custom terms.")

    try:
        doc = fitz.open(pdf_path)
    except Exception:
        raise PDFError("PDF could not be opened (corrupted or unsupported).")

    by_type: Counter = Counter()
    pages_affected: list[int] = []
    samples: list[str] = []
    warnings: list[str] = []
    unlocated = ocr_pages = 0
    ocr_langs = _check_langs(langs) if ocr_scanned else ""

    with doc:
        for idx, page in enumerate(doc):
            n = idx + 1
            try:
                text = page.get_text("text")
                rects: list[fitz.Rect] = []
                strings: set[str] = set()

                if len(text.strip()) >= 10:  # ---- normal page with a text layer
                    seen = set()
                    for kind, _s, _e, raw in _scan(text, pats):
                        raw = re.sub(r"\s+", " ", raw)
                        if (kind, raw) in seen:
                            continue
                        seen.add((kind, raw))
                        hits = page.search_for(raw)
                        if not hits:
                            unlocated += 1
                            continue
                        rects += hits
                        strings.add(raw)
                        by_type[kind] += len(hits)
                        if len(samples) < 15:
                            samples.append(f"{kind}: {_mask(raw)}")
                    for term in terms:
                        hits = page.search_for(term)
                        rects += hits
                        by_type["term"] += len(hits)
                        if hits:
                            strings.add(term)

                elif ocr_scanned:  # ---- scanned page: OCR to find PII locations
                    if ocr_pages >= config.MAX_PAGES_OCR:
                        raise PDFError(f"Too many scanned pages (limit {config.MAX_PAGES_OCR}).")
                    ocr_pages += 1
                    otext, spans = _join_words(_ocr_words(page, ocr_langs))
                    for kind, s, e, raw in _scan(otext, pats):
                        rects += _span_rects(spans, s, e)
                        by_type[kind] += 1
                        if len(samples) < 15:
                            samples.append(f"{kind}: {_mask(raw)}")
                    for term in terms:
                        for m in re.finditer(re.escape(term), otext, re.IGNORECASE):
                            rects += _span_rects(spans, m.start(), m.end())
                            by_type["term"] += 1

                elif page.get_images():
                    warnings.append(f"Page {n} is scanned and was NOT checked (OCR mode is off).")

                if rects:
                    _apply(page, [r for r in rects if not r.is_empty], mode)
                    pages_affected.append(n)
                if strings:
                    _strip_links(page, strings)
            except PDFError:
                raise
            except Exception as e:
                raise PDFError(f"Redaction failed on page {n} ({type(e).__name__}); "
                               "aborted so no unredacted file is returned.")

        # ---- remove hidden leftovers
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
            doc.scrub(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True,
                      thumbnails=True, javascript=True, hidden_text=False, remove_links=False)
        except Exception:
            warnings.append("Could not fully scrub metadata.")
        doc.save(out_path, garbage=4, deflate=True)

    if unlocated:
        warnings.append(f"{unlocated} detected value(s) could not be located on the page - please review.")
    return {
        "redactions": sum(by_type.values()),
        "by_type": dict(by_type),
        "pages_affected": pages_affected,
        "ocr_pages": ocr_pages,
        "mode": mode,
        "samples": samples,
        "warnings": warnings,
        "output": out_path.name,
          }
