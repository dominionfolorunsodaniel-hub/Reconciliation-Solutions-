"""
web.py - dashboard + API (FastAPI).  Run:  python web.py   -> http://localhost:8000

Security: login sessions (HttpOnly cookie), optional two-factor (TOTP), roles (viewer < analyst < admin),
per-company access, login throttling, audit log, encrypted storage, AI usage limits.
First start: set DASHBOARD_USER / DASHBOARD_PASSWORD (10+ chars) to create the first admin.
"""
import asyncio
import io
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import anomaly
import apikeys
import assistant
import backup
import core
import db
import forecast
import enterprise_features as ef
import extras
import landing
import notify
import portal
import receipts
import recon
import report_pdf
import safety
import scheduler
import sharing
import statement
import tickets
import totp
import weekly

log = logging.getLogger("web")
INSIGHTS = os.getenv("AUTO_INSIGHTS", "1") != "0"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"  # set to 1 when served over HTTPS
REQUIRE_2FA_ADMINS = os.getenv("REQUIRE_2FA_ADMINS", "0") == "1"
LOGO_DIR = os.path.join(db.DATA_DIR, "logos")
os.makedirs(LOGO_DIR, exist_ok=True)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FAILS: dict = {}  # (username, ip) -> timestamps of failed logins
RESULTS: "OrderedDict[str, dict]" = OrderedDict()  # recent exports, kept in memory only
_tasks: set = set()
REGISTRATION_FAILS: dict = {}  # in-memory public signup throttling by IP
REGISTRATION_OK: dict = {}  # successful sign-up requests per visitor address (stops bots flooding the approval list)
REGISTRATION_OK_MAX_PER_IP, REGISTRATION_OK_MAX_TOTAL = 10, 60  # per hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.check_encryption()
    db.bootstrap_admin()
    tasks = scheduler.start()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Report Analyzer", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
    if request.url.path.startswith("/api/") or request.url.path.startswith("/share/"):
        resp.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/share/") or request.url.path.startswith("/api/shared/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return resp


def client_ip(request: Request) -> str:
    """Best guess at the visitor's address (behind Render's proxy). Only shown to the owner of the account; never used for security limits."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


# ------------------------------- auth helpers ------------------------------ #
def current_user_any(request: Request) -> dict:
    token = request.cookies.get("session")
    user = db.get_session_user(token) if token else None
    if not user:
        raise HTTPException(401, "Please log in.")
    core.current_user_id.set(user["id"])  # AI usage is counted against this user
    return user


def current_user(user: dict = Depends(current_user_any)) -> dict:
    if REQUIRE_2FA_ADMINS and user["role"] == "admin" and not user["totp_enabled"]:
        raise HTTPException(403, "2FA_SETUP_REQUIRED")
    return user


def need(user: dict, role: str) -> None:
    if db.ROLE_RANK[user["role"]] < db.ROLE_RANK[role]:
        raise HTTPException(403, "Your role doesn't allow that.")


def need_company(user: dict, company_id: int) -> None:
    if not db.can_access(user, company_id):
        raise HTTPException(403, "You don't have access to this company.")


def report_for(user: dict, report_id: int) -> dict:
    r = db.get_report(report_id)
    if not r:
        raise HTTPException(404, "Unknown report")
    need_company(user, r["company_id"])
    return r


def public(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "totp_enabled": user["totp_enabled"], "email": user.get("email"),
            "needs_2fa_setup": REQUIRE_2FA_ADMINS and user["role"] == "admin" and not user["totp_enabled"]}


def _keep(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def reserve_plan_usage(user: dict, company_id: int, operation: str):
    if user.get("role") == "admin":
        return None
    try:
        return db.reserve_monthly_usage(company_id, user["id"], operation)
    except core.LimitReached as e:
        raise HTTPException(429, str(e))


def finish_plan_usage(reservation_id, success: bool):
    if reservation_id is not None:
        company_id = None
        try:
            with db._db() as con:
                row = con.execute("SELECT company_id FROM monthly_usage WHERE id=?", (reservation_id,)).fetchone()
                company_id = row["company_id"] if row else None
        except Exception:
            pass
        db.finish_monthly_usage(reservation_id, success)
        if success and company_id is not None:
            try:
                ef.check_quota_warning(int(company_id))
            except Exception as e:
                log.warning("Quota warning could not be sent: %s", e)


# ---------------------------------- models ---------------------------------- #
class RegistrationIn(BaseModel):
    company_name: str
    username: str
    password: str
    telegram_id: int | None = None
    email: str | None = None
    referral_code: str | None = None
    website: str | None = None  # hidden honeypot field: real people leave it empty


class RegistrationDecisionIn(BaseModel):
    decision: str
    note: str = ""


class LoginIn(BaseModel):
    username: str
    password: str
    code: str | None = None


class CompanyIn(BaseModel):
    name: str


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "analyst"
    telegram_id: int | None = None
    company_ids: list[int] = []


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    alerts: bool | None = None
    password: str | None = None
    telegram_id: int | None = None
    company_ids: list[int] | None = None
    reset_2fa: bool | None = None


class AnalyzeIn(BaseModel):
    company_id: int
    report_id: int | None = None  # None = all reports of the company combined
    question: str
    prev_plan: dict | None = None


class StatementIn(BaseModel):
    company_id: int
    report_id: int | None = None
    report_ids: list[int] | None = None   # 2 or more = combine these accounts
    from_month: str | None = None   # "2026-02"
    to_month: str | None = None


class ReconIn(BaseModel):
    company_id: int
    report_a: int
    report_b: int
    instruction: str = ""


class ForecastIn(BaseModel):
    company_id: int
    report_id: int | None = None
    days: int = 7
    cash: float | None = None  # cash loaded per ATM, if the report has no balance column
    buffer: float = 0.15


class PaymentRequestIn(BaseModel):
    company_id: int
    plan: str
    transfer_reference: str
    coupon_code: str | None = None


class GrantPlanIn(BaseModel):
    company_id: int
    plan: str = "starter"
    days: float | None = None  # defaults to the selected plan's advertised term
    note: str = ""


class PaymentDecisionIn(BaseModel):
    decision: str
    note: str = ""


class CaseUpdateIn(BaseModel):
    status: str | None = None
    assigned_user_id: int | None = None
    comment: str = ""


class ScheduleIn(BaseModel):
    report_a: int
    report_b: int
    interval_hours: int = 24
    instruction: str = ""


class NotificationSettingsIn(BaseModel):
    emails: list[str] = []
    quota_threshold: int = 80


class DigestIn(BaseModel):
    question: str
    weekday: int  # 0=Mon ... 6=Sun, -1 = every day
    time: str  # "HH:MM"
    scope: str = "latest"


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


class BroadcastIn(BaseModel):
    title: str
    message: str
    level: str = "info"
    days: int | None = 14
    email: bool = False


class EmailIn(BaseModel):
    email: str | None = None


class CouponIn(BaseModel):
    kind: str = "percent"
    value: float
    max_uses: int = 1
    days_valid: int | None = 30
    code: str | None = None
    note: str = ""


class RedeemIn(BaseModel):
    company_id: int
    code: str


class FeedbackIn(BaseModel):
    company_id: int
    rating: int
    question: str = ""
    comment: str = ""


class SavedQuestionIn(BaseModel):
    question: str


class ReadIn(BaseModel):
    ids: list[int] | None = None


class ResetRequestIn(BaseModel):
    username: str


class ResetConfirmIn(BaseModel):
    username: str
    code: str
    new_password: str


# ------------------------------- login / session ----------------------------- #
@app.post("/api/register")
def register_customer(body: RegistrationIn, request: Request):
    """Public customer request. New accounts remain disabled until an admin approves."""
    ip = request.client.host if request.client else "?"
    now = time.time()
    if body.website:  # a robot filled the hidden field: look successful, create nothing
        return {"message": "Request submitted. An administrator must approve it before you can sign in.",
                "request_id": 0, "status": "pending"}
    attempts = [t for t in REGISTRATION_FAILS.get(ip, []) if now - t < 3600]
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many registration attempts. Try again in one hour.")
    visitor = client_ip(request) or ip
    ok_mine = [t for t in REGISTRATION_OK.get(visitor, []) if now - t < 3600]
    ok_all = sum(1 for v in REGISTRATION_OK.values() for t in v if now - t < 3600)
    if len(ok_mine) >= REGISTRATION_OK_MAX_PER_IP or ok_all >= REGISTRATION_OK_MAX_TOTAL:
        raise HTTPException(429, "We have received many requests from your connection. Please try again later or email us.")
    try:
        result = portal.submit_registration(body.company_name, body.username, body.password, body.telegram_id,
                                            body.email, body.referral_code)
    except ValueError as e:
        attempts.append(now)
        REGISTRATION_FAILS[ip] = attempts
        raise HTTPException(400, str(e))
    REGISTRATION_FAILS[ip] = attempts
    REGISTRATION_OK[visitor] = ok_mine + [now]
    return {"message": "Request submitted. An administrator must approve it before you can sign in.",
            "request_id": result["id"], "status": "pending"}


@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    key = (body.username.strip().lower(), request.client.host if request.client else "?")
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 900]
    if len(FAILS[key]) >= 5:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    # Render environment credentials are authoritative for web admin access.
    # Sync the configured admin before checking the database-backed customer login.
    env_admin_user = os.getenv("DASHBOARD_USER", "").strip()
    env_admin_password = os.getenv("DASHBOARD_PASSWORD", "")
    env_admin_match = bool(env_admin_user and env_admin_password and
                           secrets.compare_digest(body.username.strip(), env_admin_user) and
                           secrets.compare_digest(body.password, env_admin_password))
    user = None
    if env_admin_user and env_admin_password:
        configured_admin = await asyncio.to_thread(db.sync_env_admin)
        if env_admin_match and configured_admin:
            user = configured_admin
    if user is None:
        user = await asyncio.to_thread(db.verify_login, body.username, body.password)
        # Database-stored admin credentials must never override Render's configured credentials.
        if user and user["role"] == "admin" and not env_admin_match:
            user = None
    if not user:
        FAILS[key].append(now)
        db.audit(None, "login_failed", detail=body.username[:60])
        raise HTTPException(401, "Wrong username or password.")
    if user["totp_enabled"]:
        if not body.code:
            return JSONResponse({"detail": "Enter the 6-digit code from your authenticator app.", "need_code": True},
                                status_code=401)
        if not await asyncio.to_thread(db.verify_totp, user["id"], body.code):
            FAILS[key].append(now)
            db.audit(user, "login_failed", detail="wrong 2FA code")
            raise HTTPException(401, "Wrong or already-used 2-factor code.")
    FAILS.pop(key, None)
    ua, ip = request.headers.get("user-agent", ""), client_ip(request)
    token = db.create_session(user["id"], ua, ip)
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=COOKIE_SECURE,
                        max_age=db.SESSION_HOURS * 3600)
    db.audit(user, "login")
    await asyncio.to_thread(safety.note_login, user, ua, ip)
    return public(user)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        db.delete_session(token)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user_any)):
    return public(user)


@app.get("/api/health")
def health():
    """For uptime monitors (UptimeRobot etc.). Reveals nothing."""
    db.list_companies()
    return {"ok": True}


_STATIC = Path(__file__).parent / "static"
_PWA_HEADERS = {"Cache-Control": "public, max-age=3600"}


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return FileResponse(_STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers=_PWA_HEADERS)


@app.get("/sw.js")
def pwa_service_worker():
    # must be served from the site root so it can cover every page; never cached for long so updates arrive
    return FileResponse(_STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/offline.html")
def pwa_offline():
    return FileResponse(_STATIC / "offline.html", media_type="text/html", headers=_PWA_HEADERS)


@app.get("/static/icon-192.png")
def pwa_icon_192():
    return FileResponse(_STATIC / "icon-192.png", media_type="image/png", headers=_PWA_HEADERS)


@app.get("/static/icon-512.png")
def pwa_icon_512():
    return FileResponse(_STATIC / "icon-512.png", media_type="image/png", headers=_PWA_HEADERS)


@app.get("/static/icon-maskable-512.png")
def pwa_icon_maskable():
    return FileResponse(_STATIC / "icon-maskable-512.png", media_type="image/png", headers=_PWA_HEADERS)


@app.get("/static/apple-touch-icon.png")
def pwa_apple_icon():
    return FileResponse(_STATIC / "apple-touch-icon.png", media_type="image/png", headers=_PWA_HEADERS)


@app.get("/static/login-character.png")
def login_character():
    return FileResponse(Path(__file__).parent / "static" / "login-character.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def public_base(request: Request) -> str:
    """The site's public address (for link previews and the sitemap). Set PUBLIC_URL to be sure; otherwise taken from the request."""
    fixed = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if fixed.startswith("http"):
        return fixed
    proto = (request.headers.get("x-forwarded-proto", "") or request.url.scheme).split(",")[0].strip() or "https"
    host = (request.headers.get("x-forwarded-host", "") or request.headers.get("host", "")).split(",")[0].strip()
    return f"{proto}://{host}" if host else str(request.base_url).rstrip("/")


