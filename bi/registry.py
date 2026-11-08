"""Source registry and bootstrap; independent of the HTTP framework."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from . import autosemantic, data, router, superstore
from .config import ROOT, Settings
from .engine import Engine
from .semantic import QueryError, SemanticModel, SemanticQuery
from .sources import FileSource, PostgresSource, SQLiteSource, open_source

UPLOAD_TYPES = (".csv", ".xlsx")

class Registry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.data_dir = self.settings.data_dir
        self.feeders = []
        self.engines: dict = {}
        self.lock = threading.RLock()
        self.log_path = self.data_dir / "query_log.jsonl"

    def add(self, engine: Engine) -> Engine:
        with self.lock:
            self.engines[engine.id] = engine
        return engine

    def get(self, dataset_id: str | None) -> Engine:
        with self.lock:
            if dataset_id and dataset_id != "auto":
                if dataset_id not in self.engines:
                    raise KeyError("Unknown data source.")
                return self.engines[dataset_id]
            if not self.engines:
                raise KeyError("No data sources are connected.")
            return next(iter(self.engines.values()))

    def listing(self) -> list:
        with self.lock:
            engines = list(self.engines.values())
        return [dict(router.info(e), freshness=e.freshness(), measures=len(e.model.metrics)) for e in engines]

    def ask(self, body: dict) -> dict:
        """Answer from the chosen source, or route the question when the user picked "Auto"."""
        question = str(body.get("question") or "").strip()[:500]
        if body.get("context") is not None:
            SemanticQuery.from_dict(body["context"])
        if body.get("clarifications") is not None and not isinstance(body["clarifications"], dict):
            raise QueryError("Clarifications must be an object.")
        clar = dict(body.get("clarifications") or {})
        chosen = clar.pop("dataset", None) or body.get("dataset") or "auto"
        if chosen != "auto" and chosen not in self.engines:
            raise KeyError("Unknown data source.")
        if chosen == "auto":
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
        res["steps"] = [source_step(engine, routed)] + res.get("steps", [])
        return res

    def add_file(self, path: Path, register: bool = True, identity: Path | None = None) -> Engine:
        """Register a CSV/Excel file, drafting a semantic model for it unless one already exists beside it."""
        source = FileSource(path, self.data_dir / "cache")
        try:
            model_path = path.with_name(path.name + ".semantic.json")
            if model_path.exists():
                model = SemanticModel.load(model_path, source.dialect)
            else:
                con = source.con
                (count,) = con.execute(f'SELECT COUNT(*) FROM "{source.table}"').fetchone()
                spec = autosemantic.infer(source.table, source.columns, path.stem.replace("_", " ").title(), count)
                model_path.write_text(json.dumps(spec, indent=2))
                model = SemanticModel(spec, source.dialect)
            digest = hashlib.sha256(str((identity or path).resolve()).encode()).hexdigest()[:16]
            engine = Engine("file_" + digest, model, source, self.log_path)
            return self.add(engine) if register else engine
        finally:
            source.close()


    def close_connections(self):
        with self.lock:
            engines = list(self.engines.values())
        for engine in engines:
            engine.source.close()

    def shutdown(self):
        for feeder in self.feeders:
            feeder.stopped.set()
        for feeder in self.feeders:
            feeder.join(timeout=2)
        self.close_connections()


def source_step(engine: Engine, routed: bool) -> dict:
    how = "matched to your question automatically" if routed else "the source you picked"
    return {"title": "Picked a source", "detail": f"{engine.model.label} · {how}"}


def build_registry(settings: Settings) -> Registry:
    reg = Registry(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if settings.source:
            spec = settings.source
            if spec.lower().endswith(UPLOAD_TYPES) and not settings.model_file:
                reg.add_file(Path(spec))
            else:
                if not settings.model_file:
                    raise ValueError("A configured database requires BI_MODEL_FILE.")
                source = open_source(spec, settings.data_dir / "cache")
                reg.add(Engine("source", SemanticModel.load(Path(settings.model_file), source.dialect), source, reg.log_path))
        if settings.demo:
            db = settings.data_dir / "demo.db"
            data.ensure(db)
            reg.add(Engine("delivery", SemanticModel.load(ROOT / "semantic" / "professional_services.json"),
                           SQLiteSource(db), reg.log_path))
            if settings.live:
                feeder = data.LiveFeeder(db, interval=settings.live_interval)
                reg.feeders.append(feeder)
                feeder.start()
            # Second demo: the Superstore retail sample, a static snapshot (no live feed). It runs on
            # PostgreSQL when BI_DEMO_POSTGRES_URL is set, otherwise on a local SQLite file.
            if settings.demo_postgres:
                url = superstore.resolve_postgres_url(settings.demo_postgres, settings.data_dir)
                superstore.ensure_postgres(url)
                store = PostgresSource(url, search_path=superstore.PG_SCHEMA)
            else:
                store_db = settings.data_dir / "superstore.db"
                superstore.ensure(store_db)
                store = SQLiteSource(store_db)
            store.live = False
            reg.add(Engine("superstore", SemanticModel.load(ROOT / "semantic" / "superstore.json", store.dialect),
                           store, reg.log_path))
        # Only uploads explicitly acknowledged as synthetic are reloaded.
        for path in sorted((settings.data_dir / "uploads").glob("*")):
            if path.suffix.lower() not in UPLOAD_TYPES:
                continue
            marker = path.with_name(path.name + ".classification.json")
            try:
                if not marker.exists() or json.loads(marker.read_text()).get("classification") != "synthetic":
                    continue
                reg.add_file(path)
            except Exception:  # One malformed persisted upload must not prevent demo startup.
                reg.close_connections()
                continue
        return reg
    except Exception:
        reg.shutdown()
        raise
