"""
extras.py - customer growth + owner tools, all website-only:
  email on file, personal notifications (+ email), referral codes, coupon codes, feedback,
  saved questions, the owner's overview numbers and the plan-expiry reminders.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import secrets
import threading

import db
import notify

log = logging.getLogger("extras")
FMT = db.FMT
REFERRAL_BONUS_DAYS = float(os.getenv("REFERRAL_BONUS_DAYS", "1"))
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS coupons (
 code TEXT PRIMARY KEY, kind TEXT NOT NULL, value REAL NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1,
 used INTEGER NOT NULL DEFAULT 0, expires_at TEXT, active INTEGER NOT NULL DEFAULT 1,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_redemptions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, company_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(code, company_id)
);
CREATE TABLE IF NOT EXISTS feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 rating INTEGER NOT NULL, question TEXT, comment TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_questions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 question TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_rewards (
 new_user_id INTEGER PRIMARY KEY, referrer_user_id INTEGER NOT NULL, rewarded_at TEXT NOT NULL
);
"""


def init() -> None:
    db.run_startup_migration(lambda con: con.executescript(SCHEMA))


init()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(FMT)


# ------------------------------------------------------------------ email on file
def clean_email(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    if not email:
        return None
    if len(email) > 160 or not EMAIL_RE.fullmatch(email):
        raise ValueError("That email address doesn't look right.")
    return email


def set_email(user_id: int, email: str | None) -> str | None:
    email = clean_email(email)
    with db._db() as con:
        con.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
    return email


# ------------------------------------------------------------------ personal notifications (+ email)
def notice(user_id: int, title: str, body: str, level: str = "info", days: int = 30) -> None:
    """A private message in the person's Notifications (the bell)."""
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime(FMT)
    with db._db() as con:
        con.execute("INSERT INTO broadcasts(title, body, level, created_by, created_at, expires_at, active, target_user_id) "
                    "VALUES (?,?,?,?,?,?,1,?)",
                    (title[:120], body[:2000], level if level in db.BROADCAST_LEVELS else "info", None, _now(), expires, user_id))


def _members(company_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT u.id, u.username, u.email FROM users u JOIN user_companies uc ON uc.user_id = u.id "
                           "WHERE uc.company_id = ? AND u.active = 1 AND u.role <> 'admin'", (company_id,)).fetchall()
    return [dict(r) for r in rows]


def _mail_async(address: str, subject: str, body: str) -> None:
    if address and notify.email_configured():
        threading.Thread(target=notify.send_email, args=(address, subject, body), daemon=True).start()


def tell_user(user_id: int, title: str, body: str, level: str = "info", email: bool = True) -> None:
    try:
        notice(user_id, title, body, level)
        if email:
            u = db.get_user(user_id)
            if u and u.get("email"):
                _mail_async(u["email"], f"Report Analyzer: {title}", f"{body}\n\n- Report Analyzer")
    except Exception:  # noqa: BLE001  (telling someone is never allowed to break the real action)
        log.exception("could not notify user %s", user_id)


def tell_company(company_id: int, title: str, body: str, level: str = "info", email: bool = True) -> int:
    n = 0
    for m in _members(company_id):
        tell_user(m["id"], title, body, level, email)
        n += 1
    return n


# ------------------------------------------------------------------ extending a plan
def extend_subscription(company_id: int, days: float, note: str = "Bonus days") -> dict:
    """Add days to the company's current plan. With no active plan, start a short Free-plan period."""
    now = dt.datetime.now(dt.timezone.utc)
    with db._db() as con:
        sub = db._active_subscription(con, company_id)
        if sub:
            new_exp = dt.datetime.strptime(sub["expires_at"], FMT) + dt.timedelta(days=days)
            con.execute("UPDATE subscriptions SET expires_at = ? WHERE id = ?", (new_exp.strftime(FMT), sub["id"]))
        else:
            new_exp = now + dt.timedelta(days=days)
            con.execute(
                "INSERT INTO subscriptions(company_id,plan,status,starts_at,expires_at,grant_source,granted_by,note,created_at) "
                "VALUES (?,?,'active',?,?, 'bonus',NULL,?,?)",
                (company_id, "free", now.strftime(FMT), new_exp.strftime(FMT), note[:300], _now()))
    return {"company_id": company_id, "expires_at": new_exp.strftime(FMT)}


# ------------------------------------------------------------------ referrals
def my_referral(user_id: int) -> dict:
    with db._db() as con:
        row = con.execute("SELECT referral_code FROM users WHERE id = ?", (user_id,)).fetchone()
        code = row["referral_code"] if row else None
        if not code:
            for _ in range(10):
                code = "RA-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
                if not con.execute("SELECT 1 FROM users WHERE referral_code = ?", (code,)).fetchone():
                    con.execute("UPDATE users SET referral_code = ? WHERE id = ?", (code, user_id))
                    break
        joined = con.execute("SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (user_id,)).fetchone()["n"]
        rewarded = con.execute("SELECT COUNT(*) AS n FROM referral_rewards WHERE referrer_user_id = ?", (user_id,)).fetchone()["n"]
    return {"code": code, "joined": int(joined), "rewarded": int(rewarded), "bonus_days": REFERRAL_BONUS_DAYS}


def referrer_for_code(code: str | None) -> int | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    with db._db() as con:
        row = con.execute("SELECT id FROM users WHERE referral_code = ? AND active = 1", (code,)).fetchone()
    return int(row["id"]) if row else None


def reward_referral_on_payment(company_id: int) -> None:
    """When a referred customer's FIRST payment is approved, both sides get bonus days (once)."""
    try:
        for m in _members(company_id):
            with db._db() as con:
                u = con.execute("SELECT referred_by FROM users WHERE id = ?", (m["id"],)).fetchone()
                ref = u["referred_by"] if u else None
                if not ref or con.execute("SELECT 1 FROM referral_rewards WHERE new_user_id = ?", (m["id"],)).fetchone():
                    continue
                con.execute("INSERT INTO referral_rewards(new_user_id, referrer_user_id, rewarded_at) VALUES (?,?,?)",
                            (m["id"], ref, _now()))
                rc = con.execute("SELECT company_id FROM user_companies WHERE user_id = ? LIMIT 1", (ref,)).fetchone()
            extend_subscription(company_id, REFERRAL_BONUS_DAYS, "Referral bonus")
            tell_user(m["id"], "🎁 Referral bonus", f"Thanks for joining through a friend. We added {REFERRAL_BONUS_DAYS:g} bonus day(s) to your plan.", "success")
            if rc:
                extend_subscription(int(rc["company_id"]), REFERRAL_BONUS_DAYS, "Referral bonus")
                tell_user(ref, "🎁 Your referral paid off", f"Someone you referred just subscribed. We added {REFERRAL_BONUS_DAYS:g} bonus day(s) to your plan. Thank you!", "success")
    except Exception:  # noqa: BLE001
        log.exception("referral reward failed")


# ------------------------------------------------------------------ coupons
def _code(c: str | None) -> str:
    return re.sub(r"[^A-Z0-9-]", "", (c or "").upper())[:32]


def create_coupon(kind: str, value: float, max_uses: int = 1, days_valid: int | None = 30,
                  code: str | None = None, note: str = "") -> dict:
    if kind not in ("days", "percent"):
        raise ValueError("Coupon type must be 'days' (free days) or 'percent' (discount on a payment).")
    value = float(value)
    if kind == "percent" and not (1 <= value <= 90):
        raise ValueError("A percent coupon must be between 1 and 90.")
    if kind == "days" and not (0.5 <= value <= 60):
        raise ValueError("A free-days coupon must be between 0.5 and 60 days.")
    max_uses = max(1, min(int(max_uses or 1), 10000))
    code = _code(code) or "RA-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    if len(code) < 4:
        raise ValueError("A coupon code needs at least 4 letters or digits.")
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=int(days_valid))).strftime(FMT) if days_valid else None
    try:
        with db._db() as con:
            con.execute("INSERT INTO coupons(code,kind,value,max_uses,used,expires_at,active,note,created_at) VALUES (?,?,?,?,0,?,1,?,?)",
                        (code, kind, value, max_uses, expires, (note or "")[:200], _now()))
    except db.IntegrityError as e:
        raise ValueError("That coupon code already exists.") from e
    return {"code": code, "kind": kind, "value": value, "max_uses": max_uses, "expires_at": expires}


