"""Enterprise workflows: discrepancy cases, durable reconciliation history, schedules, invoices and integrations.

Vendor-specific accounting APIs are not hard-coded here. The signed integration endpoint accepts
exports from external systems; provider-specific OAuth/API adapters require vendor credentials.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
from typing import Any

import db

FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS discrepancy_cases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, recon_run_id INTEGER,
 report_a INTEGER NOT NULL, report_b INTEGER NOT NULL, match_key TEXT, discrepancy_type TEXT NOT NULL,
 severity TEXT NOT NULL DEFAULT 'medium', status TEXT NOT NULL DEFAULT 'open', details_json TEXT NOT NULL,
 assigned_user_id INTEGER, created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_company_status ON discrepancy_cases(company_id,status,id DESC);
CREATE TABLE IF NOT EXISTS discrepancy_case_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, actor_user_id INTEGER,
 action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, report_a INTEGER NOT NULL,
 report_b INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual', created_by INTEGER,
 run_at TEXT NOT NULL, total_rows INTEGER NOT NULL DEFAULT 0, matched_rows INTEGER NOT NULL DEFAULT 0,
 discrepancy_rows INTEGER NOT NULL DEFAULT 0, difference_total REAL NOT NULL DEFAULT 0,
 summary_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'completed', error TEXT
);
CREATE INDEX IF NOT EXISTS idx_recon_history ON reconciliation_runs(company_id,run_at DESC);
CREATE TABLE IF NOT EXISTS scheduled_reconciliations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, report_a INTEGER NOT NULL, report_b INTEGER NOT NULL,
 created_by INTEGER NOT NULL, interval_hours INTEGER NOT NULL DEFAULT 24, instruction TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1, next_run TEXT NOT NULL, last_run TEXT, last_status TEXT, last_error TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_settings (
 company_id INTEGER PRIMARY KEY, emails_json TEXT NOT NULL DEFAULT '[]', quota_threshold INTEGER NOT NULL DEFAULT 80
);
CREATE TABLE IF NOT EXISTS invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL UNIQUE, company_id INTEGER NOT NULL,
 payment_request_id INTEGER, subscription_id INTEGER, plan TEXT NOT NULL, amount_ngn INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'issued', issued_at TEXT NOT NULL, paid_at TEXT, notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices(company_id,issued_at DESC);
CREATE TABLE IF NOT EXISTS integration_tokens (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 label TEXT NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, event_key TEXT NOT NULL, sent_at TEXT NOT NULL,
 UNIQUE(company_id,event_key)
);
"""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def stamp(value: dt.datetime | None = None) -> str:
    return (value or now()).strftime(FMT)


def init() -> None:
    with db._db() as con:
        con.executescript(SCHEMA)

