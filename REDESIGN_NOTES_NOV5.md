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

# Round 3 (Nov 6)
- Sections fade in as you scroll (off for people who prefer reduced motion); nav gets a soft shadow on scroll; thin styled scrollbar; buttons lift 1px on hover.
- Theme follows the phone/computer setting until the user picks one (then remembered).
- Mic button in "Ask a question" (shows only in browsers that support speech input).
- Friendly 404 page for mistyped addresses (API errors unchanged).
- Optional free Microsoft Clarity heatmaps: set CLARITY_PROJECT_ID in Render.
- Fixed: grey sections on the landing page were narrower than the header (a global `main` style leaked in).
- Not added on purpose: custom cursor, glow/glass/gradient/particle backgrounds, fake live-activity ticker, countdown timers, fake client logos, 3D hero.

# Round 4 (Nov 6): faster answers
Cause of 1-2 minute answers: each question makes two Gemini calls (plan, then written summary). Every call could wait 25 s, retry the same model, then try two backups (with sleeps), and a "thinking" model adds many seconds even when Google is healthy.
- core.py: a question now has a 30 s time budget (ANALYZE_TIME_BUDGET). One Gemini call waits at most 14 s (GEMINI_CALL_TIMEOUT), then moves straight to a backup model instead of retrying the slow one. The plan step keeps 8 s free for the summary; if the summary is late the answer shows the exact figures without the written part.
- Gemini "thinking" is switched off (thinkingBudget 0). If a model rejects the setting, the call is repeated once without it. Set GEMINI_THINKING_BUDGET=off to stop sending it, or a number to allow some thinking.
- Dashboard: the Analyze button shows the seconds while it works.
- If answers are still slow, look in Render > Logs for lines starting "[gemini]" (timed out / HTTP 429 / 503). Also keep the site awake with UptimeRobot on /api/health (a sleeping free Render service needs 30-60 s to wake up).

# Round 5 (Nov 6): onboarding, customer logos, About
- Get-started card (dashboard + Settings) tells new customers to open Settings first: step 1 save email, step 2 add company logo. It disappears when both are done (logo can be skipped). Customers without a saved email are taken to Settings after the first-time tour (or straight after sign-in if the tour was already seen). The tour's first step now says the same.
- Company logo and colour moved out of the owner-only admin card into its own Settings card for every customer (analyst accounts; viewers cannot). web.py: PUT /api/companies/{id}/branding now needs "analyst" plus access to that company (was admin only).
- PDF reports (report_pdf.py): coloured top edge, logo at the top, and a faint copy of the logo as the page background (shows through the table). No logo = no background; a broken logo never stops the PDF.
- Landing page: About ("The story behind Report Analyzer") section with nav + footer links, and three new FAQ items (getting started, logo on reports, who built it). OWNER: replace the About text (search for "OWNER:" in static/index.html) with your own story.
- Files changed: static/index.html, web.py, report_pdf.py.

# Round 6 (Nov 6)
- About section is now "Our story": a short intro plus a "Read the full story" chapter book (dream, TikTok, hard start, the question to his dad, day and night, where to find me, what comes next). Exact age and family workplace details are deliberately not published.
- Telegram t.me/COLDFX26 added to the About section, FAQ ("Do you have a WhatsApp number?"), contact note and footer; WhatsApp stated as not used.
- Search-engine data (JSON-LD for Organization and SoftwareApplication), press-down feedback on buttons, green/red field states for valid/invalid input.
- Not added: "Trusted by 500+ teams", SOC2 claim, fake logos/ticker, custom cursor, dark default (not true or not wanted).

# Round 7 (Nov 6, night): blue rebrand from the Reconciliation Solutions mockup
- Whole site moved from green to the mockup's blue (#0b5ce6) with navy (#08153a); light and dark themes both updated.
- New R logo (SVG symbol i-logo) and "Reconciliation Solutions" brand lockup in the nav, sidebar and footer.
- Landing: nav (Home, Features, Pricing, About, FAQ, Contact + Login and Get Started), new hero ("Turn Complex Financial Reports into Clear Results") with a tilted dashboard preview marked "Sample view / Sample result", rounded Secure/Fast/Built for Businesses bar, new "Why it is hard" section (fragmented systems, messy data, manual work), 3-step "How it works" with icons and arrows, navy call-to-action band, navy footer.
- Sign-in and sign-up now open as a pop-up card from Login / Get Started / pricing buttons (same fields and ids as before).
- App: dark navy sidebar with blue active item, coloured KPI cards (blue, green, orange, purple), navy hero card, blue charts.
- Spinner no longer shows on greyed-out buttons (for example the tour's Back button) and only appears after 350 ms of waiting.
- The numbers in the hero preview are decorative; it is labelled "Sample". Do not add real-looking statistics there unless they are real.

# Round 8 (Nov 6, night): owner's core.py + more speed
- core.py is the owner's edited version (gemini-3.8-flash, thinkingLevel "low", default temperature), plus:
  - thinkingLevel is sent only to gemini-3.x models, so backup models (flash-lite etc.) are not rejected and retried.
  - Backups are now gemini-flash-lite-latest then gemini-3.7-flash (3.8 was listed twice before).
  - GEMINI_SUMMARY_MODEL (optional): use a faster model such as gemini-flash-lite-latest just for the short written summary.
  - GEMINI_THINKING_LEVEL: low (default), medium or high. 3.8 Flash has no "minimal" or "off" level.
- web.py: the Excel file and the chart picture are now built at the same time after each answer.
- The app's top bar and the page title now say Reconciliation Solutions.
