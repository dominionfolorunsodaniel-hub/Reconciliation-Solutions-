# Render deployment notes (Gemini edition)

## Create the service

1. Push the contents of this folder to your GitHub repository (keep the project files together at the repository root).
2. In Render choose **New + → Web Service** and connect that repository.
3. Choose **Docker** as the runtime. Render will use the repository's `Dockerfile`.
4. Set the plan to Free for a first smoke test, if available.
5. Add the environment variables listed below, then deploy.

The Dockerfile uses `start.py`, which launches both `web.py` and `bot.py` in the same container. The website listens on Render's `PORT`; if the bot exits, the supervisor attempts to restart it. Keep the Telegram token private.

## Environment variables

Set these under **Render → service → Environment**. Enter real values in Render only; never commit them to GitHub or paste them into chats.

- `TELEGRAM_TOKEN`: token from BotFather.
- `GEMINI_API_KEY`: key created in Google AI Studio. Gemini's free allowance depends on model, region, account, and current limits; it is not unlimited.
- `GEMINI_MODEL`: `gemini-flash-latest` (optional; this is the code default).
- `ENCRYPTION_KEY`: output from running `python security.py genkey` locally. Back it up securely; losing it can make encrypted reports and backups unreadable.
- `REQUIRE_ENCRYPTION`: `1`.
- `DASHBOARD_USER`: your first admin username.
- `DASHBOARD_PASSWORD`: a unique strong password of at least 10 characters.
- `COOKIE_SECURE`: `1` when the app is served over HTTPS (Render's public service URL uses HTTPS).
- `REQUIRE_2FA_ADMINS`: recommended `1` before handling real client data; set up two-factor authentication before relying on it.
- `DATA_DIR`: `/data` (default in the Docker image).
- `BACKUP_DIR`: `/backups` (default in the Docker image).
- `BANK_NAME`: bank name shown to customers for manual transfer (set privately in Render).
- `BANK_ACCOUNT`: your business receiving account number (set privately in Render; do not commit it to GitHub).
- `BANK_OWNER`: account holder name shown to customers (set privately in Render).
- `PORT`: Render supplies this automatically; do not hardcode it.

## Free persistent storage

Add `DATABASE_URL` (Neon PostgreSQL) in Render's Environment tab to keep logins, reports and billing across redeploys. Setup steps are in `NEON_SETUP.md`.

## Critical storage warning (applies when DATABASE_URL is NOT set)

Render's Free web services do not provide persistent disks. In this project, SQLite data and uploaded/encrypted report files live under `DATA_DIR`; backups use `BACKUP_DIR`. They can be lost when a free instance is replaced or redeployed. Do **not** use the free instance for valuable company reports or production customer data until you have configured durable storage and verified backups/restores. A persistent disk requires a plan that supports it, or the application must be migrated to durable external storage/database.

## After deployment

1. Open the Render URL and sign in.
2. Check Render logs for both `[supervisor] starting bot` and `[supervisor] starting web`.
3. Visit `/api/health` on the public URL; a healthy response only confirms the web API is responding, not that Telegram polling works.
4. Message the Telegram bot and test with sample, non-confidential data.
5. If Gemini calls fail, check the `GEMINI_API_KEY` and free-tier usage in Google AI Studio. Do not add an Anthropic key; this version no longer uses the Anthropic SDK.

## Confidentiality note

The app masks certain card data and drops CVV/PIN columns, but report excerpts and statistics are sent to Gemini for AI-generated plans, descriptions, and insights. Review Google's current API data terms and your customers' contracts/privacy obligations before sending any real company or personal data. Use synthetic test reports until that review is complete.


## Customer registration and approval

Customers can request an account from the sign-in page using **New customer? Request an account**. A request stays pending; it does not create a login that can be used immediately.

1. Sign in with your owner/admin account and open **Admin**.
2. Review **Customer registration requests**. Approve only customers you have verified; reject requests you do not recognize.
3. Approval creates an `analyst` account and a dedicated company. Public signup cannot select a role or attach itself to an existing company.
4. Alternatively, use **Onboard a new customer manually** when you are creating an account directly.
5. Test with dummy companies first. Confirm each customer sees only their own company and that a rejected signup cannot log in.

Do not use the same company name for different customers. Duplicate company names are rejected to prevent accidental shared access. Registration requests are included in the SQLite database and its normal backup.

## Manual-transfer monetization (V2)

The project supports company-wide shared monthly quotas, manual bank-transfer requests, admin review/approval, a 2-day free trial, fixed-term paid plans from 3 to 30 days, and admin-granted temporary paid plans. Legacy Company Lifetime records remain readable but are not offered for new purchases. It does not process card payments or verify bank transfers automatically. An administrator must verify the transaction in the actual bank account before approving it. A transfer reference is not proof of payment. Configure the three `BANK_*` variables above before showing payment instructions.

Before accepting payments or confidential reports, configure durable storage for `/data` (and a safe off-service backup location), then test that subscriptions, quota records, reports, and backups survive redeploys and can be restored. The V2 handoff checklist is in `V2_HANDOFF.md`.
