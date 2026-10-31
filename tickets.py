"""
tickets.py - support tickets inside the website. A customer writes a message, the owner replies in the same thread,
and each side is told through the bell (and by email when an address is on file).
"""
from __future__ import annotations

import datetime as dt
import logging
import os

import db
import extras

log = logging.getLogger("tickets")
FMT = db.FMT
MAX_OPEN_PER_USER = 5
MAX_MESSAGES_PER_HOUR = 12
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, user_id INTEGER NOT NULL, subject TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, user_id INTEGER NOT NULL, is_staff INTEGER NOT NULL DEFAULT 0,
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
db.run_startup_migration(lambda con: con.executescript(SCHEMA))


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def _tell_admins(title: str, body: str) -> None:
    try:
        for u in db.list_users():
            if u["role"] == "admin":
                extras.notice(u["id"], title, body, "info")
        extras._mail_async(os.getenv("SUPPORT_EMAIL", "reconciliationsolutionssupport@gmail.com"), title, body)
    except Exception:  # noqa: BLE001
        log.exception("could not alert the owner about a ticket")


def _recent_messages(con, user_id: int) -> int:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime(FMT)
    return con.execute("SELECT COUNT(*) AS n FROM ticket_messages WHERE user_id = ? AND created_at >= ?", (user_id, since)).fetchone()["n"]


def create(user: dict, company_id: int, subject: str, body: str) -> dict:
    subject, body = (subject or "").strip(), (body or "").strip()
    if len(subject) < 3:
        raise ValueError("Please give your message a short title.")
    if len(body) < 5:
        raise ValueError("Please describe the problem or question.")
    subject, body = subject[:120], body[:3000]
    with db._db() as con:
        open_n = con.execute("SELECT COUNT(*) AS n FROM tickets WHERE user_id = ? AND status <> 'closed'", (user["id"],)).fetchone()["n"]
        if open_n >= MAX_OPEN_PER_USER:
            raise ValueError(f"You have {MAX_OPEN_PER_USER} open conversations already. Close one you no longer need first.")
        if _recent_messages(con, user["id"]) >= MAX_MESSAGES_PER_HOUR:
            raise ValueError("You are sending messages very quickly. Please wait a little.")
        now = _now()
        con.execute("INSERT INTO tickets(company_id, user_id, subject, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (company_id, user["id"], subject, "open", now, now))
        tid = con.execute("SELECT MAX(id) AS i FROM tickets WHERE user_id = ?", (user["id"],)).fetchone()["i"]
        con.execute("INSERT INTO ticket_messages(ticket_id, user_id, is_staff, body, created_at) VALUES (?,?,?,?,?)", (tid, user["id"], 0, body, now))
    _tell_admins(f"💬 New support message from {user['username']}", f"{subject}\n\n{body[:500]}")
    return {"id": tid}


def _row_for(con, ticket_id: int, user: dict):
    t = con.execute("SELECT t.*, u.username FROM tickets t JOIN users u ON u.id = t.user_id WHERE t.id = ?", (ticket_id,)).fetchone()
    if not t or (user["role"] != "admin" and t["user_id"] != user["id"]):
        return None
    return t


def get(ticket_id: int, user: dict) -> dict | None:
    with db._db() as con:
        t = _row_for(con, ticket_id, user)
        if not t:
            return None
        msgs = con.execute("SELECT is_staff, body, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    return {"id": t["id"], "subject": t["subject"], "status": t["status"], "username": t["username"], "company_id": t["company_id"],
            "created_at": t["created_at"], "messages": [{"from": "Support team" if m["is_staff"] else "You" if t["user_id"] == user["id"] else t["username"],
                                                          "staff": bool(m["is_staff"]), "body": m["body"], "at": m["created_at"]} for m in msgs]}


def reply(ticket_id: int, user: dict, body: str) -> dict:
    body = (body or "").strip()[:3000]
    if len(body) < 1:
        raise ValueError("Write your reply first.")
    staff = user["role"] == "admin"
    with db._db() as con:
        t = _row_for(con, ticket_id, user)
        if not t:
            raise LookupError("Conversation not found.")
        if t["status"] == "closed" and not staff:
            raise ValueError("This conversation is closed. Start a new one if you still need help.")
        if _recent_messages(con, user["id"]) >= MAX_MESSAGES_PER_HOUR:
            raise ValueError("You are sending messages very quickly. Please wait a little.")
        now = _now()
        con.execute("INSERT INTO ticket_messages(ticket_id, user_id, is_staff, body, created_at) VALUES (?,?,?,?,?)", (ticket_id, user["id"], 1 if staff else 0, body, now))
        status = "answered" if staff else "open"
        con.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?", (status, now, ticket_id))
        owner_id, subject, uname = t["user_id"], t["subject"], t["username"]
    if staff:
        extras.tell_user(owner_id, f"💬 Reply to: {subject[:60]}", body[:600] + "\n\nOpen Support to continue the conversation.", "info")
    else:
        _tell_admins(f"💬 {uname} replied: {subject[:60]}", body[:500])
    return {"status": status}


def close(ticket_id: int, user: dict) -> bool:
    with db._db() as con:
        t = _row_for(con, ticket_id, user)
        if not t:
            return False
        con.execute("UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?", (_now(), ticket_id))
    return True


def list_mine(user_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, subject, status, created_at, updated_at FROM tickets WHERE user_id = ? ORDER BY updated_at DESC LIMIT 50", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def list_all(status: str | None = None) -> list[dict]:
    q = ("SELECT t.id, t.subject, t.status, t.created_at, t.updated_at, u.username, c.name AS company FROM tickets t "
         "JOIN users u ON u.id = t.user_id LEFT JOIN companies c ON c.id = t.company_id ")
    params: tuple = ()
    if status in ("open", "answered", "closed"):
        q += "WHERE t.status = ? "
        params = (status,)
    q += "ORDER BY (t.status = 'open') DESC, t.updated_at DESC LIMIT 200"
    with db._db() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]
