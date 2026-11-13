# Advanced File Studio

FastAPI + Jinja/Tailwind SaaS: 60 file tools (PDF, Word, Excel, Image, Archive, PowerPoint), no AI/LLM.
Supabase = Postgres + private Storage. Deploy on Render (Docker).

## 1. Supabase
1. New project. **Settings → Database → Connection string → Session pooler** → copy URI into `DATABASE_URL`.
2. **Settings → API**: `SUPABASE_URL` and the **service_role** key → `SUPABASE_SERVICE_KEY` (server-side only, never in frontend).
3. Tables and the private bucket are created automatically on first start. RLS is enabled on every table
   (the backend connects as `postgres`, which bypasses RLS, so the public REST API cannot read your data).

## 2. Google login
Google Cloud Console → Credentials → OAuth client (Web). Redirect URI: `https://YOUR-APP.onrender.com/auth/google/callback`.

## 3. Email (verify / reset)
Render free blocks SMTP, so Resend HTTP API is used. Verify your domain in Resend, set `RESEND_API_KEY` + `MAIL_FROM`.
Without a key, links are printed to the server logs (dev only).

## 4. Render
Push this folder to GitHub → Render → New → Blueprint (uses `render.yaml`, Docker). Fill the env vars.
Register with `ADMIN_EMAIL` to become admin → `/admin`.

## Local
```
pip install -r requirements.txt
cp .env.example .env   # values auto-load from .env – works without Supabase using local disk + SQLite
uvicorn app.main:app --reload
pytest -q
```

## Honest limitations / pending
- Jobs run in in-process thread pools (no Redis/Celery yet). A timed-out job is marked failed but its thread may finish in background; cancel stops queued jobs only.
- Payments, Teams, file version history, malware scanning (ClamAV), Alembic migrations, PDF form filling, DOCX merge, PDF/A, OCR, PPTX→PDF fidelity guarantees are NOT implemented.
- Paid plans are assigned by admin (`/admin`) after the user contacts you.
- Watermark tools: Latin text only. Sign PDF = visual signature, not cryptographic.
- Render free tier sleeps and has 512 MB RAM; use a paid instance for LibreOffice / large files.
