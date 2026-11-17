# RECONZA build 42 - access levels, joining, welcome coupon, per-section payments, Profile

Changed: db.py, portal.py, extras.py, monetize.py, monetize_api.py, web.py, static/index.html, tests/test_monetize.py (stub), NEW seats.py, seats_api.py, tests/test_seats.py.

## Access levels
- Company creator = Owner (team, join approvals, billing). Works at Analyst level; the Admin tools (API keys, integrations, schedules, alerts, audit CSV) unlock with a one-time Admin unlock (N150,000). Owners that already existed keep every tool.
- People who join start as Viewer. Viewer -> Analyst N40,000, Analyst -> Admin N150,000, one-time per person, paid through the normal orders (kind "seat") and verified by you under Revenue > Team access. Owners cannot move people up for free any more; moving down is free.
- Approved sign-ups are now stored as Owner (before, a new company showed as a bare analyst until the next restart).
- Branding and deleting reports stay open to the owner.

## Joining
- Owner sees a join code in My Team. New person ticks "Joining a colleague's company?" on sign-up, enters the code, and waits for the owner to approve. They start as Viewer. Team seat limits of the plan still apply.

## Welcome coupon
- Each approved new company gets one single-use 30% coupon (valid 45 days, that company only), emailed and shown in Notifications; you get a Telegram alert with the code. Works on every order (packs, features, Marketplace, unlocks). It does not stack with membership discounts: the better one wins. Cancelling an order gives the coupon back.
- Companies approved before this build: POST /api/admin/coupons/welcome-test?company_id=ID issues theirs.

## Per-section payment verification
- Revenue tabs: Memberships, Marketplace (plus products), Report Studio, Packs & yearly, Team access, All payments. Each shows its own waiting payments with Verify / Reject, and a count on the tab.

## Profile
- New Profile page (click your name at the top or Profile in the sidebar): progress checklist, plan usage, access level, shortcuts. My Access, Plan & Billing, Membership, Business Services and Donations moved from the sidebar into it (still reachable).

## Tested
Predeploy passes; 16 new/updated unit tests pass (roles, joining, coupons, seat orders); browser tests with simulated server replies. The new web routes were not run against a live FastAPI server here (not installed), so smoke-test on Render: sign up with a join code, approve, unlock, coupon.
