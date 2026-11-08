"""QC regressions: independent small oracles, input contracts and source isolation."""
import copy
import datetime as dt
import http.client
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bi import autosemantic, llm, server, xlsx
from bi.config import Settings
from bi.registry import Registry
from fastapi.testclient import TestClient
from bi.engine import Engine
from bi.semantic import QueryError, SemanticModel, SemanticQuery, TimeRange, shift_range
from bi.sources import FileSource, SQLiteSource, _parse_number, profile

TODAY = dt.date(2026, 10, 29)
SPEC = {"name": "facts", "label": "Test", "base_table": "facts", "base_alias": "t", "joins": {},
        "time_dimension": {"name": "date", "label": "Date", "sql": "t.date"},
        "metrics": [{"name": "revenue", "label": "Revenue", "sql": "SUM(t.revenue)", "format": "currency", "additive": True},
                    {"name": "rate", "label": "Rate", "sql": "AVG(t.revenue)", "format": "percent", "additive": False}],
        "dimensions": [{"name": "team", "label": "Team", "sql": "t.team", "synonyms": ["teams"]}],
        "sample_queries": []}


class AnswerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "facts.db"
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE facts(date TEXT,team TEXT,revenue REAL)")
        con.executemany("INSERT INTO facts VALUES (?,?,?)", [
            ("2026-08-01", "A", 10), ("2026-08-01", "B", 100),
            ("2026-09-01", "A", 200), ("2026-09-01", "B", 1)])
        con.commit()
        con.close()
        self.source = SQLiteSource(self.db)
        self.engine = Engine("sql", SemanticModel(copy.deepcopy(SPEC)), self.source)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.source.close)

    def test_top_one_keeps_prior_value_even_when_rank_changed(self):
        q = SemanticQuery(metrics=["revenue"], dimensions=["team"], limit=1,
                          time_range=TimeRange("2026-09-01", "2026-10-01"), compare="previous_period")
        result = self.engine.query(q)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows"][0]["revenue__prev"], 10)
        self.assertEqual(result["rows"][0]["revenue__change"], 19)
        self.assertEqual(result["totals"]["revenue__prev"], 110)

    def test_top_comparison_parity_with_csv_and_excel(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        import edge_data
        rs = [["2026-08-01", "A", 10], ["2026-08-01", "B", 100], ["2026-09-01", "A", 200], ["2026-09-01", "B", 1]]
        csv = self.root / "export.csv"
        csv.write_text("Date,Team,Revenue\n" + "\n".join(",".join(map(str,r)) for r in rs))
        excel = self.root / "export.xlsx"
        edge_data.write_xlsx(excel, [("Data", ["Date", "Team", "Revenue"], rs)])
        for path in [csv, excel]:
            with self.subTest(source=path.suffix):
                src = FileSource(path, self.root / "cache")
                self.addCleanup(src.close)
                e = Engine("file", SemanticModel(autosemantic.infer(src.table, src.columns, "Export", 4)), src)
                r = e.query(SemanticQuery(metrics=["revenue"], dimensions=["team"], limit=1,
                    time_range=TimeRange("2026-09-01", "2026-10-01"), compare="previous_period"))
                self.assertEqual(r["rows"][0]["revenue__prev"], 10)

    def test_unsupported_numeric_condition_asks(self):
        r = self.engine.ask("revenue greater than 15", today=TODAY)
        self.assertEqual((r["type"], r["ambiguity"]), ("clarify", "constraint"))
        self.assertNotIn("rows", r)

    def test_unknown_filter_value_asks(self):
        self.assertEqual(self.engine.ask("revenue for team ZZZ", today=TODAY)["type"], "clarify")

    def test_huge_date_and_malformed_context_return_errors(self):
        self.assertFalse(self.engine.ask("revenue last 999999999 years", today=TODAY)["ok"])
        self.assertFalse(self.engine.ask("revenue", context={"filters":[{}]})["ok"])

    def test_invalid_query_shapes_never_raise(self):
        for q in [[], None, {"metrics":["revenue"], "filters":[{}]},
                  {"metrics":["revenue"], "time_range":"bad"}, {"metrics":[["revenue"]]},
                  {"metrics":["revenue"], "explain":"false"}, {"metrics":["revenue"], "limit":True}]:
            with self.subTest(q=q):
                self.assertFalse(self.engine.query(q)["ok"])

    def test_nonadditive_explanation_is_refused(self):
        r = self.engine.query(SemanticQuery(metrics=["rate"], explain=True,
            time_range=TimeRange("2026-09-01", "2026-10-01")))
        self.assertFalse(r["ok"])
        self.assertIn("decomposition", r["error"])

    def test_zero_baseline_does_not_mean_flat(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE facts SET revenue=0 WHERE date < '2026-09-01'")
        con.commit(); con.close()
        r = self.engine.query(SemanticQuery(metrics=["revenue"], explain=True,
            time_range=TimeRange("2026-09-01", "2026-10-01")))
        self.assertTrue(r["ok"], r)
        self.assertNotIn("flat", r["answer"])
        self.assertIn("from zero", r["answer"])

    def test_other_group_preserves_all_contributions(self):
        con = sqlite3.connect(self.db); con.execute("DELETE FROM facts")
        con.executemany("INSERT INTO facts VALUES (?,?,?)", [(date, str(i), value) for i in range(61)
            for date, value in [("2026-08-01",1),("2026-09-01",2)]])
        con.commit(); con.close()
        r = self.engine.query(SemanticQuery(metrics=["revenue"], explain=True,
            time_range=TimeRange("2026-09-01", "2026-10-01")))
        self.assertEqual(sum(x["contribution"] for x in r["drivers"]["team"]), 61)
        self.assertEqual(r["drivers"]["team"][-1]["member"], "Other groups")

    def test_too_many_drivers_declined_instead_of_incorrect_sum(self):
        con = sqlite3.connect(self.db); con.execute("DELETE FROM facts")
        con.executemany("INSERT INTO facts VALUES (?,?,?)", [("2026-09-01", str(i), i) for i in range(1001)])
        con.commit(); con.close()
        r = self.engine.query(SemanticQuery(metrics=["revenue"], explain=True,
            time_range=TimeRange("2026-09-01", "2026-10-01")))
        self.assertFalse(r["ok"])
        self.assertIn("Too many groups", r["error"])

    def test_snapshot_does_not_mix_concurrent_changes(self):
        with self.source.snapshot() as con:
            before = con.execute("SELECT SUM(revenue) FROM facts").fetchone()[0]
            writer = sqlite3.connect(self.db)
            writer.execute("UPDATE facts SET revenue=999"); writer.commit(); writer.close()
            self.assertEqual(before, con.execute("SELECT SUM(revenue) FROM facts").fetchone()[0])


class ImportChecks(unittest.TestCase):
    def test_number_formats_and_nonfinite(self):
        for val, expected in [("1,234",1234), ("$1,234",1234), ("€1.234,50",1234.5),
                              ("€980,00",980), ("($500.00)",-500), ("12.5%",.125)]:
            with self.subTest(val=val): self.assertEqual(_parse_number(val),expected)
        for val in ["NaN","Infinity",float("inf")]:
            with self.assertRaises(ValueError): _parse_number(val)

    def test_bad_cell_after_sample_is_not_silently_lost(self):
        with self.assertRaisesRegex(ValueError,"invalid"):
            profile(["Revenue"], [["10"]]*2000 + [["bad"]])

    def test_rates_prices_and_currency(self):
        cols = profile(["Bill Rate","Unit Price","Revenue"], [["100","50","€200,00"],["200","100","€300,00"]])
        metrics = {m["name"]:m for m in autosemantic.infer("f",cols,"File",2)["metrics"]}
        self.assertEqual(metrics["bill_rate"]["format"],"currency")
        self.assertIn("AVG",metrics["unit_price"]["sql"])
        self.assertEqual(metrics["revenue"]["format"],"currency_EUR")

    def test_same_name_timestamp_cache_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); sources=[]
            try:
                for directory,value in [("one",10),("two",99)]:
                    p=root/directory/"report.csv";p.parent.mkdir();p.write_text(f"Team,Revenue\nA,{value}\n")
                    os.utime(p,(1700000000,1700000000)); sources.append(FileSource(p,root/"cache"))
                self.assertNotEqual(sources[0]._cache_path,sources[1]._cache_path)
                self.assertEqual(sources[0].con.execute("SELECT revenue FROM report").fetchone()[0],10)
            finally:
                for s in sources:s.close()

    def test_schema_change_keeps_last_good_cache(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);p=root/"r.csv";p.write_text("Team,Revenue\nA,10\n")
            src=FileSource(p,root/"cache");old=src._cache_path
            p.write_text("Team,Sales\nA,99\n");os.utime(p,(1700000001,1700000001))
            with self.assertRaisesRegex(ValueError,"schema changed"):src.version()
            self.assertEqual(src._cache_path,old)
            src.close()

    def test_model_validation_duplicate_names_and_cycles(self):
        spec=copy.deepcopy(SPEC);spec["metrics"].append(spec["metrics"][0])
        with self.assertRaises(QueryError): SemanticModel(spec)
        spec=copy.deepcopy(SPEC);spec["joins"]={"a":{"requires":"b"},"b":{"requires":"a"}}
        with self.assertRaises(QueryError): SemanticModel(spec)

    def test_month_to_date_never_overlaps_current_period(self):
        r=shift_range(TimeRange("2026-03-01","2026-03-31","Month to date"),"previous_period")
        self.assertEqual((r.start,r.end),("2026-02-01","2026-03-01"))

    def test_week_boundary_uses_iso_year(self):
        from bi.semantic import DIALECTS
        con=sqlite3.connect(":memory:")
        for date,expected in [("2021-01-01","2020-W53"),("2021-01-04","2021-W01")]:
            self.assertEqual(con.execute("SELECT "+DIALECTS["sqlite"].grain("'"+date+"'","week")).fetchone()[0],expected)
        con.close()

    def test_excel_expansion_and_sparse_column_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"bomb.xlsx"
            with zipfile.ZipFile(p,"w",zipfile.ZIP_DEFLATED) as z:z.writestr("large.xml","0"*(2*1024*1024))
            with self.assertRaisesRegex(ValueError,"compression"): xlsx.read(p)
        with self.assertRaises(ValueError): xlsx._col_index("ZZZ1")

    def test_private_llm_transport_pins_ip_and_rejects_link_local(self):
        with patch("socket.getaddrinfo",return_value=[(socket.AF_INET,0,0,"",("169.254.169.254",80))]):
            self.assertFalse(llm.is_on_premises("http://model"))
        with patch("socket.getaddrinfo",return_value=[(socket.AF_INET,0,0,"",("10.0.0.2",80))]),patch("socket.create_connection") as connect:
            llm._PinnedHTTP("model",8000).connect()
            self.assertEqual(connect.call_args[0][0],("10.0.0.2",8000))
        with self.assertRaises(ValueError):llm._NoRedirect().redirect_request(None,None,None,None,None,None)


class HttpChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        settings=Settings(data_dir=cls.root,demo=False,live=False)
        cls.reg=Registry(settings);cls.reg.log_path=None
        p=cls.root/"delivery.csv";p.write_text("Date,Team,Revenue\n2026-09-01,A,10\n")
        cls.original=cls.reg.add_file(p)
        cls.reg.engines["delivery"]=cls.reg.engines.pop(cls.original.id);cls.original.id="delivery"
        cls.client=TestClient(server.create_app(settings,cls.reg));cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None,None,None)
        for e in cls.reg.engines.values():e.source.close()
        cls.tmp.cleanup()

    def request(self,path,body,headers=None):
        headers=dict(headers or {})
        if path=="/api/upload":headers.setdefault("X-Data-Classification","synthetic")
        response=self.client.post(path,content=body,headers=headers)
        return response.status_code,response.json()

    def test_wrong_shapes_and_missing_dataset(self):
        for body in ["[]","null",'{"question":"revenue","context":{"filters":[{}]}}']:
            status,r=self.request("/api/ask",body)
            self.assertEqual(status,422);self.assertFalse(r["ok"])
        self.assertEqual(self.request("/api/query",json.dumps({"dataset":"missing","query":{"metrics":["revenue"]}}))[0],404)

    def test_origin_and_host_are_checked(self):
        self.assertEqual(self.request("/api/ask",'{"question":"revenue"}',{"Origin":"https://other.example"})[0],403)
        self.assertEqual(self.request("/api/ask",'{"question":"revenue"}',{"Host":"other.example"})[0],403)

    def test_bad_replacement_keeps_previous_file(self):
        payload=b"Date,Team,Revenue\n2026-09-01,A,20\n"
        status,res=self.request("/api/upload",payload,{"X-Filename":"safe.csv"})
        self.assertEqual(status,200,res)
        before=(self.root/"uploads/safe.csv").read_bytes()
        status,_=self.request("/api/upload",b"Only headers\n",{"X-Filename":"safe.csv"})
        self.assertEqual(status,400)
        self.assertEqual((self.root/"uploads/safe.csv").read_bytes(),before)
        status,_=self.request("/api/upload",payload,{"X-Filename":"delivery.csv"})
        self.assertEqual(status,200)
        self.assertIs(self.reg.get("delivery"),self.original)


