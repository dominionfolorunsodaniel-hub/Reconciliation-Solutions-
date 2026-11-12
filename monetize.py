"""RECONZA monetisation: membership tiers + badges, Marketplace, Premium Report Studio, Business enquiries, revenue admin.

Design rules
- Nothing here talks to a payment provider and nothing replaces the existing payment flow. Membership still uses the existing
  payment_requests / admin-approval flow (bank transfer today, Paystack when it is switched on). Marketplace and Report Studio
  orders use the SAME bank accounts, the SAME admin verification and Telegram alert, and a provider hook (`mark_order_paid`)
  that a Paystack webhook can call after it has verified the charge with Paystack.
- A badge or a download is only ever derived from verified records on the server (an active subscription with grant_source
  'payment' or 'admin', or an order with status 'paid'). The browser is never asked what tier or what was paid.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import re
import secrets

import db

log = logging.getLogger("monetize")

# ----------------------------------------------------------------------------------------------- strategy / catalogue
TIERS = {
    "regular": {"name": "Regular", "badge": None, "rank": 0, "price_ngn": 0, "market_discount": 0,
                "tagline": "Everything you need to try RECONZA", "perks": []},
    "starter": {"name": "Starter", "badge": "Starter Member", "rank": 1, "price_ngn": 25000, "market_discount": 10,
                "tagline": "For individuals and small shops", "perks": ["Excel reconciliation export"]},
    "professional": {"name": "Professional", "badge": "Professional Member", "rank": 2, "price_ngn": 150000, "market_discount": 20,
                     "tagline": "For accountants and finance teams", "perks": ["Professional PDF report", "Advanced discrepancy summary"]},
    "business": {"name": "Business", "badge": "Business Member", "rank": 3, "price_ngn": 1000000, "market_discount": 30,
                 "tagline": "For companies, firms and fintechs", "perks": ["Company-branded reports", "Management report"]},
}
# Which tier a stored plan belongs to. Growth (legacy mid plan) is treated as Starter; Corporate/Enterprise/lifetime as Business.
PLAN_TIER = {"starter": "starter", "growth": "starter", "professional": "professional", "business": "business",
             "corporate": "business", "enterprise": "business", "enterprise_plus": "business", "lifetime": "business"}
BADGE_SOURCES = ("payment", "admin")     # verified payment, or an audit-logged manual grant. Trials and referral rewards get no badge.

FEATURES = {
    "excel_recon": {"name": "Excel reconciliation export", "tier": "starter", "price_ngn": 3000,
                    "desc": "The full verified workbook: daily activity, duplicate references, unusual transactions, data-quality checks and validation.",
                    "file": "reconza-reconciliation.xlsx"},
    "pro_pdf": {"name": "Professional PDF report", "tier": "professional", "price_ngn": 5000,
                "desc": "Executive summary, financial breakdown, daily activity, findings, recommendations and page numbers.",
                "file": "reconza-professional-report.pdf"},
    "discrepancy": {"name": "Advanced discrepancy summary", "tier": "professional", "price_ngn": 4000,
                    "desc": "Duplicate references, unusual transactions and data-quality issues with exact references, dates and amounts (Excel).",
                    "file": "reconza-discrepancy-summary.xlsx"},
    "branded_pdf": {"name": "Company-branded report", "tier": "business", "price_ngn": 8000,
                    "desc": "The professional PDF with your company logo, name and colour (set under Settings > Branding).",
                    "file": "reconza-branded-report.pdf"},
    "mgmt_report": {"name": "Management report", "tier": "business", "price_ngn": 10000,
                    "desc": "A board-ready summary: key figures, findings, data quality, verification status and recommendations, in your branding.",
                    "file": "reconza-management-report.pdf"},
}
for _k, _f in FEATURES.items():
    _f["key"] = _k
CATEGORIES = ["Reconciliation", "Reporting", "Cash flow", "Expenses", "Monitoring", "Audit"]
ORDER_STATUSES = ("awaiting_payment", "pending", "paid", "failed", "cancelled")
MAX_FILE = 3 * 1024 * 1024


def _now() -> str:
    return db._now()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ----------------------------------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    details TEXT,
    price_ngn INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    file_name TEXT,
    file_mime TEXT,
    file_data TEXT,
    sort INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_slug ON products(slug);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref TEXT NOT NULL,
    company_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    title TEXT NOT NULL,
    list_price_ngn INTEGER NOT NULL,
    amount_ngn INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    transfer_reference TEXT,
    provider TEXT,
    provider_ref TEXT,
    proof_data TEXT,
    proof_mime TEXT,
    proof_at TEXT,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    verified_at TEXT,
    verified_by INTEGER,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ref ON orders(order_ref);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_transfer ON orders(transfer_reference);
CREATE INDEX IF NOT EXISTS idx_orders_company ON orders(company_id, status);
CREATE TABLE IF NOT EXISTS feature_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    feature TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    used_result TEXT,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credits_order ON feature_credits(order_id);
CREATE TABLE IF NOT EXISTS enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    user_id INTEGER,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    company_name TEXT,
    phone TEXT,
    team_size TEXT,
    interest TEXT,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    admin_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
"""


def schema(con) -> None:
    """Additive only: no existing table is touched. To undo, drop products, orders, feature_credits and enquiries."""
    con.executescript(SCHEMA)


