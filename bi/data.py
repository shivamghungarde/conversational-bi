"""Demo warehouse: a professional-services firm's timesheets in SQLite.

Utilization = billable hours / available hours, recorded per consultant per
working day. Two events are built in so "why" questions have a real answer:
  * a large Data & AI engagement (Juniper Pharma) ends on 31 Aug 2026, and
  * a hiring wave of 18 junior consultants in India / Cloud starts on 1 Sep 2026.

Real-time: the most recent working days are only partly submitted. A
background feeder moves the late timesheets in a few at a time, so answers
change while you watch, as they would against a live system of record.

Swap this module for a real source (see bi/sources.py) when moving past the demo.
"""
from __future__ import annotations

import datetime as dt
import random
import sqlite3
import threading
from pathlib import Path

PRACTICES = {"Data & AI": 1.00, "Cloud": 1.00, "Strategy": 0.92, "Cybersecurity": 1.05, "ERP": 0.97}
REGIONS = {"North America": 0.32, "Europe": 0.28, "India": 0.28, "APAC": 0.12}
GRADES = {
    # share, bill rate, cost rate per hour, target billable share of the day
    "Analyst": (0.26, 95, 38, 0.86),
    "Consultant": (0.30, 140, 58, 0.84),
    "Senior Consultant": (0.22, 185, 82, 0.80),
    "Manager": (0.15, 240, 112, 0.68),
    "Director": (0.07, 310, 160, 0.45),
}
CLIENTS = [
    ("Aldermoor Bank", "Banking"), ("Brightwell Health", "Healthcare"), ("Cobalt Retail", "Retail"),
    ("Driftwood Manufacturing", "Manufacturing"), ("Evergreen Energy", "Energy"),
    ("Fairhaven Council", "Public Sector"), ("Granite Telecom", "Telecom"), ("Harborview Insurance", "Insurance"),
    ("Ironleaf Logistics", "Logistics"), ("Juniper Pharma", "Healthcare"), ("Keystone Media", "Media"),
    ("Lumen Utilities", "Energy"),
]
# name, client index, practice, billing type, start, end, weight (team size)
PROJECTS = [
    ("Juniper Clinical Data Platform", 9, "Data & AI", "Time & Materials", "2025-01-01", "2026-08-31", 6.0),
    ("Aldermoor Risk Analytics", 0, "Data & AI", "Time & Materials", "2025-03-01", "2027-06-30", 2.5),
    ("Cobalt Demand Forecasting", 2, "Data & AI", "Fixed Fee", "2025-09-01", "2026-12-31", 1.5),
    ("Keystone Audience AI", 10, "Data & AI", "Time & Materials", "2026-10-01", "2027-09-30", 1.0),
    ("Granite Cloud Migration", 6, "Cloud", "Time & Materials", "2025-01-01", "2027-03-31", 3.0),
    ("Lumen Platform Modernisation", 11, "Cloud", "Fixed Fee", "2025-06-01", "2026-11-30", 2.0),
    ("Ironleaf Cloud Ops", 8, "Cloud", "Retainer", "2025-01-01", "2027-12-31", 1.5),
    ("Harborview Landing Zone", 7, "Cloud", "Time & Materials", "2026-04-01", "2027-04-30", 1.5),
    ("Fairhaven Digital Strategy", 5, "Strategy", "Fixed Fee", "2025-02-01", "2026-12-31", 2.0),
    ("Evergreen Operating Model", 4, "Strategy", "Time & Materials", "2025-05-01", "2026-10-31", 1.5),
    ("Brightwell Growth Plan", 1, "Strategy", "Fixed Fee", "2026-03-01", "2027-02-28", 1.0),
    ("Aldermoor SOC Uplift", 0, "Cybersecurity", "Retainer", "2025-01-01", "2027-12-31", 2.5),
    ("Brightwell Zero Trust", 1, "Cybersecurity", "Time & Materials", "2025-07-01", "2027-01-31", 2.0),
    ("Granite Pen Testing", 6, "Cybersecurity", "Fixed Fee", "2025-01-01", "2027-06-30", 1.0),
    ("Driftwood S/4 Rollout", 3, "ERP", "Time & Materials", "2025-01-01", "2027-03-31", 3.0),
    ("Cobalt Finance Transformation", 2, "ERP", "Fixed Fee", "2025-04-01", "2026-12-31", 2.0),
    ("Juniper Supply Chain ERP", 9, "ERP", "Time & Materials", "2026-01-01", "2027-06-30", 1.5),
]
FIRST_DAY = dt.date(2025, 1, 1)
HIRE_WAVE = dt.date(2026, 9, 1)
N_EMPLOYEES = 132
LATE_DAYS = 3          # the last N working days are only partly submitted (the live backlog)

