"""Edge cases, checked across every kind of data source.

The same tricky records (tests/edge_data.py) are loaded as a SQLite database, a CSV export and an Excel workbook.
Every question is asked of all three, and each answer must match a total computed straight from the records.

Run with:  python3 -m unittest discover -s tests
"""
import datetime as dt
import atexit
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.pop("BI_LOCAL_LLM_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_data  # noqa: E402
from bi import autosemantic, router  # noqa: E402
from bi.engine import Engine  # noqa: E402
from bi.semantic import Filter, SemanticModel, SemanticQuery, TimeRange, shift_range  # noqa: E402
from bi.sources import FileSource, SQLiteSource  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EDGE = Path(tempfile.mkdtemp(prefix="bi-edge-fixtures-"))
edge_data.OUT = EDGE
atexit.register(shutil.rmtree, EDGE, ignore_errors=True)
TODAY = dt.date(2026, 10, 25)
R = edge_data.RECORDS
INJECTION = "Robert'); DROP TABLE timesheets;--"


# ---- the oracle: answers computed directly from the records ----------------------------------------

def rows(start=None, end=None, **where):
    out = []
    for r in R:
        if start and r["date"] < start or end and r["date"] >= end:
            continue
        if all((r[k] in v) if isinstance(v, (set, list, tuple)) else r[k] == v for k, v in where.items()):
            out.append(r)
    return out


def total(metric, rs):
    if metric == "utilization":
        avail = sum(r["available_hours"] for r in rs)
        return sum(r["billable_hours"] for r in rs) / avail if avail else None
    return sum(r[metric] for r in rs) if rs else None


def by(dim, metric, rs):
    groups = {}
    for r in rs:
        groups.setdefault(r[dim], []).append(r)
    return {k: total(metric, v) for k, v in groups.items()}


SEP, AUG = ("2026-09-01", "2026-10-01"), ("2026-08-01", "2026-09-01")
Y2026 = ("2026-01-01", "2027-01-01")


def file_engine(path: Path, cache: Path, name: str) -> Engine:
    src = FileSource(path, cache)
    (count,) = src.con.execute(f'SELECT COUNT(*) FROM "{src.table}"').fetchone()
    return Engine(name, SemanticModel(autosemantic.infer(src.table, src.columns, name, count)), src)


class EdgeSources(unittest.TestCase):
    """Builds the three sources once. Subclasses run each check against all of them."""

    @classmethod
    def setUpClass(cls):
        edge_data.build_all()  # fixtures always match the generator
        cls.tmp = Path(tempfile.mkdtemp())
        db = edge_data.build_sqlite(cls.tmp / "edge.db")
        cls.sql = Engine("sql", SemanticModel.load(ROOT / "semantic" / "professional_services.json"), SQLiteSource(db))
        for name in ("edge_timesheets.csv", "edge_timesheets.xlsx"):
            shutil.copy(EDGE / name, cls.tmp / name)
        cls.csv = file_engine(cls.tmp / "edge_timesheets.csv", cls.tmp / "cache", "csv")
        cls.xlsx = file_engine(cls.tmp / "edge_timesheets.xlsx", cls.tmp / "cache", "xlsx")
        cls.engines = {"SQL": cls.sql, "CSV": cls.csv, "Excel": cls.xlsx}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def each(self):
        return list(self.engines.items())

    def ask(self, engine, question, **kw):
        res = engine.ask(question, today=TODAY, **kw)
        self.assertTrue(res.get("ok"), f"{engine.id}: {question} -> {res.get('error')}")
        return res

    def query(self, engine, **kw):
        res = engine.query(SemanticQuery(**kw), today=TODAY)
        self.assertTrue(res.get("ok"), f"{engine.id}: {kw} -> {res.get('error')}")
        return res

    def assertNumber(self, actual, expected, places=2, msg=None):
        if expected is None:
            self.assertIsNone(actual, msg)
        else:
            self.assertIsNotNone(actual, msg)
            self.assertAlmostEqual(actual, expected, places=places, msg=msg)


class ParityTests(EdgeSources):
    def test_sources_load_with_the_same_shape(self):
        csv_types = {c["name"]: c["type"] for c in self.csv.source.columns}
        self.assertEqual(csv_types["date"], "date")         # dd/mm/yyyy, including 29/02/2024
        self.assertEqual(csv_types["revenue"], "number")    # "$1,480.00" and "($500.00)"
        for label, e in self.each():
            with self.subTest(source=label):
                for m in ("revenue", "utilization", "billable_hours", "available_hours"):
                    self.assertIn(m, e.model.metrics)
                for d in ("practice", "region", "grade", "client", "consultant"):
                    self.assertIn(d, e.model.dimensions)

    def test_headline_number_and_large_value_formatting(self):
        expected = total("revenue", rows(*SEP))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "What was revenue in September 2026?")
                self.assertNumber(res["rows"][0]["revenue"], expected)
                self.assertIn("$12.35M", res["answer"])

    def test_breakdown_matches_oracle(self):
        expected = by("client", "revenue", rows(*Y2026))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue by client in 2026")
                got = {r["client"]: r["revenue"] for r in res["rows"]}
                self.assertEqual(set(got), set(expected))
                for k, v in expected.items():
                    self.assertNumber(got[k], v, msg=k)
                self.assertNumber(res["totals"]["revenue"], total("revenue", rows(*Y2026)))

    def test_ratio_by_dimension_including_zero_capacity(self):
        expected = by("practice", "utilization", rows(*SEP))
        self.assertEqual(expected["Strategy"], 0.0)  # one bench day + one zero-capacity leave day
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Utilization by practice in September 2026")
                got = {r["practice"]: r["utilization"] for r in res["rows"]}
                for k, v in expected.items():
                    self.assertNumber(got[k], v, places=6, msg=k)

    def test_undefined_ratio_is_empty_not_an_error(self):
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.query(e, metrics=["utilization"], filters=[Filter("consultant", "in", ["Leave Only"])])
                self.assertIsNone(res["rows"][0]["utilization"])  # 0 billable / 0 available
                self.assertIn("couldn't find any data", res["answer"])

    def test_top_n_ranking(self):
        expected = sorted(by("client", "revenue", rows(*Y2026)).items(), key=lambda x: -x[1])[:2]
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Top 2 clients by revenue in 2026")
                self.assertEqual([r["client"] for r in res["rows"]], [k for k, _ in expected])

    def test_monthly_and_quarterly_trends_skip_empty_months(self):
        monthly = {}
        for r in rows(*Y2026):
            monthly[r["date"][:7]] = monthly.get(r["date"][:7], 0) + r["revenue"]
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.query(e, metrics=["revenue"], time_grain="month", time_range=TimeRange(*Y2026, "2026"))
                got = {r["period"]: r["revenue"] for r in res["rows"]}
                self.assertEqual(set(got), set(monthly))  # no June: it has no data
                for k, v in monthly.items():
                    self.assertNumber(got[k], v, msg=k)
                q = self.query(e, metrics=["revenue"], time_grain="quarter", time_range=TimeRange(*Y2026, "2026"))
                self.assertEqual([r["period"] for r in q["rows"]], ["2026-Q3", "2026-Q4"])

    def test_month_with_no_data_says_so_and_shows_coverage(self):
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue in June 2026")
                self.assertIn("couldn't find any data", res["answer"])
                self.assertIn("2024-02-29 to 2027-01-15", res["answer"])

    def test_future_rows_only_count_when_asked_for(self):
        for label, e in self.each():
            with self.subTest(source=label):
                self.assertNumber(self.ask(e, "Revenue in 2027")["rows"][0]["revenue"], 1480.0)
                ytd = self.ask(e, "Revenue this year")["rows"][0]["revenue"]
                self.assertNumber(ytd, total("revenue", rows("2026-01-01", "2026-10-26")))

    def test_leap_day_previous_year_comparison(self):
        tr = shift_range(TimeRange("2024-02-29", "2024-03-01"), "previous_year")
        self.assertEqual((tr.start, tr.end), ("2023-02-28", "2023-03-01"))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue in February 2025 vs last year")
                row = res["rows"][0]
                self.assertNumber(row["revenue"], 1110.0)
                self.assertNumber(row["revenue__prev"], 1480.0)  # includes 29 Feb 2024
                self.assertNumber(row["revenue__change"], (1110 - 1480) / 1480, places=6)

    def test_negative_revenue_is_netted_and_formatted(self):
        expected = total("revenue", rows(*SEP, practice="R/D Lab"))
        self.assertEqual(expected, 940.0)
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue for R/D Lab in September 2026")
                self.assertNumber(res["rows"][0]["revenue"], expected)
                self.assertIn("$940.00", res["answer"])
                neg = self.query(e, metrics=["revenue"], filters=[Filter("client", "in", [INJECTION])],
                                 time_range=TimeRange("2026-09-30", "2026-10-01"))
                self.assertNumber(neg["rows"][0]["revenue"], -500.0)
                self.assertIn("-$500.00", neg["answer"])

    def test_values_with_punctuation_and_unicode(self):
        cases = {
            "Revenue for Data & AI in 2026": total("revenue", rows(*Y2026, practice="Data & AI")),
            "Revenue for Zoë Müller in 2026": total("revenue", rows(*Y2026, consultant="Zoë Müller")),
            "Revenue for 李 伟 in 2026": total("revenue", rows(*Y2026, consultant="李 伟")),
            "Revenue for Ünïcödé GmbH in 2026": total("revenue", rows(*Y2026, client="Ünïcödé GmbH")),
            "Revenue for Seán O'Brien in 2026": total("revenue", rows(*Y2026, consultant="Seán O'Brien")),
            "Revenue for Smith, Jane in 2026": total("revenue", rows(*Y2026, consultant="Smith, Jane")),
        }
        for label, e in self.each():
            with self.subTest(source=label):
                for question, expected in cases.items():
                    res = self.ask(e, question)
                    self.assertEqual(res.get("type"), "result", question)
                    self.assertNumber(res["rows"][0]["revenue"], expected, msg=question)

    def test_negation_filter(self):
        expected = total("revenue", rows(*Y2026, region={"Europe", "APAC"}))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue excluding India in 2026")
                self.assertNumber(res["rows"][0]["revenue"], expected)

    def test_same_word_in_two_dimensions_asks_which(self):
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Revenue for Cloud in 2026")
                self.assertEqual((res["type"], res["ambiguity"]), ("clarify", "value"))
                dims = {o["clarification"]["values"][0]["dimension"] for o in res["options"]}
                self.assertEqual(dims, {"practice", "client"})
                client = next(o for o in res["options"] if o["clarification"]["values"][0]["dimension"] == "client")
                answered = self.ask(e, "Revenue for Cloud in 2026", clarifications=client["clarification"])
                self.assertNumber(answered["rows"][0]["revenue"], total("revenue", rows(*Y2026, client="Cloud")))

    def test_injection_shaped_value_is_just_data(self):
        expected = total("revenue", rows(client=INJECTION))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.query(e, metrics=["revenue"], filters=[Filter("client", "in", [INJECTION])])
                self.assertNumber(res["rows"][0]["revenue"], expected)
        con = sqlite3.connect(self.sql.source.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM timesheets").fetchone()[0], len(R))
        con.close()

    def test_why_answer_matches_oracle_and_adds_up(self):
        cur, prev = total("utilization", rows(*SEP)), total("utilization", rows(*AUG))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "Why did utilization change in September 2026?")
                self.assertEqual(res["type"], "explain")
                self.assertNumber(res["current"]["value"], cur, places=6)
                self.assertNumber(res["previous"]["value"], prev, places=6)
                self.assertEqual(res["decomposition"], "rate_mix")
                for dim, members in res["drivers"].items():
                    self.assertAlmostEqual(sum(m["contribution"] for m in members), cur - prev, places=9, msg=dim)
                self.assertIn("fell", res["sections"][0]["text"])

    def test_why_on_additive_metric_with_negative_and_huge_values(self):
        cur, prev = total("revenue", rows(*SEP)), total("revenue", rows(*AUG))
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.ask(e, "What drove the change in revenue in September 2026?")
                for dim, members in res["drivers"].items():
                    self.assertAlmostEqual(sum(m["contribution"] for m in members), cur - prev, places=2, msg=dim)

    def test_follow_up_and_drill_keep_working(self):
        for label, e in self.each():
            with self.subTest(source=label):
                first = self.ask(e, "Revenue by region in 2026")
                follow = self.ask(e, "now by grade", context=first["query"])
                self.assertEqual(follow["query"]["dimensions"], ["grade"])
                self.assertEqual(follow["query"]["time_range"]["label"], "2026")


