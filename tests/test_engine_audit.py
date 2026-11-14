"""Regression tests for the analysis-engine audit (build 40).

One test group per confirmed issue. The engine itself contains no values from the stress file: the expectations below are in the TESTS only,
and a generated-data test checks the same rules on datasets the engine has never seen. The AI is replaced by a stub, so nothing here
needs the network.   Run: python -m unittest discover -s tests -v
"""
import ast, asyncio, io, os, random, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for _m in ("httpx", "dotenv"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = mock.MagicMock()
import numpy as np
import pandas as pd
import core, facts, fullread, qa, report_pdf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")


def load(name: str):
    path = os.path.join(HERE, "fixtures", name)
    return core.load_report(open(path, "rb").read(), name)[0]


def load_text(csv_text: str, name="inline.csv"):
    return core.load_report(csv_text.encode(), name)[0]


class FakeAI:
    plan = None
    plan_error = None
    summary = "ANSWER: stub"


async def _fake_plan(question, df, prev_plan, hint=None, feedback=None):
    if FakeAI.plan_error:
        raise RuntimeError(FakeAI.plan_error)
    return dict(FakeAI.plan)


async def _fake_summarize(*a, **k):
    return FakeAI.summary


CREDITS = {"filters": [{"column": "Credit/Debit", "op": "==", "value": "Credit"}], "chart": "none", "title": "Total credits",
           "metrics": [{"column": "Amount (always positive)", "agg": "sum", "alias": "Total credits"}, {"column": "*", "agg": "count", "alias": "Credit rows"}]}
DEBITS = {**CREDITS, "filters": [{"column": "Credit/Debit", "op": "==", "value": "Debit"}], "title": "Total debits"}


def web_helpers():
    """The AI-down helper from web.py, loaded without importing the web framework."""
    tree = ast.parse(open(os.path.join(ROOT, "web.py"), encoding="utf-8").read())
    ns = {"core": core, "facts": facts}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "AI_DOWN_WORDS" for t in node.targets)) or (isinstance(node, ast.FunctionDef) and node.name == "_no_ai_answer"):
            exec(compile(ast.Module([node], []), "web.py", "exec"), ns)
    return ns


def pdf_text(F) -> str:
    from pypdf import PdfReader
    data = report_pdf.build_analytics_pdf("Test Co", "Audit", "Full analysis", "ANSWER: x", F, None, None, {}, None)
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (core.make_plan, core.summarize, getattr(core, "_facts_installed", False), core.unverified_figures)
        cls.df = load("stress_test_v2_reconstructed.csv")
        cls.F = facts.build(cls.df)
        cls.clean = load("clean_dataset.csv")
        cls.CF = facts.build(cls.clean)

    @classmethod
    def tearDownClass(cls):
        core.make_plan, core.summarize, core._facts_installed, core.unverified_figures = cls._saved

    def ask(self, df, question, plan=None, plan_error=None, summary="ANSWER: stub", F=None):
        """Run the real core.analyze with a stubbed AI. Returns (result, ai_words_flag)."""
        core.make_plan, core.summarize, core._facts_installed = _fake_plan, _fake_summarize, False
        facts.install_grounding()
        FakeAI.plan, FakeAI.plan_error, FakeAI.summary = plan, plan_error, summary

        async def go():
            facts.CURRENT.set(F)
            facts.AI_WORDED.set(None)
            out = await core.analyze(df, question, None, True)
            return out, facts.AI_WORDED.get()
        return asyncio.run(go())


