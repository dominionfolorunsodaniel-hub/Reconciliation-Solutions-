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
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache

import pandas as pd

import core
import dbcompat
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

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    grant_source TEXT NOT NULL DEFAULT 'admin',
    granted_by INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_company ON subscriptions(company_id, expires_at);
CREATE TABLE IF NOT EXISTS payment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount_ngn INTEGER NOT NULL,
    transfer_reference TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    review_note TEXT
);
CREATE TABLE IF NOT EXISTS monthly_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monthly_usage_company_month ON monthly_usage(company_id, month, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_reference_unique ON payment_requests(transfer_reference);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_payment_per_company ON payment_requests(company_id) WHERE status='pending';

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


IntegrityError = dbcompat.IntegrityError  # works for both SQLite and PostgreSQL
USE_PG = dbcompat.USE_PG


@contextmanager
def _db():
    if USE_PG:  # Neon / PostgreSQL (DATABASE_URL is set)
        with dbcompat.connect_pg() as con:
            yield con
        return
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


# ---- company roles --------------------------------------------------------------- #
# users.role stays the SITE-WIDE role (only the website owner is "admin").
# users.company_role is what a person can do inside their own company:
#   owner   - the person who created the company account: everything, plus team roles and billing
#   admin   - controls the company: delete reports, API keys, integrations, schedules, branding
#   analyst - does the work: upload, ask, reconcile, calculate
#   viewer  - read-only: dashboard, reports, results
COMPANY_RANK = {"viewer": 1, "analyst": 2, "admin": 3, "owner": 4}
TEAM_ASSIGNABLE = ("admin", "analyst", "viewer")      # the owner can never be assigned: there is one per company


def effective_company_role(user: dict) -> str:
    if user.get("role") == "admin":
        return "platform"                              # the website owner
    cr = user.get("company_role")
    if cr in COMPANY_RANK:
        return cr
    return "viewer" if user.get("role") == "viewer" else "analyst"


def company_rank(user: dict) -> int:
    r = effective_company_role(user)
    return 99 if r == "platform" else COMPANY_RANK[r]


def _backfill_company_roles(con) -> None:
    """One time: the first customer login of each company becomes its owner, later ones analysts."""
    rows = con.execute(
        "SELECT u.id AS uid, u.role AS urole, uc.company_id AS cid FROM users u "
        "LEFT JOIN user_companies uc ON uc.user_id = u.id "
        "WHERE u.company_role IS NULL AND u.role <> 'admin' ORDER BY u.created_at, u.id").fetchall()
    if not rows:
        return
    owners = {r["cid"] for r in con.execute(
        "SELECT uc.company_id AS cid FROM users u JOIN user_companies uc ON uc.user_id = u.id "
        "WHERE u.company_role = 'owner'").fetchall()}
    done = set()
    for r in rows:
        if r["uid"] in done:
            continue
        done.add(r["uid"])
        if r["urole"] == "viewer":
            cr = "viewer"
        elif r["cid"] is not None and r["cid"] not in owners:
            cr = "owner"
            owners.add(r["cid"])
        else:
            cr = "analyst"
        con.execute("UPDATE users SET company_role = ? WHERE id = ?", (cr, r["uid"]))


def _ensure_column(con, table: str, column: str, decl: str) -> None:
    if USE_PG:
        dbcompat.ensure_column(con, table, column, decl)
        return
    if column not in [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


PG_INIT_LOCK = 918273645  # any fixed number; every startup migration uses the same one


def run_startup_migration(fn) -> None:
    """Run fn(connection) as a startup migration. The website and the Telegram bot start at the same
    moment, and on PostgreSQL two processes altering the same tables can deadlock (that crashed a deploy).
    So on PostgreSQL they take turns using an advisory lock, and a transient deadlock is simply retried."""
    for attempt in range(6):
        try:
            with _db() as con:
                if USE_PG:
                    con.execute(f"SELECT pg_advisory_xact_lock({PG_INIT_LOCK})")
                fn(con)
            return
        except Exception as e:  # noqa: BLE001
            transient = USE_PG and any(w in str(e).lower() for w in ("deadlock", "concurrently"))
            if not transient or attempt == 5:
                raise
            log.warning("Startup migration hit a database lock conflict (%s); retrying", type(e).__name__)
            time.sleep(1.5 * (attempt + 1))


def _init_schema(_con) -> None:
    if USE_PG:
        dbcompat.ensure_citext()  # case-insensitive usernames / company names, like SQLite's NOCASE
    _con.executescript(SCHEMA)
    if USE_PG:  # Render's disk is wiped on redeploy, so keep encrypted files in the database
        _con.executescript("CREATE TABLE IF NOT EXISTS file_blobs (path TEXT PRIMARY KEY, data BLOB NOT NULL);")
    _ensure_column(_con, "reports", "stored_ext", "TEXT")
    _ensure_column(_con, "reports", "uploaded_by", "INTEGER")
    _ensure_column(_con, "queries", "user_id", "INTEGER")
    _ensure_column(_con, "users", "totp_secret", "TEXT")
    _ensure_column(_con, "audit", "entry_hash", "TEXT")
    _ensure_column(_con, "users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(_con, "users", "totp_last_step", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(_con, "payment_requests", "proof_data", "TEXT")   # encrypted base64 payment screenshot
    _ensure_column(_con, "payment_requests", "proof_mime", "TEXT")
    _ensure_column(_con, "payment_requests", "proof_at", "TEXT")
    _ensure_column(_con, "payment_requests", "coupon_code", "TEXT")
    _ensure_column(_con, "payment_requests", "discount_ngn", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(_con, "users", "email", "TEXT")
    _ensure_column(_con, "users", "referral_code", "TEXT")
    _ensure_column(_con, "users", "referred_by", "INTEGER")
    _ensure_column(_con, "users", "company_role", "TEXT")        # owner / admin / analyst / viewer inside the customer's company
    _backfill_company_roles(_con)
    _con.executescript("CREATE TABLE IF NOT EXISTS reset_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT NOT NULL, "
                       "note TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT);")
    _con.executescript("CREATE TABLE IF NOT EXISTS password_resets (user_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL, "
                       "expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);")
    _con.executescript(
        "CREATE TABLE IF NOT EXISTS email_codes (key TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, PRIMARY KEY (key, purpose));"
        "CREATE TABLE IF NOT EXISTS verified_emails (email TEXT PRIMARY KEY, verified_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS trusted_devices (user_id INTEGER NOT NULL, token_hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (user_id, token_hash));")
    _con.executescript(
        "CREATE TABLE IF NOT EXISTS manual_matches (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, report_a INTEGER NOT NULL, "
        "report_b INTEGER NOT NULL, bank_ref TEXT NOT NULL, ledger_ref TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_by INTEGER, "
        "created_by_name TEXT, created_at TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS close_checklist (company_id INTEGER NOT NULL, month TEXT NOT NULL, item TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, "
        "done_by TEXT, done_at TEXT, PRIMARY KEY (company_id, month, item));")
    _con.executescript(
        "CREATE TABLE IF NOT EXISTS broadcasts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL, "
        "level TEXT NOT NULL DEFAULT 'info', created_by INTEGER, created_at TEXT NOT NULL, expires_at TEXT, "
        "active INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE IF NOT EXISTS broadcast_reads (broadcast_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "read_at TEXT NOT NULL, PRIMARY KEY (broadcast_id, user_id));")
    _ensure_column(_con, "broadcasts", "target_user_id", "INTEGER")   # NULL = announcement for everyone
    _ensure_column(_con, "sessions", "user_agent", "TEXT")            # shown in "devices signed in"
    _ensure_column(_con, "sessions", "ip", "TEXT")
    import monetize                      # membership store: products, orders, feature credits, enquiries (additive tables only)
    monetize.schema(_con)


run_startup_migration(_init_schema)


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
            company_id = cur.lastrowid
            _insert_trial_subscription(con, company_id)
            row = con.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        return dict(row)


def create_customer_account(company_name: str, username: str, password: str,
                            telegram_id: int | None = None) -> dict:
    """Atomically create a separate customer company and analyst account."""
    company_name = company_name.strip()
    username = username.strip()
    if not company_name or len(company_name) > 120:
        raise ValueError("Company name must be between 1 and 120 characters.")
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-40 characters - letters, digits or _ . @ -")
    _check_password(password)
    if telegram_id is not None and int(telegram_id) <= 0:
        raise ValueError("Telegram ID must be a positive number.")
    # Hash before opening the transaction to keep the database lock short.
    password_hash = security.hash_password(password)
    try:
        with _db() as con:
            cur = con.execute("INSERT INTO companies(name, created_at) VALUES (?, ?)",
                              (company_name, _now()))
            company_id = cur.lastrowid
            _insert_trial_subscription(con, company_id)
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, company_role, telegram_id, created_at) "
                "VALUES (?, ?, 'analyst', 'owner', ?, ?)",
                (username, password_hash, int(telegram_id) if telegram_id else None, _now()))
            user_id = cur.lastrowid
            con.execute("INSERT INTO user_companies(user_id, company_id) VALUES (?, ?)",
                        (user_id, company_id))
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            user = _user(row)
            user["company_ids"] = [company_id]
            user["company_name"] = company_name
            return user
    except IntegrityError as e:
        message = str(e).lower()
        if "companies.name" in message:
            raise ValueError("A company with that name already exists. Use a distinct customer name to keep its data separate.") from e
        if "users.username" in message:
            raise ValueError("That username is already in use.") from e
        if "users.telegram_id" in message:
            raise ValueError("That Telegram ID is already linked to another user.") from e
        raise ValueError("Could not create the customer account. Check the details and try again.") from e


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
def _col(row, name):
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _user(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "telegram_id": row["telegram_id"], "active": bool(row["active"]),
            "alerts": bool(row["alerts"]), "totp_enabled": bool(row["totp_enabled"]),
            "email": _col(row, "email"), "referral_code": _col(row, "referral_code"),
            "company_role": _col(row, "company_role"),
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


def validate_company_membership(company_ids, exclude_user_id: int | None = None) -> None:
    for cid in set(int(x) for x in (company_ids or [])):
        status = get_plan_status(cid)
        with _db() as con:
            if exclude_user_id is None:
                count = con.execute("SELECT COUNT(*) AS n FROM user_companies WHERE company_id=?", (cid,)).fetchone()["n"]
            else:
                count = con.execute("SELECT COUNT(*) AS n FROM user_companies WHERE company_id=? AND user_id<>?", (cid, exclude_user_id)).fetchone()["n"]
        if count >= status["team_limit"]:
            raise ValueError(f"{status['plan'].title()} allows up to {status['team_limit']} team member(s) per company. Upgrade your plan to add more.")


def set_user_companies(user_id: int, company_ids) -> None:
    company_ids = set(int(x) for x in (company_ids or []))
    validate_company_membership(company_ids, exclude_user_id=user_id)
    with _db() as con:
        con.execute("DELETE FROM user_companies WHERE user_id = ?", (user_id,))
        for cid in company_ids:
            con.execute("INSERT OR IGNORE INTO user_companies(user_id, company_id) VALUES (?, ?)", (user_id, cid))


# ---- company team (managed by the company owner) -------------------------------- #
def _team_row(con, company_id: int, user_id: int):
    return con.execute(
        "SELECT u.* FROM users u JOIN user_companies uc ON uc.user_id = u.id "
        "WHERE uc.company_id = ? AND u.id = ? AND u.role <> 'admin'", (company_id, user_id)).fetchone()


def list_team(company_id: int) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT u.* FROM users u JOIN user_companies uc ON uc.user_id = u.id "
            "WHERE uc.company_id = ? AND u.role <> 'admin' ORDER BY u.created_at, u.id", (company_id,)).fetchall()
    out = []
    for r in rows:
        u = _user(r)
        out.append({"id": u["id"], "username": u["username"], "email": u["email"],
                    "company_role": effective_company_role(u), "active": u["active"],
                    "two_factor": u["totp_enabled"], "created_at": u["created_at"]})
    return sorted(out, key=lambda x: (-COMPANY_RANK.get(x["company_role"], 0), x["username"].lower()))


def add_team_member(company_id: int, username: str, password: str, role: str) -> dict:
    if role not in TEAM_ASSIGNABLE:
        raise ValueError("Choose Admin, Analyst or Viewer.")
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-40 characters - letters, digits or _ . @ -")
    _check_password(password)
    validate_company_membership([company_id])             # the plan's team limit
    site_role = "viewer" if role == "viewer" else "analyst"
    try:
        with _db() as con:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, company_role, created_at) VALUES (?,?,?,?,?)",
                (username, security.hash_password(password), site_role, role, _now()))
            uid = cur.lastrowid
            con.execute("INSERT INTO user_companies(user_id, company_id) VALUES (?, ?)", (uid, company_id))
    except IntegrityError as e:
        raise ValueError("That username is already in use. Choose another.") from e
    return get_user(uid)


