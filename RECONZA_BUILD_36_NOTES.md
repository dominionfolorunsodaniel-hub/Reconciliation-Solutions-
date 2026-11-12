# RECONZA build 36: Membership, Marketplace, Report Studio, Business Services

## Monetisation strategy
| | Regular | Starter | Professional | Business |
|---|---|---|---|---|
| Price | Free (2-day trial) | ₦25,000 / 30 days | ₦150,000 / 30 days | from ₦1,000,000 / 30 days (arranged first) |
| Analyses / month | 5 | 50 | 750 | 1,500 |
| Team seats | 1 | 3 | 10 | 15 |
| Standard Excel + PDF downloads | yes | yes | yes | yes |
| Excel reconciliation export | buy ₦3,000 | included | included | included |
| Professional PDF report | buy ₦5,000 | buy | included | included |
| Advanced discrepancy summary | buy ₦4,000 | buy | included | included |
| Company-branded report | buy ₦8,000 | buy | buy | included |
| Management report | buy ₦10,000 | buy | buy | included |
| Marketplace discount | 0 | 10% | 20% | 30% |
| Badge | none | Starter Member | Professional Member | Business Member |

Prices come from the existing plan table (db.PLANS), so billing and the pages can never disagree. Legacy Growth counts as Starter; Corporate/Enterprise/lifetime count as Business.
One-time purchases: single premium reports (per result) and Marketplace templates (₦2,500 to ₦7,500, 6 seeded; admin can add more).
Revenue streams: memberships, Marketplace, single reports, Business enquiries.

## Payment flow (nothing replaced)
- Memberships: unchanged. Existing payment request, proof upload, admin approval, invoice (Plan & Billing).
- Marketplace and single reports: new order, same bank accounts (receipts.bank_accounts), customer enters transfer reference (+ optional screenshot, forwarded to Telegram), admin verifies under Revenue > Transactions. Status: awaiting payment, pending, paid, failed. Unique references; approving twice does nothing; credits and downloads only exist after "paid".
- Paystack: `monetize.mark_order_paid(order_ref, "paystack", provider_ref, amount)` is ready for your webhook. Call it only after Paystack's verify call succeeds. It re-checks the amount and is safe to call twice.

## Security
Tier, price, badge and entitlements are computed on the server from verified records. Badge needs an active subscription from verified payment or an audit-logged admin grant (trial and referral rewards get no badge). Expired, revoked: no badge (24h grace keeps analysis working but drops paid status). Downloads need a paid order for the caller's own company; results are only exportable by their owner.

## Database (additive, reversible)
New tables: products, orders, feature_credits, enquiries. Only change to an existing table logic: grace period now skips status "revoked". Undo: drop the 4 tables; nothing else depends on them.

## Known limitations (honest list)
- Report-history length per tier (30/90/365 days) is NOT enforced; existing reports are never hidden.
- Business is arranged by enquiry, then granted by admin (Revenue > Customers > Manage), as the site already required for plans above ₦150,000. No online Business checkout.
- Membership upgrades still use Plan & Billing (owner only).
- Premium reports need a bank-statement or ledger result (verified facts). Other result types are refused before any credit is used.
- Exports use the latest result in your session; results live in memory (last 50), so re-run if the site restarted.
- No live Paystack or Postgres test was possible here; FastAPI routes were tested by calling them directly, not over HTTP. Please smoke-test once after deploy.
- Marketplace files are stored encrypted in the database (3 MB max each).

## Build 38 additions
- Analysis packs (pay as you go): 10 for ₦7,000, 25 for ₦16,000, 60 for ₦36,000, valid 90 days, used only after the monthly allowance (works even when the plan has expired). A failed analysis returns the credit. New table analysis_packs and column monthly_usage.pack_id (db.reserve_monthly_usage / finish_monthly_usage / get_plan_status changed minimally).
- Yearly plans for Starter and Professional: 12 months for the price of 10, same order and verification flow; renewing the same plan keeps unused days; buying a lower tier is blocked.
- 2FA reminder for owners/admins on money pages (a reminder, not forced: forcing it could lock people out).
- Landing-page savings calculator (an adjustable estimate, labelled as such).
- Already existed, so not rebuilt: referral rewards (extras.py) and a hidden demo-video section on the landing page.
- NOT built yet: approval workflow and comments on transactions (large, needs its own release); automatic Paystack confirmation (hook `mark_order_paid` is ready; needs your Paystack keys and a test).
