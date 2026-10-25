"""HTTP API + static UI, standard library only. Everything runs on this machine.

GET  /api/health                 liveness
GET  /api/datasets               datasets that can be queried
GET  /api/model?dataset=         metrics, dimensions and their values
GET  /api/samples?dataset=       sample questions (UI placeholder and chips)
GET  /api/freshness?dataset=     when the data last changed
POST /api/ask      {dataset, question, context?, clarifications?}
POST /api/query    {dataset, query}                     run a structured query (custom breakdowns)
POST /api/upload   raw file body, X-Filename header     add a CSV/Excel file as a new dataset
"""
from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import autosemantic, data, router
from .engine import Engine
from .semantic import SemanticModel
from .sources import FileSource, SQLiteSource, identifier, open_source

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = Path(os.environ.get("BI_DATA_DIR", ROOT / "data"))
MAX_BODY = 64 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
UPLOAD_TYPES = (".csv", ".xlsx")


class Registry:
    def __init__(self):
        self.engines: dict = {}
        self.lock = threading.Lock()
        self.log_path = DATA / "query_log.jsonl"

    def add(self, engine: Engine) -> Engine:
        with self.lock:
            self.engines[engine.id] = engine
        return engine

    def get(self, dataset_id: str | None) -> Engine:
        if dataset_id and dataset_id in self.engines:
            return self.engines[dataset_id]
        return next(iter(self.engines.values()))

    def listing(self) -> list:
        return [dict(router.info(e), freshness=e.freshness(), measures=len(e.model.metrics))
                for e in self.engines.values()]

    def ask(self, body: dict) -> dict:
        """Answer from the chosen source, or route the question when the user picked "Auto"."""
        question = str(body.get("question") or "").strip()[:500]
        clar = dict(body.get("clarifications") or {})
        chosen = clar.pop("dataset", None) or body.get("dataset") or "auto"
        if chosen == "auto" or chosen not in self.engines:
            engine, clarify = router.route(list(self.engines.values()), question, body.get("context"),
                                           body.get("context_dataset"))
            if clarify:
                return clarify
            routed = True
        else:
            engine, routed = self.engines[chosen], False
        ctx = body.get("context") if body.get("context_dataset") == engine.id else None
        res = engine.ask(question, ctx, clar or None)
        res["dataset"] = dict(router.info(engine), routed=routed)
        return res

    def add_file(self, path: Path) -> Engine:
        """Register a CSV/Excel file, drafting a semantic model for it unless one already exists beside it."""
        source = FileSource(path, DATA / "cache")
        model_path = path.with_suffix(".semantic.json")
        if model_path.exists():
            model = SemanticModel.load(model_path, source.dialect)
        else:
            con = source.con
            (count,) = con.execute(f'SELECT COUNT(*) FROM "{source.table}"').fetchone()
            spec = autosemantic.infer(source.table, source.columns, path.stem.replace("_", " ").title(), count)
            model_path.write_text(json.dumps(spec, indent=2))
            model = SemanticModel(spec, source.dialect)
        return self.add(Engine(identifier(path.stem), model, source, self.log_path))


