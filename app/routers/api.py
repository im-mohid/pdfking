"""REST API: thin async layer over the sync services (heavy work always via asyncio.to_thread)."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import (FILE_TTL_SECONDS, FREE_DAILY_LIMIT, MAX_BATCH_FILES, MAX_FILE_MB,
                        MAX_PAGES_OCR, OCR_LANGS, PII_PATTERNS)
from app.services.batch import OPERATIONS, batch_process
from app.services.compare import compare_pdfs
from app.services.compress import compress_pdf
from app.services.ocr import _check_langs, ocr_pdf
from app.services.redact import redact_pdf
from app.services.tables import extract_tables_to_xlsx
from app.utils import (PDFError, file_info, new_job, rate_limit, result_file,
                       save_upload, save_uploads)

router = APIRouter()                      # main.py adds the /api prefix

JOBS: dict[str, dict] = {}                # in-memory OCR job registry
_TASKS: set[asyncio.Task] = set()         # strong refs so tasks are not garbage-collected
_OCR_SEM = asyncio.Semaphore(1)           # one OCR at a time (512 MB RAM)


# ───────────────────────── helpers ─────────────────────────
def _bad(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


async def _run(fn, *args, **kwargs):
    """Run a sync service in a worker thread; PDFError -> HTTP 400."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except PDFError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _guard(coro):
    """Await an upload helper; PDFError -> HTTP 400."""
    try:
        return await coro
    except PDFError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _workspace() -> tuple[str, Path, Path]:
    """New job folder: results live in job_dir, uploads in job_dir/in."""
    job_id, job_dir = new_job()
    job_dir = Path(job_dir)
    in_dir = job_dir / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    return job_id, job_dir, in_dir


def _ok(job_id: str, res: dict, paths: list[Path]) -> dict:
    body = {k: v for k, v in res.items() if k != "files"}
    return {"ok": True, "job_id": job_id, **body, "files": [file_info(p, job_id) for p in paths]}


def _listify(s: Optional[str]) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,]+", s or "") if x.strip()]


def _prune() -> None:
    """Drop finished job records older than the file TTL."""
    cutoff = time.time() - FILE_TTL_SECONDS
    for k in [k for k, v in JOBS.items() if v["created"] < cutoff and v["status"] in ("done", "error")]:
        JOBS.pop(k, None)


