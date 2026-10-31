"""
sharing.py - private share links for a Statement Score result.

A customer can give a lender or accountant a link to ONE result. Safety rules:
  * the link holds a long random secret; only its hash is stored (like a password)
  * it expires (1-30 days) and the customer can switch it off at any time
  * the page shows the result only (no files, no login, no other data) and is hidden from search engines
  * the result is a frozen copy: figures and narration examples are not re-read from the account
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets

import db

FMT = db.FMT
MAX_ACTIVE_PER_USER = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_results (
 id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, company_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL, title TEXT NOT NULL, payload TEXT NOT NULL, expires_at TEXT NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0, views INTEGER NOT NULL DEFAULT 0, last_viewed_at TEXT,
 created_at TEXT NOT NULL
);
"""
db.run_startup_migration(lambda con: con.executescript(SCHEMA))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def _hash(token: str) -> str:
    return hashlib.sha256(("share:" + token).encode()).hexdigest()


def safe_payload(r: dict, company_name: str) -> dict:
    """Keep only what a lender needs. Individual transaction descriptions are left out."""
    flags = [{"severity": f.get("severity"), "title": f.get("title"), "detail": f.get("detail")} for f in (r.get("flags") or [])]
    ig = r.get("integrity") or {}
    cats = r.get("categories") or {}
    return {
        "company": company_name, "generated": _now(), "score": r["score"], "verdict": r["verdict"],
        "eligibility": r["eligibility"], "period": r["period"], "totals": r["totals"], "factors": r["factors"],
        "strengths": r["strengths"], "concerns": r["concerns"], "advice": r.get("advice", []), "loan_guide": r.get("loan_guide"),
        "integrity": {"status": ig.get("status"), "message": ig.get("message"), "checked": ig.get("checked"), "mismatches": ig.get("mismatches")},
        "flags": flags, "spending": cats.get("spending", []), "income": cats.get("income", []),
        "columns": r["columns"], "rows": r["rows"], "chart": r["chart"], "disclaimer": r["disclaimer"],
        "accounts": len(r.get("accounts") or []) or 1, "forecast": r.get("forecast"),
    }


def create(user_id: int, company_id: int, title: str, payload: dict, days: int) -> tuple[str, dict]:
    days = days if days in (1, 7, 30) else 7
    with db._db() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM shared_results WHERE user_id = ? AND revoked = 0 AND expires_at > ?",
                        (user_id, _now())).fetchone()["n"]
        if n >= MAX_ACTIVE_PER_USER:
            raise ValueError(f"You already have {MAX_ACTIVE_PER_USER} active links. Switch some off first.")
        token = secrets.token_urlsafe(32)
        exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime(FMT)
        con.execute("INSERT INTO shared_results(token_hash, company_id, user_id, title, payload, expires_at, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (_hash(token), company_id, user_id, title[:160], db._enc_text(json.dumps(payload, default=str)), exp, _now()))
    return token, {"expires_at": exp, "days": days}


def list_mine(user_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, title, expires_at, revoked, views, last_viewed_at, created_at FROM shared_results "
                           "WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
    now = _now()
    return [{**dict(r), "active": (not r["revoked"]) and r["expires_at"] > now} for r in rows]


def revoke(user_id: int, share_id: int, is_admin: bool = False) -> bool:
    with db._db() as con:
        if is_admin:
            cur = con.execute("UPDATE shared_results SET revoked = 1 WHERE id = ?", (share_id,))
        else:
            cur = con.execute("UPDATE shared_results SET revoked = 1 WHERE id = ? AND user_id = ?", (share_id, user_id))
        return (cur.rowcount or 0) > 0


def revoke_all(user_id: int) -> int:
    with db._db() as con:
        cur = con.execute("UPDATE shared_results SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,))
        return cur.rowcount or 0


def get_public(token: str, count_view: bool = True) -> dict | None:
    if not token or len(token) > 100:
        return None
    with db._db() as con:
        row = con.execute("SELECT id, company_id, payload, expires_at, revoked FROM shared_results WHERE token_hash = ?",
                          (_hash(token),)).fetchone()
        if not row or row["revoked"] or row["expires_at"] <= _now():
            return None
        if count_view:
            con.execute("UPDATE shared_results SET views = views + 1, last_viewed_at = ? WHERE id = ?", (_now(), row["id"]))
    data = json.loads(db._dec_text(row["payload"]))
    data["expires_at"] = row["expires_at"]
    data["_company_id"] = row["company_id"]
    return data
