"""
totp.py - time-based one-time passwords (RFC 6238) for two-factor login.
Works with Google Authenticator, Microsoft Authenticator, Authy, 1Password, etc. Standard library only.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30  # seconds per code


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{number:06d}"


def code_at(secret: str, t: float | None = None) -> str:
    return _code(secret, int((time.time() if t is None else t) // STEP))


def verify(secret: str, code: str, last_step: int = 0, t: float | None = None, window: int = 1) -> int | None:
    """Return the matching time-step if the code is valid AND newer than last_step (stops code re-use)."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    now_step = int((time.time() if t is None else t) // STEP)
    for step in range(now_step - window, now_step + window + 1):
        if step > last_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(username: str, secret: str, issuer: str = "Report Analyzer") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}"
            f"&issuer={quote(issuer)}&digits=6&period={STEP}")
