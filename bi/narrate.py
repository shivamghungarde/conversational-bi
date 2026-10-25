"""Plain-English answers, built deterministically from the result set.

Templates (not an LLM) write the headline so numbers are always exactly the
numbers in the table. That keeps answers instant and free of hallucination.
"""
from __future__ import annotations

from .semantic import SemanticModel, SemanticQuery


def fmt(value, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "percent":
        return f"{value * 100:.1f}%"
    if kind == "currency":
        a = abs(value)
        sign = "-" if value < 0 else ""
        if a >= 1e9:
            return f"{sign}${a / 1e9:.2f}B"
        if a >= 1e6:
            return f"{sign}${a / 1e6:.2f}M"
        if a >= 1e4:
            return f"{sign}${a / 1e3:.1f}K"
        return f"{sign}${a:,.2f}"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def change_phrase(change) -> str:
    if change is None:
        return ""
    if abs(change) < 0.0005:
        return "flat"
    return f"{'up' if change > 0 else 'down'} {abs(change) * 100:.1f}%"


def describe(model: SemanticModel, q: SemanticQuery) -> list:
    """Interpretation chips: what the system understood, in business terms."""
    chips = [{"kind": "metric", "label": model.metrics[m]["label"], "value": m} for m in q.metrics]
    chips += [{"kind": "dimension", "label": f"by {model.dimensions[d]['label']}", "value": d} for d in q.dimensions]
    if q.time_grain:
        chips.append({"kind": "grain", "label": f"{q.time_grain.capitalize()}ly".replace("Dayly", "Daily"),
                      "value": q.time_grain})
    if q.time_range:
        chips.append({"kind": "time", "label": q.time_range.label or f"{q.time_range.start} to {q.time_range.end}",
                      "value": q.time_range.label})
    for f in q.filters:
        label = model.dimensions[f.dimension]["label"]
        verb = "is not" if f.op == "not_in" else "is"
        chips.append({"kind": "filter", "label": f"{label} {verb} {', '.join(map(str, f.values))}",
                      "value": f.dimension, "op": f.op})
    if q.compare:
        chips.append({"kind": "compare",
                      "label": "vs last year" if q.compare == "previous_year" else "vs previous period",
                      "value": q.compare})
    if q.limit:
        chips.append({"kind": "limit", "label": f"{'Top' if q.order_dir == 'desc' else 'Bottom'} {q.limit}",
                      "value": q.limit})
    return chips


def answer(model: SemanticModel, q: SemanticQuery, result: dict) -> str:
    rows, totals = result["rows"], result["totals"] or {}
    m = q.metrics[0]
    metric = model.metrics[m]
    mlabel, kind = metric["label"], metric["format"]
    when = f" {_when(q)}" if q.time_range else " across all available data"
    scope = _scope(model, q)

    if not rows or all(r.get(m) is None for r in rows):
        return f"I couldn't find any data for {mlabel.lower()}{scope}{when}. Try a wider time range or fewer filters."

    # Single number
    if not q.dimensions and not q.time_grain:
        r = rows[0]
        parts = [f"{model.metrics[x]['label']} {_verb(q)} {fmt(r[x], model.metrics[x]['format'])}" for x in q.metrics]
        text = _join(parts) + f"{scope}{when}."
        if q.compare:
            prev_label = "the same period last year" if q.compare == "previous_year" else "the previous period"
            ch = r.get(f"{m}__change")
            if ch is not None:
                text += f" That's {change_phrase(ch)} from {fmt(r.get(f'{m}__prev'), kind)} in {prev_label}."
            else:
                text += f" There's no data for {prev_label} to compare against."
        return _capital(text)

    # Trend
    if q.time_grain:
        if q.dimensions:
            dim = q.dimensions[0]
            by_member: dict = {}
            for r in rows:
                by_member[r[dim]] = by_member.get(r[dim], 0) + (r[m] or 0)
            leader = max(by_member, key=by_member.get)
            total = totals.get(m)
            if not metric.get("additive"):
                # Summing a ratio across periods is meaningless: compare members in the latest period instead.
                latest = max(r["period"] for r in rows)
                last = {r[dim]: r[m] for r in rows if r["period"] == latest and r[m] is not None}
                top = max(last, key=last.get)
                return _capital(
                    f"{q.time_grain.capitalize()}ly {mlabel.lower()} across {len(by_member)} "
                    f"{model.dimensions[dim].get('plural', dim)}{scope}{when}. In {latest}, {top} is highest at "
                    f"{fmt(last[top], kind)}" + (f" and overall {mlabel.lower()} is {fmt(total, kind)}." if total is not None else ".")
                )
            return _capital(
                f"{q.time_grain.capitalize()}ly {mlabel.lower()} across {len(by_member)} "
                f"{model.dimensions[dim].get('plural', dim)}{scope}{when}"
                + (f" totals {fmt(total, kind)}" if total is not None and metric.get("additive") else "")
                + f". {leader} contributes the most"
                + (f" ({fmt(by_member[leader], kind)})." if metric.get("additive") else ".")
            )
        series = [r for r in rows if r.get(m) is not None]
        in_progress = series[-1]["period"] == _current_bucket(q) and len(series) > 2
        complete = series[:-1] if in_progress else series
        first, last = complete[0], complete[-1]
        peak = max(complete, key=lambda r: r[m])
        low = min(complete, key=lambda r: r[m])
        delta = change_phrase((last[m] - first[m]) / abs(first[m])) if first[m] else ""
        text = (f"{mlabel} went from {fmt(first[m], kind)} in {first['period']} to {fmt(last[m], kind)} "
                f"in {last['period']}" + (f" ({delta})" if delta else "") + f"{scope}. ")
        text += f"The peak was {peak['period']} at {fmt(peak[m], kind)}"
        text += f" and the low was {low['period']} at {fmt(low[m], kind)}." if low is not peak else "."
        if in_progress:
            text += f" {series[-1]['period']} is still in progress at {fmt(series[-1][m], kind)} so far."
        return _capital(text)

    # Breakdown / ranking
    dim = q.dimensions[0]
    dlabel = model.dimensions[dim]["label"]
    plural = model.dimensions[dim].get("plural", dlabel.lower() + "s")
    ordered = sorted([r for r in rows if r.get(m) is not None], key=lambda r: r[m], reverse=q.order_dir != "asc")
    top = ordered[0]
    total = totals.get(m)
    share = lambda r: (f" ({r[m] / total * 100:.0f}% of the total)"
                       if metric.get("additive") and total else "")
    label = lambda r: " / ".join(str(r[d]) for d in q.dimensions)
    lead_word = "is lowest" if q.order_dir == "asc" else "leads"
    text = f"{label(top)} {lead_word} with {fmt(top[m], kind)} {mlabel.lower()}{share(top)}{scope}{when}."
    if len(ordered) > 1:
        second = ordered[1]
        text += f" Next is {label(second)} at {fmt(second[m], kind)}"
        if len(ordered) > 2:
            tail = ordered[-1]
            text += f", and {label(tail)} is at the {'top' if q.order_dir == 'asc' else 'bottom'} with {fmt(tail[m], kind)}"
        text += "."
    if q.compare:
        movers = [r for r in ordered if r.get(f"{m}__change") is not None]
        if movers:
            best = max(movers, key=lambda r: r[f"{m}__change"])
            worst = min(movers, key=lambda r: r[f"{m}__change"])
            text += f" Biggest gain: {label(best)} ({change_phrase(best[f'{m}__change'])})."
            if worst is not best:
                text += f" Weakest: {label(worst)} ({change_phrase(worst[f'{m}__change'])})."
        if totals.get(f"{m}__change") is not None and metric.get("additive"):
            text += f" Overall {mlabel.lower()} is {change_phrase(totals[f'{m}__change'])}."
    elif total is not None and len(q.dimensions) == 1 and not q.limit:
        if metric.get("additive"):
            text += f" Across all {len(ordered)} {plural}, the total is {fmt(total, kind)}."
        else:
            text += f" Across all {len(ordered)} {plural}, overall {mlabel.lower()} is {fmt(total, kind)}."
    elif q.limit and total is not None and metric.get("additive"):
        covered = sum(r[m] for r in ordered)
        text += f" These {len(ordered)} account for {covered / total * 100:.0f}% of {mlabel.lower()}."
    return _capital(text)


def suggestions(model: SemanticModel, q: SemanticQuery) -> list:
    """Next questions a leader might ask; each is a one-click custom breakdown."""
    out = []
    used = set(q.dimensions) | {f.dimension for f in q.filters}
    for d in model.dimensions.values():
        if d["name"] not in used:
            out.append({"label": f"Break down by {d['label'].lower()}",
                        "patch": {"dimensions": [d["name"]], "time_grain": None}})
    if not q.time_grain:
        out.append({"label": "Show as a monthly trend", "patch": {"time_grain": "month", "compare": None}})
    if not q.compare and q.time_range and not q.time_grain:
        out.append({"label": "Compare with previous period", "patch": {"compare": "previous_period"}})
        out.append({"label": "Compare with last year", "patch": {"compare": "previous_year"}})
    return out[:6]


def _verb(q: SemanticQuery) -> str:
    if q.time_range and q.time_range.label.lower().startswith(("this", "today", "year to", "quarter to", "month to")):
        return "is"
    return "was" if q.time_range else "is"


def _when(q: SemanticQuery) -> str:
    label = q.time_range.label
    if label[:1].isdigit() or label.startswith(("Q", "January", "February", "March", "April", "May", "June", "July",
                                                 "August", "September", "October", "November", "December")):
        return f"in {label}"
    if label.startswith(("Since", "Last", "This", "Today", "Yesterday", "Year to", "Quarter to", "Month to")):
        return label.lower()
    if label == "All time":
        return "across all time"
    return f"for {label.lower()}" if label else f"from {q.time_range.start} to {q.time_range.end}"


def _scope(model: SemanticModel, q: SemanticQuery) -> str:
    if not q.filters:
        return ""
    parts = []
    for f in q.filters:
        vals = " and ".join(map(str, f.values))
        parts.append(f"excluding {vals}" if f.op == "not_in" else f"for {vals}")
    return " " + ", ".join(parts)


def _current_bucket(q: SemanticQuery):
    import datetime as dt
    t = dt.date.today()
    return {"month": t.strftime("%Y-%m"), "year": t.strftime("%Y"),
            "quarter": f"{t.year}-Q{(t.month + 2) // 3}", "day": t.isoformat(),
            "week": t.strftime("%Y-W%W")}.get(q.time_grain)


def _join(parts: list) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _capital(s: str) -> str:
    return s[:1].upper() + s[1:]
