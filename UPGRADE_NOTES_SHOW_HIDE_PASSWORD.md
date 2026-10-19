# Report Analyzer V2 — login and feature upgrade notes

## Login improvement added in this package

- Added an accessible **Show / Hide** control beside every password input.
- Covers sign-in, customer registration, admin customer onboarding, user creation, and password fields created dynamically in the admin user list.
- Uses a normal browser-side input visibility toggle only; passwords are still sent to the existing login/registration endpoints and are not stored in browser storage by this feature.
- Keyboard focus and accessible labels/pressed state are provided.

## Major capabilities already present in this V2 source

This package already includes the following features, so they were retained rather than duplicated:

- Financial dashboard, report upload and plain-language AI-assisted analysis.
- Reconciliation results for matched rows, amount mismatches, and rows appearing in only one report.
- Discrepancy cases, status updates, comments, assignment support and activity history.
- Excel/PDF exports, report history, historical trends and ATM cash forecasts.
- Anomaly alerts, scheduled reconciliation, scheduled digests, and configurable email/Telegram notifications.
- Customer registration requests with administrator approval, company-scoped access, roles, session cookies, login throttling and optional two-factor authentication.
- Billing plans, manual bank-transfer review, receipts/invoices, subscription limits and usage tracking.
- Company-scoped integration tokens, external report imports, audit logs, and backup/temporary restore checks.

## Features that still need external setup or further work

- Durable storage must be configured and tested on the target host. On Render, the service must support a persistent disk or the app must be migrated to another durable database/storage provider. The configured mount path must match `DATA_DIR`.
- This ZIP does **not** recover any previous customer database. Inspect Render Events and check for an existing database/backup before changing paths or redeploying.
- SMTP and Telegram alerts need valid credentials and real delivery tests.
- Direct OAuth/API integrations with QuickBooks, Xero, SAP or other accounting vendors need a selected vendor, credentials and vendor-specific implementation/testing; the generic scoped import endpoint is already present.
- Automated card/bank payment verification, formal tax-compliant invoices, and external security certification are not included.

## Validation performed in the build workspace

- `python -m py_compile *.py` — passed.
- `python billing_test.py` — 12 tests passed.
- `python smoke_test.py` — all checks passed.
- Inline dashboard JavaScript `node --check` — passed.
- The password toggle has been syntax-checked, but still needs visual testing in a real mobile browser and on the separate V2 Render service.

## Deployment safety

Use this package only for the V2 branch/service (`v2-registration-update`). Do not replace V1 or the `main` branch. Before redeploying, investigate the old SQLite database because the customer's login failures after redeployment may mean the service is now using an empty database.
