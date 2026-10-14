"""
db.py - companies, users + roles + per-company access, sessions, saved reports (encrypted at rest),
question history, audit log and data retention.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from functools import lru_cache

import pandas as pd

import core
import security
from security import ROLE_RANK

log = logging.getLogger("db")

DATA_DIR = os.getenv("DATA_DIR", "data")
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "reports.db")
os.makedirs(FILES_DIR, exist_ok=True)

ROLES = tuple(ROLE_RANK)  # viewer < analyst < admin
SESSION_HOURS = 12
FMT = "%Y-%m-%d %H:%M:%S"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,40}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    stored_ext TEXT,
    sheet TEXT,
    rows INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by INTEGER
);
CREATE TABLE IF NOT EXISTS descriptions (
    report_id INTEGER PRIMARY KEY,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    report_id INTEGER,
    user_id INTEGER,
    question TEXT NOT NULL,
    plan_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    telegram_id INTEGER UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    alerts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_companies (
    user_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, company_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
CREATE TABLE IF NOT EXISTS branding (
    company_id INTEGER PRIMARY KEY,
    display_name TEXT,
    color TEXT,
    logo_path TEXT
);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    user_id INTEGER,
    question TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'latest',
    last_run TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_seen (key TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeat (name TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    source TEXT,
    action TEXT NOT NULL,
    company_id INTEGER,
    detail TEXT
);
"""


