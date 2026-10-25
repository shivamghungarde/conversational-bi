""""Why did it change?": what happened, how it happened, and why (driver analysis).

For an additive metric (revenue, hours) each member's contribution is simply
its own change; contributions sum exactly to the total change.

For a ratio metric (utilization = billable / available) the change in each
member is split into two effects that also sum exactly to the total change:
  rate effect = (r1 - r0) * w1          the member's own ratio moved
  mix effect  = (r0 - R0) * (w1 - w0)   capacity moved toward a member that runs
                                        above/below the overall average R0
where r is the member's ratio and w its share of the denominator.
"""
from __future__ import annotations

import copy
import datetime as dt

from .narrate import change_phrase, fmt, point_or_pct
from .semantic import SemanticModel, SemanticQuery, TimeRange, _month_span_label, change, shift_range

MAX_MEMBERS = 60


def _q(q: SemanticQuery, **kw) -> SemanticQuery:
    out = copy.deepcopy(q)
    out.explain, out.compare, out.limit, out.order_by = False, None, None, None
    out.dimensions, out.time_grain = [], None
    for k, v in kw.items():
        setattr(out, k, v)
    return out


def _fetch(model, con, q, tr, extra, statements) -> list:
    sql, params, cols = model.compile(q, time_range=tr, extra_metrics=extra)
    statements.append({"sql": sql, "params": params})
    return [dict(zip(cols, r)) for r in con.execute(sql, params).fetchall()]


