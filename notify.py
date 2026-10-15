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


def alert_admins(text: str) -> int:
    """Tell every admin who linked Telegram (used for backup failures, bot down, etc.)."""
    return sum(1 for u in db.admins_to_alert() if send_telegram(u["telegram_id"], text))
