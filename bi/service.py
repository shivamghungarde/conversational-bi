"""Application use cases. HTTP and the UI do not construct or execute SQL."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import router
from .registry import Registry, UPLOAD_TYPES


class BIService:
    def __init__(self, registry: Registry):
        self.registry = registry

    def query(self, body: dict) -> dict:
        engine = self.registry.get(body.get("dataset"))
        result = engine.query(body["query"])
        result["dataset"] = dict(router.info(engine), routed=False)
        return result

    def upload(self, payload: bytes, filename: str, classification: str) -> dict:
        if classification != "synthetic":
            raise ValueError("Confirm this file contains synthetic data only.")
        name = Path(filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in UPLOAD_TYPES or not payload:
            raise ValueError("Upload a non-empty .csv or .xlsx file containing synthetic data.")
        safe = re.sub(r"[^\w.\- ]", "_", Path(name).stem).strip()[:120] or "upload"
        folder = self.registry.data_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f"{safe}{suffix}"
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=folder))
        staged = stage / target.name
        engine = None
        try:
            staged.write_bytes(payload)
            engine = self.registry.add_file(staged, register=False, identity=target)
            engine.source.close()
            marker = staged.with_name(staged.name + ".classification.json")
            marker.write_text(json.dumps({"classification": "synthetic"}))
            with self.registry.lock:
                os.replace(staged, target)
                os.replace(staged.with_name(staged.name + ".semantic.json"), target.with_name(target.name + ".semantic.json"))
                os.replace(marker, target.with_name(target.name + ".classification.json"))
                engine.source.path = target
                self.registry.add(engine)
        except Exception as exc:
            raise ValueError("Could not import file. Check its columns, values and size limits.") from exc
        finally:
            if engine is not None:
                engine.source.close()
            shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "dataset": engine.id, "label": engine.model.label}