def set_team_role(company_id: int, user_id: int, role: str) -> dict:
    if role not in TEAM_ASSIGNABLE:
        raise ValueError("Choose Admin, Analyst or Viewer.")
    with _db() as con:
        row = _team_row(con, company_id, user_id)
        if row is None:
            raise LookupError("That person is not on this team.")
        if effective_company_role(_user(row)) == "owner":
            raise ValueError("The owner's role can't be changed.")
        con.execute("UPDATE users SET company_role = ?, role = ? WHERE id = ?",
                    (role, "viewer" if role == "viewer" else "analyst", user_id))
    return get_user(user_id)


def remove_team_member(company_id: int, user_id: int) -> None:
    with _db() as con:
        row = _team_row(con, company_id, user_id)
        if row is None:
            raise LookupError("That person is not on this team.")
        if effective_company_role(_user(row)) == "owner":
            raise ValueError("The owner can't be removed.")
        con.execute("DELETE FROM user_companies WHERE user_id = ? AND company_id = ?", (user_id, company_id))
        left = con.execute("SELECT COUNT(*) AS n FROM user_companies WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if not left:
            con.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        con.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def reset_team_password(company_id: int, user_id: int, password: str) -> None:
    with _db() as con:
        row = _team_row(con, company_id, user_id)
        if row is None:
            raise LookupError("That person is not on this team.")
        if effective_company_role(_user(row)) == "owner":
            raise ValueError("Use Settings to change your own password.")
    update_user(user_id, password=password)      # also signs them out everywhere
    forget_trusted_devices(user_id)


def company_activity(company_id: int, limit: int = 40) -> list[dict]:
    with _db() as con:
        rows = con.execute("SELECT ts, username, action, detail FROM audit WHERE company_id = ? ORDER BY id DESC LIMIT ?",
                           (company_id, int(limit))).fetchall()
    return [{"time": r["ts"], "user": r["username"], "action": r["action"], "detail": r["detail"]} for r in rows]


def _check_password(pw: str) -> None:
    if len(pw) < 10:
        raise ValueError("Password must be at least 10 characters.")


def create_user(username: str, password: str, role: str = "analyst",
                telegram_id: int | None = None, company_ids=(), company_role: str | None = None) -> dict:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-40 characters - letters, digits or _ . @ -")
    if role not in ROLES:
        raise ValueError("Unknown role.")
    _check_password(password)
    validate_company_membership(company_ids)
    try:
        with _db() as con:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, company_role, telegram_id, created_at) VALUES (?,?,?,?,?,?)",
                (username, security.hash_password(password), role,
                 (company_role if company_role in COMPANY_RANK else (role if role in ("viewer", "analyst") else None)),
                 telegram_id or None, _now()))
            uid = cur.lastrowid
    except IntegrityError as e:
        raise ValueError("That username or Telegram ID is already in use.") from e
    set_user_companies(uid, company_ids)
    return get_user(uid)