@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def _ensure_column(con, table: str, column: str, decl: str) -> None:
    if column not in [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


with _db() as _con:
    _con.executescript(SCHEMA)
    _ensure_column(_con, "reports", "stored_ext", "TEXT")
    _ensure_column(_con, "reports", "uploaded_by", "INTEGER")
    _ensure_column(_con, "queries", "user_id", "INTEGER")
    _ensure_column(_con, "users", "totp_secret", "TEXT")
    _ensure_column(_con, "users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(_con, "users", "totp_last_step", "INTEGER NOT NULL DEFAULT 0")


# ---- encryption helpers ----------------------------------------------------- #
def _enc_text(t: str | None) -> str | None:
    return security.encrypt(t.encode()).decode() if t else t


def _dec_text(t: str | None) -> str | None:
    return security.decrypt(t.encode()).decode() if t else t


def check_encryption() -> bool:
    """Call at startup. Warns loudly when stored data would NOT be encrypted."""
    if security.decrypt(security.encrypt(b"ok")) != b"ok":
        raise RuntimeError("Encryption self-test failed")
    on = security.encryption_enabled()
    if not on:
        log.warning("ENCRYPTION_KEY is not set: reports are stored UNENCRYPTED. "
                    "Generate a key with `python security.py genkey` and set REQUIRE_ENCRYPTION=1.")
    return on


# ---- companies ---------------------------------------------------------------- #
def get_or_create_company(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Company name can't be empty.")
    with _db() as con:
        row = con.execute("SELECT * FROM companies WHERE name = ?", (name,)).fetchone()
        if row is None:
            cur = con.execute("INSERT INTO companies(name, created_at) VALUES (?, ?)", (name, _now()))
            row = con.execute("SELECT * FROM companies WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def list_companies(user: dict | None = None) -> list[dict]:
    """All companies, or only the ones this (non-admin) user may access."""
    q = ("SELECT c.id, c.name, COUNT(r.id) AS reports FROM companies c "
         "LEFT JOIN reports r ON r.company_id = c.id ")
    params: tuple = ()
    if user and user["role"] != "admin":
        q += "WHERE c.id IN (SELECT company_id FROM user_companies WHERE user_id = ?) "
        params = (user["id"],)
    with _db() as con:
        return [dict(r) for r in con.execute(q + "GROUP BY c.id ORDER BY c.name", params).fetchall()]


def get_company(company_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        return dict(row) if row else None


def can_access(user: dict, company_id: int) -> bool:
    if user["role"] == "admin":
        return get_company(company_id) is not None
    with _db() as con:
        return con.execute(
            "SELECT 1 FROM user_companies WHERE user_id = ? AND company_id = ?", (user["id"], company_id)
        ).fetchone() is not None


# ---- users ---------------------------------------------------------------------- #
def _user(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "telegram_id": row["telegram_id"], "active": bool(row["active"]),
            "alerts": bool(row["alerts"]), "totp_enabled": bool(row["totp_enabled"]),
            "created_at": row["created_at"]}


def get_user(user_id: int) -> dict | None:
    with _db() as con:
        return _user(con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_telegram(telegram_id: int) -> dict | None:
    with _db() as con:
        return _user(con.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone())


def list_users() -> list[dict]:
    with _db() as con:
        users = [_user(r) for r in con.execute("SELECT * FROM users ORDER BY username").fetchall()]
        for u in users:
            u["company_ids"] = [r["company_id"] for r in con.execute(
                "SELECT company_id FROM user_companies WHERE user_id = ?", (u["id"],))]
    return users


def set_user_companies(user_id: int, company_ids) -> None:
    with _db() as con:
        con.execute("DELETE FROM user_companies WHERE user_id = ?", (user_id,))
        for cid in set(company_ids or []):
            con.execute("INSERT OR IGNORE INTO user_companies(user_id, company_id) VALUES (?, ?)", (user_id, cid))


def _check_password(pw: str) -> None:
    if len(pw) < 10:
        raise ValueError("Password must be at least 10 characters.")


def create_user(username: str, password: str, role: str = "analyst",
                telegram_id: int | None = None, company_ids=()) -> dict:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-40 characters - letters, digits or _ . @ -")
    if role not in ROLES:
        raise ValueError("Unknown role.")
    _check_password(password)
    try:
        with _db() as con:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, telegram_id, created_at) VALUES (?,?,?,?,?)",
                (username, security.hash_password(password), role, telegram_id or None, _now()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError("That username or Telegram ID is already in use.") from e
    set_user_companies(uid, company_ids)
    return get_user(uid)


def update_user(user_id: int, **f) -> dict | None:
    sets, vals = [], []
    if "role" in f:
        if f["role"] not in ROLES:
            raise ValueError("Unknown role.")
        sets.append("role = ?"); vals.append(f["role"])
    if "active" in f:
        sets.append("active = ?"); vals.append(1 if f["active"] else 0)
    if "alerts" in f:
        sets.append("alerts = ?"); vals.append(1 if f["alerts"] else 0)
    if "telegram_id" in f:
        sets.append("telegram_id = ?"); vals.append(f["telegram_id"] or None)
    if f.get("reset_2fa"):
        sets += ["totp_secret = NULL", "totp_enabled = 0", "totp_last_step = 0"]
    if f.get("password"):
        _check_password(f["password"])
        sets.append("password_hash = ?"); vals.append(security.hash_password(f["password"]))
    try:
        with _db() as con:
            if sets:
                con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*vals, user_id))
            if ("active" in f and not f["active"]) or f.get("password"):
                con.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # force re-login
    except sqlite3.IntegrityError as e:
        raise ValueError("That Telegram ID is already linked to another user.") from e
    if f.get("company_ids") is not None:
        set_user_companies(user_id, f["company_ids"])
    return get_user(user_id)


_DUMMY_HASH = security.hash_password("not-a-real-password")


def verify_login(username: str, password: str) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None:
        security.verify_password(password, _DUMMY_HASH)  # equalize timing
        return None
    if not security.verify_password(password, row["password_hash"]) or not row["active"]:
        return None
    return _user(row)


def bootstrap_admin() -> bool:
    """First start: create the admin from DASHBOARD_USER / DASHBOARD_PASSWORD."""
    with _db() as con:
        if con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return False
    user, pw = os.getenv("DASHBOARD_USER", "admin"), os.getenv("DASHBOARD_PASSWORD", "")
    if not pw:
        raise RuntimeError("No users exist yet: set DASHBOARD_USER and DASHBOARD_PASSWORD "
                           "(10+ characters) to create the first admin.")
    create_user(user, pw, "admin")
    log.info("Created first admin user %r", user)
    return True


def users_to_alert(company_id: int) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM users WHERE active = 1 AND alerts = 1 AND telegram_id IS NOT NULL AND "
            "(role = 'admin' OR id IN (SELECT user_id FROM user_companies WHERE company_id = ?))",
            (company_id,)).fetchall()
        return [_user(r) for r in rows]


# ---- sessions ----------------------------------------------------------------- #
def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=SESSION_HOURS)).strftime(FMT)
    with _db() as con:
        con.execute("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?,?,?,?)",
                    (_th(token), user_id, exp, _now()))
    return token


def get_session_user(token: str) -> dict | None:
    with _db() as con:
        row = con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1", (_th(token), _now())).fetchone()
        return _user(row)


def delete_session(token: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM sessions WHERE token_hash = ?", (_th(token),))


# ---- audit log ------------------------------------------------------------------ #
def audit(user: dict | None, action: str, company_id: int | None = None, detail: str = "",
          source: str = "web") -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO audit(ts, user_id, username, source, action, company_id, detail) VALUES (?,?,?,?,?,?,?)",
            (_now(), user["id"] if user else None, user["username"] if user else None,
             source, action, company_id, (detail or "")[:500]))


def list_audit(limit: int = 100) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT a.id, a.ts, a.username, a.source, a.action, a.detail, c.name AS company "
            "FROM audit a LEFT JOIN companies c ON c.id = a.company_id ORDER BY a.id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---- reports (files are encrypted on disk) -------------------------------------- #
def add_report(company_id: int, filename: str, data: bytes, rows: int, stored_ext: str,
               sheet: str | None = None, uploaded_by: int | None = None) -> int:
    path = os.path.join(FILES_DIR, f"{uuid.uuid4().hex}.bin")
    with open(path, "wb") as f:
        f.write(security.encrypt(data))
    with _db() as con:
        cur = con.execute(
            "INSERT INTO reports(company_id, filename, stored_path, stored_ext, sheet, rows, uploaded_at, "
            "uploaded_by) VALUES (?,?,?,?,?,?,?,?)",
            (company_id, filename, path, stored_ext, sheet, rows, _now(), uploaded_by))
        return cur.lastrowid


def save_upload(company_id: int, filename: str, data: bytes, df: pd.DataFrame,
                user_id: int | None = None) -> int:
    """Store an upload. If card numbers were masked or CVV/PIN columns dropped, the ORIGINAL file is
    discarded and only the cleaned version is kept - full card data is never written to disk."""
    if df.attrs.get("sanitized"):
        stored, ext = core.to_clean_csv(df), "csv"
    else:
        stored = data
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "csv"
    return add_report(company_id, filename, stored, len(df), ext, uploaded_by=user_id)


def list_reports(company_id: int) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT id, filename, sheet, rows, uploaded_at FROM reports WHERE company_id = ? ORDER BY id",
            (company_id,)).fetchall()
        return [dict(r) for r in rows]


