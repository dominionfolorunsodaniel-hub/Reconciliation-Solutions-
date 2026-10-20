# V2 payment changes and remaining work

## Temporary account setup
The payment page now supports two accounts configured through Render environment variables. Set these only after verifying the account details and that the providers permit the intended business use:

- `MONIEPOINT_ACCOUNT`
- `MONIEPOINT_OWNER`
- `MONIEPOINT_BANK_NAME` (optional; defaults to Moniepoint)
- `OPAY_ACCOUNT`
- `OPAY_OWNER`
- `OPAY_BANK_NAME` (optional; defaults to OPay)

The UI shows a temporary-payment notice and asks customers to contact the admin for plans costing more than ₦150,000. The server independently rejects payment requests for plans above ₦150,000. Existing `BANK_NAME`, `BANK_ACCOUNT`, and `BANK_OWNER` variables remain for backward compatibility.

## Important remaining work
- Admin-to-company inbox/messaging is not implemented by this change and needs a database-backed message table, company-scoped customer inbox, admin composer, unread state, authorization checks, and tests.
- Company assignment rules need an explicit migration and API tests against the live Neon database before deployment. Do not alter existing company records blindly.
- Validate Neon connectivity and login recovery on the V2 service before deploying. Keep V1 and `backup-before-registration` untouched.
