import asyncio
import json
from datetime import date
from types import SimpleNamespace

from conftest import HashEmbedder
from fake_claude import FakeClaude, message, text, tool_use, usage
from nba_agent.agent.loop import FALLBACK_BETA, SYSTEM_PROMPT, Agent
from nba_agent.agent.mcp_tools import MCPToolbox
from nba_agent.mcp_server.server import create_server

TODAY = date(2026, 11, 5)


def run(db, responses, question="q", **agent_kwargs):
    fake = FakeClaude(responses)

    async def go():
        async with MCPToolbox.connect(create_server(db, HashEmbedder)) as toolbox:
            agent = Agent(fake, toolbox, today=TODAY, **agent_kwargs)
            return await agent.ask(question)

    return asyncio.run(go()), fake


def test_multi_step_answer_with_tool_calls(rulebook_warehouse):
    result, fake = run(
        rulebook_warehouse,
        [
            message([text("Let me check pace."), tool_use("t1", "query_stats",
                     sql="select season_phase, avg(pace) as pace from team_pace where team = 'NYK' group by 1 order by 1")],
                    "tool_use", use=usage(500, 40, cache_write=480)),
            message([text("The Knicks slowed from 98.0 to 95.0 after the break.")], "end_turn",
                    use=usage(80, 30, cache_read=560)),
        ],
        question="Did the Knicks play faster after the All-Star break?",
    )
    assert result.stop_reason == "end_turn"
    assert result.answer == "The Knicks slowed from 98.0 to 95.0 after the break."
    assert [s.step for s in result.steps] == [1, 2]
    assert (result.input_tokens, result.output_tokens) == (580, 70)
    assert (result.cache_read_tokens, result.cache_write_tokens) == (560, 480)

    [call] = result.tool_calls
    assert call.name == "query_stats" and not call.is_error and call.result_chars > 0

    # The second request carries the tool result, tied to the tool_use id.
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "user"
    [tool_result] = tool_msg["content"]
    assert tool_result["tool_use_id"] == "t1"
    assert json.loads(tool_result["content"])["rows"] == [["post_all_star", 95.0], ["pre_all_star", 98.0]]


def test_request_shape(rulebook_warehouse):
    _, fake = run(rulebook_warehouse, [message([text("ok")], "end_turn")], question="Who leads in pace?",
                  effort="high")
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "high"}
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert req["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in req and "tool_choice" not in req
    # Tools are listed in a fixed order and the system prompt carries no date,
    # so the cached prefix is identical across questions and days.
    assert [t["name"] for t in req["tools"]] == ["get_schedule", "query_stats", "search_rulebook"]
    assert req["system"] == SYSTEM_PROMPT and "2026" not in SYSTEM_PROMPT
    assert req["messages"][0]["content"].startswith("Today's date (US Eastern): 2026-11-05")


def test_parallel_tool_calls_go_back_in_one_message(rulebook_warehouse):
    result, fake = run(rulebook_warehouse, [
        message([tool_use("a", "get_schedule", team="NYK", start_date="2026-11-04", days=3),
                 tool_use("b", "search_rulebook", query="shot clock", k=1)], "tool_use"),
        message([text("done")], "end_turn"),
    ])
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert [c.name for c in result.tool_calls] == ["get_schedule", "search_rulebook"]
    assert all(c.step == 1 for c in result.tool_calls)


def test_tool_errors_are_returned_to_the_model(rulebook_warehouse):
    result, fake = run(rulebook_warehouse, [
        message([tool_use("t1", "query_stats", sql="select * from raw.secrets")], "tool_use"),
        message([tool_use("t2", "query_stats", sql="select count(*) as n from team_pace")], "tool_use"),
        message([text("There are 3 team-games.")], "end_turn"),
    ])
    first = fake.requests[1]["messages"][-1]["content"][0]
    assert first["is_error"] is True
    assert "table not allowed: raw.secrets" in first["content"]
    assert [c.is_error for c in result.tool_calls] == [True, False]
    assert result.answer == "There are 3 team-games."


def test_assistant_content_is_appended_unchanged(rulebook_warehouse):
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    first = [thinking, tool_use("t1", "search_rulebook", query="coach challenge")]
    _, fake = run(rulebook_warehouse, [message(first, "tool_use"), message([text("ok")], "end_turn")])
    assistant = fake.requests[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == first  # same block objects, thinking included


def test_stops_at_max_steps(rulebook_warehouse):
    looping = [message([text(f"checking {i}"), tool_use(f"t{i}", "search_rulebook", query="foul")], "tool_use")
               for i in range(3)]
    result, fake = run(rulebook_warehouse, looping, max_steps=3)
    assert result.stop_reason == "max_steps"
    assert len(fake.requests) == 3
    assert result.answer == "checking 2"


def test_refusal_is_reported(rulebook_warehouse):
    details = SimpleNamespace(category="cyber", explanation="...")
    result, _ = run(rulebook_warehouse, [message([], "refusal", stop_details=details)])
    assert result.stop_reason == "refusal"
    assert "declined" in result.answer and "cyber" in result.answer


def test_fallback_iterations_are_recorded(rulebook_warehouse):
    iterations = [
        SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=100, output_tokens=0,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=100, output_tokens=25,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
    ]
    result, _ = run(rulebook_warehouse, [
        message([text("answer")], "end_turn", model="claude-opus-4-8", use=usage(200, 25, iterations=iterations)),
    ])
    assert result.model == "claude-opus-4-8"
    assert [i["model"] for i in result.steps[0].iterations] == ["claude-opus-5-5", "claude-opus-4-8"]