# ----------------------------------------------------------------------------------------------- membership
def membership(company_id: int) -> dict:
    """Everything the customer dashboard shows about membership, derived only from verified server records."""
    st = db.get_plan_status(company_id)
    plan = st["plan"]
    source = st.get("subscription_source")
    tier = PLAN_TIER.get(plan, "regular")
    active_paid = tier != "regular" and not st.get("in_grace") and source in BADGE_SOURCES
    tier_eff = tier if active_paid else "regular"
    if active_paid:
        status = "active"
    else:
        status = "grace" if (st.get("in_grace") and tier != "regular") else ("expired" if plan == "expired" else "active")
    with db._db() as con:
        last = con.execute("SELECT status, plan, expires_at FROM subscriptions WHERE company_id=? AND plan NOT IN ('free','expired') "
                           "ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
    last_state = None
    if tier_eff == "regular" and last:
        last_state = "revoked" if last["status"] == "revoked" else "expired"
    t = TIERS[tier_eff]
    return {
        "company_id": company_id, "tier": tier_eff, "tier_name": t["name"], "badge": t["badge"] if tier_eff != "regular" else None,
        "plan": plan, "plan_name": st["name"], "status": status, "last_membership": last_state,
        "expires_at": st.get("expires_at"), "in_grace": st.get("in_grace"), "grace_ends_at": st.get("grace_ends_at"),
        "used": st["used"], "limit": st["monthly_limit"], "remaining": st["remaining"], "team_limit": st["team_limit"],
        "members": db.list_company_members(company_id), "market_discount": t["market_discount"],
        "features": entitlements(tier_eff), "pending_payment_requests": st.get("pending_payment_requests", 0),
    }


def entitlements(tier: str) -> dict:
    rank = TIERS[tier]["rank"]
    return {k: TIERS[f["tier"]]["rank"] <= rank for k, f in FEATURES.items()}


def catalogue() -> dict:
    """The public plan comparison (prices come from the real plan table so they can never disagree with billing)."""
    out = []
    for key, t in TIERS.items():
        plan_key = {"regular": "free", "starter": "starter", "professional": "professional", "business": "business"}[key]
        p = db.PLANS[plan_key]
        out.append({"key": key, "name": t["name"], "badge": t["badge"], "tagline": t["tagline"], "price_ngn": p["price_ngn"],
                    "term": "2-day trial" if key == "regular" else "30 days", "analyses": p["monthly_limit"], "team": p["team_limit"],
                    "market_discount": t["market_discount"], "plan_key": plan_key, "self_serve": plan_key in db.SELF_SERVE_PLANS,
                    "features": entitlements(key)})
    pk = [{"key": k, "name": db.PLANS[k]["name"], "price_ngn": db.PLANS[k]["price_ngn"], "analyses": db.PLANS[k]["monthly_limit"], "team": db.PLANS[k]["team_limit"]}
          for k in ("business", "corporate", "enterprise")]
    return {"tiers": out, "features": [dict(f) for f in FEATURES.values()], "packages": pk}


# ----------------------------------------------------------------------------------------------- products
def _seed_files() -> list[dict]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    head = PatternFill("solid", fgColor="0F1A2E")

    def book(title, headers, rows=0, widths=None, formulas=None, notes=None):
        wb = Workbook(); ws = wb.active; ws.title = title[:30]
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = head; c.alignment = Alignment(wrap_text=True, vertical="center")
        for r in range(2, rows + 2):
            if formulas:
                ws.append([f.replace("{r}", str(r)) if isinstance(f, str) and f.startswith("=") else None for f in formulas])
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[chr(64 + i)].width = w
        ws.freeze_panes = "A2"
        if notes:
            n = wb.create_sheet("How to use")
            for line in notes:
                n.append([line])
            n.column_dimensions["A"].width = 110
        b = io.BytesIO(); wb.save(b)
        return b.getvalue()

    X = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return [
        {"slug": "bank-reconciliation-template", "name": "Bank Reconciliation Template", "category": "Reconciliation", "price_ngn": 4500, "sort": 10,
         "description": "A month-end bank reconciliation workbook: book balance, bank balance, outstanding items and an automatic difference check.",
         "details": "Cash-book vs bank statement columns\nAuto 'Difference' and 'Reconciled?' cells\nOutstanding cheques and deposits in transit\nInstructions sheet included",
         "file_name": "bank-reconciliation-template.xlsx", "mime": X,
         "data": book("Reconciliation", ["Date", "Reference", "Description", "Cash book (NGN)", "Bank statement (NGN)", "Difference", "Status"], 200,
                      [12, 18, 40, 18, 20, 16, 16], [None, None, None, None, None, "=D{r}-E{r}", '=IF(AND(D{r}="",E{r}=""),"",IF(ROUND(F{r},2)=0,"Matched","Check"))'],
                      ["1. Paste your cash-book lines in column D and the matching bank lines in column E.", "2. Difference and Status fill in by themselves.",
                       "3. Anything marked Check is an outstanding or unexplained item to investigate."])},
        {"slug": "financial-reporting-pack", "name": "Financial Reporting Pack", "category": "Reporting", "price_ngn": 7500, "sort": 20,
         "description": "Monthly income statement and balance summary layout with totals and margin formulas, ready for management reporting.",
         "details": "Revenue, cost and expense sections\nGross and net margin formulas\nMonth-by-month columns\nReady to hand to a director or lender",
         "file_name": "financial-reporting-pack.xlsx", "mime": X,
         "data": book("Income statement", ["Line item", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Total"], 30,
                      [34] + [13] * 13, [None] * 13 + ["=SUM(B{r}:M{r})"],
                      ["List revenue lines first, then cost of sales, then expenses.", "The Total column sums each row. Add a net-profit row at the bottom with =SUM of revenue minus costs."])},
        {"slug": "cash-flow-tracker", "name": "Cash-Flow Tracker", "category": "Cash flow", "price_ngn": 3500, "sort": 30,
         "description": "Track money in and out day by day with a running balance, so you always see where cash stands.",
         "details": "Running balance formula\nIn / out columns\nCategory column for later filtering\nWorks for personal and business cash",
         "file_name": "cash-flow-tracker.xlsx", "mime": X,
         "data": book("Cash flow", ["Date", "Description", "Category", "Money in", "Money out", "Running balance"], 365, [12, 40, 20, 16, 16, 20],
                      [None, None, None, None, None, "=SUM($D$2:D{r})-SUM($E$2:E{r})"],
                      ["Enter each transaction on a new row. The running balance updates automatically."])},
        {"slug": "expense-management-template", "name": "Expense Management Template", "category": "Expenses", "price_ngn": 3000, "sort": 40,
         "description": "Log, categorise and total business expenses with an approval column for team use.",
         "details": "Category and vendor columns\nApproval status\nVAT column\nTotals by row",
         "file_name": "expense-management-template.xlsx", "mime": X,
         "data": book("Expenses", ["Date", "Vendor", "Category", "Amount", "VAT", "Total", "Approved by", "Receipt no."], 300, [12, 28, 20, 14, 12, 14, 18, 16],
                      [None, None, None, None, None, "=D{r}+E{r}", None, None], ["Fill one row per receipt. Total = Amount + VAT."])},
        {"slug": "transaction-monitoring-sheet", "name": "Transaction Monitoring Spreadsheet", "category": "Monitoring", "price_ngn": 6000, "sort": 50,
         "description": "Flag large, repeated and round-figure transactions automatically while you review a statement or ledger.",
         "details": "Large-transaction threshold cell\nRepeat-reference check\nRound-number flag\nReviewer sign-off column",
         "file_name": "transaction-monitoring-sheet.xlsx", "mime": X,
         "data": book("Monitoring", ["Date", "Reference", "Amount", "Large? (> threshold)", "Repeated reference?", "Round figure?", "Reviewer", "Comment"], 500,
                      [12, 18, 16, 20, 20, 16, 16, 40],
                      [None, None, None, '=IF(C{r}="","",IF(C{r}>$K$1,"REVIEW",""))', '=IF(B{r}="","",IF(COUNTIF($B$2:$B$501,B{r})>1,"DUPLICATE",""))',
                       '=IF(C{r}="","",IF(MOD(C{r},1000)=0,"ROUND",""))', None, None],
                      ["Type your large-transaction limit in cell K1.", "Paste transactions in columns A to C. Flags appear on their own."])},
        {"slug": "financial-audit-checklist", "name": "Financial Audit Checklist", "category": "Audit", "price_ngn": 2500, "sort": 60,
         "description": "A structured checklist covering cash, receivables, payables, payroll, tax and controls for internal or external audits.",
         "details": "40+ audit procedures\nStatus, owner and evidence columns\nCovers cash, AR, AP, payroll, tax, controls\nSign-off block",
         "file_name": "financial-audit-checklist.xlsx", "mime": X, "data": None},
    ]


def _audit_checklist() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    items = {
        "Cash and bank": ["Bank reconciliations prepared and reviewed monthly", "Unreconciled items older than 30 days investigated", "Petty cash counted and agreed to records",
                          "Bank signatories and mandates current", "Large or unusual transfers supported by approvals", "Cut-off tested at period end"],
        "Receivables": ["Customer balances agreed to ledger", "Aged debtors reviewed", "Bad-debt provision assessed", "Credit notes approved", "Revenue recognised in the right period"],
        "Payables": ["Supplier statements reconciled", "Unrecorded liabilities searched", "Duplicate invoices checked", "Payments approved by an authorised person", "Accruals reasonable"],
        "Payroll": ["Payroll agrees to HR records", "Starters and leavers tested", "PAYE, pension and other deductions remitted", "Overtime and bonuses approved"],
        "Tax and compliance": ["VAT returns agree to ledger", "Withholding tax deducted and remitted", "Statutory filing deadlines met", "Licences and registrations current"],
        "Controls and records": ["Segregation of duties in place", "Journal entries reviewed", "Backups of financial records tested", "Fixed-asset register agrees to ledger", "Management accounts reviewed monthly"],
    }
    wb = Workbook(); ws = wb.active; ws.title = "Audit checklist"
    ws.append(["Area", "Procedure", "Status (Done / Open / N/A)", "Owner", "Evidence / reference", "Reviewer sign-off"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="0F1A2E")
    for area, rows in items.items():
        for r in rows:
            ws.append([area, r, "Open", "", "", ""])
    for col, w in zip("ABCDEF", (22, 60, 24, 16, 28, 20)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def seed_products() -> int:
    """Put the starter catalogue in an empty store (never touches products an admin already created)."""
    with db._db() as con:
        if con.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]:
            return 0
    n = 0
    for p in _seed_files():
        data = p["data"] or _audit_checklist()
        try:
            add_product(p["name"], p["category"], p["description"], p["details"], p["price_ngn"], p["file_name"], p["mime"], data, p["sort"], slug=p["slug"])
            n += 1
        except Exception:  # noqa: BLE001  seeding must never stop the site starting
            log.exception("could not seed product %s", p["slug"])
    return n


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "product"
    return s


def _product_public(r, owned=False, discount=0) -> dict:
    price = int(r["price_ngn"])
    pay = max(0, price - price * discount // 100)
    return {"id": r["id"], "slug": r["slug"], "name": r["name"], "category": r["category"], "description": r["description"],
            "details": [x for x in (r["details"] or "").split("\n") if x.strip()], "price_ngn": price, "your_price_ngn": pay,
            "active": bool(r["active"]), "file_name": r["file_name"], "has_file": bool(r["file_name"]), "owned": owned, "sort": r["sort"]}


def _check_product(name, category, description, price_ngn):
    name, description = (name or "").strip(), (description or "").strip()
    if not 3 <= len(name) <= 80:
        raise ValueError("Product name must be 3-80 characters.")
    if category not in CATEGORIES:
        raise ValueError("Choose a category: " + ", ".join(CATEGORIES) + ".")
    if not 10 <= len(description) <= 600:
        raise ValueError("Description must be 10-600 characters.")
    try:
        price = int(price_ngn)
    except (TypeError, ValueError):
        raise ValueError("Price must be a whole number of naira.")
    if not 0 <= price <= 5_000_000:
        raise ValueError("Price must be between 0 and 5,000,000.")
    return name, description, price


def add_product(name, category, description, details, price_ngn, file_name, mime, data: bytes | None, sort=100, slug=None) -> dict:
    name, description, price = _check_product(name, category, description, price_ngn)
    if data is not None and len(data) > MAX_FILE:
        raise ValueError("The file is too large (3 MB maximum).")
    slug = slug or _slug(name)
    blob = db._enc_text(base64.b64encode(data).decode()) if data else None
    with db._db() as con:
        base, i = slug, 1
        while con.execute("SELECT 1 FROM products WHERE slug=?", (slug,)).fetchone():
            i += 1; slug = f"{base}-{i}"
        cur = con.execute("INSERT INTO products(slug,name,category,description,details,price_ngn,active,file_name,file_mime,file_data,sort,created_at,updated_at) "
                          "VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?)",
                          (slug, name, category, description, (details or "")[:800], price, (file_name or "")[:120] or None, mime, blob, int(sort), _now(), _now()))
        return {"id": cur.lastrowid, "slug": slug}


def update_product(pid: int, fields: dict, data: bytes | None = None, file_name=None, mime=None) -> None:
    with db._db() as con:
        r = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not r:
            raise LookupError("Product not found.")
        name, description, price = _check_product(fields.get("name", r["name"]), fields.get("category", r["category"]),
                                                  fields.get("description", r["description"]), fields.get("price_ngn", r["price_ngn"]))
        active = 1 if fields.get("active", bool(r["active"])) else 0
        details = (fields.get("details", r["details"]) or "")[:800]
        con.execute("UPDATE products SET name=?,category=?,description=?,details=?,price_ngn=?,active=?,sort=?,updated_at=? WHERE id=?",
                    (name, fields.get("category", r["category"]), description, details, price, active, int(fields.get("sort", r["sort"])), _now(), pid))
        if data is not None:
            if len(data) > MAX_FILE:
                raise ValueError("The file is too large (3 MB maximum).")
            con.execute("UPDATE products SET file_name=?, file_mime=?, file_data=? WHERE id=?",
                        ((file_name or "download")[:120], mime or "application/octet-stream", db._enc_text(base64.b64encode(data).decode()), pid))


def remove_product(pid: int) -> str:
    """Delete when nobody bought it; otherwise hide it (buyers keep their downloads and the history stays intact)."""
    with db._db() as con:
        if not con.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise LookupError("Product not found.")
        bought = con.execute("SELECT COUNT(*) AS n FROM orders WHERE kind='product' AND item_key=?", (str(pid),)).fetchone()["n"]
        if bought:
            con.execute("UPDATE products SET active=0, updated_at=? WHERE id=?", (_now(), pid))
            return "hidden"
        con.execute("DELETE FROM products WHERE id=?", (pid,))
        return "deleted"


def list_products(company_id: int | None = None, include_inactive=False) -> list[dict]:
    discount = membership(company_id)["market_discount"] if company_id else 0
    with db._db() as con:
        rows = con.execute("SELECT * FROM products" + ("" if include_inactive else " WHERE active=1") + " ORDER BY sort, id").fetchall()
        owned = set()
        if company_id:
            owned = {int(r["item_key"]) for r in con.execute("SELECT item_key FROM orders WHERE company_id=? AND kind='product' AND status='paid'", (company_id,)).fetchall()}
    return [_product_public(r, r["id"] in owned, discount) for r in rows]


def product_file(pid: int, company_id: int | None, is_admin=False) -> tuple[bytes, str, str]:
    """The file, only when the company has a PAID order for it (or the caller is the site admin)."""
    with db._db() as con:
        r = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not r or not r["file_data"]:
            raise LookupError("This product has no file yet.")
        if not is_admin:
            ok = con.execute("SELECT 1 FROM orders WHERE company_id=? AND kind='product' AND item_key=? AND status='paid'", (company_id, str(pid))).fetchone()
            if not ok:
                raise PermissionError("Buy this product to download it.")
    return base64.b64decode(db._dec_text(r["file_data"])), r["file_mime"] or "application/octet-stream", r["file_name"] or "download"


# ----------------------------------------------------------------------------------------------- orders & payment
def _ref() -> str:
    return "RZ-" + dt.datetime.now(dt.timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()


def create_order(company_id: int, user_id: int, kind: str, item_key: str) -> dict:
    """Server decides the title and the price. The browser only says WHAT it wants."""
    m = membership(company_id)
    if kind == "product":
        with db._db() as con:
            p = con.execute("SELECT * FROM products WHERE id=? AND active=1", (int(item_key) if str(item_key).isdigit() else -1,)).fetchone()
        if not p:
            raise LookupError("That product is not available.")
        title, list_price = p["name"], int(p["price_ngn"])
        amount = max(0, list_price - list_price * m["market_discount"] // 100)
        item_key = str(p["id"])
    elif kind == "feature":
        f = FEATURES.get(item_key)
        if not f:
            raise LookupError("Unknown report feature.")
        if m["features"].get(item_key):
            raise ValueError(f"{f['name']} is already included in your {m['tier_name']} membership, so there is nothing to buy.")
        title, list_price, amount = f["name"] + " (one report)", f["price_ngn"], f["price_ngn"]
    else:
        raise ValueError("Unknown purchase type.")
    if amount <= 0:
        raise ValueError("This item has no price set yet. Please contact support.")
    with db._db() as con:
        con.execute("BEGIN IMMEDIATE")
        if kind == "product" and con.execute("SELECT 1 FROM orders WHERE company_id=? AND kind='product' AND item_key=? AND status='paid'", (company_id, item_key)).fetchone():
            raise ValueError("Your company already owns this product. Find it under My library.")
        open_o = con.execute("SELECT * FROM orders WHERE company_id=? AND kind=? AND item_key=? AND status IN ('awaiting_payment','pending')", (company_id, kind, item_key)).fetchone()
        if open_o:
            if open_o["amount_ngn"] != amount:     # price changed since it was created: reprice an unpaid order
                con.execute("UPDATE orders SET amount_ngn=?, list_price_ngn=? WHERE id=? AND status='awaiting_payment'", (amount, list_price, open_o["id"]))
            return get_order(open_o["order_ref"], con=con)
        ref = _ref()
        con.execute("INSERT INTO orders(order_ref,company_id,user_id,kind,item_key,title,list_price_ngn,amount_ngn,status,created_at) VALUES (?,?,?,?,?,?,?,?,'awaiting_payment',?)",
                    (ref, company_id, user_id, kind, item_key, title, list_price, amount, _now()))
        return get_order(ref, con=con)


def get_order(order_ref: str, con=None) -> dict | None:
    def q(c):
        r = c.execute("SELECT id,order_ref,company_id,user_id,kind,item_key,title,list_price_ngn,amount_ngn,status,transfer_reference,provider,provider_ref,"
                      "proof_at,created_at,submitted_at,verified_at,verified_by,note,(CASE WHEN proof_data IS NULL THEN 0 ELSE 1 END) AS has_proof "
                      "FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
        return dict(r) if r else None
    if con is not None:
        return q(con)
    with db._db() as c:
        return q(c)


def submit_reference(order_ref: str, company_id: int, reference: str) -> dict:
    reference = (reference or "").strip().upper()
    if not reference or len(reference) > 120:
        raise ValueError("Enter the bank transfer reference (1-120 characters).")
    try:
        with db._db() as con:
            con.execute("BEGIN IMMEDIATE")
            o = con.execute("SELECT * FROM orders WHERE order_ref=? AND company_id=?", (order_ref, company_id)).fetchone()
            if not o:
                raise LookupError("Order not found.")
            if o["status"] not in ("awaiting_payment", "pending", "failed"):
                raise ValueError("This order is already " + o["status"] + ".")
            clash = con.execute("SELECT 1 FROM payment_requests WHERE transfer_reference=?", (reference,)).fetchone()
            if clash:
                raise ValueError("That transfer reference has already been submitted.")
            con.execute("UPDATE orders SET transfer_reference=?, status='pending', submitted_at=?, note=NULL WHERE id=?", (reference, _now(), o["id"]))
            return get_order(order_ref, con=con)
    except db.IntegrityError as e:
        raise ValueError("That transfer reference has already been submitted.") from e


def attach_order_proof(order_ref: str, company_id: int, data: bytes, mime: str) -> dict | None:
    with db._db() as con:
        o = con.execute("SELECT id FROM orders WHERE order_ref=? AND company_id=? AND status IN ('awaiting_payment','pending')", (order_ref, company_id)).fetchone()
        if not o:
            return None
        con.execute("UPDATE orders SET proof_data=?, proof_mime=?, proof_at=? WHERE id=?", (db._enc_text(base64.b64encode(data).decode()), mime, _now(), o["id"]))
    return get_order(order_ref)


def order_proof(order_id: int):
    with db._db() as con:
        r = con.execute("SELECT proof_data, proof_mime FROM orders WHERE id=?", (order_id,)).fetchone()
    if not r or not r["proof_data"]:
        return None
    return base64.b64decode(db._dec_text(r["proof_data"])), r["proof_mime"] or "image/jpeg"


def _fulfil(con, o) -> None:
    """Runs inside the transaction that flips the order to paid, so it can only ever happen once."""
    if o["kind"] == "feature":
        con.execute("INSERT INTO feature_credits(company_id,feature,order_id,created_at) VALUES (?,?,?,?)", (o["company_id"], o["item_key"], o["id"], _now()))


def decide_order(order_id: int, decision: str, admin_id: int, note: str = "") -> dict:
    """Admin verifies the money really arrived (approve) or says it did not (reject). Idempotent: a second click does nothing."""
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be approve or reject.")
    with db._db() as con:
        con.execute("BEGIN IMMEDIATE")
        o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise LookupError("Order not found.")
        if o["status"] not in ("pending", "awaiting_payment"):
            raise ValueError("This order has already been " + ("approved" if o["status"] == "paid" else o["status"]) + ".")
        if decision == "approve":
            con.execute("UPDATE orders SET status='paid', verified_at=?, verified_by=?, provider=COALESCE(provider,'bank_transfer'), note=? WHERE id=?",
                        (_now(), admin_id, (note or "")[:300], order_id))
            _fulfil(con, o)
        else:
            con.execute("UPDATE orders SET status='failed', verified_at=?, verified_by=?, note=? WHERE id=?", (_now(), admin_id, (note or "")[:300], order_id))
        r = con.execute("SELECT order_ref FROM orders WHERE id=?", (order_id,)).fetchone()
    return get_order(r["order_ref"])


def mark_order_paid(order_ref: str, provider: str, provider_ref: str, amount_paid_ngn: int) -> dict:
    """Hook for the payment provider (e.g. the Paystack webhook). Call it ONLY after the provider's own verify call said 'success'.
    It re-checks the amount against the order, is safe to call twice, and never trusts anything from the browser."""
    short = False
    with db._db() as con:
        con.execute("BEGIN IMMEDIATE")
        o = con.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
        if not o:
            raise LookupError("Unknown order.")
        if o["status"] == "paid":
            return get_order(order_ref, con=con)          # duplicate webhook: already done
        if int(amount_paid_ngn) < int(o["amount_ngn"]):
            short = True                                   # recorded below; raising here would undo the record
            con.execute("UPDATE orders SET status='failed', provider=?, provider_ref=?, note=? WHERE id=?",
                        (provider, provider_ref, f"Underpaid: received {int(amount_paid_ngn)}, expected {o['amount_ngn']}", o["id"]))
        else:
            con.execute("UPDATE orders SET status='paid', provider=?, provider_ref=?, verified_at=? WHERE id=?", (provider, provider_ref, _now(), o["id"]))
            _fulfil(con, o)
    if short:
        raise ValueError("Amount paid is less than the order amount.")
    return get_order(order_ref)


def cancel_order(order_ref: str, company_id: int) -> None:
    with db._db() as con:
        con.execute("UPDATE orders SET status='cancelled' WHERE order_ref=? AND company_id=? AND status='awaiting_payment'", (order_ref, company_id))


def company_orders(company_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id,order_ref,kind,item_key,title,amount_ngn,status,transfer_reference,created_at,verified_at,note FROM orders "
                           "WHERE company_id=? ORDER BY id DESC LIMIT 200", (company_id,)).fetchall()
    return [dict(r) for r in rows]


def company_membership_payments(company_id: int) -> list[dict]:
    with db._db() as con:
        rows = con.execute("SELECT id,plan,amount_ngn,transfer_reference,status,created_at,reviewed_at FROM payment_requests WHERE company_id=? ORDER BY id DESC LIMIT 50", (company_id,)).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------------------------- report-studio access
def studio_state(company_id: int) -> dict:
    m = membership(company_id)
    with db._db() as con:
        credits = {r["feature"]: r["n"] for r in con.execute("SELECT feature, COUNT(*) AS n FROM feature_credits WHERE company_id=? AND used_result IS NULL GROUP BY feature", (company_id,)).fetchall()}
        pend = {r["item_key"] for r in con.execute("SELECT item_key FROM orders WHERE company_id=? AND kind='feature' AND status IN ('awaiting_payment','pending')", (company_id,)).fetchall()}
    out = []
    for k, f in FEATURES.items():
        out.append({**f, "included": bool(m["features"][k]), "credits": int(credits.get(k, 0)), "pending_order": k in pend,
                    "tier_name": TIERS[f["tier"]]["name"]})
    return {"tier": m["tier"], "tier_name": m["tier_name"], "features": out}


def claim_feature(company_id: int, feature: str, result_id: str) -> str:
    """Decide, on the server, whether this export is allowed: included in the tier, already paid for this very result, or a
    paid single-use credit (consumed atomically). Returns how it was allowed. Raises PermissionError when it is not."""
    if feature not in FEATURES:
        raise LookupError("Unknown report feature.")
    if membership(company_id)["features"].get(feature):
        return "membership"
    with db._db() as con:
        con.execute("BEGIN IMMEDIATE")
        again = con.execute("SELECT id FROM feature_credits WHERE company_id=? AND feature=? AND used_result=?", (company_id, feature, result_id)).fetchone()
        if again:
            return "credit-reuse"
        c = con.execute("SELECT id FROM feature_credits WHERE company_id=? AND feature=? AND used_result IS NULL ORDER BY id LIMIT 1", (company_id, feature)).fetchone()
        if not c:
            raise PermissionError("not_entitled")
        con.execute("UPDATE feature_credits SET used_result=?, used_at=? WHERE id=? AND used_result IS NULL", (result_id, _now(), c["id"]))
        return "credit"


def release_credit(company_id: int, feature: str, result_id: str) -> None:
    """If building the file fails after a credit was claimed, give the credit back."""
    with db._db() as con:
        con.execute("UPDATE feature_credits SET used_result=NULL, used_at=NULL WHERE company_id=? AND feature=? AND used_result=?", (company_id, feature, result_id))


def build_feature(feature: str, company: dict, p: dict, xlsx: bytes) -> tuple[bytes, str, str]:
    """Create the premium file from the verified facts stored with the result."""
    import pandas as pd
    import facts
    import report_pdf
    F = p.get("verified") if p else None
    if not F:
        raise ValueError("Premium reports are built from a bank-statement or ledger result. Run Statement Score or Ask & Analytics on a transactions file first.")
    fname = FEATURES[feature]["file"]
    if feature == "excel_recon":
        return xlsx, fname, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if feature == "discrepancy":
        T = facts.tables(F)
        keep = [k for k in ("Verified summary", "Duplicate references", "Unusual transactions", "Unusual days", "Data quality", "Validation") if k in T] or list(T)
        b = io.BytesIO()
        with pd.ExcelWriter(b, engine="openpyxl") as w:
            for k in keep:
                T[k].to_excel(w, sheet_name=k[:31], index=False)
        return b.getvalue(), fname, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    branding = db.get_branding(company["id"]) if feature in ("branded_pdf", "mgmt_report") else None
    if feature == "mgmt_report":
        answer = "MANAGEMENT SUMMARY\n" + "\n".join("- " + s for s in F.get("summary", [])[:12])
        pdf = report_pdf.build_analytics_pdf(company["name"], "Management report", "Management summary for " + company["name"], answer, F, None, None, branding, None)
    else:
        pdf = report_pdf.build_analytics_pdf(company["name"], p["title"], p["question"], p.get("answer"), F, p.get("table"), p.get("chart"), branding, p.get("extra_tables"))
    return pdf, fname, "application/pdf"


# ----------------------------------------------------------------------------------------------- enquiries
def add_enquiry(user: dict, company_id, name, email, company_name, phone, team_size, interest, message) -> int:
    name, email, message = (name or "").strip(), (email or "").strip(), (message or "").strip()
    if not 2 <= len(name) <= 80:
        raise ValueError("Please enter your name.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 120:
        raise ValueError("Please enter a valid email address.")
    if not 10 <= len(message) <= 2000:
        raise ValueError("Tell us a little about what you need (10-2000 characters).")
    with db._db() as con:
        since = (_utc_now() - dt.timedelta(hours=1)).strftime(db.FMT)
        if con.execute("SELECT COUNT(*) AS n FROM enquiries WHERE user_id=? AND created_at>?", (user["id"], since)).fetchone()["n"] >= 5:
            raise ValueError("You have sent several enquiries in the last hour. We will reply soon.")
        cur = con.execute("INSERT INTO enquiries(company_id,user_id,name,email,company_name,phone,team_size,interest,message,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,'new',?)",
                          (company_id, user["id"], name, email, (company_name or "")[:120], (phone or "")[:40], (team_size or "")[:30], (interest or "")[:60], message, _now()))
        return cur.lastrowid


def my_enquiries(user_id: int) -> list[dict]:
    with db._db() as con:
        return [dict(r) for r in con.execute("SELECT id,interest,message,status,created_at FROM enquiries WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()]


def list_enquiries(status: str = "all", q: str = "") -> list[dict]:
    sql, args = "SELECT * FROM enquiries", []
    cond = []
    if status != "all":
        cond.append("status=?"); args.append(status)
    if q:
        cond.append("(name LIKE ? OR email LIKE ? OR company_name LIKE ? OR message LIKE ?)"); args += [f"%{q}%"] * 4
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    with db._db() as con:
        return [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT 300", args).fetchall()]


def update_enquiry(eid: int, status: str, note: str) -> None:
    if status not in ("new", "contacted", "quoted", "won", "closed"):
        raise ValueError("Unknown status.")
    with db._db() as con:
        if not con.execute("SELECT 1 FROM enquiries WHERE id=?", (eid,)).fetchone():
            raise LookupError("Enquiry not found.")
        con.execute("UPDATE enquiries SET status=?, admin_note=?, updated_at=? WHERE id=?", (status, (note or "")[:500], _now(), eid))


# ----------------------------------------------------------------------------------------------- admin: money
def revoke_membership(company_id: int, admin_id: int, note: str) -> bool:
    """Ends paid membership now. Status 'revoked' is never given the 24-hour grace period."""
    with db._db() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM subscriptions WHERE company_id=? AND status='active' AND plan NOT IN ('free','expired')", (company_id,)).fetchone()["n"]
        if not n:
            return False
        con.execute("UPDATE subscriptions SET status='revoked', expires_at=?, note=? WHERE company_id=? AND status='active' AND plan NOT IN ('free','expired')",
                    (_now(), ("Revoked by admin: " + (note or ""))[:300], company_id))
    return True


def list_customers(q: str = "", tier: str = "all") -> list[dict]:
    with db._db() as con:
        cos = [dict(r) for r in con.execute("SELECT id, name FROM companies ORDER BY name").fetchall()]
        owners = {}
        for r in con.execute("SELECT uc.company_id, u.username, u.email FROM user_companies uc JOIN users u ON u.id=uc.user_id WHERE u.role!='admin' ORDER BY (CASE WHEN u.company_role='owner' THEN 0 ELSE 1 END), u.id").fetchall():
            owners.setdefault(r["company_id"], dict(r))
        paid = {r["company_id"]: r["s"] for r in con.execute("SELECT company_id, COALESCE(SUM(amount_ngn),0) AS s FROM payment_requests WHERE status='approved' GROUP BY company_id").fetchall()}
        spent = {r["company_id"]: r["s"] for r in con.execute("SELECT company_id, COALESCE(SUM(amount_ngn),0) AS s FROM orders WHERE status='paid' GROUP BY company_id").fetchall()}
    out = []
    ql = (q or "").strip().lower()
    for c in cos:
        o = owners.get(c["id"], {})
        if ql and ql not in c["name"].lower() and ql not in (o.get("username") or "").lower() and ql not in (o.get("email") or "").lower():
            continue
        m = membership(c["id"])
        if tier != "all" and m["tier"] != tier:
            continue
        out.append({"company_id": c["id"], "company": c["name"], "owner": o.get("username"), "email": o.get("email"), "tier": m["tier"], "tier_name": m["tier_name"],
                    "badge": m["badge"], "plan": m["plan_name"], "status": m["status"], "last_membership": m["last_membership"], "expires_at": m["expires_at"],
                    "used": m["used"], "limit": m["limit"], "paid_membership_ngn": int(paid.get(c["id"], 0)), "paid_store_ngn": int(spent.get(c["id"], 0))})
    return out


def list_orders(status: str = "all", q: str = "", kind: str = "all") -> list[dict]:
    """Marketplace + Report Studio orders AND membership payments in one monitored list."""
    out = []
    with db._db() as con:
        rows = con.execute("SELECT o.id,o.order_ref,o.company_id,o.kind,o.title,o.amount_ngn,o.status,o.transfer_reference,o.created_at,o.submitted_at,o.verified_at,o.note,"
                           "(CASE WHEN o.proof_data IS NULL THEN 0 ELSE 1 END) AS has_proof, c.name AS company, u.username FROM orders o "
                           "JOIN companies c ON c.id=o.company_id JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 400").fetchall()
        out += [dict(r) for r in rows]
        mp = con.execute("SELECT p.id,p.plan,p.amount_ngn,p.status,p.transfer_reference,p.created_at,p.reviewed_at AS verified_at,p.review_note AS note,p.company_id,c.name AS company,u.username "
                         "FROM payment_requests p JOIN companies c ON c.id=p.company_id JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 400").fetchall()
        for r in mp:
            d = dict(r)
            d.update({"order_ref": f"PLAN-{d['id']}", "kind": "membership", "title": db.PLANS.get(d["plan"], {}).get("name", d["plan"]) + " membership",
                      "status": {"approved": "paid", "rejected": "failed"}.get(d["status"], d["status"]), "has_proof": 0, "submitted_at": d["created_at"]})
            d.pop("plan", None)
            out.append(d)
    ql = (q or "").lower()
    res = [r for r in out if (status == "all" or r["status"] == status) and (kind == "all" or r["kind"] == kind)
           and (not ql or ql in (r.get("company") or "").lower() or ql in (r.get("transfer_reference") or "").lower() or ql in (r.get("order_ref") or "").lower()
                or ql in (r.get("username") or "").lower() or ql in (r.get("title") or "").lower())]
    res.sort(key=lambda r: r["created_at"], reverse=True)
    return res[:300]


def overview() -> dict:
    with db._db() as con:
        by_status = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()}
        mstat = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM payment_requests GROUP BY status").fetchall()}
        months = {}
        for r in con.execute("SELECT SUBSTR(COALESCE(reviewed_at, created_at),1,7) AS m, SUM(amount_ngn) AS s FROM payment_requests WHERE status='approved' GROUP BY 1").fetchall():
            months.setdefault(r["m"], {"membership": 0, "marketplace": 0, "studio": 0})["membership"] += int(r["s"] or 0)
        for r in con.execute("SELECT SUBSTR(COALESCE(verified_at, created_at),1,7) AS m, kind, SUM(amount_ngn) AS s FROM orders WHERE status='paid' GROUP BY 1, 2").fetchall():
            months.setdefault(r["m"], {"membership": 0, "marketplace": 0, "studio": 0})["marketplace" if r["kind"] == "product" else "studio"] += int(r["s"] or 0)
        enq = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM enquiries GROUP BY status").fetchall()}
    custs = list_customers()
    tiers = {k: 0 for k in TIERS}
    for c in custs:
        tiers[c["tier"]] += 1
    this_month = _utc_now().strftime("%Y-%m")
    tot = {"membership": 0, "marketplace": 0, "studio": 0}
    for v in months.values():
        for k in tot:
            tot[k] += v[k]
    series = [{"month": m, **months[m], "total": sum(months[m].values())} for m in sorted(months)[-6:]]
    return {"tiers": tiers, "paying": len(custs) - tiers["regular"], "free": tiers["regular"], "companies": len(custs),
            "revenue_total": sum(tot.values()), "revenue_by_stream": tot, "revenue_this_month": sum(months.get(this_month, {}).values()), "series": series,
            "orders": by_status, "membership_payments": {"pending": mstat.get("pending", 0), "paid": mstat.get("approved", 0), "failed": mstat.get("rejected", 0)},
            "pending_total": by_status.get("pending", 0) + mstat.get("pending", 0), "failed_total": by_status.get("failed", 0) + mstat.get("rejected", 0),
            "enquiries": enq, "new_enquiries": enq.get("new", 0)}
