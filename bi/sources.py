"""Data sources. Every question runs a fresh query against the source (no result
cache), so answers are as current as the data behind them.

  * SQLite database file     (.db / .sqlite)      queried live
  * PostgreSQL               (postgresql://...)   queried live; needs `pip install psycopg`
  * CSV / Excel file         (.csv / .xlsx)       loaded into a local SQLite cache and
                                                  reloaded automatically when the file changes
"""
from __future__ import annotations

import csv
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
        version = self.version()
        if getattr(self._local, "version", None) != version:
            old = getattr(self._local, "con", None)
            if old is not None:
                old.close()
            self._local.con = self.connect()
            self._local.version = version
        return self._local.con

    def version(self):
        return 0

    def connect(self):
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class SQLiteSource(Source):
    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)

    def connect(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)

    def describe(self) -> str:
        return f"SQLite database {self.path.name}"


class PostgresSource(Source):
    kind = "postgres"

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("PostgreSQL needs the psycopg driver: pip install 'psycopg[binary]'") from exc

    @property
    def dialect(self):
        return DIALECTS["postgres"]

    def connect(self):
        import psycopg
        con = psycopg.connect(self.url, autocommit=True)
        con.execute("SET default_transaction_read_only = on")
        return con

    def describe(self) -> str:
        return "PostgreSQL database " + re.sub(r"//[^@]*@", "//", self.url)  # never echo credentials


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
        self._lock = threading.Lock()
        self._cache_path = None
        self.reload()

    def version(self):
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self.loaded_mtime:
            with self._lock:
                if mtime != self.loaded_mtime:
                    self.reload()
        return self.loaded_mtime

    def reload(self) -> None:
        headers, rows = read_table(self.path)
        if not headers:
            raise ValueError(f"{self.path.name} has no header row.")
        columns = profile(headers, rows)
        mtime = self.path.stat().st_mtime
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{self.table}-{int(mtime * 1000)}.db"
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
        self._cache_path, self.columns = target, [{k: c[k] for k in ("name", "label", "type", "distinct")}
                                                  for c in columns]
        self.loaded_mtime, self.loaded_at = mtime, dt.datetime.now()
        if old and old != target and old.exists():
            try:
                old.unlink()
            except OSError:
                pass

    def connect(self):
        return sqlite3.connect(f"file:{self._cache_path}?mode=ro", uri=True, check_same_thread=False)

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
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    width = len(headers)
    return [h.strip() for h in headers], [(r + [None] * width)[:width] for r in rows]


def _parse_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "").replace("₹", "")
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    num = float(s)
    return num / 100 if pct else num


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
        if sample and _share(sample, _parse_date) >= 0.95:
            kind, convert = "date", _safe(_parse_date)
        elif sample and _share(sample, _parse_number) >= 0.95:
            kind, convert = "number", _safe(_parse_number)
        else:
            kind, convert = "text", lambda v: None if v in (None, "") else str(v).strip()
        columns.append({"name": name, "label": str(header).strip() or name, "type": kind, "convert": convert,
                        "distinct": len(set(map(str, values)))})
    return columns


def _safe(fn):
    def wrapped(v):
        try:
            return fn(v)
        except (ValueError, TypeError):
            return None
    return wrapped