init()


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def record_reconciliation(company_id: int, report_a: int, report_b: int, result: dict,
                          actor_id: int | None, source: str = "manual") -> tuple[int, int]:
    detail = result.get("detail")
    if detail is None:
        raise ValueError("Reconciliation result has no detail table")
    total = int(len(detail))
    matched = int((detail["Status"] == "Matched").sum()) if "Status" in detail else 0
    problems = detail[detail["Status"] != "Matched"] if "Status" in detail else detail.iloc[0:0]
    difference_total = 0.0
    if "Difference (A-B)" in problems.columns:
        import pandas as pd
        difference_total = float(pd.to_numeric(problems["Difference (A-B)"], errors="coerce").fillna(0).sum())
    summary = result.get("summary")
    summary_records = summary.to_dict(orient="records") if hasattr(summary, "to_dict") else (summary or [])
    with db._db() as con:
        cur = con.execute(
            "INSERT INTO reconciliation_runs(company_id,report_a,report_b,source,created_by,run_at,total_rows,matched_rows,discrepancy_rows,difference_total,summary_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'completed')",
            (company_id, report_a, report_b, source, actor_id, stamp(), total, matched, int(len(problems)), difference_total, _json(summary_records)))
        run_id = int(cur.lastrowid)
        created = 0
        for _, row in problems.head(1000).iterrows():
            record = {str(k): (None if _is_na(v) else v) for k, v in row.to_dict().items()}
            dtype = str(record.get("Status", "Discrepancy"))[:80]
            diff = 0.0
            try:
                diff = abs(float(record.get("Difference (A-B)") or 0))
            except (TypeError, ValueError):
                pass
            severity = "high" if dtype == "Amount mismatch" and diff >= 100000 else ("medium" if dtype == "Amount mismatch" else "low")
            cur2 = con.execute(
                "INSERT INTO discrepancy_cases(company_id,recon_run_id,report_a,report_b,match_key,discrepancy_type,severity,status,details_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'open',?,?,?,?)",
                (company_id, run_id, report_a, report_b, str(record.get("Match key", ""))[:200], dtype, severity,
                 _json(record), actor_id, stamp(), stamp()))
            case_id = int(cur2.lastrowid)
            con.execute("INSERT INTO discrepancy_case_events(case_id,actor_user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                        (case_id, actor_id, "created", f"Created from reconciliation run #{run_id}", stamp()))
            created += 1
    return run_id, created


def _is_na(v: Any) -> bool:
    try:
        import pandas as pd
        return bool(pd.isna(v))
    except Exception:
        return False


def list_cases(company_id: int, status: str = "all", limit: int = 200) -> list[dict]:
    allowed = {"open", "in_review", "resolved", "accepted", "all"}
    status = status if status in allowed else "all"
    with db._db() as con:
        q = "SELECT c.*,u.username AS assigned_username,cu.username AS created_by_username FROM discrepancy_cases c LEFT JOIN users u ON u.id=c.assigned_user_id LEFT JOIN users cu ON cu.id=c.created_by WHERE c.company_id=?"
        args: list[Any] = [company_id]
        if status != "all": q += " AND c.status=?"; args.append(status)
        q += " ORDER BY CASE c.status WHEN 'open' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END, c.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = [dict(r) for r in con.execute(q, args).fetchall()]
    for row in rows:
        row["details"] = json.loads(row.pop("details_json"))
    return rows


def get_case(case_id: int) -> dict | None:
    with db._db() as con:
        r = con.execute("SELECT * FROM discrepancy_cases WHERE id=?", (case_id,)).fetchone()
    if not r: return None
    out = dict(r); out["details"] = json.loads(out.pop("details_json")); return out


def update_case(case_id: int, actor_id: int, status: str | None = None, assigned_user_id: int | None = None,
                comment: str = "") -> dict:
    if status is not None and status not in {"open", "in_review", "resolved", "accepted"}:
        raise ValueError("Status must be open, in_review, resolved or accepted.")
    if len(comment) > 2000: raise ValueError("Comment is too long (maximum 2000 characters).")
    with db._db() as con:
        row = con.execute("SELECT * FROM discrepancy_cases WHERE id=?", (case_id,)).fetchone()
        if not row: raise LookupError("Discrepancy case not found.")
        if assigned_user_id is not None:
            if not db.can_access({"id": assigned_user_id, "role": "analyst"}, row["company_id"]):
                raise ValueError("Assignee must have access to this company.")
            con.execute("UPDATE discrepancy_cases SET assigned_user_id=?,updated_at=? WHERE id=?", (assigned_user_id, stamp(), case_id))
            con.execute("INSERT INTO discrepancy_case_events(case_id,actor_user_id,action,detail,created_at) VALUES(?,?,?,?,?)", (case_id, actor_id, "assigned", f"Assigned to user #{assigned_user_id}", stamp()))
        if status is not None:
            resolved = stamp() if status in {"resolved", "accepted"} else None
            con.execute("UPDATE discrepancy_cases SET status=?,resolved_at=?,updated_at=? WHERE id=?", (status, resolved, stamp(), case_id))
            con.execute("INSERT INTO discrepancy_case_events(case_id,actor_user_id,action,detail,created_at) VALUES(?,?,?,?,?)", (case_id, actor_id, "status_changed", status, stamp()))
        if comment.strip():
            con.execute("INSERT INTO discrepancy_case_events(case_id,actor_user_id,action,detail,created_at) VALUES(?,?,?,?,?)", (case_id, actor_id, "comment", comment.strip(), stamp()))
    return get_case(case_id) or {}


def case_events(case_id: int) -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT e.*,u.username FROM discrepancy_case_events e LEFT JOIN users u ON u.id=e.actor_user_id WHERE e.case_id=? ORDER BY e.id", (case_id,)).fetchall()]


def list_reconciliation_runs(company_id: int, limit: int = 100) -> list[dict]:
    with db._db() as con:
        rows = [dict(r) for r in con.execute("SELECT r.*,a.filename AS report_a_name,b.filename AS report_b_name,u.username AS actor FROM reconciliation_runs r LEFT JOIN reports a ON a.id=r.report_a LEFT JOIN reports b ON b.id=r.report_b LEFT JOIN users u ON u.id=r.created_by WHERE r.company_id=? ORDER BY r.id DESC LIMIT ?", (company_id, max(1, min(limit, 500)))).fetchall()]
    for r in rows: r["summary"] = json.loads(r.pop("summary_json"))
    return rows


def trend_summary(company_id: int, days: int = 90) -> dict:
    cutoff = stamp(now() - dt.timedelta(days=max(1, min(days, 365))))
    with db._db() as con:
        rows = [dict(r) for r in con.execute("SELECT substr(run_at,1,10) AS day,COUNT(*) AS runs,SUM(total_rows) AS rows,SUM(discrepancy_rows) AS discrepancies,SUM(matched_rows) AS matched, SUM(difference_total) AS net_difference FROM reconciliation_runs WHERE company_id=? AND run_at>=? GROUP BY substr(run_at,1,10) ORDER BY day", (company_id, cutoff)).fetchall()]
        totals = dict(con.execute("SELECT COUNT(*) AS runs,COALESCE(SUM(discrepancy_rows),0) AS discrepancies,COALESCE(SUM(matched_rows),0) AS matched FROM reconciliation_runs WHERE company_id=? AND run_at>=?", (company_id, cutoff)).fetchone())
    return {"days": days, "series": rows, "totals": totals}


def create_schedule(company_id: int, report_a: int, report_b: int, created_by: int, interval_hours: int, instruction: str) -> dict:
    if interval_hours < 1 or interval_hours > 24*90: raise ValueError("Interval must be 1 hour to 90 days.")
    nowdt = now(); nxt = nowdt + dt.timedelta(hours=interval_hours)
    with db._db() as con:
        cur = con.execute("INSERT INTO scheduled_reconciliations(company_id,report_a,report_b,created_by,interval_hours,instruction,active,next_run,created_at) VALUES(?,?,?,?,?,?,1,?,?)", (company_id,report_a,report_b,created_by,interval_hours,instruction[:500],stamp(nxt),stamp(nowdt)))
        row = con.execute("SELECT * FROM scheduled_reconciliations WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_schedules(company_id: int) -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT s.*,a.filename AS report_a_name,b.filename AS report_b_name FROM scheduled_reconciliations s LEFT JOIN reports a ON a.id=s.report_a LEFT JOIN reports b ON b.id=s.report_b WHERE s.company_id=? ORDER BY s.id DESC", (company_id,)).fetchall()]


