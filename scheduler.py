"""
scheduler.py - background jobs that run inside the web process:
  retention   delete old reports / audit rows (RETENTION_DAYS)
  backup      nightly encrypted backup; admins get a Telegram message if it fails
  intake      pull reports from the email inbox / SFTP folder (see intake.py)
  digests     run saved questions on a schedule and send the result + PDF to Telegram
  watchdog    tell admins on Telegram if the bot stops sending its heartbeat
Times use TIMEZONE (default UTC), e.g. TIMEZONE=Africa/Lagos
"""
import asyncio
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

import backup
import core
import db
import intake
import notify
import recon
import report_pdf

log = logging.getLogger("scheduler")
_alerted: set[str] = set()


def local_tz():
    try:
        return ZoneInfo(os.getenv("TIMEZONE", "UTC"))
    except Exception:  # noqa: BLE001  (unknown zone name, or tzdata missing on Windows)
        return ZoneInfo("UTC")


async def _admin_alert(key: str, text: str) -> None:
    """Message admins once per problem (cleared when the problem is fixed)."""
    if key not in _alerted:
        _alerted.add(key)
        await asyncio.to_thread(notify.alert_admins, text)


async def retention_loop():
    while True:
        try:
            removed = await asyncio.to_thread(db.purge_expired)
            if any(removed.values()):
                log.info("Retention purge: %s", removed)
        except Exception:  # noqa: BLE001
            log.exception("retention purge failed")
        await asyncio.sleep(24 * 3600)


async def backup_loop():
    if os.getenv("BACKUP_ENABLED", "1") == "0":
        return
    while True:
        try:
            age = await asyncio.to_thread(backup.latest_age_hours)
            if age is None or age >= 24:
                path = await asyncio.to_thread(backup.create_backup)
                log.info("Backup written: %s", path)
                if os.getenv("BACKUP_S3_BUCKET"):
                    result = await asyncio.to_thread(backup.upload_offsite, path)
                    log.info("Off-site backup uploaded: s3://%s/%s", result["bucket"], result["key"])
                _alerted.discard("backup_failed")
        except Exception as e:  # noqa: BLE001
            log.exception("backup failed")
            await _admin_alert("backup_failed", f"🔴 The nightly backup FAILED: {e}")
        await asyncio.sleep(3600)


async def intake_loop():
    if not intake.configured():
        return
    minutes = max(int(os.getenv("INTAKE_INTERVAL_MIN", "10")), 1)
    while True:
        try:
            n = await intake.run_once()
            if n:
                log.info("Intake imported %d report(s)", n)
            _alerted.discard("intake_failed")
        except Exception as e:  # noqa: BLE001
            log.exception("intake failed")
            await _admin_alert("intake_failed", f"🔴 Automatic report intake is failing: {e}")
        await asyncio.sleep(minutes * 60)


async def run_digest(d: dict) -> None:
    core.current_user_id.set(d["user_id"])  # AI usage counts against whoever created the digest
    company = db.get_company(d["company_id"])
    if d["scope"] == "all":
        frame = await asyncio.to_thread(db.resolve_frame, d["company_id"], None)
    else:
        latest = db.latest_report(d["company_id"])
        if not latest:
            return
        frame = await asyncio.to_thread(db.get_frame, latest["id"])
    out = await core.analyze(frame, d["question"], None, True)
    if out.get("clarify"):
        return
    res = out["res"]
    table = res.head(15).to_string(index=False, max_colwidth=22, float_format=lambda x: f"{x:,.2f}")
    text = f"🗓 {company['name']} - {out['title']}\n\n{table}" + (f"\n\n{out['insight']}" if out["insight"] else "")
    chart = await asyncio.to_thread(core.make_chart_png, res, out["keys"], out["metric"],
                                    out["plan"].get("chart", "bar"), out["title"])
    pdf = await asyncio.to_thread(report_pdf.build_pdf, company["name"], out["title"], d["question"], out["insight"],
                                  res.head(40), chart.getvalue() if chart else None,
                                  db.get_branding(company["id"]), len(res))
    for u in db.users_to_alert(company["id"]):
        await asyncio.to_thread(notify.send_telegram, u["telegram_id"], text)
        await asyncio.to_thread(notify.send_document, u["telegram_id"], pdf, "digest.pdf", out["title"])
    db.audit(None, "digest", company["id"], d["question"][:200], "scheduler")


async def digest_loop():
    while True:
        await asyncio.sleep(60)
        try:
            now = dt.datetime.now(local_tz())
            for d in await asyncio.to_thread(db.due_digests, now):
                db.mark_digest_run(d["id"], now.strftime("%Y-%m-%d"))  # first, so a crash can't repeat-spam
                try:
                    await run_digest(d)
                except Exception as e:  # noqa: BLE001
                    log.exception("digest %s failed", d["id"])
                    db.audit(None, "digest_error", d["company_id"], f"{d['question'][:100]}: {e}", "scheduler")
        except Exception:  # noqa: BLE001
            log.exception("digest loop error")


