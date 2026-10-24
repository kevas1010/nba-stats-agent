import csv

import pytest

from nba_agent.evals.claude_code import RunResult
from nba_agent.evals.golden import Question
from nba_agent.evals.harness import (
    build_prompt, export_csv, format_summary, plan_runs, read_results, run_eval, summarize,
)

STATS = Question(id="s1", category="stats", question="NYK avg pace?", kind="number",
                 sql="select avg(pace) from marts.team_pace where team = 'NYK'", tolerance=0.1)
NAME = Question(id="s2", category="stats", question="Who?", kind="text", value="Nikola Jokić")
RULE = Question(id="r1", category="rules", question="Shot clock?", kind="rule", must_include=("24",), cite_rule=7)


class ScriptedRunner:
    def __init__(self, replies, cost=0.02, latency=5000):
        self.replies, self.cost, self.latency, self.prompts = list(replies), cost, latency, []

    def run(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, RunResult):
            return reply
        return RunResult(reply=reply, duration_ms=self.latency, num_turns=3, cost_usd=self.cost,
                         tool_calls=[{"name": "query_stats", "input": {}, "is_error": False}])


def test_plan_runs_baseline_only_on_rules_by_default():
    plan = plan_runs([STATS, RULE], ["rag", "no_rag"])
    assert [(q.id, v) for q, v in plan] == [("s1", "rag"), ("r1", "rag"), ("r1", "no_rag")]
    assert len(plan_runs([STATS, RULE], ["rag", "no_rag"], baseline_all=True)) == 4


def test_prompt_has_date_question_and_answer_instruction():
    p = build_prompt(STATS, "2026-10-24")
    assert p.startswith("Today's date (US Eastern): 2026-10-24\n\nNYK avg pace?")
    assert "ANSWER:" in p


def test_run_eval_grades_and_writes_results(tmp_path, warehouse):
    runners = {"rag": ScriptedRunner(["Pace was 96.5.\nANSWER: 96.5", "ANSWER: Jokic",
                                      "Rule 7: 24 seconds.\nANSWER: 24 seconds"]),
               "no_rag": ScriptedRunner(["It's 24 seconds.\nANSWER: 24"], cost=0.01, latency=3000)}
    plan = plan_runs([STATS, NAME, RULE], ["rag", "no_rag"])
    records = run_eval(plan, tmp_path / "run", runners.__getitem__, model="claude-opus-5-5",
                       today="2026-10-24", db_path=warehouse, log=lambda _: None)

    assert [(r["id"], r["variant"], r["correct"]) for r in records] == [
        ("s1", "rag", True), ("s2", "rag", True), ("r1", "rag", True), ("r1", "no_rag", False)]
    assert records[0]["expected"] == 96.5
    assert "Rule 7 citation" in records[3]["reason"]
    assert read_results(tmp_path / "run") == records


def test_resume_skips_finished_runs(tmp_path, warehouse):
    plan = plan_runs([STATS, NAME], ["rag"])
    first = ScriptedRunner(["ANSWER: 96.5"])
    run_eval(plan[:1], tmp_path / "run", lambda v: first, model="m", today="d", db_path=warehouse, log=lambda _: None)
    second = ScriptedRunner(["ANSWER: Jokic"])
    run_eval(plan, tmp_path / "run", lambda v: second, model="m", today="d", db_path=warehouse, log=lambda _: None)
    assert len(second.prompts) == 1 and "Who?" in second.prompts[0]
    assert [r["id"] for r in read_results(tmp_path / "run")] == ["s1", "s2"]


def test_stops_on_sign_in_errors(tmp_path, warehouse):
    runner = ScriptedRunner([RunResult(is_error=True, error="Invalid API key · Please run /login"), "unused"])
    records = run_eval(plan_runs([STATS, NAME], ["rag"]), tmp_path / "run", lambda v: runner, model="m",
                       today="d", db_path=warehouse, log=lambda _: None)
    assert len(records) == 1 and not records[0]["correct"] and "run error" in records[0]["reason"]


def _records():
    base = dict(kind="number", model="m", has_answer_line=True, error="", turns=3, tool_calls=2, tool_errors=0,
                tools_used=["query_stats"], expected=1, extracted="1", reason="", question="?", ts="t",
                api_ms=None, input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)
    rows = [dict(base, id=f"s{i}", category="stats", variant="rag", correct=i < 8, latency_ms=1000 * (i + 1),
                 cost_usd=0.02) for i in range(10)]
    rows += [dict(base, id="r1", category="rules", variant="rag", correct=True, latency_ms=4000, cost_usd=0.03),
             dict(base, id="r1", category="rules", variant="no_rag", correct=False, latency_ms=2000, cost_usd=0.01,
                  tool_calls=0)]
    return rows


def test_summary_accuracy_latency_cost():
    s = summarize(_records())
    rag = s["variants"]["rag"]
    assert rag["n"] == 11 and rag["accuracy"] == pytest.approx(9 / 11)
    assert rag["by_category"]["stats"]["accuracy"] == 0.8
    assert rag["latency_p50_s"] == 5.0 and rag["latency_p95_s"] == pytest.approx(9.5)
    assert rag["cost_total_usd"] == pytest.approx(0.23)
    assert s["rag_vs_no_rag"] == {"questions": 1, "rag_accuracy": 1.0, "no_rag_accuracy": 0.0}


def test_format_summary_markdown():
    md = format_summary(summarize(_records()), "run-1")
    assert md.startswith("# Eval results — run-1")
    assert "| rag | 11 | 82% |" in md
    assert "**RAG vs no-RAG** on the same 1 questions: 100% with the rulebook tool vs 0% without." in md


def test_export_csv_flattens_all_runs(tmp_path, warehouse):
    for name in ("run-a", "run-b"):
        run_eval(plan_runs([STATS], ["rag"]), tmp_path / name, lambda v: ScriptedRunner(["ANSWER: 96.5"]),
                 model="m", today="d", db_path=warehouse, log=lambda _: None)
    out = tmp_path / "out.csv"
    assert export_csv(tmp_path, out) == 2
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["run_id"] for r in rows] == ["run-a", "run-b"]
    assert rows[0]["correct"] == "1" and rows[0]["tools_used"] == "query_stats"