class MissingFields(Base):
    def test_each_field_counted_separately_with_row_and_details(self):
        m = self.F["missing"]
        self.assertEqual({k: v["count"] for k, v in m.items()}, {"Reference": 1, "Date": 1, "Type": 0, "Amount": 0, "Description": 1, "Counterparty": 1, "Status": 0})
        ref = m["Reference"]["rows"][0]
        self.assertEqual((ref["Row"], ref["Type"], ref["Amount"]), (31, "Credit", 47000.0))
        desc = m["Description"]["rows"][0]
        self.assertEqual((desc["Reference"], desc["Amount"], desc["Type"]), ("ST-2028", 78000.0, "Credit"))
        cp = m["Counterparty"]["rows"][0]
        self.assertEqual((cp["Reference"], cp["Amount"], cp["Type"]), ("ST-2029", 26000.0, "Debit"))
        self.assertEqual(m["Date"]["rows"][0]["Reference"], "ST-2024")

    def test_whitespace_only_and_placeholders_are_missing(self):
        df = load_text("Date,Reference,Description,Counterparty,Type,Amount,Status\n2026-10-01,A-100,   ,Acme,Credit,100,Completed\n"
                       "2026-10-02,   ,Pay,   ,Debit,50,Completed\n2026-10-03,A-102,Pay,Acme,Credit,70,N/A\n")
        F = facts.build(df)
        self.assertEqual({k: v["count"] for k, v in F["missing"].items() if v["count"]}, {"Reference": 1, "Description": 1, "Counterparty": 1, "Status": 1})

    def test_never_says_everything_has_a_reference_when_blank_values_exist(self):
        issues = facts.check_narrative("Every transaction has a reference and a description.", self.F, True)
        self.assertTrue(issues)
        self.assertIn("Missing values (rows): reference 1", " ".join(self.F["summary"]))

    def test_missing_is_not_duplicate(self):
        refs_dup = {g["reference"] for g in self.F["duplicates"]["groups"]}
        self.assertEqual(refs_dup, {"ST-2022"})
        self.assertNotIn("", refs_dup)


class InvalidValues(Base):
    def test_invalid_rows_get_explicit_treatment_and_original_values_are_kept(self):
        ex = {r["Reference"]: r for r in self.F["excluded"]}
        self.assertEqual(set(ex), {"ST-2025", "ST-2026", "ST-2027"})
        self.assertIn("Unsupported transaction type 'TRANSFER'", ex["ST-2025"]["Reason"])
        self.assertEqual(ex["ST-2025"]["Type"], "TRANSFER")
        self.assertEqual(ex["ST-2026"]["Amount"], -15000.0)                      # the sign survives for audit
        self.assertIn("Negative amount", ex["ST-2026"]["Reason"])
        self.assertIn("Zero amount", ex["ST-2027"]["Reason"])
        self.assertTrue(all(r["Reason"] for r in self.F["excluded"]))

    def test_invalid_date_is_counted_but_flagged_and_not_in_daily(self):
        warned = {r["Reference"]: r["Warnings"] for r in self.F["warned"]}
        self.assertIn("date", warned["ST-2024"].lower())
        self.assertEqual(sum(x["transactions"] for x in self.F["daily"]), self.F["rows_analysed"] - 1)

    def test_negative_amount_is_never_an_ordinary_credit(self):
        t = self.F["totals"]
        expected_credits = float(pd.to_numeric(self.df["Amount"]).where(self.df["Type"].str.lower().eq("credit") & (pd.to_numeric(self.df["Amount"]) > 0)).sum())
        self.assertAlmostEqual(t["money_in"], expected_credits, places=2)


class StatusAnalysis(Base):
    def test_pending_and_reversed_listed_with_amounts(self):
        st = {r["status"]: r for r in self.F["status"]["rows"]}
        self.assertEqual((st["Pending"]["count"], st["Pending"]["credits"]), (2, 177000.0))
        self.assertEqual((st["Reversed"]["count"], st["Reversed"]["credits"]), (2, 163000.0))
        self.assertEqual({x["Reference"] for x in st["Pending"]["transactions"]}, {"ST-2008", "ST-2032"})
        self.assertEqual({x["Reference"] for x in st["Reversed"]["transactions"]}, {"ST-2011", "ST-2033"})
        self.assertTrue(all(x["Counterparty"] for x in st["Pending"]["transactions"]))

    def test_treatment_is_stated_and_status_is_not_direction(self):
        t = self.F["totals"]
        self.assertAlmostEqual(t["money_in"] - t["settled_money_in"], 177000 + 163000)
        self.assertEqual(t["settled_money_out"], t["money_out"])
        txt = facts.analysis_text(self.F)
        self.assertIn("gross totals", txt.lower())
        self.assertIn("Pending", txt)
        self.assertIn("Reversed", txt)

    def test_other_statuses_are_reported_and_failed_is_called_out(self):
        F = facts.build(load("messy_variants.csv"))
        names = {r["status"] for r in F["status"]["rows"]}
        self.assertTrue({"Completed", "Pending", "Failed", "Cancelled"} <= names)       # "Successful" is read as Completed
        notes = [r.get("note") for r in F["status"]["rows"] if r["status"] in ("Failed", "Cancelled")]
        self.assertTrue(all(notes))


