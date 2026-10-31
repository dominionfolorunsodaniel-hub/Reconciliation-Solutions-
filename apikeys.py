"""
apikeys.py - API keys so a lender's own system can send a bank statement and get the score back automatically.

The full key is shown ONCE when it is created. Only a hash is stored (like a password), so nobody, including the
site owner, can read a key back later. A key belongs to one company and spends that company's plan checks.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

import db

FMT = db.FMT
MAX_ACTIVE_PER_COMPANY = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
 id INTEGER PRIMARY KEY AUTOINCREMENT, prefix TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
 company_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT,
 uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0
);
"""
db.run_startup_migration(lambda con: con.executescript(SCHEMA))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def _hash(key: str) -> str:
    return hashlib.sha256(("apikey:" + key).encode()).hexdigest()


def create(user_id: int, company_id: int, label: str) -> tuple[str, dict]:
    label = (label or "").strip()[:60] or "My system"
    with db._db() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM api_keys WHERE company_id = ? AND revoked = 0", (company_id,)).fetchone()["n"]
        if n >= MAX_ACTIVE_PER_COMPANY:
            raise ValueError(f"You already have {MAX_ACTIVE_PER_COMPANY} active keys. Switch one off first.")
        key = "ra_" + secrets.token_urlsafe(32)
        con.execute("INSERT INTO api_keys(prefix, key_hash, label, company_id, user_id, created_at) VALUES (?,?,?,?,?,?)",
                    (key[:10], _hash(key), label, company_id, user_id, _now()))
        kid = con.execute("SELECT id FROM api_keys WHERE key_hash = ?", (_hash(key),)).fetchone()["id"]
    return key, {"id": kid, "prefix": key[:10], "label": label}


def verify(key: str) -> dict | None:
    if not key or not key.startswith("ra_") or len(key) > 120:
        return None
    with db._db() as con:
        row = con.execute("SELECT id, company_id, user_id, label, revoked, prefix FROM api_keys WHERE key_hash = ?", (_hash(key),)).fetchone()
        if not row or row["revoked"]:
            return None
        con.execute("UPDATE api_keys SET last_used_at = ?, uses = uses + 1 WHERE id = ?", (_now(), row["id"]))
    return dict(row)


def list_for_company(company_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, prefix, label, created_at, last_used_at, uses, revoked, user_id FROM api_keys "
                           "WHERE company_id = ? ORDER BY id DESC LIMIT 50", (company_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke(key_id: int, company_id: int) -> bool:
    with db._db() as con:
        c = con.execute("UPDATE api_keys SET revoked = 1 WHERE id = ? AND company_id = ?", (key_id, company_id))
        return (c.rowcount or 0) > 0
