# Report Analyzer

Upload company reports (CSV, Excel, PDF - even scanned ones) and break them down by any categories you type.
A web dashboard and a Telegram bot share one database. Built for teams that handle client financial data.

## Quick start
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
- **Ask in plain English** -> Claude plans, pandas computes exactly -> table, chart, insights, Excel, branded PDF.
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
- Card numbers masked to last 4 digits; CVV/PIN columns dropped **before** storage or sending to Claude.
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
- Report excerpts (first rows, column stats, result tables) are sent to the Claude API. Check this against
  your clients' contracts and data-protection rules.
- Usernames, company names and the audit log sit in SQLite (backups are encrypted; also use disk encryption).
- SQLite suits a small team; move to Postgres for heavy use. OCR'd numbers must be double-checked.
- Cash forecasts are statistical estimates, not guarantees.
