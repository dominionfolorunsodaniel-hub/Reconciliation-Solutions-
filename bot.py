"""
bot.py - Telegram front end.

Only users linked by an admin (dashboard -> Admin -> Telegram ID) can use it. Unknown users are
told their Telegram ID so they can send it to the admin.

Env: TELEGRAM_TOKEN, ANTHROPIC_API_KEY, ENCRYPTION_KEY (same as the web app, shared DATA_DIR)
"""
import asyncio
import functools
import html
import io
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

import anomaly
import core
import db
import forecast
import notify
import recon
import report_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("report_bot")

INSIGHTS = os.getenv("AUTO_INSIGHTS", "1") != "0"

HELP = (
    "Send me a report (CSV, Excel or PDF), I'll tell you what it is, then ask anything:\n"
    "• total amount by branch and month\n"
    "• failed transactions per ATM, top 10\n\n"
    "/company NAME - switch company   /companies - list yours\n"
    "/reports - saved reports   /use ID - pick one   /all - combine them all\n"
    "/describe - what the current report is about   /alerts - check for anomalies\n"
    "/recon A B - reconcile two reports (e.g. /recon 3 4)\n"
    "/history - recent questions   /columns - detected columns   /sheet NAME - Excel sheet\n"
    "/forecast [days] [cash] - ATM cash forecast & refill dates   /pdf - branded PDF of the last answer\n"
    "/digests - scheduled digests   /digest add MON 08:00 [all] question   /digest delete ID\n"
    "/delete ID - remove a report (admin)   /audit - recent activity (admin)\n"
    "/usage - today's AI usage (admin)   /whoami"
)


# ------------------------------ access control ----------------------------- #
def state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    ud = context.user_data
    allowed = [c["id"] for c in db.list_companies(ud["user"])]
    if ud.get("company_id") not in allowed:
        ud["company_id"] = allowed[0] if allowed else None
        ud["report_id"] = None
    return ud


