# New in this update

- Statement Score: authenticity check, unusual-pattern flags, spending categories, combine several accounts
- Batch Check (analyst and up): score up to 25 applicants at once and rank them (Excel and PDF)
- Share links: private, expiring, switch-off-able link to one Statement Score result
- Weekly email (opt-in, Mondays 08:00 in TIMEZONE; needs RESEND_API_KEY)
- Security: devices signed in, sign out other devices, new-device alert, download my data, request account deletion
- Uploading a report no longer uses one of the plan's monthly checks
- Daily AI calls per user default raised from 150 to 300 (env DAILY_AI_CALLS_PER_USER overrides)

No new environment variables are required. New tables are created automatically on startup.
Start with `python start.py` (website only when TELEGRAM_TOKEN is not set).
