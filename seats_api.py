"""HTTP routes for role unlocks, join-company requests and coupons on orders. Registered at the end of web.py."""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

import db
import monetize as M
import seats


class JoinIn(BaseModel):
    join_code: str
    username: str
    password: str
    email: str | None = None
    website: str | None = None          # hidden field: robots fill it in


class JoinDecisionIn(BaseModel):
    decision: str


class UnlockIn(BaseModel):
    role: str


class AskIn(BaseModel):
    company_id: int
    role: str


class GrantIn(BaseModel):
    company_id: int
    member_id: int
    role: str
    note: str = ""


class CouponIn(BaseModel):
    company_id: int
    code: str


JOIN_FAILS: dict = {}


def register(app, W) -> None:
    cu = Depends(W.current_user)

    # ------------------------------------------------------------- what each level can do (public: shown on the landing page)
    @app.get("/api/roles/catalogue")
    def roles_catalogue():
        return {"roles": seats.ROLE_CATALOGUE, "prices": seats.SEAT_PRICES, "welcome_percent": seats.WELCOME_PERCENT}

    # ------------------------------------------------------------- joining a company (public form, owner approves)
    @app.post("/api/register/join")
    def register_join(body: JoinIn, request: Request):
        ip = W.client_ip(request) or "?"
        now = time.time()
        if body.website:
            return {"message": "Request sent. The company owner must approve it before you can sign in.", "status": "pending"}
        fails = [t for t in JOIN_FAILS.get(ip, []) if now - t < 3600]
        if len(fails) >= 8:
            raise HTTPException(429, "Too many attempts. Try again in one hour.")
        if W._email_verify_required() and not (body.email and db.is_email_verified(body.email.strip().lower())):
            raise HTTPException(400, "Verify your email address first: enter it, tap Send code, then enter the code we email you.")
        try:
            r = seats.submit_join_request(body.join_code, body.username, body.password, body.email)
        except ValueError as e:
            JOIN_FAILS[ip] = fails + [now]
            raise HTTPException(400, str(e))
        JOIN_FAILS[ip] = fails
        return {"message": f"Request sent to {r['company_name']}. The company owner must approve it before you can sign in. You will get an email when they do.",
                "status": "pending"}

    # ------------------------------------------------------------- owner: join code and requests
    def owner(user, company_id):
        W.need_company(user, company_id)
        W.need_role(user, "owner")

    @app.get("/api/companies/{company_id}/join")
    def join_info(company_id: int, user: dict = cu):
        owner(user, company_id)
        return {"code": seats.get_join_code(company_id), "requests": seats.list_join_requests(company_id, "pending")}

    @app.post("/api/companies/{company_id}/join/code")
    def join_new_code(company_id: int, user: dict = cu):
        owner(user, company_id)
        code = seats.regenerate_join_code(company_id)
        db.audit(user, "join_code_regenerated", company_id, "")
        return {"code": code}

    @app.post("/api/companies/{company_id}/join/{request_id}")
    def join_decide(company_id: int, request_id: int, body: JoinDecisionIn, user: dict = cu):
        owner(user, company_id)
        try:
            r = seats.decide_join_request(company_id, request_id, body.decision, user)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "join_" + r["status"], company_id, r["username"])
        return r

    # ------------------------------------------------------------- unlocks (paid through the normal orders)
    @app.post("/api/companies/{company_id}/team/{member_id}/unlock")
    def team_unlock(company_id: int, member_id: int, body: UnlockIn, user: dict = cu):
        owner(user, company_id)
        if user.get("role") == "admin":
            raise HTTPException(400, "Site admin accounts do not buy. Use Revenue > Team access to grant it.")
        try:
            o = M.create_order(company_id, user["id"], "seat", f"{body.role}:{member_id}")
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "order_created", company_id, f"order={o['order_ref']}; seat={o['item_key']}; amount={o['amount_ngn']}")
        from receipts import bank_accounts
        return {"order": o, "banks": bank_accounts()}

    @app.post("/api/team/ask-upgrade")
    def team_ask(body: AskIn, user: dict = cu):
        W.need_company(user, body.company_id)
        try:
            seats.request_upgrade(body.company_id, user, body.role)
        except (ValueError, LookupError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    # ------------------------------------------------------------- coupon on an order (every section that uses orders)
    @app.post("/api/market/orders/{order_ref}/coupon")
    def order_coupon(order_ref: str, body: CouponIn, user: dict = cu):
        W.need_company(user, body.company_id)
        W.need_spender(user)
        try:
            o = M.apply_order_coupon(order_ref, body.company_id, user["id"], body.code)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "order_coupon", body.company_id, f"order={order_ref}; coupon={o['coupon_code']}; off={o['discount_ngn']}")
        from receipts import bank_accounts
        return {"order": o, "banks": bank_accounts()}

    # ------------------------------------------------------------- site owner: grant an unlock without payment
    @app.post("/api/admin/seats/grant")
    def seat_grant(body: GrantIn, user: dict = cu):
        W.need(user, "admin")
        if not (body.note or "").strip():
            raise HTTPException(400, "Write a reason. It is saved in the audit log.")
        try:
            seats.seat_target(body.company_id, body.member_id, body.role)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        with db._db() as con:
            seats.apply_seat_unlock(con, body.company_id, body.member_id, body.role)
        db.audit(user, "seat_granted", body.company_id, f"member={body.member_id}; {body.role}; reason={body.note[:200]}")
        W.extras.tell_user(body.member_id, f"{body.role.title()} access unlocked", f"{body.role.title()} access was unlocked for your account.", "success")
        return {"ok": True}

    @app.post("/api/admin/coupons/welcome-test")
    def welcome_resend(company_id: int, user: dict = cu):
        """Issue the welcome coupon to a company that does not have one yet (for companies approved before this build)."""
        W.need(user, "admin")
        co = db.get_company(company_id)
        if not co:
            raise HTTPException(404, "Unknown company")
        owner_row = next((m for m in db.list_team(company_id) if m["company_role"] == "owner"), None)
        c = seats.issue_welcome_coupon(company_id, owner_row["username"] if owner_row else "there", co["name"], owner_row["email"] if owner_row else None)
        if not c:
            raise HTTPException(400, "This company already has a welcome coupon.")
        db.audit(user, "welcome_coupon", company_id, c["code"])
        return c