FIRST_NAMES = ["Aarav", "Maya", "Liam", "Priya", "Noah", "Sofia", "Arjun", "Emma", "Kenji", "Zara", "Lucas",
               "Ananya", "Mateo", "Chloe", "Rohan", "Isla", "Wei", "Amara", "Diego", "Freya", "Kabir", "Nina"]
LAST_NAMES = ["Shah", "Kim", "Patel", "Nguyen", "Okafor", "Rossi", "Iyer", "Müller", "Tanaka", "Silva", "Brown",
              "Mehta", "Garcia", "Lee", "Khan", "Novak", "Reddy", "Dubois", "Chen", "Walsh"]


def _weighted(rng: random.Random, weights: dict) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _workdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def last_workday(today: dt.date) -> dt.date:
    d = today
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def build(db_path: Path, end: dt.date | None = None, seed: int = 7, backlog: bool = True) -> None:
    """Create the demo database with data up to `end` (default: today)."""
    end = end or dt.date.today()
    rng = random.Random(seed)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE employees (employee_id INTEGER PRIMARY KEY, employee_name TEXT, practice TEXT, region TEXT,
                                grade TEXT, bill_rate REAL, cost_rate REAL, start_date TEXT);
        CREATE TABLE clients (client_id INTEGER PRIMARY KEY, client_name TEXT, industry TEXT);
        CREATE TABLE projects (project_id INTEGER PRIMARY KEY, project_name TEXT, client_id INTEGER, practice TEXT,
                               billing_type TEXT, start_date TEXT, end_date TEXT);
        CREATE TABLE timesheets (entry_id INTEGER PRIMARY KEY, work_date TEXT, employee_id INTEGER,
                                 project_id INTEGER, available_hours REAL, billable_hours REAL,
                                 nonbillable_hours REAL, revenue REAL, cost REAL, submitted_at TEXT);
        CREATE TABLE timesheet_backlog AS SELECT * FROM timesheets WHERE 0;
        CREATE INDEX idx_ts_date ON timesheets(work_date);
        """
    )
    con.executemany("INSERT INTO clients VALUES (?,?,?)", [(i + 1, *c) for i, c in enumerate(CLIENTS)])
    con.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
                    [(i + 1, p[0], p[1] + 1, p[2], p[3], p[4], p[5]) for i, p in enumerate(PROJECTS)])

    employees = []
    used_names = set()
    for eid in range(1, N_EMPLOYEES + 1):
        hire_wave = eid > N_EMPLOYEES - 18
        if hire_wave:
            practice, region = ("Cloud" if eid % 3 else "Data & AI"), "India"
            grade = "Analyst" if eid % 2 else "Consultant"
            start = HIRE_WAVE
        else:
            practice, region = _weighted(rng, PRACTICES), _weighted(rng, REGIONS)
            grade = _weighted(rng, {g: v[0] for g, v in GRADES.items()})
            start = FIRST_DAY - dt.timedelta(days=rng.randint(30, 2000)) if rng.random() < 0.9 \
                else FIRST_DAY + dt.timedelta(days=rng.randint(0, 500))
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used_names:
                used_names.add(name)
                break
        _, bill, cost, _ = GRADES[grade]
        region_rate = {"India": 0.55, "APAC": 0.85}.get(region, 1.0)
        employees.append((eid, name, practice, region, grade, round(bill * region_rate), round(cost * region_rate),
                          start.isoformat()))
    con.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?)", employees)

    projects = [(i + 1, p) for i, p in enumerate(PROJECTS)]
    rows, backlog_rows = [], []
    entry = 1
    assignment: dict = {}
    staffed_month = False
    late_from = list(_workdays(end - dt.timedelta(days=10), end))[-LATE_DAYS:]
    for day in _workdays(FIRST_DAY, end):
        iso = day.isoformat()
        active = [(pid, p) for pid, p in projects if p[4] <= iso <= p[5]]
        if day.day == 1 or not staffed_month:  # staffing is reviewed at the start of each month
            staffed_month = True
            active_ids = {pid for pid, _ in active}
            for e in employees:
                current = assignment.get(e[0])
                if current in active_ids and rng.random() < 0.92:
                    continue  # most people stay on their engagement
                assignment.pop(e[0], None)
                options = [(pid, p[6]) for pid, p in active if p[2] == e[2]]
                demand = min(1.0, sum(w for _, w in options) / 9.0 + 0.35)  # more live work -> more staffed
                ramp = 0.35 if dt.date.fromisoformat(e[7]) > day - dt.timedelta(days=60) else 1.0
                if options and rng.random() < demand * ramp * PRACTICES[e[2]]:
                    assignment[e[0]] = rng.choices([o[0] for o in options], weights=[o[1] for o in options])[0]
        for e in employees:
            eid, _, practice, region, grade, bill, cost, start = e
            if iso < start:
                continue
            if rng.random() < 0.045:  # leave day: no capacity
                continue
            pid = assignment.get(eid)
            target = GRADES[grade][3]
            if pid:
                billable = max(0.0, min(8.0, round(rng.gauss(8 * target, 0.9) * 2) / 2))
                ptype = PROJECTS[pid - 1][3]
                realization = 0.88 if ptype == "Fixed Fee" else (0.95 if ptype == "Retainer" else 1.0)
                revenue = round(billable * bill * realization, 2)
            else:
                billable, revenue = 0.0, 0.0
            row = (entry, iso, eid, pid, 8.0, billable, 8.0 - billable, revenue, round(8.0 * cost, 2),
                   f"{iso}T18:00:00")
            entry += 1
            if backlog and day in late_from and rng.random() < (0.75 - 0.2 * late_from.index(day)):
                backlog_rows.append(row)
            else:
                rows.append(row)
    con.executemany("INSERT INTO timesheets VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO timesheet_backlog VALUES (?,?,?,?,?,?,?,?,?,?)", backlog_rows)
    con.commit()
    con.close()


def ensure(db_path: Path) -> None:
    """Build the demo database if it is missing or doesn't reach the latest working day."""
    if db_path.exists():
        con = sqlite3.connect(db_path)
        try:
            (latest,) = con.execute(
                "SELECT MAX(work_date) FROM (SELECT work_date FROM timesheets UNION ALL "
                "SELECT work_date FROM timesheet_backlog)").fetchone()
        except sqlite3.Error:
            latest = None
        con.close()
        if latest == last_workday(dt.date.today()).isoformat():
            return
    build(db_path)


