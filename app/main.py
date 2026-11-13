import hashlib, io, json, logging, os, secrets, shutil, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
import bcrypt, httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, File as UFile, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func
from starlette.middleware.sessions import SessionMiddleware
from . import store, tools as T
from .db import (ApiKey, ApiLog, Audit, Fav, File, Job, Plan, SessionLocal, Setting, Share, Ticket, User, init_db, now)

log = logging.getLogger("fs"); logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent; E = os.getenv; MB = 1024 * 1024; START = time.time()
SECRET = E("SECRET_KEY", "dev-secret-change-me"); BASE_URL = E("BASE_URL", "http://localhost:8000").rstrip("/")
BRAND = E("BRAND", "Advanced File Studio"); ADMIN_EMAIL = (E("ADMIN_EMAIL") or "").lower()
JOB_TIMEOUT = int(E("JOB_TIMEOUT", "110")); INDIR = Path(E("IN_DIR", "/tmp/fs_in")); INDIR.mkdir(parents=True, exist_ok=True)
POOLS = [ThreadPoolExecutor(int(E("WORKERS_STANDARD", "1"))), ThreadPoolExecutor(int(E("WORKERS_PRIORITY", "2")))]
FUT = {}
STR = {"en": dict(tools="All tools", pricing="Pricing", dashboard="Dashboard", files="My Files", login="Login", signup="Sign up", logout="Logout",
                  hero1="All your file tools,", hero2="one studio", search="Search tools…", hero_sub="PDF, Word, Excel, image and ZIP tools. Fast, private, no AI, no third-party uploads."),
       "bn": dict(tools="সব টুল", pricing="প্রাইসিং", dashboard="ড্যাশবোর্ড", files="আমার ফাইল", login="লগইন", signup="সাইন আপ", logout="লগআউট",
                  hero1="আপনার সব ফাইল টুল,", hero2="এক জায়গায়", search="টুল খুঁজুন…", hero_sub="PDF, Word, Excel, ইমেজ ও ZIP টুল। দ্রুত, নিরাপদ, কোনো AI নেই।")}

