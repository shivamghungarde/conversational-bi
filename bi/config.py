"""Deployment configuration. This release deliberately supports synthetic demos only."""
from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "1" if default else "0")
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1.")
    return value == "1"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data" / "synthetic"
    host: str = "127.0.0.1"
    port: int = 8765
    data_classification: str = "synthetic"
    demo: bool = True
    live: bool = True
    live_interval: float = 8.0
    source: str | None = None
    model_file: str | None = None
    source_classification: str | None = None
    allow_network: bool = False
    allowed_hosts: tuple = ("localhost", "127.0.0.1", "::1", "testserver")
    max_requests: int = 16
    request_timeout: float = 10.0

    def __post_init__(self):
        if self.data_classification != "synthetic":
            raise ValueError("This deployment supports synthetic data only. Real-data deployment requires a separate reviewed profile.")
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.allow_network:
            raise ValueError("Network binding requires explicit BI_ALLOW_NETWORK=1. This demo does not provide authentication.")
        if (not 1 <= self.port <= 65535 or not math.isfinite(self.live_interval) or self.live_interval <= 0
                or self.max_requests < 1 or not math.isfinite(self.request_timeout) or self.request_timeout <= 0):
            raise ValueError("Invalid port, live interval or request limit.")
        if self.source and self.source_classification != "synthetic":
            raise ValueError("Set BI_SOURCE_CLASSIFICATION=synthetic to confirm the configured source contains mock data only.")

    @classmethod
    def from_env(cls) -> "Settings":
        host = os.environ.get("BI_HOST", "127.0.0.1")
        allowed = tuple(dict.fromkeys(("localhost", "127.0.0.1", "::1", "testserver", host)
                       + tuple(filter(None, os.environ.get("BI_ALLOWED_HOSTS", "").split(",")))))
        return cls(data_dir=Path(os.environ.get("BI_DATA_DIR", ROOT / "data" / "synthetic")), host=host,
                   port=int(os.environ.get("PORT", "8765")), data_classification=os.environ.get("BI_DATA_CLASSIFICATION", "synthetic"),
                   demo=flag("BI_DEMO", True), live=flag("BI_LIVE", True),
                   live_interval=float(os.environ.get("BI_LIVE_INTERVAL", "8")), source=os.environ.get("BI_SOURCE"),
                   model_file=os.environ.get("BI_MODEL_FILE"), source_classification=os.environ.get("BI_SOURCE_CLASSIFICATION"),
                   allow_network=flag("BI_ALLOW_NETWORK", False), allowed_hosts=allowed)
