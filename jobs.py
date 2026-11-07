"""Small in-process job tracker for background work (report uploads).

Statuses: Uploaded -> Validating -> Processing -> Completed | Completed with Warnings | Failed.
Jobs live in memory (the newest 200 are kept). If the server restarts mid-job the job is lost and the user simply uploads again.
The same file uploaded twice while the first is still running is not processed twice.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

ACTIVE = ("Uploaded", "Validating", "Queued", "Processing")
_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()


def create(kind: str, owner_id: int, company_id: int, key: str, label: str = "") -> tuple[dict, bool]:
    """Returns (job, is_new). If an active job with the same key exists for this owner, that one is returned (is_new False)."""
    with _LOCK:
        for j in _JOBS.values():
            if j["key"] == key and j["owner"] == owner_id and j["status"] in ACTIVE:
                return j, False
        job = {"id": secrets.token_urlsafe(9), "kind": kind, "owner": owner_id, "company_id": company_id, "key": key,
               "label": label[:200], "status": "Uploaded", "progress": 5, "message": "File received.",
               "result": None, "error": None, "created": time.time()}
        _JOBS[job["id"]] = job
        while len(_JOBS) > 200:
            _JOBS.popitem(last=False)
        return job, True


def update(job: dict, status: str | None = None, progress: int | None = None, message: str | None = None) -> None:
    with _LOCK:
        if status:
            job["status"] = status
        if progress is not None:
            job["progress"] = max(0, min(100, int(progress)))
        if message:
            job["message"] = message[:300]


def get(job_id: str) -> dict | None:
    with _LOCK:
        return _JOBS.get(job_id)


def public(job: dict) -> dict:
    return {k: job[k] for k in ("id", "kind", "status", "progress", "message", "result", "error", "label")}


async def run(job: dict, work) -> None:
    """work(progress) is an async callable returning the result dict. Never raises: failures are recorded on the job."""
    def progress(status: str, pct: int, msg: str = "") -> None:
        update(job, status, pct, msg)
    try:
        result = await work(progress)
        warn = bool(result.get("notes")) if isinstance(result, dict) else False
        with _LOCK:
            job["result"] = result
        update(job, "Completed with Warnings" if warn else "Completed", 100, "Done.")
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            job["error"] = str(e)[:400] or "Processing failed."
        update(job, "Failed", 100, "Processing failed. You can retry.")