def get_report(report_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(row) if row else None


def _remove_report_rows(con, report_id: int, path: str) -> None:
    con.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    con.execute("DELETE FROM descriptions WHERE report_id = ?", (report_id,))
    try:
        os.remove(path)
    except OSError:
        pass


def delete_report(report_id: int) -> bool:
    r = get_report(report_id)
    if not r:
        return False
    with _db() as con:
        _remove_report_rows(con, report_id, r["stored_path"])
    _load_cached.cache_clear()
    return True


def set_sheet(report_id: int, sheet: str) -> None:
    with _db() as con:
        con.execute("UPDATE reports SET sheet = ? WHERE id = ?", (sheet, report_id))
    _load_cached.cache_clear()


@lru_cache(maxsize=16)
def _load_cached(report_id: int, sheet: str | None) -> pd.DataFrame:
    r = get_report(report_id)
    if not r:
        raise ValueError("That report no longer exists.")
    with open(r["stored_path"], "rb") as f:
        data = security.decrypt(f.read())
    ext = r.get("stored_ext") or r["filename"].rsplit(".", 1)[-1]
    df, _, _ = core.load_report(data, f"stored.{ext}", sheet)
    return df


def get_frame(report_id: int) -> pd.DataFrame:
    r = get_report(report_id)
    if not r:
        raise ValueError("That report no longer exists.")
    return _load_cached(report_id, r["sheet"])


def get_company_frame(company_id: int) -> pd.DataFrame:
    """All of a company's reports stacked together, tagged with _report / _uploaded."""
    parts = [
        get_frame(r["id"]).assign(_report=r["filename"], _uploaded=pd.to_datetime(r["uploaded_at"][:10]))
        for r in list_reports(company_id)
    ]
    if not parts:
        raise ValueError("No reports saved for this company yet.")
    return pd.concat(parts, ignore_index=True, sort=False)


def resolve_frame(company_id: int, report_id: int | None) -> pd.DataFrame:
    """report_id=None means 'all reports of the company combined'."""
    if report_id is None:
        return get_company_frame(company_id)
    r = get_report(report_id)
    if not r or r["company_id"] != company_id:
        raise ValueError("That report doesn't belong to this company.")
    return get_frame(report_id)


# ---- descriptions + history (encrypted text) -------------------------------------- #
def save_description(report_id: int, desc: dict) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO descriptions(report_id, json) VALUES (?, ?)",
                    (report_id, _enc_text(json.dumps(desc, default=str))))


