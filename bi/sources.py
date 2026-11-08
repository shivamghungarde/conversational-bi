"""Data sources. Every question runs a fresh query against the source (no result
cache), so answers are as current as the data behind them.

  * SQLite database file     (.db / .sqlite)      queried live
  * PostgreSQL               (postgresql://...)   queried live; needs `pip install psycopg`
  * CSV / Excel file         (.csv / .xlsx)       loaded into a local SQLite cache and
                                                  reloaded automatically when the file changes
"""
from __future__ import annotations

import csv
import hashlib
import uuid
import math
import time
from contextlib import contextmanager
from urllib.parse import urlparse
import datetime as dt
import re
import sqlite3
import threading
from pathlib import Path

from . import xlsx
from .semantic import DIALECTS

DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%b %d %Y", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M", "%Y/%m/%d"]


class Source:
    kind = "sqlite"
    type_word = "SQL"   # one word for the UI: SQL, CSV or Excel
    live = True

    def __init__(self):
        self._local = threading.local()

    @property
    def dialect(self):
        return DIALECTS["sqlite"]

    @property
    def con(self):
        """One connection per server thread; reopened if the underlying data was reloaded."""
        if getattr(self._local, "in_snapshot", False):
            return self._local.con
        version = self.version()
        if getattr(self._local, "version", None) != version:
            old = getattr(self._local, "con", None)
            if old is not None:
                old.close()
            self._local.con = self.connect()
            self._local.version = version
        return self._local.con

    def close(self):
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None
            self._local.version = None

    @contextmanager
    def snapshot(self):
        if getattr(self._local, "in_snapshot", False):
            yield self._local.con
            return
        con = self.con
        self._local.in_snapshot = True
        try:
            if self.kind == "postgres":
                with con.transaction():
                    con.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    yield con
            else:
                deadline = time.monotonic() + 10
                con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                con.execute("BEGIN")
                try:
                    yield con
                finally:
                    con.rollback()
        finally:
            self._local.in_snapshot = False

    def version(self):
        return 0

    def connect(self):
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def columns(self, table: str) -> list:
        """(name, type) pairs for one table, for the guarded SQL fallback's prompt."""
        rows = self.con.execute(f'PRAGMA table_info("{table.replace(chr(34), "")}")').fetchall()
        return [(r[1], (r[2] or "").upper()) for r in rows]


def _readonly_sqlite(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=5)
    deadline = time.monotonic() + 10
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return con


class SQLiteSource(Source):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)

    def connect(self):
        return _readonly_sqlite(self.path)

    def describe(self) -> str:
        return f"SQLite database {self.path.name}"


class PostgresSource(Source):
    kind = "postgres"

    def __init__(self, url: str, search_path: str | None = None):
        super().__init__()
        self.url = url
        if search_path is not None and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", search_path):
            raise ValueError("Invalid PostgreSQL schema name.")
        self.search_path = search_path
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("PostgreSQL needs the psycopg driver: pip install 'psycopg[binary]'") from exc

    @property
    def dialect(self):
        return DIALECTS["postgres"]

    def connect(self):
        import psycopg
        options = "-c statement_timeout=10000" + (f" -c search_path={self.search_path}" if self.search_path else "")
        con = psycopg.connect(self.url, autocommit=True, connect_timeout=5, options=options)
        con.execute("SET default_transaction_read_only = on")
        return con

    def columns(self, table: str) -> list:
        rows = self.con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s "
            "AND table_schema = ANY(current_schemas(false)) ORDER BY ordinal_position", (table,)).fetchall()
        return [(r[0], r[1].upper()) for r in rows]

    def describe(self) -> str:
        return "SQL"  # public descriptions must never contain DSNs or credentials


class FileSource(Source):
    """A CSV or Excel file, mirrored into a SQLite cache table and kept in sync with the file."""

    kind = "file"

    def __init__(self, path: Path, cache_dir: Path):
        super().__init__()
        self.path = Path(path)
        self.cache_dir = Path(cache_dir)
        self.table = identifier(self.path.stem) or "data"
        self.columns: list = []   # [{name, label, type}]
        self.loaded_mtime = None
        self.loaded_at = None
        self._lock = threading.RLock()
        self._cache_path = None
        self.reload()

    def version(self):
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self.loaded_mtime:
            with self._lock:
                if mtime != self.loaded_mtime:
                    self.reload()
        return self.loaded_mtime

    @contextmanager
    def snapshot(self):
        with self._lock:
            with super().snapshot() as con:
                yield con

    def reload(self) -> None:
        before = self.path.stat()
        headers, rows = read_table(self.path)
        if not headers:
            raise ValueError(f"{self.path.name} has no header row.")
        if not rows:
            raise ValueError(f"{self.path.name} has a header but no data rows.")
        columns = profile(headers, rows)
        after = self.path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("File changed during import; retry after the export finishes.")
        mtime = after.st_mtime
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = tuple((c["name"], c["type"]) for c in columns)
        if self.columns and fingerprint != tuple((c["name"], c["type"]) for c in self.columns):
            raise ValueError("File schema changed. Import it again to review the new measures.")
        source_id = hashlib.sha256(str(self.path.resolve()).encode()).hexdigest()[:20]
        target = self.cache_dir / f"{source_id}-{uuid.uuid4().hex}.db"
        con = sqlite3.connect(target)
        con.execute(f'DROP TABLE IF EXISTS "{self.table}"')
        defs = ", ".join(f'"{c["name"]}" {"REAL" if c["type"] == "number" else "TEXT"}' for c in columns)
        con.execute(f'CREATE TABLE "{self.table}" ({defs})')
        marks = ", ".join("?" for _ in columns)
        con.executemany(f'INSERT INTO "{self.table}" VALUES ({marks})',
                        [[c["convert"](v) for c, v in zip(columns, r)] for r in rows])
        con.commit()
        con.close()
        old = self._cache_path
        self._cache_path, self.columns = target, [{k: c[k] for k in ("name", "label", "type", "distinct", "percent", "currency")}
                                                  for c in columns]
        self.loaded_mtime, self.loaded_at = mtime, dt.datetime.now()
        if old and old != target and old.exists():
            try:
                old.unlink()
            except OSError:
                pass

    def connect(self):
        return _readonly_sqlite(self._cache_path)

    @property
    def type_word(self) -> str:
        return "Excel" if self.path.suffix.lower() == ".xlsx" else "CSV"

    def describe(self) -> str:
        return f"{self.type_word} file {self.path.name}"


