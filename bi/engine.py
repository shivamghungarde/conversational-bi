"""The agent: question -> interpretation -> (clarify | governed query | driver analysis) -> answer."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

from . import explain, narrate
from .llm import LocalInterpreter
from .parser import RuleParser, default_trend_range, _add_months, _month_start, _quarter_start
from .semantic import QueryError, SemanticModel, SemanticQuery, TimeRange, run

CONFIDENT = 0.5
VALUES_TTL = 60  # seconds before re-reading dimension values from a live database


class Engine:
    def __init__(self, dataset_id: str, model: SemanticModel, source, log_path: Path | None = None):
        self.id = dataset_id
        self.model = model
        self.source = source
        self.log_path = log_path
        self._log_lock = threading.Lock()
        self._values_lock = threading.Lock()
        self._values_key = None
        self._refresh_values(force=True)

    # ---- metadata ----------------------------------------------------------

    def _refresh_values(self, force: bool = False) -> None:
        """Dimension values feed filter detection; re-read them when the data changes."""
        key = (self.source.version(), int(time.time() // VALUES_TTL))
        if not force and key == self._values_key:
            return
        with self._values_lock:
            if not force and key == self._values_key:
                return
            values = {}
            for name, d in self.model.dimensions.items():
                sql = f"SELECT DISTINCT {d['sql']} FROM {self.model.spec['base_table']} {self.model.spec['base_alias']}"
                for j in self.model._with_requirements({d["join"]} if d.get("join") else set()):
                    spec = self.model.joins[j]
                    sql += f" {'LEFT JOIN' if spec.get('type') == 'left' else 'JOIN'} {spec['table']} {spec['alias']} ON {spec['on']}"
                values[name] = sorted((v for (v,) in self.source.con.execute(sql).fetchall() if v is not None),
                                      key=str)
            self.dimension_values = values
            self.rules = RuleParser(self.model, values)
            self.llm = LocalInterpreter(self.model, values)
            self._values_key = key

    def catalog(self) -> dict:
        self._refresh_values()
        cat = self.model.catalog()
        for d in cat["dimensions"]:
            d["values"] = [str(v) for v in self.dimension_values.get(d["name"], [])][:200]
        cat.update(id=self.id, llm_enabled=self.llm.available, llm_error=self.llm.error,
                   source=self.source.describe(), freshness=self.freshness())
        return cat

    def freshness(self) -> dict:
        """When the data last changed, so every answer can say how current it is."""
        latest, coverage = None, None
        t = self.model.time
        try:
            if t and t.get("freshness_sql"):
                (latest,) = self.source.con.execute(t["freshness_sql"]).fetchone()
            if t:
                sql = (f"SELECT MIN({t['sql']}), MAX({t['sql']}) FROM {self.model.spec['base_table']} "
                       f"{self.model.spec['base_alias']}")
                lo, hi = self.source.con.execute(sql).fetchone()
                coverage = {"from": str(lo)[:10] if lo else None, "to": str(hi)[:10] if hi else None}
        except Exception:
            pass
        loaded_at = getattr(self.source, "loaded_at", None)
        if latest is None and loaded_at:
            latest = loaded_at.isoformat(timespec="seconds")
        return {"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "latest_update": latest,
                "coverage": coverage, "live": self.source.live, "source": self.source.describe()}

    # ---- entry points ------------------------------------------------------

    def ask(self, question: str, context: dict | None = None, clarifications: dict | None = None,
            today: dt.date | None = None) -> dict:
        started = time.perf_counter()
        self._refresh_values()
        today = today or dt.date.today()
        ctx = SemanticQuery.from_dict(context) if context else None
        interp = self.rules.parse(question, ctx, today=today, clarifications=clarifications)
        q, engine, notes = interp.query, "rules", list(interp.notes)

        if interp.confidence < CONFIDENT and self.llm.available and not clarifications:
            try:
                llm_q, message = self.llm.interpret(question, ctx, today=today)
                if llm_q is None:
                    return self._unanswerable(question, message, started)
                q, engine, notes = llm_q, "local-llm", []
                interp.ambiguities = [] if llm_q.metrics else interp.ambiguities
            except Exception as exc:  # local model down or returned bad JSON: use the rule parser's result
                notes.append(f"The local language model was unavailable ({type(exc).__name__}); "
                             f"used the built-in interpreter.")

        if interp.confidence == 0 and engine == "rules" and not clarifications:
            return self._unanswerable(
                question,
                "I couldn't match that to anything in the data. Try naming a measure (like "
                + ", ".join(self.model.metrics[m]["label"].lower() for m in self.model.featured_metrics[:3])
                + ") and how to slice it.",
                started,
            )
        if interp.ambiguities and engine == "rules":
            out = self._clarify(question, interp.ambiguities[0], today, started)
            self._log(out)
            return out

        if q.explain:
            out = self.explain(q, interp.claimed_direction, today, started)
        else:
            out = self.query(q, started=started)
        out.update(question=question, engine=engine, notes=notes + out.get("notes", []),
                   confidence=round(interp.confidence, 2))
        self._log(out)
        return out

    def query(self, q, started: float | None = None, today: dt.date | None = None) -> dict:
        """Run a structured query directly (custom breakdowns, chip edits). No interpretation step."""
        started = started or time.perf_counter()
        today = today or dt.date.today()
        if isinstance(q, dict):
            q = SemanticQuery.from_dict(q)
        if q.explain:
            if not q.time_range:
                q.time_range = _last_month(today)
            return self.explain(q, None, today, started)
        if q.time_grain and not q.time_range:
            q.time_range = default_trend_range(q.time_grain, today)
        if q.compare and not q.time_range:
            q.time_range = TimeRange(dt.date(today.year, 1, 1).isoformat(),
                                     (today + dt.timedelta(days=1)).isoformat(), "Year to date")
        try:
            q = self.model.validate(q)
            result = run(self.model, self.source.con, q)
        except (QueryError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": _ms(started)}
        freshness = self.freshness()
        answer = narrate.answer(self.model, q, result)
        if not result["rows"] and freshness.get("coverage", {}).get("from"):
            answer += f" The data covers {freshness['coverage']['from']} to {freshness['coverage']['to']}."
        return {
            "ok": True,
            "type": "result",
            "answer": answer,
            "interpretation": narrate.describe(self.model, q),
            "query": q.to_dict(),
            "columns": [self._column(c) for c in result["columns"]],
            "rows": result["rows"],
            "totals": result["totals"],
            "sql": result["sql"],
            "compare_range": result["compare_range"],
            "suggestions": narrate.suggestions(self.model, q),
            "freshness": freshness,
            "notes": [],
            "elapsed_ms": _ms(started),
        }

    def explain(self, q: SemanticQuery, claimed: str | None, today: dt.date, started: float) -> dict:
        try:
            q = self.model.validate(q)
            res = explain.analyse(self.model, self.source.con, q, claimed=claimed, today=today)
        except (QueryError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": _ms(started)}
        chips = narrate.describe(self.model, q)
        chips = [c for c in chips if c["kind"] != "compare"] + [{
            "kind": "explain", "label": f"Why: {res['current']['range']['label'] or 'this period'} vs "
                                        f"{res['previous']['range']['label']}", "value": True}]
        dims = [{"name": r["dimension"], "label": r["label"]} for r in res["ranking"]]
        res.update(
            ok=True,
            query=q.to_dict(),
            interpretation=chips,
            dimensions=dims,
            metric_meta={"label": self.model.metrics[q.metrics[0]]["label"],
                         "format": self.model.metrics[q.metrics[0]]["format"]},
            suggestions=[{"label": f"Show {self.model.metrics[q.metrics[0]]['label'].lower()} by "
                                   f"{r['label'].lower()}", "patch": {"explain": False, "dimensions": [r["dimension"]],
                                                                      "compare": q.compare}}
                         for r in res["ranking"][:3]],
            freshness=self.freshness(),
            notes=[],
            elapsed_ms=_ms(started),
        )
        return res

    # ---- clarifying questions ----------------------------------------------

    def _clarify(self, question: str, amb: dict, today: dt.date, started: float) -> dict:
        kind = amb["kind"]
        tomorrow = (today + dt.timedelta(days=1)).isoformat()
        if kind == "metric":
            opts = [{"label": self.model.metrics[m]["label"], "clarification": {"metric": m},
                     "hint": self.model.metrics[m].get("description", "")} for m in amb["options"]]
            prompt = (f"“{amb['term']}” could mean a few things. Which measure should I use?" if amb["term"]
                      else "Which measure would you like to see?")
        elif kind == "value":
            opts = [{"label": f"{v} ({self.model.dimensions[d]['label']})",
                     "clarification": {"values": [{"term": amb["term"], "dimension": d, "value": v}]}}
                    for d, v in amb["options"]]
            prompt = f"“{amb['term'].strip()}” matches more than one thing. Which do you mean?"
        elif kind == "time":
            ranges = [
                TimeRange(_month_start(today).isoformat(), tomorrow, "This month"),
                TimeRange(_add_months(_month_start(today), -1).isoformat(), _month_start(today).isoformat(),
                          "Last month"),
                TimeRange((today - dt.timedelta(days=29)).isoformat(), tomorrow, "Last 30 days"),
                TimeRange(_quarter_start(today).isoformat(), tomorrow, "This quarter"),
            ]
            opts = [{"label": r.label, "clarification": {"time": r.__dict__}} for r in ranges]
            prompt = f"What period do you mean by “{amb['term']}”?"
        elif kind == "explain_period":
            last = _last_month(today)
            opts = [
                {"label": f"{last.label} vs the month before",
                 "clarification": {"time": last.__dict__, "compare": "previous_period"}},
                {"label": "This month so far vs same days last month",
                 "clarification": {"time": TimeRange(_month_start(today).isoformat(), tomorrow,
                                                     "Month to date").__dict__, "compare": "previous_period"}},
                {"label": "Last quarter vs the quarter before",
                 "clarification": {"time": TimeRange(_add_months(_quarter_start(today), -3).isoformat(),
                                                     _quarter_start(today).isoformat(), "Last quarter").__dict__,
                                   "compare": "previous_period"}},
                {"label": "This year vs same period last year",
                 "clarification": {"time": TimeRange(dt.date(today.year, 1, 1).isoformat(), tomorrow,
                                                     "Year to date").__dict__, "compare": "previous_year"}},
            ]
            prompt = "Which periods should I compare to explain the change?"
        else:  # dimension
            dims = list(self.model.dimensions)[:5]
            opts = [{"label": self.model.dimensions[d]["label"], "clarification": {"dimension": d}} for d in dims]
            prompt = "Rank by what? Pick a breakdown."
        return {"ok": True, "type": "clarify", "question": question, "prompt": prompt, "options": opts,
                "ambiguity": kind, "elapsed_ms": _ms(started)}

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
            return {"name": name, "label": "Change", "role": "change",
                    "format": "pts" if m["format"] == "percent" else "percent"}
        return {"name": name, "label": m["label"], "role": "metric", "format": m["format"]}

    def _unanswerable(self, question: str, message: str, started: float) -> dict:
        out = {"ok": False, "question": question, "error": message, "elapsed_ms": _ms(started),
               "samples": self.model.sample_queries[:4]}
        self._log(out)
        return out

    def _log(self, out: dict) -> None:
        """Append every question to a local JSONL log: the raw material for improving synonyms and evals."""
        if not self.log_path:
            return
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "dataset": self.id,
                 "question": out.get("question"), "ok": out.get("ok"), "type": out.get("type"),
                 "engine": out.get("engine"), "confidence": out.get("confidence"), "query": out.get("query"),
                 "ambiguity": out.get("ambiguity"), "error": out.get("error"), "elapsed_ms": out.get("elapsed_ms")}
        with self._log_lock, open(self.log_path, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


def _last_month(today: dt.date) -> TimeRange:
    start = _add_months(_month_start(today), -1)
    return TimeRange(start.isoformat(), _month_start(today).isoformat(), start.strftime("%B %Y"))


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
