"""Superstore demo source: loader, semantic model and answers checked against SQL computed straight from the data.

Run with:  python3 -m unittest discover -s tests
"""
import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bi import router, superstore
from bi.config import ROOT, Settings
from bi.engine import Engine
from bi.registry import build_registry
from bi.semantic import Filter, SemanticModel, SemanticQuery, TimeRange
from bi.sources import SQLiteSource

TODAY = dt.date(2026, 11, 7)
Y2021 = TimeRange("2021-01-01", "2022-01-01", "2021")


class SuperstoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "superstore.db"
        superstore.ensure(cls.db)
        source = SQLiteSource(cls.db)
        source.live = False
        cls.engine = Engine("superstore", SemanticModel.load(ROOT / "semantic" / "superstore.json"), source)
        cls.raw = sqlite3.connect(cls.db)  # the oracle: plain SQL straight against the tables

    @classmethod
    def tearDownClass(cls):
        cls.raw.close()
        cls.engine.source.close()
        shutil.rmtree(cls.tmp)

    def sql(self, query, *params):
        return self.raw.execute(query, params).fetchone()[0]

    def ask(self, question, **kw):
        res = self.engine.ask(question, today=TODAY, **kw)
        self.assertTrue(res.get("ok"), f"{question}: {res.get('error')}")
        return res


class LoaderTests(SuperstoreTestCase):
    def test_row_counts_and_grain(self):
        self.assertEqual(self.sql("SELECT COUNT(*) FROM orders"), 9994)
        self.assertEqual(self.sql("SELECT COUNT(DISTINCT order_id) FROM orders"), 5009)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM returns"), 296)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM returns r WHERE NOT EXISTS "
                                  "(SELECT 1 FROM orders o WHERE o.order_id = r.order_id)"), 0)

    def test_zip_codes_keep_leading_zeros(self):
        self.assertEqual(self.sql("SELECT postal_code FROM orders WHERE city = 'Lawrence' AND state = 'Massachusetts' LIMIT 1"),
                         "01841")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM orders WHERE length(postal_code) <> 5"), 0)
        # The source has no ZIP for Burlington, Vermont: kept as a real gap (NULL), not a blank string.
        self.assertEqual(self.sql("SELECT COUNT(*) FROM orders WHERE postal_code IS NULL"), 11)

    def test_returns_cannot_double_count(self):
        with self.assertRaises(sqlite3.IntegrityError):
            con = sqlite3.connect(self.db)
            try:
                order = con.execute("SELECT order_id FROM returns LIMIT 1").fetchone()[0]
                con.execute("INSERT INTO returns VALUES (?, 'Duplicate')", (order,))
            finally:
                con.rollback()
                con.close()

    def test_rebuild_only_when_sample_files_change(self):
        built = self.sql("SELECT built_at FROM snapshot")
        superstore.ensure(self.db)
        self.assertEqual(self.sql("SELECT built_at FROM snapshot"), built)


class KnownTotalsTests(SuperstoreTestCase):
    """The published Superstore figures, and the same numbers via plain SQL."""

    def test_published_totals(self):
        q = lambda **kw: self.engine.query(SemanticQuery(**kw))["rows"][0]
        self.assertAlmostEqual(q(metrics=["sales"])["sales"], 2297201, delta=1)
        self.assertAlmostEqual(q(metrics=["profit"])["profit"], 286397, delta=1)
        year = q(metrics=["sales", "profit"], time_range=Y2021)
        self.assertAlmostEqual(year["sales"], 733215, delta=1)
        self.assertAlmostEqual(year["profit"], 93439, delta=1)

    def test_breakdown_matches_plain_sql(self):
        res = self.ask("Profit margin by category")
        for row in res["rows"]:
            expected = self.sql("SELECT SUM(profit) / SUM(sales) FROM orders WHERE category = ?", row["category"])
            self.assertAlmostEqual(row["profit_margin"], expected, places=9)

    def test_return_rate_is_per_order(self):
        res = self.ask("Return rate by region")
        for row in res["rows"]:
            expected = self.sql("SELECT COUNT(DISTINCT CASE WHEN r.order_id IS NOT NULL THEN o.order_id END) * 1.0 / "
                                "COUNT(DISTINCT o.order_id) FROM orders o LEFT JOIN returns r ON o.order_id = r.order_id "
                                "WHERE o.region = ?", row["region"])
            self.assertAlmostEqual(row["return_rate"], expected, places=9)
        line_based = self.sql("SELECT AVG(r.order_id IS NOT NULL) FROM orders o LEFT JOIN returns r ON o.order_id = r.order_id")
        self.assertNotAlmostEqual(res["totals"]["return_rate"], line_based, places=3)  # the naive per-line rate differs

    def test_returned_sales_by_reason_adds_up(self):
        res = self.ask("Returned sales by return reason")
        self.assertEqual(res["type"], "result")
        total = self.sql("SELECT SUM(o.sales) FROM orders o JOIN returns r ON o.order_id = r.order_id")
        self.assertAlmostEqual(sum(r["returned_sales"] for r in res["rows"]), total, places=2)


