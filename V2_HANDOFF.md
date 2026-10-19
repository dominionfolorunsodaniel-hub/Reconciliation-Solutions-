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
- This is a staged release candidate, not a substitute for a production-environment security review and recovery drill. A local backup + temporary restore check is available to admins and is covered by the automated smoke test.


## Monetization implementation notes

- Free Trial: 2 days, 5 analyses/month. Starter: ₦25,000 for 3 days, 50 analyses/month, 3 members; Growth: ₦100,000 for 5 days, 200 analyses/month, 5 members; Professional: ₦350,000 for 7 days, 750 analyses/month, 10 members; Business: ₦1,000,000 for 10.5 days, 1,500 analyses/month, 15 members; Corporate: ₦2,500,000 for 14 days, 4,000 analyses/month, 30 members; Enterprise: ₦5,000,000 for 21 days, 10,000 analyses/month, 75 members; Enterprise Plus is temporarily hidden from new purchases pending payment-provider limit confirmation and retained for legacy records. Legacy Company Lifetime records are retained but the plan is not available for new purchases.
- Dashboard and Telegram share company-level monthly quota reservations. Failed operations release their reservation; concurrent requests reserve atomically. Existing daily AI cost controls remain separate.
- Customers submit a bank transfer reference; paid access stays pending until admin approval. Every active plan uses its configured fixed term; administrators can grant the advertised term or a custom duration from 0.5 to 366 days.
- Configure `BANK_NAME`, `BANK_ACCOUNT`, and `BANK_OWNER` in Render Environment. The database and report files must live on durable storage before production.
- Run `python billing_test.py` and `python smoke_test.py` after changes.
