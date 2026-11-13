"""Central configuration. Every value can be overridden via environment variables."""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

# Working directory for uploads/results (auto-cleaned)
WORK_DIR = Path(os.getenv("WORK_DIR", Path(tempfile.gettempdir()) / "pdfkit"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

# ---- Limits (tuned for Render free tier: 512 MB RAM) ----
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "25"))        # per file
MAX_BATCH_FILES = int(os.getenv("MAX_BATCH_FILES", "20"))  # files per batch
MAX_PAGES_OCR = int(os.getenv("MAX_PAGES_OCR", "50"))    # OCR page cap
OCR_DPI = int(os.getenv("OCR_DPI", "200"))               # 200 = speed/quality balance
OCR_LANGS = os.getenv("OCR_LANGS", "eng+ben")            # English + Bengali

# ---- Cleanup ----
FILE_TTL_SECONDS = int(os.getenv("FILE_TTL_SECONDS", "1800"))   # delete after 30 min
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "300"))    # sweep every 5 min

# ---- Freemium (daily request cap per IP; 0 = unlimited) ----
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "30"))

# ---- PII regex patterns (Bangladesh + international) ----
PII_PATTERNS = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "bd_phone": r"(?:\+?88)?01[3-9]\d{8}",
    "intl_phone": r"\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}",
    "credit_card": r"\b(?:\d[ -]?){13,16}\b",
    "nid": r"\b(?:\d{10}|\d{13}|\d{17})\b",
}

ALLOWED_PDF_MIME = {"application/pdf", "application/x-pdf", "application/octet-stream"}