@app.get("/")
def index(request: Request):
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8").replace("__BASE__", public_base(request))
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/robots.txt")
def robots_txt(request: Request):
    body = f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /share/\nSitemap: {public_base(request)}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{public_base(request)}/</loc></url>\n</urlset>\n")
    return Response(content=body, media_type="application/xml")


@app.get("/api/public/sample-statement")
def public_sample_statement():
    """A ready-made example of the bank statement check, built from invented data. No sign-in needed."""
    return landing.sample_result()


class ContactIn(BaseModel):
    name: str
    email: str
    company: str | None = None
    message: str
    website: str | None = None  # hidden honeypot field


@app.post("/api/public/contact")
def public_contact(body: ContactIn, request: Request):
    try:
        return landing.submit_contact(body.name, body.email, body.company or "", body.message, body.website or "",
                                      client_ip(request) or "?")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/admin/contact-messages")
def admin_contact_messages(user: dict = Depends(current_user)):
    need(user, "admin")
    return landing.list_contacts()


@app.post("/api/admin/contact-messages/{contact_id}/handled")
def admin_contact_handled(contact_id: int, user: dict = Depends(current_user)):
    need(user, "admin")
    landing.mark_handled(contact_id, True)
    return {"ok": True}


# ----------------------------------- two-factor ------------------------------- #
@app.post("/api/2fa/setup")
def twofa_setup(user: dict = Depends(current_user_any)):
    if user["totp_enabled"]:
        raise HTTPException(400, "Two-factor is already on.")
    secret = totp.new_secret()
    db.set_totp(user["id"], secret, enabled=False)
    return {"secret": secret, "uri": totp.uri(user["username"], secret)}


@app.post("/api/2fa/enable")
def twofa_enable(body: CodeIn, user: dict = Depends(current_user_any)):
    secret = db.get_totp_secret(user["id"])
    if not secret:
        raise HTTPException(400, "Start the setup first.")
    if not db.verify_totp(user["id"], body.code, secret):
        raise HTTPException(400, "That code is wrong. Check the code in your app (and your phone's clock).")
    db.set_totp(user["id"], secret, enabled=True)
    db.audit(user, "2fa_enabled")
    return {"ok": True}


@app.post("/api/2fa/disable")
def twofa_disable(body: PasswordIn, user: dict = Depends(current_user_any)):
    if not db.verify_login(user["username"], body.password):
        raise HTTPException(400, "Wrong password.")
    if REQUIRE_2FA_ADMINS and user["role"] == "admin":
        raise HTTPException(400, "Admins must keep two-factor on.")
    db.set_totp(user["id"], None, enabled=False)
    db.audit(user, "2fa_disabled")
    return {"ok": True}


# ------------------------------ companies & reports -------------------------- #
@app.get("/api/companies")
def companies(user: dict = Depends(current_user)):
    return db.list_companies(user)


