"""Semantic layer: the governed vocabulary (metrics, dimensions, joins) and the
compiler that turns a structured SemanticQuery into SQL.

The natural-language layer never writes SQL. It only produces a SemanticQuery,
which is validated against the model here. Every identifier in the generated
SQL comes from the model file; every user-supplied value is a bound parameter.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import copy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from decimal import Decimal

GRAINS = ("day", "week", "month", "quarter", "year")
FILTER_OPS = ("in", "not_in")
COMPARES = ("previous_period", "previous_year")
MAX_LIMIT = 1000


class QueryError(ValueError):
    """A SemanticQuery that does not fit the model."""


@dataclass
class Filter:
    dimension: str
    op: str = "in"
    values: list = field(default_factory=list)


@dataclass
class TimeRange:
    start: str  # inclusive, YYYY-MM-DD
    end: str    # exclusive, YYYY-MM-DD
    label: str = ""


@dataclass
class SemanticQuery:
    metrics: list = field(default_factory=list)
    dimensions: list = field(default_factory=list)
    filters: list = field(default_factory=list)  # list[Filter]
    time_range: TimeRange | None = None
    time_grain: str | None = None
    compare: str | None = None
    order_by: str | None = None   # a metric or dimension name, or "period"
    order_dir: str = "desc"
    limit: int | None = None
    explain: bool = False         # "why" questions: driver analysis instead of a plain result

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticQuery":
        if not isinstance(d, dict):
            raise QueryError("Query must be an object.")
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise QueryError("Unknown query fields: " + ", ".join(sorted(unknown)))
        def names(key, maximum):
            values = d.get(key, [])
            if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, str) for v in values):
                raise QueryError(f"{key} must be a list of at most {maximum} names.")
            return values
        filters = d.get("filters", [])
        if not isinstance(filters, list) or len(filters) > 20:
            raise QueryError("At most 20 filters are allowed.")
        parsed = []
        for f in filters:
            if not isinstance(f, dict) or set(f) - {"dimension", "op", "values"} or not isinstance(f.get("dimension"), str):
                raise QueryError("Each filter needs a dimension and a list of values.")
            values = f.get("values", [])
            if (not isinstance(values, list) or not 1 <= len(values) <= 1000
                    or any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in values)):
                raise QueryError("Filter values must contain 1–1000 scalar values.")
            parsed.append(Filter(f["dimension"], f.get("op", "in"), values))
        tr = d.get("time_range")
        if tr is not None:
            if not isinstance(tr, dict) or set(tr) - {"start", "end", "label"} or not all(isinstance(tr.get(k), str) for k in ("start", "end")):
                raise QueryError("time_range needs start and end dates.")
            if not isinstance(tr.get("label", ""), str):
                raise QueryError("Period label must be text.")
            tr = TimeRange(**tr)
        for key in ("time_grain", "compare", "order_by", "order_dir"):
            if d.get(key) is not None and not isinstance(d[key], str):
                raise QueryError(f"{key} must be text.")
        if "explain" in d and not isinstance(d["explain"], bool):
            raise QueryError("explain must be true or false.")
        if d.get("limit") is not None and (not isinstance(d["limit"], int) or isinstance(d["limit"], bool)):
            raise QueryError("limit must be an integer.")
        return cls(metrics=names("metrics", 20), dimensions=names("dimensions", 8), filters=parsed,
                   time_range=tr, time_grain=d.get("time_grain"), compare=d.get("compare"),
                   order_by=d.get("order_by"), order_dir=d.get("order_dir") or "desc",
                   limit=d.get("limit"), explain=d.get("explain", False))


class Dialect:
    """The few SQL fragments that differ between databases."""

    name = "sqlite"
    placeholder = "?"

    def date_param(self) -> str:
        return self.placeholder

    def grain(self, col: str, grain: str) -> str:
        return {
            "day": f"substr({col}, 1, 10)",
            "week": f"strftime('%Y', date({col}, '-3 days', 'weekday 4')) || '-W' || printf('%02d', (CAST(strftime('%j', date({col}, '-3 days', 'weekday 4')) AS INTEGER) - 1) / 7 + 1)",
            "month": f"strftime('%Y-%m', {col})",
            "quarter": f"strftime('%Y', {col}) || '-Q' || ((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3)",
            "year": f"strftime('%Y', {col})",
        }[grain]


class PostgresDialect(Dialect):
    name = "postgres"
    placeholder = "%s"

    def date_param(self) -> str:
        return "CAST(%s AS DATE)"

    def grain(self, col: str, grain: str) -> str:
        return {
            "day": f"to_char({col}, 'YYYY-MM-DD')",
            "week": f"to_char({col}, 'IYYY-\"W\"IW')",
            "month": f"to_char({col}, 'YYYY-MM')",
            "quarter": f"to_char({col}, 'YYYY-\"Q\"Q')",
            "year": f"to_char({col}, 'YYYY')",
        }[grain]


DIALECTS = {"sqlite": Dialect(), "postgres": PostgresDialect()}


class SemanticModel:
    def __init__(self, spec: dict, dialect: Dialect | None = None):
        for group in ("metrics", "dimensions"):
            names = [x.get("name") for x in spec.get(group, [])]
            if len(names) != len(set(names)) or any(not isinstance(n, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", n) for n in names):
                raise QueryError(f"{group} names must be unique SQL identifiers.")
        if set(x["name"] for x in spec.get("metrics", [])) & set(x["name"] for x in spec.get("dimensions", [])):
            raise QueryError("Metric and dimension names must be distinct.")
        self.spec = spec
        self.name = spec["name"]
        self.label = spec.get("label", self.name)
        self.metrics = {m["name"]: m for m in spec["metrics"]}
        self.dimensions = {d["name"]: d for d in spec["dimensions"]}
        self.joins = spec.get("joins", {})
        self.time = spec.get("time_dimension")  # optional: snapshot files may have no date column
        self.sample_queries = spec.get("sample_queries", [])
        self.ambiguous_terms = spec.get("ambiguous_terms", {})
        self.featured_metrics = spec.get("featured_metrics") or list(self.metrics)[:4]
        self.dialect = dialect or DIALECTS["sqlite"]
        for name in self.joins:
            self._with_requirements({name})
        for m in self.metrics.values():
            if m.get("ratio") and any(n not in self.metrics for n in m["ratio"].values()):
                raise QueryError("Ratio references an unknown metric.")
            if any(j not in self.joins for j in m.get("joins", [])):
                raise QueryError("Metric references an unknown join.")
        if any(d.get("join") and d["join"] not in self.joins for d in self.dimensions.values()):
            raise QueryError("Dimension references an unknown join.")

    @classmethod
    def load(cls, path: Path, dialect: Dialect | None = None) -> "SemanticModel":
        return cls(json.loads(Path(path).read_text()), dialect)

    def is_percent(self, metric: str) -> bool:
        return self.metrics[metric].get("format") == "percent"

    def catalog(self) -> dict:
        """Public description of the model (safe to send to the browser or a local LLM)."""
        return {
            "name": self.name,
            "label": self.label,
            "description": self.spec.get("description", ""),
            "metrics": [
                {k: m[k] for k in ("name", "label", "format", "description") if k in m}
                for m in self.metrics.values()
            ],
            "dimensions": [{"name": d["name"], "label": d["label"]} for d in self.dimensions.values()],
            "time_dimension": {"name": self.time["name"], "label": self.time["label"]} if self.time else None,
            "grains": list(GRAINS),
        }

    # ---- validation -------------------------------------------------------

    def validate(self, q: SemanticQuery) -> SemanticQuery:
        if not isinstance(q, SemanticQuery):
            raise QueryError("Expected a SemanticQuery.")
        if not q.metrics:
            raise QueryError("Pick at least one metric.")
        for m in q.metrics:
            if m not in self.metrics:
                raise QueryError(f"Unknown metric '{m}'.")
        for d in q.dimensions:
            if d not in self.dimensions:
                raise QueryError(f"Unknown dimension '{d}'.")
        q.metrics = list(dict.fromkeys(q.metrics))
        q.dimensions = list(dict.fromkeys(q.dimensions))
        for f in q.filters:
            if f.dimension not in self.dimensions:
                raise QueryError(f"Unknown filter dimension '{f.dimension}'.")
            if f.op not in FILTER_OPS:
                raise QueryError(f"Unsupported filter operator '{f.op}'.")
            if not f.values:
                raise QueryError(f"Filter on '{f.dimension}' needs at least one value.")
        if not self.time and (q.time_range or q.time_grain or q.compare or q.explain):
            raise QueryError("This source has no date field. Choose a source with dates or remove the period.")
        if q.time_grain is not None and q.time_grain not in GRAINS:
            raise QueryError(f"Unknown time grain '{q.time_grain}'.")
        if q.compare is not None:
            if q.compare not in COMPARES:
                raise QueryError(f"Unknown comparison '{q.compare}'.")
            if q.time_range is None:
                raise QueryError("A comparison needs a time range.")
            if q.time_grain and not q.explain:
                q.compare = None  # trends already show change over time
        if q.time_range:
            for v in (q.time_range.start, q.time_range.end):
                dt.date.fromisoformat(v)
            start, end = dt.date.fromisoformat(q.time_range.start), dt.date.fromisoformat(q.time_range.end)
            if start >= end:
                raise QueryError("Time range start must be before its end.")
            if (end - start).days > 366 * 200:
                raise QueryError("Time range exceeds 200 years.")
        if q.order_dir not in ("asc", "desc"):
            raise QueryError("order_dir must be 'asc' or 'desc'.")
        valid_order = set(q.metrics) | set(q.dimensions) | {"period"}
        if q.order_by is not None and q.order_by not in valid_order:
            q.order_by = None
        if q.limit is not None:
            q.limit = max(1, min(int(q.limit), MAX_LIMIT))
        return q

    # ---- compilation ------------------------------------------------------

    def _with_requirements(self, joins: set) -> list:
        """Order joins so a join's prerequisite (e.g. projects before clients) comes first."""
        ordered: list = []
        visiting = set()

        def add(name: str):
            if name not in self.joins or name in visiting:
                raise QueryError("Unknown or cyclic join dependency.")
            if name in ordered:
                return
            visiting.add(name)
            req = self.joins[name].get("requires")
            if req:
                add(req)
            visiting.remove(name)
            ordered.append(name)

        for j in sorted(joins):
            add(j)
        return ordered

    def compile(self, q: SemanticQuery, time_range: TimeRange | None = None,
                with_dimensions: bool = True, extra_metrics: tuple = (), apply_limit: bool = True) -> tuple:
        """Return (sql, params, columns). `time_range` overrides q.time_range (for comparisons)."""
        q = self.validate(q)
        tr = time_range or q.time_range
        dims = q.dimensions if with_dimensions else []
        grain = q.time_grain if with_dimensions else None
        metrics = list(dict.fromkeys(list(q.metrics) + list(extra_metrics)))

        select, columns, joins = [], [], set()
        if grain:
            select.append(f'{self.dialect.grain(self.time["sql"], grain)} AS "period"')
            columns.append("period")
        for d in dims:
            dim = self.dimensions[d]
            select.append(f'{dim["sql"]} AS "{d}"')  # quoted: a column may be called "select" or "order"
            columns.append(d)
            if dim.get("join"):
                joins.add(dim["join"])
        group = list(range(1, len(columns) + 1))
        for m in metrics:
            select.append(f'{self.metrics[m]["sql"]} AS "{m}"')
            columns.append(m)
            for j in self.metrics[m].get("joins", []):
                joins.add(j)

        where, params = [], []
        if tr and self.time:
            p = self.dialect.date_param()
            where.append(f"{self.time['sql']} >= {p} AND {self.time['sql']} < {p}")
            params += [tr.start, tr.end]
        for f in q.filters:
            dim = self.dimensions[f.dimension]
            if dim.get("join"):
                joins.add(dim["join"])
            marks = ", ".join(self.dialect.placeholder for _ in f.values)
            where.append(f"{dim['sql']} {'NOT IN' if f.op == 'not_in' else 'IN'} ({marks})")
            params += [str(v) for v in f.values]

        sql = f"SELECT {', '.join(select)}\nFROM {self.spec['base_table']} {self.spec['base_alias']}"
        for j in self._with_requirements(joins):
            spec = self.joins[j]
            kind = "LEFT JOIN" if spec.get("type") == "left" else "JOIN"
            sql += f"\n{kind} {spec['table']} {spec['alias']} ON {spec['on']}"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if group:
            sql += "\nGROUP BY " + ", ".join(str(g) for g in group)
            if q.order_by:
                order_col, direction = q.order_by, q.order_dir
            elif grain:
                order_col, direction = "period", "asc"
            else:
                order_col, direction = metrics[0], "desc"
            order = f'"{order_col}" {direction.upper()}'
            if grain and order_col != "period":
                order += ', "period" ASC'
            sql += f"\nORDER BY {order}"
            limit = q.limit or (MAX_LIMIT if grain else 100)
            if apply_limit:
                sql += f"\nLIMIT {int(limit)}"
        return sql, params, columns


