"""nba-ingest: pull game logs and schedules into data/raw and DuckDB.

    nba-ingest                       # last 3 completed seasons + current schedule
    nba-ingest --seasons 2024 2025   # season start years
    nba-ingest --play-by-play        # every event of every logged game (~2 hours, resumable)
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from nba_agent.ingest.fetch import SEASON_TYPES, fetch_game_logs, fetch_schedule, season_label
from nba_agent.ingest.load import load_frame, write_parquet
from nba_agent.ingest.play_by_play import ingest_play_by_play


def current_season_start(today: date) -> int:
    """NBA seasons start in October; before that we're still in last year's season."""
    return today.year if today.month >= 10 else today.year - 1


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    current = current_season_start(date.today())

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seasons", type=int, nargs="+", default=[current - 3, current - 2, current - 1],
                        help="season start years for game logs")
    parser.add_argument("--schedule-season", type=int, default=current, help="season start year for the schedule")
    parser.add_argument("--db", default=os.getenv("DUCKDB_PATH", "data/nba.duckdb"))
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--play-by-play", action="store_true",
                        help="fetch play-by-play for games already in raw.team_game_logs")
    parser.add_argument("--limit", type=int, help="with --play-by-play: fetch at most this many games")
    args = parser.parse_args(argv)

    if args.play_by_play:
        stats = ingest_play_by_play(args.db, [season_label(s) for s in args.seasons], limit=args.limit)
        print(f"play-by-play done: {stats}")
        return

    raw_dir = Path(args.raw_dir)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    with duckdb.connect(args.db) as con:
        for start in args.seasons:
            season = season_label(start)
            for season_type in SEASON_TYPES:
                for level in ("team", "player"):
                    table = f"{level}_game_logs"
                    df = fetch_game_logs(season, level, season_type)
                    if df.empty:
                        print(f"{season} {season_type} {level}: no rows, skipped")
                        continue
                    write_parquet(df, raw_dir, table, season, season_type)
                    n = load_frame(con, df, table, {"season": season, "season_type": season_type})
                    print(f"{season} {season_type} {level}: {n} rows")

        season = season_label(args.schedule_season)
        df = fetch_schedule(season)
        write_parquet(df, raw_dir, "schedule", season)
        n = load_frame(con, df, "schedule", {"season": season})
        print(f"{season} schedule: {n} games")


if __name__ == "__main__":
    main()
