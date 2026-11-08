"""The agent: question -> interpretation -> (clarify | governed query | driver analysis) -> answer."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import sqlite3
import os
from functools import wraps
from pathlib import Path

from . import explain, narrate, sqlguard
from .llm import LocalInterpreter
from .parser import RuleParser, default_trend_range, _add_months, _month_start, _quarter_start
from .semantic import QueryError, SemanticModel, SemanticQuery, TimeRange, run

CONFIDENT = 0.5
DRAFT_LABEL = "Unverified: written by the on-prem model, not by the governed definitions"
VALUES_TTL = 60  # seconds before re-reading dimension values from a live database


_LOG_LOCK = threading.Lock()


def guarded(snapshot=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            try:
                if snapshot:
                    with self.source.snapshot():
                        return fn(self, *args, **kwargs)
                return fn(self, *args, **kwargs)
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError, sqlite3.Error) as exc:
                message = str(exc) if isinstance(exc, (QueryError, ValueError)) else "The data or query could not be read. Check its structure and source schema."
                return {"ok": False, "error": message, "elapsed_ms": 0}
        return wrapped
    return decorate


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
        self.source.close()

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
                sql += " LIMIT 2001"
                fetched = self.source.con.execute(sql).fetchall()
                values[name] = sorted((v for (v,) in fetched[:2000] if v is not None),
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
                   source=self.source.type_word, freshness=self.freshness())
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
        return {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "latest_update": latest,
                "coverage": coverage, "live": self.source.live, "source": self.source.type_word}

    # ---- entry points ------------------------------------------------------

    @guarded()
    def ask(self, question: str, context: dict | None = None, clarifications: dict | None = None,
            today: dt.date | None = None) -> dict:
        started = time.perf_counter()
        self._refresh_values()
        today = today or dt.date.today()
        ctx = SemanticQuery.from_dict(context) if context else None
        if clarifications and clarifications.get("draft"):
            steps = []
            _step(steps, "Read the question", "You asked the on-prem model to try, since no governed measure fits")
            return self._fallback(question, "The governed definitions don't cover this.", today, started, steps)
        interp = self.rules.parse(question, ctx, today=today, clarifications=clarifications)
        q, engine, notes = interp.query, "rules", list(interp.notes)
        steps = []

        if interp.confidence < CONFIDENT and self.llm.available and not clarifications and not interp.ambiguities:
            try:
                llm_q, message = self.llm.interpret(question, ctx, today=today)
                if llm_q is None:
                    _step(steps, "Read the question", "The built-in rules and the on-prem language model "
                                                      "couldn't match it to the semantic model")
                    return self._fallback(question, message, today, started, steps)
                q, engine, notes = llm_q, "local-llm", []
                interp.ambiguities = [] if llm_q.metrics else interp.ambiguities
            except Exception as exc:  # local model down or returned bad JSON: use the rule parser's result
                notes.append(f"The local language model was unavailable ({type(exc).__name__}); "
                             f"used the built-in interpreter.")

        if interp.confidence == 0 and engine == "rules" and not clarifications:
            _step(steps, "Read the question", "The built-in rules couldn't match it to the semantic model")
            return self._fallback(
                question,
                "I couldn't match that to anything in the data. Try naming a measure (like "
                + ", ".join(self.model.metrics[m]["label"].lower() for m in self.model.featured_metrics[:3])
                + ") and how to slice it.",
                today, started, steps,
            )
        if engine == "rules":
            _step(steps, "Read the question", f"Built-in rules, no language model · confidence {interp.confidence:.2f}")
        else:
            _step(steps, "Read the question", "On-prem language model (the built-in rules weren't confident); "
                                              "its reading is checked like any other")
        if interp.ambiguities and engine == "rules":
            out = self._clarify(question, interp.ambiguities[0], today, started)
            self._log(out)
            return out

        if q.explain:
            out = self.explain(q, interp.claimed_direction, today, started)
        else:
            out = self.query(q, started=started)
        out.update(question=question, engine=engine, notes=notes + out.get("notes", []),
                   confidence=round(interp.confidence, 2), steps=steps + out.get("steps", []))
        self._log(out)
        return out

    @guarded(snapshot=True)
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
            ran = time.perf_counter()
            result = run(self.model, self.source.con, q)
        except (QueryError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": _ms(started)}
        steps = []
        _step(steps, "Matched the semantic model", self._reading(q))
        _step(steps, "Compiled SQL", f"{_plural(len(result['sql']), 'parameterized statement')} from the governed "
                                     "definitions; no model-written SQL")
        _step(steps, f"Ran it read-only on {self._where()}", f"{_plural(len(result['rows']), 'row')} · {_ms(ran)} ms"
                                                             + (" · live data, no cache" if self.source.live else ""))
        freshness = self.freshness()
        answer = narrate.answer(self.model, q, result)
        coverage = freshness.get("coverage") or {}
        empty = all(r.get(m) is None for r in result["rows"] for m in q.metrics)  # also "one row of blanks"
        if empty and coverage.get("from"):
            answer += f" The data covers {coverage['from']} to {coverage['to']}."
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
            "truncated": result.get("truncated", False),
            "comparison_policy": result.get("comparison_policy"),
            "suggestions": narrate.suggestions(self.model, q),
            "freshness": freshness,
            "steps": steps + [{"title": "Wrote the answer", "detail": "Plain-English template filled from the result; "
                                                                      "no language model"}],
            "notes": (["Only the first groups are displayed; totals include all matching data."] if result.get("truncated") else [])
                     + (["Draft import: review metric aggregation and units before business use."] if self.model.spec.get("draft") else []),
            "elapsed_ms": _ms(started),
        }

    @guarded(snapshot=True)
    def explain(self, q: SemanticQuery, claimed: str | None, today: dt.date, started: float) -> dict:
        try:
            q = self.model.validate(q)
            ran = time.perf_counter()
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
        split = "rate and mix" if res.get("decomposition") == "rate_mix" else "contribution"
        res["steps"] = [
            {"title": "Matched the semantic model", "detail": self._reading(q)},
            {"title": "Compared two periods", "detail": f"{res['current']['range']['label'] or 'This period'} vs "
                                                        f"{res['previous']['range']['label']}"},
            {"title": f"Split the change by {split}", "detail": "Checked " + (", ".join(d["label"] for d in dims) or
                                                                              "no breakdowns")
                                                               + "; each split adds up to the total change"},
            {"title": f"Ran it read-only on {self._where()}",
             "detail": f"{_plural(len(res.get('sql') or []), 'parameterized statement')} · {_ms(ran)} ms"},
            {"title": "Wrote the answer", "detail": "What, how and why from the numbers; no language model"},
        ]
        return res

    # ---- clarifying questions ----------------------------------------------

    def _clarify(self, question: str, amb: dict, today: dt.date, started: float) -> dict:
        kind = amb["kind"]
        tomorrow = (today + dt.timedelta(days=1)).isoformat()
        draft = ([{"label": "Something else: let the on-prem model try",
                   "hint": "It writes a read-only query from the tables; the answer is marked unverified",
                   "clarification": {"draft": True}}] if self.llm.available and self.llm.sql_fallback else [])
        if kind == "constraint":
            return {"ok": True, "type": "clarify", "question": question,
                    "prompt": "What do you mean by ‘" + amb["term"] + "’? I cannot apply that condition safely. Please rephrase using a known measure, period and group.",
                    "options": draft, "ambiguity": kind, "elapsed_ms": _ms(started)}
        if kind == "metric":
            opts = [{"label": self.model.metrics[m]["label"], "clarification": {"metric": m},
                     "hint": self.model.metrics[m].get("description", "")} for m in amb["options"]]
            prompt = (f"“{amb['term']}” could mean a few things. Which measure should I use?" if amb["term"]
                      else "Which measure would you like to see?")
            opts += draft
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

    def _reading(self, q: SemanticQuery) -> str:
        parts = [c["label"] for c in narrate.describe(self.model, q)]
        return " · ".join(parts) or "Default measure"

    def _where(self) -> str:
        return "PostgreSQL" if self.source.kind == "postgres" else {"SQL": "the SQL database"}.get(
            self.source.type_word, f"the {self.source.type_word} file")

    def tables(self) -> set:
        """Tables the semantic model already exposes: the only ones drafted SQL may read."""
        names = [self.model.spec["base_table"]] + [j["table"] for j in self.model.joins.values()]
        return {sqlguard._bare(n) for n in names}

    @guarded(snapshot=True)
    def _fallback(self, question: str, message: str, today: dt.date, started: float, steps: list) -> dict:
        """The semantic model can't answer: let the on-prem model draft one guarded, read-only SELECT."""
        if not (self.llm.available and self.llm.sql_fallback):
            return self._unanswerable(question, message, started, steps)
        allowed = self.tables()
        schema_text = self._schema_for_llm(allowed)
        try:
            sql, note = self.llm.draft_sql(question, schema_text, self.source.dialect.name, today)
        except Exception as exc:  # local model down or returned bad JSON
            _step(steps, "Asked the on-prem model for SQL", f"It was unavailable ({type(exc).__name__})")
            return self._unanswerable(question, message, started, steps)
        if sql is None:
            _step(steps, "Asked the on-prem model for SQL", "It said these tables can't answer the question")
            return self._unanswerable(question, message, started, steps)
        _step(steps, "Asked the on-prem model for SQL", "It saw table and column names only, never data rows")
        try:
            checked = sqlguard.check(sql, allowed)
            ran = time.perf_counter()
            result = sqlguard.execute(self.source, checked, allowed)
        except sqlguard.Rejected as exc:
            _step(steps, "Blocked the drafted SQL", str(exc)[:1].upper() + str(exc)[1:])
            out = self._unanswerable(question, f"{message} The on-prem model drafted a query, but it was blocked "
                                               f"because {exc}.", started, steps)
            out["sql"] = [{"sql": sql[:sqlguard.MAX_LENGTH], "params": []}]
            return out
        except Exception as exc:  # syntax or type errors in the draft: report, never crash
            _step(steps, "The drafted SQL failed to run", type(exc).__name__)
            out = self._unanswerable(question, f"{message} The on-prem model drafted a query, but the database "
                                               "couldn't run it.", started, steps)
            out["sql"] = [{"sql": sql[:sqlguard.MAX_LENGTH], "params": []}]
            return out
        _step(steps, "Passed the safety check", "One read-only SELECT on " + ", ".join(sorted(allowed))
                                                + "; enforced by the database too")
        rows = result["rows"]
        _step(steps, f"Ran it read-only on {self._where()}",
              f"{_plural(len(rows), 'row')} (capped at {sqlguard.MAX_ROWS}) · {_ms(ran)} ms")
        columns = [{"name": c, "label": c.replace("_", " ").capitalize(),
                    "role": "metric" if rows and all(isinstance(r[c], (int, float)) and not isinstance(r[c], bool)
                                                     for r in rows if r[c] is not None)
                    and any(r[c] is not None for r in rows) else "text", "format": "number"}
                   for c in result["columns"]]
        answer = (f"The governed definitions don't cover this, so the on-prem model wrote a query. "
                  f"It returned {_plural(len(rows), 'row')}. Check it before relying on it.")
        out = {
            "ok": True, "type": "draft", "verified": False, "label": DRAFT_LABEL, "question": question,
            "answer": answer, "note": note, "columns": columns, "rows": rows, "truncated": result["truncated"],
            "sql": [{"sql": checked, "params": []}], "engine": "local-llm-sql", "freshness": self.freshness(),
            "notes": [f"Showing the first {sqlguard.MAX_ROWS} rows."] if result["truncated"] else [],
            "steps": steps + [{"title": "Wrote the answer", "detail": "Row count only; the model's own description "
                                                                      "is shown separately and is unverified"}],
            "suggestions": [], "elapsed_ms": _ms(started),
        }
        self._log(out)
        return out

    def _schema_for_llm(self, allowed: set) -> str:
        lines = ["Tables:"]
        for table in sorted(allowed):
            lines.append(f"- {table}: " + ", ".join(f"{c} {t}".strip() for c, t in self.source.columns(table)))
        lines.append("Governed definitions (the alias before each dot is a table above):")
        alias = {self.model.spec["base_alias"]: sqlguard._bare(self.model.spec["base_table"])}
        alias.update({j["alias"]: sqlguard._bare(j["table"]) for j in self.model.joins.values()})
        lines.append("  aliases: " + ", ".join(f"{a} = {t}" for a, t in alias.items()))
        for m in self.model.metrics.values():
            lines.append(f"- {m['label']}: {m['sql']}")
        return "\n".join(lines)

    def _unanswerable(self, question: str, message: str, started: float, steps: list | None = None) -> dict:
        out = {"ok": False, "question": question, "error": message, "elapsed_ms": _ms(started),
               "samples": self.model.sample_queries[:4], "steps": steps or []}
        self._log(out)
        return out

    def _log(self, out: dict) -> None:
        """Append every question to a local JSONL log: the raw material for improving synonyms and evals."""
        if not self.log_path:
            return
        entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "dataset": self.id,
                 "question": out.get("question"), "ok": out.get("ok"), "type": out.get("type"),
                 "engine": out.get("engine"), "confidence": out.get("confidence"), "query": out.get("query"),
                 "ambiguity": out.get("ambiguity"), "error": out.get("error"), "elapsed_ms": out.get("elapsed_ms")}
        try:
            with _LOG_LOCK:
                self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.log_path.exists() and self.log_path.stat().st_size > 5 * 1024 * 1024:
                    self.log_path.replace(self.log_path.with_suffix(".previous.jsonl"))
                # Query logs are sensitive; full text is opt-in.
                if os.environ.get("BI_LOG_QUESTIONS") != "1":
                    entry.pop("question", None)
                    entry.pop("query", None)
                    entry.pop("error", None)
                fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass  # failure to write telemetry must not prevent an answer



def _last_month(today: dt.date) -> TimeRange:
    start = _add_months(_month_start(today), -1)
    return TimeRange(start.isoformat(), _month_start(today).isoformat(), start.strftime("%B %Y"))


def _step(steps: list, title: str, detail: str = "") -> None:
    steps.append({"title": title, "detail": detail})


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
