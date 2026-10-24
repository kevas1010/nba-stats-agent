"""MCP server: query_stats and get_schedule over the DuckDB warehouse.

    nba-mcp            # stdio transport, for Claude Desktop / Claude Code / the agent
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nba_agent.mcp_server.sql_guard import ALLOWED_TABLES, UnsafeQueryError
from nba_agent.mcp_server.warehouse import MAX_ROWS, connect, default_db_path, run_query

MAX_SCHEDULE_DAYS = 60
_TEAM = re.compile(r"^[A-Za-z]{3}$")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

QUERY_STATS_DOC = """Run one read-only DuckDB SELECT against the NBA stats marts.

Only these tables can be queried (schema-qualified or bare names both work):
{schema}

Seasons are labelled like '2024-25'; season_type is 'Regular Season' or 'Playoffs'.
Team columns use three-letter codes ('NYK', 'BOS'). Percentages and rates are
0-1 fractions. Results are capped at {max_rows} rows; aggregate in SQL rather
than pulling raw rows. DDL, DML, file access and other schemas are rejected.
"""


def describe_schema(db_path: Path | None = None) -> str:
    """Column listing for the allowlisted tables, used in the tool description."""
    pairs = [(schema, table) for schema, tables in ALLOWED_TABLES.items() for table in sorted(tables)]
    try:
        con = connect(db_path)
    except FileNotFoundError:
        return "\n".join(f"- {s}.{t}" for s, t in pairs) + "\n(warehouse not built yet; columns unavailable)"
    lines = []
    with con:
        for schema, table in pairs:
            cols = run_query(
                con,
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? order by ordinal_position",
                [schema, table],
                guarded=False,
            ).rows
            col_text = ", ".join(f"{name} {dtype.lower()}" for name, dtype in cols) or "(missing; run dbt build)"
            lines.append(f"- {schema}.{table}: {col_text}")
    return "\n".join(lines)


def query_stats(sql: str, db_path: Path | None = None) -> dict[str, Any]:
    try:
        with connect(db_path) as con:
            result = run_query(con, sql)
    except UnsafeQueryError as exc:
        raise ToolError(f"query rejected: {exc}") from exc
    except TimeoutError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        # DuckDB binder/parser errors are useful to the model for fixing its SQL.
        if type(exc).__module__.startswith(("duckdb", "_duckdb")):
            raise ToolError(f"query failed: {exc}") from exc
        raise
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
    }


def get_schedule(
    team: str | None = None,
    start_date: str | None = None,
    days: int = 7,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if team is not None and not _TEAM.match(team):
        raise ToolError("team must be a three-letter code like 'NYK'")
    if not 1 <= days <= MAX_SCHEDULE_DAYS:
        raise ToolError(f"days must be between 1 and {MAX_SCHEDULE_DAYS}")
    try:
        start = date.fromisoformat(start_date) if start_date else None
    except ValueError as exc:
        raise ToolError("start_date must be YYYY-MM-DD") from exc
    team_code = team.upper() if team else None

    try:
        with connect(db_path) as con:
            # The NBA lists games by Eastern date, so "today" is today in New York.
            if start is None:
                start = con.execute("select cast(timezone('America/New_York', now()) as date)").fetchone()[0]
            end = start + timedelta(days=days - 1)
            # Fixed SQL written here with bound parameters, so it skips the agent guard.
            result = run_query(
                con,
                """
                select game_id, game_date_et, game_time_utc, status, away_team, home_team,
                       away_score, home_score, arena_name, arena_city, game_label
                from staging.stg_schedule
                where game_date_et between ? and ?
                  and not is_preseason
                  and (? is null or home_team = ? or away_team = ?)
                order by game_time_utc, game_id
                """,
                [start, end, team_code, team_code, team_code],
                guarded=False,
            )
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "team": team_code,
        "games": [dict(zip(result.columns, row)) for row in result.rows],
    }


def create_server(db_path: Path | None = None) -> MCPServer:
    server = MCPServer(
        name="nba-stats",
        instructions=(
            "Answer NBA questions from a stats warehouse covering recent seasons. "
            "Use query_stats for historical box scores, pace and ratings, and "
            "get_schedule for upcoming or recent games."
        ),
    )

    @server.tool(
        name="query_stats",
        description=QUERY_STATS_DOC.format(schema=describe_schema(db_path), max_rows=MAX_ROWS),
        annotations=READ_ONLY,
    )
    def _query_stats(sql: str) -> dict[str, Any]:
        return query_stats(sql, db_path)

    @server.tool(
        name="get_schedule",
        description=(
            "List NBA games in a date window from the current season's schedule, "
            "with scores for finished games. team is a three-letter code like 'NYK' "
            "(omit for all teams); start_date is YYYY-MM-DD by US Eastern game date (default today); "
            f"days is the window length, 1-{MAX_SCHEDULE_DAYS} (default 7). Preseason is excluded."
        ),
        annotations=READ_ONLY,
    )
    def _get_schedule(team: str | None = None, start_date: str | None = None, days: int = 7) -> dict[str, Any]:
        return get_schedule(team, start_date, days, db_path)

    return server


def main() -> None:
    load_dotenv()
    create_server(default_db_path().resolve()).run("stdio")


if __name__ == "__main__":
    main()
