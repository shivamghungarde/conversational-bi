"""Run with:  python3 -m unittest discover -s tests"""
import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

os.environ.pop("BI_LOCAL_LLM_URL", None)  # tests never call a model

from bi import autosemantic, data, router, xlsx
from bi.engine import Engine
from bi.llm import is_on_premises
from bi.semantic import Filter, QueryError, SemanticModel, SemanticQuery, TimeRange, shift_range
from bi.sources import FileSource, SQLiteSource

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = dt.date(2026, 10, 25)


class DemoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "demo.db"
        data.build(cls.db, end=TODAY)
        model = SemanticModel.load(ROOT / "semantic" / "professional_services.json")
        cls.engine = Engine("delivery", model, SQLiteSource(cls.db))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, question, context=None, clarifications=None):
        return self.engine.rules.parse(question, context, today=TODAY, clarifications=clarifications)

    def ask(self, question, **kw):
        return self.engine.ask(question, today=TODAY, **kw)


class ParserTests(DemoTestCase):
    def test_metric_by_dimension_and_period(self):
        q = self.parse("Utilization by practice this year").query
        self.assertEqual((q.metrics, q.dimensions), (["utilization"], ["practice"]))
        self.assertEqual((q.time_range.start, q.time_range.end), ("2026-01-01", "2026-10-26"))

    def test_synonyms(self):
        self.assertEqual(self.parse("chargeability per service line").query.dimensions, ["practice"])
        self.assertEqual(self.parse("utilisation by level").query.metrics, ["utilization"])
        self.assertEqual(self.parse("billings by account").query.dimensions, ["client"])

    def test_top_n(self):
        q = self.parse("Top 5 clients by revenue year to date").query
        self.assertEqual((q.metrics, q.dimensions, q.limit, q.order_dir), (["revenue"], ["client"], 5, "desc"))

    def test_filters_and_negation(self):
        q = self.parse("Utilization for India by grade, excluding Strategy").query
        filters = {(f.dimension, f.op): f.values for f in q.filters}
        self.assertEqual(filters, {("region", "in"): ["India"], ("practice", "not_in"): ["Strategy"]})

    def test_value_with_ampersand(self):
        q = self.parse("utilization for Data & AI").query
        self.assertEqual(q.filters[0].values, ["Data & AI"])

    def test_headcount_is_not_a_grade_filter(self):
        q = self.parse("how many consultants in India").query
        self.assertEqual(q.metrics, ["headcount"])
        self.assertEqual([f.dimension for f in q.filters], ["region"])

    def test_time_phrases(self):
        cases = {
            "revenue last quarter": ("2026-07-01", "2026-10-01"),
            "revenue last month": ("2026-09-01", "2026-10-01"),
            "revenue in Q2 2025": ("2025-04-01", "2025-07-01"),
            "revenue last 7 days": ("2026-10-19", "2026-10-26"),
        }
        for question, expected in cases.items():
            tr = self.parse(question).query.time_range
            self.assertEqual((tr.start, tr.end), expected, question)

    def test_follow_up_keeps_context(self):
        first = self.parse("Utilization by practice last quarter").query
        q = self.parse("now by region", first).query
        self.assertEqual((q.metrics, q.dimensions, q.time_range.label), (["utilization"], ["region"], "Last quarter"))
        self.assertEqual(self.parse("what about India?", q).query.filters[0].values, ["India"])

    def test_why_is_explain(self):
        interp = self.parse("Why did utilization drop last month?")
        self.assertTrue(interp.query.explain)
        self.assertEqual(interp.query.compare, "previous_period")
        self.assertEqual(interp.claimed_direction, "down")
        self.assertEqual(interp.ambiguities, [])


class ClarifyTests(DemoTestCase):
    def test_vague_measure_asks(self):
        res = self.ask("How is performance by practice?")
        self.assertEqual(res["type"], "clarify")
        self.assertEqual([o["clarification"]["metric"] for o in res["options"]],
                         ["utilization", "revenue", "gross_margin"])

    def test_no_measure_asks_instead_of_guessing(self):
        res = self.ask("by region last month")
        self.assertEqual((res["type"], res["ambiguity"]), ("clarify", "metric"))

    def test_why_without_period_asks_which_periods(self):
        res = self.ask("Why did utilization drop?")
        self.assertEqual(res["ambiguity"], "explain_period")
        answered = self.ask("Why did utilization drop?", clarifications=res["options"][0]["clarification"])
        self.assertEqual(answered["type"], "explain")

    def test_rank_without_breakdown_asks(self):
        self.assertEqual(self.ask("top 5 by revenue")["ambiguity"], "dimension")

    def test_vague_time_asks(self):
        res = self.ask("utilization recently")
        self.assertEqual(res["ambiguity"], "time")
        answered = self.ask("utilization recently", clarifications=res["options"][1]["clarification"])
        self.assertEqual(answered["type"], "result")

    def test_clarification_resolves(self):
        res = self.ask("How is performance by practice?", clarifications={"metric": "utilization"})
        self.assertEqual((res["type"], res["query"]["metrics"]), ("result", ["utilization"]))


