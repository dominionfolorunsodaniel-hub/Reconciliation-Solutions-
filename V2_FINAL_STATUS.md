# Report Analyzer V2 — Build and Deployment Status

## Included in this package
- Existing V2 Python/FastAPI application and Telegram bot.
- Render environment variables (`DASHBOARD_USER` and `DASHBOARD_PASSWORD`) are authoritative for web dashboard admin login.
- Admin credentials can be changed through the V2 Render environment settings; on login, the app synchronizes the configured identity with an admin record and revokes admin sessions when credentials change.
- Customer accounts and company/report data are not deleted by the admin credential synchronization.
- Password visibility controls on login and registration forms.
- Existing billing, reconciliation, registration/approval, scoped access, PDF/Excel exports, audit history, scheduling, alerts, and backup/restore functionality.
- Neon/PostgreSQL support when `DATABASE_URL` is set; SQLite is used when it is not set. In PostgreSQL mode, encrypted report files and logos are stored in the database.
- Python cache files are excluded from the ZIP.

## Local checks completed for this build
- `python -m py_compile *.py` — passed.
- `python billing_test.py` — 12 tests passed.
- `python smoke_test.py` — all checks passed, including admin login with Render-style environment credentials, credential change with existing customer records, old admin credential rejection, and customer-account preservation.

## Important deployment limitation
These are local tests, not a live Render/Neon test. The package has not been verified against your actual Neon database, and the live service's `DATABASE_URL` connection and persistence across redeployment still need to be tested. Do not delete customer data or replace V1. Deploy only to the separate V2 branch/service, then verify `/api/health`, admin login, customer registration/approval, report upload/export, and a test customer's login after a controlled redeploy before inviting real customers to rely on it.

## Deployment safety
- Keep V1 and `backup-before-registration` untouched.
- Use only the V2 Render service and V2 GitHub branch.
- Do not expose `DATABASE_URL`, `DASHBOARD_PASSWORD`, Telegram tokens, or API keys.
- If `DASHBOARD_USER` matches an existing non-admin customer username, admin synchronization fails safely; choose a distinct username.
