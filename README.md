# Report Analyzer

Upload company reports (CSV, Excel, PDF - even scanned ones) and break them down by any categories you type.
A web dashboard and a Telegram bot share one database. Built for teams that handle client financial data.

## Quick start

AI analysis uses the Gemini API. Set `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/)) for AI-powered questions. Google may offer a free tier subject to model, region, and rate limits; it is not unlimited. The dashboard can still use its local report profiling and fallback descriptions without a key, but natural-language AI analysis needs one.

```bash
pip install -r requirements.txt          # Python 3.10+
cp .env.example .env                     # fill in the keys (never commit .env)
python security.py genkey                # paste the output into ENCRYPTION_KEY - and back it up!
python smoke_test.py                     # checks everything works (no API keys needed)
python web.py                            # open http://localhost:8000 and sign in as the admin
python bot.py                            # Telegram bot (second terminal)
```
Docker: `docker compose up -d --build`

First-time: sign in -> **Admin** -> add a company, add users (role + which companies they see).
To let someone use the bot: they message it, it replies with their Telegram ID, the admin pastes it into that user.

## What it does
- **On upload** it explains what the report is, the period, key figures, data-quality warnings and anomaly alerts.
- **Ask in plain English** -> Gemini creates a safe analysis plan, pandas computes exactly -> table, chart, insights, Excel, branded PDF.
- **Reconcile** two reports (switch log vs ledger): matched / amount mismatch / only in A / only in B.
- **ATM cash forecast**: expected withdrawals per ATM, suggested cash load, and the day cash runs out.
- **Alerts**: failure spikes per terminal, terminals gone quiet, extreme amounts -> pushed to Telegram.
- **Scheduled digests**: a saved question runs daily/weekly and the answer + PDF goes to Telegram.
- **Automatic intake**: reports arrive by email (IMAP) or SFTP, no manual upload.
- **Scanned PDFs** are read with OCR (install Tesseract; included in the Docker image).

## Telegram commands
`/start` `/help` `/whoami` `/companies` `/company NAME` `/reports` `/use ID` `/all` `/describe` `/alerts`
`/recon A B` `/forecast [days] [cash]` `/pdf` `/history` `/columns` `/sheet NAME`
`/digests` `/digest add MON 08:00 [all] question` `/digest delete ID`
Admin only: `/delete ID` `/audit` `/usage`. You can also send a file, type a question, or tap the suggestion buttons.

## Roles
| Role | Can do |
|---|---|
| viewer | ask questions, reconcile, forecast, download (their companies only) |
| analyst | + upload reports, schedule digests |
| admin | + delete reports, create companies/users, branding, audit log, usage, backups |

## Security & privacy built in
- Card numbers masked to last 4 digits; CVV/PIN columns dropped **before** storage or sending to the configured Gemini API.
  If that happens, only the cleaned copy is stored - the original is discarded.
- Reports, descriptions, history and backups are encrypted (Fernet) with `ENCRYPTION_KEY`.
- scrypt password hashing, HttpOnly session cookies, login throttling, optional **two-factor (TOTP)**,
  per-company access, security headers, audit log of every action, optional retention.
- AI cost caps per user and per day. Excel exports never run report text as formulas.
- Intake accepts only listed senders that pass SPF/DKIM.

## Operations
- **Backups** run nightly into `backups/` (encrypted). Copy them off the server. Restore:
  `python backup.py restore backups/backup-XXXX.zip.enc NEW_EMPTY_FOLDER`, then set `DATA_DIR` to it.
- **Locked out / lost phone?** `python manage.py reset-password NAME`, `python manage.py reset-2fa NAME`.
- **Monitoring:** `GET /api/health` for uptime monitors. Admins linked to Telegram are messaged if a backup
  fails or the bot stops responding.
- **HTTPS:** put Caddy/nginx in front, then set `COOKIE_SECURE=1`.

## Before real client data - please know
- Report excerpts (first rows, column stats, result tables) are sent to the Gemini API when AI analysis is used. Review Google AI API terms and your client contracts before uploading confidential company reports. Check this against
  your clients' contracts and data-protection rules.
- Usernames, company names and the audit log sit in SQLite (backups are encrypted; also use disk encryption).
- SQLite suits a small team; move to Postgres for heavy use. OCR'd numbers must be double-checked.
- Cash forecasts are statistical estimates, not guarantees.

## Customer onboarding (owner/admin)

After signing in to the dashboard as the owner, open **Admin → Onboard a new customer**. Enter a unique company name, a username, and a temporary password of at least 10 characters. A Telegram ID is optional: without it, the customer can use the website but is not linked to the Telegram bot yet. With it, the account can use both channels. The new account is created with the `analyst` role (can upload and analyze reports), and is assigned only to its own company. Share credentials with the customer privately. This version does not yet provide self-service password changes; if you need to rotate a password, set a new one for that account from Admin.

**Data separation:** each customer must have a distinct company record. The customer onboarding form rejects duplicate company names instead of silently attaching the account to an existing company's data. Test with synthetic/sample reports first.

**Current admin tools:** user/role management, company assignment, Telegram linking, account activation, password reset, 2FA reset, AI usage limits/usage table, audit log, company PDF branding, and manual backup creation. Billing currently supports manual bank-transfer requests and administrator review; it does not include automatic bank verification, card processing, a customer support ticket center, referral/promo system, or maintenance-mode controls. Do not advertise those as available yet.


## V2 launch checklist

See [`V2_HANDOFF.md`](V2_HANDOFF.md) before deploying or sharing a test URL. V2 includes customer signup requests with admin approval/rejection; public signups cannot grant themselves access.

## Monetization (V2)

Company plans use a shared company-wide monthly analysis allowance across the dashboard and Telegram bot, while access expires after the fixed term below. Free Trial: 2 days, 5 analyses/month, 1 member. Starter: ₦25,000 for 3 days, 50 analyses/month, 3 members; Growth: ₦100,000 for 5 days, 200 analyses/month, 5 members; Professional: ₦350,000 for 7 days, 750 analyses/month, 10 members; Business: ₦1,000,000 for 10.5 days, 1,500 analyses/month, 15 members; Corporate: ₦2,500,000 for 14 days, 4,000 analyses/month, 30 members; Enterprise: ₦5,000,000 for 21 days, 10,000 analyses/month, 75 members; Enterprise Plus is temporarily hidden from new purchases pending payment-provider limit confirmation and retained for legacy records. The former Company Lifetime offer is retained only for legacy records and cannot be purchased for new subscriptions. Analyses are counted by calendar month even for shorter access terms. These are configured in `db.py`. Analysis questions, reconciliation, forecasts and AI report descriptions use the shared company quota. Existing daily AI cost limits remain separate.

Payments are manual bank transfers. Set `BANK_NAME`, `BANK_ACCOUNT`, and `BANK_OWNER` in the Render environment for the customer billing panel and `/plan` Telegram command. A payment request stays pending until an administrator verifies the actual bank transaction and approves it in Admin → Billing. Transfer references are unique and duplicate pending requests are blocked. Never approve based on a screenshot or reference alone.

Administrators can grant the selected paid plan for its advertised duration by default, or enter a custom duration from 0.5 to 366 days. Grants and payment decisions are written to the audit log. Subscription and usage records are stored in the same SQLite database as the rest of the application; configure durable storage for `DATA_DIR` before production use, and keep off-service backups.

The dashboard's **Company operations** section now includes discrepancy case review/status/comments/activity, historical trend and run history, scheduled reconciliation, email/quota settings, invoice/receipt history, integration token management, and (for admins) a backup + temporary restore check. Scheduled workers also send quota/renewal/scheduled-run alerts to configured Telegram/email recipients. SMTP requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM` (or `SMTP_USER`), and optionally `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_USE_TLS=1`. For off-site S3-compatible backups, set `BACKUP_S3_BUCKET` and the relevant AWS/S3 endpoint/region credentials; keep off-site storage access restricted.

External accounting import is available through company-scoped integration tokens and `POST /api/integrations/{company_id}/reports`. Direct vendor-specific QuickBooks/Xero/SAP integrations still require a selected vendor and its credentials. Payments are manual transfers, and PDF receipts are not tax invoices unless validated for local requirements.

Run offline billing checks with `python billing_test.py`, full application checks with `python smoke_test.py`, Python syntax checks with `python -m py_compile *.py`, and JavaScript syntax checks with `node --check` against the inline scripts in `static/index.html`. The current build passed 12 billing tests and 154 smoke checks. See `FEATURE_AUDIT_V2.md` for remaining production gates.
