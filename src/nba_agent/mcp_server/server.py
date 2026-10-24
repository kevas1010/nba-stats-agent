"""MCP server: query_stats, get_schedule and search_rulebook over the DuckDB warehouse.

    nba-mcp            # stdio transport, for Claude Desktop / Claude Code / the agent
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nba_agent.mcp_server.sql_guard import ALLOWED_TABLES, UnsafeQueryError
from nba_agent.mcp_server.warehouse import MAX_ROWS, connect, default_db_path, run_query
from nba_agent.rulebook.embed import Embedder, FastEmbedder
from nba_agent.rulebook.search import RulebookSearcher, index_version
from nba_agent.rulebook.source import RULEBOOK_SEASON

MAX_SCHEDULE_DAYS = 60
MAX_RULEBOOK_RESULTS = 10
MAX_QUERY_CHARS = 500
_TEAM = re.compile(r"^[A-Za-z]{3}$")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

QUERY_STATS_DOC = """Run one read-only DuckDB SELECT against the NBA stats marts.

Only these tables can be queried (schema-qualified or bare names both work):
{schema}

Seasons are labelled like '2024-25'; season_type is 'Regular Season' or 'Playoffs'.
Team columns use three-letter codes ('NYK', 'BOS'). Percentages and rates are
0-1 fractions. shot_events has one row per field goal attempt from play-by-play
(period 5+ is overtime; is_clutch = last 5:00 of the 4th/OT within 5 points).
Results are capped at {max_rows} rows; aggregate in SQL rather than pulling raw
rows. DDL, DML, file access and other schemas are rejected.
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


class RulebookTool:
    """search_rulebook with a lazily loaded model and index.

    The embedding model takes a few seconds to load, so it's created on the
    first search, not at server start. The index is reloaded whenever
    nba-rulebook rebuilds it, without restarting the server.
    """

    def __init__(self, db_path: Path | None = None, embedder_factory: Callable[[], Embedder] = FastEmbedder) -> None:
        self.db_path = db_path
        self.embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self._searcher: RulebookSearcher | None = None

    def _current_searcher(self) -> RulebookSearcher:
        try:
            with connect(self.db_path) as con:
                version = index_version(con)
                if version is None:
                    raise ToolError("rulebook index not built yet; run nba-rulebook")
                if self._searcher is None or self._searcher.version != version:
                    if self._embedder is None:
                        self._embedder = self.embedder_factory()
                    self._searcher = RulebookSearcher.load(con, self._embedder)
        except FileNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        return self._searcher

    def __call__(self, query: str, k: int = 5) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ToolError("query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolError(f"query must be at most {MAX_QUERY_CHARS} characters")
        if not 1 <= k <= MAX_RULEBOOK_RESULTS:
            raise ToolError(f"k must be between 1 and {MAX_RULEBOOK_RESULTS}")

        hits = self._current_searcher().search(query, k)
        return {
            "rulebook": f"{RULEBOOK_SEASON} Official NBA Playing Rules",
            "results": [
                {
                    "citation": hit.citation,
                    "rule": hit.rule,
                    "section": hit.section,
                    "page_start": hit.page_start,
                    "page_end": hit.page_end,
                    "text": hit.text,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }


def create_server(
    db_path: Path | None = None,
    embedder_factory: Callable[[], Embedder] = FastEmbedder,
) -> MCPServer:
    server = MCPServer(
        name="nba-stats",
        instructions=(
            "Answer NBA questions from a stats warehouse covering recent seasons. "
            "Use query_stats for historical box scores, pace and ratings, "
            "get_schedule for upcoming or recent games, and search_rulebook for "
            "rules questions. Cite the rule, section and page for rules answers."
        ),
    )
    search_rulebook = RulebookTool(db_path, embedder_factory)

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

    @server.tool(
        name="search_rulebook",
        description=(
            f"Search the {RULEBOOK_SEASON} Official NBA Playing Rules for passages relevant to a "
            "rules question (fouls, violations, shot clock, timeouts, replay, coach's challenge...). "
            "Plain-language questions work; hybrid semantic + keyword search. Returns the top k "
            f"passages (default 5, max {MAX_RULEBOOK_RESULTS}) with rule, section and page numbers. "
            "Answer only from the returned text and cite the citation field; if no passage covers "
            "the question, say the rulebook excerpt doesn't address it."
        ),
        annotations=READ_ONLY,
    )
    def _search_rulebook(query: str, k: int = 5) -> dict[str, Any]:
        return search_rulebook(query, k)

    return server


def main() -> None:
    load_dotenv()
    create_server(default_db_path().resolve()).run("stdio")


if __name__ == "__main__":
    main()
