"""Guarded SQL fallback (on-prem model drafts SQL the semantic model can't express) and the "Show steps" trace.

The local model is a fake OpenAI-compatible server on 127.0.0.1, so the real HTTP path is exercised.

Run with:  python3 -m unittest discover -s tests
"""
import datetime as dt
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from bi import sqlguard
from bi.config import Settings
from bi.registry import build_registry

TODAY = dt.date(2026, 11, 7)
ALLOWED = {"orders", "returns"}
SHIP_DAYS = ("SELECT ship_mode, AVG(julianday(ship_date) - julianday(order_date)) AS avg_days "
             "FROM orders GROUP BY ship_mode ORDER BY avg_days")


class FakeLocalModel(HTTPServer):
    """Answers like an OpenAI-compatible endpoint. `sql` is what the next SQL draft returns (None: refuse)."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.sql = SHIP_DAYS
        self.requests = []

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if body["response_format"]["json_schema"]["name"] == "sql_draft":
            sql = self.server.sql
            content = {"answerable": sql is not None, "sql": sql or "", "note": "Average days from order to ship."}
        else:
            content = {"answerable": False, "reason": "Not covered by the semantic model.", "metrics": [],
                       "dimensions": [], "filters": [], "time_range": None, "time_grain": None, "compare": None,
                       "explain": False}
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class GuardTests(unittest.TestCase):
    def test_read_only_selects_pass(self):
        for sql in [
            "SELECT region, SUM(sales) FROM orders GROUP BY region",
            "select o.region, count(*) from orders o left join returns r on o.order_id = r.order_id group by 1;",
            "WITH yearly AS (SELECT substr(order_date, 1, 4) AS y, SUM(sales) AS s FROM orders GROUP BY y) "
            "SELECT * FROM yearly",
            "SELECT * FROM orders WHERE product_name = 'Delete; drop -- me'",
            'SELECT "region" FROM "orders"',
        ]:
            with self.subTest(sql=sql):
                self.assertNotIn(";", sqlguard.check(sql, ALLOWED)[-1:])

    def test_anything_else_is_rejected(self):
        for sql, why in [
            ("", "no SQL"), ("DELETE FROM orders", "only SELECT"), ("UPDATE orders SET sales = 0", "only SELECT"),
            ("SELECT 1; DROP TABLE orders", "one statement"), ("SELECT 1 -- hi", "comments"),
            ("SELECT /* x */ 1", "comments"), ("SELECT * INTO backup FROM orders", "INTO"),
            ("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "DELETE"),
            ("SELECT pg_sleep(10)", "pg_sleep"), ("SELECT load_extension('x')", "load_extension"),
            ("SELECT * FROM snapshot", "snapshot"), ("SELECT * FROM sqlite_master", "sqlite_master"),
            ("SELECT * FROM main.snapshot", "snapshot"), ("SELECT 'open", "not closed"),
            ("SELECT " + "1 + " * 2000 + "1", "too long"), ("PRAGMA table_info(orders)", "only SELECT"),
            ("SELECT set_config('x', 'y', false)", "SET"),
        ]:
            with self.subTest(sql=sql[:40]):
                with self.assertRaises(sqlguard.Rejected) as caught:
                    sqlguard.check(sql, ALLOWED)
                self.assertIn(why.lower(), str(caught.exception).lower())


class FallbackTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.llm = FakeLocalModel()
        threading.Thread(target=cls.llm.serve_forever, daemon=True).start()
        cls.env = patch.dict("os.environ", {"BI_LOCAL_LLM_URL": cls.llm.url, "BI_LOCAL_LLM_MODEL": "fake"})
        cls.env.start()
        cls.tmp = Path(tempfile.mkdtemp())
        cls.reg = build_registry(Settings(data_dir=cls.tmp, live=False))
        cls.engine = cls.reg.engines["superstore"]
        cls.raw = sqlite3.connect(cls.tmp / "superstore.db")

    @classmethod
    def tearDownClass(cls):
        cls.raw.close()
        cls.reg.shutdown()
        cls.env.stop()
        cls.llm.shutdown()
        cls.llm.server_close()
        shutil.rmtree(cls.tmp)

    def setUp(self):
        self.llm.sql = SHIP_DAYS
        self.llm.requests.clear()

    def ask(self, question, **kw):
        return self.engine.ask(question, today=TODAY, **kw)

    def draft(self, question="What's the average shipping delay?"):
        return self.ask(question, clarifications={"draft": True})


class FallbackTests(FallbackTestCase):
    def test_clarifying_question_offers_the_on_prem_model(self):
        res = self.ask("What's the average shipping delay?")
        self.assertEqual(res["type"], "clarify")
        self.assertEqual(res["options"][-1]["clarification"], {"draft": True})
        self.engine.llm.sql_fallback = False
        try:
            res = self.ask("What's the average shipping delay?")
            self.assertNotIn({"draft": True}, [o["clarification"] for o in res["options"]])
        finally:
            self.engine.llm.sql_fallback = True

    def test_drafted_answer_is_marked_unverified_and_correct(self):
        res = self.draft()
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["type"], res["verified"], res["engine"]), ("draft", False, "local-llm-sql"))
        self.assertTrue(res["label"].startswith("Unverified"))
        expected = dict(self.raw.execute(SHIP_DAYS).fetchall())
        self.assertEqual({r["ship_mode"]: r["avg_days"] for r in res["rows"]}, expected)
        self.assertEqual([c["role"] for c in res["columns"]], ["text", "metric"])
        self.assertIn("Passed the safety check", [s["title"] for s in res["steps"]])
        self.assertEqual(res["sql"], [{"sql": SHIP_DAYS, "params": []}])

    def test_model_sees_schema_and_definitions_but_no_rows(self):
        self.draft()
        (body,) = [b for b in self.llm.requests if b["response_format"]["json_schema"]["name"] == "sql_draft"]
        prompt = json.dumps(body)
        self.assertIn("orders: row_id INTEGER", prompt)
        self.assertIn("Profit margin: SUM(o.profit)", prompt)
        self.assertNotIn("snapshot", prompt)
        for (customer,) in self.raw.execute("SELECT DISTINCT customer_name FROM orders LIMIT 50"):
            self.assertNotIn(customer, prompt)

    def test_unknown_question_falls_through_to_a_draft(self):
        res = self.ask("zxqv blorp")  # the rules match nothing, so the on-prem model drafts SQL
        self.assertEqual(res["type"], "draft")
        names = [b["response_format"]["json_schema"]["name"] for b in self.llm.requests]
        self.assertEqual(names[-1], "sql_draft")

    def test_blocked_and_broken_drafts_never_run(self):
        for sql, last in [("SELECT * FROM snapshot", "Blocked the drafted SQL"),
                          ("DELETE FROM orders", "Blocked the drafted SQL"),
                          ("SELECT * FROM orders o, snapshot s", "Blocked the drafted SQL"),  # caught by SQLite itself
                          ("SELECT nosuchcolumn FROM orders", "The drafted SQL failed to run")]:
            with self.subTest(sql=sql):
                self.llm.sql = sql
                res = self.draft()
                self.assertFalse(res["ok"])
                self.assertEqual(res["steps"][-1]["title"], last)
                self.assertEqual(res["sql"][0]["sql"], sql)
        self.assertEqual(self.raw.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 9994)
        # The connection is reused: the guard must leave it working normally afterwards.
        self.assertEqual(self.ask("Sales by region in 2021")["type"], "result")
        self.assertEqual(self.engine.freshness()["coverage"]["from"], "2018-01-03")

    def test_model_refusal_and_row_cap(self):
        self.llm.sql = None
        res = self.draft()
        self.assertFalse(res["ok"])
        self.assertIn("can't answer", res["steps"][-1]["detail"])
        self.llm.sql = "SELECT order_id, sales FROM orders"
        res = self.draft()
        self.assertEqual(len(res["rows"]), sqlguard.MAX_ROWS)
        self.assertTrue(res["truncated"])

    def test_no_local_model_means_no_draft(self):
        with patch.object(type(self.engine.llm), "available", new=False):
            res = self.draft()
        self.assertFalse(res["ok"])
        self.assertEqual(self.llm.requests, [])


class StepTests(FallbackTestCase):
    def test_result_steps(self):
        res = self.reg.ask({"question": "Profit margin by category in 2021", "dataset": "superstore"})
        titles = [s["title"] for s in res["steps"]]
        self.assertEqual(titles, ["Picked a source", "Read the question", "Matched the semantic model", "Compiled SQL",
                                  "Ran it read-only on the SQL database", "Wrote the answer"])
        self.assertIn("no language model", res["steps"][1]["detail"])
        self.assertIn("the source you picked", res["steps"][0]["detail"])

    def test_routed_and_why_steps(self):
        res = self.reg.ask({"question": "Why did return rate change in 2021 vs last year?"})
        self.assertIn("matched to your question automatically", res["steps"][0]["detail"])
        titles = [s["title"] for s in res["steps"]]
        self.assertIn("Compared two periods", titles)
        self.assertIn("Split the change by rate and mix", titles)

    def test_breakdown_edits_have_steps(self):
        res = self.reg.engines["delivery"].query({"metrics": ["revenue"], "dimensions": ["practice"]})
        self.assertEqual(res["steps"][0]["title"], "Matched the semantic model")

    def test_http_draft_clarification(self):
        from fastapi.testclient import TestClient
        from bi.server import create_app
        with TestClient(create_app(self.reg.settings, self.reg)) as client:
            res = client.post("/api/ask", json={"question": "What's the average shipping delay?", "dataset": "superstore",
                                                "clarifications": {"draft": True}}).json()
        self.assertEqual(res["type"], "draft")
        self.assertEqual(res["steps"][0]["title"], "Picked a source")


if __name__ == "__main__":
    unittest.main()
