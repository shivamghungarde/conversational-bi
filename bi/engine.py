"""The agent: question -> interpretation -> governed query -> answer + result set."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import narrate
from .llm import ClaudeInterpreter
from .parser import RuleParser, default_trend_range
from .semantic import QueryError, SemanticModel, SemanticQuery, TimeRange, run

CONFIDENT = 0.5


class Engine:
    def __init__(self, model_path: Path, db_path: Path, log_path: Path | None = None):
        self.model = SemanticModel.load(model_path)
        self.db_path = db_path
        self.log_path = log_path
        self._local = threading.local()
        self.dimension_values = self._load_dimension_values()
        self.rules = RuleParser(self.model, self.dimension_values)
        self.llm = ClaudeInterpreter(self.model, self.dimension_values)
        self._log_lock = threading.Lock()

    @property
    def con(self) -> sqlite3.Connection:
        # One read-only connection per server thread.
        if not hasattr(self._local, "con"):
            self._local.con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        return self._local.con

    def _load_dimension_values(self) -> dict:
        values = {}
        for name, d in self.model.dimensions.items():
            sql = f"SELECT DISTINCT {d['sql']} FROM {self.model.spec['base_table']} {self.model.spec['base_alias']}"
            if d.get("join"):
                j = self.model.joins[d["join"]]
                sql += f" JOIN {j['table']} {j['alias']} ON {j['on']}"
            values[name] = sorted(v for (v,) in self.con.execute(sql) if v is not None)
        return values

    def catalog(self) -> dict:
        cat = self.model.catalog()
        for d in cat["dimensions"]:
            d["values"] = self.dimension_values.get(d["name"], [])
        cat["llm_enabled"] = self.llm.available
        return cat

    # ---- entry points ------------------------------------------------------

    def ask(self, question: str, context: dict | None = None, today: dt.date | None = None) -> dict:
        started = time.perf_counter()
        ctx = SemanticQuery.from_dict(context) if context else None
        interp = self.rules.parse(question, ctx, today=today)
        q, engine, notes = interp.query, "rules", list(interp.notes)

        if interp.confidence < CONFIDENT and self.llm.available:
            try:
                llm_q, message = self.llm.interpret(question, ctx, today=today)
                if llm_q is None:
                    return self._unanswerable(question, message, started)
                q, engine, notes = llm_q, "claude", []
            except Exception as exc:  # network, auth, schema: fall back to the rule parser's best guess
                notes.append(f"Claude was unavailable ({type(exc).__name__}); used the built-in interpreter.")

        if interp.confidence == 0 and engine == "rules":
            return self._unanswerable(
                question,
                "I couldn't match that to any metric, breakdown or time period. "
                "Try naming a metric (like revenue, orders or margin) and how to slice it.",
                started,
            )
        out = self.query(q, started=started)
        out.update(question=question, engine=engine, notes=notes + out.get("notes", []),
                   confidence=round(interp.confidence, 2))
        self._log(out)
        return out

    def query(self, q, started: float | None = None) -> dict:
        """Run a structured query directly (custom breakdowns, chip edits). No interpretation step."""
        started = started or time.perf_counter()
        if isinstance(q, dict):
            q = SemanticQuery.from_dict(q)
        today = dt.date.today()
        if q.time_grain and not q.time_range:
            q.time_range = default_trend_range(q.time_grain, today)
        if q.compare and not q.time_range:
            q.time_range = TimeRange(dt.date(today.year, 1, 1).isoformat(),
                                     (today + dt.timedelta(days=1)).isoformat(), "Year to date")
        try:
            q = self.model.validate(q)
            result = run(self.model, self.con, q)
        except (QueryError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": _ms(started)}
        return {
            "ok": True,
            "answer": narrate.answer(self.model, q, result),
            "interpretation": narrate.describe(self.model, q),
            "query": q.to_dict(),
            "columns": [self._column(c) for c in result["columns"]],
            "rows": result["rows"],
            "totals": result["totals"],
            "sql": result["sql"],
            "compare_range": result["compare_range"],
            "suggestions": narrate.suggestions(self.model, q),
            "notes": [],
            "elapsed_ms": _ms(started),
        }

    # ---- helpers -----------------------------------------------------------

    def _column(self, name: str) -> dict:
        base, _, suffix = name.partition("__")
        if name == "period":
            return {"name": name, "label": "Period", "role": "time"}
        if base in self.model.dimensions:
            return {"name": name, "label": self.model.dimensions[base]["label"], "role": "dimension"}
        m = self.model.metrics[base]
        if suffix == "prev":
            return {"name": name, "label": f"{m['label']} (prior)", "role": "metric", "format": m["format"]}
        if suffix == "change":
            return {"name": name, "label": "Change", "role": "change", "format": "percent"}
        return {"name": name, "label": m["label"], "role": "metric", "format": m["format"]}

    def _unanswerable(self, question: str, message: str, started: float) -> dict:
        out = {"ok": False, "question": question, "error": message, "elapsed_ms": _ms(started),
               "samples": self.model.sample_queries[:4]}
        self._log(out)
        return out

    def _log(self, out: dict) -> None:
        """Append every question to a JSONL log: the raw material for improving synonyms and evals."""
        if not self.log_path:
            return
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "question": out.get("question"),
                 "ok": out.get("ok"), "engine": out.get("engine"), "confidence": out.get("confidence"),
                 "query": out.get("query"), "error": out.get("error"), "elapsed_ms": out.get("elapsed_ms")}
        with self._log_lock, open(self.log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
