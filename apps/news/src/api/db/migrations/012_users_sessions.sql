-- 012_users_sessions.sql: multi-user access with roles (Phase 6).
--
-- The GUI grows from one shared credential to three roles (admin | user |
-- viewer). Non-admin users self-register with their email and stay PENDING
-- until the admin approves them and assigns a role; they never store a
-- password — every login mints a 6-digit code sent by email (Phase 6 OTP).
-- The admin keeps the local ADMIN_USERNAME/ADMIN_PASSWORD_HASH credentials
-- and is NOT a row here.
--
-- sessions: one random HttpOnly cookie token per login (12 h), replacing the
-- legacy cookie that carried the raw password. login_codes: only the sha256
-- hash of the code is stored, with attempts/expiry/consumption columns so a
-- guessed code cannot be replayed and brute force is bounded.

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('user', 'viewer')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'disabled')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP,
    last_login_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS login_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP,
    last_sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_login_codes_email ON login_codes(email);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    identity TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user', 'viewer')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