class LiveFeeder(threading.Thread):
    """Moves late timesheets from the backlog into the live table, a few every `interval` seconds."""

    def __init__(self, db_path: Path, interval: float = 8.0, batch: int = 6):
        super().__init__(daemon=True, name="live-feeder")
        self.db_path, self.interval, self.batch = db_path, interval, batch
        self.stopped = threading.Event()

    def step(self) -> int:
        con = sqlite3.connect(self.db_path, timeout=5)
        try:
            ids = [r[0] for r in con.execute(
                "SELECT entry_id FROM timesheet_backlog ORDER BY work_date, random() LIMIT ?", (self.batch,))]
            if ids:
                now = dt.datetime.now().isoformat(timespec="seconds")
                marks = ",".join("?" * len(ids))
                con.execute(f"INSERT INTO timesheets SELECT entry_id, work_date, employee_id, project_id, "
                            f"available_hours, billable_hours, nonbillable_hours, revenue, cost, ? "
                            f"FROM timesheet_backlog WHERE entry_id IN ({marks})", [now, *ids])
                con.execute(f"DELETE FROM timesheet_backlog WHERE entry_id IN ({marks})", ids)
                con.commit()
                return len(ids)
            # Backlog done: keep the demo live with small timesheet corrections on recent days.
            now = dt.datetime.now().isoformat(timespec="seconds")
            cur = con.execute(
                "UPDATE timesheets SET billable_hours = billable_hours + 0.5, "
                "nonbillable_hours = nonbillable_hours - 0.5, "
                "revenue = revenue + 0.5 * (SELECT bill_rate FROM employees e WHERE e.employee_id = timesheets.employee_id), "
                "submitted_at = ? WHERE entry_id IN (SELECT entry_id FROM timesheets WHERE project_id IS NOT NULL "
                "AND billable_hours <= available_hours - 0.5 AND work_date >= date((SELECT MAX(work_date) FROM timesheets), '-7 days') "
                "ORDER BY random() LIMIT 2)", (now,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def run(self):
        while not self.stopped.wait(self.interval):
            try:
                self.step()
            except sqlite3.Error:
                pass
