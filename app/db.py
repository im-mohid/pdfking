import os
from datetime import datetime
from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, text)
from sqlalchemy.orm import declarative_base, sessionmaker

def _url():
    u = (os.getenv("DATABASE_URL") or "").strip()
    if not u:
        if os.getenv("RENDER"): raise RuntimeError("DATABASE_URL is not set. Render -> your service -> Environment -> add DATABASE_URL (Supabase Session pooler URI).")
        u = "sqlite:///./local.db"
    u = u.replace("postgres://", "postgresql://", 1)
    if u.startswith("postgresql") and "sslmode" not in u and "localhost" not in u:
        u += ("&" if "?" in u else "?") + "sslmode=require"   # Supabase requires SSL
    return u

engine = create_engine(_url(), pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300) if not _url().startswith("sqlite") \
    else create_engine(_url(), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()
now = datetime.utcnow

class Plan(Base):
    __tablename__ = "plans"
    key = Column(String, primary_key=True); name = Column(String); rank = Column(Integer, default=0)
    daily_jobs = Column(Integer); max_mb = Column(Integer); max_files = Column(Integer); storage_mb = Column(Integer, default=0)
    concurrent = Column(Integer, default=1); priority = Column(Integer, default=0); retention_hours = Column(Integer, default=1)
    api_rpm = Column(Integer, default=0); price = Column(Float, default=0)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True); email = Column(String, unique=True, index=True); name = Column(String)
    pw_hash = Column(String); google_id = Column(String, index=True); plan_key = Column(String, ForeignKey("plans.key"), default="free")
    is_admin = Column(Boolean, default=False); verified = Column(Boolean, default=False); banned = Column(Boolean, default=False)
    created = Column(DateTime, default=now)

class Job(Base):
    __tablename__ = "processing_jobs"
    id = Column(String, primary_key=True); user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    guest_id = Column(String, index=True); tool = Column(String, index=True); status = Column(String, index=True)
    in_names = Column(String); in_size = Column(Integer); out_file_id = Column(String); out_name = Column(String); out_size = Column(Integer)
    error = Column(String); preview = Column(Text); opts = Column(Text); via_api = Column(Boolean, default=False)
    created = Column(DateTime, default=now, index=True); started = Column(DateTime); finished = Column(DateTime)

class File(Base):
    __tablename__ = "files"
    id = Column(String, primary_key=True); user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    guest_id = Column(String, index=True); name = Column(String); key = Column(String); size = Column(Integer); kind = Column(String, index=True)
    expires = Column(DateTime, index=True); created = Column(DateTime, default=now)

class Share(Base):
    __tablename__ = "share_links"
    token = Column(String, primary_key=True); file_id = Column(String, ForeignKey("files.id", ondelete="CASCADE"), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE")); expires = Column(DateTime); created = Column(DateTime, default=now)

class Fav(Base):
    __tablename__ = "favorites"
    id = Column(Integer, primary_key=True); user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True); slug = Column(String)

class ApiKey(Base):
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True); user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name = Column(String); prefix = Column(String); key_hash = Column(String, unique=True, index=True)
    created = Column(DateTime, default=now); last_used = Column(DateTime); revoked = Column(Boolean, default=False)

class ApiLog(Base):
    __tablename__ = "api_usage"
    id = Column(Integer, primary_key=True); key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), index=True)
    tool = Column(String); status = Column(Integer); created = Column(DateTime, default=now, index=True)

class Ticket(Base):
    __tablename__ = "support_tickets"
    id = Column(Integer, primary_key=True); user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    email = Column(String); subject = Column(String); message = Column(Text); status = Column(String, default="open"); created = Column(DateTime, default=now)

class Audit(Base):
    __tablename__ = "admin_audit_logs"
    id = Column(Integer, primary_key=True); admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    action = Column(String); detail = Column(String); created = Column(DateTime, default=now)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True); value = Column(Text)

PLANS = [  # key, name, rank, daily, mb, files, storage_mb, concurrent, priority, retention_h, api_rpm, price
    ("guest", "Guest", 0, 5, 10, 3, 0, 1, 0, 1, 0, 0), ("free", "Free", 1, 25, 25, 10, 100, 1, 0, 1, 0, 0),
    ("pro", "Pro", 2, 500, 200, 50, 5000, 3, 1, 720, 0, 9), ("business", "Business", 3, 5000, 500, 200, 50000, 10, 1, 2160, 60, 29)]

def init_db():
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":   # Supabase exposes public tables via REST: lock them down (backend role bypasses RLS)
        with engine.begin() as c:
            for t in Base.metadata.tables: c.execute(text(f'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY'))
    with SessionLocal() as db:
        if not db.query(Plan).count():
            for p in PLANS: db.add(Plan(key=p[0], name=p[1], rank=p[2], daily_jobs=p[3], max_mb=p[4], max_files=p[5], storage_mb=p[6],
                                        concurrent=p[7], priority=p[8], retention_hours=p[9], api_rpm=p[10], price=p[11]))
            db.commit()
