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
| Data | 2–3 seasons of box scores, play-by-play and schedules from `nba_api` | planned |
| Warehouse | Snowflake, with a DuckDB fallback for local runs | planned |
| Modeling | dbt Core: staging models, `player_game_stats` and `team_pace` marts, dbt tests | planned |
| Agent | Python MCP server with `query_stats`, `search_rulebook` (RAG), `get_schedule` | planned |
| Security | Read-only warehouse role, SQL allowlist blocking DDL/DML, secrets in env vars | planned |
| Evals | ~50-question golden set; accuracy, p50/p95 latency, cost per query; RAG vs no-RAG | planned |
| UI | React/TypeScript chat with charts; Tableau Public dashboard of eval results | planned |
| Process | pytest, GitHub Actions CI | planned |

## Setup

```bash
cp .env.example .env   # fill in your own keys; .env is never committed
python -m venv .venv
```

Further setup steps will be added as each component lands.
