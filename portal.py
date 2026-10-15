"""portal.py - customer registration requests and approval workflow.

Uses the main Report Analyzer SQLite database so the existing encrypted backup process
backs up these records too. Passwords are hashed before a request is stored.
"""
import datetime as dt
import sqlite3

import db
import security

FMT = "%Y-%m-%d %H:%M:%S"


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


with db._db() as con:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS registration_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL,
        username TEXT NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        telegram_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewed_by INTEGER,
        review_note TEXT
    );
    """)


def submit_registration(company_name: str, username: str, password: str,
                        telegram_id: int | None = None) -> dict:
    company_name = (company_name or "").strip()
    username = (username or "").strip()
    if not company_name or len(company_name) > 120:
        raise ValueError("Company name must be between 1 and 120 characters.")
    if not db.USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3–40 characters: letters, digits, . _ @ or -.")
    db._check_password(password or "")
    if telegram_id is not None:
        try:
            telegram_id = int(telegram_id)
        except (TypeError, ValueError):
            raise ValueError("Telegram ID must be a positive number.")
        if telegram_id <= 0:
            raise ValueError("Telegram ID must be a positive number.")
    # Check both existing accounts and pending requests, then rely on a DB transaction
    # to ensure approval handles any races safely.
    with db._db() as con:
        if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("That username is already in use.")
        if con.execute("SELECT 1 FROM registration_requests WHERE username = ? AND status = 'pending'",
                       (username,)).fetchone():
            raise ValueError("A request for that username is already pending.")
        if con.execute("SELECT 1 FROM companies WHERE name = ?", (company_name,)).fetchone():
            raise ValueError("That company already exists. Contact the administrator.")
        if telegram_id and con.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone():
            raise ValueError("That Telegram ID is already linked to a user.")
        if telegram_id and con.execute(
                "SELECT 1 FROM registration_requests WHERE telegram_id = ? AND status = 'pending'",
                (telegram_id,)).fetchone():
            raise ValueError("A request with that Telegram ID is already pending.")
        cur = con.execute(
            "INSERT INTO registration_requests(company_name, username, password_hash, telegram_id, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (company_name, username, security.hash_password(password), telegram_id, _now()))
        return {"id": cur.lastrowid, "company_name": company_name, "username": username,
                "status": "pending", "created_at": _now()}


def list_registration_requests(status: str = "pending") -> list[dict]:
    with db._db() as con:
        if status not in ("pending", "approved", "rejected", "all"):
            status = "pending"
        if status == "all":
            rows = con.execute(
                "SELECT id, company_name, username, telegram_id, status, created_at, reviewed_at, review_note "
                "FROM registration_requests ORDER BY id DESC LIMIT 300").fetchall()
        else:
            rows = con.execute(
                "SELECT id, company_name, username, telegram_id, status, created_at, reviewed_at, review_note "
                "FROM registration_requests WHERE status = ? ORDER BY id DESC LIMIT 300", (status,)).fetchall()
        return [dict(r) for r in rows]


def decide_registration(request_id: int, decision: str, admin: dict, note: str = "") -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be approve or reject.")
    with db._db() as con:
        req = con.execute("SELECT * FROM registration_requests WHERE id = ?", (request_id,)).fetchone()
        if not req:
            raise LookupError("Registration request not found.")
        if req["status"] != "pending":
            raise ValueError("This request has already been reviewed.")
        if decision == "reject":
            con.execute(
                "UPDATE registration_requests SET status='rejected', reviewed_at=?, reviewed_by=?, review_note=? "
                "WHERE id=? AND status='pending'",
                (_now(), admin["id"], (note or "Not approved")[:300], request_id))
            return {"id": request_id, "status": "rejected", "username": req["username"]}
        # Insert company + user + tenant link in one transaction. Never accept a role from a public form.
        if con.execute("SELECT 1 FROM users WHERE username = ?", (req["username"],)).fetchone():
            raise ValueError("That username was created by another admin while this request was pending.")
        if con.execute("SELECT 1 FROM companies WHERE name = ?", (req["company_name"],)).fetchone():
            raise ValueError("That company name is now in use. Reject this request and ask the customer to resubmit.")
        if req["telegram_id"] and con.execute("SELECT 1 FROM users WHERE telegram_id = ?", (req["telegram_id"],)).fetchone():
            raise ValueError("That Telegram ID is now linked to another user.")
        cur = con.execute("INSERT INTO companies(name, created_at) VALUES (?, ?)", (req["company_name"], _now()))
        company_id = cur.lastrowid
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, telegram_id, created_at) VALUES (?, ?, 'analyst', ?, ?)",
            (req["username"], req["password_hash"], req["telegram_id"], _now()))
        user_id = cur.lastrowid
        con.execute("INSERT INTO user_companies(user_id, company_id) VALUES (?, ?)", (user_id, company_id))
        con.execute(
            "UPDATE registration_requests SET status='approved', reviewed_at=?, reviewed_by=?, review_note=? "
            "WHERE id=? AND status='pending'",
            (_now(), admin["id"], (note or "Approved")[:300], request_id))
        return {"id": request_id, "status": "approved", "username": req["username"],
                "company_name": req["company_name"], "user_id": user_id, "company_id": company_id}
