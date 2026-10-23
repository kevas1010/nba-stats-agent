from datetime import date

import duckdb
import pandas as pd
import pytest

from nba_agent.ingest.cli import current_season_start
from nba_agent.ingest.fetch import season_label, to_snake
from nba_agent.ingest.load import load_frame, write_parquet


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("FG3_PCT", "fg3_pct"),
        ("PLUS_MINUS", "plus_minus"),
        ("gameDateUTC", "game_date_utc"),
        ("homeTeam_teamId", "home_team_team_id"),
        ("already_snake", "already_snake"),
    ],
)
def test_to_snake(raw, expected):
    assert to_snake(raw) == expected


def test_season_label():
    assert season_label(2024) == "2024-25"
    assert season_label(1999) == "1999-00"


def test_current_season_start_rolls_over_in_october():
    assert current_season_start(date(2026, 9, 30)) == 2025
    assert current_season_start(date(2026, 10, 1)) == 2026


def _logs(season, season_type, pts):
    return pd.DataFrame({"game_id": ["g1", "g2"], "pts": pts, "season": season, "season_type": season_type})


def test_load_frame_replaces_only_its_partition():
    con = duckdb.connect()
    load_frame(con, _logs("2024-25", "Regular Season", [100, 110]), "team_game_logs",
               {"season": "2024-25", "season_type": "Regular Season"})
    load_frame(con, _logs("2023-24", "Regular Season", [90, 95]), "team_game_logs",
               {"season": "2023-24", "season_type": "Regular Season"})

    # Re-running 2024-25 swaps its rows and leaves 2023-24 untouched.
    load_frame(con, _logs("2024-25", "Regular Season", [120, 130]), "team_game_logs",
               {"season": "2024-25", "season_type": "Regular Season"})

    rows = con.execute("SELECT season, sum(pts) FROM raw.team_game_logs GROUP BY season ORDER BY season").fetchall()
    assert rows == [("2023-24", 185), ("2024-25", 250)]


def test_load_frame_rejects_unsafe_table_name():
    con = duckdb.connect()
    with pytest.raises(ValueError):
        load_frame(con, _logs("2024-25", "Playoffs", [1, 2]), "logs; DROP TABLE x", {"season": "2024-25"})


def test_write_parquet_path(tmp_path):
    path = write_parquet(_logs("2024-25", "Playoffs", [1, 2]), tmp_path, "team_game_logs", "2024-25", "Playoffs")
    assert path == tmp_path / "team_game_logs" / "2024-25_playoffs.parquet"
    assert len(pd.read_parquet(path)) == 2
