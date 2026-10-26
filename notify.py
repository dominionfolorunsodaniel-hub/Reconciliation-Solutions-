"""
notify.py - push alerts to linked Telegram users (works from the web app and the bot, no extra deps).
"""
import json
import logging
import os
import urllib.request
import uuid

import db

log = logging.getLogger("notify")


def send_telegram(chat_id: int, text: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram push to %s failed: %s", chat_id, e)
        return False


def alert_company(company_id: int, text: str, exclude_user_id: int | None = None) -> int:
    """Send to every active, linked user with access to the company (except the uploader)."""
    sent = 0
    for u in db.users_to_alert(company_id):
        if u["id"] != exclude_user_id and send_telegram(u["telegram_id"], text):
            sent += 1
    return sent


def send_document(chat_id: int, data: bytes, filename: str, caption: str = "") -> bool:
    """Send a file (e.g. a PDF) to a Telegram user. Standard library only."""
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        return False
    boundary = uuid.uuid4().hex

    def field(name: str, value) -> bytes:
        return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()

    body = (field("chat_id", chat_id) + field("caption", caption[:1000])
            + f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
              f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + data + f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram document to %s failed: %s", chat_id, e)
        return False


def send_photo(chat_id: int, data: bytes, filename: str, caption: str = "") -> bool:
    """Send an image to a Telegram chat (falls back to sending it as a file). Standard library only."""
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        return False
    boundary = uuid.uuid4().hex

    def field(name: str, value) -> bytes:
        return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()

    body = (field("chat_id", chat_id) + field("caption", caption[:1000])
            + f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
              f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + data + f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram photo to %s failed (%s); trying as a file", chat_id, e)
        return send_document(chat_id, data, filename, caption)


def payment_alert_ids() -> list[int]:
    """Telegram chats that receive payment alerts: linked admins plus optional PAYMENT_ALERT_TELEGRAM_IDS."""
    ids = [u["telegram_id"] for u in db.admins_to_alert()]
    for part in os.getenv("PAYMENT_ALERT_TELEGRAM_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return list(dict.fromkeys(ids))


def alert_admins_photo(data: bytes, filename: str, caption: str) -> int:
    return sum(1 for cid in payment_alert_ids() if send_photo(cid, data, filename, caption))


def alert_payment_admins(text: str) -> int:
    return sum(1 for cid in payment_alert_ids() if send_telegram(cid, text))


def alert_admins(text: str) -> int:
    """Tell every admin who linked Telegram (used for backup failures, bot down, etc.)."""
    return sum(1 for u in db.admins_to_alert() if send_telegram(u["telegram_id"], text))


def email_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip() or (os.getenv("SMTP_HOST", "").strip()
                and os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")).strip()))


def _send_resend(to_email: str, subject: str, body: str) -> bool:
    """Resend (resend.com) over HTTPS. Works on hosts that block SMTP ports, such as Render's free plan."""
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key or not to_email:
        return False
    try:
        import httpx
        r = httpx.post("https://api.resend.com/emails", timeout=15,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json={"from": os.getenv("RESEND_FROM", "Report Analyzer <onboarding@resend.dev>"),
                             "to": [to_email], "subject": (subject or "Report Analyzer notification")[:180],
                             "text": (body or "")[:12000]})
        if r.status_code < 300:
            return True
        log.warning("Resend refused email to %s: %s %s", to_email, r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("Resend email failed to %s: %s", to_email, e)
    return False


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Send email via Resend (RESEND_API_KEY) or SMTP; returns False when unconfigured or delivery fails."""
    if _send_resend(to_email, subject, body):
        return True
    import smtplib
    from email.message import EmailMessage
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")).strip()
    if not host or not sender or not to_email:
        return False
    msg = EmailMessage()
    msg["Subject"] = (subject or "Report Analyzer notification")[:180]
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content((body or "")[:12000])
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            if os.getenv("SMTP_USE_TLS", "1") != "0":
                server.starttls(); server.ehlo()
            username = os.getenv("SMTP_USER", "")
            password = os.getenv("SMTP_PASSWORD", "")
            if username:
                server.login(username, password)
            server.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Email delivery failed to %s: %s", to_email, e)
        return False


def email_company(company_id: int, subject: str, body: str) -> int:
    """Deliver to configured company recipients; only addresses explicitly configured by company admin."""
    try:
        import enterprise_features as ef
        settings = ef.notification_settings(company_id)
    except Exception as e:  # noqa: BLE001
        log.debug("Company email settings unavailable: %s", e)
        return 0
    sent = 0
    for address in settings.get("emails", []):
        sent += int(send_email(address, subject, body))
    return sent