def open_source(spec: str, cache_dir: Path) -> Source:
    if spec.startswith(("postgres://", "postgresql://")):
        return PostgresSource(spec)
    path = Path(spec)
    if not path.exists():
        raise FileNotFoundError(f"Data source not found: {spec}")
    if path.suffix.lower() in (".csv", ".xlsx", ".txt", ".tsv"):
        return FileSource(path, cache_dir)
    return SQLiteSource(path)


# ---- file loading and column profiling ------------------------------------

def identifier(text: str) -> str:
    ident = re.sub(r"[^0-9a-zA-Z]+", "_", str(text)).strip("_").lower()
    if ident and ident[0].isdigit():
        ident = "c_" + ident
    return ident


def read_table(path: Path) -> tuple:
    if path.suffix.lower() == ".xlsx":
        return xlsx.read(path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        headers = next(reader, [])
        rows = []
        for r in reader:
            if len(r) > 256 or len(rows) >= 100000:
                raise ValueError("File exceeds the limit of 100,000 rows or 256 columns.")
            if any(cell.strip() for cell in r):
                rows.append(r)
    width = len(headers)
    if width > 256:
        raise ValueError("Files may have at most 256 columns.")
    return [h.strip() for h in headers], [(r + [None] * width)[:width] for r in rows]


BOOLEANS = {"true": "Yes", "false": "No", "yes": "Yes", "no": "No", "y": "Yes", "n": "No"}


def _parse_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError("boolean")  # true/false is a category (Yes/No), not a number
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            raise ValueError("Non-finite numbers are not supported.")
        return float(v)
    s = str(v).strip()
    for symbol in "$£€₹":
        s = s.replace(symbol, "")
    s = s.replace(" ", "").replace("\u00a0", "")
    pct = s.endswith("%")
    s = s.rstrip("%")
    if s.startswith("(") and s.endswith(")"):  # accounting negatives: (120.00)
        s = "-" + s[1:-1]
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+,\d+|-?\d+,\d{1,2}", s):  # European: 1.234,50 or 980,00
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    num = float(s)
    if not math.isfinite(num):
        raise ValueError("Non-finite numbers are not supported.")
    return num / 100 if pct else num


def _parse_bool(v):
    if isinstance(v, bool):
        return "Yes" if v else "No"
    key = str(v).strip().lower()
    if key not in BOOLEANS:
        raise ValueError(v)
    return BOOLEANS[key]


def _parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        raise ValueError("number")
    s = str(v).strip()
    for f in DATE_FORMATS:
        try:
            d = dt.datetime.strptime(s, f)
            return d.date().isoformat() if d.time() == dt.time() else d.isoformat(sep=" ")
        except ValueError:
            continue
    raise ValueError(s)


def _share(values, fn) -> float:
    ok = 0
    for v in values:
        try:
            fn(v)
            ok += 1
        except (ValueError, TypeError):
            pass
    return ok / len(values) if values else 0.0


def profile(headers: list, rows: list) -> list:
    """Infer each column's type (date / number / text) and how to convert its values."""
    columns, seen = [], set()
    for i, header in enumerate(headers):
        name = identifier(header) or f"column_{i + 1}"
        while name in seen:
            name += "_2"
        seen.add(name)
        values = [r[i] for r in rows if r[i] not in (None, "")]
        sample = values[:2000]
        if re.search(r"(^|_)(id|code|key|number|no|num)$", name):
            kind, convert = "text", lambda v: None if v in (None, "") else str(v).strip()
        elif sample and _share(sample, _parse_bool) == 1.0:
            kind, convert = "text", _safe(_parse_bool)
        elif sample and _share(sample, _parse_date) >= 0.95:
            kind, convert = "date", _safe(_parse_date)
        elif sample and _share(sample, _parse_number) >= 0.95:
            kind, convert = "number", _safe(_parse_number)
        else:
            kind, convert = "text", lambda v: None if v in (None, "") else str(v).strip()
        percent = kind == "number" and sum(str(v).strip().endswith("%") for v in sample) > len(sample) / 2
        bad = [v for v in values if convert(v) is None]
        if bad:
            raise ValueError(f"Column '{header}' contains {len(bad)} invalid {kind} values; correct them before importing.")
        converted = {convert(v) for v in values} - {None}
        columns.append({"name": name, "label": str(header).strip() or name, "type": kind, "convert": convert,
                        "distinct": len(converted), "percent": percent,
                        "currency": next((code for symbol, code in [("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("$", "USD")]
                                          if any(symbol in str(v) for v in sample)), None)})
    return columns


def _safe(fn):
    def wrapped(v):
        try:
            return fn(v)
        except (ValueError, TypeError):
            return None
    return wrapped

