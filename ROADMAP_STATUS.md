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

## Added in FINAL_23
- Reconciliation Quality Score (0-100): matching accuracy, unresolved exceptions, missing records, duplicates, reversals, calculation consistency. Shown on the page, in the PDF text and as an Excel sheet.
- Financial exposure: value of mismatches and of rows only in one report.
- Reversal / refund / failed rows flagged from their own description.
- Statement check (FINAL_22): transactions list, references, duplicates, unusual entries, net movement, row counts.

## Added in FINAL_24
- Grouped matching (one-to-many and many-to-one when the totals agree), flagged with Medium confidence.
- Daily settlement table: expected, settled, outstanding, average and longest delay (needs a date column in both reports).
- Currency-mix warning (no conversion yet).

## Still not built
- Manual match/unmatch screen (for now: resolve or accept the case with a note).
- Settlement delay prediction (needs months of history), multi-currency conversion, bank/switch APIs.

## Added in FINAL_25
- Admin-approved password reset: customer asks on the sign-in page, admin presses Approve & send code in Password resets, a random 6-digit code (15 minutes) is emailed/Telegrammed at once. Needs email set up on the server (RESEND_API_KEY or SMTP_*).
- Repeat-question cache (30 minutes, per user and report).
- Speed settings for Render: GEMINI_SUMMARY_MODEL=gemini-flash-lite-latest, GEMINI_THINKING_LEVEL=low, ANALYZE_TIME_BUDGET=20.

## Already existed (found in audit)
Roles, company isolation, 2FA, encrypted files, backups, audit log, discrepancy cases with assignee/comments/history, scheduled reconciliation, anomaly checks, bank statement check, email/Telegram alerts, API tokens, branded PDF/Excel.

## Not done yet (needs real design and testing, one at a time)
- Background queue for reconciliation and exports (uploads are done).
- One-to-many / many-to-one matching and manual match/unmatch.
- Multi-currency, webhooks, custom report templates.
- Fraud scoring beyond the current anomaly checks; predictive analytics.
- Bank/switch API hub.
- Money columns as exact DECIMAL in PostgreSQL (needs a careful data migration).