class QuestionTests(SuperstoreTestCase):
    def test_every_sample_question_answers(self):
        for question in self.engine.model.sample_queries:
            with self.subTest(question=question):
                res = self.ask(question)
                self.assertIn(res["type"], ("result", "explain"), question)

    def test_trend_word_with_explicit_grain(self):
        res = self.ask("Monthly sales trend in 2021")
        self.assertEqual(res["query"]["time_grain"], "month")
        self.assertEqual(len(res["rows"]), 12)

    def test_why_answers_add_up(self):
        for question in ("Why did profit change in 2021 vs last year?", "Why did return rate change in 2021 vs last year?",
                         "What drove the change in profit margin in 2021?"):
            with self.subTest(question=question):
                res = self.ask(question)
                self.assertEqual(res["type"], "explain")
                total = res["current"]["value"] - res["previous"]["value"]
                for dim, members in res["drivers"].items():
                    self.assertAlmostEqual(sum(m["contribution"] for m in members), total, places=6, msg=dim)

    def test_return_rate_is_only_explained_by_order_level_groups(self):
        res = self.ask("Why did return rate change in 2021 vs last year?")
        self.assertNotIn("category", res["drivers"])  # an order can span categories, so it wouldn't add up

    def test_wrong_direction_is_corrected(self):
        res = self.ask("What is the reason sales dropped in 2021?")
        self.assertIn("didn't drop", res["sections"][0]["text"])

    def test_reason_as_a_breakdown_is_not_a_why_question(self):
        for question in ("Returned sales by return reason", "Sales by reason for return"):
            self.assertEqual(self.ask(question)["type"], "result", question)

    def test_customers_as_breakdown_and_as_count(self):
        top = self.ask("Top 5 customers by sales in 2021")
        self.assertEqual(top["query"]["dimensions"], ["customer"])
        self.assertEqual(len(top["rows"]), 5)
        count = self.ask("How many customers in 2021?")
        self.assertEqual(count["rows"][0]["customers"],
                         self.sql("SELECT COUNT(DISTINCT customer_id) FROM orders WHERE order_date LIKE '2021%'"))

    def test_city_and_state_with_the_same_name_asks(self):
        res = self.ask("Sales in Washington in 2021")
        self.assertEqual(res["ambiguity"], "value")
        city = next(o for o in res["options"] if "City" in o["label"])
        answered = self.ask("Sales in Washington in 2021", clarifications=city["clarification"])
        self.assertAlmostEqual(answered["rows"][0]["sales"], self.sql(
            "SELECT SUM(sales) FROM orders WHERE city = 'Washington' AND order_date LIKE '2021%'"), places=2)

    def test_vague_words_ask_which_measure(self):
        self.assertEqual(self.ask("How is performance by segment?")["ambiguity"], "metric")
        res = self.ask("returns by region")
        self.assertEqual({o["clarification"]["metric"] for o in res["options"]}, {"return_rate", "returned_sales"})

    def test_years_without_data_point_to_coverage(self):
        res = self.ask("Sales this year")
        self.assertIn("2018-01-03 to 2021-12-30", res["answer"])

    def test_values_with_punctuation(self):
        res = self.engine.query(SemanticQuery(metrics=["sales"], filters=[Filter("segment", "in", ["Home Office"])]))
        self.assertAlmostEqual(res["rows"][0]["sales"], self.sql("SELECT SUM(sales) FROM orders WHERE segment = 'Home Office'"))


class RegistryTests(unittest.TestCase):
    def test_both_demos_register_and_route(self):
        tmp = Path(tempfile.mkdtemp())
        reg = build_registry(Settings(data_dir=tmp, live=False))
        try:
            self.assertEqual(set(reg.engines), {"delivery", "superstore"})
            info = {d["id"]: d for d in reg.listing()}
            self.assertFalse(info["superstore"]["live"])
            self.assertEqual(info["superstore"]["type"], "SQL")
            engines = list(reg.engines.values())
            route = lambda q: router.route(engines, q, None, None, today=TODAY)
            self.assertEqual(route("Profit margin by category")[0].id, "superstore")
            self.assertEqual(route("Return rate by ship mode")[0].id, "superstore")
            self.assertEqual(route("Utilization by practice last month")[0].id, "delivery")
            self.assertEqual(route("Sales by region")[1]["ambiguity"], "dataset")  # both have sales and region
        finally:
            reg.shutdown()
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