class Duplicates(Base):
    def test_exact_duplicate_reported_not_removed(self):
        g = self.F["duplicates"]["groups"][0]
        self.assertEqual((g["reference"], g["count"], g["row_numbers"], g["exact"]), ("ST-2022", 2, [23, 35], True))
        self.assertEqual(self.F["totals"]["debit_count"], 7)                              # both copies are still counted
        self.assertIn("NOT removed", " ".join(q["detail"] for q in self.F["quality"]))

    def test_same_reference_different_amount_date_direction_is_distinguished(self):
        F = facts.build(load("messy_variants.csv"))
        kinds = {g["reference"]: (g["exact"], g["differs_in"]) for g in F["duplicates"]["groups"]}
        self.assertTrue(kinds["M-16"][0])                                                 # three identical rows
        self.assertFalse(kinds["M-5"][0])
        self.assertIn("direction", kinds["M-5"][1])
        self.assertIn("date", kinds["M-5"][1])

    def test_reference_shared_with_an_excluded_row_is_found(self):
        df = load_text("Date,Reference,Description,Counterparty,Type,Amount,Status\n2026-10-01,X-900,a,A,Credit,100,Completed\n2026-10-02,X-900,b,A,Credit,-100,Completed\n2026-10-03,Y-1,c,A,Debit,5,Completed\n")
        F = facts.build(df)
        grp = F["duplicates"]["with_excluded"]
        self.assertEqual([(g["reference"], g["row_numbers"]) for g in grp], [("X-900", [2, 3])])
        self.assertEqual({r["Used in totals"][:2] for r in grp[0]["rows"]}, {"Ye", "No"})
        self.assertIn("X-900", facts.analysis_text(F, sections=("duplicates",)))

    def test_listings_use_file_row_numbers(self):
        txt = facts.analysis_text(self.F, sections=("duplicates",))
        self.assertIn("Row 23", txt)
        self.assertIn("Row 35", txt)
        self.assertNotIn("No.22", txt)


