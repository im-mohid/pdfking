"""Shared helpers: job dirs, upload validation, ZIP, rate limit, auto-cleanup."""
import asyncio
import re
import shutil
import time
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable

import fitz  # PyMuPDF
from fastapi import HTTPException, Request, UploadFile

from app import config


class PDFError(Exception):
    """Raised for corrupted / encrypted / invalid PDFs (shown to the user)."""


# ---------------------------------------------------------------- jobs
def new_job() -> tuple[str, Path]:
    """Create an isolated working folder for one request."""
    job_id = uuid.uuid4().hex[:16]
    path = config.WORK_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return job_id, path


def job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise HTTPException(400, "Invalid job id")
    return config.WORK_DIR / job_id


def safe_name(name: str, default: str = "file") -> str:
    """Strip path parts / unsafe chars (keeps unicode letters, e.g. Bengali)."""
    name = Path(name or default).name
    name = re.sub(r"[^\w.\- ()]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:120] or default


def result_file(job_id: str, filename: str) -> Path:
    """Resolve a result file safely (blocks path traversal)."""
    base = job_path(job_id).resolve()
    target = (base / safe_name(filename)).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "File not found or expired")
    return target


# ------------------------------------------------------------- uploads
async def save_upload(up: UploadFile, folder: Path, *, pdf_only: bool = True) -> Path:
    """Stream upload to disk with size cap, then validate it."""
    max_bytes = config.MAX_FILE_MB * 1024 * 1024
    dest = folder / safe_name(up.filename)
    if dest.exists():  # avoid overwriting same-named files in a batch
        dest = folder / f"{uuid.uuid4().hex[:6]}_{dest.name}"

    size = 0
    with open(dest, "wb") as f:
        while chunk := await up.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"'{up.filename}' exceeds {config.MAX_FILE_MB} MB limit")
            f.write(chunk)

    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"'{up.filename}' is empty")

    if pdf_only:
        try:
            await asyncio.to_thread(validate_pdf, dest)
        except PDFError as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(422, f"'{up.filename}': {e}")
    return dest


def validate_pdf(path: Path) -> int:
    """Return page count or raise PDFError."""
    with open(path, "rb") as f:
        if b"%PDF" not in f.read(1024):
            raise PDFError("not a valid PDF file")
    try:
        doc = fitz.open(path)
    except Exception:
        raise PDFError("file is corrupted and cannot be opened")
    try:
        if doc.needs_pass:
            raise PDFError("file is password-protected")
        if doc.page_count == 0:
            raise PDFError("PDF has no pages")
        return doc.page_count
    finally:
        doc.close()


async def save_uploads(files: Iterable[UploadFile], folder: Path, *, pdf_only: bool = True) -> list[Path]:
    files = list(files)
    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) > config.MAX_BATCH_FILES:
        raise HTTPException(400, f"Maximum {config.MAX_BATCH_FILES} files per batch")
    return [await save_upload(f, folder, pdf_only=pdf_only) for f in files]


# ----------------------------------------------------------------- zip
def make_zip(files: Iterable[Path], out_path: Path) -> Path:
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=f.name)
    return out_path


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def file_info(path: Path, job_id: str) -> dict:
    """Uniform result descriptor sent back to the frontend."""
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "size_human": human_size(path.stat().st_size),
        "url": f"/api/download/{job_id}/{path.name}",
    }


# ---------------------------------------------------------- rate limit
_usage: dict[str, tuple[date, int]] = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


async def rate_limit(request: Request):
    """FastAPI dependency: daily free-tier cap per IP (in-memory)."""
    if config.FREE_DAILY_LIMIT <= 0:
        return
    ip, today = client_ip(request), date.today()
    day, count = _usage.get(ip, (today, 0))
    if day != today:
        day, count = today, 0
    if count >= config.FREE_DAILY_LIMIT:
        raise HTTPException(429, "Free daily limit reached. Upgrade to Pro for unlimited use.")
    _usage[ip] = (day, count + 1)


# ------------------------------------------------------------- cleanup
def sweep_expired() -> int:
    """Delete job folders older than TTL. Returns number removed."""
    now, removed = time.time(), 0
    for d in config.WORK_DIR.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > config.FILE_TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


async def cleanup_loop():
    """Background task started from main.py lifespan."""
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL)
        await asyncio.to_thread(sweep_expired)
