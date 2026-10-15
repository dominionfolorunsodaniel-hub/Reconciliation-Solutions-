# V2 handoff and safe deployment checklist

## What changed
V2 adds customer registration requests with administrator approval/rejection, alongside manual admin onboarding.
An approved customer gets an Analyst account and one dedicated company. Public signup cannot assign roles or
grant access to existing companies.

## Before sharing with a website reviewer
1. Deploy V2 to a separate Render service or test branch first. Do not overwrite the working V1 service.
2. Set all environment variables in Render's Environment page; never commit `.env`, API keys, Telegram tokens,
   passwords, real customer files, or database files to GitHub.
3. Use a strong unique `DASHBOARD_PASSWORD` (10+ characters), and keep `ENCRYPTION_KEY` backed up securely.
4. On an HTTPS deployment, set `COOKIE_SECURE=1` and `REQUIRE_2FA_ADMINS=1` after the admin has configured TOTP.
5. Attach a Render persistent disk and point `DATA_DIR` to its mount path. Without a persistent disk, SQLite users,
   registration requests, reports, and app state can disappear on redeploy/restart.
6. Keep the website and Telegram bot in the same service/container and share the same `DATA_DIR` and `ENCRYPTION_KEY`.
   Do not run two separate copies against separate local disks if they must see the same customers/reports.
7. Run `python smoke_test.py` before promoting V2. It uses a temporary data directory and synthetic test data.
8. Test customer signup with a dummy company, approve it as admin, verify the new user can only see their own company,
   then test a rejected signup. Delete the dummy records before real use.
9. Test a backup and restore procedure. Copy encrypted backups off the server too. A backup on the same disk is not enough.
10. Only after the reviewer signs off should V2 replace the production deployment. Keep the V1 branch and backup untouched.

## Important
- The `ENCRYPTION_KEY` must remain the same across the web app, Telegram bot, and restore environment. Losing it can
  make encrypted stored data unreadable.
- Configure a real supported payment provider separately before advertising paid subscriptions. This build does not
  pretend to process payments or record real revenue.
- This is a launch candidate, not a substitute for a security review or a tested backup/restore drill.
