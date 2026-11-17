"""seats.py - role unlocks, join-company requests and the welcome coupon.

How it fits together
  * The person who creates a company is its Owner (team, roles, billing, join approvals). Their working level is Analyst;
    the Admin tools (API keys, integrations, schedules, alerts, audit export) unlock with a one-time paid Admin unlock.
    Owners that existed before this build keep every tool.
  * Someone who joins a company (with its join code, after the Owner approves) starts as Viewer.
    Moving up is a paid unlock per person: Viewer -> Analyst, then Analyst -> Admin. Payment goes through the existing
    orders table (kind "seat"), so the site owner verifies it in Revenue like every other payment.
  * Every new company gets one single-use welcome coupon (30% off, one company only) by email and in-app notice,
    and the site owner is told about it on Telegram.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets

import db
import extras
import notify
import portal  # noqa: F401  (makes sure the sign-up requests table exists)
import security

log = logging.getLogger("seats")
FMT = "%Y-%m-%d %H:%M:%S"

SEAT_PRICES = {"analyst": 40000, "admin": 150000}       # one-time, per person
WELCOME_PERCENT = 30
WELCOME_DAYS = 45

ROLE_CATALOGUE = [
    {"key": "viewer", "name": "Viewer", "tagline": "Follow the numbers", "price_ngn": 0, "how": "Where everyone joining a company starts",
     "perks": ["See the dashboard, reports and results", "Follow reconciliation progress and open cases", "Read notifications from your company"],
     "locked": ["Upload files and ask questions", "Run reconciliations and statement checks"]},
    {"key": "analyst", "name": "Analyst", "tagline": "Do the work", "price_ngn": SEAT_PRICES["analyst"], "how": "One-time unlock per person",
     "perks": ["Everything a Viewer can do", "Upload bank statements and ledgers", "Ask questions in plain words", "Reconcile, forecast and score statements", "Match transactions by hand and manage cases"],
     "locked": ["API keys and accounting integrations", "Scheduled reconciliations and email digests"]},
    {"key": "admin", "name": "Admin", "tagline": "Run the controls", "price_ngn": SEAT_PRICES["admin"], "how": "One-time unlock per person",
     "perks": ["Everything an Analyst can do", "API keys and accounting integrations", "Scheduled reconciliations and email digests", "Alerts and notification settings",
               "Audit trail export (CSV) and company activity log", "Delete reports and set company branding"],
     "locked": []},
    {"key": "owner", "name": "Owner", "tagline": "Own the account", "price_ngn": 0, "how": "The person who creates the company",
     "perks": ["Approve who joins and choose their level", "Manage the team and reset passwords", "Buy plans, packs and reports for the company", "Unlock Analyst and Admin access for anyone"],
     "locked": []},
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def _init(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        email TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by INTEGER,
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_join_company ON join_requests(company_id, status);
    """)


db.run_startup_migration(_init)


# ------------------------------------------------------------------------------------------ pricing helpers
def seat_price(role: str) -> int:
    if role not in SEAT_PRICES:
        raise ValueError("Unknown access level.")
    return SEAT_PRICES[role]


def seat_target(company_id: int, member_id: int, role: str) -> dict:
    """Validate an unlock for one person and return {user, role, price}. Viewer -> Analyst -> Admin, in that order.
    An owner can unlock Admin tools for themselves."""
    if role not in SEAT_PRICES:
        raise ValueError("Choose Analyst or Admin.")
    with db._db() as con:
        row = db._team_row(con, company_id, member_id)
    if row is None:
        raise LookupError("That person is not on this team.")
    u = db._user(row)
    if not u["active"]:
        raise ValueError("That person has been removed from the team.")
    cur = db.effective_company_role(u)
    if cur == "owner":
        if role != "admin":
            raise ValueError("The owner already works at Analyst level or above. Unlock Admin tools instead.")
        if db.has_admin_tools(u):
            raise ValueError("Admin tools are already unlocked for this account.")
    elif role == "analyst":
        if cur != "viewer":
            raise ValueError("This person is already an Analyst or above.")
    else:  # admin
        if cur == "admin":
            raise ValueError("This person is already an Admin.")
        if cur == "viewer":
            raise ValueError("Unlock Analyst first. Admin comes after Analyst.")
    return {"user": u, "role": role, "price": SEAT_PRICES[role]}


