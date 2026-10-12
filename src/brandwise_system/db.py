"""SQLite: użytkownicy, passkeys, sesje, zaproszenia, wyzwania WebAuthn."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    handle BLOB UNIQUE NOT NULL,             -- WebAuthn user.id (losowe, bez danych osobowych)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    id BLOB PRIMARY KEY,                     -- credential id
    user_id INTEGER NOT NULL REFERENCES users(id),
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS challenges (
    id TEXT PRIMARY KEY,                     -- losowy identyfikator w ciasteczku
    challenge BLOB NOT NULL,
    purpose TEXT NOT NULL,                   -- register|login
    email TEXT,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user_email TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.isoformat()


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def q(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def log(self, event: str, email: str | None = None, detail: str = "") -> None:
        self.q("INSERT INTO audit(at, user_email, event, detail) VALUES (?,?,?,?)", (iso(now()), email, event, detail))

    # ---------- zaproszenia (pierwszy passkey / nowe urządzenie bez dostępu) ----------
    def create_invite(self, email: str, minutes: int = 30) -> str:
        token = secrets.token_urlsafe(32)
        self.q("INSERT INTO invites(token_hash, email, expires_at) VALUES (?,?,?)",
               (sha(token), email, iso(now() + timedelta(minutes=minutes))))
        return token

    def invite(self, token: str):
        row = self.one("SELECT * FROM invites WHERE token_hash=?", (sha(token),))
        if row is None or row["used_at"] or row["expires_at"] < iso(now()):
            return None
        return row

    def use_invite(self, token: str) -> None:
        self.q("UPDATE invites SET used_at=? WHERE token_hash=?", (iso(now()), sha(token)))

    # ---------- użytkownicy i passkeys ----------
    def user_by_email(self, email: str):
        return self.one("SELECT * FROM users WHERE email=?", (email,))

    def user(self, user_id: int):
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def ensure_user(self, email: str):
        u = self.user_by_email(email)
        if u is None:
            self.q("INSERT INTO users(email, handle, created_at) VALUES (?,?,?)",
                   (email, secrets.token_bytes(32), iso(now())))
            u = self.user_by_email(email)
        return u

    def credentials_of(self, user_id: int):
        return self.q("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at", (user_id,))

    def credential(self, cred_id: bytes):
        return self.one("SELECT * FROM credentials WHERE id=?", (cred_id,))

    def add_credential(self, user_id: int, cred_id: bytes, public_key: bytes, sign_count: int, label: str) -> None:
        self.q("INSERT INTO credentials(id, user_id, public_key, sign_count, label, created_at) VALUES (?,?,?,?,?,?)",
               (cred_id, user_id, public_key, sign_count, label, iso(now())))

    def touch_credential(self, cred_id: bytes, sign_count: int) -> None:
        self.q("UPDATE credentials SET sign_count=?, last_used_at=? WHERE id=?", (sign_count, iso(now()), cred_id))

    def delete_credential(self, user_id: int, cred_id: bytes) -> bool:
        if len(self.credentials_of(user_id)) <= 1:
            return False                     # ostatniego klucza nie usuwamy – nie zamykamy sobie drzwi
        self.q("DELETE FROM credentials WHERE id=? AND user_id=?", (cred_id, user_id))
        return True

    # ---------- wyzwania WebAuthn ----------
    def put_challenge(self, challenge: bytes, purpose: str, email: str | None = None) -> str:
        cid = secrets.token_urlsafe(24)
        self.q("DELETE FROM challenges WHERE expires_at < ?", (iso(now()),))
        self.q("INSERT INTO challenges(id, challenge, purpose, email, expires_at) VALUES (?,?,?,?,?)",
               (cid, challenge, purpose, email, iso(now() + timedelta(minutes=5))))
        return cid

    def take_challenge(self, cid: str, purpose: str):
        row = self.one("SELECT * FROM challenges WHERE id=? AND purpose=?", (cid, purpose))
        self.q("DELETE FROM challenges WHERE id=?", (cid,))      # jednorazowe
        if row is None or row["expires_at"] < iso(now()):
            return None
        return row

    # ---------- sesje ----------
    def new_session(self, user_id: int, days: int) -> str:
        token = secrets.token_urlsafe(32)
        self.q("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?,?,?,?)",
               (sha(token), user_id, iso(now() + timedelta(days=days)), iso(now())))
        return token

    def session_user(self, token: str):
        row = self.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                       "WHERE s.token_hash=? AND s.expires_at > ?", (sha(token), iso(now())))
        return row

    def end_session(self, token: str) -> None:
        self.q("DELETE FROM sessions WHERE token_hash=?", (sha(token),))
