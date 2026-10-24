# NBA Stats Agent

A chat assistant that answers basketball questions by querying a stats warehouse and searching the official NBA rulebook, with measured accuracy, latency and cost.

> Example: *"Did the Knicks play faster after the All-Star break?"* → the agent queries team pace, compares the two periods, and explains the result.

## Architecture

```
nba_api ──► ingest (Python) ──► Snowflake / DuckDB ──► dbt models (staging → marts)
                                                          │
                     NBA rulebook PDF ──► chunk + embed ──┤
                                                          ▼
                                   MCP server: query_stats · search_rulebook · get_schedule
                                                          │
                                                   Claude agent (multi-step tool calling)
                                                          │
                                         React/TypeScript chat UI  ·  eval harness
```

## Components

| Area | What | Status |
|---|---|---|
| Data | 3 seasons of team and player box scores (regular season + playoffs) and the current schedule from `nba_api`; play-by-play next | in progress |
| Warehouse | DuckDB raw layer locally; Snowflake next | in progress |
| Modeling | dbt Core on DuckDB: staging views, `player_game_stats` and `team_pace` marts, 34 data tests | done (DuckDB) |
| Agent | Python MCP server with `query_stats` and `get_schedule`; `search_rulebook` (RAG) next | in progress |
| Security | SQL allowlist (sqlglot) + read-only, no-file-access DuckDB connection; secrets in env vars; Snowflake role next | in progress |
| Evals | ~50-question golden set; accuracy, p50/p95 latency, cost per query; RAG vs no-RAG | planned |
| UI | React/TypeScript chat with charts; Tableau Public dashboard of eval results | planned |
| Process | pytest, GitHub Actions CI | planned |

## Setup

Requires Python 3.11 or 3.12.

```bash
cp .env.example .env   # fill in your own keys; .env is never committed
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"   # .venv/bin/pip on macOS/Linux
```

### Ingest data

```bash
nba-ingest                        # last 3 completed seasons + current season's schedule
nba-ingest --seasons 2024 2025    # specific seasons (by start year)
```

This writes Parquet files to `data/raw/` and loads `raw.team_game_logs`, `raw.player_game_logs` and `raw.schedule` into `data/nba.duckdb`. Re-running a season replaces only that season's rows.

### Build the models

```bash
cd dbt
dbt build --profiles-dir .    # seeds, models and data tests
```

| Schema | Model | Grain |
|---|---|---|
| `staging` | `stg_team_game_logs`, `stg_player_game_logs`, `stg_schedule` | cleaned raw tables (views) |
| `marts` | `player_game_stats` | player × game: box score, eFG%, TS%, usage rate, Game Score |
| `marts` | `team_pace` | team × game: possessions, pace, off/def/net rating, pre/post All-Star split |
| `reference` | `all_star_games` (seed) | season → All-Star Game date |

Data tests cover uniqueness of every grain, accepted values, relationships, team points matching the sum of player points, and pace staying in a plausible range.

### Run the MCP server

```bash
nba-mcp    # stdio transport
```

| Tool | What it does |
|---|---|
| `query_stats(sql)` | Runs one read-only SELECT against `marts.player_game_stats`, `marts.team_pace` and `reference.all_star_games`. The tool description includes every column, so the model can write SQL without a separate schema lookup. Returns columns, rows (max 500), a truncation flag and timing. |
| `get_schedule(team?, start_date?, days=7)` | Games in a window from the current season's schedule, by US Eastern game date, with scores for finished games. Preseason excluded. |

To use it from Claude Code:

```bash
claude mcp add nba-stats -- /path/to/nba-stats-agent/.venv/Scripts/nba-mcp
```

Run it from the repo root (or set `DUCKDB_PATH`) so it finds `data/nba.duckdb`.

#### How agent SQL is kept read-only

Two independent layers, so a bug in one doesn't open the warehouse:

1. **SQL guard** ([sql_guard.py](src/nba_agent/mcp_server/sql_guard.py)): parses the query with sqlglot and rejects anything that isn't exactly one `SELECT`. It also blocks DDL/DML/`COPY`/`ATTACH`/`PRAGMA`/`SET`, table functions like `read_csv` or `glob`, file paths in `FROM`, functions like `getenv`, and any table outside the allowlist. CTEs can't be used to smuggle in a blocked table.
2. **Connection** ([warehouse.py](src/nba_agent/mcp_server/warehouse.py)): DuckDB is opened read-only with external file/network access off and configuration locked, so even unguarded SQL can't write, read files or turn access back on. Queries are capped at 500 rows and interrupted after 15 seconds.

Rejections and SQL errors come back to the model as MCP tool errors with the reason, so it can fix the query and retry.

### Test

```bash
pytest
```

75 tests cover ingestion helpers, the SQL guard (allowed and rejected queries), the read-only connection, both tools, and real MCP client calls against the server.
