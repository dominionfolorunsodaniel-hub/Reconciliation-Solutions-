# Report Analyzer V2 update notes

## Customer onboarding and tenant separation
- Admin-only manual customer onboarding creates a dedicated company and Analyst account.
- Public customer registration requests are stored as pending; an admin must approve or reject.
- Public signup never lets a customer choose their role or company access.
- Passwords are hashed before registration requests are stored.
- Approval creates the company, Analyst user, and company assignment in one transaction.
- Duplicate usernames, company names, and Telegram IDs are checked; race conflicts are handled during approval.
- Registration decisions are written to the audit log.
- Customers are isolated to their own company unless an admin explicitly grants more access.

## Existing application features retained
- CSV/Excel/PDF report intake, cleanup, sensitive-card-data masking, analysis, chart and Excel exports.
- Two-report reconciliation, ATM cash forecasting, anomaly alerts, scheduled digests, branded PDF output.
- Roles, login throttling, optional TOTP 2FA, session expiry, audit log, encrypted report storage, backups,
  retention settings, AI usage caps, Telegram bot integration, and optional email/SFTP intake.

## Validation
- Python syntax compilation passed.
- Offline smoke suite passed, including registration approval/rejection and tenant-isolation checks.
- Dashboard JavaScript syntax checked separately where Node.js is available.

## Deliberately not wired to real money yet
- No live payment processor, automatic subscription charging, or payment webhooks are configured.
- Do not collect card numbers or payment credentials in this app. Add a verified provider (and test-mode keys)
  before accepting online payments. Subscription/revenue reporting should be added against that provider's
  verified transaction events, not simulated payments.
- A customer support ticket center, referral/promo engine, global maintenance switch, and scheduled public
  announcement center are not included in this V2 candidate; these should be added only with their own tests
  and operational rules, rather than bolted in without review.