class FinancialConsistency(Base):
    def check_invariants(self, F):
        t = F["totals"]
        self.assertEqual(t["credit_count"] + t["debit_count"], F["rows_analysed"])
        self.assertAlmostEqual(t["net_movement"], round(t["money_in"] - t["money_out"], 2), places=2)
        self.assertEqual(F["rows_uploaded"], F["rows_analysed"] + F["rows_excluded"])
        self.assertEqual(len(F["excluded"]), F["rows_excluded"])
        dated = sum(x["transactions"] for x in F["daily"])
        undated = F["rows_analysed"] - dated
        self.assertGreaterEqual(undated, 0)
        md = F["missing"]["Date"]
        listed = {g["Row"]: g for g in md["rows"] if g["Used in totals"] == "Yes"}      # blank dates ...
        excluded_rows = {e["Row"] for e in F["excluded"]}
        for g in (F.get("invalid") or {}).get("dates", []):                              # ... plus unreadable dates, listed separately
            if g["Row"] not in excluded_rows:
                listed.setdefault(g["Row"], g)
        listed = list(listed.values())
        if len(md["rows"]) == md["count"]:      # the listing is capped for very large files; only reconcile when it is complete
            self.assertEqual(len(listed), undated)
            self.assertAlmostEqual(sum(x["credit_total"] for x in F["daily"]) + sum(g["Amount"] for g in listed if g["Type"] == "Credit"), t["money_in"], places=2)
        self.assertTrue(F["validation"]["ok"], F["validation"]["checks"])
        self.assertAlmostEqual(sum(r["credits"] for r in F["status"]["rows"]), t["money_in"], places=2)

    def test_stress_invariants_and_values(self):
        self.check_invariants(self.F)
        t = self.F["totals"]
        self.assertEqual((t["credit_count"], t["debit_count"], t["money_in"], t["money_out"], t["net_movement"]), (24, 7, 4748000.0, 126000.0, 4622000.0))
        self.assertEqual((self.F["rows_uploaded"], self.F["rows_analysed"], self.F["rows_excluded"]), (34, 31, 3))

    def test_averages_min_max_use_the_counted_population_only(self):
        t = self.F["totals"]
        self.assertEqual(t["highest_in"]["Amount"], 426000.0)
        self.assertEqual(t["lowest_in"]["Amount"], 27000.0)                               # the excluded -15,000 is not the "lowest"
        self.assertEqual(t["lowest_out"]["Amount"], 12500.0)                              # the excluded 0 is not the "lowest"
        self.assertAlmostEqual(t["average_in"], 4748000 / 24, places=2)

    def test_rules_hold_on_generated_datasets(self):
        """Datasets the engine has never seen: random anomalies, same invariants."""
        ran = 0
        for seed in range(8):
            rnd = random.Random(seed)
            rows = []
            for i in range(rnd.randint(25, 120)):
                kind = rnd.choice(["Credit", "Debit"] * 6 + ["TRANSFER", ""])
                amt = rnd.choice([rnd.randint(1, 900) * 100] * 40 + [0, -rnd.randint(1, 50) * 100, "abc", ""])
                date = rnd.choice(["2026-09-%02d" % rnd.randint(1, 28)] * 9 + ["", "31/31/2026"])
                ref = rnd.choice([f"R{seed}-{i}"] * 6 + [f"R{seed}-{rnd.randint(0, 9)}", "", "  "])
                rows.append(f"{date},{ref},{rnd.choice(['pay', ''])},{rnd.choice(['A', 'B', ' '])},{kind},{amt},{rnd.choice(['Completed', 'Pending', 'Reversed', 'Failed', ''])}")
            df = load_text("Date,Reference,Description,Counterparty,Type,Amount,Status\n" + "\n".join(rows))
            F = facts.build(df)
            if F is None:          # engine declined the table (not recognisable as money in/out): nothing to check
                continue
            ran += 1
            self.check_invariants(F)
            self.assertEqual(sum(len(g["row_numbers"]) for g in F["duplicates"]["groups"]), sum(g["count"] for g in F["duplicates"]["groups"]))
            blanks = sum(1 for r in rows if r.split(",")[1].strip() == "")
            self.assertEqual(F["missing"]["Reference"]["count"], blanks, seed)
        self.assertGreaterEqual(ran, 4, "too few generated datasets were analysable")


