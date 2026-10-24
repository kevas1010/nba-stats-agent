import pytest

from nba_agent.mcp_server.sql_guard import UnsafeQueryError, check_query


@pytest.mark.parametrize(
    "sql",
    [
        "select team, avg(pace) from marts.team_pace group by team",
        "SELECT * FROM team_pace WHERE season = '2024-25' LIMIT 10",
        "from marts.player_game_stats select player_name, pts",
        """
        with phases as (
            select team, season_phase, avg(pace) as pace
            from marts.team_pace
            where team = 'NYK'
            group by all
        )
        select * from phases order by season_phase
        """,
        "select p.player_name, a.all_star_date from player_game_stats p "
        "join reference.all_star_games a using (season)",
        "select team from marts.team_pace union all select team from marts.player_game_stats",
        "select * from marts.team_pace where game_id in (select game_id from marts.player_game_stats)",
    ],
)
def test_allows_read_only_queries_on_marts(sql):
    assert check_query(sql) == sql


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("drop table marts.team_pace", "only SELECT"),
        ("delete from marts.team_pace", "only SELECT"),
        ("insert into marts.team_pace select * from marts.team_pace", "only SELECT"),
        ("update marts.team_pace set pace = 0", "only SELECT"),
        ("create table x as select * from marts.team_pace", "only SELECT"),
        ("select 1; drop table marts.team_pace", "exactly one"),
        ("attach 'other.db'", "only SELECT"),
        ("copy marts.team_pace to 'out.csv'", "only SELECT"),
        ("pragma table_info('team_pace')", "only SELECT"),
        ("set threads = 1", "only SELECT"),
        ("install httpfs", "only SELECT"),
        ("select * from raw.team_game_logs", "table not allowed"),
        ("select * from staging.stg_schedule", "table not allowed"),
        ("select * from information_schema.tables", "table not allowed"),
        ("select * from some_other_table", "table not allowed"),
        ("select * from nba.marts.team_pace", "catalog-qualified"),
        ("select * from '/etc/passwd'", "table not allowed"),
        ("select * from read_csv('/etc/passwd')", "table functions"),
        ("select * from glob('*')", "table functions"),
        ("select * from duckdb_settings()", "table functions"),
        ("select getenv('ANTHROPIC_API_KEY')", "function not allowed"),
        ("select current_setting('threads')", "function not allowed"),
        ("select * from marts.team_pace where team = read_text('x')", "function not allowed"),
        ("select * from (select * from raw.schedule) s", "table not allowed"),
        ("select * from marts.team_pace into x", None),
        ("select from where", None),
    ],
)
def test_rejects_unsafe_queries(sql, reason):
    with pytest.raises(UnsafeQueryError) as err:
        check_query(sql)
    if reason:
        assert reason in str(err.value)


def test_cte_cannot_launder_a_blocked_table():
    with pytest.raises(UnsafeQueryError, match="raw.team_game_logs"):
        check_query("with t as (select * from raw.team_game_logs) select * from t")
