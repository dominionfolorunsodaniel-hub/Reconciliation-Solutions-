"""Public landing-page helpers.

* A ready-made SAMPLE bank-statement check that anyone can run without signing up. It uses invented data
  (not a real account) and is computed once, so it costs nothing and cannot be abused.
* A contact form: messages are stored (encrypted) so none are lost even when email is not configured, the
  admin gets a bell notification, and a copy is emailed to the support address when email is set up.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
import time

import pandas as pd

import db
import extras
import notify
import statement

log = logging.getLogger("landing")

SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "reconciliationsolutionsupport@gmail.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL, company TEXT,
 message TEXT NOT NULL, created_at TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0
);
"""


def init() -> None:
    db.run_startup_migration(lambda con: con.executescript(SCHEMA))


init()


# ------------------------------------------------------------------ sample statement
# Invented transactions for six months. Balances follow from each other, so the "balance check" passes.
_MONTHS = [(2026, 4), (2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)]
_SIDE_INCOME = {4: 40000, 6: 55000, 8: 35000}  # month number -> a freelance payment
_FOOD = [("POS PURCHASE - SHOPRITE", 18500), ("POS PURCHASE - CHICKEN REPUBLIC", 7200), ("WEB PURCHASE - JUMIA FOOD", 6400)]


def _sample_frame() -> pd.DataFrame:
    rows: list[tuple[dt.datetime, str, float, float]] = []  # date, narration, debit, credit
    for y, m in _MONTHS:
        d = lambda day: dt.datetime(y, m, day)  # noqa: E731
        rows.append((d(2), "NIP TRANSFER TO LANDLORD - RENT", 70000.0, 0.0))
        rows.append((d(4), "AIRTIME / DATA - MTN", 8000.0, 0.0))
        for i, (text, amt) in enumerate(_FOOD):
            rows.append((d(6 + i * 6), text, float(amt), 0.0))
        rows.append((d(9), "IKEDC ELECTRICITY TOKEN", 12000.0, 0.0))
        rows.append((d(12), "BOLT RIDE", 4800.0, 0.0))
        rows.append((d(15), "ATM WITHDRAWAL", 20000.0, 0.0))
        rows.append((d(19), "NIP TRANSFER TO FAMILY", 25000.0, 0.0))
        if m in _SIDE_INCOME:
            rows.append((d(21), "NIP TRANSFER FROM CLIENT - FREELANCE", 0.0, float(_SIDE_INCOME[m])))
        rows.append((d(27), "SALARY - ACME LOGISTICS LTD", 0.0, 350000.0))
    rows.sort(key=lambda r: r[0])
    balance, out = 220000.0, []
    for date, text, debit, credit in rows:
        balance += credit - debit
        out.append({"Date": date, "Narration": text, "Debit": debit or None, "Credit": credit or None, "Balance": round(balance, 2)})
    return pd.DataFrame(out)


_SAMPLE: dict | None = None
_SAMPLE_LOCK = threading.Lock()


def sample_result() -> dict:
    """The sample check, shaped for the landing page. Computed once and reused."""
    global _SAMPLE
    with _SAMPLE_LOCK:
        if _SAMPLE is None:
            r = statement.build(_sample_frame())
            t = r["table"]
            cats = statement.categories_json(r.get("categories")) or {}
            _SAMPLE = {
                "sample": True,
                "note": "This is an invented sample, not a real account. Upload your own statement after you sign in.",
                "score": r["score"], "verdict": r["verdict"], "period": r["period"],
                "totals": r["totals"], "strengths": r["strengths"][:3], "concerns": r["concerns"][:3],
                "integrity": {"status": r["integrity"]["status"], "message": r["integrity"]["message"]},
                "flags": len(r["flags"]),
                "spending": (cats.get("spending") or [])[:5],
                "months": [
                    {"month": row["Month"], "income": row["Income (credits)"], "spent": row["Expenses (debits)"],
                     "left": row["Left over (income - expenses)"]}
                    for _, row in t.iterrows()
                ],
            }
        return _SAMPLE


# ------------------------------------------------------------------ contact form
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_HITS: dict[str, list[float]] = {}
MAX_PER_HOUR = 5


def _too_many(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < 3600]
    if len(hits) >= MAX_PER_HOUR:
        _HITS[ip] = hits
        return True
    hits.append(now)
    _HITS[ip] = hits
    return False


def submit_contact(name: str, email: str, company: str, message: str, honeypot: str, ip: str) -> dict:
    """Store a message from the public contact form. Raises ValueError with a friendly reason."""
    if honeypot:  # a hidden field only robots fill in: pretend it worked and keep nothing
        return {"ok": True}
    name, email, company, message = (name or "").strip(), (email or "").strip(), (company or "").strip(), (message or "").strip()
    if len(name) < 2 or len(name) > 80:
        raise ValueError("Please enter your name.")
    if not _EMAIL.match(email) or len(email) > 160:
        raise ValueError("Please enter a valid email address so we can reply.")
    if len(message) < 10:
        raise ValueError("Please write a few words about how we can help.")
    if len(message) > 2000:
        raise ValueError("Your message is too long (2000 characters maximum).")
    if _too_many(ip):
        raise ValueError("Too many messages from this connection. Please try again in an hour or email us directly.")
    with db._db() as con:
        con.execute("INSERT INTO contact_messages(name,email,company,message,created_at) VALUES (?,?,?,?,?)",
                    (db._enc_text(name), db._enc_text(email), db._enc_text(company[:120]), db._enc_text(message), extras._now()))
    try:
        for u in db.list_users():
            if u.get("role") == "admin" and u.get("active", 1):
                extras.notice(u["id"], "New contact message", f"{name} ({email}) wrote from the website: {message[:300]}")
        extras._mail_async(SUPPORT_EMAIL, f"Website contact: {name}",
                           f"From: {name} <{email}>\nCompany: {company or '-'}\n\n{message}\n\nReply to {email}.")
    except Exception:  # noqa: BLE001  (telling the owner must never lose the message that is already saved)
        log.exception("could not notify about contact message")
    return {"ok": True}


def list_contacts(limit: int = 50) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id,name,email,company,message,created_at,handled FROM contact_messages ORDER BY id DESC LIMIT ?",
                           (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("name", "email", "company", "message"):
            d[k] = db._dec_text(d[k]) or ""
        out.append(d)
    return out


def mark_handled(contact_id: int, handled: bool = True) -> bool:
    with db._db() as con:
        cur = con.execute("UPDATE contact_messages SET handled = ? WHERE id = ?", (1 if handled else 0, int(contact_id)))
        return bool(getattr(cur, "rowcount", 1))