def list_coupons() -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 200").fetchall()]


def disable_coupon(code: str) -> bool:
    with db._db() as con:
        row = con.execute("SELECT code FROM coupons WHERE code = ?", (_code(code),)).fetchone()
        if not row:
            return False
        con.execute("UPDATE coupons SET active = 0 WHERE code = ?", (_code(code),))
    return True


def _usable(con, code: str, company_id: int):
    c = con.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
    if not c or not c["active"] or (c["expires_at"] and c["expires_at"] < _now()):
        raise ValueError("That coupon code isn't valid or has expired.")
    if c["used"] >= c["max_uses"]:
        raise ValueError("That coupon has already been used up.")
    if con.execute("SELECT 1 FROM coupon_redemptions WHERE code = ? AND company_id = ?", (code, company_id)).fetchone():
        raise ValueError("Your company has already used that coupon.")
    return c


def _consume(con, c, company_id: int, user_id: int) -> None:
    cur = con.execute("UPDATE coupons SET used = used + 1 WHERE code = ? AND used < max_uses AND active = 1", (c["code"],))
    if cur.rowcount == 0:
        raise ValueError("That coupon has already been used up.")
    con.execute("INSERT INTO coupon_redemptions(code, company_id, user_id, created_at) VALUES (?,?,?,?)",
                (c["code"], company_id, user_id, _now()))


