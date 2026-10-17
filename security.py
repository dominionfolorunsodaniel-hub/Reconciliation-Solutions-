"""
security.py - password hashing, encryption at rest, and role ranks.

Generate an encryption key once and keep it safe (lose it = stored reports unreadable):
    python security.py genkey
"""
import base64
import hashlib
import hmac
import os

ROLE_RANK = {"viewer": 1, "analyst": 2, "admin": 3}

# ---- passwords (scrypt from the standard library) ------------------------- #
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001
        return False


# ---- encryption at rest (Fernet = AES-128-CBC + HMAC) ----------------------- #
_fernet = None
_loaded = False


def _get_fernet():
    global _fernet, _loaded
    if _loaded:
        return _fernet
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if key:
        try:
            from cryptography.fernet import Fernet
        except ImportError as e:
            raise RuntimeError("ENCRYPTION_KEY is set but 'cryptography' is not installed.") from e
        _fernet = Fernet(key.encode())
    elif os.getenv("REQUIRE_ENCRYPTION", "0") == "1":
        raise RuntimeError("REQUIRE_ENCRYPTION=1 but ENCRYPTION_KEY is not set (python security.py genkey).")
    _loaded = True
    return _fernet


def encryption_enabled() -> bool:
    return _get_fernet() is not None


def encrypt(data: bytes) -> bytes:
    f = _get_fernet()
    return b"ENC1:" + f.encrypt(data) if f else b"RAW0:" + data


def decrypt(blob: bytes) -> bytes:
    if isinstance(blob, str):
        blob = blob.encode()
    if blob.startswith(b"RAW0:"):
        return blob[5:]
    if blob.startswith(b"ENC1:"):
        f = _get_fernet()
        if f is None:
            raise RuntimeError("Stored data is encrypted but ENCRYPTION_KEY is not set.")
        try:
            return f.decrypt(blob[5:])
        except Exception as e:  # noqa: BLE001
            raise ValueError("Could not decrypt stored data - wrong ENCRYPTION_KEY?") from e
    return blob


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
    else:
        print("usage: python security.py genkey")
