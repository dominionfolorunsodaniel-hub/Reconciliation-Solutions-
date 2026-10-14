"""
backup.py - backups of the database + (already encrypted) report files + company logos.

    python backup.py                      create a backup now
    python backup.py restore FILE DIR     restore a backup into an EMPTY folder

The backup is itself encrypted with ENCRYPTION_KEY when encryption is on, so the database metadata
(usernames, audit log) is protected too. Keep copies OFF the server (another disk, cloud storage).
"""
import datetime as dt
import glob
import io
import os
import sqlite3
import sys
import tempfile
import zipfile

import security

DATA_DIR = os.getenv("DATA_DIR", "data")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
KEEP = int(os.getenv("BACKUP_KEEP", "14"))


def create_backup(dest_dir: str | None = None, data_dir: str | None = None, keep: int | None = None) -> str:
    data_dir, dest_dir = data_dir or DATA_DIR, dest_dir or BACKUP_DIR
    keep = KEEP if keep is None else keep
    os.makedirs(dest_dir, exist_ok=True)
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "reports.db")
        src, dst = sqlite3.connect(os.path.join(data_dir, "reports.db")), sqlite3.connect(snap)
        src.backup(dst)  # consistent snapshot even while the app is running
        dst.close()
        src.close()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "reports.db")
            for sub in ("files", "logos"):
                folder = os.path.join(data_dir, sub)
                if os.path.isdir(folder):
                    for name in sorted(os.listdir(folder)):
                        z.write(os.path.join(folder, name), f"{sub}/{name}")
    encrypted = security.encryption_enabled()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(dest_dir, f"backup-{stamp}.zip" + (".enc" if encrypted else ""))
    with open(path, "wb") as f:
        f.write(security.encrypt(buf.getvalue()) if encrypted else buf.getvalue())
    if keep > 0:
        for old in sorted(glob.glob(os.path.join(dest_dir, "backup-*")))[:-keep]:
            os.remove(old)
    return path


def restore(path: str, target_dir: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".enc"):
        data = security.decrypt(data)
    os.makedirs(target_dir, exist_ok=True)
    if os.listdir(target_dir):
        raise ValueError("The target folder must be empty.")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(target_dir)


def latest_age_hours(dest_dir: str | None = None) -> float | None:
    files = glob.glob(os.path.join(dest_dir or BACKUP_DIR, "backup-*"))
    if not files:
        return None
    newest = max(os.path.getmtime(f) for f in files)
    return (dt.datetime.now().timestamp() - newest) / 3600


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "restore":
        restore(sys.argv[2], sys.argv[3])
        print(f"Restored into {sys.argv[3]}. Point DATA_DIR at it (and use the same ENCRYPTION_KEY).")
    else:
        print("Backup written:", create_backup())
