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
import notify
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


# ---------------------------------- models ---------------------------------- #
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


class DigestIn(BaseModel):
    question: str
    weekday: int  # 0=Mon ... 6=Sun, -1 = every day
    time: str  # "HH:MM"
    scope: str = "latest"


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


# ------------------------------- login / session ----------------------------- #
@app.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    key = (body.username.strip().lower(), request.client.host if request.client else "?")
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 900]
    if len(FAILS[key]) >= 5:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = await asyncio.to_thread(db.verify_login, body.username, body.password)
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
def _store_result(data: bytes, user: dict, company_id: int, pdf: dict | None = None) -> str:
    rid = secrets.token_urlsafe(8)
    RESULTS[rid] = {"data": data, "user": user["id"], "company": company_id, "pdf": pdf}
    while len(RESULTS) > 50:
        RESULTS.popitem(last=False)
    return rid


@app.post("/api/analyze")
async def analyze(body: AnalyzeIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    try:
        df = await asyncio.to_thread(db.resolve_frame, body.company_id, body.report_id)
        out = await core.analyze(df, body.question, body.prev_plan, INSIGHTS)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    if out.get("clarify"):
        return {"clarify": out["clarify"]}

    res = out["res"]
    db.log_query(body.company_id, body.report_id, body.question, out["plan"], user["id"])
    db.audit(user, "analyze", body.company_id, body.question[:200])
    xlsx = await asyncio.to_thread(core.make_excel, res, out["filtered"], out["plan"], out["insight"])
    chart_png = await asyncio.to_thread(core.make_chart_png, res, out["keys"], out["metric"],
                                        out["plan"].get("chart", "bar"), out["title"])
    pdf = {"title": out["title"], "question": body.question, "insight": out["insight"], "table": res.head(40),
           "chart": chart_png.getvalue() if chart_png else None, "total_rows": len(res)}
    return {
        "title": out["title"],
        "columns": list(res.columns),
        "rows": json.loads(res.head(500).to_json(orient="records", date_format="iso")),
        "total_rows": len(res),
        "chart": core.chart_payload(res, out["keys"], out["metric"], out["plan"].get("chart", "bar")),
        "insight": out["insight"],
        "plan": out["plan"],
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id, pdf),
    }


@app.post("/api/reconcile")
async def reconcile(body: ReconIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    ra, rb = report_for(user, body.report_a), report_for(user, body.report_b)
    if ra["company_id"] != body.company_id or rb["company_id"] != body.company_id:
        raise HTTPException(400, "Both reports must belong to the selected company.")
    try:
        a = await asyncio.to_thread(db.get_frame, body.report_a)
        b = await asyncio.to_thread(db.get_frame, body.report_b)
        out = await recon.reconcile(a, b, body.instruction)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    if out.get("clarify"):
        return {"clarify": out["clarify"]}

    la, lb = f"#{body.report_a} {ra['filename']}", f"#{body.report_b} {rb['filename']}"
    detail = out["detail"]
    problems = detail[detail["Status"] != "Matched"].dropna(axis=1, how="all")
    xlsx = await asyncio.to_thread(recon.to_excel, out)
    db.audit(user, "reconcile", body.company_id, f"{la} vs {lb}")
    text = recon.summary_text(out, la, lb)
    pdf = {"title": "Reconciliation", "question": f"{la} vs {lb}", "insight": text.split("\n\n")[0],
           "table": out["summary"], "chart": None, "total_rows": len(out["summary"])}
    return {
        "text": text,
        "summary": json.loads(out["summary"].to_json(orient="records")),
        "columns": list(problems.columns),
        "rows": json.loads(problems.head(200).to_json(orient="records", date_format="iso")),
        "total_problem_rows": len(problems),
        "result_id": _store_result(xlsx.getvalue(), user, body.company_id, pdf),
    }


@app.post("/api/forecast")
async def forecast_cash(body: ForecastIn, user: dict = Depends(current_user)):
    need_company(user, body.company_id)
    days, buffer = min(max(body.days, 1), 30), min(max(body.buffer, 0.0), 1.0)
    try:
        df = await asyncio.to_thread(db.resolve_frame, body.company_id, body.report_id)
        table, info = await asyncio.to_thread(forecast.forecast, df, days, body.cash, buffer)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    db.audit(user, "forecast", body.company_id, f"{days} days")
    fc_col = next(c for c in table.columns if str(c).startswith("Forecast next"))
    title = f"ATM cash forecast - next {days} days"
    note = (info + " Estimate from past withdrawals (same weekday, last 4 weeks) - a planning aid, not a guarantee. "
            "Check against your cash-in-transit schedule.")
    xlsx = await asyncio.to_thread(core.dfs_to_excel, {"Forecast": table})
    return {
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
            "has_logo": bool(b.get("logo_path") and os.path.exists(b["logo_path"]))}


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
            logo_path = os.path.join(LOGO_DIR, f"{company_id}.png")
            img.save(logo_path, "PNG")  # re-encoded, so only a real image is ever stored
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


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
