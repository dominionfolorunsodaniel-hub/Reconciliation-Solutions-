# V2 design upgrade notes

This update adds a beginner-friendly sidebar and guided tour to `static/index.html` while keeping the existing API and authentication code in `web.py` unchanged.

## Included
- Sidebar links for Home, My Files, Ask a Question, Compare Files, Results, Past Questions, My Plan, and Help & Guide.
- Compact bottom navigation on mobile screens.
- A first-login tour with Next, Back, Skip tour, and Finish controls.
- A Help button in the app header to replay the tour.
- Plain-language headings for adding files, asking questions, and comparing files.

## Important
- This is a V2 test build. Do not replace or redeploy the working V1 service.
- The sidebar is a navigation layer over existing sections; it does not create new backend features.
- Existing billing, reports, login, registration, admin, and Telegram behavior are retained from the supplied ZIP.
- Persistent SQLite data still requires a persistent disk on the hosting service. A front-end redesign does not solve storage persistence.
- Before using with real customer data, test login, signup approval, uploads, questions, comparison, billing, and admin actions on the separate V2 service.
