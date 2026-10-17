# Report Analyzer V2 — Consolidated Upgrade Status

## Included
- Existing V2 Python/FastAPI application and features.
- Accessible show/hide controls for password fields on login, registration, and dynamically-created password inputs.
- Existing billing, reconciliation, customer registration/approval, scoped access, PDF/Excel exports, audit history, scheduling, alerts, and backup/restore functionality.
- Python bytecode/cache files excluded from the package.

## Tests run in the build workspace
- `python -m py_compile *.py` — passed.
- `python billing_test.py` — 12 tests passed.
- `python smoke_test.py` — all checks passed.

## Neon PostgreSQL support (added)
Set `DATABASE_URL` to use Neon PostgreSQL; leave it unset for SQLite. See `NEON_SETUP.md`. Database access goes through `dbcompat.py`; report files and logos are stored in the database in PostgreSQL mode. Not yet tested against a live Neon database.

## IMPORTANT: deployment readiness (SQLite mode only)
This package still uses SQLite. It has **not** been migrated to Neon PostgreSQL and has not been tested against a live Neon database or a Render redeployment. Do not deploy this package expecting PostgreSQL persistence. The password visibility feature is present in `static/index.html`, but visually verify it on the V2 service after a safe test deployment.

Before a final persistent-storage deployment:
1. Migrate the database layer and every SQLite-dependent module to PostgreSQL-compatible operations.
2. Add a PostgreSQL driver and set `DATABASE_URL` only on the V2 Render service.
3. Test registration/login, roles and company isolation, report storage, billing, exports, backup/restore, and persistence across redeployment.
4. Keep V1 unchanged.

Do not place database credentials in source code or upload `.env` files to GitHub.