def shift_range(tr: TimeRange, compare: str) -> TimeRange:
    start, end = dt.date.fromisoformat(tr.start), dt.date.fromisoformat(tr.end)
    if compare == "previous_year":
        def back(d: dt.date) -> dt.date:
            try:
                return d.replace(year=d.year - 1)
            except ValueError:  # Feb 29
                return d.replace(year=d.year - 1, day=28)
        return TimeRange(back(start).isoformat(), back(end).isoformat(), _shift_label(tr.label, "previous_year"))
    # previous_period: same length, immediately before. Calendar-aligned when the range is whole months.
    if start.day == 1 and end.day == 1:
        months = (end.year - start.year) * 12 + end.month - start.month
        y, m = divmod(start.year * 12 + start.month - 1 - months, 12)
        prev_start = dt.date(y, m + 1, 1)
        return TimeRange(prev_start.isoformat(), tr.start, _month_span_label(prev_start, months))
    if start.day == 1 and end.day != 1:
        # A to-date range (e.g. month to date): compare with the same number of days of the previous period.
        days = (end - start).days
        y, m = divmod(start.year * 12 + start.month - 2, 12)
        prev_start = dt.date(y, m + 1, 1)
        if "year" in tr.label.lower():
            prev_start = start.replace(year=start.year - 1)
        elif "quarter" in tr.label.lower():
            y, m = divmod(start.year * 12 + start.month - 4, 12)
            prev_start = dt.date(y, m + 1, 1)
        return TimeRange(prev_start.isoformat(), min(prev_start + dt.timedelta(days=days), start).isoformat(),
                         "same days of the previous period")
    length = end - start
    return TimeRange((start - length).isoformat(), tr.start, "previous period")


