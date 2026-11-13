"""Batch engine: convert / merge / split / watermark many PDFs -> files (+ optional ZIP)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF
from docx import Document

from app.config import MAX_BATCH_FILES
from app.services.tables import _parse_pages
from app.utils import PDFError, make_zip, safe_name, validate_pdf

OPERATIONS = ("convert", "merge", "split", "watermark")
MAX_IMG_PAGES = 200        # per-file cap for PDF -> images
MAX_SPLIT_OUTPUTS = 500    # cap for split result files
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")   # chars python-docx rejects


# ───────────────────────── helpers ─────────────────────────
def _open(path: Path) -> fitz.Document:
    validate_pdf(path)
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise PDFError(f"Cannot open PDF: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise PDFError("PDF is password-protected")
    return doc


def _out(out_dir: Path, name: str) -> Path:
    """Collision-free output path inside out_dir."""
    name = safe_name(name)
    p, n = out_dir / name, 2
    while p.exists():
        p = out_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return p


def _bool(v) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)


def _num(opts: dict, key: str, default, lo, hi, cast=float):
    try:
        v = cast(opts.get(key, default))
    except (TypeError, ValueError):
        raise PDFError(f"Invalid '{key}'")
    if not lo <= v <= hi:
        raise PDFError(f"'{key}' must be between {lo} and {hi}")
    return v


def _parse_options(op: str, o: dict) -> dict:
    """Validate + normalize options once, up-front (so bad options abort the whole batch)."""
    if op == "convert":
        target = str(o.get("target", "images")).lower()
        if target not in ("images", "txt", "docx"):
            raise PDFError("convert target must be: images, txt or docx")
        fmt = str(o.get("format", "png")).lower().replace("jpeg", "jpg")
        if fmt not in ("png", "jpg"):
            raise PDFError("image format must be png or jpg")
        return {"target": target, "format": fmt, "dpi": _num(o, "dpi", 150, 72, 300, int),
                "pages": (str(o["pages"]).strip() or None) if o.get("pages") else None}
    if op == "split":
        mode = str(o.get("mode", "each")).lower()
        if mode not in ("each", "chunks", "ranges"):
            raise PDFError("split mode must be: each, chunks or ranges")
        ranges = str(o.get("ranges", "")).strip()
        if mode == "ranges" and not ranges:
            raise PDFError("Provide 'ranges' like '1-3,5,7-9' for split mode 'ranges'")
        return {"mode": mode, "chunk_size": _num(o, "chunk_size", 10, 1, 1000, int), "ranges": ranges}
    if op == "watermark":
        text = str(o.get("text", "")).strip()
        if not text or len(text) > 60:
            raise PDFError("Watermark text is required (max 60 characters)")
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            raise PDFError("Watermark text supports Latin characters only")
        col = str(o.get("color", "#808080")).strip()
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", col):
            raise PDFError("color must be hex like #808080")
        col = col.lstrip("#")
        return {"text": text, "opacity": _num(o, "opacity", 0.2, 0.05, 1.0),
                "angle": _num(o, "angle", 45, -90, 90), "fontsize": _num(o, "fontsize", 48, 10, 200),
                "color": tuple(int(col[i:i + 2], 16) / 255 for i in (0, 2, 4)),
                "tile": _bool(o.get("tile", False))}
    # merge
    name = Path(safe_name(str(o.get("name", "merged")) or "merged")).stem or "merged"
    return {"name": f"{name}.pdf", "add_bookmarks": _bool(o.get("add_bookmarks", True))}


# ───────────────────────── operations ─────────────────────────
def _convert(f: Path, out_dir: Path, cfg: dict, warnings: list[str]) -> list[Path]:
    doc = _open(f)
    try:
        total = len(doc)
        idx = _parse_pages(cfg["pages"], total) if cfg["pages"] else list(range(total))
        target, outs = cfg["target"], []

        if target == "images":
            if len(idx) > MAX_IMG_PAGES:
                raise PDFError(f"Too many pages for image export ({len(idx)}); use a page range (max {MAX_IMG_PAGES})")
            zoom = fitz.Matrix(cfg["dpi"] / 72, cfg["dpi"] / 72)
            ext = cfg["format"]
            for p in idx:     # render one page at a time
                pix = doc[p].get_pixmap(matrix=zoom, colorspace=fitz.csRGB, alpha=False)
                path = _out(out_dir, f"{f.stem}_p{p + 1:03d}.{ext}")
                if ext == "jpg":
                    pix.save(str(path), output="jpg", jpg_quality=85)
                else:
                    pix.save(str(path), output="png")
                outs.append(path)
                pix = None
            return outs

        if target == "txt":
            parts = []
            for p in idx:
                t = doc[p].get_text("text").strip()
                parts.append(f"=== Page {p + 1} ===\n{t}")
            if not any(doc[p].get_text("text").strip() for p in idx):
                raise PDFError("No text layer (scanned PDF). Run OCR first")
            path = _out(out_dir, f"{f.stem}.txt")
            path.write_text("\n\n".join(parts), encoding="utf-8")
            return [path]

        # docx: one paragraph per text block, page breaks between pages
        word, found = Document(), False
        for n, p in enumerate(idx):
            for b in doc[p].get_text("blocks", sort=True):
                if b[6] != 0:      # skip image blocks
                    continue
                txt = _XML_BAD.sub("", " ".join(b[4].split()))
                if txt:
                    word.add_paragraph(txt)
                    found = True
            if n < len(idx) - 1:
                word.add_page_break()
        if not found:
            raise PDFError("No text layer (scanned PDF). Run OCR first")
        path = _out(out_dir, f"{f.stem}.docx")
        word.save(str(path))
        return [path]
    finally:
        doc.close()


def _split(f: Path, out_dir: Path, cfg: dict, warnings: list[str]) -> list[Path]:
    src = _open(f)
    try:
        total, mode = len(src), cfg["mode"]
        if mode == "each":
            groups = [[i] for i in range(total)]
        elif mode == "chunks":
            n = cfg["chunk_size"]
            groups = [list(range(s, min(s + n, total))) for s in range(0, total, n)]
        else:   # each comma-separated range becomes its own file
            groups = [_parse_pages(g.strip(), total) for g in cfg["ranges"].split(",") if g.strip()]
        groups = [g for g in groups if g]
        if not groups:
            raise PDFError("Nothing to split")
        if len(groups) > MAX_SPLIT_OUTPUTS:
            raise PDFError(f"Split would create {len(groups)} files (max {MAX_SPLIT_OUTPUTS})")

        outs = []
        for g in groups:
            part = fitz.open()
            try:
                for p in g:
                    part.insert_pdf(src, from_page=p, to_page=p)
                label = f"p{g[0] + 1}" if len(g) == 1 else f"p{min(g) + 1}-{max(g) + 1}"
                path = _out(out_dir, f"{f.stem}_{label}.pdf")
                part.save(str(path), garbage=3, deflate=True)
                outs.append(path)
            finally:
                part.close()
        return outs
    finally:
        src.close()


def _stamp(page: fitz.Page, cfg: dict) -> None:
    """Draw (optionally tiled) rotated, semi-transparent text; handles rotated pages."""
    fs, text = cfg["fontsize"], cfg["text"]
    tw = fitz.get_text_length(text, fontname="helv", fontsize=fs)
    r = page.rect
    if cfg["tile"]:
        sx, sy = max(tw * 1.5, 180), max(fs * 4, 150)
        centers = [(sx / 2 + i * sx, sy / 2 + j * sy)
                   for j in range(int(r.height // sy) + 1) for i in range(int(r.width // sx) + 1)]
    else:
        centers = [(r.width / 2, r.height / 2)]
    for cx, cy in centers:
        c = fitz.Point(cx, cy) * page.derotation_matrix      # displayed -> unrotated space
        page.insert_text(fitz.Point(c.x - tw / 2, c.y + fs * 0.3), text, fontsize=fs, fontname="helv",
                         color=cfg["color"], fill_opacity=cfg["opacity"],
                         morph=(c, fitz.Matrix(-cfg["angle"] - page.rotation)), overlay=True)


def _watermark(f: Path, out_dir: Path, cfg: dict, warnings: list[str]) -> list[Path]:
    doc = _open(f)
    try:
        for page in doc:
            _stamp(page, cfg)
        path = _out(out_dir, f"{f.stem}_watermarked.pdf")
        doc.save(str(path), garbage=3, deflate=True)
        return [path]
    finally:
        doc.close()


def _merge(files: list[Path], out_dir: Path, cfg: dict, failed: list[dict],
           on_progress: Callable[[int, int], None] | None) -> list[Path]:
    merged, toc, ok = fitz.open(), [], 0
    try:
        for k, f in enumerate(files, 1):
            try:
                src = _open(f)
            except PDFError as exc:
                failed.append({"file": f.name, "error": str(exc)})
                src = None
            if src is not None:
                try:
                    if len(src):
                        toc.append([1, f.stem, len(merged) + 1])
                        merged.insert_pdf(src)
                        ok += 1
                finally:
                    src.close()
            if on_progress:
                on_progress(k, len(files))
        if ok < 2:
            raise PDFError("Need at least 2 valid PDFs to merge")
        if cfg["add_bookmarks"] and toc:
            merged.set_toc(toc)
        path = _out(out_dir, cfg["name"])
        merged.save(str(path), garbage=3, deflate=True)
        return [path]
    finally:
        merged.close()


# ───────────────────────── entry point ─────────────────────────
def batch_process(files: list[Path], out_dir: Path, operation: str, *,
                  options: dict | None = None, create_zip: bool = True,
                  on_progress: Callable[[int, int], None] | None = None) -> dict:
    """
    operation + options:
      convert   {target: images|txt|docx, format: png|jpg, dpi: 72-300, pages: "1-3,5"}
      merge     {name: "merged", add_bookmarks: true}
      split     {mode: each|chunks|ranges, chunk_size: 10, ranges: "1-3,5,7-9"}
      watermark {text, opacity 0.05-1, angle -90..90, fontsize 10-200, color "#808080", tile}
    Failed files are reported in `failed`; only a total failure raises PDFError.
    Returns {operation, inputs, processed, failed[{file,error}], outputs[filenames],
             zip(filename|None), warnings[]}.
    """
    if operation not in OPERATIONS:
        raise PDFError(f"Unknown operation '{operation}'. Use: {', '.join(OPERATIONS)}")
    if not files:
        raise PDFError("No files provided")
    if len(files) > MAX_BATCH_FILES:
        raise PDFError(f"Too many files ({len(files)}); limit is {MAX_BATCH_FILES}")

    cfg = _parse_options(operation, options or {})
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    failed: list[dict] = []
    warnings: list[str] = []

    if operation == "merge":
        outputs = _merge(files, out_dir, cfg, failed, on_progress)
    else:
        fn = {"convert": _convert, "split": _split, "watermark": _watermark}[operation]
        for k, f in enumerate(files, 1):
            try:
                outputs.extend(fn(f, out_dir, cfg, warnings))
            except PDFError as exc:
                failed.append({"file": f.name, "error": str(exc)})
            except Exception as exc:
                failed.append({"file": f.name, "error": f"Unexpected error: {exc}"})
            if on_progress:
                on_progress(k, len(files))

    if not outputs:
        raise PDFError("All files failed: " + "; ".join(f"{x['file']}: {x['error']}" for x in failed[:5]))
    if failed:
        warnings.append(f"{len(failed)} file(s) failed and were skipped")

    zip_name = None
    if create_zip and len(outputs) > 1:
        zip_path = out_dir / f"{operation}_results.zip"
        make_zip(outputs, zip_path)
        zip_name = zip_path.name

    return {"operation": operation, "inputs": len(files), "processed": len(files) - len(failed),
            "failed": failed, "outputs": [p.name for p in outputs], "zip": zip_name, "warnings": warnings}