def update_user(user_id: int, **f) -> dict | None:
    if f.get("company_ids") is not None:
        validate_company_membership(f["company_ids"], exclude_user_id=user_id)
    sets, vals = [], []
    if "role" in f:
        if f["role"] not in ROLES:
            raise ValueError("Unknown role.")
        sets.append("role = ?"); vals.append(f["role"])
        cur_u = get_user(user_id)
        if f["role"] == "viewer":
            sets.append("company_role = ?"); vals.append("viewer")
        elif f["role"] == "analyst":
            keep = (cur_u or {}).get("company_role")
            sets.append("company_role = ?"); vals.append(keep if keep in ("owner", "admin", "analyst") else "analyst")
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
    except IntegrityError as e:
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


def sync_env_admin() -> dict | None:
    """Ensure the Render-configured credentials map to one persistent admin record.

    Existing customer data is preserved. The environment variables are authoritative
    for web admin authentication; this function only synchronizes the admin identity.
    """
    username = os.getenv("DASHBOARD_USER", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if not username or not password or len(password) < 10:
        return None
    if not USERNAME_RE.match(username):
        log.error("DASHBOARD_USER is invalid; admin login is disabled")
        return None
    # Hash outside the transaction to keep database locks short.
    password_hash = security.hash_password(password)
    with _db() as con:
        target = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if target is not None and target["role"] != "admin":
            log.error("DASHBOARD_USER conflicts with a non-admin account; admin login is disabled")
            return None
        admins = con.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY id").fetchall()
        credentials_changed = False
        if target is None:
            if admins:
                admin_id = admins[0]["id"]
                # Keep the existing admin row and all associated data, changing only its login identity.
                con.execute("UPDATE users SET username = ?, password_hash = ?, active = 1 WHERE id = ?",
                            (username, password_hash, admin_id))
                target_id = admin_id
                credentials_changed = True
            else:
                cur = con.execute(
                    "INSERT INTO users(username, password_hash, role, created_at, active) VALUES (?, ?, 'admin', ?, 1)",
                    (username, password_hash, _now()))
                target_id = cur.lastrowid
                credentials_changed = True
        else:
            target_id = target["id"]
            stored_hash = target["password_hash"]
            credentials_changed = (not security.verify_password(password, stored_hash) or not target["active"])
            if credentials_changed:
                con.execute("UPDATE users SET password_hash = ?, active = 1 WHERE id = ?",
                            (password_hash, target_id))
        # Revoke sessions only when the configured admin credentials actually change.
        if credentials_changed:
            con.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE role = 'admin')")
        row = con.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
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