def apply_seat_unlock(con, company_id: int, member_id: int, role: str) -> None:
    """Called inside the transaction that marks the seat order paid."""
    if role == "analyst":
        con.execute("UPDATE users SET company_role='analyst', role='analyst' WHERE id=? AND company_role='viewer'", (member_id,))
    elif role == "admin":
        con.execute("UPDATE users SET tools_unlocked=1 WHERE id=? AND company_role='owner'", (member_id,))
        con.execute("UPDATE users SET company_role='admin', role='analyst' WHERE id=? AND company_role='analyst'", (member_id,))


def request_upgrade(company_id: int, member: dict, role: str) -> None:
    """A Viewer or Analyst asks the owner to unlock the next level for them."""
    seat_target(company_id, member["id"], role)          # validates the step
    for m in db.list_team(company_id):
        if m["company_role"] == "owner" and m["active"]:
            extras.tell_user(m["id"], f"{member['username']} would like {role.title()} access",
                             f"{member['username']} asked for {role.title()} access (one-time ₦{SEAT_PRICES[role]:,}). "
                             "You can unlock it for them under My Team.", "info")


# ------------------------------------------------------------------------------------------ join code
def _make_code() -> str:
    return "RZJ-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


def get_join_code(company_id: int, create: bool = True) -> str | None:
    with db._db() as con:
        row = con.execute("SELECT join_code FROM companies WHERE id=?", (company_id,)).fetchone()
        if row is None:
            raise LookupError("Unknown company.")
        if row["join_code"] or not create:
            return row["join_code"]
        code = _make_code()
        con.execute("UPDATE companies SET join_code=? WHERE id=?", (code, company_id))
        return code


def regenerate_join_code(company_id: int) -> str:
    code = _make_code()
    with db._db() as con:
        con.execute("UPDATE companies SET join_code=? WHERE id=?", (code, company_id))
    return code