def guard(role: str = "viewer", company: bool = True):
    """Only linked + active users, with at least `role`, and (optionally) a company assigned."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            msg, tg = update.effective_message, update.effective_user
            if update.callback_query:
                await update.callback_query.answer()
            user = db.get_user_by_telegram(tg.id) if tg else None
            if not user or not user["active"]:
                await msg.reply_text(
                    f"🔒 You're not authorized yet.\nSend this Telegram ID to your admin: {tg.id if tg else '?'}")
                return
            if db.ROLE_RANK[user["role"]] < db.ROLE_RANK[role]:
                await msg.reply_text("🚫 Your role doesn't allow that.")
                return
            context.user_data["user"] = user
            core.current_user_id.set(user["id"])  # AI usage is counted per user
            ud = state(context)
            if company and ud["company_id"] is None:
                await msg.reply_text("No company is assigned to you yet - ask your admin.")
                return
            return await fn(update, context)
        return wrapper
    return deco


def pre(text: str) -> str:
    return f"<pre>{html.escape(text[:3500])}</pre>"


def suggestion_keyboard(questions):
    rows = [[InlineKeyboardButton(q[:60], callback_data=f"q:{i}")] for i, q in enumerate(questions)]
    return InlineKeyboardMarkup(rows) if rows else None


def current_frame(ud):
    rid = ud.get("report_id")
    if rid is None:
        if not db.list_reports(ud["company_id"]):
            raise ValueError("No reports yet - send me a file first.")
        return db.resolve_frame(ud["company_id"], None)
    return db.resolve_frame(ud["company_id"], None if rid == "all" else rid)


def own_report(ud, raw: str):
    rid = int(raw)
    r = db.get_report(rid)
    if not r or r["company_id"] != ud["company_id"]:
        raise ValueError
    return r


# --------------------------------- commands -------------------------------- #
@guard(company=False)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 " + HELP)


@guard(company=False)
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data["user"]
    names = ", ".join(c["name"] for c in db.list_companies(u)) or "none"
    await update.message.reply_text(f"{u['username']} - role: {u['role']}\nCompanies: {names}\n"
                                    f"Your Telegram ID: {update.effective_user.id}")


@guard(company=False)
async def cmd_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_companies(context.user_data["user"])
    await update.message.reply_text("\n".join(f"• {c['name']} ({c['reports']} reports)" for c in rows) or "None assigned yet.")


@guard(company=False)
async def cmd_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, user = context.user_data, context.user_data["user"]
    name = " ".join(context.args).strip()
    mine = db.list_companies(user)
    if not name:
        cur = next((c["name"] for c in mine if c["id"] == ud.get("company_id")), "none")
        await update.message.reply_text(f"Current company: {cur}\nSwitch with /company NAME")
        return
    match = next((c for c in mine if c["name"].lower() == name.lower()), None)
    if match is None:
        if user["role"] != "admin":
            await update.message.reply_text("You don't have access to a company with that name. See /companies.")
            return
        match = db.get_or_create_company(name)
        db.audit(user, "create_company", match["id"], match["name"], "telegram")
    reports = db.list_reports(match["id"])
    ud.update(company_id=match["id"], report_id=reports[-1]["id"] if reports else None, plan=None)
    await update.message.reply_text(
        f"🏢 {match['name']} - {len(reports)} saved report(s)."
        + (f" Using the latest (#{reports[-1]['id']})." if reports else " Send a file to start."))


@guard()
async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    reports = db.list_reports(ud["company_id"])
    if not reports:
        await update.message.reply_text("No reports saved for this company yet.")
        return
    lines = [f"#{r['id']}  {r['filename']}  ({r['rows']:,} rows, {r['uploaded_at'][:10]})" for r in reports]
    cur = "all reports combined" if ud.get("report_id") == "all" else f"#{ud.get('report_id')}"
    await update.message.reply_text(pre("\n".join(lines)) + f"\nAnalyzing: {cur}\n/use ID  or  /all",
                                    parse_mode=ParseMode.HTML)


@guard()
async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    try:
        r = own_report(ud, context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /use ID (see /reports)")
        return
    ud.update(report_id=r["id"], plan=None)
    await update.message.reply_text(f"✅ Now analyzing #{r['id']} {r['filename']}")


@guard()
async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    ud.update(report_id="all", plan=None)
    await update.message.reply_text(
        "✅ Analyzing ALL reports of this company combined. Use the _report and _uploaded columns "
        "to compare them, e.g. 'revenue by branch for each report'.")


@guard(role="admin")
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    try:
        r = own_report(ud, context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /delete ID (see /reports)")
        return
    db.delete_report(r["id"])
    db.audit(ud["user"], "delete_report", ud["company_id"], f"#{r['id']} {r['filename']}", "telegram")
    if ud.get("report_id") == r["id"]:
        ud["report_id"] = None
    await update.message.reply_text(f"🗑 Deleted #{r['id']}.")


@guard()
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    rows = db.list_queries(ud["company_id"], 10)
    await update.message.reply_text("\n".join(f"{r['created_at'][:16]}  {r['question']}" for r in rows)
                                    or "No questions asked yet.")


@guard(role="admin", company=False)
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_audit(15)
    text = "\n".join(f"{r['ts'][5:16]} {r['username'] or '-'} {r['action']} {r['detail'] or ''}"[:110] for r in rows)
    await update.message.reply_text(pre(text or "Nothing logged yet."), parse_mode=ParseMode.HTML)


@guard()
async def cmd_columns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    try:
        df = await asyncio.to_thread(current_frame, ud)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    await update.message.reply_text(pre(core.describe_schema(df)), parse_mode=ParseMode.HTML)


@guard(role="analyst")
async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, sheet = state(context), " ".join(context.args).strip()
    rid = ud.get("report_id")
    if not isinstance(rid, int) or not sheet:
        await update.message.reply_text("Select a single report (/use ID) then: /sheet SheetName")
        return
    db.set_sheet(rid, sheet)
    try:
        df = await asyncio.to_thread(db.get_frame, rid)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"❌ {e}")
        return
    await update.message.reply_text(f"Switched sheet. {len(df):,} rows × {len(df.columns)} columns.")


@guard()
async def cmd_describe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    rid = ud.get("report_id")
    if not isinstance(rid, int):
        await update.message.reply_text("Pick a single report first: /reports then /use ID")
        return
    desc = db.get_description(rid)
    if desc is None:
        desc = await core.describe_report(await asyncio.to_thread(db.get_frame, rid), db.get_report(rid)["filename"])
        db.save_description(rid, desc)
    ud["suggestions"] = desc["suggested_questions"][:5]
    await update.message.reply_text(core.description_text(desc), reply_markup=suggestion_keyboard(ud["suggestions"]))


@guard()
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    try:
        df = await asyncio.to_thread(current_frame, ud)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    found = await asyncio.to_thread(anomaly.scan, df)
    icon = {"high": "🔴", "medium": "🟠", "info": "ℹ️"}
    await update.message.reply_text("\n".join(f"{icon[a['severity']]} {a['text']}" for a in found)
                                    or "✅ No anomalies found.")


@guard()
async def cmd_recon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, message = state(context), update.message
    try:
        ra, rb = own_report(ud, context.args[0]), own_report(ud, context.args[1])
    except (IndexError, ValueError):
        await message.reply_text("Usage: /recon A B [note]  e.g. /recon 3 4\n(IDs from /reports; both must be in this company)")
        return
    note = " ".join(context.args[2:])
    status = await message.reply_text("⚖️ Reconciling…")
    try:
        a = await asyncio.to_thread(db.get_frame, ra["id"])
        b = await asyncio.to_thread(db.get_frame, rb["id"])
        out = await recon.reconcile(a, b, note)
        if out.get("clarify"):
            await status.edit_text("🤔 " + out["clarify"])
            return
    except Exception as e:  # noqa: BLE001
        log.exception("recon failed")
        await status.edit_text(f"❌ {e}")
        return
    la, lb = f"#{ra['id']}", f"#{rb['id']}"
    await status.edit_text(pre(recon.summary_text(out, la, lb)), parse_mode=ParseMode.HTML)
    await message.reply_document(await asyncio.to_thread(recon.to_excel, out), filename="reconciliation.xlsx")
    db.audit(ud["user"], "reconcile", ud["company_id"], f"{la} vs {lb}", "telegram")


@guard()
async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/forecast [days] [cash_per_atm]  - e.g. /forecast 7 500000"""
    ud, message = state(context), update.message
    days, cash = 7, None
    try:
        if context.args:
            days = min(max(int(context.args[0]), 1), 30)
        if len(context.args) > 1:
            cash = float(context.args[1].replace(",", ""))
    except ValueError:
        await message.reply_text("Usage: /forecast [days] [cash loaded per ATM]\ne.g. /forecast 7 500000")
        return
    try:
        df = await asyncio.to_thread(current_frame, ud)
        table, info = await asyncio.to_thread(forecast.forecast, df, days, cash, 0.15)
    except ValueError as e:
        await message.reply_text(f"❌ {e}")
        return
    text = table.head(25).to_string(index=False, max_colwidth=18, float_format=lambda x: f"{x:,.2f}")
    await message.reply_text(f"📈 ATM cash forecast - next {days} days\n{info}\n" + pre(text)
                             + "\nEstimate from past withdrawals - a planning aid, not a guarantee.",
                             parse_mode=ParseMode.HTML)
    await message.reply_document(await asyncio.to_thread(core.dfs_to_excel, {"Forecast": table}), filename="forecast.xlsx")
    db.audit(ud["user"], "forecast", ud["company_id"], f"{days} days", "telegram")