def due_schedules() -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM scheduled_reconciliations WHERE active=1 AND next_run<=? ORDER BY next_run LIMIT 20", (stamp(),)).fetchall()]


def finish_schedule(schedule_id: int, status: str, error: str = "") -> None:
    with db._db() as con:
        row = con.execute("SELECT interval_hours FROM scheduled_reconciliations WHERE id=?", (schedule_id,)).fetchone()
        if not row: return
        nxt = now() + dt.timedelta(hours=int(row["interval_hours"]))
        con.execute("UPDATE scheduled_reconciliations SET last_run=?,last_status=?,last_error=?,next_run=? WHERE id=?", (stamp(), status[:30], error[:500], stamp(nxt), schedule_id))


def delete_schedule(schedule_id: int) -> bool:
    with db._db() as con: return con.execute("DELETE FROM scheduled_reconciliations WHERE id=?", (schedule_id,)).rowcount > 0


def set_notification_emails(company_id: int, emails: list[str], threshold: int = 80) -> None:
    import re
    clean = []
    for email in emails[:10]:
        email = str(email).strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): raise ValueError(f"Invalid email address: {email}")
        if email not in clean: clean.append(email)
    if not 1 <= threshold <= 100: raise ValueError("Quota threshold must be between 1 and 100 percent.")
    with db._db() as con:
        con.execute("INSERT INTO notification_settings(company_id,emails_json,quota_threshold) VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET emails_json=excluded.emails_json,quota_threshold=excluded.quota_threshold", (company_id,_json(clean),threshold))


