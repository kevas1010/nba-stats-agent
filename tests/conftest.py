import hashlib
import re
from datetime import datetime, timezone

import duckdb
import pytest

from nba_agent.rulebook.chunk import Chunk
from nba_agent.rulebook.index import build_index


class HashEmbedder:
    """Deterministic bag-of-words embedder so tests don't need the real model."""

    name = "test-hash"
    dim = 64

    def _vector(self, text):
        vec = [0.0] * self.dim
        for word in re.findall(r"[a-z]+", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        return vec

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


RULE_CHUNKS = [
    Chunk("c0000", "Rule 7 — Shot Clock", "Section I — Definition", 28, 28,
          "a. The shot clock is 24 seconds. The offensive team must attempt a field goal within 24 seconds."),
    Chunk("c0001", "Rule 3 — Players, Substitutes, and Coaches", "Section I — Team", 15, 15,
          "a. Each team shall consist of five players. A player is disqualified when he receives his sixth personal foul."),
    Chunk("c0002", "Rule 11 — Basket Interference—Goaltending", "Section I — A Player Shall Not:", 39, 39,
          "a. Touch the ball or the basket ring when the ball is on the ring, or hang on the rim."),
    Chunk("c0003", "Rule 14 — Coach’s Challenge", "Section I — Overview", 60, 61,
          "Each team is entitled to one challenge per game, regardless of whether the challenge is successful."),
]


@pytest.fixture
def warehouse(tmp_path):
    """A tiny DuckDB file shaped like the dbt output: marts, reference and staging schemas."""
    path = tmp_path / "nba.duckdb"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts; CREATE SCHEMA reference; CREATE SCHEMA staging; CREATE SCHEMA raw")
    con.execute("""
        CREATE TABLE marts.team_pace AS SELECT * FROM (VALUES
            ('g1', 'NYK', '2024-25', 'pre_all_star', 98.0),
            ('g2', 'NYK', '2024-25', 'post_all_star', 95.0),
            ('g1', 'BOS', '2024-25', 'pre_all_star', 98.0)
        ) t(game_id, team, season, season_phase, pace)
    """)
    con.execute("CREATE TABLE marts.player_game_stats AS SELECT range AS player_id, 10 AS pts FROM range(1000)")
    con.execute("CREATE TABLE reference.all_star_games AS SELECT '2024-25' AS season, DATE '2025-02-16' AS all_star_date")
    con.execute("CREATE TABLE raw.secrets AS SELECT 'nope' AS value")
    # Times are UTC; game_date_et is derived the same way stg_schedule does it.
    # s2 tips at 02:30 UTC on Nov 6, which is still Nov 5 in New York.
    con.execute(
        """
        CREATE TABLE staging.stg_schedule AS
        SELECT *, cast(timezone('America/New_York', game_time_utc) as date) AS game_date_et
        FROM (VALUES
            ('s1', TIMESTAMPTZ '2026-11-04 23:30:00+00', 'final',     'NYK', 'BOS', 110, 104, 'Madison Square Garden', 'New York', NULL, false),
            ('s2', TIMESTAMPTZ '2026-11-06 02:30:00+00', 'scheduled', 'LAL', 'NYK', NULL, NULL, 'Crypto.com Arena', 'Los Angeles', 'Emirates NBA Cup', false),
            ('s3', TIMESTAMPTZ '2026-11-06 00:00:00+00', 'scheduled', 'BOS', 'MIA', NULL, NULL, 'TD Garden', 'Boston', NULL, false),
            ('s4', TIMESTAMPTZ '2026-12-01 03:00:00+00', 'scheduled', 'GSW', 'NYK', NULL, NULL, 'Chase Center', 'San Francisco', NULL, false),
            ('s5', TIMESTAMPTZ '2026-11-04 23:00:00+00', 'final',     'MIA', 'ORL', 99, 101, 'Kaseya Center', 'Miami', 'Preseason', true),
            ('s6', ?,                                    'scheduled', 'DEN', 'UTA', NULL, NULL, 'Ball Arena', 'Denver', NULL, false)
        ) t(game_id, game_time_utc, status, home_team, away_team, home_score, away_score,
            arena_name, arena_city, game_label, is_preseason)
        """,
        [now],
    )
    con.close()
    return path


@pytest.fixture
def rulebook_warehouse(warehouse):
    """The warehouse fixture plus a rulebook index built with HashEmbedder."""
    with duckdb.connect(str(warehouse)) as con:
        build_index(con, RULE_CHUNKS, HashEmbedder(), season="2026-27", source_url="https://example.test/rules.pdf")
    return warehouse
