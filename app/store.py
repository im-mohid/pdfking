"""File storage: Supabase Storage (private bucket, signed URLs) with local-disk fallback."""
import logging, os
from urllib.parse import quote
from pathlib import Path
import httpx
from fastapi.responses import FileResponse, RedirectResponse

log = logging.getLogger("fs.store")
URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
BUCKET = os.getenv("SUPABASE_BUCKET", "file-studio")
REMOTE = bool(URL and KEY)
LOCAL = Path(os.getenv("OUT_DIR", "/tmp/fs_out")); LOCAL.mkdir(parents=True, exist_ok=True)
H = {"Authorization": f"Bearer {KEY}", "apikey": KEY}

def init():
    if REMOTE:
        r = httpx.post(f"{URL}/storage/v1/bucket", headers=H, json={"id": BUCKET, "name": BUCKET, "public": False}, timeout=20)
        if r.status_code not in (200, 201, 400, 409): log.warning("bucket create: %s %s", r.status_code, r.text[:200])

def put(key, data):
    if REMOTE:
        r = httpx.post(f"{URL}/storage/v1/object/{BUCKET}/{key}", headers={**H, "x-upsert": "true", "Content-Type": "application/octet-stream"}, content=data, timeout=120)
        if r.status_code >= 300: log.error("upload failed %s %s", r.status_code, r.text[:200]); raise RuntimeError("storage upload failed")
    else:
        p = LOCAL / key.replace("/", "__"); p.write_bytes(data)

def exists(key):
    return True if REMOTE else (LOCAL / key.replace("/", "__")).exists()

def delete(keys):
    if not keys: return
    if REMOTE:
        try: httpx.request("DELETE", f"{URL}/storage/v1/object/{BUCKET}", headers=H, json={"prefixes": list(keys)}, timeout=30)
        except Exception: log.exception("storage delete failed")
    else:
        for k in keys:
            try: (LOCAL / k.replace("/", "__")).unlink()
            except OSError: pass

def serve(key, name, inline=False):
    if REMOTE:
        r = httpx.post(f"{URL}/storage/v1/object/sign/{BUCKET}/{key}", headers=H, json={"expiresIn": 120}, timeout=20)
        if r.status_code != 200: return None
        u = f"{URL}/storage/v1{r.json()['signedURL']}" + ("" if inline else "&download=" + quote(name))
        return RedirectResponse(u, 302, headers={"Cache-Control": "private, no-store"})
    p = LOCAL / key.replace("/", "__")
    if not p.exists(): return None
    return FileResponse(p, filename=name, content_disposition_type="inline" if inline else "attachment", headers={"Cache-Control": "private, no-store"})
