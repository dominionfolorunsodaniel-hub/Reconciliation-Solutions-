# Report Analyzer V2 feature audit — staged release

This audit reflects source inspection plus the automated tests run in this build workspace. It does not imply a live Render deployment or independent security certification.

## Implemented in the V2 source and exposed in the dashboard/API

- **Advanced discrepancy workflow:** reconciliation identifies matched, amount-mismatch, only-in-A and only-in-B rows; it persists discrepancy cases with severity, status, comments, assignment support in the API, resolution/acceptance timestamps, and case activity events. Dashboard users can list cases, update status, add comments, and inspect events.
- **Audit trail:** audit log and admin view exist; uploads, analyses, reconciliation, billing, case updates, integrations, exports and backup checks write audit events. A full production action-coverage review remains a release gate.
- **Excel/PDF exports:** reconciliation exports and report exports are implemented, with existing privacy protections against Excel formula injection and branded PDF support. Visual QA with representative customer files remains necessary.
- **Company permissions/security:** role and company membership checks, session authentication, password hashing, optional TOTP, encrypted report storage, upload validation, and company-scoped case/invoice/schedule APIs exist. Smoke tests cover important cross-company paths, but an independent security review is still recommended.
- **Historical comparison:** persisted reconciliation runs and a trend API are available. Dashboard now shows run history and daily counts of runs, matched rows, discrepancies, and net difference over 30/90/180/365-day windows. This is a practical baseline, not a full BI pivot/report builder.
- **Email/Telegram alerts:** company notification emails and quota thresholds can be configured in the dashboard; usage warnings, scheduled reconciliation notices and subscription expiry reminders are wired into the code. Telegram delivery requires linked Telegram accounts and a valid bot token. Email delivery requires working SMTP environment settings and was not tested against a real mail server.
- **Backup/recovery:** scheduled encrypted backups remain available; admin dashboard now has a backup + restore-check action that creates a backup, verifies archive/SQLite integrity, and restores into a temporary directory without replacing live data. S3-compatible off-site upload is supported when configured. A production recovery-time drill and off-service copy are still required.
- **Billing portal and receipts:** dashboard can list company invoices/receipts and download a PDF receipt. Verified admin approval creates the receipt; renewal reminders are scheduled. Payments remain manual bank transfers, not automatic bank/card processing. The generated PDF clearly says it is not a tax invoice unless locally validated.
- **Onboarding:** dashboard getting-started content and Telegram `/guide` exist. First-customer usability testing remains.
- **External integrations:** administrators can issue, list and revoke company-scoped hashed integration tokens. `POST /api/integrations/{company_id}/reports` imports supported CSV/TXT/XLSX/XLSM/XLS/PDF files (up to 20 MB) and is tested. Direct QuickBooks/Xero/SAP/etc. OAuth/API adapters are deferred until a vendor is selected and credentials are available.
- **Scheduled reconciliation:** analysts/admins can configure report pairs and intervals from 1 hour to 90 days; the background worker reserves company quota, records runs/cases, sends configured alerts, and records failures. Local tests cover schedule creation/list/delete; a real production worker run still needs verification after deployment.

## Billing configuration

- Free Trial: ₦0, 2 days, 5 analyses/month, 1 member.
- Starter: ₦25,000, 3 days, 50 analyses/month, 3 members.
- Growth: ₦100,000, 5 days, 200 analyses/month, 5 members.
- Professional: ₦350,000, 7 days, 750 analyses/month, 10 members.
- Business: ₦1,000,000, 10.5 days, 1,500 analyses/month, 15 members.
- Corporate: ₦2,500,000, 14 days, 4,000 analyses/month, 30 members.
- Enterprise: ₦5,000,000, 21 days, 10,000 analyses/month, 75 members.
- Enterprise Plus: temporarily unavailable for new purchases; retained only for existing legacy records until payment-provider limits are confirmed.

Analysis allowances reset by calendar month even though the access terms above are shorter. The legacy Company Lifetime plan is retained only for historical records and is not offered for new purchases. Paid access activates only after an administrator verifies the transfer in the actual bank account.

## Still deferred / external prerequisites

1. Direct vendor-specific accounting API integrations (requires selected vendors, API/OAuth credentials, mapping, and vendor-specific tests).
2. Live SMTP delivery test, real Telegram delivery test, and production scheduled-worker verification.
3. Off-service backup destination and a documented recovery-time objective; configure a persistent disk or durable database before handling real customer data.
4. Formal tax-compliant invoices, automatic bank/card payment verification, SLA/uptime guarantees, external penetration testing, and compliance certification.
5. UX testing with first-time customers and large/odd customer report files.

## Release gates

- `python -m py_compile *.py`
- `node --check` on the inline dashboard scripts
- `python billing_test.py`
- `python smoke_test.py`
- Verify ZIP integrity and inspect for secrets or customer data.
- Deploy only to the separate V2 branch/service with persistent storage. Test login, company isolation, payment approval/expiry, dashboard operations, Telegram, SMTP, and backup/restore in the target environment before considering any V1 replacement.
