# Redesign, step 1: landing page (Nov 5)

- Landing page rebuilt: light theme, one green brand colour, no gradients, glow or emoji, simple line icons, plain wording.
- All features, sign-in / forgot password / request account, sample statement check, contact form and the new pricing are kept (same element ids, so the existing JavaScript still works).
- Phone fixes: form fields are 16px (iPhone Safari no longer zooms in when you tap a field), safe-area padding, solid header, no fixed-height sections, iOS status bar style set to default (the old black-translucent style overlapped light pages).
- The floating "Contact support" button is hidden on the landing page (Contact is in the header and footer); it still appears inside the dashboard.
- Emoji removed from the landing sample-check and contact messages.
- NOT yet done: dashboard redesign, and old unused landing CSS is still in index.html (to be removed with the dashboard step).
- Tested in Chromium at 1440, 1024, 390 (iPhone 14 profile) and 360 widths: no horizontal scroll, no script errors. Real Safari was not available for testing.
