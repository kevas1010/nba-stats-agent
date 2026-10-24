import pytest

from nba_agent.agent.loop import AgentResult, StepRecord, ToolCallRecord
from nba_agent.agent.pricing import PRICES, step_cost, token_cost, total_cost
from nba_agent.agent.runlog import append_run, read_runs, summarize


def step(n=1, model="claude-opus-5-5", inp=1_000_000, out=0, cr=0, cw=0, iterations=None, ms=1000.0):
    return StepRecord(n, model, "end_turn", ms, inp, out, cr, cw, iterations or [])


def test_token_cost_uses_per_million_rates():
    assert token_cost("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.00)
    assert token_cost("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.00)
    assert token_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.20)
    assert token_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(5.00)


def test_cache_writes_cost_more_and_reads_cost_less_than_input():
    for rates in PRICES.values():
        assert rates.cache_read < rates.input < rates.cache_write


def test_unknown_model_is_unpriced_not_zero():
    assert token_cost("some-future-model", 100, 100) is None
    assert total_cost([step(), step(model="some-future-model")]) is None


def test_fallback_turns_are_priced_per_model():
    iterations = [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0},
    ]
    assert step_cost(step(model="claude-opus-4-8", iterations=iterations)) == pytest.approx(4.00 + 5.00)


def _result():
    return AgentResult(
        question="Did the Knicks play faster after the break?",
        answer="No, pace fell from 99.1 to 95.3.",
        stop_reason="end_turn",
        model="claude-opus-5-5",
        effort="medium",
        steps=[step(1, inp=2000, out=150, cw=1800, ms=2400.0), step(2, inp=300, out=90, cr=2100, ms=1800.0)],
        tool_calls=[
            ToolCallRecord(1, "query_stats", {"sql": "select 1"}, False, 12.0, 80),
            ToolCallRecord(1, "search_rulebook", {"query": "x"}, True, 30.0, 40),
        ],
        latency_ms=4300.0,
    )


def test_summarize_totals():
    s = summarize(_result())
    assert (s["steps"], s["tool_calls"], s["tool_errors"]) == (2, 2, 1)
    assert s["tools_used"] == ["query_stats", "search_rulebook"]
    assert (s["input_tokens"], s["output_tokens"]) == (2300, 240)
    assert (s["cache_read_tokens"], s["cache_write_tokens"]) == (2100, 1800)
    assert s["model_latency_ms"] == 4200.0
    assert s["tool_latency_ms"] == 42.0 and s["tool_latency_p50_ms"] == 21.0
    expected = (2300 * 4 + 240 * 20 + 2100 * 0.20 + 1800 * 5) / 1_000_000
    assert s["cost_usd"] == pytest.approx(expected, abs=1e-6)
    assert sum(r["cost_usd"] for r in s["step_records"]) == pytest.approx(expected, abs=1e-6)


def test_run_log_round_trip(tmp_path):
    path = tmp_path / "logs" / "runs.jsonl"
    append_run(summarize(_result()), path)
    append_run(summarize(_result()), path)
    runs = read_runs(path)
    assert len(runs) == 2
    assert runs[0]["tool_records"][1]["is_error"] is True
    assert read_runs(tmp_path / "missing.jsonl") == []
