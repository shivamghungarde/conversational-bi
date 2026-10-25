"""Pick which connected data source should answer a question ("Auto" mode).

Each source's own semantic model scores the question (how many of its
measures, breakdowns, values and periods the question mentions). Follow-ups
stay with the source that answered the previous question. When two sources
fit about equally well, the user is asked which one to use; nothing is guessed.
"""
from __future__ import annotations

import datetime as dt

from .semantic import SemanticQuery

TIE_MARGIN = 0.75


def info(engine) -> dict:
    return {"id": engine.id, "label": engine.model.label, "source": engine.source.describe(),
            "kind": engine.source.kind, "type": engine.source.type_word, "live": engine.source.live}


def score(engine, question: str, context: SemanticQuery | None, today: dt.date) -> tuple:
    engine._refresh_values()
    interp = engine.rules.parse(question, context, today=today)
    m = interp.matched
    s = (2.0 * len(m["metrics"]) + 1.0 * len(m["dimensions"]) + 1.0 * len(m["filters"])
         + (0.5 if m["time"] else 0.0) + interp.confidence)
    if any(a["kind"] == "metric" and a.get("term") for a in interp.ambiguities):
        s += 1.0  # the source knows the vague word ("performance"): it can ask a useful clarifying question
    if interp.follow_up:
        s += 3.0
    return s, interp


def route(engines: list, question: str, context: dict | None, context_dataset: str | None,
          today: dt.date | None = None):
    """Return (engine, None) for a clear choice, or (None, clarify_response) when sources tie."""
    today = today or dt.date.today()
    if len(engines) == 1:
        return engines[0], None
    scored = []
    for e in engines:
        ctx = SemanticQuery.from_dict(context) if context and context_dataset == e.id else None
        s, _ = score(e, question, ctx, today)
        scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    if best_score <= 0.0:
        prev = next((e for e in engines if e.id == context_dataset), None)
        return prev or best, None
    tied = [e for s, e in scored if best_score - s < TIE_MARGIN and s > 1.0]
    if len(tied) > 1:
        return None, {
            "ok": True, "type": "clarify", "ambiguity": "dataset", "question": question,
            "prompt": "More than one connected source can answer this. Which should I use?",
            "options": [{"label": f"{e.model.label} · {e.source.type_word}", "hint": e.source.describe(),
                         "clarification": {"dataset": e.id}} for e in tied],
        }
    return best, None
