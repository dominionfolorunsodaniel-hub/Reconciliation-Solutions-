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

**Current admin tools:** user/role management, company assignment, Telegram linking, account activation, password reset, 2FA reset, AI usage limits/usage table, audit log, company PDF branding, and manual backup creation. Dedicated payment/subscription processing, customer support ticket center, owner-to-customer broadcast/scheduler, referral/promo system, and maintenance-mode controls are not implemented in this version. Do not advertise those as available yet.


## V2 launch checklist

See [`V2_HANDOFF.md`](V2_HANDOFF.md) before deploying or sharing a test URL. V2 includes customer signup requests with admin approval/rejection; public signups cannot grant themselves access.