def create_session(user_id: int, user_agent: str = "", ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=SESSION_HOURS)).strftime(FMT)
    with _db() as con:
        con.execute("INSERT INTO sessions(token_hash, user_id, expires_at, created_at, user_agent, ip) VALUES (?,?,?,?,?,?)",
                    (_th(token), user_id, exp, _now(), (user_agent or "")[:300], (ip or "")[:64]))
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
def _audit_hash(prev: str, ts, user_id, username, source, action, company_id, detail) -> str:
    import hashlib
    raw = "|".join(str(x) for x in (prev, ts, user_id, username, source, action, company_id, detail))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audit(user: dict | None, action: str, company_id: int | None = None, detail: str = "",
          source: str = "web") -> None:
    ts, uid, uname, det = _now(), (user["id"] if user else None), (user["username"] if user else None), (detail or "")[:500]
    with _db() as con:
        cur_hash = None
        try:  # tamper-evident chain: each row's hash covers the previous row's hash
            last = con.execute("SELECT entry_hash FROM audit WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            cur_hash = _audit_hash(last["entry_hash"] if last else "", ts, uid, uname, source, action, company_id, det)
        except Exception:  # noqa: BLE001  e.g. column not there yet: still write the log
            cur_hash = None
        if cur_hash:
            con.execute(
                "INSERT INTO audit(ts, user_id, username, source, action, company_id, detail, entry_hash) VALUES (?,?,?,?,?,?,?,?)",
                (ts, uid, uname, source, action, company_id, det, cur_hash))
        else:
            con.execute(
                "INSERT INTO audit(ts, user_id, username, source, action, company_id, detail) VALUES (?,?,?,?,?,?,?)",
                (ts, uid, uname, source, action, company_id, det))


def verify_audit() -> dict:
    """Re-computes the chain. Any edited or deleted row after chaining began shows up as a break."""
    with _db() as con:
        rows = con.execute("SELECT id, ts, user_id, username, source, action, company_id, detail, entry_hash FROM audit WHERE entry_hash IS NOT NULL ORDER BY id").fetchall()
    prev, checked = "", 0
    for r in rows:
        if _audit_hash(prev, r["ts"], r["user_id"], r["username"], r["source"], r["action"], r["company_id"], r["detail"]) != r["entry_hash"]:
            return {"ok": False, "checked": checked, "broken_at_id": r["id"]}
        prev, checked = r["entry_hash"], checked + 1
    return {"ok": True, "checked": checked, "broken_at_id": None}


def list_audit(limit: int = 100) -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT a.id, a.ts, a.username, a.source, a.action, a.detail, c.name AS company "
            "FROM audit a LEFT JOIN companies c ON c.id = a.company_id ORDER BY a.id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---- file storage: encrypted files live on disk (SQLite mode) or in the database (PostgreSQL mode) ---- #
def write_file(path: str, data: bytes) -> None:
    """Store bytes under `path`. Paths starting with 'db:' live in the database."""
    if path.startswith("db:"):
        with _db() as con:
            con.execute("INSERT OR REPLACE INTO file_blobs(path, data) VALUES (?, ?)", (path, bytes(data)))
    else:
        with open(path, "wb") as f:
            f.write(data)


def read_file(path: str) -> bytes | None:
    if path and path.startswith("db:"):
        with _db() as con:
            row = con.execute("SELECT data FROM file_blobs WHERE path = ?", (path,)).fetchone()
            return bytes(row["data"]) if row else None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def delete_file(path: str, con=None) -> None:
    if path and path.startswith("db:"):
        if con is not None:
            con.execute("DELETE FROM file_blobs WHERE path = ?", (path,))
        else:
            with _db() as c:
                c.execute("DELETE FROM file_blobs WHERE path = ?", (path,))
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ---- reports (files are encrypted at rest) -------------------------------------- #
def add_report(company_id: int, filename: str, data: bytes, rows: int, stored_ext: str,
               sheet: str | None = None, uploaded_by: int | None = None) -> int:
    name = f"{uuid.uuid4().hex}.bin"
    path = f"db:{name}" if USE_PG else os.path.join(FILES_DIR, name)
    write_file(path, security.encrypt(data))
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
    delete_file(path, con)


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
    raw = read_file(r["stored_path"])
    if raw is None:
        raise ValueError("The stored file for this report is missing.")
    data = security.decrypt(raw)
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
    per_user = int(os.getenv("DAILY_AI_CALLS_PER_USER", "300"))
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
            "ON CONFLICT(day, user_id) DO UPDATE SET calls = usage.calls + 1, "
            "tokens_in = usage.tokens_in + excluded.tokens_in, tokens_out = usage.tokens_out + excluded.tokens_out",
            (_today(), user_id or 0, tokens_in, tokens_out))


def usage_today() -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT COALESCE(u.username, 'system / automatic') AS user, s.calls, s.tokens_in, s.tokens_out "
            "FROM usage s LEFT JOIN users u ON u.id = s.user_id WHERE s.day = ? ORDER BY s.calls DESC",
            (_today(),)).fetchall()
        return [dict(r) for r in rows]


# ---- subscriptions, manual payments, and shared monthly quotas ---------------------------
# Plan duration controls access expiry; analysis allowances remain company-wide per calendar month.
# Bank transfers are manually verified by an administrator before paid access is activated.
# Starter, Growth and Professional are self-serve monthly plans (30 days). Business, Corporate and Enterprise are
# "custom": arranged with the customer first (quote), then granted by an administrator; they are not shown for self-service purchase.
PLANS = {
    "free": {"name": "Free Trial", "price_ngn": 0, "billing_period": "trial", "duration_days": 2, "monthly_limit": 5, "team_limit": 1},
    "starter": {"name": "Starter", "price_ngn": 25000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 50, "team_limit": 3},
    "growth": {"name": "Growth", "price_ngn": 60000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 200, "team_limit": 5},
    "professional": {"name": "Professional", "price_ngn": 150000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 750, "team_limit": 10},
    "business": {"name": "Business", "price_ngn": 1000000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 1500, "team_limit": 15, "custom": True},
    "corporate": {"name": "Corporate", "price_ngn": 2500000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 4000, "team_limit": 30, "custom": True},
    "enterprise": {"name": "Enterprise", "price_ngn": 5000000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 10000, "team_limit": 75, "custom": True},
    "enterprise_plus": {"name": "Enterprise Plus", "price_ngn": 50000000, "billing_period": "fixed_term", "duration_days": 30, "monthly_limit": 20000, "team_limit": 100, "available": False},  # Temporarily hidden from new purchases; retained for existing records.
    # Legacy-only: retained to read existing subscriptions, but not offered for new purchases.
    "lifetime": {"name": "Company Lifetime (legacy)", "price_ngn": 150000, "billing_period": "lifetime", "duration_days": None, "monthly_limit": 4000, "team_limit": 50, "available": False},
    "expired": {"name": "Expired", "price_ngn": 0, "billing_period": "expired", "duration_days": 0, "monthly_limit": 0, "team_limit": 1, "available": False},
}

PAID_PLANS = tuple(k for k, p in PLANS.items() if k not in ("free", "expired") and p.get("available", True))
ADMIN_GRANT_PLANS = PAID_PLANS
SELF_SERVE_PLANS = tuple(k for k in PAID_PLANS if not PLANS[k].get("custom"))


def duration_label(plan: str) -> str:
    p = PLANS[plan]
    days = p.get("duration_days")
    if plan == "lifetime":
        return "legacy lifetime"
    if days == 0:
        return "expired"
    if days == 0.5:
        return "12 hours"
    if days == 1:
        return "1 day"
    if days == 1.5:
        return "1 day 12 hours"
    if days == 10.5:
        return "1½ weeks (10 days 12 hours)"
    if days == 7:
        return "1 week (7 days)"
    if days == 14:
        return "2 weeks (14 days)"
    if days == 21:
        return "3 weeks (21 days)"
    if days == 30:
        return "1 month (30 days)"
    if days == 2:
        return "2 days"
    if days == 3:
        return "3 days"
    if days == 5:
        return "5 days"
    return f"{days:g} days"


