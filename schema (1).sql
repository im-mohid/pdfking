-- Advanced File Studio: Supabase -> SQL Editor -> New query -> paste -> Run (safe to re-run)

CREATE TABLE IF NOT EXISTS plans (
  key VARCHAR PRIMARY KEY, name VARCHAR, rank INTEGER DEFAULT 0,
  daily_jobs INTEGER, max_mb INTEGER, max_files INTEGER, storage_mb INTEGER DEFAULT 0,
  concurrent INTEGER DEFAULT 1, priority INTEGER DEFAULT 0, retention_hours INTEGER DEFAULT 1,
  api_rpm INTEGER DEFAULT 0, price DOUBLE PRECISION DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY, email VARCHAR, name VARCHAR, pw_hash VARCHAR, google_id VARCHAR,
  plan_key VARCHAR DEFAULT 'free' REFERENCES plans(key),
  is_admin BOOLEAN DEFAULT FALSE, verified BOOLEAN DEFAULT FALSE, banned BOOLEAN DEFAULT FALSE,
  created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id);

CREATE TABLE IF NOT EXISTS files (
  id VARCHAR PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, guest_id VARCHAR,
  name VARCHAR, key VARCHAR, size INTEGER, kind VARCHAR, expires TIMESTAMP,
  created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_files_user_id ON files (user_id);
CREATE INDEX IF NOT EXISTS ix_files_guest_id ON files (guest_id);
CREATE INDEX IF NOT EXISTS ix_files_kind ON files (kind);
CREATE INDEX IF NOT EXISTS ix_files_expires ON files (expires);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id VARCHAR PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, guest_id VARCHAR,
  tool VARCHAR, status VARCHAR, in_names VARCHAR, in_size INTEGER, out_file_id VARCHAR, out_name VARCHAR,
  out_size INTEGER, error VARCHAR, preview TEXT, opts TEXT, via_api BOOLEAN DEFAULT FALSE,
  created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'), started TIMESTAMP, finished TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_processing_jobs_user_id ON processing_jobs (user_id);
CREATE INDEX IF NOT EXISTS ix_processing_jobs_guest_id ON processing_jobs (guest_id);
CREATE INDEX IF NOT EXISTS ix_processing_jobs_tool ON processing_jobs (tool);
CREATE INDEX IF NOT EXISTS ix_processing_jobs_status ON processing_jobs (status);
CREATE INDEX IF NOT EXISTS ix_processing_jobs_created ON processing_jobs (created);

CREATE TABLE IF NOT EXISTS share_links (
  token VARCHAR PRIMARY KEY, file_id VARCHAR REFERENCES files(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, expires TIMESTAMP,
  created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_share_links_file_id ON share_links (file_id);

CREATE TABLE IF NOT EXISTS favorites (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, slug VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_favorites_user_id ON favorites (user_id);

CREATE TABLE IF NOT EXISTS api_keys (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  name VARCHAR, prefix VARCHAR, key_hash VARCHAR, created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc'),
  last_used TIMESTAMP, revoked BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys (key_hash);

CREATE TABLE IF NOT EXISTS api_usage (
  id SERIAL PRIMARY KEY, key_id INTEGER REFERENCES api_keys(id) ON DELETE CASCADE,
  tool VARCHAR, status INTEGER, created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_api_usage_key_id ON api_usage (key_id);
CREATE INDEX IF NOT EXISTS ix_api_usage_created ON api_usage (created);

CREATE TABLE IF NOT EXISTS support_tickets (
  id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  email VARCHAR, subject VARCHAR, message TEXT, status VARCHAR DEFAULT 'open',
  created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
  id SERIAL PRIMARY KEY, admin_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  action VARCHAR, detail VARCHAR, created TIMESTAMP DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS settings (key VARCHAR PRIMARY KEY, value TEXT);

-- Lock the public REST API (backend connects as postgres, which bypasses RLS)
ALTER TABLE plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE files ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE share_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;

-- Plans: key, name, rank, daily_jobs, max_mb, max_files, storage_mb, concurrent, priority, retention_hours, api_rpm, price
INSERT INTO plans (key, name, rank, daily_jobs, max_mb, max_files, storage_mb, concurrent, priority, retention_hours, api_rpm, price) VALUES
  ('guest',    'Guest',    0, 5,    10,  3,   0,     1,  0, 1,    0,  0),
  ('free',     'Free',     1, 25,   25,  10,  100,   1,  0, 1,    0,  0),
  ('pro',      'Pro',      2, 500,  200, 50,  5000,  3,  1, 720,  0,  9),
  ('business', 'Business', 3, 5000, 500, 200, 50000, 10, 1, 2160, 60, 29)
ON CONFLICT (key) DO NOTHING;

-- Private storage bucket for files
INSERT INTO storage.buckets (id, name, public) VALUES ('file-studio', 'file-studio', false)
ON CONFLICT (id) DO NOTHING;
