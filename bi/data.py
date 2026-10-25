"""Demo warehouse: a small, deterministic retail dataset in SQLite.

Swap this module for a real connector (Postgres, Snowflake, BigQuery, DuckDB)
when moving past the demo. Nothing else in the app talks to the database
directly except `connect()` callers in the engine.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import sqlite3
from pathlib import Path

REGIONS = {"North": 1.15, "South": 0.85, "East": 1.30, "West": 1.00}
CHANNELS = {"Online": 0.50, "Retail Store": 0.35, "Partner": 0.15}
SEGMENTS = {"Consumer": 0.60, "SMB": 0.28, "Enterprise": 0.12}
PRODUCTS = [
    # name, category, unit price, unit cost
    ("Laptop", "Electronics", 1100.0, 820.0),
    ("Headphones", "Electronics", 180.0, 95.0),
    ("Smartwatch", "Electronics", 320.0, 190.0),
    ("Blender", "Home", 90.0, 48.0),
    ("Air Purifier", "Home", 240.0, 150.0),
    ("Desk Lamp", "Home", 45.0, 18.0),
    ("Jacket", "Apparel", 140.0, 60.0),
    ("Sneakers", "Apparel", 110.0, 52.0),
    ("T-Shirt", "Apparel", 25.0, 8.0),
    ("Yoga Mat", "Sports", 40.0, 15.0),
    ("Bike Helmet", "Sports", 75.0, 38.0),
    ("Dumbbells", "Sports", 60.0, 33.0),
]
START = dt.date(2024, 1, 1)
N_CUSTOMERS = 900


def _weighted(rng: random.Random, weights: dict) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def build(db_path: Path, end: dt.date | None = None, seed: int = 42) -> None:
    """Create the demo database. Data runs from 2024-01-01 to `end` (default: today)."""
    end = end or dt.date.today()
    rng = random.Random(seed)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT,
                               unit_price REAL, unit_cost REAL);
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, customer_name TEXT, segment TEXT,
                                signup_date TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, order_date TEXT, customer_id INTEGER,
                             product_id INTEGER, region TEXT, channel TEXT, quantity INTEGER,
                             revenue REAL, cost REAL, discount REAL, returned INTEGER);
        CREATE INDEX idx_orders_date ON orders(order_date);
        """
    )
    con.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?)",
        [(i + 1, *p) for i, p in enumerate(PRODUCTS)],
    )
    customers = []
    for cid in range(1, N_CUSTOMERS + 1):
        signup = START + dt.timedelta(days=rng.randint(0, max(1, (end - START).days - 30)))
        customers.append((cid, f"Customer {cid:04d}", _weighted(rng, SEGMENTS), signup.isoformat()))
    con.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)

    orders = []
    oid = 1
    day = START
    total_days = (end - START).days or 1
    while day <= end:
        growth = 1.0 + 0.35 * ((day - START).days / total_days)          # steady growth
        season = 1.0 + 0.45 * max(0.0, math.cos((day.month - 12) / 12 * 2 * math.pi)) ** 4  # Nov/Dec peak
        weekend = 1.15 if day.weekday() >= 5 else 1.0
        for _ in range(int(rng.gauss(22, 4) * growth * season * weekend)):
            cust = customers[rng.randrange(N_CUSTOMERS)]
            region = _weighted(rng, REGIONS)
            channel = _weighted(rng, CHANNELS)
            pid = rng.randrange(len(PRODUCTS))
            _, category, price, cost = PRODUCTS[pid]
            qty = 1 + int(rng.expovariate(1.2)) + (2 if cust[2] == "Enterprise" else 0)
            discount = rng.choice([0, 0, 0, 0.05, 0.10, 0.15, 0.20 if channel == "Partner" else 0.10])
            revenue = round(qty * price * (1 - discount), 2)
            returned = 1 if rng.random() < (0.09 if category == "Apparel" else 0.04) else 0
            orders.append((oid, day.isoformat(), cust[0], pid + 1, region, channel, qty,
                           revenue, round(qty * cost, 2), discount, returned))
            oid += 1
        day += dt.timedelta(days=1)
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", orders)
    con.commit()
    con.close()


def ensure(db_path: Path) -> None:
    """Build the demo database if it is missing or stale (older than today)."""
    if db_path.exists():
        con = sqlite3.connect(db_path)
        try:
            (latest,) = con.execute("SELECT MAX(order_date) FROM orders").fetchone()
        except sqlite3.Error:
            latest = None
        con.close()
        if latest == dt.date.today().isoformat():
            return
    build(db_path)
