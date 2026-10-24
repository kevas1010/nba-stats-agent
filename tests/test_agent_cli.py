import pytest

from nba_agent.agent import cli
from nba_agent.agent.loop import AgentResult, StepRecord, ToolCallRecord
from nba_agent.agent.runlog import append_run, summarize


def _result(latency_ms=4300.0, stop="end_turn"):
    return AgentResult(
        question="q", answer="a", stop_reason=stop, model="claude-opus-5-5", effort="medium",
        steps=[
            StepRecord(1, "claude-opus-5-5", "tool_use", 2400.0, 2000, 150, 0, 1800),
            StepRecord(2, "claude-opus-5-5", "end_turn", 1800.0, 300, 90, 2100, 0),
        ],
        tool_calls=[ToolCallRecord(1, "query_stats", {"sql": "select 1"}, False, 12.0, 80)],
        latency_ms=latency_ms,
    )


def test_trace_shows_steps_tokens_cost_and_tools():
    result = _result()
    trace = cli.format_trace(result, summarize(result), verbose=True)
    assert "2 steps, 1 tool calls, 4.3s" in trace
    assert "2,300 in / 240 out tokens (2,100 cache read, 1,800 cache write)" in trace
    assert "$0.0" in trace and "claude-opus-5-5 (medium)" in trace
    assert "step 1: 2400ms" in trace and "query_stats 12ms" in trace
    assert 'query_stats {"sql": "select 1"}' in trace
    assert "stopped" not in trace


def test_trace_flags_abnormal_stops():
    result = _result(stop="max_steps")
    assert "-- stopped: max_steps" in cli.format_trace(result, summarize(result))


def test_report_percentiles(tmp_path):
    log = tmp_path / "runs.jsonl"
    for seconds in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
        append_run(summarize(_result(latency_ms=seconds * 1000)), log)
    out = cli.report(log)
    assert out.startswith("10 runs")
    assert "p50 5.5s" in out and "p95 9.6s" in out and "max 10.0s" in out
    assert "query_stats p50 12ms (n=10)" in out
    assert "stops    end_turn 10" in out


def test_report_with_no_runs(tmp_path):
    assert "no runs logged" in cli.report(tmp_path / "none.jsonl")


def test_report_flag_does_not_call_the_api(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli.anthropic, "AsyncAnthropic", lambda: pytest.fail("API client created"))
    assert cli.main(["--report", "--log", str(tmp_path / "x.jsonl")]) == 0
    assert "no runs logged" in capsys.readouterr().out


def test_missing_credentials_message(monkeypatch, capsys):
    # What SDK 2.x actually raises with no key, wrapped by the MCP client's task group.
    async def boom(*_args, **_kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [TypeError(
            '"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."'
        )])

    monkeypatch.setattr(cli, "_ask_all", boom)
    assert cli.main(["who leads the league in pace?"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_unexpected_errors_still_raise(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise ExceptionGroup("tg", [ValueError("a real bug")])

    monkeypatch.setattr(cli, "_ask_all", boom)
    with pytest.raises(ExceptionGroup):
        cli.main(["q"])