class RobustnessTests(EdgeSources):
    def test_nonsense_and_punctuation_are_declined_politely(self):
        for label, e in self.each():
            with self.subTest(source=label):
                for q in ("????", "tell me a joke", "!!!", "   "):
                    res = e.ask(q, today=TODAY)
                    self.assertFalse(res["ok"], q)
                    self.assertTrue(res["error"])

    def test_very_long_question_does_not_break(self):
        q = "revenue by client in 2026 " + "please " * 400
        for label, e in self.each():
            with self.subTest(source=label):
                self.assertTrue(e.ask(q, today=TODAY)["ok"])

    def test_huge_limit_is_capped(self):
        for label, e in self.each():
            with self.subTest(source=label):
                res = self.query(e, metrics=["revenue"], dimensions=["client"], limit=10 ** 9)
                self.assertEqual(res["query"]["limit"], 1000)

    def test_empty_and_reversed_ranges_are_rejected(self):
        for label, e in self.each():
            with self.subTest(source=label):
                for tr in (TimeRange("2026-09-01", "2026-09-01", "empty"), TimeRange("2026-10-01", "2026-09-01", "reversed")):
                    res = e.query(SemanticQuery(metrics=["revenue"], time_range=tr))
                    self.assertFalse(res["ok"])
                    self.assertIn("before", res["error"])
                res = self.query(e, metrics=["revenue"], time_range=TimeRange("1999-01-01", "2000-01-01", "1999"))
                self.assertIsNone(res["rows"][0]["revenue"])

    def test_invalid_structured_queries_are_rejected_cleanly(self):
        bad = [dict(metrics=["salary"]), dict(metrics=["revenue"], dimensions=["nope"]),
               dict(metrics=["revenue"], filters=[Filter("region", "like", ["%"])]),
               dict(metrics=["revenue"], time_range=TimeRange("2026-13-01", "2026-14-01")),
               dict(metrics=["revenue"], time_grain="fortnight")]
        for label, e in self.each():
            with self.subTest(source=label):
                for kw in bad:
                    res = e.query(SemanticQuery(**kw), today=TODAY)
                    self.assertFalse(res["ok"], kw)

    def test_routing_between_sql_and_file_sources(self):
        engines = [self.sql, self.csv]
        _, clarify = router.route(engines, "Revenue by client in 2026", None, None, today=TODAY)
        self.assertEqual(clarify["ambiguity"], "dataset")  # both sources have revenue
        self.assertEqual(router.route(engines, "Average bill rate by grade", None, None, today=TODAY)[0].id, "sql")
        self.assertEqual(router.route(engines, "How many records are there?", None, None, today=TODAY)[0].id, "csv")


class MessyFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        edge_data.build_all()
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def load(self, name):
        shutil.copy(EDGE / name, self.tmp / name)
        return file_engine(self.tmp / name, self.tmp / "cache", name)

    def test_semicolon_european_csv(self):
        e = self.load("messy_semicolon.csv")
        cols = {c["name"]: c for c in e.source.columns}
        self.assertEqual(cols["order_date"]["type"], "date")        # mixed ISO and dd/mm/yyyy
        self.assertEqual(cols["sales"]["type"], "number")           # €1.234,50 and (€120,00)
        self.assertIn("region_2", cols)                             # duplicate header
        self.assertNotIn("empty_col", e.model.dimensions)           # all-blank column ignored
        self.assertNotIn("order_id", e.model.metrics)                # an ID is counted, not summed
        self.assertIn("distinct_order_id", e.model.metrics)
        sales = e.query(SemanticQuery(metrics=["sales"]))["rows"][0]["sales"]
        self.assertAlmostEqual(sales, 1234.5 + 980 - 120 + 2000 + 0)
        self.assertEqual(e.model.metrics["discount"]["format"], "percent")  # "10%" values are averaged
        disc = e.query(SemanticQuery(metrics=["discount"]))["rows"][0]["discount"]
        self.assertAlmostEqual(disc, (0.10 + 0 + 0.05 + 0.125) / 4)
        by_returned = e.query(SemanticQuery(metrics=["sales"], dimensions=["returned"]))
        self.assertEqual({r["returned"] for r in by_returned["rows"]}, {"Yes", "No"})  # true/TRUE/false/FALSE
        by_keyword = e.query(SemanticQuery(metrics=["sales"], dimensions=["select"]))  # SQL keyword as a column
        self.assertTrue(by_keyword["ok"], by_keyword.get("error"))
        self.assertEqual({r["select"] for r in by_keyword["rows"]}, {"A", "B", "C"})
        regions = {r["region"] for r in e.query(SemanticQuery(metrics=["sales"], dimensions=["region"]))["rows"]}
        self.assertIn("West", regions)                               # "  West  " is trimmed

    def test_tab_separated(self):
        e = self.load("team_hours.tsv")
        res = e.query(SemanticQuery(metrics=["hours"], dimensions=["team"]))
        self.assertEqual({r["team"]: r["hours"] for r in res["rows"]}, {"Alpha": 215.5, "Beta": 40.0})

    def test_single_row(self):
        e = self.load("single_row.csv")
        self.assertEqual(e.query(SemanticQuery(metrics=["revenue"]))["rows"][0]["revenue"], 1000.0)

    def test_header_only_and_empty_files_are_rejected_with_a_reason(self):
        with self.assertRaisesRegex(ValueError, "no data rows"):
            self.load("header_only.csv")
        with self.assertRaisesRegex(ValueError, "no header row"):
            self.load("empty.csv")

    def test_excel_sparse_cells_formulas_booleans_and_sheet_choice(self):
        e = self.load("sparse_formulas.xlsx")
        cols = {c["name"]: c["type"] for c in e.source.columns}
        self.assertEqual(cols["active"], "text")                     # booleans become Yes/No
        self.assertEqual(e.query(SemanticQuery(metrics=["hours"]))["rows"][0]["hours"], 15.0)
        self.assertEqual(e.query(SemanticQuery(metrics=["revenue"]))["rows"][0]["revenue"], 1000.0)  # cached values
        teams = {r["team"] for r in e.query(SemanticQuery(metrics=["row_count"], dimensions=["team"]))["rows"]}
        self.assertEqual(teams, {"Alpha", "Beta", "Gamma"})          # inline string read
        workbook = self.load("edge_timesheets.xlsx")                 # has a small "Read me" sheet first
        self.assertEqual(workbook.query(SemanticQuery(metrics=["row_count"]))["rows"][0]["row_count"], len(R))


