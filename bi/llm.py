"""Optional Claude interpreter for questions the rule parser isn't confident about.

Claude never sees data and never writes SQL. It fills in a JSON SemanticQuery
whose metric and dimension names are constrained (by JSON schema enums) to the
semantic model, and the result goes through the same validator as everything else.

Enabled when the `anthropic` package is installed and credentials are available
(ANTHROPIC_API_KEY or an `ant auth login` profile). Set BI_LLM=off to disable.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from .semantic import GRAINS, SemanticModel, SemanticQuery

MODEL = os.environ.get("BI_MODEL", "claude-opus-5-5")

try:
    import anthropic
except ImportError:  # the app works fully without it
    anthropic = None


def _schema(model: SemanticModel, dimension_values: dict) -> dict:
    dims = list(model.dimensions)
    nullable = lambda s: {"anyOf": [s, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "answerable": {"type": "boolean",
                           "description": "False if the question cannot be answered with this model."},
            "reason": {"type": "string", "description": "If not answerable, a short plain-English reason."},
            "metrics": {"type": "array", "items": {"type": "string", "enum": list(model.metrics)}},
            "dimensions": {"type": "array", "items": {"type": "string", "enum": dims}},
            "filters": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "enum": dims},
                    "op": {"type": "string", "enum": ["in", "not_in"]},
                    "values": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["dimension", "op", "values"], "additionalProperties": False}},
            "time_range": nullable({
                "type": "object",
                "properties": {"start": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                               "end": {"type": "string", "description": "YYYY-MM-DD exclusive"},
                               "label": {"type": "string", "description": "Short human label, e.g. 'Last quarter'"}},
                "required": ["start", "end", "label"], "additionalProperties": False}),
            "time_grain": nullable({"type": "string", "enum": list(GRAINS)}),
            "compare": nullable({"type": "string", "enum": ["previous_period", "previous_year"]}),
            "order_dir": {"type": "string", "enum": ["asc", "desc"]},
            "limit": nullable({"type": "integer"}),
        },
        "required": ["answerable", "reason", "metrics", "dimensions", "filters", "time_range",
                     "time_grain", "compare", "order_dir", "limit"],
        "additionalProperties": False,
    }


def _system(model: SemanticModel, dimension_values: dict) -> str:
    # Stable across requests (no dates, sorted values) so it can be prompt-cached.
    lines = [
        "You translate business questions into a structured query over a governed semantic model.",
        "Use only the metrics, dimensions and dimension values listed. Map loose wording to the closest",
        "metric (e.g. 'sales' -> revenue). Only set filter values that appear in the value lists.",
        "Resolve relative dates against the 'Today' line in the user message; ranges are [start, end).",
        "If the question asks for something the model cannot answer (e.g. a metric that doesn't exist),",
        "set answerable=false and explain briefly what is available instead.",
        "",
        f"Model: {model.label} - {model.spec.get('description', '')}",
        "Metrics:",
    ]
    for m in model.metrics.values():
        lines.append(f"- {m['name']}: {m['label']}. {m.get('description', '')}")
    lines.append("Dimensions (with known values):")
    for d in model.dimensions.values():
        vals = ", ".join(sorted(map(str, dimension_values.get(d["name"], []))))
        lines.append(f"- {d['name']}: {d['label']}. Values: {vals}")
    return "\n".join(lines)


class ClaudeInterpreter:
    def __init__(self, model: SemanticModel, dimension_values: dict):
        self.model = model
        self.dimension_values = dimension_values
        self.client = None
        if anthropic is not None and os.environ.get("BI_LLM", "on") != "off":
            try:
                self.client = anthropic.Anthropic()
            except Exception:
                self.client = None
        self.schema = _schema(model, dimension_values)
        self.system = _system(model, dimension_values)

    @property
    def available(self) -> bool:
        return self.client is not None

    def interpret(self, question: str, context: SemanticQuery | None = None,
                  today: dt.date | None = None) -> tuple:
        """Return (SemanticQuery | None, message). None means 'not answerable'."""
        today = today or dt.date.today()
        user = f"Today: {today.isoformat()}\n"
        if context is not None:
            user += f"Previous query (the user may be following up on it): {json.dumps(context.to_dict())}\n"
        user += f"Question: {question}"
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": self.schema}},
            # Server-side refusal fallback: reroutes a declined request to a suitable model.
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason == "refusal":
            return None, "I can't help with that question."
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        if not data.pop("answerable", True):
            return None, data.get("reason") or "That isn't something the data model covers yet."
        data.pop("reason", None)
        return SemanticQuery.from_dict(data), ""
