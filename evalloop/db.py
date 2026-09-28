"""Chinook access. Every query the agent (or a gold label) runs goes through `execute`.

Safety model (defence in depth):
  1. the database is opened read-only (mode=ro), so a write cannot succeed;
  2. only a single statement is allowed (sqlite3 rejects multi-statement strings);
  3. a progress handler aborts queries that run longer than `timeout_s`;
  4. results are capped at `max_rows`.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CHINOOK_DB", ROOT / "data" / "Chinook.sqlite"))


@dataclass
class ExecResult:
    ok: bool
    rows: list = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def schema_ddl() -> str:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return "\n\n".join(r[0] for r in rows)


def execute(sql: str, timeout_s: float = 5.0, max_rows: int = 10_000) -> ExecResult:
    conn = _connect()
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20_000)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return ExecResult(True, rows[:max_rows], truncated=truncated)
    except Exception as e:  # sqlite3.Error, ProgrammingError for multi-statements, interrupted
        return ExecResult(False, error=f"{type(e).__name__}: {e}")
    finally:
        conn.close()
