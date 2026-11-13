"""PDF -> XLSX table extraction (pdfplumber + openpyxl).

Features:
- 3 detection strategies, tried in order: ruled -> semi-ruled -> text-aligned
- Merged cells (row/col spans) detected from cell geometry and kept in Excel
- Tables continuing across pages are stitched into one table (repeated headers dropped)
- Numbers become real Excel numbers; phone numbers / leading zeros stay text
- Page-by-page processing (low RAM), bad pages are skipped with a warning
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.utils import PDFError

# ------------------------------------------------------------------ models


@dataclass
class Cell:
    r0: int
    r1: int  # exclusive
    c0: int
    c1: int  # exclusive
    text: str


@dataclass
class Grid:
    n_rows: int
    n_cols: int
    cells: list[Cell]


@dataclass
class Segment:
    """One logical table (may span several pages)."""
    cells: list[Cell]
    n_rows: int
    n_cols: int
    method: str
    pages: list[int] = field(default_factory=list)  # 1-based


# Detection strategies, tried in this order until one finds valid tables
STRATEGIES = [
    ("ruled", {"vertical_strategy": "lines", "horizontal_strategy": "lines"}),
    ("semi-ruled", {"vertical_strategy": "text", "horizontal_strategy": "lines",
                    "snap_tolerance": 3, "join_tolerance": 3}),
    ("text-aligned", {"vertical_strategy": "text", "horizontal_strategy": "text",
                      "snap_tolerance": 3, "join_tolerance": 3,
                      "intersection_tolerance": 5}),
]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
THIN = Side(style="thin", color="B0B7C3")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="E8EDF5")

# ----------------------------------------------------------------- helpers


def _clean(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", _CTRL.sub("", str(v))).strip()


def _cluster(values: list[float], tol: float = 2.5) -> list[float]:
    """Merge nearly-equal coordinates into one grid line."""
    values = sorted(values)
    groups, cur = [], [values[0]]
    for v in values[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    return [sum(g) / len(g) for g in groups]


def _idx(edges: list[float], v: float) -> int:
    return min(range(len(edges)), key=lambda i: abs(edges[i] - v))


def _parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,5' -> [0,1,2,4] (zero-based). Empty = all pages."""
    if not spec or not spec.strip():
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise PDFError(f"Invalid page range: '{part}'")
        a, b = int(m[1]), int(m[2] or m[1])
        if a < 1 or b < a or a > total:
            raise PDFError(f"Page range '{part}' is outside 1-{total}")
        out.extend(range(a - 1, min(b, total)))
    return sorted(set(out))


def _coerce(text: str):
    """Return (value, number_format). Keeps IDs/phones/leading-zero values as text."""
    if not text:
        return None, None
    t = text.strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    pct = t.endswith("%")
    t = t.rstrip("%").lstrip("$€£৳ ").strip()
    if "," in t:
        if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", t):
            return text, None
        t = t.replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t) or re.match(r"-?0\d", t):
        return text, None
    if len(t.replace("-", "").replace(".", "")) > 15:
        return text, None
    num = float(t) if "." in t else int(t)
    if neg:
        num = -num
    if pct:
        return num / 100, "0.00%"
    return num, ("#,##0.00" if isinstance(num, float) else "#,##0")


# -------------------------------------------------------------- extraction


def _build_grid(table) -> Grid | None:
    """Convert a pdfplumber table into a span-aware grid."""
    boxes = [b for row in table.rows for b in row.cells if b]
    if not boxes:
        return None
    raw = table.extract(x_tolerance=2, y_tolerance=2)
    xs = _cluster([v for b in boxes for v in (b[0], b[2])])
    ys = _cluster([v for b in boxes for v in (b[1], b[3])])
    cells: list[Cell] = []
    for r, row in enumerate(table.rows):
        for c, box in enumerate(row.cells):
            if box is None:  # covered by a merged cell
                continue
            text = _clean(raw[r][c]) if r < len(raw) and c < len(raw[r]) else ""
            r0, r1 = _idx(ys, box[1]), _idx(ys, box[3])
            c0, c1 = _idx(xs, box[0]), _idx(xs, box[2])
            if r1 <= r0 or c1 <= c0:
                continue
            cells.append(Cell(r0, r1, c0, c1, text))
    if not cells:
        return None
    return Grid(len(ys) - 1, len(xs) - 1, cells)


