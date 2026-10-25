"""Rule-based interpreter: plain English -> SemanticQuery, in well under a millisecond.

It handles the bulk of everyday business questions (metric by dimension, time
ranges, top-N, trends, comparisons, filters, follow-ups). Anything it is not
confident about is handed to the LLM interpreter when one is configured.
"""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import re
from dataclasses import dataclass, field

from .semantic import Filter, SemanticModel, SemanticQuery, TimeRange

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
FOLLOW_UP_CUES = ("and ", "now ", "what about", "how about", "only ", "just ", "same ", "instead",
                  "also ", "then ", "break", "split", "drill", "filter", "exclude", "excluding",
                  "except", "without", "but ", "by ", "show it", "make it", "vs", "versus",
                  "compare", "compared")
NEGATIONS = ("excluding", "except", "without", "not", "exclude", "other than", "minus", "ex")


@dataclass
class Interpretation:
    query: SemanticQuery
    confidence: float
    notes: list = field(default_factory=list)  # human-readable reasons, shown in the UI
    follow_up: bool = False
    matched: dict = field(default_factory=dict)


def _month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return dt.date(y, m + 1, 1)


def _quarter_start(d: dt.date) -> dt.date:
    return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def parse_time(text: str, today: dt.date) -> tuple:
    """Find a time phrase. Returns (TimeRange | None, matched_span_text | None)."""
    tomorrow = today + dt.timedelta(days=1)
    t = text
    num = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"
    patterns = [
        (rf"\b(?:last|past|previous|trailing)\s+{num}\s+days?\b",
         lambda m: TimeRange((tomorrow - dt.timedelta(days=_num(m.group(1)))).isoformat(), tomorrow.isoformat(),
                             f"Last {_num(m.group(1))} days")),
        (rf"\b(?:last|past|previous|trailing)\s+{num}\s+weeks?\b",
         lambda m: TimeRange((tomorrow - dt.timedelta(weeks=_num(m.group(1)))).isoformat(), tomorrow.isoformat(),
                             f"Last {_num(m.group(1))} weeks")),
        (rf"\b(?:last|past|previous|trailing)\s+{num}\s+months?\b",
         lambda m: TimeRange(_add_months(_month_start(today), -_num(m.group(1))).isoformat(),
                             _month_start(today).isoformat(), f"Last {_num(m.group(1))} full months")),
        (rf"\b(?:last|past|previous|trailing)\s+{num}\s+quarters?\b",
         lambda m: TimeRange(_add_months(_quarter_start(today), -3 * _num(m.group(1))).isoformat(),
                             _quarter_start(today).isoformat(), f"Last {_num(m.group(1))} full quarters")),
        (rf"\b(?:last|past|previous|trailing)\s+{num}\s+years?\b",
         lambda m: TimeRange(dt.date(today.year - _num(m.group(1)), 1, 1).isoformat(),
                             dt.date(today.year, 1, 1).isoformat(), f"Last {_num(m.group(1))} full years")),
        (r"\b(?:ytd|year to date|this year so far|so far this year)\b",
         lambda m: TimeRange(dt.date(today.year, 1, 1).isoformat(), tomorrow.isoformat(), "Year to date")),
        (r"\b(?:qtd|quarter to date)\b",
         lambda m: TimeRange(_quarter_start(today).isoformat(), tomorrow.isoformat(), "Quarter to date")),
        (r"\b(?:mtd|month to date)\b",
         lambda m: TimeRange(_month_start(today).isoformat(), tomorrow.isoformat(), "Month to date")),
        (r"\btoday\b", lambda m: TimeRange(today.isoformat(), tomorrow.isoformat(), "Today")),
        (r"\byesterday\b",
         lambda m: TimeRange((today - dt.timedelta(days=1)).isoformat(), today.isoformat(), "Yesterday")),
        (r"\b(?:this|current)\s+week\b",
         lambda m: TimeRange((today - dt.timedelta(days=today.weekday())).isoformat(), tomorrow.isoformat(),
                             "This week")),
        (r"\b(?:last|previous|prior)\s+week\b",
         lambda m: TimeRange((today - dt.timedelta(days=today.weekday() + 7)).isoformat(),
                             (today - dt.timedelta(days=today.weekday())).isoformat(), "Last week")),
        (r"\b(?:this|current)\s+month\b",
         lambda m: TimeRange(_month_start(today).isoformat(), tomorrow.isoformat(), "This month")),
        (r"\b(?:last|previous|prior)\s+month\b",
         lambda m: TimeRange(_add_months(_month_start(today), -1).isoformat(), _month_start(today).isoformat(),
                             "Last month")),
        (r"\b(?:this|current)\s+quarter\b",
         lambda m: TimeRange(_quarter_start(today).isoformat(), tomorrow.isoformat(), "This quarter")),
        (r"\b(?:last|previous|prior)\s+quarter\b",
         lambda m: TimeRange(_add_months(_quarter_start(today), -3).isoformat(), _quarter_start(today).isoformat(),
                             "Last quarter")),
        (r"\b(?:this|current)\s+year\b",
         lambda m: TimeRange(dt.date(today.year, 1, 1).isoformat(), tomorrow.isoformat(), "This year")),
        (r"\b(?:last|previous|prior)\s+year\b",
         lambda m: TimeRange(dt.date(today.year - 1, 1, 1).isoformat(), dt.date(today.year, 1, 1).isoformat(),
                             "Last year")),
        (r"\b(?:q([1-4])\s*(?:of\s*)?((?:19|20)\d{2})|((?:19|20)\d{2})\s*q([1-4]))\b",
         lambda m: _quarter_range(int(m.group(1) or m.group(4)), int(m.group(2) or m.group(3)))),
        (r"\bq([1-4])\b", lambda m: _quarter_range(int(m.group(1)), _latest_year_for_quarter(int(m.group(1)), today))),
        (r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s+((?:19|20)\d{2})\b",
         lambda m: _month_range(MONTHS[m.group(1)], int(m.group(2)))),
        (r"\b(?:since|from)\s+((?:19|20)\d{2})\b",
         lambda m: TimeRange(dt.date(int(m.group(1)), 1, 1).isoformat(), tomorrow.isoformat(),
                             f"Since {m.group(1)}")),
        (r"\b((?:19|20)\d{2})\b",
         lambda m: TimeRange(dt.date(int(m.group(1)), 1, 1).isoformat(), dt.date(int(m.group(1)) + 1, 1, 1).isoformat(),
                             m.group(1))),
        (r"\b(?:in|for|during)\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b",
         lambda m: _month_range(MONTHS[m.group(1)], _latest_year_for_month(MONTHS[m.group(1)], today))),
    ]
    for pattern, build in patterns:
        m = re.search(pattern, t)
        if m:
            return build(m), m.group(0)
    return None, None


