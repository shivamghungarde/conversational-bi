"""HTTP API + static UI, standard library only.

GET  /api/health          liveness
GET  /api/model           metrics, dimensions and their values (for the UI's pickers)
GET  /api/samples         sample questions (UI placeholder and chips)
POST /api/ask             {question, context?}  -> answer + result set
POST /api/query           {query}               -> run a structured query (custom breakdowns)
"""
from __future__ import annotations

import json
import mimetypes
import os
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import data
from .engine import Engine

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
MAX_BODY = 64 * 1024


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConversationalBI/0.1"

        def log_message(self, fmt, *args):  # quieter default logging
            if os.environ.get("BI_QUIET") != "1":
                super().log_message(fmt, *args)

        def _send(self, status: int, body, content_type="application/json"):
            payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Request too large.")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/health":
                return self._send(200, {"ok": True, "llm_enabled": engine.llm.available})
            if path == "/api/model":
                return self._send(200, engine.catalog())
            if path == "/api/samples":
                samples = list(engine.model.sample_queries)
                random.shuffle(samples)
                return self._send(200, {"samples": samples})
            if path == "/":
                path = "/index.html"
            target = (WEB / path.lstrip("/")).resolve()
            if WEB in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype)
            return self._send(404, {"error": "Not found"})

        def do_POST(self):
            try:
                body = self._json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            if self.path == "/api/ask":
                question = str(body.get("question") or "").strip()[:500]
                if not question:
                    return self._send(400, {"ok": False, "error": "Ask a question."})
                return self._send(200, engine.ask(question, body.get("context")))
            if self.path == "/api/query":
                return self._send(200, engine.query(body.get("query") or {}))
            return self._send(404, {"error": "Not found"})

    return Handler


def main():
    db_path = Path(os.environ.get("BI_DB", ROOT / "data" / "demo.db"))
    model_path = Path(os.environ.get("BI_MODEL_FILE", ROOT / "semantic" / "retail_sales.json"))
    data.ensure(db_path)
    engine = Engine(model_path, db_path, log_path=db_path.parent / "query_log.jsonl")
    host, port = os.environ.get("BI_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    print(f"Conversational BI on http://{host}:{port}  (Claude interpreter: "
          f"{'on' if engine.llm.available else 'off - using built-in rules'})", flush=True)
    ThreadingHTTPServer((host, port), make_handler(engine)).serve_forever()


if __name__ == "__main__":
    main()
