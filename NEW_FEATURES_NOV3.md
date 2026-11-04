# Update 4 (Nov 3, on top of NEW_FEATURES_NOV1.md)

## Contact details
- Support email everywhere is now `reconciliationsolutionsupport@gmail.com` (receipts, support tickets, safety emails, notices, assistant help text, landing page, Terms and Privacy).
- All WhatsApp *contact* points are removed: floating support button, trust line, "Bigger teams" plan button, sidebar "Get Support", donation box, and the Terms/Privacy wording. The floating button is now "Contact support" (landing page: scrolls to the contact form; signed in: opens Support).
- Kept on purpose: the "Share on WhatsApp" buttons for customers sharing their own statement result or referral link (they do not contact you).

## Landing page
- Sample bank-statement check with no sign-up (invented data, computed once, no cost, cannot be abused): `GET /api/public/sample-statement`.
- "Who it's for" section and a strip of supported banks.
- Contact form: `POST /api/public/contact`. Messages are saved (encrypted) so none are lost even if email is not set up, you get a bell notification, and a copy is emailed to the support address when email is configured. Admin: Overview -> "Messages from the website" (Reply by email / Mark handled). Limits: 5 messages per hour per visitor, hidden spam field.
- Link previews and search: page description, Open Graph/Twitter tags, `/robots.txt`, `/sitemap.xml`. Set `PUBLIC_URL` (for example `https://your-site.onrender.com`) so previews use the right address; otherwise it is read from the request.
- FAQ: added "Which bank statements and files work?", "How do I pay?", "Can I talk to someone before I sign up?". The landing page no longer promotes Telegram.

## Sign-up protection
- Hidden spam field on the registration form (robots that fill it get a fake success and nothing is created).
- Successful sign-up requests are now limited too: 10 per hour per visitor and 60 per hour overall (previously only failed attempts counted).

## New optional environment variables
- `PUBLIC_URL` - the site's public address (link previews, sitemap).
- `SUPPORT_EMAIL` - overrides the support address used for notices (default is the Gmail above).

## Checked
- `python -m py_compile` on all changed Python files; `billing_test.py` (13 tests) passes; every inline script in `static/index.html` passes `node --check`; landing page exercised in a browser at phone and desktop width (sample check, contact form, floating button).
- NOT run in the build workspace: `smoke_test.py` and the FastAPI routes themselves (the web framework could not be installed there). Run `python smoke_test.py` and open `/robots.txt`, `/sitemap.xml`, the contact form and the registration form once after deploying.

## Pricing (moderate monthly plans)
- Starter ₦25,000, Growth ₦60,000, Professional ₦150,000: each 30 days of access (was 3, 5 and 7 days). Allowances and team sizes are unchanged (50 / 200 / 750 analyses per month; 3 / 5 / 10 members). Professional at ₦150,000 stays within the temporary payment-account limit.
- Business, Corporate and Enterprise are now custom quotes: hidden from the customer plan list, the payment request, the pro-forma invoice and the bot's plan list. Their payment requests are refused with a friendly message that points to Support. You can still grant them from Admin after agreeing a price (default 30 days).
- Landing page: four plan cards (Free, Starter, Growth - recommended, Professional) plus a "Business and above - get a custom quote" banner that scrolls to the contact form. Dashboard billing list and admin grant list updated.
- Existing subscriptions keep their current plan and expiry date. Only new payments and grants use the new prices and terms.
- Tests: `billing_test.py` updated (13 tests, passing).