class QuestionPathUsesVerifiedRows(Base):
    """Root cause: core.add_money_columns() takes abs() of every amount and counts zero rows, so a question like 'total credits' was
    answered from a different population than the full analysis."""

    def test_plain_table_disagrees_with_verified_totals(self):
        out, _ = self.ask(self.df, "total credits", CREDITS)
        res = out["res"].iloc[0]
        self.assertEqual(res["Total credits"], 4763000.0)                                 # +15,000 from the negative row (the old, wrong answer)
        self.assertNotEqual(res["Total credits"], self.F["totals"]["money_in"])

    def test_question_frame_agrees_with_verified_totals(self):
        qdf = facts.question_frame(self.df, self.F)
        for plan, total_key, count_key in ((CREDITS, "money_in", "credit_count"), (DEBITS, "money_out", "debit_count")):
            out, _ = self.ask(qdf, "total", plan, F=self.F)
            row = out["res"].iloc[0]
            self.assertEqual(row[list(row.index)[0]], self.F["totals"][total_key])
            self.assertEqual(row[list(row.index)[1]], self.F["totals"][count_key])

    def test_question_frame_labels_excluded_rows_and_keeps_all_rows(self):
        qdf = facts.question_frame(self.df, self.F)
        self.assertEqual(len(qdf), len(self.df))
        self.assertEqual(int((qdf["Analysis treatment"] == "Excluded from totals").sum()), 3)
        self.assertEqual(int((qdf["Credit/Debit"] == "Not counted").sum()), 3)
        self.assertEqual(float(qdf["Amount (always positive)"].sum()), 4748000.0 + 126000.0)
        self.assertEqual(sorted(qdf.loc[qdf["Analysis treatment"] == "Excluded from totals", "Amount as uploaded"].tolist()), [-15000.0, 0.0, 40000.0])

    def test_clean_table_is_unchanged_in_meaning(self):
        qdf = facts.question_frame(self.clean, self.CF)
        self.assertTrue((qdf["Analysis treatment"] == "Counted").all())
        out, _ = self.ask(qdf, "total credits", CREDITS, F=self.CF)
        self.assertEqual(out["res"].iloc[0]["Total credits"], self.CF["totals"]["money_in"])

    def test_web_uses_the_shared_frame(self):
        src = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
        self.assertIn("facts.question_frame(df, F)", src)
        self.assertIn("core.analyze(qdf,", src)
        self.assertNotIn("Exact analysis of your file", src)                              # the old unrelated-report fallback is gone

    def test_everything_text_has_no_mixed_total(self):
        old = fullread.describe(self.df)
        new = fullread.describe(self.df, self.F)
        self.assertIn("total 4,899,000.00", old)                                          # credits + debits + excluded rows added together
        self.assertNotIn("4,899,000", new)
        self.assertIn("₦4,748,000.00", new)
        self.assertIn("34 uploaded = 31 analysed + 3 excluded", new)


class AiUnavailableFallback(Base):
    def setUp(self):
        self.h = web_helpers()

    def answer(self, question, err="The AI service is slow right now. Please try again in a moment."):
        return self.h["_no_ai_answer"](self.df, self.F, question, RuntimeError(err))

    def test_totals_question_answers_that_question_from_verified_data(self):
        o = self.answer("What is the net movement?")
        self.assertEqual((o["answer_source"], o["retry"], o["fallback"]), ("calculated", True, True))
        self.assertIn("₦4,622,000.00", o["insight"])
        self.assertIn("CALCULATED BY RECONZA", o["insight"])
        self.assertEqual(o["res"].loc[o["res"].iloc[:, 0] == "Net movement"].iloc[0]["Total"], 4622000.0)
        self.assertNotIn("Everything in this file", o["title"])

    def test_missing_reference_question(self):
        o = self.answer("Which rows have a missing reference?")
        self.assertEqual(o["res"]["Row"].tolist(), [31])
        self.assertEqual(o["res"]["Field"].tolist(), ["Reference"])

    def test_status_questions(self):
        p = self.answer("Which transactions are pending?")["res"]
        self.assertEqual(sorted(p["Reference"]), ["ST-2008", "ST-2032"])
        r = self.answer("show reversed transactions")["res"]
        self.assertEqual(sorted(r["Reference"]), ["ST-2011", "ST-2033"])

    def test_duplicates_excluded_reference_and_credit_questions(self):
        self.assertEqual(self.answer("list the duplicate references")["res"]["Row"].tolist(), [23, 35])
        self.assertEqual(sorted(self.answer("which rows were excluded and why?")["res"]["Reference"]), ["ST-2025", "ST-2026", "ST-2027"])
        one = self.answer("tell me about ST-2026")
        self.assertEqual(one["res"]["Row"].tolist(), [27])
        self.assertIn("No -", one["res"]["Used in totals"].iloc[0])
        c = self.answer("How many credits are there?")["res"]
        self.assertEqual(c.iloc[0]["Count"], 24)
        self.assertEqual(len(c), 1)

    def test_unsupported_question_is_not_guessed_and_not_replaced_by_a_report(self):
        o = self.answer("Why did revenue fall in October and what should we do about it?")
        self.assertEqual((o["answer_source"], o["retry"]), ("unavailable", True))
        self.assertIn("I have not guessed an answer", o["insight"])
        self.assertIn("not an answer to your question", o["insight"])
        self.assertNotIn("VERIFIED ANALYSIS OF THE WHOLE FILE", o["insight"])
        self.assertLessEqual(len(o["res"]), 5)                                            # headline figures only, not the whole file

    def test_error_kinds(self):
        for msg, kind in (("The AI service is slow right now. Please try again in a moment.", "timeout"), ("Out of time budget", "timeout"), ("rate limit exceeded (429)", "rate_limit"),
                          ("could not connect to the AI", "network"), ("The AI gave no answer", "invalid_response"), ("empty answer", "invalid_response"), ("something odd", "unavailable")):
            self.assertEqual(qa.classify_error(RuntimeError(msg))[0], kind, msg)
        src = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
        for kw in ("rate limit", "time budget", "could not connect", "timeout", "429"):
            self.assertIn(kw, src.split("AI_DOWN_WORDS = ")[1].split(")")[0])

    def test_answers_agree_with_the_full_analysis(self):
        o = self.answer("What is the net movement?")
        self.assertIn(facts._m(self.F["totals"]["net_movement"]), o["insight"])
        self.assertIn(facts._m(self.F["totals"]["net_movement"]), facts.analysis_text(self.F))

    def test_ai_wording_failure_is_flagged_for_retry(self):
        qdf = facts.question_frame(self.df, self.F)
        out, worded = self.ask(qdf, "total credits", CREDITS, summary=None, F=self.F)
        self.assertFalse(worded)                                                          # figures exist, but the AI wrote nothing
        self.assertIn("The exact figures are in the table", out["insight"])
        out, worded = self.ask(qdf, "total credits", CREDITS, summary="ANSWER: Credits were ₦4,748,000.00", F=self.F)
        self.assertTrue(worded)

    def test_planner_failure_raises_an_ai_down_error(self):
        qdf = facts.question_frame(self.df, self.F)
        with self.assertRaises(RuntimeError) as cm:
            self.ask(qdf, "anything", CREDITS, plan_error="The AI service is slow right now.", F=self.F)
        self.assertTrue(any(k in str(cm.exception).lower() for k in ast.literal_eval("(" + open(os.path.join(ROOT, "web.py"), encoding="utf-8").read().split("AI_DOWN_WORDS = (")[1].split(")")[0] + ")")))


