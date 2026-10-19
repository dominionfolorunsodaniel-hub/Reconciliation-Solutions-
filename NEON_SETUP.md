# Free persistent storage: Neon PostgreSQL

The app now runs in two modes, chosen by one environment variable:

- `DATABASE_URL` **not set** -> local SQLite file (same as before; data is lost on free Render redeploys).
- `DATABASE_URL` **set** -> Neon PostgreSQL. Logins, companies, billing, audit AND the encrypted report
  files and company logos are stored in the database, so they survive redeploys.

## Setup (about 10 minutes, all free)

1. Create a free project at neon.tech. Copy the **pooled** connection string
   (looks like `postgresql://user:password@ep-xxxx-pooler.region.aws.neon.tech/dbname?sslmode=require`).
2. In Render -> your **V2** service -> Environment, add `DATABASE_URL` with that string.
   Never put it in GitHub, `.env` files you upload, or chat. Do not touch V1.
3. Push this code to GitHub (Render redeploys automatically). On first start the tables are created for you.
4. Create the admin account (DASHBOARD_USER / DASHBOARD_PASSWORD), register a test customer, approve it.
5. Redeploy once from Render (Manual Deploy) and confirm the test customer can still log in. That is the real test.

## Test on a Neon *branch* first
Neon lets you create a branch (a copy) of your database in one click. Point a test deployment at the branch,
run through registration, login, upload, billing request/approval, PDF export and backup, then switch
`DATABASE_URL` to the main branch.

## Things to know
- Neon's free database sleeps when idle; the first request after a quiet period can take 1-3 seconds.
- Free storage is 0.5 GB. Report files are stored in the database, so watch usage, or set `RETENTION_DAYS`.
- Backups: in PostgreSQL mode the nightly backup contains `database.json` (all tables, including files).
  Restore into an EMPTY database with `python backup.py restore-db database.json` (start the app once first
  so the tables exist). Download backups off the server regularly; Render's disk is wiped on redeploy.
- Moving existing SQLite data: this package does not import an old `reports.db`. If the old data was already
  lost on redeploy there is nothing to move; otherwise ask for a one-off import script.
- Status: validated on SQLite (billing tests) and by translating every SQL statement. It has NOT yet been run
  against a live Neon database, so do the branch test above before real customers rely on it.
