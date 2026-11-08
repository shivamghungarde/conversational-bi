"""Draft a semantic model from a table's columns, so a CSV/Excel file is askable immediately.

The draft is saved next to the file as `<name>.semantic.json`; edit labels,
synonyms and metric definitions there and it is picked up on the next start.
"""
from __future__ import annotations

import re

CURRENCY = re.compile(r"revenue|sales|amount|cost|price|value|fee|income|spend|margin_amt|profit|billing|invoice|usd|eur|gbp|inr")
PERCENT = re.compile(r"pct|percent|ratio|util|share|margin_pct|_%")
ID_LIKE = re.compile(r"(^|_)(id|code|key|number|no|num)$")


def _words(label: str) -> str:
    return re.sub(r"[_\s]+", " ", label).strip().lower()


def _plural(word: str) -> str:
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy")):
        return word[:-1] + "ies"
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


def infer(table: str, columns: list, label: str, row_count: int) -> dict:
    alias = "t"
    metrics, dimensions = [], []
    time_dim = None
    for c in columns:
        c = dict(c, label=re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", c["label"]).strip() or c["label"])  # "Revenue ($)"
        name, words = c["name"], _words(c["label"])
        col = f'{alias}."{name}"'
        if c["type"] == "date":
            # First date column wins, unless an earlier one isn't named like a date ("Order Date" beats "Ship Date").
            if time_dim is None or ("date" in name and "date" not in time_dim["name"]):
                time_dim = {"name": name, "label": c["label"], "sql": col, "default_grain": "month"}
            continue
        if c["type"] == "number" and not ID_LIKE.search(name):
            currency = c.get("currency")
            fmt = (f"currency_{currency}" if currency and currency != "USD" else "currency") if currency or CURRENCY.search(name) else "number"
            if re.search(r"(?:bill|hourly|unit|daily|cost)_?(?:rate|price)$|^price$", name):
                metrics.append({"name": name, "label": c["label"], "sql": f"AVG({col})",
                                "format": "currency" if not currency else ("currency" if currency == "USD" else f"currency_{currency}"),
                                "additive": False, "description": "Unweighted average; confirm the weighting before business use.",
                                "synonyms": [words, f"average {words}"]})
                continue
            if c.get("percent") or PERCENT.search(name):
                metrics.append({"name": name, "label": c["label"], "sql": f"AVG({col})", "format": "percent",
                                "additive": False, "description": f"Average of {c['label']}.",
                                "synonyms": [words, f"average {words}", f"avg {words}"]})
                continue
            metrics.append({"name": name, "label": c["label"], "sql": f"SUM({col})", "format": fmt,
                            "additive": True, "description": f"Total of {c['label']}.",
                            "synonyms": [words, f"total {words}"]})
            continue
        # text (or id-like numbers): a dimension if it groups rows, a distinct count otherwise
        if c["distinct"] and (c["distinct"] <= 500 or c["distinct"] <= row_count * 0.5):
            dimensions.append({"name": name, "label": c["label"], "plural": _plural(words), "sql": col,
                               "join": None, "synonyms": [words, _plural(words)]})
        if ID_LIKE.search(name) or c["distinct"] > 500:
            base = re.sub(ID_LIKE, "", name).strip("_") or name
            metrics.append({"name": f"distinct_{name}", "label": f"Number of {_words(base)}s",
                            "sql": f"COUNT(DISTINCT {col})", "format": "number", "additive": False,
                            "description": f"Distinct values of {c['label']}.",
                            "synonyms": [f"number of {_words(base)}s", f"{_words(base)} count",
                                         f"how many {_words(base)}s"]})

    metrics.append({"name": "row_count", "label": "Records", "sql": "COUNT(*)", "format": "number", "additive": True,
                    "description": "Number of rows.", "synonyms": ["records", "rows", "record count", "entries",
                                                                   "number of records", "count"]})
    names = {m["name"] for m in metrics}
    # Utilization-shaped data: billable vs available/capacity hours becomes a governed ratio metric.
    billable = next((n for n in names if "billable" in n and "non" not in n), None)
    capacity = next((n for n in names if "available" in n or "capacity" in n), None)
    if billable and capacity:
        b = next(m for m in metrics if m["name"] == billable)
        a = next(m for m in metrics if m["name"] == capacity)
        metrics.insert(0, {"name": "utilization", "label": "Utilization",
                           "sql": f"{b['sql']} * 1.0 / NULLIF({a['sql']}, 0)", "format": "percent",
                           "additive": False, "ratio": {"numerator": billable, "denominator": capacity},
                           "description": f"{b['label']} as a share of {a['label']}.",
                           "synonyms": ["utilization", "utilisation", "utilization rate", "billability",
                                        "chargeability"]})

    featured = [m["name"] for m in metrics if m["name"] != "row_count"][:4] or ["row_count"]
    return {
        "name": table,
        "label": label,
        "description": f"Draft model from {label}. Review aggregation, units and date field before business use.",
        "draft": True,
        "base_table": f'"{table}"',
        "base_alias": alias,
        "joins": {},
        "time_dimension": time_dim,
        "featured_metrics": featured,
        "metrics": metrics,
        "dimensions": dimensions,
        "ambiguous_terms": {},
        "sample_queries": sample_questions(metrics, dimensions, time_dim),
    }


def sample_questions(metrics: list, dimensions: list, time_dim) -> list:
    ms = [m for m in metrics if m["name"] != "row_count"] or metrics
    ds = dimensions or []
    qs = []
    m0 = ms[0]["label"]
    if ds:
        qs.append(f"{m0} by {ds[0]['label'].lower()}")
        qs.append(f"Top 5 {ds[0]['plural']} by {m0.lower()}")
    if time_dim:
        qs.append(f"Monthly {m0.lower()} trend")
        qs.append(f"What was {m0.lower()} last month?")
        qs.append(f"Why did {m0.lower()} change last month?")
    if len(ms) > 1 and len(ds) > 1:
        qs.append(f"{ms[1]['label']} by {ds[1]['label'].lower()}")
    if len(ds) > 2:
        qs.append(f"{m0} by {ds[2]['label'].lower()}")
    qs.append("How many records are there?")
    return qs