class Verification(Base):
    def test_source_row_check_is_labelled_for_what_it_does_and_covers_every_row(self):
        v = self.F["validation"]
        row_checks = [c for c in v["checks"] if c["kind"] == "source row"]
        self.assertEqual(len(row_checks), 1)
        self.assertIn("table as read from your file", row_checks[0]["check"])
        self.assertIn("not re-opened", row_checks[0]["detail"])
        self.assertEqual(v["coverage"]["method"], "every row")
        self.assertEqual(v["coverage"]["checked"], v["coverage"]["total"])
        self.assertIn("every row, not a sample", " ".join(self.F["summary"]))

    def test_arithmetic_success_does_not_hide_data_quality_problems(self):
        v = self.F["validation"]
        self.assertTrue(v["ok"])
        self.assertEqual(v["status"], "CALCULATIONS VERIFIED - DATA-QUALITY ISSUES REMAIN")
        self.assertGreaterEqual(len(v["issues"]), 5)
        self.assertIn("does not mean the data is clean", " ".join(self.F["summary"]))

    def test_clean_file_is_fully_verified(self):
        self.assertEqual(self.CF["validation"]["status"], "VERIFIED")
        self.assertEqual(self.CF["validation"]["issues"], [])

    def test_check_kinds_are_separated(self):
        kinds = {c["kind"] for c in self.F["validation"]["checks"]}
        self.assertTrue({"arithmetic", "row accounting", "source row"} <= kinds)