def _valid(grid: Grid | None, method: str) -> bool:
    if not grid or grid.n_rows < 2 or grid.n_cols < 2 or grid.n_cols > 40:
        return False
    if method == "ruled":
        return True
    # Loose strategies can hallucinate tables from paragraphs -> stricter checks
    filled = sum(1 for c in grid.cells if c.text)
    return grid.n_rows >= 3 and filled / max(len(grid.cells), 1) >= 0.4


def _find_page_tables(page) -> tuple[str, list[tuple[Grid, tuple]]]:
    """Return (method, [(grid, bbox), ...]) sorted top-to-bottom."""
    for method, settings in STRATEGIES:
        found = []
        try:
            tables = page.find_tables(table_settings=settings)
        except Exception:
            continue
        for t in tables:
            try:
                g = _build_grid(t)
            except Exception:
                continue
            if _valid(g, method):
                found.append((g, t.bbox))
        if found:
            found.sort(key=lambda x: x[1][1])
            return method, found
    return "", []


def _row0(cells: list[Cell], n_cols: int) -> list[str]:
    row = [""] * n_cols
    for c in cells:
        if c.r0 == 0 and c.c0 < n_cols:
            row[c.c0] = c.text.lower()
    return row


def _append(seg: Segment, grid: Grid) -> None:
    """Stitch a continuation table below an existing one (drop repeated header)."""
    skip = 1 if _row0(grid.cells, grid.n_cols) == _row0(seg.cells, seg.n_cols) else 0
    for c in grid.cells:
        if c.r1 <= skip:
            continue
        seg.cells.append(Cell(max(c.r0 - skip, 0) + seg.n_rows,
                              c.r1 - skip + seg.n_rows, c.c0, c.c1, c.text))
    seg.n_rows += grid.n_rows - skip


# ----------------------------------------------------------------- writing


def _page_label(pages: list[int]) -> str:
    return f"p{pages[0]}" if pages[0] == pages[-1] else f"p{pages[0]}-{pages[-1]}"


def _sheet_name(base: str, used: set[str]) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "_", base)[:28]
    final, i = name, 2
    while final.lower() in used:
        final = f"{name}_{i}"
        i += 1
    used.add(final.lower())
    return final


def _write_segment(ws, seg: Segment, top: int, widths: dict, coerce: bool) -> None:
    for c in seg.cells:
        r, col = top + c.r0, c.c0 + 1
        value, fmt = _coerce(c.text) if coerce else (c.text or None, None)
        # Border on every position inside a merged range (before merging)
        for rr in range(r, top + c.r1):
            for cc in range(col, c.c1 + 1):
                ws.cell(row=rr, column=cc).border = BORDER
        cell = ws.cell(row=r, column=col, value=value)
        if isinstance(value, str) and value.startswith("="):
            cell.data_type = "s"  # never let PDF text become an Excel formula
        if fmt:
            cell.number_format = fmt
        is_num = isinstance(value, (int, float))
        merged = (c.r1 - c.r0 > 1) or (c.c1 - c.c0 > 1)
        cell.alignment = Alignment(
            wrap_text=True, vertical="center",
            horizontal="right" if is_num else ("center" if merged else "left"))
        if c.r0 == 0:
            cell.font = Font(bold=True)
            for cc in range(col, c.c1 + 1):
                ws.cell(row=r, column=cc).fill = HEAD_FILL
        if merged:
            ws.merge_cells(start_row=r, start_column=col,
                           end_row=top + c.r1 - 1, end_column=c.c1)
        elif c.text:
            widths[col] = max(widths.get(col, 8), min(60, len(c.text) * 1.1 + 2))