class HttpEdgeTests(unittest.TestCase):
    """ASGI transport: bad requests, bad uploads, path tricks."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from bi.config import Settings
        from bi.registry import Registry
        from bi.server import create_app
        edge_data.build_all()
        cls.tmp = Path(tempfile.mkdtemp())
        settings = Settings(data_dir=cls.tmp, demo=False, live=False)
        reg = Registry(settings)
        reg.log_path = None
        db = edge_data.build_sqlite(cls.tmp / "edge.db")
        reg.add(Engine("sql", SemanticModel.load(ROOT / "semantic" / "professional_services.json"), SQLiteSource(db)))
        cls.client = TestClient(create_app(settings, reg))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.tmp)

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if path == "/api/upload":
            headers.setdefault("X-Data-Classification", "synthetic")
        resp = self.client.request(method, path, content=body, headers=headers)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.content

    def test_bad_json_and_empty_question(self):
        self.assertEqual(self.request("POST", "/api/ask", b"{not json", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/ask", json.dumps({"question": "   "}))[0], 422)

    def test_oversized_request_body_is_refused(self):
        status, _ = self.request("POST", "/api/ask", json.dumps({"question": "x" * 70000}))
        self.assertEqual(status, 413)

    def test_long_question_is_rejected(self):
        status, res = self.request("POST", "/api/ask", json.dumps({"question": "revenue in 2026 " + "a" * 2000}))
        self.assertEqual(status, 422)
        self.assertFalse(res["ok"])

    def test_upload_rejections(self):
        cases = [("evil.exe", b"MZ"), ("empty.csv", b""), ("header_only.csv", edge_data.HEADER_ONLY.encode())]
        for name, body in cases:
            with self.subTest(file=name):
                status, res = self.request("POST", "/api/upload", body, {"X-Filename": name})
                self.assertEqual(status, 400)
                self.assertFalse(res["ok"])

    def test_upload_path_traversal_stays_in_uploads(self):
        status, res = self.request("POST", "/api/upload", edge_data.SINGLE_ROW.encode(), {"X-Filename": "../../evil.csv"})
        self.assertEqual(status, 200, res)
        self.assertTrue((self.tmp / "uploads" / "evil.csv").exists())
        self.assertFalse((self.tmp.parent / "evil.csv").exists())

    def test_static_path_traversal_is_blocked(self):
        status, _ = self.request("GET", "/../bi/server.py")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/%2e%2e/bi/server.py")
        self.assertEqual(status, 404)

    def test_unknown_dataset_is_rejected(self):
        status, res = self.request("POST", "/api/query", json.dumps({"dataset": "nope", "query": {"metrics": ["revenue"]}}))
        self.assertEqual(status, 404)
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
