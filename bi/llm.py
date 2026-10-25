"""Optional on-premises LLM interpreter for phrasings the rule parser can't handle.

Questions must not leave the network, so this only talks to a self-hosted,
OpenAI-compatible chat endpoint (Ollama, vLLM, LM Studio, llama.cpp server)
and refuses any endpoint that doesn't resolve to a loopback or private address.

The model never sees data and never writes SQL. It fills in a JSON
SemanticQuery whose metric/dimension names are constrained by JSON-schema enums,
and the result goes through the same validator as everything else.

Off unless configured:
  BI_LOCAL_LLM_URL     e.g. http://localhost:11434   (Ollama) or http://10.0.0.5:8000 (vLLM)
  BI_LOCAL_LLM_MODEL   e.g. llama3.1:8b-instruct
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import socket
import urllib.request
from urllib.parse import urlparse

from .semantic import GRAINS, SemanticModel, SemanticQuery


def is_on_premises(url: str) -> bool:
    """True only if every address the host resolves to is loopback, private or link-local."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    return bool(addrs) and all(a.is_loopback or a.is_private or a.is_link_local for a in addrs)


def _schema(model: SemanticModel) -> dict:
    dims = list(model.dimensions)
    nullable = lambda s: {"anyOf": [s, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "answerable": {"type": "boolean"},
            "reason": {"type": "string"},
            "metrics": {"type": "array", "items": {"type": "string", "enum": list(model.metrics)}},
            "dimensions": {"type": "array", "items": {"type": "string", "enum": dims}},
            "filters": {"type": "array", "items": {
                "type": "object",
                "properties": {"dimension": {"type": "string", "enum": dims},
                               "op": {"type": "string", "enum": ["in", "not_in"]},
                               "values": {"type": "array", "items": {"type": "string"}}},
                "required": ["dimension", "op", "values"]}},
            "time_range": nullable({"type": "object",
                                    "properties": {"start": {"type": "string"}, "end": {"type": "string"},
                                                   "label": {"type": "string"}},
                                    "required": ["start", "end", "label"]}),
            "time_grain": nullable({"type": "string", "enum": list(GRAINS)}),
            "compare": nullable({"type": "string", "enum": ["previous_period", "previous_year"]}),
            "explain": {"type": "boolean"},
            "order_dir": {"type": "string", "enum": ["asc", "desc"]},
            "limit": nullable({"type": "integer"}),
        },
        "required": ["answerable", "metrics", "dimensions", "filters", "time_range", "time_grain", "compare",
                     "explain"],
    }


def _system(model: SemanticModel, dimension_values: dict) -> str:
    lines = [
        "Translate the business question into a JSON query over the semantic model below.",
        "Use only listed metric, dimension and value names. Ranges are [start, end) in YYYY-MM-DD,",
        "relative to the 'Today' line. Set explain=true for 'why'/'what drove' questions.",
        "If the question can't be answered with this model, set answerable=false and give a short reason.",
        f"Model: {model.label}. Metrics:",
    ]
    lines += [f"- {m['name']}: {m['label']}. {m.get('description', '')}" for m in model.metrics.values()]
    lines.append("Dimensions:")
    for d in model.dimensions.values():
        vals = ", ".join(sorted(map(str, dimension_values.get(d["name"], [])))[:40])
        lines.append(f"- {d['name']}: {d['label']}. Values: {vals}")
    return "\n".join(lines)


class LocalInterpreter:
    def __init__(self, model: SemanticModel, dimension_values: dict):
        self.model = model
        self.url = os.environ.get("BI_LOCAL_LLM_URL", "").rstrip("/")
        self.model_name = os.environ.get("BI_LOCAL_LLM_MODEL", "")
        self.error = None
        if self.url and not is_on_premises(self.url):
            self.error = f"BI_LOCAL_LLM_URL {self.url} is not on a private network; refusing to send questions there."
            self.url = ""
        self.schema = _schema(model)
        self.system = _system(model, dimension_values)

    @property
    def available(self) -> bool:
        return bool(self.url and self.model_name)

    def interpret(self, question: str, context: SemanticQuery | None = None,
                  today: dt.date | None = None) -> tuple:
        """Return (SemanticQuery | None, message). None means 'not answerable'."""
        today = today or dt.date.today()
        user = f"Today: {today.isoformat()}\n"
        if context is not None:
            user += f"Previous query: {json.dumps(context.to_dict())}\n"
        user += f"Question: {question}"
        body = {
            "model": self.model_name,
            "temperature": 0,
            "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "semantic_query", "schema": self.schema}},
        }
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(json.loads(resp.read())["choices"][0]["message"]["content"])
        if not data.pop("answerable", True):
            return None, data.get("reason") or "That isn't something the data model covers yet."
        data.pop("reason", None)
        return SemanticQuery.from_dict(data), ""