def analyse(model: SemanticModel, con, q: SemanticQuery, claimed: str | None = None,
            today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    m = q.metrics[0]
    meta = model.metrics[m]
    ratio = meta.get("ratio")
    extra = (ratio["numerator"], ratio["denominator"]) if ratio else ()
    cur_tr = q.time_range
    prev_tr = shift_range(cur_tr, q.compare or "previous_period")
    statements: list = []

    base = _q(q)
    cur = (_fetch(model, con, base, cur_tr, extra, statements) or [{}])[0]
    prev = (_fetch(model, con, base, prev_tr, extra, statements) or [{}])[0]
    delta = change(model, m, cur.get(m), prev.get(m))
    abs_delta = (cur.get(m) or 0) - (prev.get(m) or 0)

    # ---- drivers by every dimension that isn't pinned by a filter ----
    pinned = {f.dimension for f in q.filters if f.op == "in" and len(f.values) == 1}
    drivers, ranking = {}, []
    # Some breakdowns are circular for some metrics (utilization "by client" just says the bench isn't billed),
    # so a metric can name the dimensions worth explaining it by.
    for d in meta.get("explain_by") or list(model.dimensions):
        if d in pinned or d not in model.dimensions:
            continue
        dq = _q(q, dimensions=[d], limit=1000)
        c_rows = {r[d]: r for r in _fetch(model, con, dq, cur_tr, extra, statements)}
        p_rows = {r[d]: r for r in _fetch(model, con, dq, prev_tr, extra, statements)}
        members = []
        for key in set(c_rows) | set(p_rows):
            c, p = c_rows.get(key, {}), p_rows.get(key, {})
            item = {"member": key, "current": c.get(m), "previous": p.get(m),
                    "change": change(model, m, c.get(m), p.get(m))}
            if ratio:
                num, den = ratio["numerator"], ratio["denominator"]
                d1, d0 = c.get(den) or 0, p.get(den) or 0
                D1, D0 = cur.get(den) or 0, prev.get(den) or 0
                w1 = d1 / D1 if D1 else 0
                w0 = d0 / D0 if D0 else 0
                r1 = (c.get(num) or 0) / d1 if d1 else 0
                r0 = (p.get(num) or 0) / d0 if d0 else 0
                R0 = (prev.get(num) or 0) / D0 if D0 else 0
                item.update(rate=(r1 - r0) * w1 if d1 and d0 else 0.0,
                            mix=(r0 - R0) * (w1 - w0) + (r1 * w1 if d1 and not d0 else 0.0),
                            share_now=w1, share_before=w0)
                item["contribution"] = item["rate"] + item["mix"]
            else:
                item["contribution"] = (c.get(m) or 0) - (p.get(m) or 0)
            members.append(item)
        members.sort(key=lambda x: -abs(x["contribution"]))
        same_dir = [x["contribution"] for x in members if abs_delta and x["contribution"] * abs_delta > 0]
        score = (sum(same_dir[:2]) / abs_delta) if abs_delta else 0.0
        # Penalise very granular dimensions slightly: "two consultants" explains less than "two practices".
        score *= 1.0 if len(members) <= 12 else 0.85
        ranking.append({"dimension": d, "label": model.dimensions[d]["label"], "score": round(score, 3),
                        "members": len(members)})
        drivers[d] = members[:MAX_MEMBERS]
    ranking.sort(key=lambda x: -x["score"])

    trend = _trend(model, con, q, cur_tr, prev_tr, statements)
    sections = _narrate(model, m, cur, prev, delta, abs_delta, cur_tr, prev_tr, drivers, ranking, trend, claimed,
                        ratio, today)
    return {
        "type": "explain",
        "metric": m,
        "current": {"value": cur.get(m), "range": dict(cur_tr.__dict__, label=_period_name(cur_tr)),
                    **({k: cur.get(k) for k in extra})},
        "previous": {"value": prev.get(m), "range": dict(prev_tr.__dict__, label=_period_name(prev_tr)),
                     **({k: prev.get(k) for k in extra})},
        "change": delta,
        "change_kind": "pts" if model.is_percent(m) else "pct",
        "sections": sections,
        "answer": " ".join(s["text"] for s in sections),
        "drivers": drivers,
        "ranking": ranking,
        "primary_dimension": ranking[0]["dimension"] if ranking else None,
        "decomposition": "rate_mix" if ratio else ("additive" if meta.get("additive") else "member_change"),
        "trend": trend,
        "sql": statements,
    }


def _trend(model, con, q, cur_tr, prev_tr, statements) -> dict:
    start = dt.date.fromisoformat(prev_tr.start)
    end = dt.date.fromisoformat(cur_tr.end)
    span = (dt.date.fromisoformat(cur_tr.end) - dt.date.fromisoformat(cur_tr.start)).days
    if span <= 10:
        grain, lookback = "day", dt.timedelta(days=21)
    elif span <= 120:
        grain, lookback = "month", dt.timedelta(days=150)
    else:
        grain, lookback = "quarter", dt.timedelta(days=550)
    window = TimeRange((start - lookback).replace(day=1 if grain != "day" else (start - lookback).day).isoformat(),
                       end.isoformat(), "trend window")
    tq = _q(q, time_grain=grain)
    rows = _fetch(model, con, tq, window, (), statements)
    m = q.metrics[0]

    def segment(period: str):
        first = _bucket_start(period, grain)
        if first is None:
            return None
        if cur_tr.start <= first < cur_tr.end or (first <= cur_tr.start < _bucket_end(first, grain)):
            return "current"
        if prev_tr.start <= first < prev_tr.end or (first <= prev_tr.start < _bucket_end(first, grain)):
            return "previous"
        return None

    return {"grain": grain, "rows": [{"period": r["period"], "value": r[m], "segment": segment(r["period"])}
                                     for r in rows]}


def _bucket_start(period: str, grain: str):
    try:
        if grain == "day":
            return period[:10]
        if grain == "month":
            return f"{period}-01"
        if grain == "quarter":
            y, qn = period.split("-Q")
            return dt.date(int(y), 3 * (int(qn) - 1) + 1, 1).isoformat()
    except ValueError:
        return None
    return None


def _bucket_end(first: str, grain: str) -> str:
    d = dt.date.fromisoformat(first)
    if grain == "day":
        return (d + dt.timedelta(days=1)).isoformat()
    months = 1 if grain == "month" else 3
    y, mo = divmod(d.year * 12 + d.month - 1 + months, 12)
    return dt.date(y, mo + 1, 1).isoformat()


def _period_name(tr: TimeRange) -> str:
    start, end = dt.date.fromisoformat(tr.start), dt.date.fromisoformat(tr.end)
    if start.day == 1 and end.day == 1:
        months = (end.year - start.year) * 12 + end.month - start.month
        named = _month_span_label(start, months)
        if named != "previous period":
            return named
    label = tr.label or f"{tr.start} to {tr.end}"
    return label if label[:1].isupper() and not label.lower().startswith(("last", "this")) else label.lower()


def _narrate(model, m, cur, prev, delta, abs_delta, cur_tr, prev_tr, drivers, ranking, trend, claimed, ratio,
             today) -> list:
    meta = model.metrics[m]
    label, kind = meta["label"], meta["format"]
    pct = model.is_percent(m)
    sections = []

    # ---- what happened ----
    if cur.get(m) is None or prev.get(m) is None:
        return [{"title": "What happened", "text": f"There isn't enough data in both periods to explain {label.lower()}."}]
    flat = abs(delta or 0) < (0.002 if pct else 0.01)
    verb = "was essentially flat" if flat else ("rose" if abs_delta > 0 else "fell")
    actual = "up" if abs_delta > 0 else "down"
    lead = ""
    if claimed and not flat and claimed != actual:
        lead = f"Actually, {label.lower()} didn't {'drop' if claimed == 'down' else 'rise'}: "
    what = (f"{lead}{label if not lead else label.lower()} {verb}"
            + ("" if flat else f" {point_or_pct(delta, pct)}")
            + f", from {fmt(prev[m], kind)} in {_period_name(prev_tr)} to {fmt(cur[m], kind)} in {_period_name(cur_tr)}.")
    if ratio:
        num, den = ratio["numerator"], ratio["denominator"]
        n_ch, d_ch = change(model, num, cur.get(num), prev.get(num)), change(model, den, cur.get(den), prev.get(den))
        what += (f" {model.metrics[num]['label']} were {change_phrase(n_ch)} while "
                 f"{model.metrics[den]['label'].lower()} were {change_phrase(d_ch)}.")
    sections.append({"title": "What happened", "text": what[0].upper() + what[1:]})

    # ---- how it happened (shape of the trend) ----
    rows = [r for r in trend["rows"] if r["value"] is not None]
    before = [r for r in rows if r["segment"] is None and r["period"] < (next(
        (x["period"] for x in rows if x["segment"] == "previous"), "9999"))]
    how = ""
    if len(before) >= 2:
        vals = [r["value"] for r in before]
        lo, hi = min(vals), max(vals)
        steady = (hi - lo) < (0.02 if pct else 0.08 * max(abs(hi), 1e-9))
        if steady:
            how = (f"Before this, {label.lower()} had been steady at {fmt(lo, kind)}–{fmt(hi, kind)} "
                   f"for {len(before)} {trend['grain']}s, so this is a break from the pattern, not a long slide.")
        elif all(b["value"] >= a["value"] for a, b in zip(before, before[1:])):
            how = f"It had already been rising for {len(before)} {trend['grain']}s before this."
        elif all(b["value"] <= a["value"] for a, b in zip(before, before[1:])):
            how = f"It continues a decline that has run for {len(before)} {trend['grain']}s."
        else:
            how = f"Over the prior {len(before)} {trend['grain']}s it moved between {fmt(lo, kind)} and {fmt(hi, kind)}."
    if cur_tr.end > today.isoformat():
        how += (" " if how else "") + f"Note that {_period_name(cur_tr)} is still in progress."
    if how:
        sections.append({"title": "How it happened", "text": how})

    # ---- why (drivers) ----
    if flat or not ranking or not abs_delta:
        return sections
    top_dim = ranking[0]["dimension"]
    dlabel = model.dimensions[top_dim]["label"].lower()
    top = [x for x in drivers[top_dim] if x["contribution"] * abs_delta > 0][:2]
    unit = (lambda v: f"{v * 100:+.1f} pts") if pct else (lambda v: fmt(v, kind) if v < 0 else "+" + fmt(v, kind))
    parts = []
    for x in top:
        share = x["contribution"] / abs_delta * 100
        if ratio and abs(x.get("mix", 0)) > abs(x.get("rate", 0)):
            direction = "more" if x["share_now"] > x["share_before"] else "less"
            parts.append(f"{x['member']} ({unit(x['contribution'])}): {direction} of the capacity sat here "
                         f"({x['share_before'] * 100:.0f}% → {x['share_now'] * 100:.0f}% of "
                         f"{model.metrics[ratio['denominator']]['label'].lower()}), and it runs "
                         f"{'below' if (x['current'] or 0) < (cur.get(m) or 0) else 'above'} the average at "
                         f"{fmt(x['current'], kind)}")
        elif x["previous"] is not None and x["current"] is not None:
            share_txt = f", {share:.0f}% of the change" if 0 < share <= 100 else ""
            parts.append(f"{x['member']} ({unit(x['contribution'])}{share_txt}): "
                         f"{label.lower()} went from {fmt(x['previous'], kind)} to {fmt(x['current'], kind)}")
        else:
            parts.append(f"{x['member']} ({unit(x['contribution'])})")
    why = f"The change is concentrated by {dlabel}. " + "; ".join(parts) + "." if parts else ""
    offset = next((x for x in drivers[top_dim] if x["contribution"] * abs_delta < 0), None)
    if offset and abs(offset["contribution"]) > abs(abs_delta) * 0.08:
        why += f" Partly offset by {offset['member']} ({unit(offset['contribution'])})."
    for r in ranking[1:3]:
        best = next((x for x in drivers[r["dimension"]] if x["contribution"] * abs_delta > 0), None)
        if best and r["score"] > 0.25:
            why += f" By {r['label'].lower()}, the biggest factor is {best['member']} ({unit(best['contribution'])})."
    if why:
        sections.append({"title": "Why it happened", "text": why})
    return sections
