"""
smoke_test.py - checks the whole system without Telegram or Gemini API keys.
Run:  python smoke_test.py
(The AI planning step is replaced by a fixed plan; everything else is real.)
"""
import asyncio
import io
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("GEMINI_API_KEY", None)  # force the offline fallbacks
os.environ.pop("ANTHROPIC_API_KEY", None)  # legacy key is not used
try:
    from cryptography.fernet import Fernet

    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["REQUIRE_ENCRYPTION"] = "1"
    ENCRYPTED = True
except ImportError:
    ENCRYPTED = False

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import anomaly  # noqa: E402
import core  # noqa: E402
import db  # noqa: E402
import recon  # noqa: E402

PLAN = {
    "title": "Amount by branch and month",
    "filters": [{"column": "Status", "op": "==", "value": "success"}],
    "group_by": [{"column": "Branch"}, {"column": "Date", "freq": "M"}],
    "metrics": [{"column": "Amount", "agg": "sum", "alias": "Total amount"}],
    "percent_of_total": True, "chart": "bar", "sort": None, "top_n": None, "clarify": None,
}
PAN = "4111111111111111"


def pos_csv() -> bytes:
    return (
        "ACME BANK - POS REPORT\nGenerated 2026-10-01\n\n"
        "Date,Branch,Terminal,Amount,Status\n"
        '2026-01-05,Lagos,T1,"1,200.50",success\n'
        '2026-01-20,Lagos,T2,"800.00",success\n'
        '2026-02-03,Abuja,T3,"2,500.00",success\n'
        '2026-02-04,Abuja,T3,"999.00",failed\n'
        '2026-02-10,Lagos,T1,"300.00",success\n'
        "Total,,,\"5,800.50\",\n"
    ).encode()


def card_csv() -> bytes:
    return (
        "Date,Card Number,CVV,Reference,Amount\n"
        f"2026-01-05,{PAN},123,7000000000012345,100.00\n"
        "2026-01-06,5500 0000 0000 0004,456,7000000000012346,50.00\n"
    ).encode()


