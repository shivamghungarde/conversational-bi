"""Run with:  python3 -m unittest discover -s tests"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

os.environ["BI_LLM"] = "off"  # tests never call the network

from bi import data
from bi.engine import Engine
from bi.semantic import Filter, QueryError, SemanticQuery, TimeRange, shift_range

ROOT = Path(__file__).resolve().parent.parent
TODAY = dt.date(2026, 10, 25)


class EngineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "demo.db"
        data.build(db, end=TODAY)
        cls.engine = Engine(ROOT / "semantic" / "retail_sales.json", db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def parse(self, question, context=None):
        return self.engine.rules.parse(question, context, today=TODAY)


class ParserTests(EngineTestCase):
    def test_metric_by_dimension_and_period(self):
        q = self.parse("Revenue by region this year").query
        self.assertEqual(q.metrics, ["revenue"])
        self.assertEqual(q.dimensions, ["region"])
        self.assertEqual((q.time_range.start, q.time_range.end), ("2026-01-01", "2026-10-26"))

    def test_synonyms(self):
        q = self.parse("sales per channel").query
        self.assertEqual((q.metrics, q.dimensions), (["revenue"], ["channel"]))
        q = self.parse("basket size by customer type").query
        self.assertEqual((q.metrics, q.dimensions), (["aov"], ["segment"]))

    def test_top_n(self):
        q = self.parse("Top 5 products by gross profit").query
        self.assertEqual((q.metrics, q.dimensions, q.limit, q.order_dir), (["gross_profit"], ["product"], 5, "desc"))
        q = self.parse("bottom 3 categories by margin").query
        self.assertEqual((q.limit, q.order_dir, q.metrics), (3, "asc", ["margin_pct"]))

    def test_filters_and_negation(self):
        q = self.parse("Revenue for Electronics by region, excluding Partner").query
        filters = {(f.dimension, f.op): f.values for f in q.filters}
        self.assertEqual(filters, {("category", "in"): ["Electronics"], ("channel", "not_in"): ["Partner"]})
        self.assertEqual(q.dimensions, ["region"])

    def test_plural_value(self):
        q = self.parse("how many laptops did we sell in March 2026").query
        self.assertEqual(q.metrics, ["units"])
        self.assertEqual(q.filters[0].values, ["Laptop"])
        self.assertEqual(q.time_range.label, "March 2026")

    def test_time_phrases(self):
        cases = {
            "revenue last quarter": ("2026-07-01", "2026-10-01"),
            "revenue last month": ("2026-09-01", "2026-10-01"),
            "revenue in Q2 2025": ("2025-04-01", "2025-07-01"),
            "revenue in 2025": ("2025-01-01", "2026-01-01"),
            "revenue last 7 days": ("2026-10-19", "2026-10-26"),
            "revenue ytd": ("2026-01-01", "2026-10-26"),
        }
        for question, expected in cases.items():
            tr = self.parse(question).query.time_range
            self.assertEqual((tr.start, tr.end), expected, question)

    def test_trend_defaults_to_twelve_months(self):
        q = self.parse("revenue trend").query
        self.assertEqual(q.time_grain, "month")
        self.assertEqual(q.time_range.label, "Last 12 months")

    def test_comparison(self):
        q = self.parse("How did revenue compare to last year?").query
        self.assertEqual(q.compare, "previous_year")
        self.assertEqual(q.time_range.label, "Year to date")

    def test_follow_up_keeps_context(self):
        first = self.parse("Revenue by region last quarter").query
        q = self.parse("now by channel", first).query
        self.assertEqual((q.metrics, q.dimensions), (["revenue"], ["channel"]))
        self.assertEqual(q.time_range.label, "Last quarter")
        q2 = self.parse("what about Online?", q).query
        self.assertEqual(q2.filters[0].values, ["Online"])

    def test_nonsense_has_zero_confidence(self):
        self.assertEqual(self.parse("tell me a joke").confidence, 0.0)


class SemanticLayerTests(EngineTestCase):
    def test_unknown_names_rejected(self):
        with self.assertRaises(QueryError):
            self.engine.model.validate(SemanticQuery(metrics=["salary"]))
        with self.assertRaises(QueryError):
            self.engine.model.validate(SemanticQuery(dimensions=["region; DROP TABLE orders"]))

    def test_filter_values_are_parameters(self):
        evil = "x') OR 1=1 --"
        q = SemanticQuery(filters=[Filter("region", "in", [evil])])
        sql, params, _ = self.engine.model.compile(q)
        self.assertNotIn(evil, sql)
        self.assertIn(evil, params)
        res = self.engine.query(q)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["rows"][0]["revenue"])

    def test_joins_added_only_when_needed(self):
        sql, _, _ = self.engine.model.compile(SemanticQuery(dimensions=["region"]))
        self.assertNotIn("JOIN", sql)
        sql, _, _ = self.engine.model.compile(SemanticQuery(dimensions=["category"]))
        self.assertIn("JOIN products", sql)

    def test_breakdown_sums_to_total(self):
        q = SemanticQuery(metrics=["revenue"], dimensions=["region"],
                          time_range=TimeRange("2025-01-01", "2026-01-01", "2025"))
        res = self.engine.query(q)
        self.assertAlmostEqual(sum(r["revenue"] for r in res["rows"]), res["totals"]["revenue"], places=2)

    def test_previous_period_is_calendar_aligned(self):
        tr = shift_range(TimeRange("2026-07-01", "2026-10-01"), "previous_period")
        self.assertEqual((tr.start, tr.end), ("2026-04-01", "2026-07-01"))
        tr = shift_range(TimeRange("2026-07-01", "2026-10-01"), "previous_year")
        self.assertEqual((tr.start, tr.end), ("2025-07-01", "2025-10-01"))


class EndToEndTests(EngineTestCase):
    def test_every_sample_query_answers(self):
        for question in self.engine.model.sample_queries:
            res = self.engine.ask(question, today=TODAY)
            self.assertTrue(res["ok"], f"{question}: {res.get('error')}")
            self.assertTrue(res["answer"])
            self.assertTrue(res["rows"], question)

    def test_answer_is_plain_english_with_numbers(self):
        res = self.engine.ask("Revenue by region in 2025", today=TODAY)
        self.assertRegex(res["answer"], r"leads with \$[\d.]+[KM]")

    def test_unanswerable_returns_samples(self):
        res = self.engine.ask("tell me a joke", today=TODAY)
        self.assertFalse(res["ok"])
        self.assertTrue(res["samples"])


if __name__ == "__main__":
    unittest.main()