async def watchdog_loop():
    while True:
        await asyncio.sleep(60)
        try:
            beat = await asyncio.to_thread(db.last_beat, "bot")
            if beat is None:  # the bot has never run: nothing to watch
                continue
            age = (dt.datetime.now(dt.timezone.utc) - beat).total_seconds()
            if age > 300:
                await _admin_alert("bot_down", "🔴 The Telegram bot seems to be DOWN (no heartbeat for 5+ minutes).")
            elif age < 120 and "bot_down" in _alerted:
                _alerted.discard("bot_down")
                await asyncio.to_thread(notify.alert_admins, "🟢 The Telegram bot is back online.")
        except Exception:  # noqa: BLE001
            log.exception("watchdog error")


def start() -> list[asyncio.Task]:
    return [asyncio.create_task(f()) for f in
            (retention_loop, backup_loop, intake_loop, digest_loop, watchdog_loop, scheduled_reconciliation_loop, subscription_reminder_loop, expiry_reminder_loop)]


async def scheduled_reconciliation_loop():
    """Run due reconciliation pairs on a schedule and persist case/history records."""
    import enterprise_features as ef
    while True:
        await asyncio.sleep(60)
        try:
            for job in await asyncio.to_thread(ef.due_schedules):
                reservation = None
                try:
                    ra, rb = db.get_report(job["report_a"]), db.get_report(job["report_b"])
                    if not ra or not rb or ra["company_id"] != job["company_id"] or rb["company_id"] != job["company_id"]:
                        raise ValueError("A scheduled report was deleted or no longer belongs to the company.")
                    reservation = await asyncio.to_thread(db.reserve_monthly_usage, job["company_id"], job["created_by"], "scheduled_reconciliation")
                    a = await asyncio.to_thread(db.get_frame, job["report_a"])
                    b = await asyncio.to_thread(db.get_frame, job["report_b"])
                    result = await recon.reconcile(a, b, job.get("instruction", ""))
                    if result.get("clarify"):
                        raise ValueError("The mapping needs review: " + str(result["clarify"]))
                    run_id, cases = await asyncio.to_thread(ef.record_reconciliation, job["company_id"], job["report_a"], job["report_b"], result, job["created_by"], "scheduled")
                    await asyncio.to_thread(db.finish_monthly_usage, reservation, True)
                    reservation = None
                    summary = result.get("summary")
                    problem_count = int((result["detail"]["Status"] != "Matched").sum())
                    company = db.get_company(job["company_id"])
                    message = f"🗓 Scheduled reconciliation complete for {company['name'] if company else 'company'}\nRun #{run_id}: {problem_count} discrepancy row(s), {cases} case(s) created."
                    await asyncio.to_thread(notify.alert_company, job["company_id"], message)
                    await asyncio.to_thread(notify.email_company, job["company_id"], "Report Analyzer scheduled reconciliation", message)
                    await asyncio.to_thread(db.audit, None, "scheduled_reconcile", job["company_id"], f"job={job['id']}; run={run_id}", "scheduler")
                    await asyncio.to_thread(ef.finish_schedule, job["id"], "completed", "")
                except Exception as e:  # noqa: BLE001
                    if reservation is not None:
                        await asyncio.to_thread(db.finish_monthly_usage, reservation, False)
                    log.exception("scheduled reconciliation job %s failed", job["id"])
                    await asyncio.to_thread(ef.finish_schedule, job["id"], "failed", str(e))
                    await asyncio.to_thread(notify.alert_company, job["company_id"], f"⚠️ Scheduled reconciliation job #{job['id']} failed: {str(e)[:300]}")
                    await asyncio.to_thread(db.audit, None, "scheduled_reconcile_error", job["company_id"], f"job={job['id']}: {str(e)[:300]}", "scheduler")
        except Exception:  # noqa: BLE001
            log.exception("scheduled reconciliation loop error")


async def expiry_reminder_loop():
    """In-app + email reminders 72h / 24h before a plan ends, and when the grace period starts."""
    import extras
    await asyncio.sleep(60)
    while True:
        try:
            n = await asyncio.to_thread(extras.run_expiry_reminders)
            if n:
                log.info("Sent %s expiry reminder(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("expiry reminders failed")
        await asyncio.sleep(3600)


async def subscription_reminder_loop():
    """Send each company a once-per-subscription 72-hour expiry reminder."""
    import enterprise_features as ef
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            cutoff = (now + dt.timedelta(hours=72)).strftime(db.FMT)
            current = now.strftime(db.FMT)
            with db._db() as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT s.id,s.company_id,s.plan,s.expires_at,c.name AS company_name FROM subscriptions s "
                    "JOIN companies c ON c.id=s.company_id WHERE s.status='active' AND s.expires_at>? AND s.expires_at<=?",
                    (current, cutoff)).fetchall()]
            for sub in rows:
                key = f"subscription:{sub['id']}:72h"
                if not await asyncio.to_thread(ef.log_notification_once, sub["company_id"], key):
                    continue
                message = (f"⏰ Report Analyzer subscription reminder: {sub['company_name']} plan "
                           f"{sub['plan'].upper()} expires at {sub['expires_at']} UTC. Please arrange renewal before expiry.")
                await asyncio.to_thread(notify.alert_company, sub["company_id"], message)
                await asyncio.to_thread(notify.email_company, sub["company_id"], "Report Analyzer subscription renewal reminder", message)
                await asyncio.to_thread(db.audit, None, "renewal_reminder", sub["company_id"], f"subscription={sub['id']}; expires={sub['expires_at']}", "scheduler")
        except Exception:  # noqa: BLE001
            log.exception("subscription reminder loop failed")
        await asyncio.sleep(6 * 3600)