app = FastAPI(title=BRAND, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site="lax", https_only=BASE_URL.startswith("https"), max_age=60 * 60 * 24 * 14)
templates = Jinja2Templates(directory=str(BASE / "templates"))
ser = URLSafeTimedSerializer(SECRET)
oauth = OAuth()
if E("GOOGLE_CLIENT_ID"):
    oauth.register("google", client_id=E("GOOGLE_CLIENT_ID"), client_secret=E("GOOGLE_CLIENT_SECRET"),
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration", client_kwargs={"scope": "openid email profile"})

@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    if request.method == "POST" and not p.startswith("/api/v1/"):   # CSRF: same-origin check (plus SameSite=Lax cookies)
        o = request.headers.get("origin") or request.headers.get("referer")
        if not o or urlparse(o).netloc != request.headers.get("host"): return PlainTextResponse("Cross-site request blocked.", 403)
    r = await call_next(request)
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "strict-origin-when-cross-origin",
                      "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
    if BASE_URL.startswith("https"): r.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return r

def cleaner():
    while True:
        try:
            with SessionLocal() as db:
                old = db.query(File).filter(File.expires != None, File.expires < now()).limit(500).all()  # noqa: E711
                store.delete([f.key for f in old])
                for f in old: db.query(Share).filter(Share.file_id == f.id).delete(); db.delete(f)
                db.query(Share).filter(Share.expires < now()).delete(); db.commit()
            for d in INDIR.glob("*"):
                if time.time() - d.stat().st_mtime > 7200: shutil.rmtree(d, ignore_errors=True)
        except Exception: log.exception("cleaner")
        time.sleep(600)

@app.on_event("startup")
def startup():
    init_db(); store.init(); threading.Thread(target=cleaner, daemon=True).start()
    log.info("storage=%s tools=%d office_engine=%s", "supabase" if store.REMOTE else "local", len(T.TOOLS), bool(T.SOFFICE))

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ---------- helpers ----------
def setting(db, k, d=""): s = db.get(Setting, k); return s.value if s and s.value is not None else d
def set_setting(db, k, v):
    s = db.get(Setting, k) or Setting(key=k); s.value = v; db.add(s); db.commit()
def visible_tools(db):
    dis = set(setting(db, "disabled").split(","))
    return {s: t for s, t in T.TOOLS.items() if t["avail"] and s not in dis}
def cur_user(request, db):
    uid = request.session.get("uid"); u = db.get(User, uid) if uid else None
    if u and u.banned: request.session.clear(); return None
    return u
def gid(request): return request.session.setdefault("gid", uuid.uuid4().hex)
def plan_of(db, u): return db.get(Plan, u.plan_key if u else "guest") or db.get(Plan, "guest")
def kind_of(n):
    e = T.ext(n)
    return ("pdf" if e == ".pdf" else "image" if e in (".jpg", ".jpeg", ".png", ".webp", ".heic") else "archive" if e == ".zip" else
            "sheet" if e in (".xlsx", ".csv") else "doc" if e in (".docx", ".pptx", ".html") else "text" if e in (".txt", ".json") else "other")
def flash(request, m): request.session["flash"] = m

def render(request, name, db, status=200, **ctx):
    lang = request.session.get("lang", "en"); S = STR.get(lang, STR["en"]); tl = visible_tools(db)
    ctx.update(user=cur_user(request, db), brand=BRAND, base_url=BASE_URL, flash=request.session.pop("flash", None), tools=tl,
               cats=sorted({t["cat"] for t in tl.values()}), google=bool(E("GOOGLE_CLIENT_ID")), path=request.url.path, lang=lang,
               announce=setting(db, "announce"), maint=setting(db, "maintenance") == "1", _=lambda k: S.get(k, STR["en"].get(k, k)))
    return templates.TemplateResponse(request, name, ctx, status_code=status)

def send_mail(to, subject, html):
    k = E("RESEND_API_KEY")
    if not k: log.warning("MAIL (no provider configured) to=%s subject=%s\n%s", to, subject, html); return
    try: httpx.post("https://api.resend.com/emails", timeout=15, headers={"Authorization": f"Bearer {k}"}, json={"from": E("MAIL_FROM", "onboarding@resend.dev"), "to": [to], "subject": subject, "html": html})
    except Exception: log.exception("mail failed")

def hash_pw(p): return bcrypt.hashpw(p.encode()[:72], bcrypt.gensalt(12)).decode()
def check_pw(p, h): return bool(h) and bcrypt.checkpw(p.encode()[:72], h.encode())
_att = {}
def throttled(key, limit=10, window=600):
    t = time.time(); L = [x for x in _att.get(key, []) if t - x < window]; L.append(t); _att[key] = L; return len(L) > limit
def ip(request): return request.client.host if request.client else "?"
def audit(db, admin, action, detail=""): db.add(Audit(admin_id=admin.id, action=action, detail=detail[:300])); db.commit()

def usage_today(db, u, g):
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(func.count(Job.id)).filter(Job.status.in_(["done", "queued", "running"]), Job.created >= start)
    return q.filter(Job.user_id == u.id if u else Job.guest_id == g).scalar()
def storage_used(db, uid):
    return db.query(func.coalesce(func.sum(File.size), 0)).filter(File.user_id == uid, (File.expires == None) | (File.expires > now())).scalar()  # noqa: E711
def owns(request, db, uid, guest):
    u = cur_user(request, db); return bool((u and uid == u.id) or (not uid and guest and guest == request.session.get("gid")))
def alive(f): return bool(f and (f.expires is None or f.expires > now()))

# ---------- job pipeline ----------
def work(job_id, o):
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job or job.status != "queued": return
        job.status, job.started = "running", now(); db.commit()
        try:
            d = INDIR / job_id
            items = [(f.name.split("__", 1)[1], f.read_bytes()) for f in sorted(d.iterdir(), key=lambda p: int(p.name.split("__")[0]))]
            ex = ThreadPoolExecutor(1)
            try: name, data = ex.submit(T.TOOLS[job.tool]["fn"], items, o).result(timeout=JOB_TIMEOUT)
            finally: ex.shutdown(wait=False)
            T.validate_output(name, data); db.refresh(job)
            if job.status == "cancelled": return
            owner = db.get(User, job.user_id) if job.user_id else None; plan = plan_of(db, owner)
            if owner and storage_used(db, owner.id) + len(data) > plan.storage_mb * MB: raise ValueError("Cloud storage is full. Delete old files or upgrade.")
            fid = uuid.uuid4().hex; key = f"{'u' + str(job.user_id) if job.user_id else 'g'}/{fid}"
            store.put(key, data)
            db.add(File(id=fid, user_id=job.user_id, guest_id=job.guest_id, name=name, key=key, size=len(data), kind=kind_of(name), expires=now() + timedelta(hours=plan.retention_hours)))
            job.status, job.out_file_id, job.out_name, job.out_size = "done", fid, name, len(data)
            if T.ext(name) in (".txt", ".csv"): job.preview = data[:3000].decode("utf-8", "replace")
        except FTimeout: job.status, job.error = "failed", "Processing took too long. Try a smaller file."
        except ValueError as e: job.status, job.error = "failed", str(e)[:300]
        except Exception: log.exception("job %s failed", job_id); job.status, job.error = "failed", "Could not process this file. It may be damaged or unsupported."
        finally:
            job.finished = now(); db.commit(); FUT.pop(job_id, None)

def handle_upload(db, slug, files, opts_json, u, g, via_api=False):
    spec = visible_tools(db).get(slug)
    if not spec: return {"error": "Tool unavailable."}, 404
    if setting(db, "maintenance") == "1" and not (u and u.is_admin): return {"error": "Maintenance in progress. Please try again shortly."}, 503
    plan = plan_of(db, u)
    if plan.rank < spec["rank"]: return {"error": f"{spec['name']} requires a {'free account' if spec['rank'] == 1 else 'Pro plan'}.", "upgrade": True}, 403
    if usage_today(db, u, g) >= plan.daily_jobs: return {"error": f"Daily limit reached ({plan.daily_jobs} jobs).", "upgrade": True}, 429
    own = Job.user_id == u.id if u else Job.guest_id == g
    if db.query(func.count(Job.id)).filter(Job.status.in_(["queued", "running"]), own).scalar() >= plan.concurrent:
        return {"error": f"You can run {plan.concurrent} job(s) at once. Wait for the current job to finish."}, 429
    maxf = plan.max_files if spec["multi"] else 1
    if not files or len(files) > maxf: return {"error": f"Select 1-{maxf} file(s)."}, 400
    limit = plan.max_mb * MB; allowed = [a.strip() for a in spec["accept"].split(",")]; items = []
    for f in files:
        n = T._safe(f.filename); b = f.file.read(limit + 1)
        if len(b) > limit: return {"error": f"{n}: exceeds the {plan.max_mb} MB limit.", "upgrade": True}, 413
        if not b: return {"error": f"{n} is empty."}, 400
        if "*" not in allowed and T.ext(n) not in allowed: return {"error": f"{n}: unsupported type. Allowed: {spec['accept']}"}, 400
        if not T.sig_ok(n, b): return {"error": f"{n} is not a valid {T.ext(n)} file."}, 400
        items.append((n, b))
    try: raw = json.loads(opts_json or "{}"); raw = raw if isinstance(raw, dict) else {}
    except Exception: raw = {}
    o = {p["name"]: str(raw.get(p["name"], p["default"]))[:2000] for p in spec["opts"]}
    safe_o = {k: v for k, v in o.items() if not any(p["name"] == k and p["type"] == "password" for p in spec["opts"])}
    job = Job(id=uuid.uuid4().hex, user_id=u.id if u else None, guest_id=g, tool=slug, status="queued", in_names=", ".join(n for n, _ in items)[:300],
              in_size=sum(len(b) for _, b in items), opts=json.dumps(safe_o), via_api=via_api)
    d = INDIR / job.id; d.mkdir(parents=True)
    for i, (n, b) in enumerate(items): (d / f"{i}__{n}").write_bytes(b)
    db.add(job); db.commit()
    FUT[job.id] = POOLS[1 if plan.priority > 0 else 0].submit(work, job.id, o)
    return {"job_id": job.id, "status": "queued"}, 202

def job_json(db, j, dl=None):
    d = {"id": j.id, "status": j.status, "error": j.error, "tool": j.tool, "in_size": j.in_size, "preview": j.preview}
    if j.status == "done":
        f = db.get(File, j.out_file_id)
        d["out"] = {"name": j.out_name, "size": j.out_size, "kind": f.kind, "url": dl or f"/f/{f.id}", "inline": f"/f/{f.id}?inline=1", "expires": f.expires.isoformat() if f.expires else None} if alive(f) else None
    return d

class Mem:
    def __init__(self, n, b): self.filename, self.file = n, io.BytesIO(b)

# ---------- public pages ----------
@app.get("/health")
def health(): return {"ok": True}

@app.get("/lang/{code}")
def lang(code: str, request: Request):
    if code in STR: request.session["lang"] = code
    return RedirectResponse(request.headers.get("referer") or "/", 303)

@app.get("/")
def home(request: Request, db=Depends(get_db)):
    u = cur_user(request, db); favs = [f.slug for f in db.query(Fav).filter(Fav.user_id == u.id)] if u else []
    return render(request, "home.html", db, favs=favs, title=f"{BRAND} – PDF, Word, Excel, Image & ZIP Tools Online",
                  desc="Merge, split, compress and convert PDF; process Excel, images and ZIP files. Private, no AI, files auto-deleted.")

@app.get("/tools/{slug}")
def tool_page(slug: str, request: Request, db=Depends(get_db)):
    tl = visible_tools(db); t = tl.get(slug)
    if not t: return render(request, "page.html", db, 404, title="Tool not found", body="<p>This tool does not exist or is currently disabled.</p>")
    u = cur_user(request, db); plan = plan_of(db, u)
    return render(request, "tool.html", db, t=t, plan=plan, used=usage_today(db, u, gid(request)), is_fav=bool(u and db.query(Fav).filter(Fav.user_id == u.id, Fav.slug == slug).first()),
                  related=[x for x in tl.values() if x["cat"] == t["cat"] and x["slug"] != slug][:5], title=f'{t["name"]} Online – {BRAND}', desc=t["desc"])

@app.get("/pricing")
def pricing(request: Request, db=Depends(get_db)):
    return render(request, "pricing.html", db, plans=db.query(Plan).order_by(Plan.rank).all(), title=f"Pricing – {BRAND}", desc="Free, Pro and Business plans.")

@app.get("/api-docs")
def api_docs(request: Request, db=Depends(get_db)): return render(request, "apidocs.html", db, title=f"API Documentation – {BRAND}", desc="REST API for file processing.")

@app.get("/contact")
def contact_form(request: Request, db=Depends(get_db)): return render(request, "contact.html", db, title="Contact & Support")

@app.post("/contact")
def contact(request: Request, email: str = Form(...), subject: str = Form(...), message: str = Form(...), db=Depends(get_db)):
    if throttled("c" + ip(request), 5, 3600): flash(request, "Too many messages. Try later.")
    elif "@" in email and message.strip():
        u = cur_user(request, db); db.add(Ticket(user_id=u.id if u else None, email=email[:200], subject=subject[:200], message=message[:5000])); db.commit(); flash(request, "Message sent. We will reply by email.")
    return RedirectResponse("/contact", 303)

@app.get("/privacy")
def privacy(request: Request, db=Depends(get_db)):
    return render(request, "page.html", db, title="Privacy Policy", body="""<p>Uploaded files are processed on our servers and never sent to AI or third-party processing services. Files are stored in private storage and are only reachable by you (or by share links you create).</p>
<p><b>Retention:</b> output files are deleted automatically after the retention period of your plan (Guest/Free: 1 hour; Pro: 30 days; Business: 90 days). Uploaded input files are removed within 2 hours. You can delete files at any time.</p>
<p>We store your account email/name and job metadata (tool, file names, sizes, times) for history and quotas. Request account deletion any time from Account settings.</p>""")

@app.get("/terms")
def terms(request: Request, db=Depends(get_db)):
    return render(request, "page.html", db, title="Terms of Service", body="<p>Use the service only for files you own or are authorised to process. Unlock tools require the correct password; no cracking is provided. Abuse, malware or excessive automation may lead to suspension. The service is provided as-is without warranty of uninterrupted availability.</p>")

@app.get("/robots.txt")
def robots(): return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /dashboard\nDisallow: /files\nDisallow: /account\nDisallow: /api\nSitemap: {BASE_URL}/sitemap.xml\n")

@app.get("/sitemap.xml")
def sitemap(db=Depends(get_db)):
    urls = [BASE_URL + p for p in ("/", "/pricing", "/api-docs", "/contact")] + [f"{BASE_URL}/tools/{s}" for s in visible_tools(db)]
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>", media_type="application/xml")

# ---------- auth ----------
def login_user(request, u): request.session["uid"] = u.id
def send_verify(u): send_mail(u.email, f"Verify your email – {BRAND}", f'<p>Hi {u.name},</p><p><a href="{BASE_URL}/verify/{ser.dumps({"u": u.id}, salt="verify")}">Verify your email</a></p>')

@app.get("/login")
def login_form(request: Request, db=Depends(get_db)): return render(request, "auth.html", db, mode="login", title="Login")

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    if throttled("l" + ip(request)): return render(request, "auth.html", db, 429, mode="login", title="Login", error="Too many attempts. Try later.")
    u = db.query(User).filter(User.email == email.strip().lower()).first()
    if not u or u.banned or not check_pw(password, u.pw_hash): return render(request, "auth.html", db, 400, mode="login", title="Login", error="Invalid email or password.")
    login_user(request, u); return RedirectResponse("/dashboard", 303)

@app.get("/register")
def register_form(request: Request, db=Depends(get_db)): return render(request, "auth.html", db, mode="register", title="Create account")

@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    email = email.strip().lower()
    def err(m): return render(request, "auth.html", db, 400, mode="register", title="Create account", error=m)
    if throttled("r" + ip(request), 5, 3600): return err("Too many sign-ups. Try later.")
    if "@" not in email or len(password) < 8: return err("Valid email and a password of 8+ characters are required.")
    if db.query(User).filter(User.email == email).first(): return err("This email is already registered.")
    u = User(email=email, name=name.strip()[:60] or "User", pw_hash=hash_pw(password), is_admin=(email == ADMIN_EMAIL), plan_key="free")
    db.add(u); db.commit(); send_verify(u); login_user(request, u); flash(request, "Account created. Please verify your email."); return RedirectResponse("/dashboard", 303)

@app.get("/verify/{token}")
def verify(token: str, request: Request, db=Depends(get_db)):
    try: u = db.get(User, ser.loads(token, salt="verify", max_age=259200)["u"])
    except BadSignature: flash(request, "Verification link is invalid or expired."); return RedirectResponse("/", 303)
    u.verified = True; db.commit(); flash(request, "Email verified."); return RedirectResponse("/dashboard", 303)

@app.get("/logout")
def logout(request: Request): request.session.clear(); return RedirectResponse("/", 303)

@app.get("/forgot")
def forgot_form(request: Request, db=Depends(get_db)): return render(request, "auth.html", db, mode="forgot", title="Reset password")

@app.post("/forgot")
def forgot(request: Request, email: str = Form(...), db=Depends(get_db)):
    if not throttled("f" + ip(request), 5, 3600):
        u = db.query(User).filter(User.email == email.strip().lower()).first()
        if u: send_mail(u.email, f"Reset your password – {BRAND}", f'<p><a href="{BASE_URL}/reset/{ser.dumps({"u": u.id, "h": (u.pw_hash or "")[-12:]}, salt="reset")}">Reset password</a> (valid 1 hour, single use)</p>')
    return render(request, "auth.html", db, mode="forgot", title="Reset password", info="If that email exists, a reset link has been sent.")

@app.get("/reset/{token}")
def reset_form(token: str, request: Request, db=Depends(get_db)): return render(request, "auth.html", db, mode="reset", token=token, title="New password")

@app.post("/reset/{token}")
def reset(token: str, request: Request, password: str = Form(...), db=Depends(get_db)):
    try:
        d = ser.loads(token, salt="reset", max_age=3600); u = db.get(User, d["u"])
        if not u or (u.pw_hash or "")[-12:] != d["h"]: raise BadSignature("used")
    except BadSignature: return render(request, "auth.html", db, 400, mode="forgot", title="Reset password", error="This link is invalid, already used or expired.")
    if len(password) < 8: return render(request, "auth.html", db, 400, mode="reset", token=token, title="New password", error="Minimum 8 characters.")
    u.pw_hash = hash_pw(password); db.commit(); flash(request, "Password changed. Please log in."); return RedirectResponse("/login", 303)

@app.get("/auth/google")
async def g_login(request: Request):
    if not E("GOOGLE_CLIENT_ID"): return RedirectResponse("/login", 303)
    return await oauth.google.authorize_redirect(request, BASE_URL + "/auth/google/callback")

@app.get("/auth/google/callback")
async def g_cb(request: Request, db=Depends(get_db)):
    try: info = (await oauth.google.authorize_access_token(request))["userinfo"]
    except Exception: flash(request, "Google sign-in failed."); return RedirectResponse("/login", 303)
    if not info.get("email_verified", True): flash(request, "Google email is not verified."); return RedirectResponse("/login", 303)
    email = info["email"].lower(); u = db.query(User).filter((User.google_id == info["sub"]) | (User.email == email)).first()
    if not u: u = User(email=email, name=info.get("name") or email.split("@")[0], google_id=info["sub"], verified=True, is_admin=(email == ADMIN_EMAIL), plan_key="free"); db.add(u)
    elif u.banned: flash(request, "This account is suspended."); return RedirectResponse("/login", 303)
    else: u.google_id = u.google_id or info["sub"]; u.verified = True
    db.commit(); login_user(request, u); return RedirectResponse("/dashboard", 303)

# ---------- user area ----------
def need_user(request, db):
    u = cur_user(request, db); return u

@app.get("/dashboard")
def dashboard(request: Request, db=Depends(get_db)):
    u = need_user(request, db)
    if not u: return RedirectResponse("/login", 303)
    jobs = db.query(Job).filter(Job.user_id == u.id).order_by(Job.created.desc()).limit(40).all()
    fids = {j.out_file_id for j in jobs if j.out_file_id}; fl = {f.id: f for f in db.query(File).filter(File.id.in_(fids))} if fids else {}
    for j in jobs: j.alive = alive(fl.get(j.out_file_id))
    recent = list(dict.fromkeys(j.tool for j in jobs))[:6]
    month = now() - timedelta(days=30)
    return render(request, "dashboard.html", db, title="Dashboard", plan=plan_of(db, u), used=usage_today(db, u, ""), jobs=jobs, recent=recent, storage=storage_used(db, u.id) / MB,
                  favs=[f.slug for f in db.query(Fav).filter(Fav.user_id == u.id)], monthly=db.query(func.count(Job.id)).filter(Job.user_id == u.id, Job.status == "done", Job.created >= month).scalar())

@app.get("/files")
def my_files(request: Request, q: str = "", kind: str = "", sort: str = "new", db=Depends(get_db)):
    u = need_user(request, db)
    if not u: return RedirectResponse("/login", 303)
    qs = db.query(File).filter(File.user_id == u.id, (File.expires == None) | (File.expires > now()))  # noqa: E711
    if q: qs = qs.filter(File.name.ilike(f"%{q}%"))
    if kind: qs = qs.filter(File.kind == kind)
    qs = qs.order_by({"size": File.size.desc(), "name": File.name.asc()}.get(sort, File.created.desc()))
    return render(request, "files.html", db, title="My Files", files=qs.limit(200).all(), q=q, kind=kind, sort=sort, plan=plan_of(db, u), storage=storage_used(db, u.id) / MB)

def file_of(request, db, fid):
    f = db.get(File, fid); u = cur_user(request, db)
    return f if f and u and f.user_id == u.id else None

@app.post("/files/{fid}/rename")
def f_rename(fid: str, request: Request, name: str = Form(...), db=Depends(get_db)):
    f = file_of(request, db, fid)
    if f and name.strip(): f.name = T._safe(name.strip())[:120]; db.commit()
    return RedirectResponse("/files", 303)

@app.post("/files/{fid}/delete")
def f_delete(fid: str, request: Request, db=Depends(get_db)):
    f = file_of(request, db, fid)
    if f: store.delete([f.key]); db.query(Share).filter(Share.file_id == f.id).delete(); db.delete(f); db.commit(); flash(request, "File deleted.")
    return RedirectResponse("/files", 303)

@app.post("/files/{fid}/share")
def f_share(fid: str, request: Request, hours: int = Form(24), db=Depends(get_db)):
    f = file_of(request, db, fid)
    if f:
        t = secrets.token_urlsafe(18); db.add(Share(token=t, file_id=f.id, user_id=f.user_id, expires=now() + timedelta(hours=max(1, min(hours, 720))))); db.commit()
        flash(request, f"Share link (expires in {hours}h): {BASE_URL}/s/{t}")
    return RedirectResponse("/files", 303)

@app.get("/f/{fid}")
def get_file(fid: str, request: Request, inline: int = 0, db=Depends(get_db)):
    f = db.get(File, fid)
    if not alive(f) or not owns(request, db, f.user_id, f.guest_id): return PlainTextResponse("File not found or expired.", 404)
    return store.serve(f.key, f.name, bool(inline)) or PlainTextResponse("File not found or expired.", 404)

@app.get("/s/{token}")
def shared(token: str, db=Depends(get_db)):
    s = db.get(Share, token); f = db.get(File, s.file_id) if s else None
    if not s or s.expires < now() or not alive(f): return PlainTextResponse("This link is invalid or has expired.", 404)
    return store.serve(f.key, f.name) or PlainTextResponse("File not found.", 404)

@app.post("/fav/{slug}")
def fav(slug: str, request: Request, db=Depends(get_db)):
    u = cur_user(request, db)
    if not u or slug not in T.TOOLS: return JSONResponse({"error": "Login required"}, 401)
    x = db.query(Fav).filter(Fav.user_id == u.id, Fav.slug == slug).first()
    if x: db.delete(x)
    else: db.add(Fav(user_id=u.id, slug=slug))
    db.commit(); return {"fav": not x}

@app.get("/account")
def account(request: Request, db=Depends(get_db)):
    u = need_user(request, db)
    if not u: return RedirectResponse("/login", 303)
    keys = db.query(ApiKey).filter(ApiKey.user_id == u.id).order_by(ApiKey.id.desc()).all(); since = now() - timedelta(days=1)
    for k in keys: k.calls = db.query(func.count(ApiLog.id)).filter(ApiLog.key_id == k.id, ApiLog.created >= since).scalar()
    logs = db.query(ApiLog).join(ApiKey, ApiKey.id == ApiLog.key_id).filter(ApiKey.user_id == u.id).order_by(ApiLog.id.desc()).limit(15).all()
    return render(request, "account.html", db, title="Account", plan=plan_of(db, u), keys=keys, logs=logs, newkey=request.session.pop("newkey", None))

@app.post("/account/profile")
def acc_profile(request: Request, name: str = Form(...), db=Depends(get_db)):
    u = need_user(request, db)
    if u and name.strip(): u.name = name.strip()[:60]; db.commit(); flash(request, "Profile updated.")
    return RedirectResponse("/account", 303)

@app.post("/account/password")
def acc_password(request: Request, current: str = Form(""), new: str = Form(...), db=Depends(get_db)):
    u = need_user(request, db)
    if not u: return RedirectResponse("/login", 303)
    if u.pw_hash and not check_pw(current, u.pw_hash): flash(request, "Current password is incorrect.")
    elif len(new) < 8: flash(request, "New password must be at least 8 characters.")
    else: u.pw_hash = hash_pw(new); db.commit(); flash(request, "Password changed.")
    return RedirectResponse("/account", 303)

@app.post("/account/delete")
def acc_delete(request: Request, confirm: str = Form(""), db=Depends(get_db)):
    u = need_user(request, db)
    if not u or confirm != "DELETE": flash(request, 'Type DELETE to confirm.'); return RedirectResponse("/account", 303)
    fs = db.query(File).filter(File.user_id == u.id).all(); store.delete([f.key for f in fs])
    for f in fs: db.query(Share).filter(Share.file_id == f.id).delete()
    ids = [k.id for k in db.query(ApiKey).filter(ApiKey.user_id == u.id)]
    if ids: db.query(ApiLog).filter(ApiLog.key_id.in_(ids)).delete(synchronize_session=False)
    for M in (File, Fav, ApiKey, Job): db.query(M).filter(M.user_id == u.id).delete()
    db.query(Share).filter(Share.user_id == u.id).delete(); db.delete(u); db.commit(); request.session.clear(); return RedirectResponse("/", 303)

@app.post("/account/keys")
def key_new(request: Request, name: str = Form("My key"), db=Depends(get_db)):
    u = need_user(request, db)
    if not u: return RedirectResponse("/login", 303)
    if plan_of(db, u).api_rpm <= 0: flash(request, "API access is available on the Business plan.")
    elif db.query(ApiKey).filter(ApiKey.user_id == u.id, ApiKey.revoked == False).count() >= 5: flash(request, "Maximum 5 active keys.")  # noqa: E712
    else:
        raw = "fs_" + secrets.token_urlsafe(32); db.add(ApiKey(user_id=u.id, name=name[:60], prefix=raw[:10], key_hash=hashlib.sha256(raw.encode()).hexdigest())); db.commit(); request.session["newkey"] = raw
    return RedirectResponse("/account", 303)

@app.post("/account/keys/{kid}/revoke")
def key_revoke(kid: int, request: Request, db=Depends(get_db)):
    u = need_user(request, db); k = db.get(ApiKey, kid)
    if u and k and k.user_id == u.id: k.revoked = True; db.commit()
    return RedirectResponse("/account", 303)

# ---------- processing (web) ----------
@app.post("/api/run/{slug}")
def run(slug: str, request: Request, files: list[UploadFile] = UFile(...), opts: str = Form("{}"), db=Depends(get_db)):
    r, code = handle_upload(db, slug, files, opts, cur_user(request, db), gid(request)); return JSONResponse(r, code)

def job_for(request, db, jid):
    j = db.get(Job, jid); return j if j and owns(request, db, j.user_id, j.guest_id) else None

@app.get("/api/job/{jid}")
def job_status(jid: str, request: Request, db=Depends(get_db)):
    j = job_for(request, db, jid); return job_json(db, j) if j else JSONResponse({"error": "Not found"}, 404)

@app.post("/api/job/{jid}/cancel")
def job_cancel(jid: str, request: Request, db=Depends(get_db)):
    j = job_for(request, db, jid)
    if not j: return JSONResponse({"error": "Not found"}, 404)
    if j.status in ("queued", "running"):
        f = FUT.get(jid); f and f.cancel(); j.status, j.error, j.finished = "cancelled", "Cancelled", now(); db.commit()
    return job_json(db, j)

@app.post("/api/job/{jid}/retry")
def job_retry(jid: str, request: Request, db=Depends(get_db)):
    j = job_for(request, db, jid); d = INDIR / jid
    if not j or not d.exists(): return JSONResponse({"error": "Original files are no longer available. Please upload again."}, 410)
    items = [Mem(f.name.split("__", 1)[1], f.read_bytes()) for f in sorted(d.iterdir(), key=lambda p: int(p.name.split("__")[0]))]
    r, code = handle_upload(db, j.tool, items, j.opts, cur_user(request, db), gid(request)); return JSONResponse(r, code)

# ---------- public API v1 ----------
def api_auth(request, db):
    h = request.headers.get("authorization", "")
    k = db.query(ApiKey).filter(ApiKey.key_hash == hashlib.sha256(h[7:].strip().encode()).hexdigest(), ApiKey.revoked == False).first() if h.lower().startswith("bearer ") else None  # noqa: E712
    u = db.get(User, k.user_id) if k else None
    if not u or u.banned: return None, None, ({"error": "Invalid API key."}, 401)
    p = plan_of(db, u)
    if p.api_rpm <= 0: return None, None, ({"error": "API access is not enabled for your plan."}, 403)
    if throttled(f"k{k.id}", p.api_rpm, 60): return None, None, ({"error": "Rate limit exceeded."}, 429)
    k.last_used = now(); db.commit(); return u, k, None

@app.get("/api/v1/tools")
def api_tools(request: Request, db=Depends(get_db)):
    u, k, e = api_auth(request, db)
    if e: return JSONResponse(*e)
    return [{"slug": t["slug"], "name": t["name"], "category": t["cat"], "accept": t["accept"], "multi": t["multi"], "options": [{"name": p["name"], "default": p["default"], "choices": p["choices"]} for p in t["opts"]]} for t in visible_tools(db).values()]

@app.post("/api/v1/tools/{slug}")
def api_run(slug: str, request: Request, files: list[UploadFile] = UFile(...), opts: str = Form("{}"), db=Depends(get_db)):
    u, k, e = api_auth(request, db)
    if e: return JSONResponse(*e)
    r, code = handle_upload(db, slug, files, opts, u, f"api{u.id}", True); db.add(ApiLog(key_id=k.id, tool=slug, status=code)); db.commit()
    if code == 202: r["status_url"] = f"{BASE_URL}/api/v1/jobs/{r['job_id']}"
    return JSONResponse(r, code)

@app.get("/api/v1/jobs/{jid}")
def api_job(jid: str, request: Request, db=Depends(get_db)):
    u, k, e = api_auth(request, db)
    if e: return JSONResponse(*e)
    j = db.get(Job, jid)
    if not j or j.user_id != u.id: return JSONResponse({"error": "Not found"}, 404)
    return job_json(db, j, dl=f"{BASE_URL}/api/v1/jobs/{jid}/download")

@app.get("/api/v1/jobs/{jid}/download")
def api_dl(jid: str, request: Request, db=Depends(get_db)):
    u, k, e = api_auth(request, db)
    if e: return JSONResponse(*e)
    j = db.get(Job, jid); f = db.get(File, j.out_file_id) if j and j.user_id == u.id and j.out_file_id else None
    return (store.serve(f.key, f.name) if alive(f) else None) or JSONResponse({"error": "Not found or expired"}, 404)

# ---------- admin ----------
def admin_only(request, db):
    u = cur_user(request, db); return u if u and u.is_admin else None

@app.get("/admin")
def admin(request: Request, q: str = "", db=Depends(get_db)):
    a = admin_only(request, db)
    if not a: return PlainTextResponse("Forbidden", 403)
    d1, d7, d30 = now() - timedelta(days=1), now() - timedelta(days=7), now() - timedelta(days=30); J = Job
    c = lambda *f: db.query(func.count(J.id)).filter(*f).scalar()  # noqa: E731
    stats = dict(users=db.query(func.count(User.id)).scalar(), new7=db.query(func.count(User.id)).filter(User.created >= d7).scalar(),
                 active=db.query(func.count(func.distinct(J.user_id))).filter(J.created >= d1).scalar(), jobs24=c(J.created >= d1), jobs30=c(J.created >= d30),
                 failed24=c(J.created >= d1, J.status == "failed"), api24=db.query(func.count(ApiLog.id)).filter(ApiLog.created >= d1).scalar(),
                 storage=(db.query(func.coalesce(func.sum(File.size), 0)).scalar() or 0) / MB, paid=db.query(func.count(User.id)).filter(User.plan_key.in_(["pro", "business"])).scalar())
    us = db.query(User).order_by(User.created.desc())
    if q: us = us.filter(User.email.ilike(f"%{q}%"))
    du = shutil.disk_usage("/tmp")
    health = dict(storage="Supabase Storage" if store.REMOTE else "Local disk (temporary)", office=bool(T.SOFFICE), uptime_h=round((time.time() - START) / 3600, 1),
                  disk_free_gb=round(du.free / 1e9, 1), q_std=POOLS[0]._work_queue.qsize(), q_pri=POOLS[1]._work_queue.qsize(), running=db.query(func.count(J.id)).filter(J.status == "running").scalar())
    return render(request, "admin.html", db, title="Admin", stats=stats, health=health, q=q, plans=db.query(Plan).order_by(Plan.rank).all(), users=us.limit(30).all(),
                  top=db.query(J.tool, func.count(J.id)).filter(J.created >= d30).group_by(J.tool).order_by(func.count(J.id).desc()).limit(10).all(),
                  alltools=[t for t in T.TOOLS.values() if t["avail"]], disabled=set(setting(db, "disabled").split(",")), maintenance=setting(db, "maintenance") == "1",
                  announcement=setting(db, "announce"), tickets=db.query(Ticket).order_by(Ticket.id.desc()).limit(20).all(), audits=db.query(Audit).order_by(Audit.id.desc()).limit(15).all())

@app.post("/admin/plan/{key}")
def admin_plan(key: str, request: Request, name: str = Form(...), daily_jobs: int = Form(...), max_mb: int = Form(...), max_files: int = Form(...), storage_mb: int = Form(...),
               concurrent: int = Form(...), priority: int = Form(...), retention_hours: int = Form(...), api_rpm: int = Form(...), price: float = Form(0), db=Depends(get_db)):
    a = admin_only(request, db)
    if not a: return PlainTextResponse("Forbidden", 403)
    p = db.get(Plan, key)
    for k, v in dict(name=name, daily_jobs=daily_jobs, max_mb=max_mb, max_files=max_files, storage_mb=storage_mb, concurrent=concurrent, priority=priority, retention_hours=retention_hours, api_rpm=api_rpm, price=price).items(): setattr(p, k, v)
    db.commit(); audit(db, a, "plan.update", key); return RedirectResponse("/admin", 303)

@app.post("/admin/user")
def admin_user(request: Request, email: str = Form(...), plan: str = Form(...), banned: str = Form("no"), db=Depends(get_db)):
    a = admin_only(request, db)
    if not a: return PlainTextResponse("Forbidden", 403)
    u = db.query(User).filter(User.email == email.strip().lower()).first()
    if u and db.get(Plan, plan): u.plan_key, u.banned = plan, banned == "yes"; db.commit(); audit(db, a, "user.update", f"{u.email} plan={plan} banned={banned}")
    return RedirectResponse("/admin", 303)

@app.post("/admin/settings")
def admin_settings(request: Request, announce: str = Form(""), maintenance: str = Form("0"), disabled: list[str] = Form([]), db=Depends(get_db)):
    a = admin_only(request, db)
    if not a: return PlainTextResponse("Forbidden", 403)
    set_setting(db, "announce", announce[:300]); set_setting(db, "maintenance", "1" if maintenance == "1" else "0"); set_setting(db, "disabled", ",".join(x for x in disabled if x in T.TOOLS))
    audit(db, a, "settings.update", f"maint={maintenance} disabled={len(disabled)}"); return RedirectResponse("/admin", 303)

@app.post("/admin/ticket/{tid}")
def admin_ticket(tid: int, request: Request, db=Depends(get_db)):
    a = admin_only(request, db); t = db.get(Ticket, tid)
    if a and t: t.status = "closed"; db.commit(); audit(db, a, "ticket.close", str(tid))
    return RedirectResponse("/admin", 303)
