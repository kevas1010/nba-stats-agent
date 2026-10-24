import asyncio

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from nba_agent.mcp_server.server import create_server, describe_schema, get_schedule, query_stats


# --- query_stats -----------------------------------------------------------------

def test_query_stats_returns_rows_and_metadata(warehouse):
    result = query_stats(
        "select season_phase, avg(pace) as pace from team_pace where team = 'NYK' group by 1 order by 1",
        warehouse,
    )
    assert result["columns"] == ["season_phase", "pace"]
    assert result["rows"] == [["post_all_star", 95.0], ["pre_all_star", 98.0]]
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert result["elapsed_ms"] >= 0


def test_query_stats_rejects_unsafe_sql_as_tool_error(warehouse):
    with pytest.raises(ToolError, match="query rejected"):
        query_stats("drop table marts.team_pace", warehouse)


def test_query_stats_surfaces_sql_errors_so_the_model_can_retry(warehouse):
    with pytest.raises(ToolError, match="query failed.*no_such_column"):
        query_stats("select no_such_column from team_pace", warehouse)


def test_query_stats_flags_truncation(warehouse):
    result = query_stats("select * from player_game_stats", warehouse)
    assert result["truncated"] is True
    assert result["row_count"] == 500


# --- get_schedule ----------------------------------------------------------------

def test_get_schedule_filters_by_team_and_window(warehouse):
    result = get_schedule("nyk", start_date="2026-11-04", days=7, db_path=warehouse)
    assert result["team"] == "NYK"
    assert (result["from"], result["to"]) == ("2026-11-04", "2026-11-10")
    assert [g["game_id"] for g in result["games"]] == ["s1", "s2"]


def test_get_schedule_uses_eastern_game_dates(warehouse):
    # s2 tips 02:30 UTC Nov 6 = 9:30pm ET Nov 5, so it belongs to Nov 5.
    games = get_schedule(start_date="2026-11-05", days=1, db_path=warehouse)["games"]
    assert {g["game_id"] for g in games} == {"s2", "s3"}
    assert all(g["game_date_et"] == "2026-11-05" for g in games)


def test_get_schedule_excludes_preseason(warehouse):
    games = get_schedule(start_date="2026-11-04", days=1, db_path=warehouse)["games"]
    assert [g["game_id"] for g in games] == ["s1"]


def test_get_schedule_returns_scores_for_final_games(warehouse):
    game = get_schedule("BOS", start_date="2026-11-04", days=1, db_path=warehouse)["games"][0]
    assert (game["status"], game["home_score"], game["away_score"]) == ("final", 110, 104)
    assert game["game_time_utc"].startswith("2026-11-04T23:30:00")


def test_get_schedule_defaults_to_today(warehouse):
    games = get_schedule(days=1, db_path=warehouse)["games"]
    assert [g["game_id"] for g in games] == ["s6"]


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"team": "Knicks"}, "three-letter"),
        ({"team": "NY'"}, "three-letter"),
        ({"days": 0}, "days must be"),
        ({"days": 61}, "days must be"),
        ({"start_date": "11/05/2026"}, "YYYY-MM-DD"),
    ],
)
def test_get_schedule_validates_arguments(warehouse, kwargs, message):
    with pytest.raises(ToolError, match=message):
        get_schedule(db_path=warehouse, **kwargs)


# --- schema description and MCP wiring ---------------------------------------------

def test_describe_schema_lists_only_allowlisted_tables(warehouse):
    text = describe_schema(warehouse)
    assert "marts.team_pace: game_id varchar" in text
    assert "reference.all_star_games" in text
    assert "raw." not in text and "staging." not in text


def test_describe_schema_without_warehouse(tmp_path):
    assert "not built yet" in describe_schema(tmp_path / "missing.duckdb")


def _call(server, name, args):
    async def go():
        async with Client(server) as client:
            tools = await client.list_tools()
            result = await client.call_tool(name, args)
            return tools, result

    return asyncio.run(go())


def test_mcp_client_sees_all_tools_as_read_only(warehouse):
    tools, _ = _call(create_server(warehouse), "get_schedule", {"days": 1})
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == {"query_stats", "get_schedule", "search_rulebook"}
    assert all(t.annotations.read_only_hint for t in by_name.values())
    assert "marts.player_game_stats" in by_name["query_stats"].description


def test_mcp_call_returns_structured_result(warehouse):
    _, result = _call(create_server(warehouse), "query_stats", {"sql": "select count(*) as n from team_pace"})
    assert not result.is_error
    assert result.structured_content["rows"] == [[3]]


def test_parallel_tool_calls_all_succeed(warehouse):
    # Claude often issues several tool calls in one turn; the agent runs them concurrently.
    async def go():
        async with Client(create_server(warehouse)) as client:
            return await asyncio.gather(*(
                client.call_tool(name, args) for name, args in [
                    ("query_stats", {"sql": "select count(*) as n from team_pace"}),
                    ("get_schedule", {"start_date": "2026-11-04", "days": 3}),
                    ("query_stats", {"sql": "select count(*) as n from player_game_stats"}),
                    ("get_schedule", {"team": "NYK", "start_date": "2026-11-04", "days": 30}),
                ] * 3
            ))

    results = asyncio.run(go())
    assert not any(r.is_error for r in results), [r.content[0].text for r in results if r.is_error]


def test_mcp_call_reports_rejection_as_tool_error(warehouse):
    _, result = _call(create_server(warehouse), "query_stats", {"sql": "select * from raw.secrets"})
    assert result.is_error
    assert "table not allowed: raw.secrets" in result.content[0].text
