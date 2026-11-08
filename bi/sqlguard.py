"""Safety checks for SQL drafted by the local language model (the fallback path only).

Governed answers never come through here: they are compiled from the semantic model.
A drafted query is only run when every layer agrees it is a single read-only SELECT
over the tables the semantic model already exposes:

  1. Text checks: one statement, starts with SELECT/WITH, no comments, no write or
     session keywords, no dangerous functions, only allow-listed tables after FROM/JOIN.
  2. Database checks: SQLite runs it under an authorizer that denies everything except
     reads of allow-listed tables; PostgreSQL's plan (EXPLAIN, not executed) must touch
     only allow-listed tables.
  3. Execution limits: read-only snapshot, statement timeout, and a row cap.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import sys
from decimal import Decimal

MAX_ROWS = 200
MAX_LENGTH = 4000

FORBIDDEN_WORDS = {
    "insert", "update", "delete", "merge", "upsert", "drop", "alter", "create", "truncate", "rename",
    "attach", "detach", "pragma", "vacuum", "reindex", "analyze", "grant", "revoke", "copy", "call", "do",
    "execute", "exec", "prepare", "deallocate", "listen", "notify", "unlisten", "lock", "set", "reset",
    "begin", "commit", "rollback", "savepoint", "release", "load", "into", "returning", "comment", "security",
}
FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(pg_\w+|lo_\w+|dblink\w*|set_config|current_setting|query_to_xml\w*|load_extension|readfile|writefile|"
    r"fts3_tokenizer|edit|sqlite_\w+)\s*\(", re.I)
TABLE_REF = re.compile(r"\b(?:from|join)\s+((?:\"[^\"]+\"|\w+)(?:\s*\.\s*(?:\"[^\"]+\"|\w+))?)", re.I)
CTE_NAME = re.compile(r"(?:\bwith\b|,)\s*(?:recursive\s+)?(\"[^\"]+\"|\w+)\s*(?:\([^)]*\))?\s+as\s*\(", re.I)


class Rejected(ValueError):
    """The drafted SQL failed a safety check; the message says which, in plain words."""


def _bare(name: str) -> str:
    return name.split(".")[-1].strip().strip('"').lower()


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def check(sql: str, allowed_tables: set) -> str:
    """Return the cleaned statement or raise Rejected."""
    allowed = {t.lower() for t in allowed_tables}
    text = (sql or "").strip()
    if not text:
        raise Rejected("the local model returned no SQL")
    if len(text) > MAX_LENGTH:
        raise Rejected("the query is too long")
    text = text.rstrip().rstrip(";").rstrip()
    scan = _strip_literals(text)
    if "'" in scan.replace("''", ""):
        raise Rejected("a text value is not closed")
    if "--" in scan or "/*" in scan or "#" in scan:
        raise Rejected("comments are not allowed")
    if ";" in scan:
        raise Rejected("only one statement is allowed")
    if not re.match(r"\s*(select|with)\b", scan, re.I):
        raise Rejected("only SELECT queries are allowed")
    words = {w.lower() for w in re.findall(r"[A-Za-z_]+", re.sub(r'"[^"]*"', " ", scan))}
    blocked = sorted(words & FORBIDDEN_WORDS)
    if blocked:
        raise Rejected(f"it uses {blocked[0].upper()}, which can change data or settings")
    fn = FORBIDDEN_FUNCTIONS.search(scan)
    if fn:
        raise Rejected(f"it calls {fn.group(1)}(), which is not allowed")
    ctes = {_bare(n) for n in CTE_NAME.findall(scan)}
    for ref in TABLE_REF.findall(scan):
        name = _bare(ref)
        if name not in allowed and name not in ctes:
            raise Rejected(f"it reads {name}, which isn't one of the tables this source exposes")
    return text


def _authorizer(allowed: set):
    ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
    read_only = {sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}

    def decide(action, arg1, arg2, dbname, trigger):
        if action in read_only:
            return ok
        if action == sqlite3.SQLITE_READ:
            # arg1 is the table; CTEs and subqueries report the underlying real tables.
            return ok if (arg1 or "").lower() in allowed else deny
        if action == sqlite3.SQLITE_FUNCTION:
            return deny if FORBIDDEN_FUNCTIONS.search(f"{arg2}(") else ok
        return deny
    return decide


def _allow_all(*_):
    return sqlite3.SQLITE_OK


def _plan_relations(plan) -> set:
    found = set()
    if isinstance(plan, dict):
        if "Relation Name" in plan:
            found.add(plan["Relation Name"].lower())
        for value in plan.values():
            found |= _plan_relations(value)
    elif isinstance(plan, list):
        for value in plan:
            found |= _plan_relations(value)
    return found


def _plain(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return value


def execute(source, sql: str, allowed_tables: set) -> dict:
    """Run a checked statement inside the caller's read-only snapshot. Returns columns, rows, truncated."""
    allowed = {t.lower() for t in allowed_tables}
    capped = f"SELECT * FROM ({sql}) AS draft LIMIT {MAX_ROWS + 1}"
    con = source.con
    if source.kind == "postgres":
        (plan,) = con.execute("EXPLAIN (FORMAT JSON) " + capped).fetchone()
        plan = json.loads(plan) if isinstance(plan, str) else plan
        extra = sorted(_plan_relations(plan) - allowed)
        if extra:
            raise Rejected(f"it reads {extra[0]}, which isn't one of the tables this source exposes")
        with con.transaction():  # a savepoint: a failing draft must not abort the caller's snapshot
            cur = con.execute(capped)
            names = [d[0] for d in cur.description]
            fetched = cur.fetchall()
    else:
        con.set_authorizer(_authorizer(allowed))
        try:
            cur = con.execute(capped)
            names = [d[0] for d in cur.description]
            fetched = cur.fetchall()
        except sqlite3.DatabaseError as exc:
            if "not authorized" in str(exc) or "prohibited" in str(exc):
                raise Rejected("the database refused it: it reaches beyond the allowed tables") from exc
            raise
        finally:
            # Python < 3.11 can't clear an authorizer with None; an allow-all one restores normal behaviour.
            con.set_authorizer(None if sys.version_info >= (3, 11) else _allow_all)
    rows = [dict(zip(names, map(_plain, r))) for r in fetched[:MAX_ROWS]]
    return {"columns": names, "rows": rows, "truncated": len(fetched) > MAX_ROWS}
