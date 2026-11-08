"""Superstore sample: fictional retail orders and returns, loaded from samples/superstore/ into SQLite.

The database is a static snapshot (no live feed). It is rebuilt only when the sample files change.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import os
import sqlite3
from pathlib import Path

from .config import ROOT

SAMPLES = ROOT / "samples" / "superstore"
FILES = ("orders.csv.gz", "returns.csv.gz")
LOADER_VERSION = "2"  # bump when the schema or cleaning changes, so existing databases are rebuilt

SCHEMA = """
CREATE TABLE orders (
    row_id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, order_date TEXT NOT NULL, ship_date TEXT,
    ship_mode TEXT, customer_id TEXT, customer_name TEXT, segment TEXT, country TEXT, city TEXT, state TEXT,
    postal_code TEXT, region TEXT, product_id TEXT, category TEXT, sub_category TEXT, product_name TEXT,
    sales REAL, quantity INTEGER, discount REAL, profit REAL
);
-- One row per returned order. The primary key means a returned order can never be counted twice.
CREATE TABLE returns (order_id TEXT PRIMARY KEY, return_reason TEXT);
CREATE TABLE snapshot (built_at TEXT, fingerprint TEXT);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_order ON orders(order_id);
"""


def fingerprint() -> str:
    h = hashlib.sha256(LOADER_VERSION.encode())
    for name in FILES:
        h.update((SAMPLES / name).read_bytes())
    return h.hexdigest()


def _rows(name: str):
    with gzip.open(SAMPLES / name, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        for row in reader:
            yield dict(zip(header, row))


def build(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".building")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    num = lambda v, cast=float: cast(v) if v not in ("", None) else None
    con.executemany(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((num(r["row_id"], int), r["order_id"], r["order_date"], r["ship_date"] or None, r["ship_mode"],
          r["customer_id"], r["customer_name"], r["segment"], r["country"], r["city"], r["state"], r["postal_code"] or None,
          r["region"], r["product_id"], r["category"], r["sub_category"], r["product_name"], num(r["sales"]),
          num(r["quantity"], int), num(r["discount"]), num(r["profit"])) for r in _rows("orders.csv.gz")))
    con.executemany("INSERT OR IGNORE INTO returns VALUES (?, ?)",
                    ((r["order_id"], r["return_reason"]) for r in _rows("returns.csv.gz")))
    con.execute("INSERT INTO snapshot VALUES (?, ?)", (dt.datetime.now().isoformat(timespec="seconds"), fingerprint()))
    con.commit()
    con.close()
    os.replace(tmp, db_path)  # atomic: readers never see a half-built database


def ensure(db_path: Path) -> None:
    """Build the database if it is missing or was built from different sample files."""
    if db_path.exists():
        try:
            con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
            (stored,) = con.execute("SELECT fingerprint FROM snapshot").fetchone()
            con.close()
            if stored == fingerprint():
                return
        except sqlite3.Error:
            pass
    build(db_path)