def _quarter_range(q: int, year: int) -> TimeRange:
    start = dt.date(year, 3 * (q - 1) + 1, 1)
    return TimeRange(start.isoformat(), _add_months(start, 3).isoformat(), f"Q{q} {year}")


def _month_range(month: int, year: int) -> TimeRange:
    start = dt.date(year, month, 1)
    return TimeRange(start.isoformat(), _add_months(start, 1).isoformat(), f"{calendar.month_name[month]} {year}")


def _latest_year_for_quarter(q: int, today: dt.date) -> int:
    return today.year if dt.date(today.year, 3 * (q - 1) + 1, 1) <= today else today.year - 1


def _latest_year_for_month(month: int, today: dt.date) -> int:
    return today.year if month <= today.month else today.year - 1


def default_trend_range(grain: str, today: dt.date) -> TimeRange:
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    if grain == "day":
        return TimeRange((today - dt.timedelta(days=29)).isoformat(), tomorrow, "Last 30 days")
    if grain == "week":
        return TimeRange((today - dt.timedelta(days=today.weekday() + 7 * 11)).isoformat(), tomorrow, "Last 12 weeks")
    if grain == "quarter":
        return TimeRange(_add_months(_quarter_start(today), -21).isoformat(), tomorrow, "Last 8 quarters")
    if grain == "year":
        return TimeRange("1900-01-01", tomorrow, "All time")
    return TimeRange(_add_months(_month_start(today), -11).isoformat(), tomorrow, "Last 12 months")


