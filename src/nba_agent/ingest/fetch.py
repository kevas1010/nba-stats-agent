"""Thin wrappers around nba_api endpoints that return tidy DataFrames."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pandas as pd
from nba_api.stats.endpoints import leaguegamelog, scheduleleaguev2

REQUEST_TIMEOUT = 60
RETRIES = 3
# stats.nba.com throttles aggressive clients; pause between calls.
PAUSE_SECONDS = 1.0

SEASON_TYPES = ("Regular Season", "Playoffs")

SCHEDULE_COLUMNS = {
    "gameId": "game_id",
    "seasonYear": "season_year",
    "gameDateTimeUTC": "game_time_utc",
    "gameStatus": "game_status",
    "gameStatusText": "game_status_text",
    "gameLabel": "game_label",
    "arenaName": "arena_name",
    "arenaCity": "arena_city",
    "homeTeam_teamId": "home_team_id",
    "homeTeam_teamTricode": "home_team",
    "homeTeam_score": "home_score",
    "awayTeam_teamId": "away_team_id",
    "awayTeam_teamTricode": "away_team",
    "awayTeam_score": "away_score",
}


def to_snake(name: str) -> str:
    """'FG3_PCT' -> 'fg3_pct', 'gameDateUTC' -> 'game_date_utc'."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return name.lower()


def season_label(start_year: int) -> str:
    """2024 -> '2024-25'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def _call(endpoint_cls, **kwargs) -> list[pd.DataFrame]:
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            frames = endpoint_cls(timeout=REQUEST_TIMEOUT, **kwargs).get_data_frames()
            time.sleep(PAUSE_SECONDS)
            return frames
        except Exception as exc:  # nba_api raises plain requests/JSON errors
            last_error = exc
            time.sleep(PAUSE_SECONDS * 2**attempt)
    raise RuntimeError(f"{endpoint_cls.__name__} failed after {RETRIES} tries") from last_error


def _stamp(df: pd.DataFrame, season: str, season_type: str | None = None) -> pd.DataFrame:
    df = df.rename(columns=to_snake)
    df["season"] = season
    if season_type is not None:
        df["season_type"] = season_type
    df["ingested_at"] = datetime.now(timezone.utc)
    return df


def fetch_game_logs(season: str, level: str, season_type: str = "Regular Season") -> pd.DataFrame:
    """One row per team-game (level='team') or player-game (level='player')."""
    flag = {"team": "T", "player": "P"}[level]
    df = _call(
        leaguegamelog.LeagueGameLog,
        season=season,
        player_or_team_abbreviation=flag,
        season_type_all_star=season_type,
    )[0]
    df = df.drop(columns=["VIDEO_AVAILABLE"], errors="ignore")
    return _stamp(df, season, season_type)


def fetch_schedule(season: str) -> pd.DataFrame:
    """Full league schedule for a season, including games not yet played."""
    df = _call(scheduleleaguev2.ScheduleLeagueV2, season=season)[0]
    df = df[list(SCHEDULE_COLUMNS)].rename(columns=SCHEDULE_COLUMNS)
    df["game_time_utc"] = pd.to_datetime(df["game_time_utc"], utc=True)
    return _stamp(df, season)
