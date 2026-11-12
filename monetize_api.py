"""HTTP routes for the monetisation system. Registered at the end of web.py with register(app, web_module)."""
from __future__ import annotations

import base64

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

import db
import monetize as M


class OrderIn(BaseModel):
    company_id: int
    kind: str
    item_key: str


class RefIn(BaseModel):
    company_id: int
    transfer_reference: str


class EnquiryIn(BaseModel):
    company_id: int | None = None
    name: str
    email: str
    company_name: str = ""
    phone: str = ""
    team_size: str = ""
    interest: str = ""
    message: str


class DecisionIn(BaseModel):
    decision: str
    note: str = ""


class ProductIn(BaseModel):
    name: str = ""
    category: str = "Reconciliation"
    description: str = ""
    details: str = ""
    price_ngn: int = 0
    active: bool = True
    sort: int = 100
    file_name: str | None = None
    file_b64: str | None = None
    file_mime: str | None = None


class EnquiryUpdateIn(BaseModel):
    status: str
    note: str = ""


class RevokeIn(BaseModel):
    company_id: int
    note: str = ""


def register(app, W) -> None:
    cu = Depends(W.current_user)
    XLSX_ = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def own(user, company_id):
        W.need_company(user, company_id)

    def buyer(user, company_id):
        own(user, company_id)
        W.need_role(user, "admin")      # company owner or admin may spend the company's money
        if user.get("role") == "admin":
            raise HTTPException(400, "Site admin accounts do not buy. Use a customer account to test.")

    # ------------------------------------------------------------- customer: membership
    @app.get("/api/membership")
    def membership(company_id: int, user: dict = cu):
        own(user, company_id)
        m = M.membership(company_id)
        m["catalogue"] = M.catalogue()
        m["payments"] = M.company_membership_payments(company_id)
        m["orders"] = M.company_orders(company_id)
        return m

    # ------------------------------------------------------------- marketplace
    @app.get("/api/market/products")
    def products(company_id: int, user: dict = cu):
        own(user, company_id)
        return {"products": M.list_products(company_id), "categories": M.CATEGORIES, "discount": M.membership(company_id)["market_discount"]}

    def checkout_info(o):
        from receipts import bank_accounts
        return {"order": o, "banks": bank_accounts()}

    @app.post("/api/market/orders")
    def new_order(body: OrderIn, user: dict = cu):
        buyer(user, body.company_id)
        try:
            o = M.create_order(body.company_id, user["id"], body.kind, body.item_key)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "order_created", body.company_id, f"order={o['order_ref']}; {o['kind']}={o['item_key']}; amount={o['amount_ngn']}")
        return checkout_info(o)

    @app.post("/api/market/orders/{order_ref}/reference")
    def order_reference(order_ref: str, body: RefIn, user: dict = cu):
        buyer(user, body.company_id)
        try:
            o = M.submit_reference(order_ref, body.company_id, body.transfer_reference)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "order_payment_submitted", body.company_id, f"order={order_ref}; ref={o['transfer_reference']}")
        company = db.get_company(body.company_id)
        W.notify.alert_payment_admins(
            f"New {o['kind']} order {o['order_ref']}\nCompany: {company['name'] if company else body.company_id}\nUser: {user['username']}\n"
            f"Item: {o['title']} · ₦{o['amount_ngn']:,}\nReference: {o['transfer_reference']}\nCheck your bank, then approve it under Revenue > Transactions.")
        return checkout_info(o)

    @app.post("/api/market/orders/{order_ref}/proof")
    def order_proof(order_ref: str, company_id: int = Form(...), file: UploadFile = File(...), user: dict = cu):
        buyer(user, company_id)
        data = file.file.read(W.MAX_PROOF_BYTES + 1)
        if not data or len(data) > W.MAX_PROOF_BYTES:
            raise HTTPException(400, "Upload a screenshot under 5 MB.")
        kind = W._image_kind(data)
        if not kind:
            raise HTTPException(400, "Please upload a screenshot as a JPG, PNG or WEBP image.")
        o = M.attach_order_proof(order_ref, company_id, data, kind[0])
        if not o:
            raise HTTPException(400, "Start the order first, then attach the screenshot.")
        company = db.get_company(company_id)
        W.notify.alert_admins_photo(data, f"order_{o['order_ref']}.{kind[1]}",
                                    f"Payment screenshot for order {o['order_ref']}\nCompany: {company['name'] if company else company_id}\nItem: {o['title']} · ₦{o['amount_ngn']:,}")
        db.audit(user, "order_proof", company_id, f"order={order_ref}")
        return {"ok": True}

    @app.post("/api/market/orders/{order_ref}/cancel")
    def order_cancel(order_ref: str, company_id: int, user: dict = cu):
        buyer(user, company_id)
        M.cancel_order(order_ref, company_id)
        return {"ok": True}

    @app.get("/api/market/orders")
    def orders(company_id: int, user: dict = cu):
        own(user, company_id)
        return M.company_orders(company_id)

    @app.get("/api/market/download/{product_id}")
    def download_product(product_id: int, company_id: int, user: dict = cu):
        own(user, company_id)
        try:
            data, mime, name = M.product_file(product_id, company_id, is_admin=(user.get("role") == "admin"))
        except LookupError as e:
            raise HTTPException(404, str(e))
        except PermissionError as e:
            raise HTTPException(402, str(e))
        db.audit(user, "product_download", company_id, f"product={product_id}")
        return Response(content=data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                                  "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------- report studio
    @app.get("/api/studio")
    def studio(company_id: int, user: dict = cu):
        own(user, company_id)
        return M.studio_state(company_id)

    @app.get("/api/studio/export/{result_id}/{feature}")
    def studio_export(result_id: str, feature: str, user: dict = cu):
        item = W.RESULTS.get(result_id)
        if item is None or item["user"] != user["id"]:     # same ownership rule as the standard downloads
            raise HTTPException(404, "Result expired - run it again.")
        cid = item["company"]
        W.need_company(user, cid)
        if feature not in M.FEATURES:
            raise HTTPException(404, "Unknown report feature.")
        p = item.get("pdf")
        if not (p and p.get("verified")):
            raise HTTPException(400, "Premium reports are built from a bank-statement or ledger result. Run Statement Score or Ask & Analytics on a transactions file first.")
        if user.get("role") != "admin":
            try:
                how = M.claim_feature(cid, feature, result_id)
            except PermissionError:
                f = M.FEATURES[feature]
                raise HTTPException(402, {"code": "not_entitled", "feature": feature, "name": f["name"], "price_ngn": f["price_ngn"],
                                          "tier": M.TIERS[f["tier"]]["name"]})
        else:
            how = "admin"
        company = db.get_company(cid)
        try:
            data, fname, mime = M.build_feature(feature, company, p, item["data"])
        except ValueError as e:
            if how == "credit":
                M.release_credit(cid, feature, result_id)
            raise HTTPException(400, str(e))
        except Exception:  # noqa: BLE001
            if how == "credit":
                M.release_credit(cid, feature, result_id)
            raise HTTPException(500, "We could not build that report. You were not charged: your credit is still available.")
        db.audit(user, "studio_export", cid, f"{feature}; via={how}")
        return StreamingResponse(iter([data]), media_type=mime, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # ------------------------------------------------------------- business enquiries
    @app.post("/api/enquiries")
    def enquire(body: EnquiryIn, user: dict = cu):
        if body.company_id:
            own(user, body.company_id)
        try:
            eid = M.add_enquiry(user, body.company_id, body.name, body.email, body.company_name, body.phone, body.team_size, body.interest, body.message)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "business_enquiry", body.company_id, f"enquiry={eid}")
        W.notify.alert_admins(f"New business enquiry #{eid}\nFrom: {body.name} <{body.email}>\nCompany: {body.company_name or '-'} · Team: {body.team_size or '-'}\n"
                              f"Interest: {body.interest or '-'}\n{body.message[:300]}")
        return {"id": eid}

    @app.get("/api/enquiries/mine")
    def my_enquiries(user: dict = cu):
        return M.my_enquiries(user["id"])

    # ------------------------------------------------------------- admin
    def adm(user):
        W.need(user, "admin")

    @app.get("/api/admin/money/overview")
    def a_overview(user: dict = cu):
        adm(user)
        return M.overview()

    @app.get("/api/admin/money/customers")
    def a_customers(q: str = "", tier: str = "all", user: dict = cu):
        adm(user)
        return M.list_customers(q, tier)

    @app.get("/api/admin/money/orders")
    def a_orders(status: str = "all", q: str = "", kind: str = "all", user: dict = cu):
        adm(user)
        return M.list_orders(status, q, kind)

    @app.get("/api/admin/money/orders/{order_id}/proof")
    def a_order_proof(order_id: int, user: dict = cu):
        adm(user)
        f = M.order_proof(order_id)
        if not f:
            raise HTTPException(404, "No screenshot was attached.")
        return Response(content=f[0], media_type=f[1], headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @app.post("/api/admin/money/orders/{order_id}/decision")
    def a_decide(order_id: int, body: DecisionIn, user: dict = cu):
        adm(user)
        try:
            o = M.decide_order(order_id, body.decision, user["id"], body.note)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, f"order_{o['status']}", o["company_id"], f"order={o['order_ref']}; amount={o['amount_ngn']}; ref={o.get('transfer_reference')}")
        if o["status"] == "paid":
            W.extras.tell_company(o["company_id"], "Payment confirmed",
                                  f"We have confirmed your payment for {o['title']}." + (" It is ready under Marketplace > My library." if o["kind"] == "product" else " It is ready in Report Studio."), "success")
        else:
            W.extras.tell_company(o["company_id"], "Payment not confirmed",
                                  f"We could not confirm your payment for {o['title']}. Check the transfer reference and submit it again, or contact support.", "warning")
        return o

    @app.post("/api/admin/money/revoke")
    def a_revoke(body: RevokeIn, user: dict = cu):
        adm(user)
        if not db.get_company(body.company_id):
            raise HTTPException(404, "Unknown company")
        if not (body.note or "").strip():
            raise HTTPException(400, "Write a reason. It is saved in the audit log.")
        if not M.revoke_membership(body.company_id, user["id"], body.note):
            raise HTTPException(400, "This company has no active paid membership to revoke.")
        db.audit(user, "membership_revoked", body.company_id, f"reason={body.note[:200]}")
        W.extras.tell_company(body.company_id, "Membership ended", "Your paid membership has ended. Contact support if you think this is a mistake.", "warning")
        return M.membership(body.company_id)

    @app.get("/api/admin/money/products")
    def a_products(user: dict = cu):
        adm(user)
        return M.list_products(None, include_inactive=True)

    def _file(b: ProductIn):
        if not b.file_b64:
            return None
        try:
            return base64.b64decode(b.file_b64, validate=False)
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "The file could not be read.")

    @app.post("/api/admin/money/products")
    def a_add_product(body: ProductIn, user: dict = cu):
        adm(user)
        try:
            r = M.add_product(body.name, body.category, body.description, body.details, body.price_ngn, body.file_name, body.file_mime or "application/octet-stream", _file(body), body.sort)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "product_added", None, f"product={r['id']}; name={body.name[:60]}; price={body.price_ngn}")
        return r

    @app.put("/api/admin/money/products/{pid}")
    def a_edit_product(pid: int, body: ProductIn, user: dict = cu):
        adm(user)
        try:
            M.update_product(pid, body.model_dump(exclude={"file_name", "file_b64", "file_mime"}), _file(body), body.file_name, body.file_mime)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "product_edited", None, f"product={pid}; price={body.price_ngn}; active={body.active}")
        return {"ok": True}

    @app.delete("/api/admin/money/products/{pid}")
    def a_del_product(pid: int, user: dict = cu):
        adm(user)
        try:
            r = M.remove_product(pid)
        except LookupError as e:
            raise HTTPException(404, str(e))
        db.audit(user, "product_" + r, None, f"product={pid}")
        return {"result": r}

    @app.get("/api/admin/money/enquiries")
    def a_enquiries(status: str = "all", q: str = "", user: dict = cu):
        adm(user)
        return M.list_enquiries(status, q)

    @app.post("/api/admin/money/enquiries/{eid}")
    def a_enquiry(eid: int, body: EnquiryUpdateIn, user: dict = cu):
        adm(user)
        try:
            M.update_enquiry(eid, body.status, body.note)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.audit(user, "enquiry_updated", None, f"enquiry={eid}; status={body.status}")
        return {"ok": True}
