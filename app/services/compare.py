"""PDF diff: word-level comparison -> side-by-side annotated PDF (page-by-page, low RAM)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import fitz  # PyMuPDF

from app.utils import PDFError, validate_pdf

MAX_COMPARE_PAGES = 150          # hard cap to protect 512 MB RAM
GAP, HEADER = 12, 30             # layout: gap between pages, header band height
C_ADD = (0.15, 0.70, 0.25)       # green  -> only in B
C_DEL = (0.90, 0.20, 0.20)       # red    -> only in A
C_CHG = (1.00, 0.70, 0.00)       # amber  -> replaced
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _open(path: Path, label: str) -> fitz.Document:
    """Validate + open a PDF; errors are prefixed with A/B label."""
    try:
        validate_pdf(path)
        doc = fitz.open(path)
    except PDFError as exc:
        raise PDFError(f"{label}: {exc}") from exc
    except Exception as exc:
        raise PDFError(f"{label}: cannot open PDF ({exc})") from exc
    if doc.needs_pass:
        doc.close()
        raise PDFError(f"{label}: PDF is password-protected")
    return doc


def _words(page: fitz.Page, ignore_case: bool, ignore_punct: bool) -> list[tuple]:
    """Return [(normalized_word, rect_in_displayed_space, (block, line))]."""
    rot, out = page.rotation_matrix, []
    for x0, y0, x1, y1, txt, blk, ln, _ in page.get_text("words"):
        key = unicodedata.normalize("NFC", txt)
        if ignore_case:
            key = key.casefold()
        if ignore_punct:
            key = _PUNCT.sub("", key)
        if key:
            out.append((key, fitz.Rect(x0, y0, x1, y1) * rot, (blk, ln)))
    return out


def _spans(words: list[tuple], i: int, j: int) -> list[fitz.Rect]:
    """Merge words[i:j] into one rectangle per text line (cleaner highlights)."""
    rects, cur, cur_id = [], None, None
    for _, rect, lid in words[i:j]:
        if cur is not None and lid == cur_id:
            cur.include_rect(rect)
        else:
            if cur is not None:
                rects.append(cur)
            cur, cur_id = fitz.Rect(rect), lid
    if cur is not None:
        rects.append(cur)
    return rects


def _diff_page(wa: list[tuple], wb: list[tuple]) -> dict:
    """Word-level diff of one page pair."""
    ka, kb = [w[0] for w in wa], [w[0] for w in wb]
    # autojunk only for very large pages (keeps worst-case time bounded)
    sm = SequenceMatcher(None, ka, kb, autojunk=len(ka) * len(kb) > 40_000_000)
    res = {"marks_a": [], "marks_b": [], "added": 0, "removed": 0, "changed": 0,
           "matched": sum(m.size for m in sm.get_matching_blocks())}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            res["removed"] += i2 - i1
            res["marks_a"].append((_spans(wa, i1, i2), C_DEL))
        elif tag == "insert":
            res["added"] += j2 - j1
            res["marks_b"].append((_spans(wb, j1, j2), C_ADD))
        else:  # replace
            res["changed"] += max(i2 - i1, j2 - j1)
            res["marks_a"].append((_spans(wa, i1, i2), C_CHG))
            res["marks_b"].append((_spans(wb, j1, j2), C_CHG))
    return res


def _short(name: str, n: int = 24) -> str:
    """ASCII-safe, truncated label for the built-in Helvetica header font."""
    s = name.encode("ascii", "replace").decode()
    return s if len(s) <= n else s[: n - 3] + "..."


def _compose(out: fitz.Document, da, db, pa, pb, num: int,
             name_a: str, name_b: str, marks_a: list, marks_b: list) -> None:
    """Append one side-by-side annotated page to `out`."""
    wa, ha = (pa.rect.width, pa.rect.height) if pa else (pb.rect.width, pb.rect.height)
    wb, hb = (pb.rect.width, pb.rect.height) if pb else (wa, ha)
    page = out.new_page(width=wa + GAP + wb, height=HEADER + max(ha, hb))
    page.draw_rect(page.rect, color=None, fill=(0.92, 0.92, 0.92))

    for src, p, ox, w, h, marks, tag in (
        (da, pa, 0, wa, ha, marks_a, "A"),
        (db, pb, wa + GAP, wb, hb, marks_b, "B"),
    ):
        box = fitz.Rect(ox, HEADER, ox + w, HEADER + h)
        if p is None:
            page.draw_rect(box, color=None, fill=(1, 1, 1))
            page.insert_text((ox + 12, HEADER + 24), f"(no page {num} in {tag})",
                             fontsize=11, color=(0.5, 0.5, 0.5))
            continue
        page.show_pdf_page(box, src, p.number)          # original vector page, no rasterizing
        for rects, color in marks:
            for r in rects:
                page.draw_rect(fitz.Rect(r.x0 + ox, r.y0 + HEADER, r.x1 + ox, r.y1 + HEADER),
                               color=None, fill=color, fill_opacity=0.35, overlay=True)

    # header: labels + legend
    page.insert_text((6, 19), f"A: {_short(name_a)}   |   page {num}", fontsize=9, color=(0.1, 0.1, 0.1))
    page.insert_text((wa + GAP + 6, 19), f"B: {_short(name_b)}", fontsize=9, color=(0.1, 0.1, 0.1))
    lx = page.rect.width - 215
    for label, col in (("added", C_ADD), ("removed", C_DEL), ("changed", C_CHG)):
        page.draw_rect(fitz.Rect(lx, 10, lx + 10, 20), color=None, fill=col)
        page.insert_text((lx + 14, 19), label, fontsize=9, color=(0.1, 0.1, 0.1))
        lx += 70


def compare_pdfs(pdf_a: Path, pdf_b: Path, out_path: Path, *,
                 ignore_case: bool = False, ignore_punct: bool = False) -> dict:
    """
    Compare two PDFs page-by-page (word level) and write a side-by-side annotated PDF.
    Returns {pages_a, pages_b, pages_compared, additions, deletions, changes, similarity,
             identical, pages_changed, details[{page,added,removed,changed}], warnings[], output}.
    """
    da = db = out = None
    warnings: list[str] = []
    try:
        da, db = _open(pdf_a, "A"), _open(pdf_b, "B")
        na, nb = len(da), len(db)
        total = max(na, nb)
        if total > MAX_COMPARE_PAGES:
            raise PDFError(f"Too many pages ({total}); limit is {MAX_COMPARE_PAGES} for comparison")
        if na != nb:
            warnings.append(f"Page count differs: A has {na}, B has {nb}")

        out = fitz.open()
        add = rem = chg = matched = words_total = text_pages = 0
        details: list[dict] = []
        scanned: list[int] = []

        for i in range(total):
            pa = da[i] if i < na else None
            pb = db[i] if i < nb else None
            wa = _words(pa, ignore_case, ignore_punct) if pa else []
            wb = _words(pb, ignore_case, ignore_punct) if pb else []

            if not wa and not wb:
                # no text on either side: scanned page or truly blank
                if (pa and pa.get_images()) or (pb and pb.get_images()):
                    scanned.append(i + 1)
                d = {"marks_a": [], "marks_b": [], "added": 0, "removed": 0, "changed": 0, "matched": 0}
            else:
                text_pages += 1
                d = _diff_page(wa, wb)

            add, rem, chg = add + d["added"], rem + d["removed"], chg + d["changed"]
            matched += d["matched"]
            words_total += len(wa) + len(wb)
            if d["added"] or d["removed"] or d["changed"] or pa is None or pb is None:
                details.append({"page": i + 1, "added": d["added"],
                                "removed": d["removed"], "changed": d["changed"]})

            _compose(out, da, db, pa, pb, i + 1, pdf_a.name, pdf_b.name, d["marks_a"], d["marks_b"])
            pa = pb = None  # release page refs early

        if text_pages == 0:
            raise PDFError("No extractable text in either PDF (scanned?). Run OCR first, then compare.")
        if scanned:
            warnings.append(f"No text layer on page(s) {', '.join(map(str, scanned[:15]))}"
                            f"{'...' if len(scanned) > 15 else ''}; those pages were not compared (OCR first)")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path, garbage=3, deflate=True)
        sim = round(2 * matched / words_total * 100, 1) if words_total else 100.0
        return {
            "pages_a": na, "pages_b": nb, "pages_compared": total,
            "additions": add, "deletions": rem, "changes": chg,
            "similarity": sim,
            "identical": not (add or rem or chg) and na == nb,
            "pages_changed": len(details), "details": details,
            "warnings": warnings, "output": out_path.name,
        }
    except PDFError:
        raise
    except Exception as exc:
        raise PDFError(f"Comparison failed: {exc}") from exc
    finally:
        for d in (out, da, db):
            if d is not None:
                d.close()