def get_description(report_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT json FROM descriptions WHERE report_id = ?", (report_id,)).fetchone()
    return json.loads(_dec_text(row["json"])) if row else None


def log_query(company_id: int, report_id: int | None, question: str, plan: dict | None,
              user_id: int | None = None) -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO queries(company_id, report_id, user_id, question, plan_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (company_id, report_id, user_id, _enc_text(question), _enc_text(json.dumps(plan)) if plan else None, _now()))


def list_queries(company_id: int, limit: int = 10) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT id, report_id, question, created_at FROM queries WHERE company_id = ? "
            "ORDER BY id DESC LIMIT ?", (company_id, limit)).fetchall()
    return [{**dict(r), "question": _dec_text(r["question"])} for r in rows]


# ---- retention -------------------------------------------------------------------- #
def purge_expired(report_days: int | None = None, audit_days: int | None = None) -> dict:
    """Delete reports older than RETENTION_DAYS (0 = keep forever) and old audit rows."""
    report_days = int(os.getenv("RETENTION_DAYS", "0")) if report_days is None else report_days
    audit_days = int(os.getenv("AUDIT_RETENTION_DAYS", "365")) if audit_days is None else audit_days
    ago = lambda d: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=d)).strftime(FMT)  # noqa: E731
    removed = {"reports": 0, "queries": 0, "audit": 0}
    with _db() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        if report_days > 0:
            cutoff = ago(report_days)
            for r in con.execute("SELECT id, stored_path FROM reports WHERE uploaded_at < ?", (cutoff,)).fetchall():
                _remove_report_rows(con, r["id"], r["stored_path"])
                removed["reports"] += 1
            removed["queries"] = con.execute("DELETE FROM queries WHERE created_at < ?", (cutoff,)).rowcount
        if audit_days > 0:
            removed["audit"] = con.execute("DELETE FROM audit WHERE ts < ?", (ago(audit_days),)).rowcount
    _load_cached.cache_clear()
    return removed


# ---- extra user helpers + two-factor (TOTP) ------------------------------------------- #
def get_user_by_name(username: str) -> dict | None:
    with _db() as con:
        return _user(con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone())


def admins_to_alert() -> list[dict]:
    with _db() as con:
        rows = con.execute("SELECT * FROM users WHERE role = 'admin' AND active = 1 AND alerts = 1 "
                           "AND telegram_id IS NOT NULL").fetchall()
        return [_user(r) for r in rows]


def get_totp_secret(user_id: int) -> str | None:
    with _db() as con:
        row = con.execute("SELECT totp_secret FROM users WHERE id = ?", (user_id,)).fetchone()
    return _dec_text(row["totp_secret"]) if row and row["totp_secret"] else None


def set_totp(user_id: int, secret: str | None, enabled: bool) -> None:
    with _db() as con:
        con.execute("UPDATE users SET totp_secret = ?, totp_enabled = ?, totp_last_step = 0 WHERE id = ?",
                    (_enc_text(secret), 1 if enabled else 0, user_id))


def verify_totp(user_id: int, code: str, secret: str | None = None) -> bool:
    """Check a 6-digit code. Each code works once (replay protection)."""
    import totp

    secret = secret or get_totp_secret(user_id)
    if not secret:
        return False
    with _db() as con:
        last = con.execute("SELECT totp_last_step FROM users WHERE id = ?", (user_id,)).fetchone()["totp_last_step"]
        step = totp.verify(secret, code, last_step=last)
        if step is None:
            return False
        con.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user_id))
    return True


# ---- AI usage limits (cost control) ------------------------------------------------------ #
def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def check_limit(user_id: int | None) -> None:
    """Raise core.LimitReached when today's AI budget (per user / whole system) is used up."""
    per_user = int(os.getenv("DAILY_AI_CALLS_PER_USER", "150"))
    total_cap = int(os.getenv("DAILY_AI_CALLS_TOTAL", "1500"))
    with _db() as con:
        total = con.execute("SELECT COALESCE(SUM(calls), 0) AS c FROM usage WHERE day = ?", (_today(),)).fetchone()["c"]
        mine = con.execute("SELECT COALESCE(SUM(calls), 0) AS c FROM usage WHERE day = ? AND user_id = ?",
                           (_today(), user_id or 0)).fetchone()["c"]
    if user_id and per_user > 0 and mine >= per_user:
        raise core.LimitReached(f"You've reached today's AI limit ({per_user} requests). Try again tomorrow or ask your admin.")
    if total_cap > 0 and total >= total_cap:
        raise core.LimitReached("The system's daily AI budget is used up. Try again tomorrow or ask your admin.")


