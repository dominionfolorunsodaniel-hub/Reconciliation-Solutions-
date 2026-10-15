# Report Analyzer update notes

## Customer onboarding and tenant separation

- Added an owner/admin-only `POST /api/customers` endpoint.
- Added an **Onboard a new customer** form to the dashboard Admin panel.
- A new customer is always created with the `analyst` role and a dedicated company record.
- Company, user, and company assignment are written in one SQLite transaction.
- Duplicate company names, duplicate usernames, and duplicate Telegram IDs return clear errors.
- The dashboard records customer creation in the audit log.
- Added regression tests for onboarding, role enforcement, and cross-company isolation.
- Updated README and Render deployment notes with safe customer testing steps.

## Validation performed

- Python compilation passed for all project Python files.
- Full `python smoke_test.py` suite passed.
- Dashboard inline JavaScript passed `node --check`.

## Not included yet

Payments/subscriptions, customer support ticket center, owner broadcast/message center, scheduled announcements, promo/referral system, maintenance mode, and revenue analytics still need implementation.
