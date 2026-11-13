"""Load .env (project root) into os.environ before any module reads config. Real env vars win."""
import os
from pathlib import Path

def _load(p):
    try: lines = p.read_text(encoding="utf-8").splitlines()
    except OSError: return
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln: continue
        k, v = ln.split("=", 1); k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k): os.environ[k] = v

_load(Path(__file__).resolve().parent.parent / ".env")