@guard()
async def cmd_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, last = state(context), context.user_data.get("last")
    if not last:
        await update.message.reply_text("Ask a question first, then send /pdf to get it as a branded PDF.")
        return
    company = db.get_company(ud["company_id"])
    pdf = await asyncio.to_thread(report_pdf.build_pdf, company["name"], last["title"], last["question"],
                                  last["insight"], last["table"], last["chart"], db.get_branding(company["id"]),
                                  last["total_rows"])
    await update.message.reply_document(io.BytesIO(pdf), filename="report.pdf")
    db.audit(ud["user"], "export_pdf", ud["company_id"], last["title"][:100], "telegram")


@guard(role="admin", company=False)
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.usage_today()
    text = "\n".join(f"{r['user']:<22} {r['calls']:>4} requests  {r['tokens_in'] + r['tokens_out']:>9,} tokens" for r in rows)
    await update.message.reply_text(pre(text or "No AI usage yet today."), parse_mode=ParseMode.HTML)


DAY_CODES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6, "daily": -1}
DAY_NAMES = {v: k.upper() for k, v in DAY_CODES.items()}


@guard()
async def cmd_digests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    rows = db.list_digests(ud["company_id"])
    lines = [f"#{d['id']}  {DAY_NAMES[d['weekday']]} {d['hour']:02d}:{d['minute']:02d}  ({d['scope']})  {d['question']}" for d in rows]
    await update.message.reply_text(pre("\n".join(lines)) if lines else "No digests yet.\n/digest add MON 08:00 total amount by branch",
                                    parse_mode=ParseMode.HTML if lines else None)


