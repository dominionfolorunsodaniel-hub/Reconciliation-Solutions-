# Build 34 - RECONZA, company roles, secure tour

## Roles (inside each company)
- Owner: created the account. Everything, plus My Team (add/change/remove people, reset their password) and billing.
- Admin: delete reports, company logo, API keys, schedules, alerts, integrations, company audit CSV. Cannot manage team or billing.
- Analyst: upload, ask, reconcile, forecast, statement score, manual matches, cases.
- Viewer: read-only (dashboard, reports, results, cases). The server blocks every write.
- The website owner (site "admin") is separate and unchanged. A company Admin is never a site admin.
- Existing accounts: the first customer login of each company becomes Owner automatically on first start; later logins become Analysts; viewers stay viewers.
- Seats follow the plan's team limit (Starter 3, Growth 5, Professional 10 ...), owner included.

## Server
db.py: users.company_role, list/add/set-role/remove/reset-password team functions. web.py: need_role(), /api/companies/{id}/team (GET/POST/PATCH/DELETE, password), /api/companies/{id}/activity.

## Brand
Name RECONZA everywhere. Logo files in static/brand (also at /brand/<file>). Icons, favicon and manifest updated. Tagline: We don't guess, we uncover.

## Tour
New "Your account is protected" step (email code on new devices + authenticator app) with a button to Settings. Tour is role-aware and shows once more to everyone (key ra_tour_v34).
Two-factor card is now always visible in Settings (it was hidden on phones).
