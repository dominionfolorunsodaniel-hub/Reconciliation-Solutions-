"""
intake.py - pull reports in automatically so nobody has to upload by hand.

EMAIL (IMAP): a company emails its report to a mailbox you control. Only senders listed in
    INTAKE_SENDERS="reports@acme.com=ACME,ops@globex.com=Globex"
are accepted, and (INTAKE_REQUIRE_AUTH=1, default) the mail must pass SPF or DKIM so a forged
"From" address can't inject data. Settings: INTAKE_EMAIL_HOST / _PORT / _USER / _PASSWORD / _FOLDER.

SFTP: new files in a remote folder are imported for one company. Needs `pip install paramiko`.
    INTAKE_SFTP_HOST / _PORT / _USER / _PASSWORD (or _KEY_FILE) / _DIR / _COMPANY

Everything is de-duplicated, audited, and goes through the same privacy pipeline as an upload.
"""
import asyncio
import email
import email.policy
import email.utils
import logging
import os
import re
import stat

import core
import db
import ingest

log = logging.getLogger("intake")
UNSAFE = re.compile(r"[^\w.\- ]")


def configured() -> bool:
    return bool(os.getenv("INTAKE_EMAIL_HOST") or os.getenv("INTAKE_SFTP_HOST"))


def sender_map() -> dict[str, str]:
    out = {}
    for pair in os.getenv("INTAKE_SENDERS", "").split(","):
        if "=" in pair:
            addr, company = pair.split("=", 1)
            out[addr.strip().lower()] = company.strip()
    return out


def find_company(name: str) -> dict | None:
    return next((c for c in db.list_companies() if c["name"].lower() == name.lower()), None)


def extract(raw: bytes) -> dict:
    """Pull sender, authentication result and supported attachments out of a raw email."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    auth = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
    attachments = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if name and name.rsplit(".", 1)[-1].lower() in core.ALLOWED_EXT:
            payload = part.get_payload(decode=True)
            if payload:
                attachments.append((UNSAFE.sub("_", os.path.basename(name))[:100], payload))
    return {
        "sender": email.utils.parseaddr(str(msg.get("From", "")))[1].lower(),
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "auth_ok": "dkim=pass" in auth or "spf=pass" in auth,
        "attachments": attachments,
    }


def decide(info: dict, senders: dict[str, str], require_auth: bool = True) -> tuple[str | None, str]:
    company = senders.get(info["sender"])
    if not company:
        return None, f"sender not on the allowed list: {info['sender'] or '(none)'}"
    if require_auth and not info["auth_ok"]:
        return None, f"sender failed SPF/DKIM check: {info['sender']}"
    if not info["attachments"]:
        return None, "no CSV/Excel/PDF attachment"
    return company, "ok"


def _imap_connect():
    import imaplib

    conn = imaplib.IMAP4_SSL(os.environ["INTAKE_EMAIL_HOST"], int(os.getenv("INTAKE_EMAIL_PORT", "993")))
    conn.login(os.environ["INTAKE_EMAIL_USER"], os.environ["INTAKE_EMAIL_PASSWORD"])
    conn.select(os.getenv("INTAKE_EMAIL_FOLDER", "INBOX"))
    return conn


def _imap_fetch():
    senders = sender_map()
    require_auth = os.getenv("INTAKE_REQUIRE_AUTH", "1") != "0"
    items, rejected = [], []
    conn = _imap_connect()
    try:
        _, data = conn.uid("SEARCH", None, "UNSEEN")
        for uid in (data[0] or b"").split()[:50]:
            _, md = conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: stays unread until we succeed
            info = extract(md[0][1])
            company, why = decide(info, senders, require_auth)
            if not company:
                rejected.append((uid, why))
                continue
            for filename, payload in info["attachments"]:
                items.append({"company": company, "filename": filename, "data": payload, "sender": info["sender"],
                              "key": f"email:{info['message_id']}:{filename}", "uid": uid})
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return items, rejected


def _imap_mark(uids) -> None:
    if not uids:
        return
    conn = _imap_connect()
    try:
        for uid in uids:
            conn.uid("STORE", uid, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def _sftp_fetch() -> list[dict]:
    import paramiko  # optional dependency

    host, port = os.environ["INTAKE_SFTP_HOST"], int(os.getenv("INTAKE_SFTP_PORT", "22"))
    remote = os.getenv("INTAKE_SFTP_DIR", ".")
    transport = paramiko.Transport((host, port))
    try:
        key_file = os.getenv("INTAKE_SFTP_KEY_FILE")
        transport.connect(username=os.environ["INTAKE_SFTP_USER"], password=os.getenv("INTAKE_SFTP_PASSWORD"),
                          pkey=paramiko.RSAKey.from_private_key_file(key_file) if key_file else None)
        sftp = paramiko.SFTPClient.from_transport(transport)
        items = []
        for attr in sftp.listdir_attr(remote):
            ext = attr.filename.rsplit(".", 1)[-1].lower()
            if not stat.S_ISREG(attr.st_mode) or ext not in core.ALLOWED_EXT:
                continue
            if attr.st_size > core.MAX_FILE_MB * 1024 * 1024:
                continue
            key = f"sftp:{host}:{attr.filename}:{attr.st_mtime}:{attr.st_size}"
            if db.intake_seen(key):
                continue
            with sftp.open(f"{remote.rstrip('/')}/{attr.filename}", "rb") as f:
                items.append({"company": os.environ["INTAKE_SFTP_COMPANY"], "filename": UNSAFE.sub("_", attr.filename),
                              "data": f.read(), "sender": f"sftp://{host}", "key": key})
        return items
    finally:
        transport.close()


async def _process(items: list[dict], source: str) -> tuple[int, set]:
    ok, failed = 0, set()
    for it in items:
        company = find_company(it["company"])
        if not company:
            db.audit(None, "intake_error", detail=f"unknown company {it['company']!r} for {it['filename']}", source=source)
            failed.add(it.get("uid"))
            continue
        try:
            await ingest.ingest(company["id"], it["filename"], it["data"], source, it["sender"])
            db.mark_seen(it["key"])
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.exception("intake failed for %s", it["filename"])
            db.audit(None, "intake_error", company["id"], f"{it['filename']}: {e}", source)
            failed.add(it.get("uid"))
    return ok, failed


async def run_once() -> int:
    """Check the inbox / SFTP folder once. Returns the number of reports imported."""
    total = 0
    if os.getenv("INTAKE_EMAIL_HOST"):
        items, rejected = await asyncio.to_thread(_imap_fetch)
        for _, why in rejected:
            db.audit(None, "intake_rejected", detail=why, source="email")
        fresh = []
        done = {uid for uid, _ in rejected}
        for it in items:
            if db.intake_seen(it["key"]):
                done.add(it["uid"])
            else:
                fresh.append(it)
        ok, failed = await _process(fresh, "email")
        total += ok
        done |= {it["uid"] for it in fresh} - failed
        await asyncio.to_thread(_imap_mark, sorted(done - failed))
    if os.getenv("INTAKE_SFTP_HOST"):
        items = await asyncio.to_thread(_sftp_fetch)
        ok, _ = await _process(items, "sftp")
        total += ok
    return total
