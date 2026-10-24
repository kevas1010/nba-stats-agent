import duckdb
import pandas as pd
import pytest

from nba_agent.ingest.play_by_play import ingest_play_by_play, pending_games


@pytest.fixture
def logged_games(tmp_path):
    db = tmp_path / "nba.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE SCHEMA raw")
        con.execute("""
            CREATE TABLE raw.team_game_logs AS SELECT * FROM (VALUES
                ('g1', '2024-25', 'Regular Season'), ('g1', '2024-25', 'Regular Season'),
                ('g2', '2024-25', 'Regular Season'), ('g2', '2024-25', 'Regular Season'),
                ('g3', '2024-25', 'Playoffs'),       ('g3', '2024-25', 'Playoffs'),
                ('g9', '2023-24', 'Regular Season'), ('g9', '2023-24', 'Regular Season')
            ) t(game_id, season, season_type)
        """)
    return db


def fake_fetch(calls, fail=(), empty=()):
    def fetch(game_id, season, season_type):
        calls.append(game_id)
        if game_id in fail:
            raise ConnectionError("throttled")
        if game_id in empty:
            return pd.DataFrame()
        return pd.DataFrame({
            "game_id": [game_id] * 3, "action_number": [1, 2, 3],
            "season": season, "season_type": season_type,
        })
    return fetch


def rows(db):
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute("select game_id, count(*) from raw.play_by_play group by 1 order by 1").fetchall()


def test_pending_games_dedupes_and_filters_seasons(logged_games):
    assert pending_games(logged_games, ["2024-25"]) == [
        ("g1", "2024-25", "Regular Season"), ("g2", "2024-25", "Regular Season"), ("g3", "2024-25", "Playoffs"),
    ]


def test_ingest_loads_every_game(logged_games):
    calls = []
    stats = ingest_play_by_play(logged_games, ["2024-25"], fetch=fake_fetch(calls), log=lambda _: None)
    assert stats == {"games": 3, "rows": 9, "empty": 0, "failed": 0}
    assert rows(logged_games) == [("g1", 3), ("g2", 3), ("g3", 3)]


def test_rerun_skips_loaded_games_and_retries_failures(logged_games):
    first = []
    stats = ingest_play_by_play(logged_games, ["2024-25"], fetch=fake_fetch(first, fail={"g2"}), log=lambda _: None)
    assert stats["failed"] == 1 and rows(logged_games) == [("g1", 3), ("g3", 3)]

    second = []
    ingest_play_by_play(logged_games, ["2024-25"], fetch=fake_fetch(second), log=lambda _: None)
    assert second == ["g2"]
    assert rows(logged_games) == [("g1", 3), ("g2", 3), ("g3", 3)]


def test_limit_and_empty_games(logged_games):
    calls = []
    stats = ingest_play_by_play(logged_games, ["2024-25"], limit=2, fetch=fake_fetch(calls, empty={"g1"}),
                                log=lambda _: None)
    assert calls == ["g1", "g2"]
    assert stats["empty"] == 1 and rows(logged_games) == [("g2", 3)]
