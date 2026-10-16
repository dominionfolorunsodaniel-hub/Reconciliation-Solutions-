"""
backup.py - backups of the database + (already encrypted) report files + company logos.

    python backup.py                      create a backup now
    python backup.py restore FILE DIR     restore a backup into an EMPTY folder

The backup is itself encrypted with ENCRYPTION_KEY when encryption is on, so the database metadata
(usernames, audit log) is protected too. Keep copies OFF the server (another disk, cloud storage).
"""
import datetime as dt
import glob
import pathlib
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
        root = os.path.abspath(target_dir)
        for member in z.infolist():
            name = member.filename.replace("\\", "/")
            parts = pathlib.PurePosixPath(name).parts
            if name.startswith("/") or any(part in ("..", "") for part in parts):
                raise ValueError("Unsafe path found inside backup archive.")
            destination = os.path.abspath(os.path.join(root, *parts))
            if os.path.commonpath([root, destination]) != root:
                raise ValueError("Backup archive attempts to write outside the restore folder.")
        z.extractall(root)


def verify_backup(path: str) -> dict:
    """Verify archive integrity and SQLite consistency without modifying production data."""
    with open(path, "rb") as f:
        raw = f.read()
    data = security.decrypt(raw) if path.endswith(".enc") else raw
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        bad = z.testzip()
        if bad:
            raise ValueError(f"Corrupt ZIP member: {bad}")
        names = set(z.namelist())
        if "reports.db" not in names:
            raise ValueError("Backup does not contain reports.db")
        with tempfile.TemporaryDirectory() as tmp:
            dbfile = os.path.join(tmp, "reports.db")
            with open(dbfile, "wb") as f:
                f.write(z.read("reports.db"))
            con = sqlite3.connect(dbfile)
            try:
                check = con.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise ValueError(f"SQLite quick_check failed: {check}")
                tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            finally:
                con.close()
    return {"ok": True, "file": os.path.basename(path), "size_bytes": len(raw),
            "members": len(names), "sqlite_tables": int(tables), "sqlite_check": "ok",
            "encrypted": path.endswith(".enc")}


def restore_drill(path: str | None = None) -> dict:
    """Restore the latest/specified backup into a temporary empty directory and validate it."""
    if path is None:
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup-*")))
        if not files:
            raise FileNotFoundError("No backup is available for restore drill.")
        path = files[-1]
    verified = verify_backup(path)
    with tempfile.TemporaryDirectory() as tmp:
        restored = os.path.join(tmp, "restored")
        restore(path, restored)
        dbfile = os.path.join(restored, "reports.db")
        con = sqlite3.connect(dbfile)
        try:
            check = con.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise ValueError(f"Restored database failed SQLite check: {check}")
        finally:
            con.close()
        verified["restore_drill"] = "passed"
        verified["restored_files"] = sum(1 for r, _, files in os.walk(restored) for _ in files)
    return verified


def upload_offsite(path: str) -> dict:
    """Upload to an S3-compatible bucket if configured; boto3 is optional."""
    bucket = os.getenv("BACKUP_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("Set BACKUP_S3_BUCKET to enable off-site backup uploads.")
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError("Install boto3 to enable S3-compatible off-site backups.") from e
    key = os.getenv("BACKUP_S3_PREFIX", "report-analyzer/").strip("/")
    key = (key + "/" if key else "") + os.path.basename(path)
    client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None,
                          endpoint_url=os.getenv("BACKUP_S3_ENDPOINT") or None)
    client.upload_file(path, bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
    return {"bucket": bucket, "key": key, "uploaded": True}


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