def _insert_trial_subscription(con, company_id: int) -> None:
    """Start a single two-day trial when a company is first created."""
    now = dt.datetime.now(dt.timezone.utc)
    expiry = now + dt.timedelta(days=PLANS["free"]["duration_days"])
    con.execute(
        "INSERT INTO subscriptions(company_id,plan,status,starts_at,expires_at,grant_source,note,created_at) "
        "VALUES (?, 'free', 'active', ?, ?, 'trial', 'Automatic 2-day free trial', ?)",
        (company_id, now.strftime(FMT), expiry.strftime(FMT), _now()))


def _ensure_trial(con, company_id: int) -> None:
    """Give pre-existing companies one trial if they have never had a subscription."""
    row = con.execute("SELECT 1 FROM subscriptions WHERE company_id=? LIMIT 1", (company_id,)).fetchone()
    if not row:
        _insert_trial_subscription(con, company_id)


def _subscription_expiry(plan: str, now: dt.datetime) -> dt.datetime:
    period = PLANS[plan]["billing_period"]
    if period == "lifetime":
        return dt.datetime(9999, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc)
    return now + dt.timedelta(days=float(PLANS[plan]["duration_days"] or 0))

def _month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _active_subscription(con, company_id: int):
    now = _now()
    return con.execute(
        "SELECT * FROM subscriptions WHERE company_id=? AND status='active' AND starts_at<=? AND expires_at>? "
        "ORDER BY id DESC LIMIT 1", (company_id, now, now)).fetchone()


GRACE_HOURS = float(os.getenv("GRACE_HOURS", "24"))


def _grace_subscription(con, company_id: int):
    """A PAID plan that ended less than GRACE_HOURS ago: customers keep working while they renew.
    (The free trial gets no grace.)"""
    if GRACE_HOURS <= 0:
        return None
    now = _now()
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=GRACE_HOURS)).strftime(FMT)
    return con.execute(
        "SELECT * FROM subscriptions WHERE company_id=? AND plan NOT IN ('free','expired') AND status!='revoked' AND expires_at<=? AND expires_at>? "
        "ORDER BY id DESC LIMIT 1", (company_id, now, since)).fetchone()