def record_usage(user_id: int | None, tokens_in: int, tokens_out: int) -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO usage(day, user_id, calls, tokens_in, tokens_out) VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(day, user_id) DO UPDATE SET calls = calls + 1, "
            "tokens_in = tokens_in + excluded.tokens_in, tokens_out = tokens_out + excluded.tokens_out",
            (_today(), user_id or 0, tokens_in, tokens_out))


def usage_today() -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT COALESCE(u.username, 'system / automatic') AS user, s.calls, s.tokens_in, s.tokens_out "
            "FROM usage s LEFT JOIN users u ON u.id = s.user_id WHERE s.day = ? ORDER BY s.calls DESC",
            (_today(),)).fetchall()
        return [dict(r) for r in rows]


core.LIMIT_CHECK, core.USAGE_RECORD = check_limit, record_usage


# ---- branding (per-company PDF look) ------------------------------------------------------- #
def get_branding(company_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM branding WHERE company_id = ?", (company_id,)).fetchone()
        return dict(row) if row else None


def set_branding(company_id: int, display_name: str, color: str, logo_path: str | None) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO branding(company_id, display_name, color, logo_path) VALUES (?,?,?,?)",
                    (company_id, display_name, color, logo_path))


# ---- scheduled digests ---------------------------------------------------------------------- #
def add_digest(company_id: int, user_id: int, question: str, weekday: int, hour: int, minute: int,
               scope: str = "latest") -> int:
    with _db() as con:
        cur = con.execute(
            "INSERT INTO digests(company_id, user_id, question, weekday, hour, minute, scope, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (company_id, user_id, _enc_text(question), weekday, hour, minute, scope, _now()))
        return cur.lastrowid


def _digest(row) -> dict:
    return {**dict(row), "question": _dec_text(row["question"])}


def list_digests(company_id: int) -> list[dict]:
    with _db() as con:
        return [_digest(r) for r in con.execute("SELECT * FROM digests WHERE company_id = ? ORDER BY id", (company_id,))]


def get_digest(digest_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        return _digest(row) if row else None


def delete_digest(digest_id: int) -> None:
    with _db() as con:
        con.execute("DELETE FROM digests WHERE id = ?", (digest_id,))


def due_digests(now_local: dt.datetime) -> list[dict]:
    """Digests whose time has come today and that haven't run yet today (weekday -1 = every day)."""
    today = now_local.strftime("%Y-%m-%d")
    with _db() as con:
        rows = con.execute("SELECT * FROM digests WHERE last_run IS NULL OR last_run != ?", (today,)).fetchall()
    return [_digest(r) for r in rows
            if r["weekday"] in (-1, now_local.weekday()) and (now_local.hour, now_local.minute) >= (r["hour"], r["minute"])]


def mark_digest_run(digest_id: int, day: str) -> None:
    with _db() as con:
        con.execute("UPDATE digests SET last_run = ? WHERE id = ?", (day, digest_id))


# ---- intake de-duplication, heartbeat, misc ---------------------------------------------------- #
def intake_seen(key: str) -> bool:
    with _db() as con:
        return con.execute("SELECT 1 FROM intake_seen WHERE key = ?", (key,)).fetchone() is not None


def mark_seen(key: str) -> None:
    with _db() as con:
        con.execute("INSERT OR IGNORE INTO intake_seen(key, ts) VALUES (?, ?)", (key, _now()))


def beat(name: str) -> None:
    with _db() as con:
        con.execute("INSERT OR REPLACE INTO heartbeat(name, ts) VALUES (?, ?)", (name, _now()))


def last_beat(name: str) -> dt.datetime | None:
    with _db() as con:
        row = con.execute("SELECT ts FROM heartbeat WHERE name = ?", (name,)).fetchone()
    return dt.datetime.strptime(row["ts"], FMT).replace(tzinfo=dt.timezone.utc) if row else None


def latest_report(company_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT * FROM reports WHERE company_id = ? ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
        return dict(row) if row else None
