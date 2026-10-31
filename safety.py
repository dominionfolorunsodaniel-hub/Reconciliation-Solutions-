"""
safety.py - account security tools, all website-only:
  * devices signed in (list, sign one out, sign all others out)
  * an alert (bell + email) when someone signs in from a device never seen before
  * "download my data" (ZIP) and "delete my account" requests that the owner then handles
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import zipfile

import db
import extras
import notify

log = logging.getLogger("safety")
FMT = db.FMT
SCHEMA = """
CREATE TABLE IF NOT EXISTS known_devices (
 user_id INTEGER NOT NULL, fp TEXT NOT NULL, label TEXT NOT NULL, ip TEXT, first_seen TEXT NOT NULL,
 PRIMARY KEY (user_id, fp)
);
CREATE TABLE IF NOT EXISTS deletion_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT NOT NULL, note TEXT,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, handled_at TEXT
);
"""
db.run_startup_migration(lambda con: con.executescript(SCHEMA))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


# ------------------------------------------------------------------ devices
def device_label(ua: str | None) -> str:
    ua = ua or ""
    u = ua.lower()
    if "edg/" in u or "edge/" in u:
        br = "Edge"
    elif "opr/" in u or "opera" in u:
        br = "Opera"
    elif "firefox" in u:
        br = "Firefox"
    elif "chrome" in u or "crios" in u:
        br = "Chrome"
    elif "safari" in u:
        br = "Safari"
    else:
        br = "A browser"
    if "iphone" in u:
        os_ = "iPhone"
    elif "ipad" in u:
        os_ = "iPad"
    elif "android" in u:
        os_ = "Android"
    elif "windows" in u:
        os_ = "Windows"
    elif "mac os" in u or "macintosh" in u:
        os_ = "Mac"
    elif "linux" in u:
        os_ = "Linux"
    else:
        os_ = "an unknown device"
    return f"{br} on {os_}"


def _fp(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:24]


def note_login(user: dict, ua: str, ip: str) -> bool:
    """Remember this device. Returns True (and alerts the owner) when it is new and the account already knew other devices."""
    label = device_label(ua)
    fp = _fp(label)
    try:
        with db._db() as con:
            seen = con.execute("SELECT 1 FROM known_devices WHERE user_id = ? AND fp = ?", (user["id"], fp)).fetchone()
            if seen:
                return False
            others = con.execute("SELECT COUNT(*) AS n FROM known_devices WHERE user_id = ?", (user["id"],)).fetchone()["n"]
            con.execute("INSERT INTO known_devices(user_id, fp, label, ip, first_seen) VALUES (?,?,?,?,?)",
                        (user["id"], fp, label, (ip or "")[:64], _now()))
        if others:
            extras.tell_user(user["id"], "🔐 New sign-in to your account",
                             f"Someone signed in from a device we have not seen before: {label} (network address {ip or 'unknown'}) "
                             f"at {_now()} UTC.\n\nIf this was you, you can ignore this message. If it was not you, go to "
                             f"Settings, choose \"Sign out all other devices\", and change your password.", "warning")
            return True
    except Exception:  # noqa: BLE001  (an alert problem must never block a login)
        log.exception("device check failed")
    return False


def list_sessions(user_id: int, current_token: str | None) -> list[dict]:
    cur = db._th(current_token) if current_token else ""
    with db._db() as con:
        rows = con.execute("SELECT token_hash, user_agent, ip, created_at, expires_at FROM sessions "
                           "WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC", (user_id, _now())).fetchall()
    return [{"id": r["token_hash"][:20], "device": device_label(r["user_agent"]), "ip": r["ip"] or "", "signed_in": r["created_at"],
             "current": r["token_hash"] == cur} for r in rows]


def revoke_session(user_id: int, session_id: str, current_token: str | None) -> bool:
    if not session_id or len(session_id) < 12 or len(session_id) > 64:
        return False
    cur = db._th(current_token) if current_token else ""
    with db._db() as con:
        rows = con.execute("SELECT token_hash FROM sessions WHERE user_id = ?", (user_id,)).fetchall()
        hit = [r["token_hash"] for r in rows if r["token_hash"].startswith(session_id) and r["token_hash"] != cur]
        for h in hit:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (h,))
    return bool(hit)


def revoke_other_sessions(user_id: int, current_token: str | None) -> int:
    cur = db._th(current_token) if current_token else ""
    with db._db() as con:
        c = con.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?", (user_id, cur))
        return c.rowcount or 0


# ------------------------------------------------------------------ data export
def _table(con, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001  (a missing optional table just means an empty section)
        log.exception("export section skipped")
        return []


def _csv(rows: list[dict]) -> str:
    if not rows:
        return "(nothing here)\n"
    out = io.StringIO()
    cols = list(rows[0].keys())
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return out.getvalue()


def export_zip(user: dict) -> bytes:
    """Everything the website holds about THIS person. Company files and other people's data are not included."""
    uid = user["id"]
    with db._db() as con:
        profile = _table(con, "SELECT id, username, role, email, active, created_at, referral_code FROM users WHERE id = ?", (uid,))
        companies = [dict(c) for c in db.list_companies(user)]
        ids = [c["id"] for c in companies]
        reports = []
        for cid in ids:
            reports += _table(con, "SELECT id, company_id, filename, rows, uploaded_at FROM reports WHERE company_id = ? ORDER BY id", (cid,))
        questions = _table(con, "SELECT created_at, company_id, report_id, question FROM queries WHERE user_id = ? ORDER BY id", (uid,))
        activity = _table(con, "SELECT ts, source, action, company_id, detail FROM audit WHERE user_id = ? ORDER BY id DESC LIMIT 5000", (uid,))
        notices = _table(con, "SELECT created_at, title, body FROM broadcasts WHERE target_user_id = ? ORDER BY id DESC", (uid,))
        saved = _table(con, "SELECT created_at, company_id, question FROM saved_questions WHERE user_id = ?", (uid,))
        feedback = _table(con, "SELECT created_at, company_id, rating, question, comment FROM feedback WHERE user_id = ?", (uid,))
        shares = _table(con, "SELECT created_at, title, expires_at, revoked, views FROM shared_results WHERE user_id = ?", (uid,))
        devices = _table(con, "SELECT label, ip, first_seen FROM known_devices WHERE user_id = ?", (uid,))
    readme = (
        "Your Report Analyzer data\n"
        f"Created: {_now()} UTC for user '{user['username']}'.\n\n"
        "This file holds the information the website keeps about YOUR account: profile, the companies you belong to, the\n"
        "list of reports (names and dates, not the files), the questions you asked, your activity log, your notifications,\n"
        "saved questions, feedback, shared links and known devices.\n"
        "Your company's uploaded files belong to the company. Ask your company admin or write to the support email for those.\n"
        "Passwords and security codes are never included.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("profile.json", json.dumps(profile[0] if profile else {}, indent=2, default=str))
        for name, rows in (("companies", companies), ("reports", reports), ("questions_asked", questions), ("activity_log", activity),
                           ("notifications", notices), ("saved_questions", saved), ("feedback", feedback),
                           ("shared_links", shares), ("known_devices", devices)):
            z.writestr(f"{name}.csv", _csv(rows))
    return buf.getvalue()


# ------------------------------------------------------------------ deletion requests
def my_request(user_id: int) -> dict | None:
    with db._db() as con:
        row = con.execute("SELECT id, status, created_at FROM deletion_requests WHERE user_id = ? AND status = 'pending' "
                          "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    return dict(row) if row else None


def request_deletion(user: dict, note: str) -> dict:
    if user["role"] == "admin":
        raise ValueError("The owner account cannot be deleted from here.")
    if my_request(user["id"]):
        raise ValueError("You already asked for this. We will contact you soon.")
    with db._db() as con:
        con.execute("INSERT INTO deletion_requests(user_id, username, note, created_at) VALUES (?,?,?,?)",
                    (user["id"], user["username"], (note or "")[:500], _now()))
    msg = f"Account deletion request from '{user['username']}' (user #{user['id']}). Note: {(note or '-')[:300]}"
    try:
        for u in db.list_users():
            if u["role"] == "admin":
                extras.notice(u["id"], "🗑️ Account deletion request", msg, "warning")
        notify.alert_admins("🗑️ " + msg)
        support = os.getenv("SUPPORT_EMAIL", "reconciliationsolutionssupport@gmail.com")
        extras._mail_async(support, "Account deletion request", msg)
    except Exception:  # noqa: BLE001
        log.exception("could not alert the owner about a deletion request")
    extras.tell_user(user["id"], "We received your deletion request",
                     "We will remove your account and personal details within 7 days, and write to you when it is done. "
                     "Changed your mind? Cancel it in Settings, or write to our support email.", "info")
    return my_request(user["id"]) or {}


def cancel_request(user_id: int) -> bool:
    with db._db() as con:
        c = con.execute("UPDATE deletion_requests SET status = 'cancelled', handled_at = ? WHERE user_id = ? AND status = 'pending'",
                        (_now(), user_id))
        return (c.rowcount or 0) > 0


def list_requests() -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, user_id, username, note, status, created_at, handled_at FROM deletion_requests "
                           "ORDER BY (status = 'pending') DESC, id DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def mark_done(request_id: int) -> bool:
    with db._db() as con:
        c = con.execute("UPDATE deletion_requests SET status = 'done', handled_at = ? WHERE id = ? AND status = 'pending'",
                        (_now(), request_id))
        return (c.rowcount or 0) > 0
