"""Live PostgreSQL integration. CI runs this against an isolated synthetic PostgreSQL service
(BI_TEST_POSTGRES_URL); locally it starts a private embedded server when `pgserver` is installed.
"""
import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from bi import sqlguard, superstore
from bi.config import ROOT
from bi.engine import Engine
from bi.semantic import SemanticModel, SemanticQuery, TimeRange
from bi.sources import PostgresSource

_server = None


def postgres_url():
    """The CI service, else an embedded server shared by this module, else None (tests skip)."""
    global _server
    if os.environ.get("BI_TEST_POSTGRES_URL"):
        return os.environ["BI_TEST_POSTGRES_URL"]
    try:
        import pgserver
        import psycopg  # noqa: F401
    except ImportError:
        return None
    if _server is None:
        _server = pgserver.get_server(tempfile.mkdtemp(prefix="bi-pg-"), cleanup_mode="delete")
    return _server.get_uri()


def tearDownModule():
    if _server is not None:
        _server.cleanup()


def requires_postgres(cls):
    return unittest.skipUnless(postgres_url(), "Set BI_TEST_POSTGRES_URL or pip install pgserver psycopg[binary]")(cls)


@requires_postgres
class PostgresChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.url = postgres_url()
        cls.admin = psycopg.connect(cls.url, autocommit=True)
        cls.schema = "qc_" + uuid.uuid4().hex
        cls.admin.execute(f"CREATE SCHEMA {cls.schema}")
        cls.admin.execute(f"CREATE TABLE {cls.schema}.facts(date DATE, team TEXT, revenue NUMERIC(18,2))")
        for row in [("2026-08-01","A",10),("2026-08-01","B",100),("2026-09-01","A",200),("2026-09-01","B",1)]:
            cls.admin.execute(f"INSERT INTO {cls.schema}.facts VALUES (%s,%s,%s)",row)
        cls.source = PostgresSource(cls.url)
        cls.engine = Engine("pg", SemanticModel({"name":"test", "base_table": cls.schema+".facts", "base_alias":"t",
            "metrics":[{"name":"revenue","label":"Revenue","sql":"SUM(t.revenue)","format":"currency","additive":True}],
            "dimensions":[{"name":"team","label":"Team","sql":"t.team"}],
            "time_dimension":{"name":"date","label":"Date","sql":"t.date"}}, cls.source.dialect), cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.source.close()
        cls.admin.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        cls.admin.close()

    def test_numeric_comparison(self):
        r = self.engine.query(SemanticQuery(metrics=["revenue"],dimensions=["team"],limit=1,
            time_range=TimeRange("2026-09-01","2026-10-01"),compare="previous_period"))
        self.assertTrue(r["ok"],r)
        self.assertEqual(r["rows"][0]["revenue__prev"],10)
        self.assertIsInstance(r["rows"][0]["revenue"],float)

    def test_readonly_and_statement_timeout(self):
        self.assertEqual(self.source.con.execute("SHOW default_transaction_read_only").fetchone()[0],"on")
        self.assertEqual(self.source.con.execute("SHOW statement_timeout").fetchone()[0],"10s")

    def test_why_and_date_grains(self):
        r = self.engine.query(SemanticQuery(metrics=["revenue"],explain=True,
            time_range=TimeRange("2026-09-01","2026-10-01")))
        self.assertTrue(r["ok"],r)
        self.assertEqual(sum(x["contribution"] for x in r["drivers"]["team"]),91)
        r = self.engine.query(SemanticQuery(metrics=["revenue"],time_grain="week",
            time_range=TimeRange("2026-01-01","2027-01-01")))
        self.assertTrue(r["ok"],r)


class _Stub:
    """Stands in for the on-prem model: returns whatever SQL the test sets."""
    available, sql_fallback, error = True, True, None
    sql = None

    def draft_sql(self, question, schema_text, dialect, today=None):
        self.schema_text, self.dialect = schema_text, dialect
        return self.sql, "Stub draft."


@requires_postgres
class SuperstorePostgresChecks(unittest.TestCase):
    """The Superstore demo on PostgreSQL gives the same answers as on SQLite."""

    @classmethod
    def setUpClass(cls):
        cls.url = postgres_url()
        cls.schema = "qc_store_" + uuid.uuid4().hex[:12]
        superstore.ensure_postgres(cls.url, cls.schema)
        cls.source = PostgresSource(cls.url, search_path=cls.schema)
        cls.source.live = False
        cls.engine = Engine("superstore", SemanticModel.load(ROOT / "semantic" / "superstore.json", cls.source.dialect),
                            cls.source)
        cls.tmp = Path(tempfile.mkdtemp())
        superstore.ensure(cls.tmp / "s.db")
        lite = __import__("bi.sources", fromlist=["SQLiteSource"]).SQLiteSource(cls.tmp / "s.db")
        cls.lite = Engine("lite", SemanticModel.load(ROOT / "semantic" / "superstore.json"), lite)

    @classmethod
    def tearDownClass(cls):
        import psycopg
        cls.source.close()
        cls.lite.source.close()
        with psycopg.connect(cls.url, autocommit=True) as con:
            con.execute(f"DROP SCHEMA {cls.schema} CASCADE")
        shutil.rmtree(cls.tmp)

    def ask(self, engine, question, **kw):
        res = engine.ask(question, today=dt.date(2026, 11, 7), **kw)
        self.assertTrue(res.get("ok"), f"{question}: {res.get('error')}")
        return res

    def test_load_is_complete_and_not_repeated(self):
        con = self.source.con
        self.assertEqual(con.execute("SELECT COUNT(*), COUNT(DISTINCT order_id) FROM orders").fetchone(), (9994, 5009))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM returns").fetchone()[0], 296)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM orders WHERE postal_code IS NULL").fetchone()[0], 11)
        built = con.execute("SELECT built_at FROM snapshot").fetchone()[0]
        superstore.ensure_postgres(self.url, self.schema)
        self.source.close()
        self.assertEqual(self.source.con.execute("SELECT built_at FROM snapshot").fetchone()[0], built)

    def test_every_sample_question_matches_sqlite(self):
        for question in self.engine.model.sample_queries:
            with self.subTest(question=question):
                pg, lite = self.ask(self.engine, question), self.ask(self.lite, question)
                self.assertEqual(pg["type"], lite["type"])
                if pg["type"] == "result":
                    self.assertEqual(len(pg["rows"]), len(lite["rows"]))
                    for a, b in zip(pg["rows"], lite["rows"]):
                        for key, value in b.items():
                            if isinstance(value, float):
                                self.assertAlmostEqual(a[key], value, places=6, msg=key)
                            else:
                                self.assertEqual(a[key], value, key)
                else:
                    self.assertAlmostEqual(pg["current"]["value"], lite["current"]["value"], places=6)

    def test_steps_name_postgres_and_source_is_read_only(self):
        res = self.ask(self.engine, "Profit margin by category in 2021")
        self.assertIn("Ran it read-only on PostgreSQL", [s["title"] for s in res["steps"]])
        import psycopg
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            self.source.con.execute("DELETE FROM returns")

    def test_guarded_fallback_on_postgres(self):
        stub = _Stub()
        self.engine.llm, original = stub, self.engine.llm
        try:
            stub.sql = "SELECT ship_mode, AVG(ship_date - order_date) AS avg_days FROM orders GROUP BY ship_mode"
            res = self.ask(self.engine, "What's the average shipping delay?", clarifications={"draft": True})
            self.assertEqual((res["type"], stub.dialect), ("draft", "postgres"))
            self.assertIn("orders: row_id INTEGER", stub.schema_text)
            self.assertEqual(len(res["rows"]), 4)
            for sql, last in [("SELECT * FROM orders o, snapshot s", "Blocked the drafted SQL"),  # caught by EXPLAIN
                              ("SELECT * FROM pg_catalog.pg_user", "Blocked the drafted SQL"),
                              ("SELECT nosuchcolumn FROM orders", "The drafted SQL failed to run")]:
                with self.subTest(sql=sql):
                    stub.sql = sql
                    res = self.engine.ask("What's the average shipping delay?", clarifications={"draft": True})
                    self.assertFalse(res["ok"])
                    self.assertEqual(res["steps"][-1]["title"], last)
            stub.sql = "SELECT order_id FROM orders"
            res = self.ask(self.engine, "x", clarifications={"draft": True})
            self.assertEqual(len(res["rows"]), sqlguard.MAX_ROWS)
            # A failed draft must leave the connection usable.
            self.assertEqual(self.ask(self.engine, "Sales by region in 2021")["type"], "result")
        finally:
            self.engine.llm = original
