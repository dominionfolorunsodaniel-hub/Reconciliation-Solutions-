# Build 39 - landing page premium pass (only static/index.html changed)

- New design layer (`<style id="lp-premium">`, placed last): Inter, fluid type scale, 10/16/20px radii, 3 shadow levels, one focus ring.
- Content no longer stays hidden until scrolled: only hidden once JS is running, shown within 1.2s at most, always shown in print / reduced motion.
- Hero: outcome headline, "Try a sample check" + "Create free account", result mock (Test Pack 2 style sample, labelled Sample).
- Proof bar replaces the "Secure & Reliable / Fast & Accurate / Built for Businesses" strip (facts only, no invented numbers).
- Pricing: recommended card clearly leads; each card shows its membership badge (Regular grey, Starter teal, Professional violet, Business gold). Growth shows Starter Member because the server treats Growth as Starter. Prices and plan names unchanged.
- New "Security" section (encryption, verified sign-in, roles, audit trail) with links to /security /privacy /status.
- Loading skeleton on the sample check, aria-current on the nav, mobile pricing is a swipe row.
- Still needs YOU: 2 real testimonials in static/testimonials.json (set approved: true).
