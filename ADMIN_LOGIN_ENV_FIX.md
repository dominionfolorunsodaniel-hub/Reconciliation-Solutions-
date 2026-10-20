# V2 Admin Login — Render Environment Variables

This V2 update makes `DASHBOARD_USER` and `DASHBOARD_PASSWORD` the authoritative web-dashboard admin credentials.

## Render setup
1. Open the **V2 Render service only** (do not change the V1 service).
2. In Environment, set `DASHBOARD_USER` to the desired admin username.
3. Set `DASHBOARD_PASSWORD` to a strong password of at least 10 characters.
4. Save changes and let Render redeploy/restart.
5. Log in using those exact values.

On login, V2 synchronizes the configured admin identity with an existing admin record or creates one if necessary. Customer accounts and company/report data are not deleted. Changing the configured admin credentials revokes existing admin sessions.

Security notes:
- Do not share or commit the password.
- Keep V1 and its environment variables untouched.
- If the configured admin username already belongs to a non-admin customer account, the system fails safely; choose a different admin username.
- These tests ran locally against the packaged code. Live Render/Postgres behavior still needs to be verified on the V2 service after deployment.