def redeem_days_coupon(code: str, company_id: int, user_id: int) -> dict:
    code = _code(code)
    with db._db() as con:
        c = _usable(con, code, company_id)
        if c["kind"] != "days":
            raise ValueError("That is a discount coupon. Enter it when you submit a payment.")
        _consume(con, c, company_id, user_id)
    out = extend_subscription(company_id, float(c["value"]), f"Coupon {code}")
    return {"days": float(c["value"]), "expires_at": out["expires_at"]}


def percent_discount(code: str, company_id: int, price_ngn: int) -> tuple[str, int]:
    """Check a discount coupon for a payment request WITHOUT using it. Returns (code, discount in naira)."""
    code = _code(code)
    with db._db() as con:
        c = _usable(con, code, company_id)
        if c["kind"] != "percent":
            raise ValueError("That is a free-days coupon. Redeem it on the Plan page instead.")
    return code, int(round(price_ngn * float(c["value"]) / 100))


def use_coupon(code: str, company_id: int, user_id: int) -> None:
    """Mark a coupon as used (called once the payment request was really created)."""
    code = _code(code)
    with db._db() as con:
        _consume(con, _usable(con, code, company_id), company_id, user_id)


# ------------------------------------------------------------------ feedback + saved questions
def add_feedback(user_id: int, company_id: int, rating: int, question: str = "", comment: str = "") -> None:
    if rating not in (1, -1):
        raise ValueError("Rating must be 👍 or 👎.")
    with db._db() as con:
        con.execute("INSERT INTO feedback(company_id,user_id,rating,question,comment,created_at) VALUES (?,?,?,?,?,?)",
                    (company_id, user_id, rating, db._enc_text((question or "")[:500]), db._enc_text((comment or "")[:1000]), _now()))


