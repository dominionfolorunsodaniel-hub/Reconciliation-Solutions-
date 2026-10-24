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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import anomaly
import backup
import core
import db
import forecast
import enterprise_features as ef
import notify
import portal
import recon
import report_pdf
import scheduler
import totp

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
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


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
            "totp_enabled": user["totp_enabled"],
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
    attempts = [t for t in REGISTRATION_FAILS.get(ip, []) if now - t < 3600]
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many registration attempts. Try again in one hour.")
    try:
        result = portal.submit_registration(body.company_name, body.username, body.password, body.telegram_id)
    except ValueError as e:
        attempts.append(now)
        REGISTRATION_FAILS[ip] = attempts
        raise HTTPException(400, str(e))
    REGISTRATION_FAILS[ip] = attempts
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
    token = db.create_session(user["id"])
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=COOKIE_SECURE,
                        max_age=db.SESSION_HOURS * 3600)
    db.audit(user, "login")
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


@app.get("/static/login-character.png")
def login_character():
    return FileResponse(Path(__file__).parent / "static" / "login-character.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


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
    if name.lower().rsplit(".", 1)[-1] not in core.ALLOWED_EXT:
        raise HTTPException(400, "Supported files: CSV, Excel (.xlsx/.xls), PDF")
    data = await file.read()
    if len(data) > core.MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"File too large (limit {core.MAX_FILE_MB} MB)")
    try:
        df, notes, _ = await asyncio.to_thread(core.load_report, data, name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read that file: {e}")
    reservation = reserve_plan_usage(user, company_id, "report_description")
    try:
        desc = await core.describe_report(df, name)
    except Exception:
        finish_plan_usage(reservation, False)
        raise
    finish_plan_usage(reservation, True)
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
        reservation = reserve_plan_usage(user, r["company_id"], "report_description")
        try:
            df = await asyncio.to_thread(db.get_frame, report_id)
            desc = await core.describe_report(df, r["filename"])
        except Exception:
            finish_plan_usage(reservation, False)
            raise
        finish_plan_usage(reservation, True)
        db.save_description(report_id, desc)
    return desc


# ----------------------------------- analysis -------------------------------- #
def _store_result(data: bytes, user: dict, company_id: int, pdf: dict | None = None) -> str:
    rid = secrets.token_urlsafe(8)
    RESULTS[rid] = {"data": data, "user": user["id"], "company": company_id, "pdf": pdf}
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
    pdf = {"title": "Reconciliation", "question": f"{la} vs {lb}", "insight": text.split("\n\n")[0],
           "table": out["summary"], "chart": None, "total_rows": len(out["summary"])}
    response = {
        "text": text,
        "summary": json.loads(out["summary"].to_json(orient="records")),
        "columns": list(problems.columns),
        "rows": json.loads(problems.head(200).to_json(orient="records", date_format="iso")),
        "total_problem_rows": len(problems),
        "reconciliation_run_id": run_id,
        "cases_created": cases_created,
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id, pdf),
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
                        if plan.get("available", True) or key == result.get("plan")}
    # Support two temporary receiving accounts; secrets/details stay in Render environment variables.
    result["banks"] = [
        {"name": os.getenv("MONIEPOINT_BANK_NAME") or "Moniepoint",
         "account_number": os.getenv("MONIEPOINT_ACCOUNT") or "8100481004",
         "account_name": os.getenv("MONIEPOINT_OWNER") or "Daniel Dominion Folorunso"},
        {"name": os.getenv("OPAY_BANK_NAME") or "OPay",
         "account_number": os.getenv("OPAY_ACCOUNT") or "8083878507",
         "account_name": os.getenv("OPAY_OWNER") or "Mary Olubunmi Folorunso"},
    ]
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
    if not plan or int(plan.get("price_ngn", 0)) > 150000:
        raise HTTPException(400, "Plans above ₦150,000 require contacting the admin before payment. No payment request was created.")
    try:
        row = db.create_payment_request(body.company_id, user["id"], body.plan, body.transfer_reference)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(user, "payment_request", body.company_id, f"request={row['id']}; plan={body.plan}")
    company = db.get_company(body.company_id)
    notify.alert_payment_admins(
        f"💳 New payment request #{row['id']}\nCompany: {company['name'] if company else body.company_id}\n"
        f"User: {user['username']}\nPlan: {body.plan.upper()} · ₦{row['amount_ngn']:,}\nReference: {row['transfer_reference']}\n"
        "Check your bank, then approve it under Plan & Billing.")
    return {"id": row["id"], "status": row["status"], "amount_ngn": row["amount_ngn"]}


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
    caption = (f"📸 Payment screenshot for request #{row['id']}\nCompany: {row['company_name']}\nUser: {user['username']}\n"
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
        notify.send_telegram(result["user"]["telegram_id"],
                             f"🔑 Report Analyzer password reset code: {result['code']}\n"
                             f"It expires in {db.RESET_MINUTES} minutes. If you did not ask for this, ignore it.")
        db.audit(result["user"], "password_reset_requested")
    # Same answer either way, so nobody can discover which usernames exist.
    return {"message": "If that account has Telegram linked, a 6-digit code has been sent there. "
                       "No Telegram? Ask your administrator to reset your password."}


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
            "per_user_limit": int(os.getenv("DAILY_AI_CALLS_PER_USER", "150")),
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
    if not invoice: raise HTTPException(404, "Invoice not found.")
    need_company(user, invoice["company_id"])
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    buf = io.BytesIO(); styles = getSampleStyleSheet()
    story = [Paragraph("Report Analyzer — Payment Invoice / Receipt", styles["Title"]), Spacer(1, 16),
             Paragraph(f"Invoice: {invoice['invoice_number']}", styles["Normal"]),
             Paragraph(f"Company: {invoice['company_name']}", styles["Normal"]),
             Paragraph(f"Issued (UTC): {invoice['issued_at']}", styles["Normal"]),
             Paragraph(f"Status: {invoice['status'].upper()}", styles["Normal"]), Spacer(1, 18)]
    table = Table([["Plan", "Billing term", "Amount"], [invoice["plan"].replace("_", " ").title(), "Fixed-term subscription", f"NGN {invoice['amount_ngn']:,}"]], colWidths=[190, 150, 130])
    table.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,0), colors.HexColor("#243b75")), ("TEXTCOLOR", (0,0),(-1,0),colors.white), ("GRID",(0,0),(-1,-1),0.5,colors.grey), ("PADDING",(0,0),(-1,-1),8)]))
    story.extend([table, Spacer(1, 16), Paragraph("Payment was manually verified by a Report Analyzer administrator. This document is a product receipt and is not a tax invoice unless separately validated for local tax requirements.", styles["BodyText"])])
    SimpleDocTemplate(buf, pagesize=A4).build(story)
    db.audit(user, "invoice_export", invoice["company_id"], invoice["invoice_number"])
    return StreamingResponse(iter([buf.getvalue()]), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{invoice["invoice_number"]}.pdf"'})


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
    if ext not in {".csv", ".txt", ".xlsx", ".xlsm", ".xls", ".pdf"}:
        raise HTTPException(400, "Supported formats: CSV, TXT, XLSX, XLSM, XLS, PDF.")
    raw = await file.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024: raise HTTPException(413, "Maximum integration upload size is 20 MB.")
    try:
        df, notes, _sheet_names = await asyncio.to_thread(core.load_report, raw, name)
        rid = await asyncio.to_thread(db.save_upload, company_id, name, raw, df, token_row["created_by"])
        await asyncio.to_thread(db.save_description, rid, await core.describe_report(df, name))
    except Exception as e: raise HTTPException(400, f"Could not ingest external report: {str(e)[:250]}")
    db.audit(None, "integration_report_upload", company_id, f"token={token_row['id']}; report=#{rid}; {name}", "integration")
    return {"ok": True, "report_id": rid, "filename": name, "rows": int(len(df)), "notes": notes}


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
