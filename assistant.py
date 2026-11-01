"""
assistant.py - "Ask about this": plain-language answers about a result, the discrepancy cases, or how to use the website.

People can type in any wording (spelling mistakes, short forms, Pidgin). The AI only explains facts that are in the context it is
given, and any number it writes that is not in that context is flagged. If the AI is unavailable, the built-in help text or the
computed summary is shown instead, so people are never left with nothing.
"""
from __future__ import annotations

import re

import core

MAX_CONTEXT = 12000

SYSTEM = """You are a patient, friendly finance and website teacher for business owners and staff who may not be technical.
The person can write in any wording: casual English, short forms, spelling mistakes, Nigerian Pidgin. Work out what they mean and answer it.
Rules:
- Use ONLY the facts and numbers in CONTEXT. Never invent figures. If the answer is not in the context, say what is missing and which
  screen of the website would show it.
- Explain step by step in simple words. Define any finance word the first time you use it. Use a short example when it helps.
- Write money with the naira sign and thousands separators. Keep it under 250 words unless they ask for more detail.
- Never ask them to rephrase. If the question is vague, answer the most likely meaning and add one line saying what you assumed.
- You explain; you do not approve loans or give legal, tax or investment advice."""

HELP_SECTIONS = [
    ("Upload a report", "upload file csv excel pdf photo scan word json xml add report import",
     "Open Upload Report in the side menu and choose a file. CSV, Excel, PDF, Word, JSON, XML, HTML, text, OpenDocument and photos or scans are accepted (photos and scans are read with OCR, so check the numbers). Uploading does not use one of your plan checks."),
    ("Ask a question about a report", "ask question analyze analytics total average count top compare explain",
     "Open Ask & Analytics, pick a report (or All reports combined), type your question in your own words and press Analyze. Say what to work out (total, average, how many, highest, lowest), how to split it (by branch, month, bank), any filter (March, failed, above 50,000) and how many to show (top 10). Tap How to ask for examples. Tick Refine the previous result to change the last answer. Each question uses one check from your plan."),
    ("Statement Score", "statement score loan eligibility bank statement credit income expenses months credibility",
     "Open Statement Score, pick a bank statement, choose months if you want, and press Check statement. You get monthly income, spending and what is left, a loan-readiness score out of 100, a balance check that shows if the statement may have been edited, unusual patterns, spending categories and a three-month forecast. You can combine several accounts, download Excel or PDF, and share a private expiring link with a lender."),
    ("Batch Check for lenders", "batch lender rank applicants many statements",
     "Batch Check (analyst accounts and above) scores up to 25 applicants' statements at once and ranks them best to worst, with a suggested next step for each, and Excel and PDF downloads. Files are not saved. Each statement uses one plan check."),
    ("Reconciliation", "reconcile reconciliation compare two reports match mismatch missing atm pos bank ledger",
     "Open Reconciliation, choose Report A and Report B and press Reconcile. Rows are matched on a reference column and amounts are compared. You see Matched, Amount mismatch, Only in A and Only in B, with likely reasons. In the note box you can write in your own words, for example: match Reference with Ref No, or ignore differences under 10 naira. If there is no note the site works out the columns itself and tells you how it matched them. Mismatches become discrepancy cases automatically."),
    ("Discrepancy cases", "discrepancy cases open in review resolved accepted track assign",
     "Open Discrepancies and press Load cases. Each difference found by a reconciliation becomes a case with a status: Open, In review, Resolved or Accepted. You can assign cases, change status and keep notes so nothing is forgotten."),
    ("Forecast", "forecast cash atm predict next days statement future",
     "ATM cash forecast is under Ask & Analytics: choose how many days ahead and optionally cash per ATM. Statement Score also shows a three-month forecast of income, spending and balance. Forecasts are planning aids, not promises."),
    ("Plans and payments", "plan billing payment pay transfer subscription limit checks upgrade invoice receipt coupon",
     "Open Plan & Billing. The Free Trial has 5 checks a month and lasts 2 days. Paid plans give more checks and team members. Pay by bank transfer to the account shown, enter the transfer reference, attach a screenshot and wait for the owner to confirm. Download a pro-forma invoice before paying and a receipt after. Coupons and referral bonuses add days or discounts. Questions, comparisons, statement checks and reconciliations use checks; uploads do not."),
    ("Team and access", "team user invite member role viewer analyst admin access",
     "Roles: Viewer can look, Analyst can upload and analyze, Admin (the owner) can do everything. Team size depends on the plan. The owner manages users under Team & Access."),
    ("Security and privacy", "security password two factor 2fa devices login delete account data export privacy",
     "Turn on two-factor login with the 2FA button. In Settings, Security & privacy shows devices signed in, lets you sign out others, download your data and ask for account deletion. New-device sign-ins create an alert. Data is stored encrypted and each company only sees its own data."),
    ("Share links", "share link lender accountant private expire",
     "After a Statement Score check, press Share with a lender and choose 1, 7 or 30 days. Anyone with the link can see that one result (no files, no login, no transaction descriptions). Switch it off any time under My shared links."),
    ("Notifications and support", "notification bell announcement support help ticket message email",
     "The bell shows announcements and personal messages. Open Support in the side menu to write to the team and read replies. You can also email reconciliationsolutionssupport@gmail.com."),
    ("API access", "api key developer lender integration automatic",
     "Analysts can create API keys in Settings, API access. Send a statement to /api/v1/statement-score with the X-API-Key header and get the score as JSON. Each call uses one plan check."),
    ("Install as an app", "install app phone home screen pwa offline",
     "Press Install app at the top of the page (on iPhone use Safari's Share button then Add to Home Screen). Nothing private is stored on the device."),
]
HELP_TEXT = "\n\n".join(f"## {t}\n{x}" for t, _, x in HELP_SECTIONS)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def help_fallback(question: str) -> str:
    """Used only when the AI is unavailable: the most relevant help sections by shared words."""
    q = _words(question)
    scored = sorted(((len(q & _words(t + " " + kw + " " + x)), t, x) for t, kw, x in HELP_SECTIONS), reverse=True)
    best = [s for s in scored if s[0] > 0][:2] or scored[:1]
    return "\n\n".join(f"{t}\n{x}" for _, t, x in best)


async def answer(kind: str, question: str, context: str) -> dict:
    """Returns {"answer": str, "source": "ai" | "built-in"}."""
    context = (context or "")[:MAX_CONTEXT]
    try:
        resp = await core._ask(
            model=core.MODEL, max_tokens=1200, system=SYSTEM,
            messages=[{"role": "user", "content": f"CONTEXT ({kind}):\n{context}\n\nQUESTION: {question}"}],
        )
        text = core._text_of(resp).strip()
        if not text:
            raise ValueError("empty answer")
        bad = core.unverified_figures(text, context)
        if bad:
            text += "\n\n⚠️ Please double-check these figures, I could not find them in your data: " + ", ".join(bad) + "."
        return {"answer": text, "source": "ai"}
    except core.LimitReached:
        raise
    except Exception:  # noqa: BLE001  (AI down / no key: never leave the person with nothing)
        if kind == "help":
            return {"answer": "The smart helper is busy right now, so here is what the guide says about it:\n\n" + help_fallback(question), "source": "built-in"}
        return {"answer": "The smart helper is busy right now. Here is the summary the website already worked out:\n\n" + context[:2500], "source": "built-in"}
