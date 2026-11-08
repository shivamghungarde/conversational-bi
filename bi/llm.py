"""Optional on-premises LLM interpreter for phrasings the rule parser can't handle.

Questions must not leave the network, so this only talks to a self-hosted,
OpenAI-compatible chat endpoint (Ollama, vLLM, LM Studio, llama.cpp server)
and refuses any endpoint that doesn't resolve to a loopback or private address.

The model sees bounded dimension value candidates (which may contain business names), but never raw fact rows and never writes SQL. It fills in a JSON
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
import http.client
from urllib.parse import urlparse

from .semantic import GRAINS, SemanticModel, SemanticQuery


def _addresses(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return []
    try:
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(parsed.hostname, parsed.port or 80)}
    except (OSError, ValueError):
        return []
    networks = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
    return list(addrs) if addrs and all(a.is_loopback or any(a in n for n in networks if n.version == a.version) for a in addrs) else []


def is_on_premises(url: str) -> bool:
    return bool(_addresses(url))


class _PinnedHTTP(http.client.HTTPConnection):
    def connect(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        addresses = _addresses(f"http://{host}:{self.port}")
        if not addresses:
            raise ValueError("LLM address is no longer on an approved private network.")
        # Connect to the validated numeric IP: no second DNS resolution.
        self.sock = socket.create_connection((str(addresses[0]), self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        _PinnedHTTP.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPS, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirects are disabled for the local LLM endpoint.")


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
        if not is_on_premises(self.url):
            raise ValueError("LLM endpoint is not private.")
        user = f"Today: {today.isoformat()}\n"
        if context is not None:
            user += f"Previous query: {json.dumps(context.to_dict())}\n"
        user += f"Question: {question}"
        body = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": 768,
            "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "semantic_query", "schema": self.schema}},
        }
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _NoRedirect())
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("LLM response exceeds its size limit.")
            data = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        if not data.pop("answerable", True):
            return None, data.get("reason") or "That isn't something the data model covers yet."
        data.pop("reason", None)
        return SemanticQuery.from_dict(data), ""