def _apply_widths(ws, widths: dict) -> None:
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


# -------------------------------------------------------------- public API


def extract_tables_to_xlsx(
    pdf_path: Path,
    out_path: Path,
    *,
    pages: str | None = None,
    layout: str = "sheets",      # "sheets" = one sheet per table | "single" = all in one sheet
    coerce_numbers: bool = True,
    merge_continued: bool = True,
) -> dict:
    """Extract all tables from a PDF into an XLSX file. Returns a summary dict."""
    segments: list[Segment] = []
    warnings: list[str] = []
    total_chars, scanned = 0, 0

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception:
        raise PDFError("PDF could not be opened (corrupted or unsupported).")

    with pdf:
        wanted = _parse_pages(pages, len(pdf.pages))
        open_seg: Segment | None = None
        prev_idx = -2

        for idx in wanted:
            page = pdf.pages[idx]
            try:
                total_chars += len(page.chars)
                method, found = _find_page_tables(page)
                scanned += 1
                h = float(page.height)

                for n, (grid, bbox) in enumerate(found):
                    top_ratio = bbox[1] / h
                    if (n == 0 and merge_continued and open_seg is not None
                            and prev_idx == idx - 1 and open_seg.n_cols == grid.n_cols
                            and top_ratio < 0.35):
                        _append(open_seg, grid)
                        open_seg.pages.append(idx + 1)
                        seg = open_seg
                    else:
                        seg = Segment(list(grid.cells), grid.n_rows, grid.n_cols,
                                      method, [idx + 1])
                        segments.append(seg)
                    last_seg, last_bottom = seg, bbox[3] / h

                open_seg = last_seg if (found and last_bottom > 0.65) else None
                prev_idx = idx
            except Exception as e:  # one bad page must not kill the whole job
                warnings.append(f"Page {idx + 1} skipped: {type(e).__name__}")
                open_seg = None
            finally:
                try:
                    page.flush_cache()  # free page memory (512 MB tier)
                except Exception:
                    pass

    if not segments:
        if total_chars == 0:
            raise PDFError("No selectable text found - this looks like a scanned PDF. Run OCR first.")
        raise PDFError("No tables were detected in this PDF.")

    # ---- build workbook
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    summary_rows = []

    if layout == "single":
        ws = wb.create_sheet(_sheet_name("Tables", used))
        widths: dict = {}
        row = 1
        for i, seg in enumerate(segments, 1):
            ws.cell(row=row, column=1, value=f"Table {i} ({_page_label(seg.pages)})").font = Font(bold=True, size=12)
            _write_segment(ws, seg, row + 1, widths, coerce_numbers)
            row += seg.n_rows + 3
        _apply_widths(ws, widths)
    else:
        for i, seg in enumerate(segments, 1):
            ws = wb.create_sheet(_sheet_name(f"T{i}_{_page_label(seg.pages)}", used))
            widths = {}
            _write_segment(ws, seg, 1, widths, coerce_numbers)
            _apply_widths(ws, widths)
            ws.freeze_panes = "A2"

    for i, seg in enumerate(segments, 1):
        summary_rows.append([i, _page_label(seg.pages), seg.n_rows, seg.n_cols, seg.method])

    ws = wb.create_sheet(_sheet_name("Summary", used))
    ws.append(["Table", "Pages", "Rows", "Columns", "Detection"])
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), HEAD_FILL
    for r in summary_rows:
        ws.append(r)
    for col, w in zip("ABCDE", (8, 12, 8, 10, 16)):
        ws.column_dimensions[col].width = w

    wb.save(out_path)
    return {
        "tables": len(segments),
        "pages_scanned": scanned,
        "stitched": sum(1 for s in segments if len(s.pages) > 1),
        "details": [dict(zip(("table", "pages", "rows", "cols", "method"), r)) for r in summary_rows],
        "warnings": warnings,
        "output": out_path.name,
                                }