@app.post("/api/companies")
def create_company(body: CompanyIn, user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        name = body.name.strip()
        if not name:
            raise ValueError("Company name can't be empty.")
        existing = next((c for c in db.list_companies() if c["name"].casefold() == name.casefold()), None)
        if existing:
            raise ValueError("That company already exists. Use its existing record or choose a distinct customer name.")
        c = db.get_or_create_company(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "create_company", c["id"], c["name"])
    return c


@app.get("/api/companies/{company_id}/reports")
def reports(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return db.list_reports(company_id)


@app.get("/api/companies/{company_id}/history")
def history(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return db.list_queries(company_id, 15)


@app.post("/api/companies/{company_id}/reports")
async def upload(company_id: int, file: UploadFile = File(...), user: dict = Depends(current_user)):
    need(user, "analyst")
    need_company(user, company_id)
    name = file.filename or "report.csv"
    if "." in name and name.lower().rsplit(".", 1)[-1] not in core.ALLOWED_EXT:
        raise HTTPException(400, "Supported files: " + core.FILE_TYPES_TEXT)
    data = await file.read()
    if len(data) > core.MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"File too large (limit {core.MAX_FILE_MB} MB)")
    try:
        df, notes, _ = await asyncio.to_thread(core.load_report, data, name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read that file: {e}")
    # Reading a new upload is not charged as one of the plan's monthly checks (only real questions, comparisons and
    # statement checks are). The daily AI limit still protects against abuse.
    desc = await core.describe_report(df, name)
    rid = await asyncio.to_thread(db.save_upload, company_id, name, data, df, user["id"])
    db.save_description(rid, desc)
    db.audit(user, "upload", company_id, f"#{rid} {name} ({len(df)} rows)")

    push = anomaly.push_text(db.get_company(company_id)["name"], name, desc.get("alerts", []))
    if push:
        _keep(asyncio.create_task(asyncio.to_thread(notify.alert_company, company_id, push, user["id"])))
    return {"id": rid, "rows": len(df), "columns": list(df.columns), "notes": notes, "description": desc}


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int, user: dict = Depends(current_user)):
    need(user, "admin")
    r = report_for(user, report_id)
    db.delete_report(report_id)
    db.audit(user, "delete_report", r["company_id"], f"#{report_id} {r['filename']}")
    return {"deleted": report_id}


@app.get("/api/reports/{report_id}/preview")
def preview(report_id: int, user: dict = Depends(current_user)):
    report_for(user, report_id)
    df = db.get_frame(report_id)
    return {"schema": core.describe_schema(df), "columns": list(df.columns),
            "rows": json.loads(df.head(15).to_json(orient="records", date_format="iso"))}


@app.get("/api/reports/{report_id}/describe")
async def describe(report_id: int, user: dict = Depends(current_user)):
    r = report_for(user, report_id)
    desc = db.get_description(report_id)
    if desc is None:
        df = await asyncio.to_thread(db.get_frame, report_id)
        desc = await core.describe_report(df, r["filename"])
        db.save_description(report_id, desc)
    return desc


# ----------------------------------- analysis -------------------------------- #
def _store_result(data: bytes, user: dict, company_id: int, pdf: dict | None = None, share: dict | None = None, ctx: str | None = None) -> str:
    rid = secrets.token_urlsafe(8)
    RESULTS[rid] = {"data": data, "user": user["id"], "company": company_id, "pdf": pdf, "share": share, "ctx": ctx}
    while len(RESULTS) > 50:
        RESULTS.popitem(last=False)
    return rid


@app.post("/api/analyze")
async def analyze(body: AnalyzeIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    reservation = reserve_plan_usage(user, body.company_id, "analysis_question")
    try:
        df = await asyncio.to_thread(db.resolve_frame, body.company_id, body.report_id)
        out = await core.analyze(df, body.question, body.prev_plan, INSIGHTS)
    except Exception as e:  # noqa: BLE001
        finish_plan_usage(reservation, False)
        raise HTTPException(400, str(e))
    if out.get("clarify"):
        finish_plan_usage(reservation, False)
        return {"clarify": out["clarify"]}
    finish_plan_usage(reservation, True)

    res = out["res"]
    db.log_query(body.company_id, body.report_id, body.question, out["plan"], user["id"])
    db.audit(user, "analyze", body.company_id, body.question[:200])
    xlsx = await asyncio.to_thread(core.make_excel, res, out["filtered"], out["plan"], out["insight"])
    chart_png = await asyncio.to_thread(core.make_chart_png, res, out["keys"], out["metric"],
                                        out["plan"].get("chart", "bar"), out["title"])
    pdf = {"title": out["title"], "question": body.question, "insight": out["insight"], "table": res.head(40),
           "chart": chart_png.getvalue() if chart_png else None, "total_rows": len(res)}
    response = {
        "title": out["title"],
        "columns": list(res.columns),
        "rows": json.loads(res.head(500).to_json(orient="records", date_format="iso")),
        "total_rows": len(res),
        "chart": core.chart_payload(res, out["keys"], out["metric"], out["plan"].get("chart", "bar")),
        "insight": out["insight"],
        "plan": out["plan"],
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id, pdf),
    }
    return response


def _statement_items(user: dict, body: "StatementIn") -> list[tuple[str, "pd.DataFrame"]]:
    """One account, all of the company's reports, or several chosen accounts to be added together."""
    ids = list(dict.fromkeys(body.report_ids or []))
    if len(ids) >= 2:
        if len(ids) > 6:
            raise ValueError("Combine at most 6 accounts at a time.")
        items = []
        for rid in ids:
            r = db.get_report(rid)
            if not r or r["company_id"] != body.company_id:
                raise ValueError("One of the chosen reports doesn't belong to this company.")
            f = db.get_frame(rid)
            f.attrs["filename"] = r["filename"]
            items.append((f"#{rid} {r['filename']}", f))
        return items
    one = ids[0] if ids else body.report_id
    f = db.resolve_frame(body.company_id, one)
    if one:
        rep = db.get_report(one)
        if rep:
            f.attrs["filename"] = rep["filename"]
    return [("", f)]


@app.post("/api/statement-score")
async def statement_score(body: StatementIn, user: dict = Depends(current_user)):
    """Monthly income / spending / what is left, a loan-readiness score, an authenticity check, unusual patterns and spending categories."""
    need_company(user, body.company_id)
    month_ok = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
    for m in (body.from_month, body.to_month):
        if m and not month_ok.match(m):
            raise HTTPException(400, "Months must look like 2026-02.")
    reservation = reserve_plan_usage(user, body.company_id, "statement_score")
    try:
        items = await asyncio.to_thread(_statement_items, user, body)
        r = await asyncio.to_thread(statement.build_many, items, body.from_month or None, body.to_month or None)
    except Exception as e:  # noqa: BLE001
        finish_plan_usage(reservation, False)
        raise HTTPException(400, str(e))
    finish_plan_usage(reservation, True)

    text = statement.summary_text(r)
    simple = None
    if INSIGHTS:
        simple = await core.summarize("Explain this bank statement result and loan-readiness score in simple words", r["table"],
                                      text, statement.STATEMENT_PROMPT)
        if simple:
            bad = core.unverified_figures(simple, text + " " + r["table"].to_string())
            if bad:
                simple += "\n\nPlease double-check these figures, I could not match them directly: " + ", ".join(bad) + "."
    full = ((simple + "\n\n") if simple else "") + text
    db.audit(user, "statement_score", body.company_id,
             f"report={body.report_id or body.report_ids}; score={r['score']}; months={r['totals']['months']}; balance_check={r['integrity']['status']}; flags={len(r['flags'])}")
    xlsx = await asyncio.to_thread(statement.excel, r)
    png = await asyncio.to_thread(statement.chart_png, r["table"])
    pdf = {"title": f"Statement check: {r['verdict']} ({r['score']}/100)", "question": f"Period {r['period']}", "insight": full,
           "table": r["table"], "chart": png, "total_rows": len(r["table"])}
    t = r["table"]
    response = {
        "score": r["score"], "verdict": r["verdict"], "eligibility": r["eligibility"], "factors": r["factors"],
        "strengths": r["strengths"], "concerns": r["concerns"], "advice": r["advice"], "loan_guide": r["loan_guide"],
        "totals": r["totals"], "disclaimer": r["disclaimer"], "period": r["period"], "months_available": r["months_available"],
        "notes": r["notes"], "simple": simple, "text": full,
        "integrity": r["integrity"], "flags": r["flags"], "categories": statement.categories_json(r.get("categories")),
        "forecast": r.get("forecast"),
        "accounts": r.get("accounts", []),
        "columns": list(t.columns), "rows": json.loads(t.to_json(orient="records")),
        "chart": {"labels": t["Month"].tolist(), "income": t["Income (credits)"].tolist(),
                  "expenses": t["Expenses (debits)"].tolist(), "left": t["Left over (income - expenses)"].tolist()},
    }
    share = sharing.safe_payload(response, (db.get_company(body.company_id) or {}).get("name", ""))
    share["simple"] = simple
    ctx = full + "\n\nMONTH BY MONTH:\n" + r["table"].to_string(index=False)
    cats = r.get("categories") or {}
    if cats.get("spending"):
        ctx += "\n\nSPENDING BY CATEGORY:\n" + "\n".join(f"{c['category']}: {c['amount']:,.2f} ({c['share_pct']}%)" for c in cats["spending"][:12])
    if cats.get("income"):
        ctx += "\n\nMONEY IN BY CATEGORY:\n" + "\n".join(f"{c['category']}: {c['amount']:,.2f} ({c['share_pct']}%)" for c in cats["income"][:12])
    fc = r.get("forecast") or {}
    if fc.get("available"):
        ctx += "\n\nFORECAST (estimate):\n" + pd.DataFrame(fc["rows"]).to_string(index=False) + "\n" + fc["note"]
    response["result_id"] = _store_result(xlsx.getvalue(), user, body.company_id, pdf, share, ctx)
    return response


@app.post("/api/statement-batch")
async def statement_batch(company_id: int = Form(...), files: list[UploadFile] = File(...), user: dict = Depends(current_user)):
    """For lenders: score many applicants' statements at once and rank them. Files are read and thrown away, never stored."""
    need(user, "analyst")
    need_company(user, company_id)
    if not files:
        raise HTTPException(400, "Choose at least one statement.")
    if len(files) > 25:
        raise HTTPException(400, "Up to 25 statements at a time.")
    rows, total, stopped = [], 0, False
    for f in files:
        name = f.filename or "statement"
        label = name.rsplit(".", 1)[0][:80] or name
        if stopped:
            rows.append({"applicant": label, "error": "Skipped: your plan's monthly checks ran out"})
            continue
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        if ext and ext not in core.ALLOWED_EXT:
            rows.append({"applicant": label, "error": "File type not supported"})
            continue
        data = await f.read()
        total += len(data)
        if len(data) > core.MAX_FILE_MB * 1024 * 1024 or total > 80 * 1024 * 1024:
            rows.append({"applicant": label, "error": f"File too large (limit {core.MAX_FILE_MB} MB each)"})
            continue
        try:
            reservation = reserve_plan_usage(user, company_id, "statement_score")
        except HTTPException:
            stopped = True
            rows.append({"applicant": label, "error": "Skipped: your plan's monthly checks ran out"})
            continue

        def work(data=data, name=name, label=label):
            df, _, _ = core.load_report(data, name)
            df.attrs["filename"] = name
            return statement.score_one(label, df)
        try:
            row = await asyncio.to_thread(work)
        except Exception as e:  # noqa: BLE001
            row = {"applicant": label, "error": str(e)[:200]}
        finish_plan_usage(reservation, "error" not in row)
        rows.append(row)
    table = statement.batch_table(rows)
    scored = [r for r in rows if "error" not in r]
    db.audit(user, "statement_batch", company_id, f"files={len(files)}; scored={len(scored)}")
    xlsx = await asyncio.to_thread(statement.batch_excel, rows)
    insight = (f"{len(scored)} of {len(rows)} statements scored. " +
               (f"Highest {max(r['score'] for r in scored)}, lowest {min(r['score'] for r in scored)}. " if scored else "") +
               statement.DISCLAIMER)
    pdf = {"title": "Statement ranking", "question": f"{len(rows)} statements", "insight": insight,
           "table": table.drop(columns=[c for c in ("Main concern", "Period") if c in table.columns]), "chart": None, "total_rows": len(table)}
    return {"columns": list(table.columns), "rows": json.loads(table.to_json(orient="records")), "scored": len(scored),
            "failed": len(rows) - len(scored), "summary": insight, "result_id": _store_result(xlsx.getvalue(), user, company_id, pdf)}


# ------------------------------- share links ------------------------------- #
SHARE_HITS: dict[str, list[float]] = {}


def _share_guard(request: Request) -> None:
    ip, now = client_ip(request) or "?", time.time()
    hits = [t for t in SHARE_HITS.get(ip, []) if now - t < 300]
    if len(hits) >= 60:
        raise HTTPException(429, "Too many requests. Please wait a few minutes.")
    hits.append(now)
    SHARE_HITS[ip] = hits
    if len(SHARE_HITS) > 5000:
        SHARE_HITS.clear()


class ShareIn(BaseModel):
    result_id: str
    days: int = 7


@app.post("/api/shares")
def create_share(body: ShareIn, user: dict = Depends(current_user)):
    item = _owned_result(body.result_id, user)
    payload = item.get("share")
    if not payload:
        raise HTTPException(400, "Only Statement Score results can be shared.")
    try:
        token, meta = sharing.create(user["id"], item["company"], f"Statement check {payload['score']}/100 - {payload['period']}", payload, body.days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "share_create", item["company"], f"expires={meta['expires_at']}")
    return {"url_path": f"/share/{token}", **meta}


@app.get("/api/shares")
def my_shares(user: dict = Depends(current_user)):
    return sharing.list_mine(user["id"])


@app.delete("/api/shares/{share_id}")
def revoke_share(share_id: int, user: dict = Depends(current_user)):
    if not sharing.revoke(user["id"], share_id, user["role"] == "admin"):
        raise HTTPException(404, "Link not found.")
    db.audit(user, "share_revoke", None, f"id={share_id}")
    return {"ok": True}


@app.get("/share/{token}")
def share_page(token: str, request: Request):
    _share_guard(request)
    return FileResponse(Path(__file__).parent / "static" / "share.html", headers={"Cache-Control": "no-store"})


@app.get("/api/shared/{token}")
def shared_data(token: str, request: Request):
    _share_guard(request)
    data = sharing.get_public(token)
    if not data:
        raise HTTPException(404, "This link has expired or was switched off.")
    data.pop("_company_id", None)
    return data


@app.get("/api/shared/{token}/pdf")
def shared_pdf(token: str, request: Request):
    _share_guard(request)
    d = sharing.get_public(token, count_view=False)
    if not d:
        raise HTTPException(404, "This link has expired or was switched off.")
    table = pd.DataFrame(d["rows"], columns=d["columns"])
    png = statement.chart_png(table)
    good = "; ".join(d.get("strengths") or []) or "none noted"
    worry = "; ".join(d.get("concerns") or []) or "none noted"
    text = (f"Loan readiness {d['score']} out of 100 - {d['verdict']}. {d['eligibility']}\n\nGood signs: {good}\nConcerns: {worry}\n\n"
            f"Authenticity check: {(d.get('integrity') or {}).get('message') or 'not available'}\n\n{d['disclaimer']}")
    pdf = report_pdf.build_pdf(d.get("company") or "Report Analyzer", f"Statement check: {d['verdict']} ({d['score']}/100)",
                               f"Period {d['period']}", text, table, png, db.get_branding(d["_company_id"]), len(table))
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": 'attachment; filename="statement-check.pdf"', "X-Robots-Tag": "noindex"})


# ------------------------------- ask about this ------------------------------- #
class ExplainIn(BaseModel):
    kind: str                       # "result" (a statement or reconciliation result), "cases" or "help"
    question: str
    company_id: int | None = None
    result_id: str | None = None


EXPLAIN_HITS: dict[int, list[float]] = {}


def _cases_context(company_id: int) -> str:
    rows = ef.list_cases(company_id, "all", 60)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [f"Discrepancy cases for this company (showing up to 60 newest/open): " + ", ".join(f"{k}: {v}" for k, v in counts.items())]
    for r in rows:
        d = json.dumps(r.get("details"), default=str)[:260]
        lines.append(f"#{r['id']} status={r['status']} created={r.get('created_at')} assigned={r.get('assigned_username') or '-'} details={d}")
    return "\n".join(lines)


@app.post("/api/explain")
async def explain(body: ExplainIn, user: dict = Depends(current_user_any)):
    """Plain-language answers about a result, the discrepancy cases, or how to use the website. Not charged as a plan check."""
    q = (body.question or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "Type your question first.")
    q = q[:600]
    now = time.time()
    hits = [t for t in EXPLAIN_HITS.get(user["id"], []) if now - t < 3600]
    if len(hits) >= 40:
        raise HTTPException(429, "You have asked a lot of questions this hour. Please try again a little later.")
    hits.append(now)
    EXPLAIN_HITS[user["id"]] = hits
    if body.kind == "help":
        ctx = assistant.HELP_TEXT
    elif body.kind == "cases":
        if body.company_id is None:
            raise HTTPException(400, "Choose a company first.")
        need_company(user, body.company_id)
        ctx = await asyncio.to_thread(_cases_context, body.company_id)
    elif body.kind == "result":
        item = _owned_result(body.result_id or "", user)
        ctx = item.get("ctx")
        if not ctx:
            raise HTTPException(400, "Run it again, then ask your question about the new result.")
    else:
        raise HTTPException(400, "Unknown kind of question.")
    try:
        out = await assistant.answer(body.kind, q, ctx)
    except core.LimitReached as e:
        raise HTTPException(429, str(e))
    db.audit(user, "explain", body.company_id, f"{body.kind}: {q[:120]}")
    return out


# ------------------------------- support tickets ------------------------------- #
class TicketIn(BaseModel):
    company_id: int
    subject: str
    body: str


class TicketReplyIn(BaseModel):
    body: str


@app.get("/api/tickets")
def my_tickets(all: bool = False, status: str | None = None, user: dict = Depends(current_user_any)):
    if all:
        need(user, "admin")
        return tickets.list_all(status)
    return tickets.list_mine(user["id"])


@app.post("/api/tickets")
def new_ticket(body: TicketIn, user: dict = Depends(current_user_any)):
    need_company(user, body.company_id)
    try:
        out = tickets.create(user, body.company_id, body.subject, body.body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "ticket_create", body.company_id, f"#{out['id']}")
    return out


@app.get("/api/tickets/{ticket_id}")
def read_ticket(ticket_id: int, user: dict = Depends(current_user_any)):
    t = tickets.get(ticket_id, user)
    if not t:
        raise HTTPException(404, "Conversation not found.")
    return t


@app.post("/api/tickets/{ticket_id}/reply")
def reply_ticket(ticket_id: int, body: TicketReplyIn, user: dict = Depends(current_user_any)):
    try:
        out = tickets.reply(ticket_id, user, body.body)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "ticket_reply", None, f"#{ticket_id}")
    return out


@app.post("/api/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int, user: dict = Depends(current_user_any)):
    if not tickets.close(ticket_id, user):
        raise HTTPException(404, "Conversation not found.")
    db.audit(user, "ticket_close", None, f"#{ticket_id}")
    return {"ok": True}


# ----------------------------------- lender API ----------------------------------- #
class ApiKeyIn(BaseModel):
    company_id: int
    label: str = ""


API_HITS: dict[int, list[float]] = {}


@app.get("/api/api-keys")
def list_api_keys(company_id: int, user: dict = Depends(current_user)):
    need(user, "analyst")
    need_company(user, company_id)
    return apikeys.list_for_company(company_id)


@app.post("/api/api-keys")
def create_api_key(body: ApiKeyIn, user: dict = Depends(current_user)):
    need(user, "analyst")
    need_company(user, body.company_id)
    try:
        key, meta = apikeys.create(user["id"], body.company_id, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "api_key_create", body.company_id, f"{meta['prefix']}... {meta['label']}")
    return {"key": key, **meta, "note": "Copy this key now. For your safety it is never shown again."}


@app.delete("/api/api-keys/{key_id}")
def revoke_api_key(key_id: int, company_id: int, user: dict = Depends(current_user)):
    need(user, "analyst")
    need_company(user, company_id)
    if not apikeys.revoke(key_id, company_id):
        raise HTTPException(404, "Key not found.")
    db.audit(user, "api_key_revoke", company_id, f"id={key_id}")
    return {"ok": True}


@app.post("/api/v1/statement-score")
async def api_statement_score(request: Request, file: UploadFile = File(...)):
    """For a lender's own system: send one statement with the header X-API-Key, get the score back as JSON."""
    info = apikeys.verify(request.headers.get("x-api-key", "").strip())
    if not info:
        raise HTTPException(401, "Missing or invalid API key.")
    owner = db.get_user(info["user_id"])
    if not owner or not owner["active"]:
        raise HTTPException(401, "This key's owner is no longer active.")
    now = time.time()
    hits = [t for t in API_HITS.get(info["id"], []) if now - t < 60]
    if len(hits) >= 30:
        raise HTTPException(429, "Too many requests. Limit is 30 per minute per key.")
    hits.append(now)
    API_HITS[info["id"]] = hits
    name = file.filename or "statement"
    if "." in name and name.lower().rsplit(".", 1)[-1] not in core.ALLOWED_EXT:
        raise HTTPException(400, "Supported files: " + core.FILE_TYPES_TEXT)
    data = await file.read()
    if len(data) > core.MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"File too large (limit {core.MAX_FILE_MB} MB)")
    charged = {"id": owner["id"], "role": "analyst"}      # API calls always spend the company's plan checks
    reservation = reserve_plan_usage(charged, info["company_id"], "statement_score")
    try:
        df, _, _ = await asyncio.to_thread(core.load_report, data, name)
        df.attrs["filename"] = name
        r = await asyncio.to_thread(statement.build, df)
    except Exception as e:  # noqa: BLE001
        finish_plan_usage(reservation, False)
        raise HTTPException(400, str(e))
    finish_plan_usage(reservation, True)
    db.audit(owner, "api_statement_score", info["company_id"], f"key={info['prefix']}; score={r['score']}", source="api")
    g = r["loan_guide"]
    cats = (r.get("categories") or {})
    return {
        "score": r["score"], "verdict": r["verdict"], "eligibility": r["eligibility"], "period": r["period"], "months": r["totals"]["months"],
        "totals": r["totals"], "loan_guide": g, "balance_check": {"status": r["integrity"]["status"], "message": r["integrity"]["message"]},
        "flags": [{"severity": f["severity"], "title": f["title"], "detail": f["detail"]} for f in r["flags"]],
        "strengths": r["strengths"], "concerns": r["concerns"],
        "monthly": json.loads(r["table"].to_json(orient="records")),
        "top_spending": cats.get("spending", [])[:5], "top_income": cats.get("income", [])[:5],
        "next_step": statement.next_step(r["verdict"], r["integrity"]["status"], sum(f["severity"] == "high" for f in r["flags"])),
        "disclaimer": r["disclaimer"],
    }


# --------------------------- account security & data --------------------------- #
@app.get("/api/me/sessions")
def my_sessions(request: Request, user: dict = Depends(current_user_any)):
    return safety.list_sessions(user["id"], request.cookies.get("session"))


@app.delete("/api/me/sessions/{session_id}")
def end_session(session_id: str, request: Request, user: dict = Depends(current_user_any)):
    if not safety.revoke_session(user["id"], session_id, request.cookies.get("session")):
        raise HTTPException(404, "That device is not signed in any more.")
    db.audit(user, "session_revoke", None, "one device signed out")
    return {"ok": True}


@app.post("/api/me/sessions/revoke-others")
def end_other_sessions(request: Request, user: dict = Depends(current_user_any)):
    n = safety.revoke_other_sessions(user["id"], request.cookies.get("session"))
    db.audit(user, "session_revoke_others", None, f"{n} device(s) signed out")
    return {"signed_out": n}


EXPORT_LAST: dict[int, float] = {}


@app.get("/api/me/export")
def export_my_data(user: dict = Depends(current_user_any)):
    now = time.time()
    if now - EXPORT_LAST.get(user["id"], 0) < 60:
        raise HTTPException(429, "Please wait a minute before downloading again.")
    EXPORT_LAST[user["id"]] = now
    data = safety.export_zip(user)
    db.audit(user, "data_export", None, f"{len(data)} bytes")
    return StreamingResponse(iter([data]), media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="my-report-analyzer-data.zip"'})


class DeleteReqIn(BaseModel):
    password: str
    note: str = ""


@app.get("/api/me/deletion-request")
def my_deletion_request(user: dict = Depends(current_user_any)):
    return {"pending": safety.my_request(user["id"])}


@app.post("/api/me/deletion-request")
async def ask_deletion(body: DeleteReqIn, user: dict = Depends(current_user_any)):
    if user["role"] == "admin":
        raise HTTPException(400, "The owner account cannot be deleted from here.")
    ok = await asyncio.to_thread(db.verify_login, user["username"], body.password)
    if not ok:
        db.audit(user, "deletion_request_bad_password")
        raise HTTPException(401, "That password is not right.")
    try:
        out = await asyncio.to_thread(safety.request_deletion, user, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "deletion_request", None, "requested")
    return {"pending": out}


@app.delete("/api/me/deletion-request")
def cancel_deletion(user: dict = Depends(current_user_any)):
    if not safety.cancel_request(user["id"]):
        raise HTTPException(404, "You have no open request.")
    db.audit(user, "deletion_request_cancel")
    return {"ok": True}


@app.get("/api/admin/deletion-requests")
def admin_deletion_requests(user: dict = Depends(current_user)):
    need(user, "admin")
    return safety.list_requests()


@app.post("/api/admin/deletion-requests/{request_id}/done")
def admin_deletion_done(request_id: int, user: dict = Depends(current_user)):
    need(user, "admin")
    if not safety.mark_done(request_id):
        raise HTTPException(404, "Request not found or already handled.")
    db.audit(user, "deletion_request_done", None, f"id={request_id}")
    return {"ok": True}


class WeeklyIn(BaseModel):
    enabled: bool


@app.get("/api/me/weekly-email")
def weekly_get(user: dict = Depends(current_user_any)):
    return {**weekly.get_pref(user["id"]), "email_ready": notify.email_configured()}


@app.post("/api/me/weekly-email")
def weekly_set(body: WeeklyIn, user: dict = Depends(current_user_any)):
    try:
        return {**weekly.set_pref(user["id"], body.enabled), "email_ready": notify.email_configured()}
    except ValueError as e:
        raise HTTPException(400, str(e))


WEEKLY_TEST: dict[int, float] = {}


@app.post("/api/me/weekly-email/test")
async def weekly_test(user: dict = Depends(current_user_any)):
    if not notify.email_configured():
        raise HTTPException(400, "Email sending is not set up on this website yet.")
    now = time.time()
    if now - WEEKLY_TEST.get(user["id"], 0) < 300:
        raise HTTPException(429, "Please wait a few minutes before sending another sample.")
    WEEKLY_TEST[user["id"]] = now
    u = db.get_user(user["id"]) or {}
    if not u.get("email"):
        raise HTTPException(400, "Add your email address first.")
    subject, body = await asyncio.to_thread(weekly.build_report, u)
    ok = await asyncio.to_thread(notify.send_email, u["email"], subject, body)
    if not ok:
        raise HTTPException(502, "The email could not be sent right now. Please try again later.")
    return {"sent_to": u["email"]}


@app.post("/api/reconcile")
async def reconcile(body: ReconIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    ra, rb = report_for(user, body.report_a), report_for(user, body.report_b)
    if ra["company_id"] != body.company_id or rb["company_id"] != body.company_id:
        raise HTTPException(400, "Both reports must belong to the selected company.")
    reservation = reserve_plan_usage(user, body.company_id, "reconciliation")
    try:
        a = await asyncio.to_thread(db.get_frame, body.report_a)
        b = await asyncio.to_thread(db.get_frame, body.report_b)
        out = await recon.reconcile(a, b, body.instruction)
    except Exception as e:  # noqa: BLE001
        finish_plan_usage(reservation, False)
        raise HTTPException(400, str(e))
    if out.get("clarify"):
        finish_plan_usage(reservation, False)
        return {"clarify": out["clarify"]}
    finish_plan_usage(reservation, True)

    la, lb = f"#{body.report_a} {ra['filename']}", f"#{body.report_b} {rb['filename']}"
    detail = out["detail"]
    problems = detail[detail["Status"] != "Matched"].dropna(axis=1, how="all")
    xlsx = await asyncio.to_thread(recon.to_excel, out)
    run_id, cases_created = await asyncio.to_thread(ef.record_reconciliation, body.company_id, body.report_a, body.report_b, out, user["id"], "manual")
    db.audit(user, "reconcile", body.company_id, f"{la} vs {lb}; run={run_id}; cases={cases_created}")
    text = recon.summary_text(out, la, lb)
    hints = recon.likely_causes(out)
    if hints:
        text += "\n\nPOSSIBLE REASONS (found by comparing the numbers; please confirm):\n" + "\n".join("- " + h for h in hints)
    pdf = {"title": "Reconciliation", "question": f"{la} vs {lb}", "insight": (out.get("how") or "") + "\n\n" + recon._summary_text(out, la, lb).split("\n\n")[0],
           "table": out["summary"], "chart": None, "total_rows": len(out["summary"])}
    response = {
        "text": text, "hints": hints,
        "summary": json.loads(out["summary"].to_json(orient="records")),
        "columns": list(problems.columns),
        "rows": json.loads(problems.head(200).to_json(orient="records", date_format="iso")),
        "total_problem_rows": len(problems),
        "reconciliation_run_id": run_id,
        "cases_created": cases_created,
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id, pdf,
                                   ctx=text + "\n\nROWS THAT DID NOT MATCH (first 40):\n" + problems.head(40).to_string(index=False, max_colwidth=40)),
    }
    return response


@app.post("/api/forecast")
async def forecast_cash(body: ForecastIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    reservation = reserve_plan_usage(user, body.company_id, "forecast")
    days, buffer = min(max(body.days, 1), 30), min(max(body.buffer, 0.0), 1.0)
    try:
        df = await asyncio.to_thread(db.resolve_frame, body.company_id, body.report_id)
        table, info = await asyncio.to_thread(forecast.forecast, df, days, body.cash, buffer)
    except Exception as e:  # noqa: BLE001
        finish_plan_usage(reservation, False)
        raise HTTPException(400, str(e))
    finish_plan_usage(reservation, True)
    db.audit(user, "forecast", body.company_id, f"{days} days")
    fc_col = next(c for c in table.columns if str(c).startswith("Forecast next"))
    title = f"ATM cash forecast - next {days} days"
    note = (info + " Estimate from past withdrawals (same weekday, last 4 weeks) - a planning aid, not a guarantee. "
            "Check against your cash-in-transit schedule.")
    xlsx = await asyncio.to_thread(core.dfs_to_excel, {"Forecast": table})
    response = {
        "title": title,
        "columns": list(table.columns),
        "rows": json.loads(table.to_json(orient="records")),
        "total_rows": len(table),
        "chart": {"type": "bar", "labels": table.iloc[:, 0].astype(str).tolist()[:40],
                  "values": [float(v) for v in table[fc_col].tolist()[:40]], "metric": str(fc_col)},
        "insight": note,
        "plan": {},
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id,
                                   {"title": title, "question": f"Forecast, {days} days", "insight": note,
                                    "table": table, "chart": None, "total_rows": len(table)}),
    }
    return response


def _owned_result(result_id: str, user: dict) -> dict:
    item = RESULTS.get(result_id)
    if item is None or item["user"] != user["id"]:
        raise HTTPException(404, "Result expired - run it again.")
    return item


@app.get("/api/download/{result_id}")
def download(result_id: str, user: dict = Depends(current_user)):
    item = _owned_result(result_id, user)
    db.audit(user, "export", item["company"], "Excel download")
    return StreamingResponse(iter([item["data"]]), media_type=XLSX,
                             headers={"Content-Disposition": 'attachment; filename="analysis.xlsx"'})


@app.get("/api/download/{result_id}/pdf")
def download_pdf(result_id: str, user: dict = Depends(current_user)):
    item = _owned_result(result_id, user)
    p = item.get("pdf")
    if not p:
        raise HTTPException(404, "No PDF for this result.")
    company = db.get_company(item["company"])
    pdf = report_pdf.build_pdf(company["name"], p["title"], p["question"], p["insight"], p["table"],
                               p["chart"], db.get_branding(company["id"]), p.get("total_rows"))
    db.audit(user, "export_pdf", item["company"], p["title"][:100])
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": 'attachment; filename="report.pdf"'})


# ------------------------------- branding (admin) ------------------------------ #
@app.get("/api/companies/{company_id}/branding")
def get_branding(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    b = db.get_branding(company_id) or {}
    return {"display_name": b.get("display_name") or "", "color": b.get("color") or "#2b59ff",
            "has_logo": bool(b.get("logo_path") and db.read_file(b["logo_path"]) is not None)}


@app.put("/api/companies/{company_id}/branding")
async def put_branding(company_id: int, display_name: str = Form(""), color: str = Form("#2b59ff"),
                       logo: UploadFile | None = File(None), user: dict = Depends(current_user)):
    need(user, "admin")
    if not db.get_company(company_id):
        raise HTTPException(404, "Unknown company")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(400, "Colour must look like #2b59ff")
    logo_path = (db.get_branding(company_id) or {}).get("logo_path")
    if logo is not None and logo.filename:
        data = await logo.read()
        if len(data) > 2 * 1024 * 1024:
            raise HTTPException(400, "Logo must be under 2 MB")
        try:
            from PIL import Image

            Image.open(io.BytesIO(data)).verify()
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img.thumbnail((600, 300))
            logo_path = f"db:logo-{company_id}.png" if db.USE_PG else os.path.join(LOGO_DIR, f"{company_id}.png")
            png = io.BytesIO()
            img.save(png, "PNG")  # re-encoded, so only a real image is ever stored
            db.write_file(logo_path, png.getvalue())
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "That doesn't look like a PNG or JPG image.")
    db.set_branding(company_id, display_name.strip()[:80], color, logo_path)
    db.audit(user, "branding", company_id, display_name[:60])
    return {"ok": True}


# ----------------------------------- digests ----------------------------------- #
@app.get("/api/companies/{company_id}/digests")
def digests(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return db.list_digests(company_id)


@app.post("/api/companies/{company_id}/digests")
def add_digest(company_id: int, body: DigestIn, user: dict = Depends(current_user)):
    need(user, "analyst")
    need_company(user, company_id)
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", body.time.strip())
    if not m or body.weekday not in range(-1, 7) or body.scope not in ("latest", "all"):
        raise HTTPException(400, "Use a time like 08:00, a day (or every day) and 'latest' or 'all'.")
    if not 3 <= len(body.question.strip()) <= 300:
        raise HTTPException(400, "Write the question to run (3-300 characters).")
    did = db.add_digest(company_id, user["id"], body.question.strip(), body.weekday, int(m[1]), int(m[2]), body.scope)
    db.audit(user, "digest_add", company_id, body.question[:100])
    return {"id": did}


@app.delete("/api/digests/{digest_id}")
def delete_digest(digest_id: int, user: dict = Depends(current_user)):
    d = db.get_digest(digest_id)
    if not d:
        raise HTTPException(404, "Unknown digest")
    need_company(user, d["company_id"])
    need(user, "analyst")
    if user["role"] != "admin" and d["user_id"] != user["id"]:
        raise HTTPException(403, "Only the creator or an admin can delete this.")
    db.delete_digest(digest_id)
    db.audit(user, "digest_delete", d["company_id"], d["question"][:100])
    return {"deleted": digest_id}


# ------------------------------------ admin ---------------------------------- #
@app.get("/api/admin/registrations")
def admin_registrations(status: str = "pending", user: dict = Depends(current_user)):
    need(user, "admin")
    return portal.list_registration_requests(status)


@app.post("/api/admin/registrations/{request_id}/decision")
def admin_registration_decision(request_id: int, body: RegistrationDecisionIn,
                                user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        result = portal.decide_registration(request_id, body.decision, user, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, f"registration_{result['status']}", result.get("company_id"),
             f"request={request_id}; username={result.get('username')}")
    if result.get("status") == "approved" and result.get("user_id"):
        extras.tell_user(result["user_id"], "Welcome to Report Analyzer",
                         "Your account is approved. Upload your first report to get started. Your free trial is already running.", "success")
    return result


@app.get("/api/users")
def users(user: dict = Depends(current_user)):
    need(user, "admin")
    return db.list_users()


class CustomerIn(BaseModel):
    company_name: str
    username: str
    password: str
    telegram_id: int | None = None


@app.post("/api/customers")
def create_customer(body: CustomerIn, user: dict = Depends(current_user)):
    """Owner-only onboarding: each customer gets a dedicated company and analyst account."""
    need(user, "admin")
    try:
        customer = db.create_customer_account(
            body.company_name, body.username, body.password, body.telegram_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "create_customer", customer["company_ids"][0],
             f"{customer['username']} for {customer['company_name']}")
    return {"username": customer["username"], "role": customer["role"],
            "company_name": customer["company_name"], "telegram_linked": bool(customer["telegram_id"])}


@app.post("/api/users")
def new_user(body: UserIn, user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        u = db.create_user(body.username, body.password, body.role, body.telegram_id, body.company_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "create_user", detail=f"{u['username']} ({u['role']})")
    return u


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, user: dict = Depends(current_user)):
    need(user, "admin")
    fields = body.model_dump(exclude_unset=True)
    if user_id == user["id"] and ({"role", "active"} & fields.keys()):
        raise HTTPException(400, "You can't change your own role or deactivate yourself.")
    try:
        u = db.update_user(user_id, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not u:
        raise HTTPException(404, "Unknown user")
    db.audit(user, "update_user", detail=f"{u['username']}: {', '.join(sorted(fields))}")  # never log passwords
    return u


@app.get("/api/billing/status")
def billing_status(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    result = db.get_plan_status(company_id)
    # Do not advertise temporarily unavailable plans; keep the current plan visible for existing subscribers.
    result["plans"] = {key: plan for key, plan in db.PLANS.items()
                        if (plan.get("available", True) and not plan.get("custom")) or key == result.get("plan")}
    # Two temporary receiving accounts; the details can be changed with environment variables.
    result["banks"] = receipts.bank_accounts()
    # Backward compatibility for any older frontend using the original single-account fields.
    result["bank"] = {"name": os.getenv("BANK_NAME", ""), "account_number": os.getenv("BANK_ACCOUNT", ""),
                      "account_name": os.getenv("BANK_OWNER", "")}
    return result


@app.post("/api/billing/payment-requests")
def billing_payment_request(body: PaymentRequestIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    if user.get("role") == "admin":
        raise HTTPException(400, "Admin accounts do not need customer subscriptions.")
    plan = db.PLANS.get(body.plan)
    if plan and plan.get("custom"):
        raise HTTPException(400, "Business and larger plans are arranged with us first. Open Support and tell us what you need. No payment request was created.")
    if not plan or int(plan.get("price_ngn", 0)) > 150000:
        raise HTTPException(400, "Plans above ₦150,000 require contacting the admin before payment. No payment request was created.")
    coupon, discount = None, 0
    try:
        if (body.coupon_code or "").strip():
            coupon, discount = extras.percent_discount(body.coupon_code, body.company_id, int(plan["price_ngn"]))
        row = db.create_payment_request(body.company_id, user["id"], body.plan, body.transfer_reference, coupon, discount)
        if coupon:
            extras.use_coupon(coupon, body.company_id, user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "payment_request", body.company_id, f"request={row['id']}; plan={body.plan}")
    company = db.get_company(body.company_id)
    notify.alert_payment_admins(
        f"New payment request #{row['id']}\nCompany: {company['name'] if company else body.company_id}\n"
        f"User: {user['username']}\nPlan: {body.plan.upper()} · ₦{row['amount_ngn']:,}" + (f" (coupon {coupon}: -₦{discount:,})" if coupon else "") + f"\nReference: {row['transfer_reference']}\n"
        "Check your bank, then approve it under Plan & Billing.")
    return {"id": row["id"], "status": row["status"], "amount_ngn": row["amount_ngn"], "discount_ngn": discount}


MAX_PROOF_BYTES = 5 * 1024 * 1024


def _image_kind(data: bytes) -> tuple[str, str] | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


@app.post("/api/billing/payment-proof")
def billing_payment_proof(company_id: int = Form(...), file: UploadFile = File(...), user: dict = Depends(current_user)):
    """Customer attaches a screenshot of the transfer; the bot forwards it to the admin(s) on Telegram."""
    need_company(user, company_id)
    if user.get("role") == "admin":
        raise HTTPException(400, "Admin accounts do not need customer subscriptions.")
    data = file.file.read(MAX_PROOF_BYTES + 1)
    if not data:
        raise HTTPException(400, "The screenshot is empty.")
    if len(data) > MAX_PROOF_BYTES:
        raise HTTPException(400, "The screenshot is too large (5 MB maximum).")
    kind = _image_kind(data)
    if not kind:
        raise HTTPException(400, "Please upload a screenshot as a JPG, PNG or WEBP image.")
    row = db.attach_payment_proof(company_id, data, kind[0])
    if not row:
        raise HTTPException(400, "Submit your payment request first, then attach the screenshot.")
    db.audit(user, "payment_proof", company_id, f"request={row['id']}")
    caption = (f"Payment screenshot for request #{row['id']}\nCompany: {row['company_name']}\nUser: {user['username']}\n"
               f"Plan: {row['plan'].upper()} · ₦{int(row['amount_ngn']):,}\nReference: {row['transfer_reference']}\n"
               "Verify it in your bank, then approve under Plan & Billing.")
    sent = notify.alert_admins_photo(data, f"payment_{row['id']}.{kind[1]}", caption)
    return {"request_id": row["id"], "telegram_sent": sent}


@app.get("/api/admin/billing/payments/{request_id}/proof")
def admin_payment_proof(request_id: int, user: dict = Depends(current_user)):
    need(user, "admin")
    found = db.get_payment_proof(request_id)
    if not found:
        raise HTTPException(404, "No screenshot was attached to this request.")
    return Response(content=found[0], media_type=found[1],
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


# ----------------------- notifications + admin broadcast ----------------------- #
@app.get("/api/notifications")
def notifications(user: dict = Depends(current_user)):
    return db.list_notifications(user["id"])


@app.post("/api/notifications/read")
def notifications_read(body: ReadIn, user: dict = Depends(current_user)):
    db.mark_notifications_read(user["id"], body.ids)
    return {"ok": True}


@app.get("/api/admin/broadcasts")
def admin_broadcasts(user: dict = Depends(current_user)):
    need(user, "admin")
    return db.list_broadcasts_admin()


@app.post("/api/admin/broadcasts")
def admin_broadcast_send(body: BroadcastIn, user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        row = db.create_broadcast(body.title, body.message, body.level, body.days, user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "broadcast_sent", None, f"#{row['id']}: {row['title'][:80]}")
    emailed = 0
    if body.email and notify.email_configured():
        with db._db() as con:
            rows = con.execute("SELECT email FROM users WHERE role <> 'admin' AND active = 1 AND email IS NOT NULL AND email <> ''").fetchall()
        for r in rows:
            extras._mail_async(r["email"], f"Report Analyzer: {row['title']}", f"{row['body']}\n\n- Report Analyzer")
            emailed += 1
    row = dict(row)
    row["emailed"] = emailed
    return row


@app.delete("/api/admin/broadcasts/{broadcast_id}")
def admin_broadcast_withdraw(broadcast_id: int, user: dict = Depends(current_user)):
    need(user, "admin")
    if not db.withdraw_broadcast(broadcast_id):
        raise HTTPException(404, "Announcement not found.")
    db.audit(user, "broadcast_withdrawn", None, f"#{broadcast_id}")
    return {"ok": True}


# ----------------------- forgot password (Telegram one-time code) ----------------------- #
RESET_REQUESTS: dict = {}
RESET_FAILS: dict = {}


@app.post("/api/password-reset/request")
def password_reset_request(body: ResetRequestIn, request: Request):
    ip = request.client.host if request.client else "?"
    key = (body.username.strip().lower(), ip)
    now = time.time()
    RESET_REQUESTS[key] = [t for t in RESET_REQUESTS.get(key, []) if now - t < 3600]
    if len(RESET_REQUESTS[key]) >= 3:
        raise HTTPException(429, "Too many reset requests. Try again in one hour.")
    RESET_REQUESTS[key].append(now)
    result = db.create_password_reset(body.username)
    if result:
        text = (f"Your Report Analyzer password reset code is {result['code']}\n"
                f"It expires in {db.RESET_MINUTES} minutes. If you did not ask for this, ignore this message.")
        if result["user"].get("email"):
            extras._mail_async(result["user"]["email"], "Your Report Analyzer reset code", text)
        if result["user"].get("telegram_id"):
            notify.send_telegram(result["user"]["telegram_id"], "" + text)
        db.audit(result["user"], "password_reset_requested")
    # Same answer either way, so nobody can discover which usernames exist.
    return {"message": "If that account has an email address (or Telegram) on file, a 6-digit code has been sent there. "
                       "Nothing arrived? Ask your administrator to reset your password."}


@app.post("/api/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmIn, request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    RESET_FAILS[ip] = [t for t in RESET_FAILS.get(ip, []) if now - t < 3600]
    if len(RESET_FAILS[ip]) >= 10:
        raise HTTPException(429, "Too many attempts. Try again in one hour.")
    try:
        u = db.confirm_password_reset(body.username, body.code, body.new_password)
    except ValueError as e:
        if "code" in str(e):
            RESET_FAILS[ip].append(now)
        raise HTTPException(400, str(e))
    db.audit(u, "password_reset_done")
    return {"message": "Password changed. You can sign in now."}


@app.get("/api/admin/billing/payments")
def admin_billing_payments(status: str = "pending", user: dict = Depends(current_user)):
    need(user, "admin")
    return db.list_payment_requests(status)


@app.post("/api/admin/billing/payments/{request_id}/decision")
def admin_billing_payment_decision(request_id: int, body: PaymentDecisionIn,
                                   user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        row = db.decide_payment_request(request_id, body.decision, user["id"], body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, f"payment_{row['status']}", row["company_id"], f"request={request_id}; plan={row['plan']}")
    if row["status"] == "approved":
        with db._db() as con:
            sub = con.execute("SELECT id FROM subscriptions WHERE company_id=? AND status='active' ORDER BY id DESC LIMIT 1", (row["company_id"],)).fetchone()
        invoice = ef.create_invoice(row["company_id"], request_id, sub["id"] if sub else None, row["plan"], row["amount_ngn"])
        company = db.get_company(row["company_id"])
        message = f"Payment verified for {company['name'] if company else 'company'}. Plan {row['plan'].upper()} is active. Invoice: {invoice['invoice_number']} for ₦{row['amount_ngn']:,}."
        notify.alert_company(row["company_id"], message)
        notify.email_company(row["company_id"], "Report Analyzer payment approved", message)
        extras.tell_company(row["company_id"], "Payment approved",
                            f"Your {row['plan'].title()} plan is now active. Thank you! Your invoice is under Plan & Billing.", "success")
        extras.reward_referral_on_payment(row["company_id"])
    elif row["status"] == "rejected":
        extras.tell_company(row["company_id"], "Payment not approved",
                            "We could not confirm your payment. Please check the transfer reference and send a new request, or contact support.", "warning")
    return row


@app.get("/api/admin/billing/subscriptions")
def admin_billing_subscriptions(user: dict = Depends(current_user)):
    need(user, "admin")
    return db.list_subscriptions()


@app.post("/api/admin/billing/grant")
def admin_billing_grant(body: GrantPlanIn, user: dict = Depends(current_user)):
    need(user, "admin")
    if not db.get_company(body.company_id):
        raise HTTPException(404, "Unknown company")
    try:
        result = db.grant_subscription(body.company_id, body.plan, body.days, user["id"], body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "subscription_grant", body.company_id,
             f"plan={body.plan}; days={body.days}; note={body.note[:100]}")
    return result


@app.get("/api/audit")
def audit_log(limit: int = 100, user: dict = Depends(current_user)):
    need(user, "admin")
    return db.list_audit(min(max(limit, 1), 500))


@app.get("/api/usage")
def usage(user: dict = Depends(current_user)):
    need(user, "admin")
    return {"today": db.usage_today(),
            "per_user_limit": int(os.getenv("DAILY_AI_CALLS_PER_USER", "300")),
            "total_limit": int(os.getenv("DAILY_AI_CALLS_TOTAL", "1500"))}


@app.post("/api/backup")
async def backup_now(user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        path = await asyncio.to_thread(backup.create_backup)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Backup failed: {e}")
    db.audit(user, "backup", detail=os.path.basename(path))
    return {"file": os.path.basename(path), "size": os.path.getsize(path), "folder": backup.BACKUP_DIR}


@app.post("/api/backup/restore-check")
async def backup_restore_check(user: dict = Depends(current_user)):
    """Create a fresh backup, verify archive + SQLite integrity, and restore it into a temp folder."""
    need(user, "admin")
    try:
        path = await asyncio.to_thread(backup.create_backup)
        result = await asyncio.to_thread(backup.restore_drill, path)
    except Exception as e:  # noqa: BLE001
        db.audit(user, "backup_restore_check_failed", detail=str(e)[:300])
        raise HTTPException(500, f"Backup/restore check failed: {str(e)[:300]}")
    db.audit(user, "backup_restore_check_passed", detail=os.path.basename(path))
    return result


# ----------------------- discrepancy cases and historical comparisons ----------------------- #
@app.get("/api/companies/{company_id}/members")
def company_members(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    with db._db() as con:
        rows = con.execute("SELECT u.id,u.username,u.role FROM users u JOIN user_companies uc ON uc.user_id=u.id WHERE uc.company_id=? AND u.active=1 ORDER BY u.username", (company_id,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/companies/{company_id}/cases")
def company_cases(company_id: int, status: str = "all", limit: int = 200, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.list_cases(company_id, status, limit)


@app.get("/api/cases/{case_id}/events")
def discrepancy_case_events(case_id: int, user: dict = Depends(current_user)):
    case = ef.get_case(case_id)
    if not case: raise HTTPException(404, "Discrepancy case not found.")
    need_company(user, case["company_id"])
    return ef.case_events(case_id)


@app.patch("/api/cases/{case_id}")
def update_discrepancy_case(case_id: int, body: CaseUpdateIn, user: dict = Depends(current_user)):
    case = ef.get_case(case_id)
    if not case: raise HTTPException(404, "Discrepancy case not found.")
    need_company(user, case["company_id"])
    need(user, "analyst")
    if body.assigned_user_id is not None:
        target = db.get_user(body.assigned_user_id) if hasattr(db, "get_user") else None
        if target is None:
            with db._db() as con:
                r = con.execute("SELECT id,username,role FROM users WHERE id=? AND active=1", (body.assigned_user_id,)).fetchone()
                target = dict(r) if r else None
        if not target or not db.can_access(target, case["company_id"]):
            raise HTTPException(400, "Assignee must be an active member of this company.")
    try:
        out = ef.update_case(case_id, user["id"], body.status, body.assigned_user_id, body.comment)
    except LookupError as e: raise HTTPException(404, str(e))
    except ValueError as e: raise HTTPException(400, str(e))
    db.audit(user, "discrepancy_case_update", case["company_id"], f"case={case_id}; status={body.status or 'unchanged'}")
    return out


@app.get("/api/companies/{company_id}/reconciliation-history")
def reconciliation_history(company_id: int, limit: int = 100, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.list_reconciliation_runs(company_id, limit)


@app.get("/api/companies/{company_id}/trends")
def reconciliation_trends(company_id: int, days: int = 90, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.trend_summary(company_id, days)


# ----------------------- scheduled reconciliation ----------------------- #
@app.get("/api/companies/{company_id}/scheduled-reconciliations")
def get_scheduled_reconciliations(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.list_schedules(company_id)


@app.post("/api/companies/{company_id}/scheduled-reconciliations")
def add_scheduled_reconciliation(company_id: int, body: ScheduleIn, user: dict = Depends(current_user)):
    need_company(user, company_id); need(user, "analyst")
    a, b = report_for(user, body.report_a), report_for(user, body.report_b)
    if a["company_id"] != company_id or b["company_id"] != company_id:
        raise HTTPException(400, "Both reports must belong to the selected company.")
    try:
        row = ef.create_schedule(company_id, body.report_a, body.report_b, user["id"], body.interval_hours, body.instruction)
    except ValueError as e: raise HTTPException(400, str(e))
    db.audit(user, "scheduled_reconciliation_create", company_id, f"job={row['id']}; every={body.interval_hours}h")
    return row


@app.delete("/api/scheduled-reconciliations/{schedule_id}")
def remove_scheduled_reconciliation(schedule_id: int, user: dict = Depends(current_user)):
    need(user, "analyst")
    with db._db() as con:
        row = con.execute("SELECT * FROM scheduled_reconciliations WHERE id=?", (schedule_id,)).fetchone()
    if not row: raise HTTPException(404, "Scheduled reconciliation not found.")
    need_company(user, row["company_id"])
    ef.delete_schedule(schedule_id)
    db.audit(user, "scheduled_reconciliation_delete", row["company_id"], f"job={schedule_id}")
    return {"ok": True}


# ----------------------- email settings and billing records ----------------------- #
@app.get("/api/companies/{company_id}/notification-settings")
def get_notification_settings(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.notification_settings(company_id)


@app.put("/api/companies/{company_id}/notification-settings")
def put_notification_settings(company_id: int, body: NotificationSettingsIn, user: dict = Depends(current_user)):
    need_company(user, company_id); need(user, "analyst")
    try: ef.set_notification_emails(company_id, body.emails, body.quota_threshold)
    except ValueError as e: raise HTTPException(400, str(e))
    db.audit(user, "notification_settings_update", company_id, f"emails={len(body.emails)}; quota={body.quota_threshold}%")
    return ef.notification_settings(company_id)


@app.get("/api/billing/invoices")
def customer_invoices(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return ef.list_invoices(company_id)


@app.get("/api/billing/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, user: dict = Depends(current_user)):
    invoice = ef.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found.")
    need_company(user, invoice["company_id"])
    pay = sub = None
    try:
        with db._db() as con:
            if invoice.get("payment_request_id"):
                row = con.execute("SELECT transfer_reference FROM payment_requests WHERE id = ?", (invoice["payment_request_id"],)).fetchone()
                pay = dict(row) if row else None
            if invoice.get("subscription_id"):
                row = con.execute("SELECT starts_at, expires_at FROM subscriptions WHERE id = ?", (invoice["subscription_id"],)).fetchone()
                sub = dict(row) if row else None
    except Exception:  # noqa: BLE001  (a missing detail just leaves that line off the receipt)
        log.exception("receipt details unavailable")
    pdf = receipts.receipt_pdf(invoice, invoice["company_name"], pay, sub, db.get_branding(invoice["company_id"]))
    db.audit(user, "invoice_export", invoice["company_id"], invoice["invoice_number"])
    return StreamingResponse(iter([pdf]), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{invoice["invoice_number"]}.pdf"'})


class ProformaIn(BaseModel):
    company_id: int
    plan: str
    coupon_code: str | None = None


@app.post("/api/billing/proforma")
def billing_proforma(body: ProformaIn, user: dict = Depends(current_user)):
    """A pro-forma invoice (a request for payment with the bank details) for a plan the company is thinking of buying."""
    need_company(user, body.company_id)
    if body.plan not in db.SELF_SERVE_PLANS:
        raise HTTPException(400, "Choose one of the currently offered plans. Business and larger plans are arranged with us first: open Support.")
    company = db.get_company(body.company_id)
    discount = 0
    if body.coupon_code:
        try:
            _, discount = extras.percent_discount(body.coupon_code, body.company_id, int(db.PLANS[body.plan]["price_ngn"]))
        except ValueError as e:
            raise HTTPException(400, str(e))
    pdf = receipts.proforma_pdf(company["name"], body.plan, db.get_branding(body.company_id), discount)
    db.audit(user, "proforma_export", body.company_id, body.plan)
    return StreamingResponse(iter([pdf]), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="invoice.pdf"'})


# ----------------------- secure generic external integration ----------------------- #
@app.post("/api/companies/{company_id}/integrations/tokens")
def create_integration_token(company_id: int, request: Request, user: dict = Depends(current_user)):
    need_company(user, company_id); need(user, "admin")
    label = request.query_params.get("label", "External accounting export")[:100]
    token = ef.create_integration_token(company_id, label, user["id"])
    db.audit(user, "integration_token_create", company_id, label)
    return {"token": token, "warning": "Copy this token now. It is shown only once; store it as a secret."}


@app.get("/api/companies/{company_id}/integrations/tokens")
def list_integration_tokens(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id); need(user, "admin")
    return ef.list_integration_tokens(company_id)


@app.delete("/api/companies/{company_id}/integrations/tokens/{token_id}")
def revoke_integration_token(company_id: int, token_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id); need(user, "admin")
    if not ef.revoke_integration_token(token_id, company_id): raise HTTPException(404, "Integration token not found.")
    db.audit(user, "integration_token_revoke", company_id, f"token={token_id}")
    return {"ok": True}


@app.post("/api/integrations/{company_id}/reports")
async def integration_upload(company_id: int, request: Request, file: UploadFile = File(...)):
    token = request.headers.get("X-Integration-Token", "")
    token_row = ef.validate_integration_token(token)
    if not token_row or int(token_row["company_id"]) != company_id:
        raise HTTPException(401, "Invalid integration token for this company.")
    if not db.get_company(company_id): raise HTTPException(404, "Unknown company.")
    name = os.path.basename(file.filename or "external-report.csv")[:160]
    ext = os.path.splitext(name)[1].lower()
    if ext and ext.lstrip(".") not in core.ALLOWED_EXT:
        raise HTTPException(400, "Supported files: " + core.FILE_TYPES_TEXT)
    raw = await file.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024: raise HTTPException(413, "Maximum integration upload size is 20 MB.")
    try:
        df, notes, _sheet_names = await asyncio.to_thread(core.load_report, raw, name)
        rid = await asyncio.to_thread(db.save_upload, company_id, name, raw, df, token_row["created_by"])
        await asyncio.to_thread(db.save_description, rid, await core.describe_report(df, name))
    except Exception as e: raise HTTPException(400, f"Could not ingest external report: {str(e)[:250]}")
    db.audit(None, "integration_report_upload", company_id, f"token={token_row['id']}; report=#{rid}; {name}", "integration")
    return {"ok": True, "report_id": rid, "filename": name, "rows": int(len(df)), "notes": notes}


# ----------------------- email on file, referrals, coupons, feedback, saved questions, owner overview ----------------------- #
@app.put("/api/me/email")
def set_my_email(body: EmailIn, user: dict = Depends(current_user)):
    try:
        email = extras.set_email(user["id"], body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "email_updated")
    return {"email": email, "email_service_ready": notify.email_configured()}


@app.get("/api/referral")
def my_referral(user: dict = Depends(current_user)):
    return extras.my_referral(user["id"])


@app.post("/api/billing/redeem")
def redeem_coupon(body: RedeemIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    try:
        out = extras.redeem_days_coupon(body.code, body.company_id, user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "coupon_redeemed", body.company_id, body.code[:40])
    return out


@app.get("/api/admin/coupons")
def admin_coupons(user: dict = Depends(current_user)):
    need(user, "admin")
    return extras.list_coupons()


@app.post("/api/admin/coupons")
def admin_coupon_create(body: CouponIn, user: dict = Depends(current_user)):
    need(user, "admin")
    try:
        row = extras.create_coupon(body.kind, body.value, body.max_uses, body.days_valid, body.code, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "coupon_created", None, f"{row['code']} {row['kind']} {row['value']}")
    return row


@app.delete("/api/admin/coupons/{code}")
def admin_coupon_disable(code: str, user: dict = Depends(current_user)):
    need(user, "admin")
    if not extras.disable_coupon(code):
        raise HTTPException(404, "Coupon not found.")
    return {"ok": True}


@app.get("/api/admin/overview")
def admin_overview(user: dict = Depends(current_user)):
    need(user, "admin")
    return extras.overview()


@app.post("/api/feedback")
def send_feedback(body: FeedbackIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    try:
        extras.add_feedback(user["id"], body.company_id, body.rating, body.question, body.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/admin/feedback")
def admin_feedback(user: dict = Depends(current_user)):
    need(user, "admin")
    return extras.list_feedback()


@app.get("/api/companies/{company_id}/saved-questions")
def saved_questions(company_id: int, user: dict = Depends(current_user)):
    need_company(user, company_id)
    return extras.list_saved(user["id"], company_id)


@app.post("/api/companies/{company_id}/saved-questions")
def save_question(company_id: int, body: SavedQuestionIn, user: dict = Depends(current_user)):
    need_company(user, company_id)
    try:
        return extras.save_question(user["id"], company_id, body.question)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/saved-questions/{saved_id}")
def delete_saved_question(saved_id: int, user: dict = Depends(current_user)):
    if not extras.delete_saved(user["id"], saved_id):
        raise HTTPException(404, "Saved question not found.")
    return {"ok": True}



if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