def make_pdf() -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    out = io.BytesIO()
    t = Table([["Date", "Branch", "Amount", "Status"],
               ["2026-03-01", "Lagos", "1,000.00", "success"],
               ["2026-03-02", "Abuja", "2,000.00", "success"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(out, pagesize=A4).build([t])
    return out.getvalue()


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    assert cond, msg


def anomaly_frame() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    days = pd.date_range("2026-01-01", periods=30)
    rows = []
    for _ in range(600):
        t = f"T{rng.integers(1, 6)}"
        d = days[rng.integers(0, 30)]
        if t == "T5" and d > days[9]:  # T5 stops after day 10
            d = days[rng.integers(0, 10)]
        fail = rng.random() < (0.6 if t == "T3" else 0.05)  # T3 is failing a lot
        rows.append({"Date": d, "Terminal": t, "Amount": float(rng.integers(50, 500)),
                     "Status": "failed" if fail else "success"})
    rows.append({"Date": days[3], "Terminal": "T1", "Amount": 900000.0, "Status": "success"})
    return pd.DataFrame(rows)


def main():
    print(f"(encryption at rest: {'ON' if ENCRYPTED else 'OFF - cryptography not installed'})")

    # 1. loading + cleaning --------------------------------------------------------
    csv_bytes = pos_csv()
    df, notes, _ = core.load_report(csv_bytes, "pos.csv")
    check(list(df.columns) == ["Date", "Branch", "Terminal", "Amount", "Status"], "header row found below title lines")
    check(len(df) == 5, "Total row ignored")
    check(float(df["Amount"].sum()) == 5799.5, "amounts parsed from '1,200.50' strings")
    check(pd.api.types.is_datetime64_any_dtype(df["Date"]), "date column detected")
    check(not pd.api.types.is_datetime64_any_dtype(df["Terminal"]), "IDs like T1 are NOT mistaken for dates")
    check(not df.attrs.get("sanitized"), "ordinary report is not flagged as sanitized")
    try:
        dfp, _, _ = core.load_report(make_pdf(), "pos.pdf")
        check(len(dfp) == 2 and "Amount" in dfp.columns, "PDF table extracted")
    except ImportError:
        print("SKIP  PDF (pdfplumber/reportlab missing)")

    # 2. report description ---------------------------------------------------------
    d = asyncio.run(core.describe_report(df, "pos.csv"))
    check(d["facts"]["rows"] == 5 and d["facts"]["numbers"]["Amount"]["sum"] == 5799.5, "report profile: exact facts")
    check(bool(d["what_it_is"]) and len(d["suggested_questions"]) >= 3, "description + suggested questions")
    check(d["period"] == "2026-01-05 to 2026-02-10", "period detected")
    bad = df.copy()
    bad.loc[0, "Amount"] = -5
    check(any("negative" in f for f in core.quality_flags(core.profile_facts(bad))), "data-quality flag: negative values")

    # 3. privacy ----------------------------------------------------------------------
    cdf, cnotes, _ = core.load_report(card_csv(), "cards.csv")
    check("CVV" not in cdf.columns, "CVV column dropped")
    check(cdf["Card Number"].iloc[0] == "************1111" and cdf["Card Number"].iloc[1] == "************0004",
          "card numbers masked to last 4 (incl. spaced format)")
    check(int(cdf["Reference"].iloc[0]) == 7000000000012345, "16-digit reference number NOT mistaken for a card")
    check(cdf.attrs.get("sanitized") is True and any("Masked" in n for n in cnotes), "masking reported in notes")

    # 4. analysis (exact pandas math) ---------------------------------------------------
    res, filtered, keys, metric = core.run_plan(df, PLAN)
    check(len(filtered) == 4, "filter status=success keeps 4 rows")
    lagos_jan = res[(res["Branch"] == "Lagos") & (res["Date (Month)"] == "2026-01")]["Total amount"].iloc[0]
    check(float(lagos_jan) == 2000.5, "Lagos Jan total = 2000.50")
    check("% of total" in res.columns, "percent of total added")
    check(core.make_chart_png(res, keys, metric, "bar", "t").getbuffer().nbytes > 1000, "PNG chart renders")
    xl = core.make_excel(res, filtered, PLAN, "- insight")
    check(len(pd.ExcelFile(xl).sheet_names) == 3, "Excel export has 3 sheets")

    # 5. Excel formula-injection safety ----------------------------------------------------
    import openpyxl

    wb = openpyxl.load_workbook(core.dfs_to_excel({"T": pd.DataFrame({"a": ["=1+1", "ok"]})}))
    cell = wb["T"]["A2"]
    check(cell.value == "=1+1" and cell.data_type != "f", "text starting with '=' is never an Excel formula")

    # 6. anomalies -----------------------------------------------------------------------------
    adf = anomaly_frame()
    found = anomaly.scan(adf)
    texts = [(a["severity"], a["text"]) for a in found]
    check(any(s == "high" and '"T3"' in t for s, t in texts), "alert: T3 failure-rate spike (high)")
    check(any('"T5"' in t and "went quiet" in t for _, t in texts), "alert: T5 went quiet")
    check(any("extreme value" in t for _, t in texts), "alert: extreme amount")
    check(anomaly.push_text("ACME", "r.csv", found) is not None, "push message built for high alerts")
    check(anomaly.scan(df) == [], "small clean report -> no alerts")
    ad = asyncio.run(core.describe_report(adf, "terminals.csv"))
    check("Alerts" in core.description_text(ad), "alerts shown in the upload description")

    # 7. reconciliation --------------------------------------------------------------------------
    a = pd.DataFrame({"Ref": [1, 2, 3, 4, 5, 6], "Amount": [10.0, 20, 30, 40, 50, 60]})
    b = pd.DataFrame({"Reference": ["1", "2", "3", "4", "5", "7"], "Amt": [10.0, 20, 30, 40, 45, 70]})
    plan = {"keys": [{"a": "Ref", "b": "Reference"}], "amount": {"a": "Amount", "b": "Amt"}, "tolerance": 0.01}
    out = recon.run_recon(a, b, plan)
    counts = dict(zip(out["summary"]["Status"], out["summary"]["Count"]))
    check(counts == {"Matched": 4, "Amount mismatch": 1, "Only in A": 1, "Only in B": 1}, "recon: 4 matched / 1 mismatch / 1 only A / 1 only B")
    mism = out["detail"][out["detail"]["Status"] == "Amount mismatch"]
    check(float(mism["Difference (A-B)"].iloc[0]) == 5.0, "recon: mismatch difference = 5.00")
    check(len(pd.ExcelFile(recon.to_excel(out)).sheet_names) == 6, "recon Excel export")
    same = asyncio.run(recon.reconcile(a, a.copy(), ""))  # offline: same-named columns
    check(int(same["summary"]["Count"].iloc[0]) == 6, "recon fallback matches same-named columns")

    # 8. users, roles, per-company access, sessions, audit -----------------------------------------
    c1, c2 = db.get_or_create_company("ACME"), db.get_or_create_company("Globex")
    admin = db.create_user("boss", "boss-password-1", "admin")
    ana = db.create_user("ana", "analyst-pass-1", "analyst", telegram_id=111, company_ids=[c1["id"]])
    viewer = db.create_user("vic", "viewer-pass-12", "viewer", company_ids=[c1["id"], c2["id"]])
    check(db.verify_login("ana", "analyst-pass-1")["role"] == "analyst", "login works")
    check(db.verify_login("ana", "wrong-password") is None, "wrong password rejected")
    try:
        db.create_user("weak", "short", "viewer")
        check(False, "weak password rejected")
    except ValueError:
        check(True, "weak password rejected")
    check(db.can_access(admin, c2["id"]) and db.can_access(ana, c1["id"]) and not db.can_access(ana, c2["id"]),
          "per-company access enforced")
    check([c["name"] for c in db.list_companies(ana)] == ["ACME"], "analyst only sees her company")
    check(db.get_user_by_telegram(111)["username"] == "ana", "telegram ID links to user")
    customer = db.create_customer_account("CustomerCo Ltd", "customer01", "temp-password-123", 222)
    check(customer["role"] == "analyst" and customer["company_name"] == "CustomerCo Ltd",
          "customer onboarding creates an analyst account and dedicated company")
    check(db.can_access(customer, customer["company_ids"][0]) and not db.can_access(customer, c1["id"]),
          "new customer cannot access another company's data")
    check(db.get_user_by_telegram(222)["username"] == "customer01", "customer Telegram account is linked")
    try:
        db.create_customer_account("CustomerCo Ltd", "customer02", "another-password-123")
        check(False, "duplicate customer company rejected")
    except ValueError:
        check(True, "duplicate customer company rejected")
    tok = db.create_session(ana["id"])
    check(db.get_session_user(tok)["username"] == "ana", "session works")
    db.update_user(ana["id"], active=False)
    check(db.get_session_user(tok) is None and db.verify_login("ana", "analyst-pass-1") is None, "deactivated user is locked out at once")
    db.update_user(ana["id"], active=True)
    tok2 = db.create_session(ana["id"])
    with db._db() as con:
        con.execute("UPDATE sessions SET expires_at = '2000-01-01 00:00:00'")
    check(db.get_session_user(tok2) is None, "expired session rejected")
    db.audit(admin, "test_action", c1["id"], "hello")
    check(db.list_audit(5)[0]["action"] == "test_action", "audit log records actions")

    # 9. storage: encrypted at rest, card data never written to disk ------------------------------
    rid_card = db.save_upload(c1["id"], "cards.csv", card_csv(), cdf, admin["id"])
    path = db.get_report(rid_card)["stored_path"]
    raw = open(path, "rb").read()
    check(PAN.encode() not in raw and PAN.encode() not in db.security.decrypt(raw), "full card number never stored on disk")
    check(db.get_report(rid_card)["stored_ext"] == "csv", "sanitized copy stored instead of the original")
    check(db.get_frame(rid_card)["Card Number"].iloc[0] == "************1111", "stored report reloads masked")
    rid_pos = db.save_upload(c1["id"], "pos.csv", csv_bytes, df, admin["id"])
    raw_pos = open(db.get_report(rid_pos)["stored_path"], "rb").read()
    if ENCRYPTED:
        check(b"Lagos" not in raw_pos and raw_pos.startswith(b"ENC1:"), "report file is encrypted on disk")
    check(db.get_frame(rid_pos).shape == (5, 5), "encrypted report loads back")
    db.save_description(rid_pos, d)
    check(db.get_description(rid_pos)["what_it_is"] == d["what_it_is"], "description saved and reloaded")
    db.log_query(c1["id"], rid_pos, "secret question", PLAN, admin["id"])
    check(db.list_queries(c1["id"])[0]["question"] == "secret question", "history saved and reloaded")
    allf = db.resolve_frame(c1["id"], None)
    check("_report" in allf.columns and len(allf) == 7, "company frame combines reports")

    # 10. retention ----------------------------------------------------------------------------------
    with db._db() as con:
        con.execute("UPDATE reports SET uploaded_at = '2000-01-01 00:00:00' WHERE id = ?", (rid_card,))
    removed = db.purge_expired(report_days=30, audit_days=0)
    check(removed["reports"] == 1 and not os.path.exists(path), "retention: old report and its file deleted")

    # 10b. two-factor login ------------------------------------------------------------------------
    import base64

    import totp

    rfc_secret = base64.b32encode(b"12345678901234567890").decode()
    check(totp.code_at(rfc_secret, 59) == "287082", "2FA: code matches the RFC 6238 test vector")
    sec = totp.new_secret()
    db.set_totp(admin["id"], sec, True)
    good = totp.code_at(sec)
    check(db.verify_totp(admin["id"], good), "2FA: valid code accepted")
    check(not db.verify_totp(admin["id"], good), "2FA: the same code can't be used twice")
    check(not db.verify_totp(admin["id"], "000000"), "2FA: wrong code rejected")
    db.update_user(admin["id"], reset_2fa=True)
    check(db.get_user(admin["id"])["totp_enabled"] is False, "2FA: admin can reset someone's 2FA")

    # 10c. AI usage limits (cost control) -------------------------------------------------------------
    os.environ["DAILY_AI_CALLS_PER_USER"] = "2"
    db.record_usage(viewer["id"], 100, 50)
    db.record_usage(viewer["id"], 10, 5)
    try:
        db.check_limit(viewer["id"])
        check(False, "usage limit blocks after the daily cap")
    except core.LimitReached:
        check(True, "usage limit blocks after the daily cap")
    core.current_user_id.set(viewer["id"])
    try:
        asyncio.run(core.make_plan("q", df, None))
        check(False, "AI requests are refused once the cap is reached")
    except core.LimitReached:
        check(True, "AI requests are refused once the cap is reached")
    soft = asyncio.run(core.describe_report(df, "pos.csv"))
    check(bool(soft["what_it_is"]), "upload still works (plain description) when the AI cap is reached")
    core.current_user_id.set(None)
    os.environ["DAILY_AI_CALLS_PER_USER"] = "150"
    check(db.usage_today()[0]["calls"] >= 2, "usage is recorded per user")

    # 10d. branded PDF ---------------------------------------------------------------------------------
    from PIL import Image

    import report_pdf

    logo = os.path.join(db.DATA_DIR, "logo.png")
    Image.new("RGBA", (200, 80), (43, 89, 255, 255)).save(logo)
    db.set_branding(c1["id"], "ACME Bank", "#d62828", logo)
    brand = db.get_branding(c1["id"])
    check(brand["display_name"] == "ACME Bank" and brand["color"] == "#d62828", "branding saved")
    chart_bytes = core.make_chart_png(res, keys, metric, "bar", "t").getvalue()
    pdf = report_pdf.build_pdf("ACME", "Amount by branch", "total by branch", "- Lagos is highest\n- Abuja second",
                               res.head(40), chart_bytes, brand, len(res))
    check(pdf[:4] == b"%PDF" and len(pdf) > 3000, "branded PDF (logo, colour, chart, table) is generated")

    # 10e. ATM cash forecast -----------------------------------------------------------------------------
    import forecast

    frows = []
    for atm, base in (("ATM1", 1000), ("ATM2", 500), ("ATM3", 200)):
        for day in pd.date_range("2026-01-01", periods=40):
            for _ in range(5):
                frows.append({"ATM": atm, "Date": day, "Amount": base / 5 * (1.5 if day.dayofweek == 4 else 1.0),
                              "Status": "success"})
    fdf = pd.DataFrame(frows)
    ftable, finfo = forecast.forecast(fdf, 7, 8000, 0.15)
    f_col = "Forecast next 7d"
    check(len(ftable) == 3 and abs(float(ftable[ftable["ATM"] == "ATM1"][f_col].iloc[0]) - 7500) < 1, "forecast: ATM1 next 7 days = 7,500")
    check(abs(float(ftable[ftable["ATM"] == "ATM1"].iloc[0]["Suggested cash (+15%)"]) - 8625) < 1, "forecast: suggested cash includes the 15% buffer")
    check(ftable.iloc[0]["ATM"] == "ATM1" and int(ftable.iloc[0]["Days of cash left"]) == 7, "forecast: ATM that runs out first is listed first (7 days)")
    try:
        forecast.forecast(fdf[fdf["Date"] < "2026-01-04"], 7)
        check(False, "forecast refuses too little history")
    except ValueError:
        check(True, "forecast refuses too little history")

    # 10f. scheduled digests ---------------------------------------------------------------------------------
    import datetime as dt

    did = db.add_digest(c1["id"], admin["id"], "total by branch", 0, 8, 30, "latest")  # Mondays 08:30
    monday = dt.datetime(2026, 10, 12, 9, 0)
    check([d["id"] for d in db.due_digests(monday)] == [did], "digest: due on Monday after 08:30")
    check(db.due_digests(dt.datetime(2026, 10, 12, 8, 0)) == [], "digest: not due before its time")
    check(db.due_digests(dt.datetime(2026, 10, 13, 9, 0)) == [], "digest: not due on another weekday")
    db.mark_digest_run(did, "2026-10-12")
    check(db.due_digests(monday) == [], "digest: runs only once per day")
    check(db.list_digests(c1["id"])[0]["question"] == "total by branch", "digest question stored (encrypted) and reloaded")

    # 10g. backup + restore ------------------------------------------------------------------------------------
    import sqlite3
    import zipfile

    import backup

    bpath = backup.create_backup(dest_dir=tempfile.mkdtemp(), data_dir=db.DATA_DIR)
    if ENCRYPTED:
        check(bpath.endswith(".enc") and not zipfile.is_zipfile(bpath), "backup file is encrypted")
    restored = os.path.join(tempfile.mkdtemp(), "restored")
    backup.restore(bpath, restored)
    con = sqlite3.connect(os.path.join(restored, "reports.db"))
    check(con.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 3 and len(os.listdir(os.path.join(restored, "files"))) >= 1,
          "backup restores the database and report files")
    con.close()

    # 10h. automatic intake (email) --------------------------------------------------------------------------------
    from email.message import EmailMessage

    import ingest
    import intake

    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Reports <reports@acme.com>", "in@x.com", "daily", "<abc@x>"
    m["Authentication-Results"] = "mx.google.com; dkim=pass header.i=@acme.com; spf=pass"
    m.set_content("see attached")
    m.add_attachment(csv_bytes, maintype="application", subtype="octet-stream", filename="daily.csv")
    info = intake.extract(m.as_bytes())
    senders = {"reports@acme.com": "ACME"}
    check(info["sender"] == "reports@acme.com" and info["auth_ok"] and info["attachments"][0][0] == "daily.csv", "intake: email parsed")
    check(intake.decide(info, senders)[0] == "ACME", "intake: allowed sender accepted")
    check(intake.decide(dict(info, auth_ok=False), senders)[0] is None, "intake: forged sender (no SPF/DKIM) rejected")
    check(intake.decide(dict(info, sender="evil@x.com"), senders)[0] is None, "intake: unknown sender rejected")
    check(intake.find_company("acme")["id"] == c1["id"], "intake: company found by name")
    got = asyncio.run(ingest.ingest(c1["id"], "daily.csv", csv_bytes, "email", "reports@acme.com"))
    check(db.get_report(got["report_id"]) is not None and db.list_audit(1)[0]["source"] == "email", "intake: report imported and audited")
    db.mark_seen("k1")
    check(db.intake_seen("k1") and not db.intake_seen("k2"), "intake: duplicates are remembered")

    # 10i. OCR layout (word boxes -> table rows) ---------------------------------------------------------------------
    words = [dict(left=10, top=10, width=40, height=12, text="Date"), dict(left=100, top=10, width=50, height=12, text="Branch"),
             dict(left=300, top=10, width=60, height=12, text="Amount"),
             dict(left=10, top=40, width=70, height=12, text="2026-01-05"), dict(left=100, top=41, width=40, height=12, text="Lagos"),
             dict(left=145, top=41, width=45, height=12, text="Island"), dict(left=300, top=40, width=40, height=12, text="1,200")]
    check(core._ocr_words_to_rows(words) == [["Date", "Branch", "Amount"], ["2026-01-05", "Lagos Island", "1,200"]],
          "OCR: word boxes grouped into table rows and cells")

    # 11. web API (only if fastapi + httpx are installed) ----------------------------------------------
    try:
        from fastapi.testclient import TestClient

        import web
    except ImportError:
        print("SKIP  web API (fastapi/httpx not installed)")
        print("\nAll checks passed.")
        return

    async def fake_plan(q, frame, prev):
        return PLAN

    async def fake_summary(q, r):
        return "- test insight"

    core.make_plan, core.summarize = fake_plan, fake_summary

    def login(username, password):
        cl = TestClient(web.app)
        r = cl.post("/api/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return cl

    anon = TestClient(web.app)
    check(anon.get("/api/companies").status_code == 401, "web: not logged in -> 401")
    check(anon.post("/api/login", json={"username": "boss", "password": "nope-nope-nope"}).status_code == 401, "web: bad login -> 401")
    adm, an, vw = login("boss", "boss-password-1"), login("ana", "analyst-pass-1"), login("vic", "viewer-pass-12")

    check([c["id"] for c in an.get("/api/companies").json()] == [c1["id"]], "web: analyst sees only her company")
    check(an.get(f"/api/companies/{c2['id']}/reports").status_code == 403, "web: other company's reports -> 403")
    up_file = {"file": ("m.csv", csv_bytes)}
    check(vw.post(f"/api/companies/{c1['id']}/reports", files=up_file).status_code == 403, "web: viewer can't upload")
    check(an.post(f"/api/companies/{c2['id']}/reports", files=up_file).status_code == 403, "web: analyst can't upload to other company")
    up = an.post(f"/api/companies/{c1['id']}/reports", files={"file": ("m.csv", csv_bytes)})
    check(up.status_code == 200 and up.json()["description"]["what_it_is"], "web: upload returns 'what this report is about'")
    rid = up.json()["id"]
    check(vw.get(f"/api/reports/{rid}/describe").status_code == 200, "web: viewer can read the description")
    an_res = an.post("/api/analyze", json={"company_id": c1["id"], "report_id": rid, "question": "x"})
    check(an_res.status_code == 200 and an_res.json()["insight"] == "- test insight", "web: analyze returns table + insight")
    rid_dl = an_res.json()["result_id"]
    check(vw.get(f"/api/download/{rid_dl}").status_code == 404, "web: another user can't download my result")
    dl = an.get(f"/api/download/{rid_dl}")
    check(dl.status_code == 200 and dl.content[:2] == b"PK", "web: Excel download")
    check(vw.post("/api/analyze", json={"company_id": c2["id"], "report_id": None, "question": "x"}).status_code in (200, 400), "web: viewer can analyze own companies")
    check(an.post("/api/analyze", json={"company_id": c2["id"], "report_id": None, "question": "x"}).status_code == 403, "web: analyze other company -> 403")
    check(an.delete(f"/api/reports/{rid}").status_code == 403, "web: only admin deletes reports")
    check(vw.get("/api/users").status_code == 403 and an.get("/api/audit").status_code == 403, "web: admin endpoints locked for others")
    check(adm.get("/api/users").status_code == 200, "web: admin lists users")
    check(adm.patch(f"/api/users/{admin['id']}", json={"role": "viewer"}).status_code == 400, "web: admin can't demote self")
    nu = adm.post("/api/users", json={"username": "newbie", "password": "newbie-pass-12", "role": "viewer", "company_ids": [c1["id"]]})
    check(nu.status_code == 200, "web: admin creates user")
    customer_response = adm.post("/api/customers", json={"company_name": "Pilot Customer Ltd", "username": "pilotuser", "password": "pilot-password-123", "telegram_id": 333})
    check(customer_response.status_code == 200 and customer_response.json()["role"] == "analyst",
          "web: admin creates a customer with its own company")
    pilot = db.get_user_by_telegram(333)
    pilot_full = next(u for u in db.list_users() if u["username"] == "pilotuser")
    check(pilot is not None and db.can_access(pilot, pilot_full["company_ids"][0]) and not db.can_access(pilot, c1["id"]),
          "web: customer account cannot access existing companies")
    check(an.post("/api/customers", json={"company_name": "Nope Ltd", "username": "badcustomer", "password": "pilot-password-123"}).status_code == 403,
          "web: non-admin cannot create customers")
    check(adm.post("/api/customers", json={"company_name": "Pilot Customer Ltd", "username": "pilotuser2", "password": "pilot-password-123"}).status_code == 400,
          "web: duplicate customer company rejected")
    check(any(r["action"] == "upload" for r in adm.get("/api/audit").json()), "web: audit log shows uploads")

    ra = an.post(f"/api/companies/{c1['id']}/reports", files={"file": ("a.csv", b"Ref,Amount\n1,10\n2,20\n3,30\n")}).json()["id"]
    rb = an.post(f"/api/companies/{c1['id']}/reports", files={"file": ("b.csv", b"Ref,Amount\n1,10\n2,25\n4,40\n")}).json()["id"]
    rc = an.post("/api/reconcile", json={"company_id": c1["id"], "report_a": ra, "report_b": rb})
    summ = {r["Status"]: r["Count"] for r in rc.json()["summary"]} if rc.status_code == 200 else rc.text
    check(summ == {"Matched": 1, "Amount mismatch": 1, "Only in A": 1, "Only in B": 1}, "web: reconcile two reports")

    for _ in range(5):
        anon.post("/api/login", json={"username": "boss", "password": "wrong-wrong-wrong"})
    check(anon.post("/api/login", json={"username": "boss", "password": "boss-password-1"}).status_code == 429, "web: login throttled after repeated failures")

    # ---- new web features ----
    check(anon.get("/api/health").json() == {"ok": True}, "web: health check is public")
    check(adm.get("/api/usage").status_code == 200 and an.get("/api/usage").status_code == 403, "web: usage visible to admin only")
    check(adm.post("/api/backup").status_code == 200, "web: admin can back up now")
    pdf_dl = an.get(f"/api/download/{rid_dl}/pdf")
    check(pdf_dl.status_code == 200 and pdf_dl.content[:4] == b"%PDF", "web: PDF download")
    check(vw.get(f"/api/download/{rid_dl}/pdf").status_code == 404, "web: another user can't download my PDF")

    fup = an.post(f"/api/companies/{c1['id']}/reports", files={"file": ("atm.csv", fdf.to_csv(index=False).encode())})
    fc = an.post("/api/forecast", json={"company_id": c1["id"], "report_id": fup.json()["id"], "days": 7, "cash": 8000})
    check(fc.status_code == 200 and fc.json()["total_rows"] == 3 and fc.json()["chart"], "web: ATM cash forecast")

    dg = an.post(f"/api/companies/{c1['id']}/digests", json={"question": "total by branch", "weekday": 0, "time": "08:30"})
    check(dg.status_code == 200 and vw.post(f"/api/companies/{c1['id']}/digests", json={"question": "x y z", "weekday": 0, "time": "08:30"}).status_code == 403,
          "web: analyst adds a digest, viewer can't")
    check(an.post(f"/api/companies/{c1['id']}/digests", json={"question": "abc", "weekday": 9, "time": "25:99"}).status_code == 400, "web: bad digest schedule rejected")
    check(len(an.get(f"/api/companies/{c1['id']}/digests").json()) >= 1 and an.delete(f"/api/digests/{dg.json()['id']}").status_code == 200, "web: digests list + delete")

    buf = io.BytesIO()
    Image.new("RGB", (120, 60), (200, 30, 30)).save(buf, "PNG")
    brand_put = adm.put(f"/api/companies/{c1['id']}/branding", data={"display_name": "ACME Bank", "color": "#d62828"},
                        files={"logo": ("l.png", buf.getvalue(), "image/png")})
    check(brand_put.status_code == 200 and adm.get(f"/api/companies/{c1['id']}/branding").json()["has_logo"], "web: admin sets company branding + logo")
    check(an.put(f"/api/companies/{c1['id']}/branding", data={"display_name": "x", "color": "#000000"}).status_code == 403, "web: only admin sets branding")
    check(adm.put(f"/api/companies/{c1['id']}/branding", data={"display_name": "x", "color": "red"}).status_code == 400, "web: bad colour rejected")
    check(adm.put(f"/api/companies/{c1['id']}/branding", data={"display_name": "x", "color": "#000000"},
                  files={"logo": ("evil.png", b"<script>alert(1)</script>", "image/png")}).status_code == 400, "web: a fake image is refused")

    tfa_user = db.create_user("tfauser", "tfa-password-12", "analyst", company_ids=[c1["id"]])
    tsec = totp.new_secret()
    db.set_totp(tfa_user["id"], tsec, True)
    t_client = TestClient(web.app)
    first = t_client.post("/api/login", json={"username": "tfauser", "password": "tfa-password-12"})
    check(first.status_code == 401 and first.json().get("need_code") is True, "web: login asks for the 2FA code")
    tcode = totp.code_at(tsec)
    check(t_client.post("/api/login", json={"username": "tfauser", "password": "tfa-password-12", "code": tcode}).status_code == 200, "web: login works with the right 2FA code")
    check(TestClient(web.app).post("/api/login", json={"username": "tfauser", "password": "tfa-password-12", "code": tcode}).status_code == 401, "web: the same 2FA code can't be replayed")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
