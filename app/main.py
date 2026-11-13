"""FastAPI entrypoint: lifespan (auto-cleanup), static SPA, routers, error handling."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.routers import api
from app.utils import PDFError, cleanup_loop, sweep_expired

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pdfkit")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background cleaner on boot, stop it on shutdown."""
    sweep_expired()  # clear leftovers from a previous run
    task = asyncio.create_task(cleanup_loop())
    log.info("PDFKit started. WORK_DIR=%s", config.WORK_DIR)
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="PDFKit Pro",
    description="Advanced PDF toolkit: tables, OCR, redaction, diff, compression, batch.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Basic hardening headers on every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# ------------------------------------------------------------ error handling
@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse({"ok": False, "error": "Invalid request. Please check your input."}, status_code=422)


@app.exception_handler(PDFError)
async def pdf_error(_: Request, exc: PDFError):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(
        {"ok": False, "error": "Something went wrong while processing the file. Please try again."},
        status_code=500,
    )


# ------------------------------------------------------------------- routes
app.include_router(api.router, prefix="/api")


@app.get("/health", include_in_schema=False)
async def health():
    """Used by Render health checks / uptime pingers."""
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")
