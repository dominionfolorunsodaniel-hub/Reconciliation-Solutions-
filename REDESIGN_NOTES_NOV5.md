# Redesign (Nov 5): landing page + dashboard + other pages

Look: light/neutral theme, one green brand colour (#0b6b4b), no gradients, glow, animation or emoji, simple line icons, flat cards.

## Changed
- static/index.html: new landing page; dashboard (sidebar, top bar, home cards, all sections, pop-ups, bell) restyled with a new theme block (`#app-theme`, at the end of <head>); emoji replaced by line icons (menu, KPI cards) or plain words; chart colours changed to the green family.
- static/share.html (shared statement page) and static/offline.html restyled the same way.
- App icons (icon-192/512, maskable, apple-touch-icon) redrawn in green. manifest theme/background colour now white. Service worker cache name bumped to v2 so phones pick up the new offline page.
- Server messages (notifications, alerts, emails) no longer start with emoji. The Telegram bot file (bot.py) was left as it was.
- Dark mode still works (one dark palette, same green).

## iPhone / desktop fixes
- Form fields are 16px (iPhone Safari no longer zooms in when you tap a field); buttons and fields are at least 38-46px tall.
- iOS status bar style set to "default" (the old "black-translucent" overlapped light pages); safe-area padding; viewport-fit=cover.
- Phone header no longer scrolls sideways: theme switch and Log out moved into the menu (Account section) on screens under 900px.
- File upload field and form rows can no longer push cards wider than the phone screen.
- Page height uses 100dvh where supported (iPhone address bar).

## Tested (Chromium only)
1440, 1024, 390 (iPhone 14 profile) and 360 wide: landing page, signed-in dashboard (all 18 menu views, using stand-in data), shared page. No horizontal scroll, no script errors. Real Safari, Windows and Mac browsers and the real backend were NOT available here, so please test on your own devices.

## Not changed / notes
- Charts still load Chart.js from cdn.jsdelivr.net (could not be tested here because that address is blocked in the build workspace; it works normally on the internet).
- Old unused landing styles remain in index.html (harmless, can be removed later).

# Round 2 (Nov 6): polish pass
- Inter font (Google Fonts) everywhere; CSP in web.py now allows fonts.googleapis.com / fonts.gstatic.com.
- New headline, Fast/Exact/Simple highlights band, dark footer (product, legal, contact, (c) year), skip link, better placeholder contrast, more section spacing.
- Spinner on any button the page disables while working.
- Exit-intent offer (desktop only, once per visit): free 8-point reconciliation checklist; the email lands in the admin contact inbox.
- Optional analytics, OFF by default: set PLAUSIBLE_DOMAIN or GA_MEASUREMENT_ID in Render.
- Not done on purpose: fake "trusted by" logos, fake scarcity, 99.9% claims, Vercel/Netlify move (backend is Python + database).
- Keep Render awake for free: point UptimeRobot at /api/health every 5 minutes.