def company_for_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    if len(code) < 8:
        return None
    with db._db() as con:
        row = con.execute("SELECT id, name FROM companies WHERE join_code=?", (code,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------------------------------ join requests
def submit_join_request(code: str, username: str, password: str, email: str | None) -> dict:
    company = company_for_code(code)
    if not company:
        raise ValueError("That join code isn't valid. Ask your company owner for the current code.")
    username = (username or "").strip()
    if not db.USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3–40 characters: letters, digits, . _ @ or -.")
    db._check_password(password or "")
    email = extras.clean_email(email)
    with db._db() as con:
        if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("That username is already in use.")
        if con.execute("SELECT 1 FROM registration_requests WHERE username = ? AND status = 'pending'", (username,)).fetchone() or \
           con.execute("SELECT 1 FROM join_requests WHERE username = ? AND status = 'pending'", (username,)).fetchone():
            raise ValueError("A request for that username is already waiting.")
        pending = con.execute("SELECT COUNT(*) AS n FROM join_requests WHERE company_id=? AND status='pending'", (company["id"],)).fetchone()["n"]
        if pending >= 25:
            raise ValueError("This company already has many requests waiting. Please ask the owner to review them first.")
        cur = con.execute("INSERT INTO join_requests(company_id, username, password_hash, email, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
                          (company["id"], username, security.hash_password(password), email, _now()))
        rid = cur.lastrowid
    for m in db.list_team(company["id"]):
        if m["company_role"] == "owner" and m["active"]:
            extras.tell_user(m["id"], "Someone wants to join your company",
                             f"{username} asked to join {company['name']}. Open My Team to approve or decline. They will start as a Viewer.", "info")
    return {"id": rid, "company_name": company["name"]}


def list_join_requests(company_id: int, status: str = "pending") -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, username, email, status, created_at, decided_at FROM join_requests WHERE company_id=? AND status=? "
                           "ORDER BY id DESC LIMIT 100", (company_id, status)).fetchall()
    return [dict(r) for r in rows]


def decide_join_request(company_id: int, request_id: int, decision: str, owner: dict) -> dict:
    if decision not in ("approve", "decline"):
        raise ValueError("Decision must be approve or decline.")
    with db._db() as con:
        req = con.execute("SELECT * FROM join_requests WHERE id=? AND company_id=?", (request_id, company_id)).fetchone()
    if not req:
        raise LookupError("Join request not found.")
    if req["status"] != "pending":
        raise ValueError("This request was already " + req["status"] + ".")
    company = db.get_company(company_id)
    if decision == "decline":
        with db._db() as con:
            con.execute("UPDATE join_requests SET status='declined', decided_at=?, decided_by=? WHERE id=? AND status='pending'", (_now(), owner["id"], request_id))
        if req["email"]:
            extras._mail_async(req["email"], "Your request to join " + company["name"],
                               f"Hello {req['username']},\n\nThe owner of {company['name']} did not approve your request to join.\n\nRECONZA")
        return {"id": request_id, "status": "declined", "username": req["username"]}
    db.validate_company_membership([company_id])          # the plan's seat limit; raises a clear message
    try:
        with db._db() as con:
            con.execute("BEGIN IMMEDIATE")
            cur_req = con.execute("SELECT status FROM join_requests WHERE id=?", (request_id,)).fetchone()
            if cur_req["status"] != "pending":
                raise ValueError("This request was already " + cur_req["status"] + ".")
            if con.execute("SELECT 1 FROM users WHERE username = ?", (req["username"],)).fetchone():
                raise ValueError("That username was taken while the request was waiting. Decline it and ask them to try another username.")
            cur = con.execute("INSERT INTO users(username, password_hash, role, company_role, email, created_at) VALUES (?,?,'viewer','viewer',?,?)",
                              (req["username"], req["password_hash"], req["email"], _now()))
            uid = cur.lastrowid
            con.execute("INSERT INTO user_companies(user_id, company_id) VALUES (?, ?)", (uid, company_id))
            con.execute("UPDATE join_requests SET status='approved', decided_at=?, decided_by=? WHERE id=?", (_now(), owner["id"], request_id))
    except db.IntegrityError as e:
        raise ValueError("That username is already in use.") from e
    if req["email"]:
        extras._mail_async(req["email"], "You have joined " + company["name"],
                           f"Hello {req['username']},\n\nThe owner of {company['name']} approved your request. You can sign in now as a Viewer: "
                           "you can see the dashboard, reports and results. Ask your owner to unlock Analyst access when you need to upload and analyse files.\n\nRECONZA")
    return {"id": request_id, "status": "approved", "username": req["username"], "user_id": uid}


# ------------------------------------------------------------------------------------------ welcome coupon
def issue_welcome_coupon(company_id: int, username: str, company_name: str, email: str | None) -> dict | None:
    """One single-use coupon for this company only. Safe to call twice: a company only ever gets one."""
    with db._db() as con:
        if con.execute("SELECT 1 FROM coupons WHERE company_id=? AND note LIKE 'Welcome offer%'", (company_id,)).fetchone():
            return None
    c = extras.create_coupon("percent", WELCOME_PERCENT, max_uses=1, days_valid=WELCOME_DAYS,
                             code="WELCOME-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)),
                             note=f"Welcome offer for {company_name}"[:190], company_id=company_id)
    exp = (c["expires_at"] or "")[:10]
    body = (f"Welcome to RECONZA, {username}!\n\nHere is a thank-you for joining: {WELCOME_PERCENT}% off your company's first payment "
            f"(plans, report packs, Marketplace, Report Studio or access unlocks).\n\nYour code: {c['code']}\n"
            f"It works once, for {company_name} only, and expires on {exp}. Enter it where you see \"Coupon\" when you pay.")
    try:
        extras.tell_company(company_id, f"Your welcome offer: {WELCOME_PERCENT}% off", body, "success")
    except Exception:  # noqa: BLE001
        log.exception("welcome coupon notice failed")
    try:
        notify.alert_payment_admins(f"New company approved: {company_name} ({username}).\nA single-use {WELCOME_PERCENT}% welcome coupon {c['code']} "
                                    f"was created and sent to them (valid until {exp}). Change or disable it under Coupons if you wish.")
    except Exception:  # noqa: BLE001
        log.exception("welcome coupon admin alert failed")
    return c
