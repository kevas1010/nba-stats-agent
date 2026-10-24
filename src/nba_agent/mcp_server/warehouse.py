"""Read-only access to the DuckDB warehouse for MCP tools.

Second layer of defense behind sql_guard: the connection is opened read-only,
with external file/network access disabled and configuration locked so a
query can't turn either back on. DuckDB has no per-table grants, so limiting
reads to the marts is sql_guard's job (a Snowflake role will enforce it too).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from nba_agent.mcp_server.sql_guard import check_query

MAX_ROWS = 500
TIMEOUT_SECONDS = 15.0


def default_db_path() -> Path:
    return Path(os.getenv("DUCKDB_PATH", "data/nba.duckdb"))


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float
    row_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.row_count = len(self.rows)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def connect(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path else default_db_path()
    if not path.exists():
        raise FileNotFoundError(f"warehouse not found at {path}; run nba-ingest and dbt build first")
    con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})
    con.execute("SET search_path = 'marts,reference'")
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET lock_configuration = true")
    return con


def run_query(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
    *,
    guarded: bool = True,
    max_rows: int = MAX_ROWS,
    timeout: float = TIMEOUT_SECONDS,
) -> QueryResult:
    """Run a query and return at most max_rows rows.

    guarded=False is only for SQL written in this codebase (e.g. get_schedule),
    never for SQL that came from the agent.
    """
    if guarded:
        check_query(sql)

    # DuckDB has no statement timeout; interrupt the connection from a timer.
    timer = threading.Timer(timeout, con.interrupt)
    started = time.perf_counter()
    timer.start()
    try:
        cursor = con.execute(sql, params or [])
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException as exc:
        raise TimeoutError(f"query exceeded {timeout:.0f}s") from exc
    finally:
        timer.cancel()
    elapsed_ms = (time.perf_counter() - started) * 1000

    truncated = len(rows) > max_rows
    rows = [[_json_safe(v) for v in row] for row in rows[:max_rows]]
    return QueryResult(columns=columns, rows=rows, truncated=truncated, elapsed_ms=round(elapsed_ms, 1))
