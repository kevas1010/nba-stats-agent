import duckdb
import pytest

from nba_agent.mcp_server.sql_guard import UnsafeQueryError
from nba_agent.mcp_server.warehouse import connect, run_query


def test_unqualified_mart_names_resolve(warehouse):
    result = run_query(connect(warehouse), "select avg(pace) as pace from team_pace where team = 'NYK'")
    assert result.columns == ["pace"]
    assert result.rows == [[96.5]]
    assert not result.truncated


def test_rows_are_capped_and_flagged(warehouse):
    result = run_query(connect(warehouse), "select * from player_game_stats", max_rows=100)
    assert result.row_count == 100
    assert result.truncated


def test_dates_are_json_safe(warehouse):
    result = run_query(connect(warehouse), "select all_star_date from reference.all_star_games")
    assert result.rows == [["2025-02-16"]]


def test_guard_runs_before_execution(warehouse):
    with pytest.raises(UnsafeQueryError):
        run_query(connect(warehouse), "select * from raw.secrets")


def test_connection_is_read_only_even_without_guard(warehouse):
    con = connect(warehouse)
    with pytest.raises(duckdb.Error):
        run_query(con, "create table marts.x as select 1", guarded=False)


def test_connection_cannot_read_files_even_without_guard(warehouse, tmp_path):
    (tmp_path / "secret.csv").write_text("a\n1\n")
    con = connect(warehouse)
    with pytest.raises(duckdb.Error):
        run_query(con, f"select * from read_csv('{(tmp_path / 'secret.csv').as_posix()}')", guarded=False)


def test_connection_cannot_reenable_external_access(warehouse):
    con = connect(warehouse)
    with pytest.raises(duckdb.Error):
        con.execute("set enable_external_access = true")


def test_overlapping_connections_share_the_locked_config(warehouse):
    # Regression: SET-ing instance-wide options after connecting failed as soon as
    # a second connection was open while the first held the configuration lock.
    first, second = connect(warehouse), connect(warehouse)
    assert run_query(first, "select count(*) from team_pace").rows == [[3]]
    assert run_query(second, "select count(*) from team_pace").rows == [[3]]
    with pytest.raises(duckdb.Error):
        second.execute("set enable_external_access = true")


def test_concurrent_queries_from_threads(warehouse):
    import concurrent.futures

    def count(_):
        with connect(warehouse) as con:
            return run_query(con, "select count(*) from player_game_stats").rows[0][0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(count, range(40))) == {1000}


def test_timestamps_are_reported_in_utc(warehouse):
    result = run_query(connect(warehouse), "select game_time_utc from staging.stg_schedule where game_id = 's1'",
                       guarded=False)
    assert result.rows == [["2026-11-04T23:30:00+00:00"]]


def test_slow_query_times_out(warehouse):
    con = connect(warehouse)
    with pytest.raises(TimeoutError):
        # range() is a table function, which the guard blocks; test the timer alone.
        run_query(con, "select count(*) from range(100000000000) a", guarded=False, timeout=0.2)


def test_connect_waits_out_another_process_writing(warehouse):
    import subprocess
    import sys

    # A separate process holds the write lock for ~1.5s, like an ingest batch.
    writer = subprocess.Popen([sys.executable, "-c",
                               f"import duckdb, time; c = duckdb.connect(r'{warehouse}'); print('locked', flush=True); time.sleep(1.5)"],
                              stdout=subprocess.PIPE, text=True)
    assert writer.stdout.readline().strip() == "locked"
    try:
        con = connect(warehouse)  # retries until the writer exits
        assert run_query(con, "select count(*) from team_pace").rows == [[3]]
    finally:
        writer.wait()


def test_missing_warehouse_has_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="dbt build"):
        connect(tmp_path / "nope.duckdb")