def list_feedback(limit: int = 100) -> dict:
    with db._db() as con:
        rows = con.execute("SELECT f.id, f.rating, f.question, f.comment, f.created_at, u.username, c.name AS company "
                           "FROM feedback f LEFT JOIN users u ON u.id = f.user_id LEFT JOIN companies c ON c.id = f.company_id "
                           "ORDER BY f.id DESC LIMIT ?", (int(limit),)).fetchall()
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime(FMT)
        up = con.execute("SELECT COUNT(*) AS n FROM feedback WHERE rating = 1 AND created_at > ?", (since,)).fetchone()["n"]
        down = con.execute("SELECT COUNT(*) AS n FROM feedback WHERE rating = -1 AND created_at > ?", (since,)).fetchone()["n"]
    items = []
    for r in rows:
        d = dict(r)
        d["question"], d["comment"] = db._dec_text(d["question"]) or "", db._dec_text(d["comment"]) or ""
        items.append(d)
    return {"up_30d": int(up), "down_30d": int(down), "items": items}


def save_question(user_id: int, company_id: int, question: str) -> dict:
    question = (question or "").strip()
    if not question or len(question) > 600:
        raise ValueError("Write a question (up to 600 characters).")
    with db._db() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM saved_questions WHERE user_id = ? AND company_id = ?", (user_id, company_id)).fetchone()["n"]
        if n >= 30:
            raise ValueError("You can save up to 30 questions. Delete one first.")
        for r in con.execute("SELECT id, question FROM saved_questions WHERE user_id = ? AND company_id = ?", (user_id, company_id)).fetchall():
            if (db._dec_text(r["question"]) or "").strip().lower() == question.lower():
                return {"id": r["id"], "question": question}
        cur = con.execute("INSERT INTO saved_questions(company_id,user_id,question,created_at) VALUES (?,?,?,?)",
                          (company_id, user_id, db._enc_text(question), _now()))
        return {"id": cur.lastrowid, "question": question}


def list_saved(user_id: int, company_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id, question FROM saved_questions WHERE user_id = ? AND company_id = ? ORDER BY id DESC",
                           (user_id, company_id)).fetchall()
    return [{"id": r["id"], "question": db._dec_text(r["question"]) or ""} for r in rows]


def delete_saved(user_id: int, saved_id: int) -> bool:
    with db._db() as con:
        cur = con.execute("DELETE FROM saved_questions WHERE id = ? AND user_id = ?", (saved_id, user_id))
        return cur.rowcount > 0