GRAIN_PATTERNS = [
    (r"\b(?:daily|by day|per day|each day|day by day|day over day)\b", "day"),
    (r"\b(?:weekly|by week|per week|each week|week over week|wow)\b", "week"),
    (r"\b(?:monthly|by month|per month|each month|month over month|mom|month by month)\b", "month"),
    (r"\b(?:quarterly|by quarter|per quarter|each quarter|quarter over quarter|qoq)\b", "quarter"),
    (r"\b(?:yearly|annually|annual|by year|per year|each year)\b", "year"),
    (r"\b(?:trend|trends|trending|over time|time series|timeline|history|historical)\b", "auto"),
]
COMPARE_PATTERNS = [
    (r"\b(?:vs\.?|versus|compared? (?:to|with)|against)\s+(?:the\s+)?(?:same\s+period\s+)?last\s+year\b", "previous_year"),
    (r"\b(?:yoy|year over year|year-over-year|same period last year)\b", "previous_year"),
    (r"\b(?:vs\.?|versus|compared? (?:to|with)|against)\s+(?:the\s+)?(?:previous|prior|last)\s+(?:period|month|quarter|week)\b",
     "previous_period"),
    (r"\b(?:growth|grow|grew|change|changed|compare|compared|comparison|increase|decrease|up or down|delta)\b",
     "previous_period"),
]


def _phrase_regex(phrase: str) -> str:
    return r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])"


