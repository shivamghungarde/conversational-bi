"""Optional live integration. CI runs this with an isolated synthetic PostgreSQL service."""
import copy
import os
import unittest
import uuid

from bi.engine import Engine
from bi.semantic import SemanticModel, SemanticQuery, TimeRange
from bi.sources import PostgresSource


@unittest.skipUnless(os.environ.get("BI_TEST_POSTGRES_URL"), "Set BI_TEST_POSTGRES_URL for the PostgreSQL integration")
class PostgresChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.admin = psycopg.connect(os.environ["BI_TEST_POSTGRES_URL"], autocommit=True)
        cls.schema = "qc_" + uuid.uuid4().hex
        cls.admin.execute(f"CREATE SCHEMA {cls.schema}")
        cls.admin.execute(f"CREATE TABLE {cls.schema}.facts(date DATE, team TEXT, revenue NUMERIC(18,2))")
        for row in [("2026-08-01","A",10),("2026-08-01","B",100),("2026-09-01","A",200),("2026-09-01","B",1)]:
            cls.admin.execute(f"INSERT INTO {cls.schema}.facts VALUES (%s,%s,%s)",row)
        cls.source = PostgresSource(os.environ["BI_TEST_POSTGRES_URL"])
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