def get_plan_status(company_id: int) -> dict:
    with _db() as con:
        now = _now()
        _ensure_trial(con, company_id)
        con.execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND expires_at<=?", (now,))
        sub = _active_subscription(con, company_id)
        grace = None
        if sub is None:
            grace = _grace_subscription(con, company_id)
            sub = grace
        plan_key = sub["plan"] if sub and sub["plan"] in PLANS else "expired"
        plan = dict(PLANS[plan_key])
        count = con.execute(
            "SELECT COUNT(*) AS n FROM monthly_usage WHERE company_id=? AND month=? AND status='used'",
            (company_id, _month())).fetchone()["n"]
        pending = con.execute(
            "SELECT COUNT(*) AS n FROM payment_requests WHERE company_id=? AND status='pending'",
            (company_id,)).fetchone()["n"]
        last_sub = con.execute("SELECT expires_at FROM subscriptions WHERE company_id=? ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
        return {"company_id": company_id, "plan": plan_key, **plan, "used": count,
                "remaining": max(0, plan["monthly_limit"]-count),
                "expires_at": sub["expires_at"] if sub else (last_sub["expires_at"] if last_sub else None),
                "term_label": duration_label(plan_key),
                "subscription_source": sub["grant_source"] if sub else None,
                "in_grace": grace is not None,
                "grace_ends_at": (dt.datetime.strptime(grace["expires_at"], FMT) + dt.timedelta(hours=GRACE_HOURS)).strftime(FMT) if grace else None,
                "pending_payment_requests": pending, "month": _month()}


def reserve_monthly_usage(company_id: int, user_id: int, operation: str) -> int:
    """Atomically reserve one shared company quota slot, including concurrent requests."""
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        _ensure_trial(con, company_id)
        sub = _active_subscription(con, company_id) or _grace_subscription(con, company_id)
        plan_key = sub["plan"] if sub and sub["plan"] in PLANS else "expired"
        limit = PLANS[plan_key]["monthly_limit"]
        month = _month()
        stale_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime(FMT)
        con.execute("DELETE FROM monthly_usage WHERE status='reserved' AND created_at<?", (stale_before,))
        used = con.execute(
            "SELECT COUNT(*) AS n FROM monthly_usage WHERE company_id=? AND month=? AND status IN ('reserved','used')",
            (company_id, month)).fetchone()["n"]
        if used >= limit:
            if plan_key == "expired":
                raise core.LimitReached("Your free trial or subscription has expired. Choose a paid plan or contact your company administrator.")
            raise core.LimitReached(
                f"You've used this company's {limit} {plan_key.title()} analyses for {month}. "
                "Upgrade your plan or ask the administrator about an eligible temporary plan grant."
            )
        cur = con.execute(
            "INSERT INTO monthly_usage(month, company_id, user_id, operation, status, created_at) "
            "VALUES (?, ?, ?, ?, 'reserved', ?)", (month, company_id, user_id, operation[:40], _now()))
        return int(cur.lastrowid)


def finish_monthly_usage(reservation_id: int, success: bool) -> None:
    with _db() as con:
        if success:
            con.execute("UPDATE monthly_usage SET status='used' WHERE id=? AND status='reserved'", (reservation_id,))
        else:
            con.execute("DELETE FROM monthly_usage WHERE id=? AND status='reserved'", (reservation_id,))


def grant_subscription(company_id: int, plan: str, days: float | None, admin_id: int, note: str = "") -> dict:
    if plan not in ADMIN_GRANT_PLANS:
        raise ValueError("Choose one of the currently offered paid plans.")
    days = float(PLANS[plan]["duration_days"] if days is None else days)
    if days < 0.5 or days > 366:
        raise ValueError("Grant duration must be between 0.5 and 366 days.")
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(days=days)
    with _db() as con:
        con.execute("UPDATE subscriptions SET status='superseded' WHERE company_id=? AND status='active'", (company_id,))
        cur = con.execute(
            "INSERT INTO subscriptions(company_id,plan,status,starts_at,expires_at,grant_source,granted_by,note,created_at) "
            "VALUES (?,?,'active',?,?, 'admin',?,?,?)",
            (company_id, plan, now.strftime(FMT), expires.strftime(FMT), admin_id, (note or "")[:300], _now()))
        sid = cur.lastrowid
    return {"id": sid, **get_plan_status(company_id)}


def list_subscriptions() -> list[dict]:
    with _db() as con:
        rows = con.execute(
            "SELECT s.*, c.name AS company_name, u.username AS granted_by_username "
            "FROM subscriptions s JOIN companies c ON c.id=s.company_id "
            "LEFT JOIN users u ON u.id=s.granted_by ORDER BY s.id DESC LIMIT 300").fetchall()
        return [dict(r) for r in rows]


def create_payment_request(company_id: int, user_id: int, plan: str, reference: str,
                           coupon_code: str | None = None, discount_ngn: int = 0) -> dict:
    if plan not in PAID_PLANS:
        raise ValueError("Choose one of the currently offered paid company plans.")
    reference = (reference or "").strip().upper()
    if not reference or len(reference) > 120:
        raise ValueError("Enter the bank transfer reference (1-120 characters).")
    discount_ngn = max(0, min(int(discount_ngn or 0), PLANS[plan]["price_ngn"]))
    amount = PLANS[plan]["price_ngn"] - discount_ngn
    try:
        with _db() as con:
            con.execute("BEGIN IMMEDIATE")
            pending = con.execute("SELECT id FROM payment_requests WHERE company_id=? AND status='pending'", (company_id,)).fetchone()
            if pending:
                raise ValueError("You already have a payment request awaiting review.")
            cur = con.execute(
                "INSERT INTO payment_requests(company_id,user_id,plan,amount_ngn,transfer_reference,status,created_at,coupon_code,discount_ngn) "
                "VALUES (?,?,?,?,?,'pending',?,?,?)", (company_id,user_id,plan,amount,reference,_now(),coupon_code,discount_ngn))
            row = con.execute("SELECT * FROM payment_requests WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)
    except IntegrityError as e:
        raise ValueError("That transfer reference has already been submitted.") from e


def attach_payment_proof(company_id: int, data: bytes, mime: str) -> dict | None:
    """Store an (encrypted) payment screenshot on the company's pending payment request."""
    import base64
    blob = _enc_text(base64.b64encode(data).decode())
    with _db() as con:
        row = con.execute("SELECT id FROM payment_requests WHERE company_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                          (company_id,)).fetchone()
        if not row:
            return None
        con.execute("UPDATE payment_requests SET proof_data=?, proof_mime=?, proof_at=? WHERE id=?",
                    (blob, mime, _now(), row["id"]))
        out = con.execute("SELECT p.id, p.plan, p.amount_ngn, p.transfer_reference, c.name AS company_name "
                          "FROM payment_requests p JOIN companies c ON c.id=p.company_id WHERE p.id=?", (row["id"],)).fetchone()
        return dict(out)


def get_payment_proof(request_id: int) -> tuple[bytes, str] | None:
    import base64
    with _db() as con:
        row = con.execute("SELECT proof_data, proof_mime FROM payment_requests WHERE id=?", (request_id,)).fetchone()
    if not row or not row["proof_data"]:
        return None
    return base64.b64decode(_dec_text(row["proof_data"])), row["proof_mime"] or "image/jpeg"


# ---- broadcasts: admin announcements that every user sees under Notifications ----------- #
BROADCAST_LEVELS = ("info", "success", "warning")


def create_broadcast(title: str, body: str, level: str, days: int | None, admin_id: int) -> dict:
    title, body = (title or "").strip(), (body or "").strip()
    if not title or len(title) > 120:
        raise ValueError("Write a title (up to 120 characters).")
    if not body or len(body) > 2000:
        raise ValueError("Write a message (up to 2000 characters).")
    level = level if level in BROADCAST_LEVELS else "info"
    expires = None
    if days:
        days = max(1, min(int(days), 365))
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime(FMT)
    with _db() as con:
        cur = con.execute("INSERT INTO broadcasts(title, body, level, created_by, created_at, expires_at, active) "
                          "VALUES (?,?,?,?,?,?,1)", (title, body, level, admin_id, _now(), expires))
        bid = cur.lastrowid
        # the sender has obviously read it
        con.execute("INSERT OR IGNORE INTO broadcast_reads(broadcast_id, user_id, read_at) VALUES (?,?,?)", (bid, admin_id, _now()))
        return dict(con.execute("SELECT * FROM broadcasts WHERE id = ?", (bid,)).fetchone())


def list_notifications(user_id: int, limit: int = 30) -> list[dict]:
    """Live announcements for one user, newest first, with an is_read flag."""
    with _db() as con:
        rows = con.execute(
            "SELECT b.id, b.title, b.body, b.level, b.created_at, "
            "(CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END) AS is_read "
            "FROM broadcasts b LEFT JOIN broadcast_reads r ON r.broadcast_id = b.id AND r.user_id = ? "
            "WHERE b.active = 1 AND (b.expires_at IS NULL OR b.expires_at > ?) "
            "AND (b.target_user_id IS NULL OR b.target_user_id = ?) ORDER BY b.id DESC LIMIT ?",
            (user_id, _now(), user_id, int(limit))).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["is_read"] = bool(r["is_read"])
    return out


def mark_notifications_read(user_id: int, ids: list[int] | None = None) -> None:
    """Mark the given announcements (or every live one) as read for this user."""
    wanted = None if ids is None else {int(i) for i in ids[:200]}
    with _db() as con:
        rows = con.execute("SELECT id FROM broadcasts WHERE active = 1 AND (target_user_id IS NULL OR target_user_id = ?) "
                           "ORDER BY id DESC LIMIT 200", (user_id,)).fetchall()
        now = _now()
        for r in rows:
            if wanted is None or r["id"] in wanted:
                con.execute("INSERT OR IGNORE INTO broadcast_reads(broadcast_id, user_id, read_at) VALUES (?,?,?)",
                            (r["id"], user_id, now))


def list_broadcasts_admin(limit: int = 50) -> dict:
    with _db() as con:
        rows = con.execute(
            "SELECT b.id, b.title, b.body, b.level, b.created_at, b.expires_at, b.active, u.username AS created_by_name, "
            "(SELECT COUNT(*) FROM broadcast_reads r JOIN users ru ON ru.id = r.user_id "
            " WHERE r.broadcast_id = b.id AND ru.role <> 'admin') AS read_count "
            "FROM broadcasts b LEFT JOIN users u ON u.id = b.created_by WHERE b.target_user_id IS NULL "
            "ORDER BY b.id DESC LIMIT ?", (int(limit),)).fetchall()
        customers = con.execute("SELECT COUNT(*) AS n FROM users WHERE role <> 'admin' AND active = 1").fetchone()["n"]
    return {"customers": int(customers), "items": [dict(r) for r in rows]}


def withdraw_broadcast(broadcast_id: int) -> bool:
    with _db() as con:
        row = con.execute("SELECT id FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
        if not row:
            return False
        con.execute("UPDATE broadcasts SET active = 0 WHERE id = ?", (broadcast_id,))
    return True


# ---- password reset (one-time code sent to the user's linked Telegram) -------- #
RESET_MINUTES = 15
RESET_MAX_TRIES = 5


def _reset_hash(user_id: int, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


def create_password_reset(username: str) -> dict | None:
    """Returns {user, code} when the account can be reset by Telegram, else None (callers must not reveal which)."""
    with _db() as con:
        row = con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None or not row["active"] or row["role"] == "admin" or not (row["telegram_id"] or _col(row, "email")):
        return None  # admins use the configured credentials; no email/Telegram on file = ask the admin to reset
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=RESET_MINUTES)).strftime(FMT)
    with _db() as con:
        con.execute("DELETE FROM password_resets WHERE user_id = ?", (row["id"],))
        con.execute("INSERT INTO password_resets(user_id, code_hash, expires_at, attempts, created_at) VALUES (?,?,?,0,?)",
                    (row["id"], _reset_hash(row["id"], code), expires, _now()))
    return {"user": _user(row), "code": code}


def create_reset_request(identifier: str, note: str = "") -> bool:
    """A customer asks the admin to approve a password reset. identifier = username or email. Returns True when a request was
    recorded (callers must give the same answer either way, so nobody can discover which accounts exist)."""
    ident = (identifier or "").strip()
    if not ident:
        return False
    with _db() as con:
        row = con.execute("SELECT * FROM users WHERE username = ? OR LOWER(email) = ?", (ident, ident.lower())).fetchone()
        if row is None or not row["active"] or row["role"] == "admin":
            return False
        if con.execute("SELECT 1 FROM reset_requests WHERE user_id = ? AND status = 'pending'", (row["id"],)).fetchone():
            return True
        con.execute("INSERT INTO reset_requests(user_id, username, note, status, created_at) VALUES (?,?,?,?,?)",
                    (row["id"], row["username"], (note or "")[:300], "pending", _now()))
    return True


def list_reset_requests(status: str = "pending") -> list[dict]:
    with _db() as con:
        q = ("SELECT r.id, r.user_id, r.username, r.note, r.status, r.created_at, r.decided_at, u.email, u.telegram_id "
             "FROM reset_requests r JOIN users u ON u.id = r.user_id")
        rows = con.execute(q + (" WHERE r.status = ?" if status != "all" else "") + " ORDER BY r.id DESC LIMIT 100", (status,) if status != "all" else ()).fetchall()
    return [{"id": r["id"], "username": r["username"], "note": r["note"] or "", "status": r["status"], "created_at": r["created_at"],
             "has_email": bool(r["email"]), "has_telegram": bool(r["telegram_id"])} for r in rows]


def decide_reset_request(request_id: int, approve: bool, admin_name: str) -> dict:
    """Approve = generate a random 6-digit one-time code for that customer (valid RESET_MINUTES). Returns {user, code} or {user, code: None}."""
    with _db() as con:
        req = con.execute("SELECT * FROM reset_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None or req["status"] != "pending":
            raise ValueError("That request was already handled.")
        row = con.execute("SELECT * FROM users WHERE id = ?", (req["user_id"],)).fetchone()
        con.execute("UPDATE reset_requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                    ("approved" if approve else "rejected", _now(), admin_name, request_id))
    if not approve or row is None:
        return {"user": _user(row) if row else None, "code": None}
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=RESET_MINUTES)).strftime(FMT)
    with _db() as con:
        con.execute("DELETE FROM password_resets WHERE user_id = ?", (row["id"],))
        con.execute("INSERT INTO password_resets(user_id, code_hash, expires_at, attempts, created_at) VALUES (?,?,?,0,?)",
                    (row["id"], _reset_hash(row["id"], code), expires, _now()))
    return {"user": _user(row), "code": code}


def confirm_password_reset(username: str, code: str, new_password: str) -> dict:
    bad = ValueError("That code is wrong or has expired. Request a new one.")
    _check_password(new_password)  # do not burn the code on a too-short password
    ok, uid = False, None
    with _db() as con:  # decide inside, raise after: an exception would roll back the attempt counter
        user = con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if user is not None and user["role"] != "admin":
            rec = con.execute("SELECT * FROM password_resets WHERE user_id = ?", (user["id"],)).fetchone()
            if rec is not None:
                if rec["expires_at"] < _now() or rec["attempts"] >= RESET_MAX_TRIES:
                    con.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
                elif secrets.compare_digest(rec["code_hash"], _reset_hash(user["id"], (code or "").strip())):
                    con.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
                    ok, uid = True, user["id"]
                else:
                    con.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE user_id = ?", (user["id"],))
    if not ok:
        raise bad
    return update_user(uid, password=new_password)


def list_payment_requests(status: str = "pending") -> list[dict]:
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    with _db() as con:
        q = ("SELECT p.id, p.company_id, p.user_id, p.plan, p.amount_ngn, p.transfer_reference, p.status, "
             "p.created_at, p.reviewed_at, p.reviewed_by, p.review_note, p.proof_at, "
             "(CASE WHEN p.proof_data IS NULL THEN 0 ELSE 1 END) AS has_proof, "
             "c.name AS company_name, u.username FROM payment_requests p "
             "JOIN companies c ON c.id=p.company_id JOIN users u ON u.id=p.user_id")
        args = ()
        if status != "all":
            q += " WHERE p.status=?"; args = (status,)
        q += " ORDER BY p.id DESC LIMIT 300"
        return [dict(r) for r in con.execute(q, args).fetchall()]


def decide_payment_request(request_id: int, decision: str, admin_id: int, note: str = "") -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be approve or reject.")
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM payment_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise LookupError("Payment request not found.")
        if row["status"] != "pending":
            raise ValueError("This payment request has already been reviewed.")
        new_status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE payment_requests SET status=?, reviewed_at=?, reviewed_by=?, review_note=? WHERE id=?",
                    (new_status, _now(), admin_id, (note or "")[:300], request_id))
        if decision == "approve":
            now = dt.datetime.now(dt.timezone.utc)
            expiry = _subscription_expiry(row["plan"], now)
            con.execute("UPDATE subscriptions SET status='superseded' WHERE company_id=? AND status='active'",
                        (row["company_id"],))
            con.execute(
                "INSERT INTO subscriptions(company_id,plan,status,starts_at,expires_at,grant_source,granted_by,note,created_at) "
                "VALUES (?,?, 'active',?,?, 'payment',?,?,?)",
                (row["company_id"], row["plan"], now.strftime(FMT), expiry.strftime(FMT), admin_id,
                 f"Payment request #{request_id}", _now()))
        return dict(con.execute("SELECT * FROM payment_requests WHERE id=?", (request_id,)).fetchone())


def list_company_members(company_id: int) -> int:
    with _db() as con:
        return int(con.execute("SELECT COUNT(*) AS n FROM user_companies WHERE company_id=?", (company_id,)).fetchone()["n"])


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


# ---- manual matches and the month-end checklist ------------------------------ #
def add_manual_match(company_id: int, report_a: int, report_b: int, bank_ref: str, ledger_ref: str, reason: str, user: dict) -> int:
    with _db() as con:
        exists = con.execute("SELECT id FROM manual_matches WHERE company_id=? AND report_a=? AND report_b=? AND (bank_ref=? OR ledger_ref=?)",
                             (company_id, report_a, report_b, bank_ref, ledger_ref)).fetchone()
        if exists:
            raise ValueError("One of these two transactions is already manually matched.")
        con.execute("INSERT INTO manual_matches(company_id, report_a, report_b, bank_ref, ledger_ref, reason, created_by, created_by_name, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (company_id, report_a, report_b, bank_ref, ledger_ref, (reason or "")[:300], user["id"], user["username"], _now()))
        return con.execute("SELECT MAX(id) AS m FROM manual_matches WHERE company_id=?", (company_id,)).fetchone()["m"]


def list_manual_matches(company_id: int, report_a: int | None = None, report_b: int | None = None) -> list[dict]:
    q, p = "SELECT * FROM manual_matches WHERE company_id=?", [company_id]
    if report_a is not None and report_b is not None:
        q += " AND report_a=? AND report_b=?"; p += [report_a, report_b]
    with _db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", p).fetchall()]


def delete_manual_match(match_id: int, company_id: int) -> bool:
    with _db() as con:
        con.execute("DELETE FROM manual_matches WHERE id=? AND company_id=?", (match_id, company_id))
    return True


CLOSE_ITEMS = ["Upload every bank statement for the month", "Upload the matching ledger or POS report for each account",
               "Run the reconciliation for each account", "Clear or explain every unmatched transaction",
               "Resolve duplicate references", "Review the discrepancy cases and close the resolved ones",
               "Download the PDF and Excel reports for your records", "Sign off the month"]


def get_checklist(company_id: int, month: str) -> list[dict]:
    with _db() as con:
        rows = {r["item"]: dict(r) for r in con.execute("SELECT * FROM close_checklist WHERE company_id=? AND month=?", (company_id, month)).fetchall()}
    return [{"item": i, "done": bool(rows.get(i, {}).get("done")), "done_by": rows.get(i, {}).get("done_by"), "done_at": rows.get(i, {}).get("done_at")} for i in CLOSE_ITEMS]


def set_checklist(company_id: int, month: str, item: str, done: bool, user: dict) -> None:
    if item not in CLOSE_ITEMS:
        raise ValueError("Unknown checklist item.")
    with _db() as con:
        con.execute("DELETE FROM close_checklist WHERE company_id=? AND month=? AND item=?", (company_id, month, item))
        con.execute("INSERT INTO close_checklist(company_id, month, item, done, done_by, done_at) VALUES (?,?,?,?,?,?)",
                    (company_id, month, item, 1 if done else 0, user["username"] if done else None, _now() if done else None))


# ---- email codes: sign-up verification and sign-in on a new device ---------------- #
EMAIL_CODE_MINUTES = 10


def _ecode_hash(key: str, purpose: str, code: str) -> str:
    return hashlib.sha256(f"{key}|{purpose}|{code}|{os.getenv('ENCRYPTION_KEY', 'ra')}".encode()).hexdigest()


def issue_email_code(key: str, purpose: str) -> str:
    """A fresh 6-digit code. Raises ValueError if one was sent less than a minute ago (stops mail flooding)."""
    key = key.strip().lower()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with _db() as con:
        row = con.execute("SELECT created_at FROM email_codes WHERE key=? AND purpose=?", (key, purpose)).fetchone()
        if row:
            try:
                if (now - dt.datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 60:
                    raise ValueError("A code was just sent. Wait a minute before asking for another.")
            except ValueError as e:
                if "just sent" in str(e):
                    raise
        code = f"{secrets.randbelow(1_000_000):06d}"
        con.execute("DELETE FROM email_codes WHERE key=? AND purpose=?", (key, purpose))
        con.execute("INSERT INTO email_codes(key, purpose, code_hash, expires_at, attempts, created_at) VALUES (?,?,?,?,0,?)",
                    (key, purpose, _ecode_hash(key, purpose, code), (now + dt.timedelta(minutes=EMAIL_CODE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")))
    return code


def check_email_code(key: str, purpose: str, code: str) -> bool:
    """True once for a right, unexpired code. Five wrong tries burn the code."""
    key = key.strip().lower()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with _db() as con:
        rec = con.execute("SELECT * FROM email_codes WHERE key=? AND purpose=?", (key, purpose)).fetchone()
        if not rec:
            return False
        if rec["attempts"] >= 5 or dt.datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S") < now:
            con.execute("DELETE FROM email_codes WHERE key=? AND purpose=?", (key, purpose))
            return False
        if secrets.compare_digest(rec["code_hash"], _ecode_hash(key, purpose, (code or "").strip())):
            con.execute("DELETE FROM email_codes WHERE key=? AND purpose=?", (key, purpose))
            return True
        con.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE key=? AND purpose=?", (key, purpose))
        return False


def mark_email_verified(email: str) -> None:
    e = (email or "").strip().lower()
    with _db() as con:
        con.execute("DELETE FROM verified_emails WHERE email=?", (e,))
        con.execute("INSERT INTO verified_emails(email, verified_at) VALUES (?,?)", (e, _now()))


def is_email_verified(email: str | None) -> bool:
    if not email:
        return False
    with _db() as con:
        return con.execute("SELECT 1 FROM verified_emails WHERE email=?", ((email or "").strip().lower(),)).fetchone() is not None


def trust_device(user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    with _db() as con:
        con.execute("INSERT INTO trusted_devices(user_id, token_hash, created_at) VALUES (?,?,?)", (user_id, hashlib.sha256(token.encode()).hexdigest(), _now()))
    return token


def is_trusted_device(user_id: int, token: str | None) -> bool:
    if not token:
        return False
    with _db() as con:
        return con.execute("SELECT 1 FROM trusted_devices WHERE user_id=? AND token_hash=?", (user_id, hashlib.sha256(token.encode()).hexdigest())).fetchone() is not None


def forget_trusted_devices(user_id: int) -> None:
    with _db() as con:
        con.execute("DELETE FROM trusted_devices WHERE user_id=?", (user_id,))