# ───────────────────────── meta ─────────────────────────
@router.get("/config")
async def get_config():
    """Limits + options the SPA needs to render itself."""
    return {"ok": True, "max_file_mb": MAX_FILE_MB, "max_batch_files": MAX_BATCH_FILES,
            "max_pages_ocr": MAX_PAGES_OCR, "free_daily_limit": FREE_DAILY_LIMIT,
            "ttl_minutes": max(1, FILE_TTL_SECONDS // 60), "pii_types": list(PII_PATTERNS),
            "ocr_langs": OCR_LANGS, "batch_operations": list(OPERATIONS)}


@router.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    try:
        path = Path(result_file(job_id, filename))
    except Exception:
        _bad("File not found or expired", 404)
    if not path.is_file():
        _bad("File not found or expired", 404)
    return FileResponse(path, filename=path.name,
                        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


# ───────────────────────── synchronous tools ─────────────────────────
@router.post("/tables", dependencies=[Depends(rate_limit)])
async def tables(file: UploadFile = File(...), pages: Optional[str] = Form(None),
                 layout: str = Form("sheets"), coerce_numbers: bool = Form(True),
                 merge_continued: bool = Form(True)):
    if layout not in ("sheets", "single"):
        _bad("layout must be 'sheets' or 'single'")
    job_id, job_dir, in_dir = _workspace()
    src = await _guard(save_upload(file, in_dir))
    out = job_dir / f"{src.stem}_tables.xlsx"
    res = await _run(extract_tables_to_xlsx, src, out, pages=pages or None, layout=layout,
                     coerce_numbers=coerce_numbers, merge_continued=merge_continued)
    return _ok(job_id, res, [out])


@router.post("/redact", dependencies=[Depends(rate_limit)])
async def redact(file: UploadFile = File(...), types: Optional[str] = Form(None),
                 custom_regex: Optional[str] = Form(None), terms: Optional[str] = Form(None),
                 mode: str = Form("black"), ocr_scanned: bool = Form(True),
                 langs: Optional[str] = Form(None)):
    if mode not in ("black", "blur"):
        _bad("mode must be 'black' or 'blur'")
    type_list = None if types is None else _listify(types)       # None = all, [] = none
    term_list = _listify(terms) or None
    if type_list is not None and not type_list and not (custom_regex or "").strip() and not term_list:
        _bad("Select at least one PII type, or provide a custom regex / terms")
    job_id, job_dir, in_dir = _workspace()
    src = await _guard(save_upload(file, in_dir))
    out = job_dir / f"{src.stem}_redacted.pdf"
    res = await _run(redact_pdf, src, out, types=type_list, custom_regex=(custom_regex or "").strip() or None,
                     terms=term_list, mode=mode, ocr_scanned=ocr_scanned, langs=langs or None)
    return _ok(job_id, res, [out])


@router.post("/compare", dependencies=[Depends(rate_limit)])
async def compare(file_a: UploadFile = File(...), file_b: UploadFile = File(...),
                  ignore_case: bool = Form(False), ignore_punct: bool = Form(False)):
    job_id, job_dir, in_dir = _workspace()
    (in_dir / "a").mkdir(exist_ok=True)          # separate folders: same filename is fine
    (in_dir / "b").mkdir(exist_ok=True)
    a = await _guard(save_upload(file_a, in_dir / "a"))
    b = await _guard(save_upload(file_b, in_dir / "b"))
    out = job_dir / "comparison.pdf"
    res = await _run(compare_pdfs, a, b, out, ignore_case=ignore_case, ignore_punct=ignore_punct)
    return _ok(job_id, res, [out])


@router.post("/compress", dependencies=[Depends(rate_limit)])
async def compress(file: UploadFile = File(...), level: str = Form("balanced"),
                   image_dpi: Optional[int] = Form(None), jpeg_quality: Optional[int] = Form(None),
                   grayscale: bool = Form(False), strip_metadata: bool = Form(True)):
    job_id, job_dir, in_dir = _workspace()
    src = await _guard(save_upload(file, in_dir))
    out = job_dir / f"{src.stem}_compressed.pdf"
    res = await _run(compress_pdf, src, out, level=level, image_dpi=image_dpi,
                     jpeg_quality=jpeg_quality, grayscale=grayscale, strip_metadata=strip_metadata)
    return _ok(job_id, res, [out])


@router.post("/batch", dependencies=[Depends(rate_limit)])
async def batch(files: list[UploadFile] = File(...), operation: str = Form(...),
                options: str = Form("{}")):
    if operation not in OPERATIONS:
        _bad(f"operation must be one of: {', '.join(OPERATIONS)}")
    try:
        opts = json.loads(options or "{}")
        if not isinstance(opts, dict):
            raise ValueError
    except ValueError:
        _bad("options must be a JSON object")
    if len(files) > MAX_BATCH_FILES:
        _bad(f"Too many files (max {MAX_BATCH_FILES})")
    job_id, job_dir, in_dir = _workspace()
    paths = await _guard(save_uploads(files, in_dir))
    res = await _run(batch_process, list(paths), job_dir, operation, options=opts)
    infos = [file_info(job_dir / n, job_id) for n in res["outputs"]]
    zip_info = file_info(job_dir / res["zip"], job_id) if res.get("zip") else None
    body = {k: v for k, v in res.items() if k not in ("zip", "outputs")}
    return {"ok": True, "job_id": job_id, **body, "zip": zip_info, "files": infos}


# ───────────────────────── OCR (background + polling) ─────────────────────────
async def _ocr_worker(job_id: str, src: Path, out_dir: Path, kwargs: dict) -> None:
    job = JOBS[job_id]
    async with _OCR_SEM:                           # queue behind other OCR jobs
        job["status"] = "running"

        def progress(done: int, total: int) -> None:   # called from the worker thread
            job["done"], job["total"] = done, total

        try:
            res = await asyncio.to_thread(ocr_pdf, src, out_dir, on_progress=progress, **kwargs)
            body = {k: v for k, v in res.items() if k != "files"}
            body["files"] = [file_info(out_dir / n, job_id) for n in res["files"]]
            job.update(status="done", result=body, done=job["total"] or job["done"])
        except PDFError as exc:
            job.update(status="error", error=str(exc))
        except Exception as exc:                   # never leave a job hanging
            job.update(status="error", error=f"OCR failed: {exc}")


@router.post("/ocr", dependencies=[Depends(rate_limit)])
async def ocr(file: UploadFile = File(...), langs: Optional[str] = Form(None),
              pages: Optional[str] = Form(None), outputs: str = Form("pdf"),
              deskew: bool = Form(True), denoise: bool = Form(True), color: bool = Form(False),
              skip_text_pages: bool = Form(True)):
    outs = tuple(o for o in _listify(outputs) if o in ("pdf", "txt", "docx", "all")) or ("pdf",)
    try:
        lang = _check_langs(langs) if langs else None
    except PDFError as exc:
        _bad(str(exc))
    job_id, job_dir, in_dir = _workspace()
    src = await _guard(save_upload(file, in_dir))
    _prune()
    JOBS[job_id] = {"status": "queued", "done": 0, "total": 0, "result": None,
                    "error": None, "created": time.time()}
    task = asyncio.create_task(_ocr_worker(job_id, src, job_dir, dict(
        langs=lang, pages=pages or None, outputs=outs, deskew=deskew, denoise=denoise,
        color=color, skip_text_pages=skip_text_pages)))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        _bad("Job not found or expired", 404)
    total, done = job["total"], job["done"]
    pct = 100.0 if job["status"] == "done" else (round(done / total * 100, 1) if total else 0.0)
    return {"ok": True, "job_id": job_id, "status": job["status"], "done": done, "total": total,
            "percent": pct, "error": job["error"], "result": job["result"]}
