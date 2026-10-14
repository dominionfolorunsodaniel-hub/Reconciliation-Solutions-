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
- `PORT`: Render supplies this automatically; do not hardcode it.

## Critical storage warning

Render's Free web services do not provide persistent disks. In this project, SQLite data and uploaded/encrypted report files live under `DATA_DIR`; backups use `BACKUP_DIR`. They can be lost when a free instance is replaced or redeployed. Do **not** use the free instance for valuable company reports or production customer data until you have configured durable storage and verified backups/restores. A persistent disk requires a plan that supports it, or the application must be migrated to durable external storage/database.

## After deployment

1. Open the Render URL and sign in.
2. Check Render logs for both `[supervisor] starting bot` and `[supervisor] starting web`.
3. Visit `/api/health` on the public URL; a healthy response only confirms the web API is responding, not that Telegram polling works.
4. Message the Telegram bot and test with sample, non-confidential data.
5. If Gemini calls fail, check the `GEMINI_API_KEY` and free-tier usage in Google AI Studio. Do not add an Anthropic key; this version no longer uses the Anthropic SDK.

## Confidentiality note

The app masks certain card data and drops CVV/PIN columns, but report excerpts and statistics are sent to Gemini for AI-generated plans, descriptions, and insights. Review Google's current API data terms and your customers' contracts/privacy obligations before sending any real company or personal data. Use synthetic test reports until that review is complete.