class RuleParser:
    def __init__(self, model: SemanticModel, dimension_values: dict):
        """`dimension_values` maps dimension name -> list of distinct values (for filter detection)."""
        self.model = model
        self.metric_terms = sorted(
            ((s.lower(), m["name"]) for m in model.metrics.values() for s in m.get("synonyms", []) + [m["label"]]),
            key=lambda x: -len(x[0]),
        )
        self.dimension_terms = sorted(
            ((s.lower(), d["name"]) for d in model.dimensions.values() for s in d.get("synonyms", []) + [d["label"]]),
            key=lambda x: -len(x[0]),
        )
        self.value_terms = []
        for dim, values in dimension_values.items():
            for v in values:
                lv = str(v).lower()
                for form in {lv, lv + "s", lv + "es"}:
                    self.value_terms.append((form, dim, v))
        self.value_terms.sort(key=lambda x: -len(x[0]))

    def parse(self, question: str, context: SemanticQuery | None = None,
              today: dt.date | None = None) -> Interpretation:
        today = today or dt.date.today()
        raw = question.strip()
        text = " " + re.sub(r"[^\w%\-\s']", " ", raw.lower()).replace("'s", "") + " "
        text = re.sub(r"\s+", " ", text)
        notes, matched = [], {}
        score = 0.0

        def consume(span: str):
            nonlocal text
            text = text.replace(span, " ", 1)

        # 1. comparison ("vs last year") before time, so "last year" isn't read as the range.
        compare = None
        for pattern, kind in COMPARE_PATTERNS:
            m = re.search(pattern, text)
            if m:
                compare = kind
                if kind == "previous_year" or "vs" in m.group(0) or "compar" in m.group(0) or "against" in m.group(0):
                    consume(m.group(0))
                break

        # 2. time range
        time_range, span = parse_time(text, today)
        if span:
            consume(span)
            score += 1

        # 3. time grain
        grain = None
        for pattern, g in GRAIN_PATTERNS:
            m = re.search(pattern, text)
            if m:
                grain = g
                consume(m.group(0))
                score += 1
                break

        # 4. top / bottom N
        limit, order_dir, wants_rank = None, "desc", False
        m = re.search(r"\b(top|bottom|best|worst|highest|lowest|largest|smallest|biggest)\s+(\d+|"
                      + "|".join(NUMBER_WORDS) + r")\b", text)
        if m:
            limit = _num(m.group(2))
            order_dir = "asc" if m.group(1) in ("bottom", "worst", "lowest", "smallest") else "desc"
            consume(m.group(0))
            score += 0.5
        elif re.search(r"\b(lowest|least|worst|bottom|smallest|weakest|fewest)\b", text):
            order_dir, wants_rank = "asc", True
        elif re.search(r"\b(highest|most|best|top|largest|biggest|strongest|leading|leader)\b", text):
            wants_rank = True

        # 5. filters from known dimension values ("Electronics", "excluding Partner")
        filters: dict = {}
        for term, dim, value in self.value_terms:
            rx = r"(?:\b(" + "|".join(NEGATIONS) + r")\s+(?:the\s+)?)?" + _phrase_regex(term)
            m = re.search(rx, text)
            if not m:
                continue
            op = "not_in" if m.group(1) else "in"
            f = filters.setdefault((dim, op), Filter(dim, op, []))
            if value not in f.values:
                f.values.append(value)
            consume(m.group(0))
            score += 1

        # 6. metrics
        metrics = []
        for term, name in self.metric_terms:
            m = re.search(_phrase_regex(term), text)
            if m:
                if name not in metrics:
                    metrics.append(name)
                consume(m.group(0))
                score += 1.5

        if not metrics and re.search(r"\bhow many\b", lowered := raw.lower()):
            if re.search(r"\b(sell|sold|ship|shipped)\b", lowered) and "units" in self.model.metrics:
                metrics.append("units")
                score += 1

        # 7. group-by dimensions
        dims = []
        for term, name in self.dimension_terms:
            m = re.search(r"(?:\b(?:by|per|across|each|every|split by|broken down by|breakdown by|for each)\s+)?"
                          + _phrase_regex(term), text)
            if m:
                if name not in dims:
                    dims.append(name)
                consume(m.group(0))
                score += 1

        # 8. assemble, with follow-up merging
        lowered = raw.lower()
        is_follow_up = context is not None and (
            lowered.startswith(FOLLOW_UP_CUES) or (not metrics and score > 0 and len(lowered.split()) <= 6)
        )
        if is_follow_up:
            q = copy.deepcopy(context)
            notes.append("Building on your previous question.")
            if metrics:
                q.metrics = metrics
            if dims:
                q.dimensions = dims
            if re.search(r"\b(total|overall|in total|all up)\b", lowered) and not dims:
                q.dimensions = []
            if time_range:
                q.time_range = time_range
            if grain:
                q.time_grain = None if grain == "auto" and q.time_grain else (grain if grain != "auto" else "month")
            if compare:
                q.compare = compare
            for f in filters.values():
                # A new value for the same dimension replaces the old filter ("what about South?")
                q.filters = [x for x in q.filters if x.dimension != f.dimension] + [f]
            if limit:
                q.limit, q.order_dir = limit, order_dir
        else:
            q = SemanticQuery(
                metrics=metrics or ["revenue"],
                dimensions=dims,
                filters=list(filters.values()),
                time_range=time_range,
                time_grain=(self.model.time.get("default_grain", "month") if grain == "auto" else grain),
                compare=compare,
                order_dir=order_dir,
                limit=limit,
            )
            if not metrics:
                notes.append("No metric named, so I used Revenue.")

        if q.compare and not q.time_range:
            if q.compare == "previous_year":
                q.time_range = TimeRange(dt.date(today.year, 1, 1).isoformat(),
                                         (today + dt.timedelta(days=1)).isoformat(), "Year to date")
            else:
                q.time_range = TimeRange(_add_months(_month_start(today), -1).isoformat(),
                                         _month_start(today).isoformat(), "Last month")
            notes.append(f"No period given for the comparison, so I used {q.time_range.label.lower()}.")
        if q.time_grain and not q.time_range:
            q.time_range = default_trend_range(q.time_grain, today)
            notes.append(f"No period given for the trend, so I used {q.time_range.label.lower()}.")
        if limit and not q.dimensions and not q.time_grain:
            # "top 5" with no dimension: default to the most natural ranking dimension
            q.dimensions = ["product"]
            notes.append("Ranked by product, since no breakdown was named.")
        if q.limit and q.dimensions:
            q.order_by = q.metrics[0]
        if wants_rank and q.dimensions and not q.time_grain:
            q.order_by, q.order_dir = q.metrics[0], order_dir

        matched = {"metrics": metrics, "dimensions": dims, "filters": [f.__dict__ for f in filters.values()],
                   "time": time_range.label if time_range else None, "grain": grain, "compare": compare}
        leftovers = [w for w in text.split() if len(w) > 2 and w not in STOPWORDS]
        confidence = min(1.0, score / 3.0) * (0.6 if len(leftovers) > 3 else 1.0)
        if not metrics and not dims and not filters and not time_range and not is_follow_up:
            confidence = 0.0
        return Interpretation(q, confidence, notes, is_follow_up, matched)


STOPWORDS = set("""
what whats was were is are the show me give tell how much many did does do our we for of in on at to and
with total overall all please can you could would list find get see which who where when there their this
that those these a an by per vs between from into over across each every it its be been being has have had
any some value values number numbers figure figures report data look like doing perform performance breakdown
split broken down level amount sum versus about latest current recent so far rank ranked ranking compare
growth grow grew change changed increase decrease delta highest lowest most least best worst top bottom had now just only
""".split())