def notification_settings(company_id: int) -> dict:
    with db._db() as con: row = con.execute("SELECT * FROM notification_settings WHERE company_id=?", (company_id,)).fetchone()
    return {"company_id":company_id,"emails":json.loads(row["emails_json"]),"quota_threshold":row["quota_threshold"]} if row else {"company_id":company_id,"emails":[],"quota_threshold":80}


def create_invoice(company_id: int, payment_request_id: int, subscription_id: int | None, plan: str, amount: int) -> dict:
    with db._db() as con:
        existing = con.execute("SELECT * FROM invoices WHERE payment_request_id=?", (payment_request_id,)).fetchone()
        if existing: return dict(existing)
        company = con.execute("SELECT name FROM companies WHERE id=?", (company_id,)).fetchone()
        stamp_id = now().strftime("%Y%m%d")
        cur = con.execute("INSERT INTO invoices(invoice_number,company_id,payment_request_id,subscription_id,plan,amount_ngn,status,issued_at,paid_at,notes) VALUES(?,?,?,?,?,?,'paid',?,?,?)", (f"RA-{stamp_id}-{secrets.token_hex(3).upper()}",company_id,payment_request_id,subscription_id,plan,amount,stamp(),stamp(),"Bank transfer manually verified by administrator"))
        row = con.execute("SELECT * FROM invoices WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_invoices(company_id: int) -> list[dict]:
    with db._db() as con: return [dict(r) for r in con.execute("SELECT * FROM invoices WHERE company_id=? ORDER BY id DESC LIMIT 100", (company_id,)).fetchall()]


def get_invoice(invoice_id: int) -> dict | None:
    with db._db() as con:
        r = con.execute("SELECT i.*,c.name AS company_name FROM invoices i JOIN companies c ON c.id=i.company_id WHERE i.id=?", (invoice_id,)).fetchone()
    return dict(r) if r else None


def create_integration_token(company_id: int, label: str, user_id: int) -> str:
    token = "ra_int_" + secrets.token_urlsafe(32)
    with db._db() as con:
        con.execute("INSERT INTO integration_tokens(company_id,token_hash,label,created_by,created_at) VALUES(?,?,?,?,?)", (company_id,hashlib.sha256(token.encode()).hexdigest(),label[:100],user_id,stamp()))
    return token


def validate_integration_token(token: str) -> dict | None:
    if not token or len(token) > 200: return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db._db() as con:
        row = con.execute("SELECT * FROM integration_tokens WHERE token_hash=? AND revoked_at IS NULL", (digest,)).fetchone()
        if not row: return None
        con.execute("UPDATE integration_tokens SET last_used_at=? WHERE id=?", (stamp(),row["id"]))
    return dict(row)


def revoke_integration_token(token_id: int, company_id: int) -> bool:
    with db._db() as con:
        return con.execute("UPDATE integration_tokens SET revoked_at=? WHERE id=? AND company_id=? AND revoked_at IS NULL", (stamp(),token_id,company_id)).rowcount > 0


def list_integration_tokens(company_id: int) -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT id,company_id,label,created_by,created_at,last_used_at,revoked_at FROM integration_tokens WHERE company_id=? ORDER BY id DESC", (company_id,)).fetchall()]


def log_notification_once(company_id: int, event_key: str) -> bool:
    with db._db() as con:
        try:
            con.execute("INSERT INTO notification_log(company_id,event_key,sent_at) VALUES(?,?,?)", (company_id,event_key,stamp()))
            return True
        except db.IntegrityError: return False


def check_quota_warning(company_id: int) -> bool:
    status = db.get_plan_status(company_id)
    limit = int(status.get("monthly_limit") or 0)
    if limit <= 0:
        return False
    settings = notification_settings(company_id)
    threshold = int(settings.get("quota_threshold", 80))
    percent = int(status.get("used", 0) * 100 / limit)
    if percent < threshold:
        return False
    key = f"quota:{status.get('month')}:{threshold}"
    if not log_notification_once(company_id, key):
        return False
    company = db.get_company(company_id)
    message = (f"⚠️ Report Analyzer usage warning for {company['name'] if company else 'your company'}: "
               f"{status['used']}/{limit} analyses used ({percent}%). {status['remaining']} remain this month.")
    import notify
    notify.alert_company(company_id, message)
    notify.email_company(company_id, "Report Analyzer usage warning", message)
    return True
