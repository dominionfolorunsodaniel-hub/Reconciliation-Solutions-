"""
weekly.py - an optional Monday-morning email: what you did last week, plan days and checks left.
Opt-in per person (off by default), needs an email on file, at most one a week.
"""
from __future__ import annotations

import datetime as dt
import logging

import db
import extras
import notify

log = logging.getLogger("weekly")
FMT = db.FMT
SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_prefs (
 user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, last_sent_at TEXT
);
"""
db.run_startup_migration(lambda con: con.executescript(SCHEMA))
ACTIONS = {"upload": "reports uploaded", "analyze": "questions answered", "reconcile": "reconciliations",
           "statement_score": "statement checks", "export": "Excel downloads", "export_pdf": "PDF downloads"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


def get_pref(user_id: int) -> dict:
    with db._db() as con:
        row = con.execute("SELECT enabled, last_sent_at FROM weekly_prefs WHERE user_id = ?", (user_id,)).fetchone()
    u = db.get_user(user_id) or {}
    return {"enabled": bool(row and row["enabled"]), "last_sent_at": row["last_sent_at"] if row else None,
            "has_email": bool(u.get("email"))}


def set_pref(user_id: int, enabled: bool) -> dict:
    u = db.get_user(user_id) or {}
    if enabled and not u.get("email"):
        raise ValueError("Add your email address first (in Settings), then turn this on.")
    with db._db() as con:
        if con.execute("SELECT 1 FROM weekly_prefs WHERE user_id = ?", (user_id,)).fetchone():
            con.execute("UPDATE weekly_prefs SET enabled = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        else:
            con.execute("INSERT INTO weekly_prefs(user_id, enabled) VALUES (?,?)", (user_id, 1 if enabled else 0))
    return get_pref(user_id)


def build_report(user: dict) -> tuple[str, str]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).strftime(FMT)
    companies = db.list_companies(user)[:10]
    lines = [f"Hello {user['username']},", "", "Here is your week on Report Analyzer.", ""]
    if not companies:
        lines.append("You are not linked to a company yet.")
    for c in companies:
        with db._db() as con:
            rows = con.execute("SELECT action, COUNT(*) AS n FROM audit WHERE company_id = ? AND ts >= ? GROUP BY action",
                               (c["id"], since)).fetchall()
        counts = {r["action"]: int(r["n"]) for r in rows}
        done = [f"{counts[a]} {label}" for a, label in ACTIONS.items() if counts.get(a)]
        lines.append(f"{c['name']}")
        lines.append("  Last 7 days: " + (", ".join(done) if done else "no activity"))
        try:
            p = db.get_plan_status(c["id"])
            lines.append(f"  Plan: {str(p['plan']).title()} - {p['remaining']} of {p['monthly_limit']} checks left this month")
            if p.get("expires_at"):
                try:
                    left = (dt.datetime.strptime(p["expires_at"], FMT) - dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)).total_seconds() / 86400
                    if 0 < left <= 7:
                        lines.append(f"  Heads up: your plan ends in about {max(1, round(left))} day(s). Renew to keep going without a break.")
                    elif left <= 0:
                        lines.append("  Your plan has ended. Renew to carry on.")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            log.debug("plan status unavailable for %s", c["id"])
        lines.append("")
    lines += ["Tip: use Statement Score to turn any bank statement into monthly income, spending and a loan-readiness score.", "",
              "You get this email because you turned on the weekly email. Turn it off any time in Settings.", "", "- Report Analyzer"]
    return "Your Report Analyzer week", "\n".join(lines)


def send_one(user_id: int) -> bool:
    u = db.get_user(user_id)
    if not u or not u.get("email") or not u.get("active", 1):
        return False
    subject, body = build_report(u)
    ok = notify.send_email(u["email"], subject, body)
    if ok:
        with db._db() as con:
            con.execute("UPDATE weekly_prefs SET last_sent_at = ? WHERE user_id = ?", (_now(), user_id))
    return ok


def due_user_ids(now_utc: dt.datetime | None = None) -> list[int]:
    cutoff = ((now_utc or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=6, hours=12)).strftime(FMT)
    with db._db() as con:
        rows = con.execute("SELECT user_id FROM weekly_prefs WHERE enabled = 1 AND (last_sent_at IS NULL OR last_sent_at <= ?)",
                           (cutoff,)).fetchall()
    return [r["user_id"] for r in rows]


def run_due() -> int:
    if not notify.email_configured():
        return 0
    n = 0
    for uid in due_user_ids():
        try:
            n += int(send_one(uid))
        except Exception:  # noqa: BLE001
            log.exception("weekly email failed for %s", uid)
    return n