def build_registry() -> Registry:
    reg = Registry()
    DATA.mkdir(parents=True, exist_ok=True)
    # 1. A configured source (a real database or file): BI_SOURCE=/path/file.xlsx | /path/db.sqlite | postgresql://...
    spec = os.environ.get("BI_SOURCE")
    if spec:
        model_file = os.environ.get("BI_MODEL_FILE")
        if spec.lower().endswith(UPLOAD_TYPES) and not model_file:
            reg.add_file(Path(spec))
        else:
            if not model_file:
                raise SystemExit("BI_SOURCE is a database: also set BI_MODEL_FILE to its semantic model JSON.")
            source = open_source(spec, DATA / "cache")
            reg.add(Engine("source", SemanticModel.load(Path(model_file), source.dialect), source, reg.log_path))
    # 2. The built-in live demo
    if os.environ.get("BI_DEMO", "1") != "0":
        db = DATA / "demo.db"
        data.ensure(db)
        reg.add(Engine("delivery", SemanticModel.load(ROOT / "semantic" / "professional_services.json"),
                       SQLiteSource(db), reg.log_path))
        if os.environ.get("BI_LIVE", "1") != "0":
            data.LiveFeeder(db, interval=float(os.environ.get("BI_LIVE_INTERVAL", "8"))).start()
    # 3. Previously uploaded files
    for path in sorted((DATA / "uploads").glob("*")) if (DATA / "uploads").exists() else []:
        if path.suffix.lower() in UPLOAD_TYPES:
            try:
                reg.add_file(path)
            except Exception as exc:  # a broken upload shouldn't stop the server
                print(f"Skipping {path.name}: {exc}", flush=True)
    return reg


def make_handler(reg: Registry):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConversationalBI/0.2"

        def log_message(self, fmt, *args):
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
            url = urlparse(self.path)
            path, params = url.path, parse_qs(url.query)
            ds = (params.get("dataset") or [None])[0]
            if path == "/api/health":
                return self._send(200, {"ok": True, "datasets": len(reg.engines)})
            if path == "/api/datasets":
                return self._send(200, {"datasets": reg.listing()})
            if path == "/api/model":
                return self._send(200, reg.get(ds).catalog())
            if path == "/api/samples":
                samples = list(reg.get(ds).model.sample_queries)
                random.shuffle(samples)
                return self._send(200, {"samples": samples})
            if path == "/api/freshness":
                return self._send(200, reg.get(ds).freshness())
            if path == "/":
                path = "/index.html"
            target = (WEB / path.lstrip("/")).resolve()
            if WEB in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype)
            return self._send(404, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/api/upload":
                return self._upload()
            try:
                body = self._json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            if self.path == "/api/ask":
                if not str(body.get("question") or "").strip():
                    return self._send(400, {"ok": False, "error": "Ask a question."})
                return self._send(200, reg.ask(body))
            if self.path == "/api/query":
                engine = reg.get(body.get("dataset"))
                res = engine.query(body.get("query") or {})
                res["dataset"] = dict(router.info(engine), routed=False)
                return self._send(200, res)
            return self._send(404, {"error": "Not found"})

        def _upload(self):
            name = os.path.basename(self.headers.get("X-Filename") or "")
            length = int(self.headers.get("Content-Length") or 0)
            suffix = Path(name).suffix.lower()
            if suffix not in UPLOAD_TYPES:
                return self._send(400, {"ok": False, "error": "Upload a .csv or .xlsx file."})
            if not 0 < length <= MAX_UPLOAD:
                return self._send(400, {"ok": False, "error": "Files must be under 25 MB."})
            safe = re.sub(r"[^\w.\- ]", "_", Path(name).stem).strip() or "upload"
            folder = DATA / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{safe}{suffix}"
            target.write_bytes(self.rfile.read(length))
            target.with_suffix(".semantic.json").unlink(missing_ok=True)  # re-draft the model for new columns
            try:
                engine = reg.add_file(target)
            except Exception as exc:
                return self._send(400, {"ok": False, "error": f"Couldn't read {name}: {exc}"})
            return self._send(200, {"ok": True, "dataset": engine.id, "label": engine.model.label})

    return Handler


def main():
    reg = build_registry()
    host, port = os.environ.get("BI_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    print(f"Conversational BI on http://{host}:{port}", flush=True)
    for e in reg.engines.values():
        llm = "local LLM on" if e.llm.available else "rules engine (no LLM)"
        print(f"  dataset '{e.id}': {e.source.describe()}  [{llm}]", flush=True)
        if e.llm.error:
            print(f"  ! {e.llm.error}", flush=True)
    ThreadingHTTPServer((host, port), make_handler(reg)).serve_forever()


if __name__ == "__main__":
    main()