@guard(role="analyst")
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud, a = state(context), context.args
    usage = ("/digest add MON 08:00 total amount by branch\n/digest add DAILY 07:30 all failed transactions per ATM\n"
             "(add the word 'all' after the time to use all reports; default is the latest report)\n/digest delete ID")
    if len(a) >= 2 and a[0].lower() == "delete":
        d = db.get_digest(int(a[1])) if a[1].isdigit() else None
        if not d or d["company_id"] != ud["company_id"] or (ud["user"]["role"] != "admin" and d["user_id"] != ud["user"]["id"]):
            await update.message.reply_text("I can't find a digest of yours with that ID. See /digests")
            return
        db.delete_digest(d["id"])
        await update.message.reply_text(f"🗑 Digest #{d['id']} deleted.")
        return
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", a[2]) if len(a) >= 4 and a[0].lower() == "add" else None
    if not m or a[1].lower() not in DAY_CODES:
        await update.message.reply_text(usage)
        return
    rest = a[3:]
    scope = "all" if rest and rest[0].lower() == "all" else "latest"
    question = " ".join(rest[1:] if scope == "all" else rest).strip()
    if len(question) < 3:
        await update.message.reply_text(usage)
        return
    did = db.add_digest(ud["company_id"], ud["user"]["id"], question, DAY_CODES[a[1].lower()], int(m[1]), int(m[2]), scope)
    db.audit(ud["user"], "digest_add", ud["company_id"], question[:100], "telegram")
    await update.message.reply_text(f"✅ Digest #{did} saved: {a[1].upper()} {int(m[1]):02d}:{m[2]} ({scope}).\n"
                                    "It will be sent to this company's linked Telegram users with a PDF.")


# ------------------------------- file + text ---------------------------------- #
@guard(role="analyst")
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = state(context)
    user, message, doc = ud["user"], update.message, update.message.document
    name = doc.file_name or "report.csv"
    if name.lower().rsplit(".", 1)[-1] not in core.ALLOWED_EXT:
        await message.reply_text("I can read CSV, Excel (.xlsx/.xls) and PDF files.")
        return
    if doc.file_size and doc.file_size > core.MAX_FILE_MB * 1024 * 1024:
        await message.reply_text(f"File is too large (limit {core.MAX_FILE_MB} MB).")
        return

    status = await message.reply_text("📥 Reading your report…")
    try:
        buf = io.BytesIO()
        await (await doc.get_file()).download_to_memory(buf)
        data = buf.getvalue()
        df, notes, _ = await asyncio.to_thread(core.load_report, data, name)
    except Exception as e:  # noqa: BLE001
        log.exception("load failed")
        await status.edit_text(f"❌ Couldn't read that file: {e}")
        return

    await status.edit_text("🧠 Reading through it to see what it's about…")
    desc = await core.describe_report(df, name)
    cid = ud["company_id"]
    rid = await asyncio.to_thread(db.save_upload, cid, name, data, df, user["id"])
    db.save_description(rid, desc)
    db.audit(user, "upload", cid, f"#{rid} {name} ({len(df)} rows)", "telegram")
    ud.update(report_id=rid, plan=None, suggestions=desc["suggested_questions"][:5])

    company = db.get_company(cid)["name"]
    msg = (f"✅ Saved to <b>{html.escape(company)}</b> as #{rid}: {html.escape(name)} "
           f"({len(df):,} rows × {len(df.columns)} columns)\n\n{html.escape(core.description_text(desc))}")
    if notes:
        msg += "\n\n" + "\n".join(f"ℹ️ {html.escape(n)}" for n in notes)
    msg += "\n\nTap a question below or type your own:"
    await status.edit_text(msg[:4000], parse_mode=ParseMode.HTML, reply_markup=suggestion_keyboard(ud["suggestions"]))

    push = anomaly.push_text(company, name, desc.get("alerts", []))
    if push:
        await asyncio.to_thread(notify.alert_company, cid, push, user["id"])


