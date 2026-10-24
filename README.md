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
| Agent | Python MCP server with `query_stats`, `get_schedule` and `search_rulebook` (hybrid RAG over the 2026-27 rulebook); multi-step Claude agent (`nba-ask`) with per-step token, cost and latency logging | done |
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

### Build the rulebook index

```bash
nba-rulebook    # downloads the official PDF once, then chunks and embeds it (~10s)
```

This fetches the [2026-27 Official NBA Playing Rules](https://official.nba.com/rulebook) PDF into `data/raw/rulebook/` (gitignored; the PDF is the NBA's and isn't redistributed here). It then builds `rulebook.chunks` and `rulebook.meta` in `data/nba.duckdb`:

- **Chunking** ([chunk.py](src/nba_agent/rulebook/chunk.py)): text is split along the rulebook's own structure (Rule → Section, Rule 12's Technical/Personal Foul parts, and the Comments on the Rules), packed into chunks of at most 1,200 characters, with breaks between lettered items. That gives 250 chunks across 122 sections, each tagged with its printed page numbers. The table of contents and signal-chart pages are skipped. pypdf splits some kerned words ("PENAL TIES", "T echnical"). They're rejoined only when the joined word appears elsewhere in the rulebook, so real pairs like "a way" or "in to" stay apart.
- **Embedding** ([embed.py](src/nba_agent/rulebook/embed.py)): `BAAI/bge-small-en-v1.5` via fastembed, running locally on ONNX, so there's no API key and no per-query cost. The model (~65 MB) is cached in `data/models/`.
- **Search** ([search.py](src/nba_agent/rulebook/search.py)): hybrid retrieval. Cosine similarity over the embeddings catches paraphrases ("take someone down on a fast break" → Transition Take Foul). BM25 catches the rulebook's exact wording ("hang on the rim" → Basket Interference). The two rankings are merged with reciprocal rank fusion. Both run in memory over the 250 chunks, so no database connection is held between searches.

### Run the MCP server

```bash
nba-mcp    # stdio transport
```

| Tool | What it does |
|---|---|
| `query_stats(sql)` | Runs one read-only SELECT against `marts.player_game_stats`, `marts.team_pace` and `reference.all_star_games`. The tool description includes every column, so the model can write SQL without a separate schema lookup. Returns columns, rows (max 500), a truncation flag and timing. |
| `get_schedule(team?, start_date?, days=7)` | Games in a window from the current season's schedule, by US Eastern game date, with scores for finished games. Preseason excluded. |
| `search_rulebook(query, k=5)` | Top-k rulebook passages for a rules question, each with a citation like `Rule 7 — Shot Clock, Section IV — Resetting Shot Clock, p. 29`. The model loads on the first search, and the index reloads automatically after `nba-rulebook` rebuilds it. |

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

### Ask the agent

Put your key in `.env` (`ANTHROPIC_API_KEY=...`, see `.env.example`), then:

```bash
nba-ask "Did the Knicks play faster after the All-Star break?"
nba-ask -v "When does the shot clock reset to 14 seconds?"   # also print each tool call's input
nba-ask                                                      # interactive session
nba-ask --report                                             # p50/p95 latency, tokens and cost over logged runs
```

Each answer is followed by a trace:

```
-- 3 steps, 4 tool calls, 9.8s (model 8.4s, tools 1.43s summed; parallel calls overlap)
-- 4,500 in / 270 out tokens (7,500 cache read, 3,100 cache write), $0.0404, claude-opus-5-5 (medium)
   step 1: 3900ms, 3,200+0 cached in, 120 out -> tool_use: query_stats 188ms, search_rulebook 920ms, get_schedule 265ms
   step 2: 2600ms, 900+3,300 cached in, 60 out -> tool_use: query_stats [error] 59ms
   step 3: 1900ms, 400+4,200 cached in, 90 out -> end_turn
```

Every run is appended to `data/logs/agent_runs.jsonl` (gitignored) with the question, answer, totals and one record per model call and tool call. That log is what the eval harness and dashboard will read.

How the agent works ([loop.py](src/nba_agent/agent/loop.py)):

- **Hand-written tool loop** instead of the SDK's beta tool runner, so each Messages API call and each tool call can be timed and its usage recorded. It spawns `nba-mcp` over stdio, the same way Claude Desktop does, and turns the server's tool list into Claude tool definitions ([mcp_tools.py](src/nba_agent/agent/mcp_tools.py)).
- **Model:** `claude-opus-5-5` at `medium` effort by default (`--model`, `--effort`, or `NBA_AGENT_MODEL` / `NBA_AGENT_EFFORT`).
- **Parallel tools:** when Claude asks for several tools in one turn, they run concurrently and all results go back in one message. Tool failures are returned with `is_error` so Claude can correct its SQL and retry.
- **Prompt caching:** tools are listed in a fixed order, and the system prompt has nothing that changes per request (today's date goes in the user message). Each step re-reads the growing conversation from cache at about a tenth of the input price.
- **Refusal fallback:** `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). If a safety classifier declines, the API retries on a fallback model, and that turn is priced at the fallback model's own rates from `usage.iterations`.
- **Append-only history:** the full assistant content (thinking and tool-use blocks) is sent back unchanged each step. A run stops at `end_turn`, a refusal, or after 10 steps.
- **Cost** ([pricing.py](src/nba_agent/agent/pricing.py)): estimated from first-party list prices, counting cache reads and writes separately. An unknown model is reported as unpriced rather than $0.

### Test

```bash
pytest
```

142 tests cover ingestion helpers, the SQL guard (allowed and rejected queries), the read-only connection, rulebook chunking and word repair, the index, hybrid search, all three tools, parallel MCP calls, and the agent loop, telemetry and CLI. Agent tests drive the real MCP server with a scripted stand-in for the Claude client ([fake_claude.py](tests/fake_claude.py)), so they run offline and cost nothing. Rulebook tests use a small deterministic embedder, so they don't need the model download.