class AdditionalBoundaryChecks(unittest.TestCase):
    def test_unknown_date_field_is_not_ignored(self):
        spec=copy.deepcopy(SPEC);spec.pop("time_dimension")
        with self.assertRaisesRegex(QueryError,"no date field"):
            SemanticModel(spec).validate(SemanticQuery(metrics=["revenue"],time_range=TimeRange("2026-09-01","2026-10-01")))

    def test_same_stem_extensions_have_separate_sidecars(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parent))
        import edge_data
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            a=root/"r.csv";a.write_text("Team,Revenue\nA,10\n")
            b=root/"r.xlsx";edge_data.write_xlsx(b,[("Data",["Team","Hours"],[["B",20]])])
            reg=Registry(Settings(data_dir=root,demo=False,live=False));reg.log_path=None
            first=reg.add_file(a);second=reg.add_file(b)
            self.assertNotEqual(first.id,second.id)
            self.assertTrue((root/"r.csv.semantic.json").exists())
            self.assertTrue((root/"r.xlsx.semantic.json").exists())
            self.assertIn("hours",second.model.metrics)
            first.source.close();second.source.close()

    def test_default_logs_hide_questions_filters_and_have_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            e=object.__new__(Engine);e.log_path=Path(folder)/"questions.jsonl";e.id="test"
            with patch.dict(os.environ,{"BI_LOG_QUESTIONS":"0"}):
                e._log({"question":"sensitive question","query":{"filters":["secret"]},"ok":True})
            text=e.log_path.read_text()
            self.assertNotIn("sensitive",text);self.assertNotIn("secret",text)
            self.assertEqual(e.log_path.stat().st_mode & 0o777,0o600)

    def test_excel_1904_epoch_and_percentage_style(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parent))
        import edge_data
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/"book.xlsx"
            edge_data.write_xlsx(p,[("Data",["Date","Share"],[[1,.5]])])
            with zipfile.ZipFile(p) as z: parts={n:z.read(n) for n in z.namelist()}
            wb=parts["xl/workbook.xml"].decode().replace("<sheets>",'<workbookPr date1904="1"/><sheets>')
            parts["xl/workbook.xml"]=wb.encode()
            parts["xl/styles.xml"]=b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="9"/></cellXfs></styleSheet>'
            key=next(n for n in parts if n.startswith("xl/worksheets/"))
            sheet=parts[key].decode().replace('<c r="A2">','<c r="A2" s="1">').replace('<c r="B2">','<c r="B2" s="2">')
            parts[key]=sheet.encode()
            with zipfile.ZipFile(p,"w") as z:
                for n,v in parts.items():z.writestr(n,v)
            headers,rows=xlsx.read(p)
            self.assertEqual(rows[0][0],"1904-01-02")
            self.assertEqual(rows[0][1],"50.0%")

    def test_file_snapshot_pins_version_until_answer_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/"facts.csv";p.write_text("Team,Revenue\nA,10\n")
            src=FileSource(p,root/"cache")
            try:
                with src.snapshot() as con:
                    self.assertEqual(con.execute("SELECT revenue FROM facts").fetchone()[0],10)
                    p.write_text("Team,Revenue\nA,99\n");os.utime(p,(1700000001,1700000001))
                    with src.snapshot() as nested:
                        self.assertIs(nested,con)
                        self.assertEqual(src.con.execute("SELECT revenue FROM facts").fetchone()[0],10)
                self.assertEqual(src.con.execute("SELECT revenue FROM facts").fetchone()[0],99)
            finally:src.close()

    def test_numeric_identifiers_keep_leading_zeros(self):
        column=profile(["Employee ID"],[["001"],["002"]])[0]
        self.assertEqual(column["type"],"text")
        self.assertEqual(column["convert"]("001"),"001")