async def run_question(message, context: ContextTypes.DEFAULT_TYPE, question: str):
    ud = state(context)
    try:
        df = await asyncio.to_thread(current_frame, ud)
    except ValueError as e:
        await message.reply_text(str(e))
        return

    status = await message.reply_text("🔎 Analyzing…")
    try:
        out = await core.analyze(df, question, ud.get("plan"), INSIGHTS)
        if out.get("clarify"):
            await status.edit_text("🤔 " + out["clarify"])
            return
    except Exception as e:  # noqa: BLE001
        log.exception("analysis failed")
        await status.edit_text(f"❌ Couldn't run that: {e}\nTry /columns to check column names.")
        return

    ud["plan"] = out["plan"]
    rid = ud.get("report_id")
    db.log_query(ud["company_id"], rid if isinstance(rid, int) else None, question, out["plan"], ud["user"]["id"])
    db.audit(ud["user"], "analyze", ud["company_id"], question[:200], "telegram")

    res = out["res"]
    table = res.head(30).to_string(index=False, max_colwidth=26, float_format=lambda x: f"{x:,.2f}")
    if len(res) > 30:
        table += f"\n… {len(res) - 30} more rows in the Excel file"
    text = f"<b>{html.escape(out['title'])}</b>\n{pre(table)}"
    if out["insight"]:
        text += "\n" + html.escape(out["insight"])
    await status.edit_text(text[:4000], parse_mode=ParseMode.HTML)

    chart = await asyncio.to_thread(core.make_chart_png, res, out["keys"], out["metric"],
                                    out["plan"].get("chart", "bar"), out["title"])
    context.user_data["last"] = {"title": out["title"], "question": question, "insight": out["insight"],
                                 "table": res.head(40), "total_rows": len(res),
                                 "chart": chart.getvalue() if chart else None}
    if chart:
        chart.seek(0)
        await message.reply_photo(chart)
    xlsx = await asyncio.to_thread(core.make_excel, res, out["filtered"], out["plan"], out["insight"])
    await message.reply_document(xlsx, filename="analysis.xlsx")


@guard()
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_question(update.message, context, update.message.text)


@guard()
async def on_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, ud = update.callback_query, state(context)
    try:
        question = ud.get("suggestions", [])[int(query.data.split(":")[1])]
    except (IndexError, ValueError):
        await query.message.reply_text("That suggestion expired - type your question instead.")
        return
    ud["plan"] = None  # suggestions are independent questions, not follow-ups
    await query.message.reply_text(f"❓ {question}")
    await run_question(query.message, context, question)


async def _heartbeat():
    while True:  # the web app's watchdog messages admins if this stops
        try:
            db.beat("bot")
        except Exception:  # noqa: BLE001
            log.exception("heartbeat failed")
        await asyncio.sleep(60)


async def post_init(app: Application) -> None:
    app.bot_data["heartbeat"] = asyncio.create_task(_heartbeat())


def main():
    db.check_encryption()
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).post_init(post_init).build()
    for names, fn in [
        (["start", "help"], start), ("whoami", cmd_whoami), ("company", cmd_company),
        ("companies", cmd_companies), ("reports", cmd_reports), ("use", cmd_use), ("all", cmd_all),
        ("delete", cmd_delete), ("history", cmd_history), ("audit", cmd_audit),
        ("columns", cmd_columns), ("sheet", cmd_sheet), ("describe", cmd_describe),
        ("alerts", cmd_alerts), ("recon", cmd_recon), ("forecast", cmd_forecast), ("pdf", cmd_pdf),
        ("usage", cmd_usage), ("digests", cmd_digests), ("digest", cmd_digest),
    ]:
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(on_suggestion, pattern=r"^q:\d+$"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()
