# Roadmap status (FINAL_20)

Before every deploy run:  python predeploy_check.py   (must say SAFE TO DEPLOY)

## Added in FINAL_20
- Reconciliation: Confidence (High/Medium/Low, from the evidence in the rows) and Finding (partial settlement, possible duplicate, missing from the other report) columns on every result and Excel file.
- Audit trail: tamper-evident hash chain. Admin can check it at /api/admin/audit/verify.
- Maker-checker: set MAKER_CHECKER=on in Render. The officer who moves a case to review cannot also close it.
- Case ageing buckets (0-1, 2-7, 8-30, over 30 days) above the cases list.
- Searchable Help page with glossary (RRN, STAN, settlement, reversal...).
- Automated tests (tests/) and predeploy_check.py.

## Added in FINAL_21
- Background upload job with statuses (Uploaded, Validating, Processing, Completed, Completed with Warnings, Failed) and progress %. Duplicate uploads of the same file while one is running are not processed twice. Old upload route is kept as an automatic fallback. Jobs are held in memory: if the server restarts mid-upload, the user just uploads again.

## Already existed (found in audit)
Roles, company isolation, 2FA, encrypted files, backups, audit log, discrepancy cases with assignee/comments/history, scheduled reconciliation, anomaly checks, bank statement check, email/Telegram alerts, API tokens, branded PDF/Excel.

## Not done yet (needs real design and testing, one at a time)
- Background queue for reconciliation and exports (uploads are done).
- One-to-many / many-to-one matching and manual match/unmatch.
- Multi-currency, webhooks, custom report templates.
- Fraud scoring beyond the current anomaly checks; predictive analytics.
- Bank/switch API hub.
- Money columns as exact DECIMAL in PostgreSQL (needs a careful data migration).