def _month_span_label(start: dt.date, months: int) -> str:
    if months == 1:
        return start.strftime("%B %Y")
    if months == 3 and start.month in (1, 4, 7, 10):
        return f"Q{(start.month + 2) // 3} {start.year}"
    if months == 12 and start.month == 1:
        return str(start.year)
    return "previous period"


def _shift_label(label: str, compare: str) -> str:
    return "same period last year"


def change(model: SemanticModel, metric: str, cur: Any, prev: Any):
    """Percent metrics change in points (absolute); everything else changes in percent (relative)."""
    if cur is None or prev is None:
        return None
    if model.is_percent(metric):
        return cur - prev
    if not prev:
        return None
    return (cur - prev) / abs(prev)


def run(model: SemanticModel, con, q: SemanticQuery) -> dict:
    """Execute bounded output with comparisons joined before the display limit."""
    q = model.validate(q)
    sql, params, columns = model.compile(q)
    cap = q.limit or (MAX_LIMIT if q.time_grain else 100)
    grouped = bool(q.dimensions or q.time_grain)
    if grouped:
        sql = sql.rsplit("LIMIT ", 1)[0] + f"LIMIT {cap + 1}"
    statements = [{"sql": sql, "params": params}]
    rows = [dict(zip(columns, [float(v) if isinstance(v, Decimal) else v for v in r])) for r in con.execute(sql, params).fetchall()]
    truncated = grouped and len(rows) > cap
    rows = rows[:cap] if grouped else rows
    totals = dict(rows[0]) if rows and not grouped else None
    if grouped:
        tsql, tp, tc = model.compile(q, with_dimensions=False)
        totals = dict(zip(tc, [float(v) if isinstance(v, Decimal) else v for v in con.execute(tsql, tp).fetchone()]))
        statements.append({"sql": tsql, "params": tp})
    compare_range = None
    if q.compare and q.time_range and not q.time_grain:
        prev = shift_range(q.time_range, q.compare)
        psql, pp, pc = model.compile(q, time_range=prev, apply_limit=False)
        if q.dimensions:
            # Match prior values only for the currently displayed keys. The prior aggregate is never top-N limited.
            keys = " AND ".join(f'(p."{d}" = c."{d}" OR (p."{d}" IS NULL AND c."{d}" IS NULL))' for d in q.dimensions)
            projection = ", ".join(f'p."{x}"' for x in pc)
            psql = f"WITH p AS ({psql}), c AS ({sql}) SELECT {projection} FROM p JOIN c ON {keys}"
            pp = pp + params
        prior = [dict(zip(pc, [float(v) if isinstance(v, Decimal) else v for v in r])) for r in con.execute(psql, pp).fetchall()]
        statements.append({"sql": psql, "params": pp})
        key = lambda r: tuple(r[d] for d in q.dimensions)
        by_key = {key(r): r for r in prior}
        for r in rows:
            p = by_key.get(key(r), {})
            for m in q.metrics:
                r[f"{m}__prev"] = p.get(m)
                r[f"{m}__change"] = change(model, m, r[m], p.get(m))
        for m in q.metrics:
            columns += [f"{m}__prev", f"{m}__change"]
        tsql, tp, tc = model.compile(q, time_range=prev, with_dimensions=False)
        pt = dict(zip(tc, [float(v) if isinstance(v, Decimal) else v for v in con.execute(tsql, tp).fetchone()]))
        statements.append({"sql": tsql, "params": tp})
        if totals is not None:
            for m in q.metrics:
                totals[f"{m}__prev"] = pt.get(m)
                totals[f"{m}__change"] = change(model, m, totals.get(m), pt.get(m))
        compare_range = asdict(prev)
    return {"columns": columns, "rows": rows, "totals": totals, "sql": statements,
            "compare_range": compare_range, "truncated": truncated,
            "comparison_policy": "Current-period ranking; prior values for the same groups." if q.compare else None}