# ------------------------------------------------------------------ the owner's overview
def overview() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    month_start = now.strftime("%Y-%m-01 00:00:00")
    week_ago = (now - dt.timedelta(days=7)).strftime(FMT)
    with db._db() as con:
        customers = con.execute("SELECT COUNT(*) AS n FROM users WHERE role <> 'admin' AND active = 1").fetchone()["n"]
        companies = con.execute("SELECT id, name FROM companies ORDER BY id").fetchall()
        new7 = con.execute("SELECT COUNT(*) AS n FROM users WHERE role <> 'admin' AND created_at > ?", (week_ago,)).fetchone()["n"]
        pending_pay = con.execute("SELECT COUNT(*) AS n FROM payment_requests WHERE status = 'pending'").fetchone()["n"]
        pending_reg = con.execute("SELECT COUNT(*) AS n FROM registration_requests WHERE status = 'pending'").fetchone()["n"]
        paid = con.execute("SELECT amount_ngn, reviewed_at FROM payment_requests WHERE status = 'approved'").fetchall()
        used_month = con.execute("SELECT COUNT(*) AS n FROM monthly_usage WHERE month = ? AND status = 'used'", (db._month(),)).fetchone()["n"]
    income_month = sum(int(p["amount_ngn"]) for p in paid if (p["reviewed_at"] or "") >= month_start)
    by_month: dict = {}
    for p in paid:
        m = (p["reviewed_at"] or "")[:7]
        if m:
            by_month[m] = by_month.get(m, 0) + int(p["amount_ngn"])
    months = sorted(by_month)[-6:]
    plans: dict = {}
    near, expiring, in_grace = [], [], []
    soon = now + dt.timedelta(hours=72)
    for c in companies:
        try:
            st = db.get_plan_status(c["id"])
        except Exception:  # noqa: BLE001
            continue
        plans[st["plan"]] = plans.get(st["plan"], 0) + 1
        if st["monthly_limit"] and st["used"] / st["monthly_limit"] >= 0.8 and st["plan"] != "expired":
            near.append({"company": c["name"], "used": st["used"], "limit": st["monthly_limit"], "plan": st["plan"]})
        if st.get("in_grace"):
            in_grace.append({"company": c["name"], "plan": st["plan"], "grace_ends_at": st["grace_ends_at"]})
        elif st["plan"] not in ("expired",) and st["expires_at"]:
            try:
                if dt.datetime.strptime(st["expires_at"], FMT).replace(tzinfo=dt.timezone.utc) <= soon:
                    expiring.append({"company": c["name"], "plan": st["plan"], "expires_at": st["expires_at"]})
            except ValueError:
                pass
    fb = list_feedback(0)
    return {"customers": int(customers), "companies": len(companies), "new_customers_7d": int(new7),
            "pending_payments": int(pending_pay), "pending_registrations": int(pending_reg),
            "income_this_month_ngn": income_month, "income_total_ngn": sum(by_month.values()),
            "income_by_month": [{"month": m, "ngn": by_month[m]} for m in months],
            "analyses_this_month": int(used_month), "companies_by_plan": plans,
            "near_limit": sorted(near, key=lambda x: -x["used"] / max(x["limit"], 1))[:10],
            "expiring_soon": expiring[:10], "in_grace": in_grace[:10],
            "feedback_up_30d": fb["up_30d"], "feedback_down_30d": fb["down_30d"]}


# ------------------------------------------------------------------ expiry reminders (called by the scheduler)
def run_expiry_reminders() -> int:
    """72h and 24h before a paid plan ends, and once when it ends (grace starts). Each is sent once."""
    import enterprise_features as ef
    now = dt.datetime.now(dt.timezone.utc)
    sent = 0
    with db._db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT s.id, s.company_id, s.plan, s.expires_at, c.name AS company FROM subscriptions s JOIN companies c ON c.id = s.company_id "
            "WHERE s.plan NOT IN ('expired') AND s.expires_at > ? AND s.expires_at <= ? AND s.status IN ('active','expired')",
            ((now - dt.timedelta(hours=db.GRACE_HOURS)).strftime(FMT), (now + dt.timedelta(hours=72)).strftime(FMT))).fetchall()]
    for s in rows:
        exp = dt.datetime.strptime(s["expires_at"], FMT).replace(tzinfo=dt.timezone.utc)
        hours_left = (exp - now).total_seconds() / 3600
        paid = s["plan"] != "free"
        if hours_left > 24:
            key, title, level = f"expiry:{s['id']}:72h", "⏰ Your plan ends in about 3 days", "warning"
            body = f"Your {s['plan'].title()} plan ends on {s['expires_at']} UTC. Renew under Plan & Billing to keep working without a break."
        elif hours_left > 0:
            key, title, level = f"expiry:{s['id']}:24h", "⏰ Your plan ends within 24 hours", "warning"
            body = f"Your {s['plan'].title()} plan ends on {s['expires_at']} UTC. Renew under Plan & Billing so you are not interrupted."
        elif paid and db.GRACE_HOURS > 0:
            key, title, level = f"expiry:{s['id']}:grace", "Your plan has ended: grace period started", "warning"
            body = (f"Your {s['plan'].title()} plan has ended, but you can keep working for {db.GRACE_HOURS:g} more hours while you renew. "
                    "Please renew under Plan & Billing.")
        else:
            continue
        if ef.log_notification_once(s["company_id"], key):
            sent += tell_company(s["company_id"], title, body, level)
    return sent
