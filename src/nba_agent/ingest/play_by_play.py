"""Resumable play-by-play ingestion: one API call per game, ~4,000 games for three seasons.

Games come from raw.team_game_logs, so run the box-score ingest first. Games
already in raw.play_by_play are skipped, so an interrupted run picks up where
it stopped. Rows are written in small batches through short-lived connections,
which lets dbt or the MCP server use the database between batches.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import duckdb
import pandas as pd

from nba_agent.ingest.fetch import fetch_play_by_play
from nba_agent.ingest.load import RAW_SCHEMA

TABLE = f"{RAW_SCHEMA}.play_by_play"
BATCH_GAMES = 25
# stats.nba.com throttles in bursts: requests hang, then recover after a minute or so.
# After this many failures in a row, pause before trying the next game.
COOLDOWN_AFTER_FAILURES = 3
COOLDOWN_SECONDS = 60.0


def pending_games(db: Path | str, seasons: list[str]) -> list[tuple[str, str, str]]:
    """(game_id, season, season_type) for every logged game not yet in raw.play_by_play."""
    with duckdb.connect(str(db), read_only=True) as con:
        has_pbp = con.execute(
            "select count(*) from information_schema.tables where table_schema = ? and table_name = 'play_by_play'",
            [RAW_SCHEMA],
        ).fetchone()[0]
        done = "select distinct game_id from raw.play_by_play" if has_pbp else "select null::varchar as game_id where false"
        return con.execute(
            f"""
            select distinct g.game_id, g.season, g.season_type
            from raw.team_game_logs g
            where g.season in (select unnest(?::varchar[]))
              and g.game_id not in ({done})
            order by g.game_id
            """,
            [seasons],
        ).fetchall()


def write_batch(db: Path | str, frames: list[pd.DataFrame]) -> int:
    if not frames:
        return 0
    batch = pd.concat(frames, ignore_index=True)
    with duckdb.connect(str(db)) as con:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
        con.register("incoming", batch)
        try:
            con.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} AS SELECT * FROM incoming LIMIT 0")
            # A game is written whole or not at all; drop any partial rows before inserting.
            con.execute(f"DELETE FROM {TABLE} WHERE game_id IN (SELECT DISTINCT game_id FROM incoming)")
            con.execute(f"INSERT INTO {TABLE} BY NAME SELECT * FROM incoming")
        finally:
            con.unregister("incoming")
    return len(batch)


def ingest_play_by_play(
    db: Path | str,
    seasons: list[str],
    *,
    limit: int | None = None,
    fetch: Callable[[str, str, str], pd.DataFrame] = fetch_play_by_play,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    games = pending_games(db, seasons)
    if limit is not None:
        games = games[:limit]
    log(f"play-by-play: {len(games)} games to fetch")

    stats = {"games": 0, "rows": 0, "empty": 0, "failed": 0}
    frames: list[pd.DataFrame] = []
    started = time.perf_counter()
    streak = 0
    for i, (game_id, season, season_type) in enumerate(games, start=1):
        try:
            df = fetch(game_id, season, season_type)
            streak = 0
        except Exception as exc:  # keep going; the next run retries this game
            stats["failed"] += 1
            streak += 1
            log(f"  {game_id}: failed ({exc.__class__.__name__}), will retry next run")
            if streak >= COOLDOWN_AFTER_FAILURES:
                log(f"  {streak} failures in a row; cooling down {COOLDOWN_SECONDS:.0f}s")
                sleep(COOLDOWN_SECONDS)
                streak = 0
            continue
        if df.empty:
            stats["empty"] += 1
            continue
        frames.append(df)
        stats["games"] += 1
        if len(frames) >= BATCH_GAMES or i == len(games):
            stats["rows"] += write_batch(db, frames)
            frames = []
            rate = i / (time.perf_counter() - started)
            log(f"  {i}/{len(games)} games, {stats['rows']:,} rows, ~{(len(games) - i) / rate / 60:.0f} min left")
    stats["rows"] += write_batch(db, frames)
    return stats