class ExplainTests(DemoTestCase):
    def test_ratio_contributions_sum_to_change(self):
        res = self.ask("Why did utilization drop last month?")
        self.assertEqual(res["type"], "explain")
        total = res["current"]["value"] - res["previous"]["value"]
        self.assertLess(total, 0)  # the demo's September dip
        for dim, members in res["drivers"].items():
            if len(members) < 60:  # all members present (not truncated)
                self.assertAlmostEqual(sum(m["contribution"] for m in members), total, places=9, msg=dim)
        self.assertIn(res["primary_dimension"], ("region", "practice"))
        titles = [s["title"] for s in res["sections"]]
        self.assertEqual(titles[0], "What happened")
        self.assertIn("Why it happened", titles)

    def test_additive_contributions_sum_to_change(self):
        res = self.ask("What drove the change in revenue last month?")
        total = res["current"]["value"] - res["previous"]["value"]
        self.assertAlmostEqual(sum(m["contribution"] for m in res["drivers"]["client"]), total, places=2)

    def test_names_the_planted_causes(self):
        res = self.ask("Why did utilization drop last month?")
        practice_top = res["drivers"]["practice"][0]["member"]
        self.assertEqual(practice_top, "Data & AI")
        self.assertEqual(res["drivers"]["region"][0]["member"], "India")

    def test_wrong_direction_is_corrected(self):
        res = self.ask("Why did utilization rise last month?")
        self.assertIn("Actually", res["sections"][0]["text"])


class SemanticLayerTests(DemoTestCase):
    def test_unknown_names_rejected(self):
        with self.assertRaises(QueryError):
            self.engine.model.validate(SemanticQuery(metrics=["salary"]))
        with self.assertRaises(QueryError):
            self.engine.model.validate(SemanticQuery(metrics=["revenue"], dimensions=["region; DROP TABLE x"]))

    def test_filter_values_are_parameters(self):
        evil = "x') OR 1=1 --"
        q = SemanticQuery(metrics=["revenue"], filters=[Filter("region", "in", [evil])])
        sql, params, _ = self.engine.model.compile(q)
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)
        self.assertIsNone(self.engine.query(q)["rows"][0]["revenue"])

    def test_left_join_keeps_bench_capacity(self):
        q = SemanticQuery(metrics=["available_hours"], time_range=TimeRange("2026-09-01", "2026-10-01"))
        total = self.engine.query(q)["rows"][0]["available_hours"]
        by_client = self.engine.query(SemanticQuery(metrics=["available_hours"], dimensions=["client"],
                                                    time_range=TimeRange("2026-09-01", "2026-10-01")))
        self.assertAlmostEqual(sum(r["available_hours"] for r in by_client["rows"]), total)
        self.assertIn("Bench / internal", [r["client"] for r in by_client["rows"]])

    def test_percent_changes_are_points(self):
        q = SemanticQuery(metrics=["utilization"], time_range=TimeRange("2026-09-01", "2026-10-01"),
                          compare="previous_period")
        row = self.engine.query(q)["rows"][0]
        self.assertAlmostEqual(row["utilization__change"], row["utilization"] - row["utilization__prev"])

    def test_previous_period_alignment(self):
        tr = shift_range(TimeRange("2026-07-01", "2026-10-01"), "previous_period")
        self.assertEqual((tr.start, tr.end), ("2026-04-01", "2026-07-01"))
        tr = shift_range(TimeRange("2026-10-01", "2026-10-26", "Month to date"), "previous_period")
        self.assertEqual((tr.start, tr.end), ("2026-09-01", "2026-09-26"))


class LiveDataTests(DemoTestCase):
    def test_feeder_moves_late_timesheets(self):
        db = Path(self.tmp.name) / "live.db"
        shutil.copy(self.db, db)
        con = sqlite3.connect(db)
        before = con.execute("SELECT COUNT(*) FROM timesheets").fetchone()[0]
        backlog = con.execute("SELECT COUNT(*) FROM timesheet_backlog").fetchone()[0]
        con.close()
        self.assertGreater(backlog, 0)
        moved = data.LiveFeeder(db, batch=5).step()
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM timesheets").fetchone()[0], before + moved)
        con.close()

    def test_answers_carry_freshness(self):
        res = self.ask("What is utilization this month?")
        self.assertIn("checked_at", res["freshness"])
        self.assertEqual(res["freshness"]["coverage"]["from"], "2025-01-01")


class FileSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def engine_for(self, path):
        src = FileSource(path, self.tmp / "cache")
        (count,) = src.con.execute(f'SELECT COUNT(*) FROM "{src.table}"').fetchone()
        spec = autosemantic.infer(src.table, src.columns, "Export", count)
        return Engine("file", SemanticModel(spec), src)

    def test_csv_profile_and_auto_model(self):
        path = self.tmp / "export.csv"
        shutil.copy(FIXTURES / "timesheet_export.csv", path)
        e = self.engine_for(path)
        types = {c["name"]: c["type"] for c in e.source.columns}
        self.assertEqual((types["date"], types["revenue"], types["practice"]), ("date", "number", "text"))
        self.assertIn("utilization", e.model.metrics)  # billable vs available detected
        res = e.ask("Why did utilization change last month?", today=TODAY)
        self.assertEqual(res["type"], "explain")
        self.assertEqual(e.ask("Utilization by practice", today=TODAY)["type"], "result")

    def test_file_changes_are_picked_up(self):
        path = self.tmp / "export.csv"
        shutil.copy(FIXTURES / "timesheet_export.csv", path)
        e = self.engine_for(path)
        before = e.query(SemanticQuery(metrics=["row_count"]))["rows"][0]["row_count"]
        time.sleep(0.05)
        with open(path, "a") as fh:
            fh.write('30/09/2026,New Person,Strategy,Europe,Manager,Bench,8,8,"$2,000.00"\n')
        os.utime(path, (time.time() + 5, time.time() + 5))
        after = e.query(SemanticQuery(metrics=["row_count"]))["rows"][0]["row_count"]
        self.assertEqual(after, before + 1)

    def test_xlsx_reader(self):
        path = self.tmp / "book.xlsx"
        write_xlsx(path, ["Month", "Practice", "Revenue"],
                   [(45931, "Cloud", 1200.5), (45962, "ERP", 800)])  # 2025-10-01, 2025-11-01
        headers, rows = xlsx.read(path)
        self.assertEqual(headers, ["Month", "Practice", "Revenue"])
        self.assertEqual(rows, [["2025-10-01", "Cloud", 1200.5], ["2025-11-01", "ERP", 800]])
        e = self.engine_for(path)
        self.assertEqual(e.query(SemanticQuery(metrics=["revenue"]))["rows"][0]["revenue"], 2000.5)


class RoutingTests(DemoTestCase):
    """Two connected sources: the demo database and a CSV export that shares some measures."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = Path(cls.tmp.name) / "timesheet_export.csv"
        shutil.copy(FIXTURES / "timesheet_export.csv", path)
        src = FileSource(path, Path(cls.tmp.name) / "cache")
        (count,) = src.con.execute(f'SELECT COUNT(*) FROM "{src.table}"').fetchone()
        cls.csv = Engine("export", SemanticModel(autosemantic.infer(src.table, src.columns, "Export", count)), src)
        cls.engines = [cls.engine, cls.csv]

    def route(self, question, context=None, context_dataset=None):
        return router.route(self.engines, question, context, context_dataset, today=TODAY)

    def test_unique_measure_goes_to_its_source(self):
        engine, clarify = self.route("Average bill rate by grade")
        self.assertIsNone(clarify)
        self.assertEqual(engine.id, "delivery")
        self.assertEqual(self.route("How many records are there?")[0].id, "export")

    def test_shared_measure_asks_which_source(self):
        engine, clarify = self.route("Utilization by practice last month")
        self.assertIsNone(engine)
        self.assertEqual(clarify["ambiguity"], "dataset")
        self.assertEqual({o["clarification"]["dataset"] for o in clarify["options"]}, {"delivery", "export"})

    def test_follow_up_stays_on_previous_source(self):
        first = self.csv.ask("Utilization by practice last month", today=TODAY)
        engine, clarify = self.route("now by region", first["query"], "export")
        self.assertIsNone(clarify)
        self.assertEqual(engine.id, "export")


class OnPremGuardTests(unittest.TestCase):
    def test_only_private_endpoints_allowed(self):
        self.assertTrue(is_on_premises("http://localhost:11434"))
        self.assertTrue(is_on_premises("http://10.0.0.5:8000"))
        self.assertTrue(is_on_premises("http://192.168.1.20:8000"))
        self.assertFalse(is_on_premises("https://8.8.8.8/v1"))


class EndToEndTests(DemoTestCase):
    def test_every_sample_query_answers(self):
        for question in self.engine.model.sample_queries:
            res = self.ask(question)
            self.assertTrue(res["ok"], f"{question}: {res.get('error')}")
            self.assertIn(res["type"], ("result", "explain", "clarify"), question)
            if res["type"] == "result":
                self.assertTrue(res["rows"], question)

    def test_nonsense_is_declined(self):
        res = self.ask("tell me a joke")
        self.assertFalse(res["ok"])
        self.assertTrue(res["samples"])


def write_xlsx(path, headers, rows):
    """A tiny .xlsx writer for tests: shared strings, numbers, and a date style on column A."""
    strings = []

    def sidx(v):
        if v not in strings:
            strings.append(v)
        return strings.index(v)

    def cell(ref, v, date=False):
        if isinstance(v, str):
            return f'<c r="{ref}" t="s"><v>{sidx(v)}</v></c>'
        style = ' s="1"' if date else ""
        return f'<c r="{ref}"{style}><v>{v}</v></c>'

    xml_rows = ["<row>" + "".join(cell(f"{chr(65 + i)}1", h) for i, h in enumerate(headers)) + "</row>"]
    for n, r in enumerate(rows, start=2):
        xml_rows.append("<row>" + "".join(cell(f"{chr(65 + i)}{n}", v, date=i == 0) for i, v in enumerate(r)) + "</row>")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(xml_rows)}</sheetData></worksheet>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')


if __name__ == "__main__":
    unittest.main()
