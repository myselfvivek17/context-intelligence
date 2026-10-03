"""One SQLite connection, one writer lock. The MCP server is the only process that writes the file."""
import contextlib
import json
import os
import pathlib
import sqlite3
import threading
from datetime import datetime, timezone

import sqlite_vec

DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

# ponytail: a single connection behind an RLock. Fine for a personal memory server; switch to a reader
# pool + one writer connection if concurrent read latency ever shows up in the events table.
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (and migrate) the database. Call once at startup; tests pass a temp path."""
    global _conn
    path = path or DB_PATH
    if path != ":memory:":
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript((pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
    _conn = conn
    return conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        connect()
    return _conn


def query(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def one(sql: str, params=()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, params).fetchone()


@contextlib.contextmanager
def tx():
    """Serialized write transaction: BEGIN IMMEDIATE … COMMIT, rollback on error."""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def log_event(c: sqlite3.Connection, op: str, *, tool=None, agent=None, memory_id=None, latency_ms=None, **detail):
    c.execute("INSERT INTO events(ts, op, tool, agent, memory_id, latency_ms, detail) VALUES (?,?,?,?,?,?,?)",
              (now(), op, tool, agent, memory_id, latency_ms, json.dumps(detail, default=str)))


def row_dict(r: sqlite3.Row | None, drop=("embedding",)) -> dict | None:
    if r is None:
        return None
    d = {k: r[k] for k in r.keys() if k not in drop}
    for k in ("tags", "aliases", "extra", "trigger_tags", "examples", "detail"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
