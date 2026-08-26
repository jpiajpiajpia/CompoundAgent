"""SQLite access. One connection helper, schema bootstrap, tiny run log."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from . import paths


def utcnow() -> str:
    """ISO8601 UTC to the second. Used for every timestamp in the ledger."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path=None) -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(str(db_path or paths.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(paths.SCHEMA_PATH.read_text())
    conn.commit()


@contextmanager
def session(db_path=None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


class RunLog:
    """Records one trigger firing. Every scheduled job wraps itself in this so
    there is always an audit trail of what ran, when, and what it decided."""

    def __init__(self, conn: sqlite3.Connection, trigger: str):
        self.conn = conn
        self.trigger = trigger
        self.id: Optional[int] = None
        self.payload: Dict[str, Any] = {}

    def __enter__(self) -> "RunLog":
        cur = self.conn.execute(
            "INSERT INTO runs (trigger, started_at, status) VALUES (?,?,?)",
            (self.trigger, utcnow(), "running"),
        )
        self.id = cur.lastrowid
        self.conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        status = "ok" if exc_type is None else "error"
        if exc_type is not None:
            self.payload["error"] = repr(exc)
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, payload_json=? WHERE id=?",
            (utcnow(), status, json.dumps(self.payload, default=str), self.id),
        )
        self.conn.commit()
        return False  # never swallow exceptions
