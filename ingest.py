"""
ingest.py - the same pipeline as a manual upload, for reports that arrive automatically.
Reads the file, masks card data, describes it, saves it (encrypted), logs it, and tells the company's users.
"""
import asyncio

import core
import db
import notify


async def ingest(company_id: int, filename: str, data: bytes, source: str, sender: str | None = None) -> dict:
    if filename.lower().rsplit(".", 1)[-1] not in core.ALLOWED_EXT:
        raise ValueError(f"Unsupported file type: {filename}")
    if len(data) > core.MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"{filename} is larger than {core.MAX_FILE_MB} MB")

    df, notes, _ = await asyncio.to_thread(core.load_report, data, filename)
    desc = await core.describe_report(df, filename)
    rid = await asyncio.to_thread(db.save_upload, company_id, filename, data, df, None)
    db.save_description(rid, desc)
    db.audit(None, "upload", company_id, f"#{rid} {filename} ({len(df)} rows) via {source}"
             + (f" from {sender}" if sender else ""), source)

    company = db.get_company(company_id)["name"]
    lines = [f"📥 {company}: new report received by {source}", f"{filename} - {desc['what_it_is']}"]
    lines += [f"• {a['text']}" for a in desc.get("alerts", []) if a["severity"] in ("high", "medium")][:5]
    await asyncio.to_thread(notify.alert_company, company_id, "\n".join(lines), None)
    return {"report_id": rid, "rows": len(df), "notes": notes}
