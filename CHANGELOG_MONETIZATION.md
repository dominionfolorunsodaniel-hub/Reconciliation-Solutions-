# V2 monetization and fixed-term pricing update

- Free Trial: 2 days, 5 analyses per company per calendar month, 1 member.
- Starter: ₦25,000 / 3 days; Growth: ₦100,000 / 5 days; Professional: ₦350,000 / 7 days.
- Business: ₦1,000,000 / 10.5 days; Corporate: ₦2,500,000 / 14 days.
- Enterprise: ₦5,000,000 / 21 days; Enterprise Plus: temporarily hidden from new purchases pending payment-provider limit confirmation (legacy records remain supported).
- Existing per-plan monthly analysis quotas and team limits remain unchanged; quota accounting uses calendar months even for shorter access terms.
- New companies receive a single trial subscription. Expired trial/subscription access is blocked until an eligible paid plan is activated.
- Paid access remains pending until an administrator verifies the bank transfer in the actual account.
- Company Lifetime is retained for compatibility with historical subscription records but is not offered for new purchases.
- V1 is not modified or deployed by this update.


## Enterprise workflows and staged UI update
- Added dashboard Company operations tools for discrepancy case resolution/activity, trends/run history, scheduled reconciliation, email/quota settings, invoice PDF receipts, integration token management, and admin backup/restore drill.
- Added admin API to create a fresh backup, validate archive + SQLite consistency, and restore into a temporary directory without overwriting production data.
- Fixed integration report ingestion to persist the original uploaded bytes, and fixed admin grant validation to allow 10.5-day Business terms.
- Added API smoke tests for the new workflows and documented direct accounting-vendor connectors as deferred until vendor credentials are selected.

## Payment-provider readiness adjustment
- Reduced the advertised Enterprise price from ₦10,000,000 to ₦5,000,000 for its existing 21-day term.
- Temporarily disabled Enterprise Plus for new payment requests and admin grants; legacy plan records remain readable.
- Paystack/Flutterwave checkout is not integrated in this release. Current payment flow remains manual transfer plus admin verification.