class ExcludedRows(Base):
    def test_every_excluded_row_is_listed_and_rows_reconcile(self):
        F = self.F
        self.assertEqual(F["rows_uploaded"], F["rows_analysed"] + len(F["excluded"]))
        for r in F["excluded"]:
            for k in ("Row", "Reference", "Date", "Type", "Amount", "Reason"):
                self.assertIn(k, r)
        self.assertEqual(sorted(r["Row"] for r in F["excluded"]), [26, 27, 28])
        self.assertEqual({r["Row"] for r in F["warned"]}, {25, 29, 30, 31})                # analysed WITH warnings are kept apart from excluded
        self.assertFalse({r["Row"] for r in F["warned"]} & {r["Row"] for r in F["excluded"]})

    def test_messy_file_reconciles(self):
        F = facts.build(load("messy_variants.csv"))
        self.assertEqual((F["rows_uploaded"], F["rows_analysed"], F["rows_excluded"]), (19, 12, 7))
        self.assertEqual(len(F["excluded"]), 7)


class ExportsAgree(Base):
    def test_pdf_has_every_section_and_every_exception(self):
        txt = pdf_text(self.F)
        for needle in ("CALCULATIONS VERIFIED, DATA-QUALITY ISSUES REMAIN", "3. Transaction status", "5.2 Missing fields", "5.3 Excluded rows (3)", "ST-2025", "ST-2026", "ST-2027",
                       "ST-2008", "ST-2032", "ST-2011", "ST-2033", "ST-2022", "ST-2024", "ST-2028", "ST-2029", "How rows are treated", "table as read from your file", "4,748,000.00", "4,622,000.00"):
            self.assertIn(needle, txt)
        self.assertNotIn("4,899,000", txt)
        self.assertIn("34 uploaded = 31 analysed + 3 excluded", txt)

    def test_excel_has_the_same_content(self):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf) as w:
            pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="Result", index=False)
        book = pd.ExcelFile(io.BytesIO(facts.add_sheets(buf.getvalue(), self.F)))
        for sheet in ("Excluded rows", "Missing fields", "Transaction status", "Status transactions", "Duplicate references", "Validation", "Data quality", "How rows are treated", "Pattern findings"):
            self.assertIn(sheet, book.sheet_names)
        self.assertEqual(len(book.parse("Excluded rows")), 3)
        self.assertEqual(sorted(book.parse("Missing fields")["Field"]), ["Counterparty", "Date", "Description", "Reference"])
        self.assertEqual(book.parse("Transaction status").set_index("Status").loc["Pending", "Count"], 2)
        self.assertEqual(sorted(book.parse("Status transactions")["Reference"]), ["ST-2008", "ST-2011", "ST-2032", "ST-2033"])
        self.assertEqual(book.parse("Duplicate references")["Row"].tolist(), [23, 35])
        self.assertEqual(book.parse("Validation").iloc[-1]["Result"], "CALCULATIONS VERIFIED - DATA-QUALITY ISSUES REMAIN")

    def test_text_pdf_excel_and_screen_use_one_set_of_numbers(self):
        t = self.F["totals"]
        money = facts._m(t["money_in"])
        for txt in (facts.analysis_text(self.F), pdf_text(self.F), " ".join(self.F["summary"]), fullread.describe(self.df, self.F)):
            self.assertIn(money, txt)
        self.assertEqual(facts.compact(self.F)["rows_excluded"], 3)

    def test_clean_pdf_says_verified(self):
        txt = pdf_text(self.CF)
        self.assertIn("VERIFIED:", txt)
        self.assertIn("no data-quality issue was found", txt)


class CleanDataset(Base):
    def test_no_false_warnings(self):
        F = self.CF
        self.assertEqual((F["rows_excluded"], F["rows_with_warnings"]), (0, 0))
        self.assertTrue(all(v["count"] == 0 for v in F["missing"].values()))
        self.assertEqual(F["duplicates"]["groups"], [])
        self.assertEqual(F["duplicates"]["with_excluded"], [])
        self.assertEqual(F["invalid"], {"dates": [], "types": [], "negative": [], "zero": []})
        self.assertEqual([q["check"] for q in F["quality"] if q["status"] == "warn"], [])
        self.assertEqual(F["rows_uploaded"], F["rows_analysed"])

    def test_questions_still_work_without_ai(self):
        o = web_helpers()["_no_ai_answer"](self.clean, self.CF, "What is the net movement?", RuntimeError("timeout"))
        self.assertIn(facts._m(self.CF["totals"]["net_movement"]), o["insight"])


if __name__ == "__main__":
    unittest.main()
