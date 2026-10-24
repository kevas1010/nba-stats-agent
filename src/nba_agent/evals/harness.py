"""Run the golden set, grade every answer, and summarize accuracy, latency and cost."""

from __future__ import annotations

import csv
import json
import statistics
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nba_agent.evals.claude_code import RunResult
from nba_agent.evals.golden import Question, resolve_expected
from nba_agent.evals.grade import ANSWER_INSTRUCTION, grade

EVALS_DIR = Path("data/evals")
# Signs that every later run would fail the same way, so stop instead of looping.
_FATAL_ERRORS = ("login", "api key", "not found", "authentication")


def plan_runs(questions: list[Question], variants: list[str], baseline_all: bool = False) -> list[tuple[Question, str]]:
    """RAG on every question; the no-RAG baseline only where it can differ (rules), unless baseline_all."""
    plan = []
    for q in questions:
        for v in variants:
            if v == "no_rag" and not baseline_all and q.category != "rules":
                continue
            plan.append((q, v))
    return plan


def build_prompt(q: Question, today: str) -> str:
    return f"Today's date (US Eastern): {today}\n\n{q.question}{ANSWER_INSTRUCTION}"


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def run_eval(
    plan: list[tuple[Question, str]],
    run_dir: Path,
    runner_for: Callable[[str], Any],  # variant -> object with .run(prompt) -> RunResult
    *,
    model: str,
    today: str,
    db_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    done = {(r["id"], r["variant"]) for r in read_results(run_dir)}
    expected_cache: dict[str, Any] = {}
    runners: dict[str, Any] = {}
    records = []

    todo = [(q, v) for q, v in plan if (q.id, v) not in done]
    if len(todo) < len(plan):
        log(f"resuming: {len(plan) - len(todo)} of {len(plan)} runs already done")
    for i, (q, variant) in enumerate(todo, start=1):
        if q.id not in expected_cache:
            expected_cache[q.id] = resolve_expected(q, db_path)
        expected = expected_cache[q.id]
        runner = runners.setdefault(variant, runner_for(variant))
        run: RunResult = runner.run(build_prompt(q, today))
        g = grade(q, expected, run.reply) if not run.is_error else None

        record = {
            "id": q.id, "category": q.category, "kind": q.kind, "variant": variant, "model": model,
            "question": q.question, "expected": expected,
            "correct": bool(g and g.correct), "reason": g.reason if g else f"run error: {run.error}",
            "has_answer_line": bool(g and g.has_answer_line), "extracted": g.extracted if g else "",
            "reply": run.reply, "error": run.error, "subtype": run.subtype,
            "latency_ms": run.duration_ms if run.duration_ms is not None else run.wall_ms,
            "api_ms": run.duration_api_ms, "wall_ms": run.wall_ms, "turns": run.num_turns,
            "cost_usd": run.cost_usd, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "cache_read_tokens": run.cache_read_tokens, "cache_write_tokens": run.cache_write_tokens,
            "models": run.models, "mcp_status": run.mcp_status,
            "tool_calls": len(run.tool_calls), "tool_errors": sum(c["is_error"] for c in run.tool_calls),
            "tools_used": sorted({c["name"] for c in run.tool_calls}), "tool_trace": run.tool_calls,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        records.append(record)

        mark = "ok " if record["correct"] else ("ERR" if run.is_error else "x  ")
        cost = f"${run.cost_usd:.4f}" if run.cost_usd is not None else "$?"
        log(f"[{i}/{len(todo)}] {mark} {q.id:<10} {variant:<6} {record['latency_ms'] / 1000:5.1f}s {cost}  {record['reason'][:70]}")

        if run.is_error and any(s in run.error.lower() for s in _FATAL_ERRORS):
            log(f"stopping: {run.error.strip()[:200]}")
            break
    return records


def read_results(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = list(records)
    out: dict[str, Any] = {"runs": len(records), "variants": {}}
    for variant in sorted({r["variant"] for r in records}):
        rs = [r for r in records if r["variant"] == variant]
        latency = [r["latency_ms"] / 1000 for r in rs if r["latency_ms"] is not None]
        costs = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        by_cat = {}
        for cat in sorted({r["category"] for r in rs}):
            cs = [r for r in rs if r["category"] == cat]
            by_cat[cat] = {"n": len(cs), "accuracy": sum(r["correct"] for r in cs) / len(cs)}
        out["variants"][variant] = {
            "n": len(rs),
            "accuracy": sum(r["correct"] for r in rs) / len(rs),
            "by_category": by_cat,
            "run_errors": sum(bool(r["error"]) for r in rs),
            "answer_line_rate": sum(r["has_answer_line"] for r in rs) / len(rs),
            "latency_p50_s": _percentile(latency, 50),
            "latency_p95_s": _percentile(latency, 95),
            "cost_mean_usd": statistics.mean(costs) if costs else None,
            "cost_total_usd": sum(costs) if costs else None,
            "turns_mean": statistics.mean([r["turns"] for r in rs if r["turns"] is not None] or [0]),
            "tool_calls_mean": statistics.mean([r["tool_calls"] for r in rs]),
            "tool_error_rate": (sum(r["tool_errors"] for r in rs) / max(sum(r["tool_calls"] for r in rs), 1)),
        }
    # Head-to-head on the questions both variants answered (the rules subset by default).
    v = out["variants"]
    if "rag" in v and "no_rag" in v:
        shared = {r["id"] for r in records if r["variant"] == "no_rag"}
        pair = {var: [r for r in records if r["variant"] == var and r["id"] in shared] for var in ("rag", "no_rag")}
        out["rag_vs_no_rag"] = {
            "questions": len(shared),
            **{f"{var}_accuracy": sum(r["correct"] for r in rs) / len(rs) for var, rs in pair.items() if rs},
        }
    return out


def format_summary(summary: dict[str, Any], title: str = "") -> str:
    def pct(x):
        return f"{x * 100:.0f}%" if x is not None else "n/a"

    def sec(x):
        return f"{x:.1f}s" if x is not None else "n/a"

    def usd(x):
        return f"${x:.4f}" if x is not None else "n/a"

    lines = [f"# Eval results{f' — {title}' if title else ''}", "", f"{summary['runs']} runs", ""]
    lines += ["| variant | n | accuracy | p50 latency | p95 latency | mean cost | total cost | turns | tool calls | tool error rate |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for name, v in summary["variants"].items():
        lines.append(f"| {name} | {v['n']} | {pct(v['accuracy'])} | {sec(v['latency_p50_s'])} | {sec(v['latency_p95_s'])} | "
                     f"{usd(v['cost_mean_usd'])} | {usd(v['cost_total_usd'])} | {v['turns_mean']:.1f} | "
                     f"{v['tool_calls_mean']:.1f} | {pct(v['tool_error_rate'])} |")
    lines += ["", "| variant | category | n | accuracy |", "|---|---|---|---|"]
    for name, v in summary["variants"].items():
        for cat, c in v["by_category"].items():
            lines.append(f"| {name} | {cat} | {c['n']} | {pct(c['accuracy'])} |")
    if h2h := summary.get("rag_vs_no_rag"):
        lines += ["", f"**RAG vs no-RAG** on the same {h2h['questions']} questions: "
                  f"{pct(h2h.get('rag_accuracy'))} with the rulebook tool vs {pct(h2h.get('no_rag_accuracy'))} without."]
    if any(v["run_errors"] for v in summary["variants"].values()):
        lines += ["", "Run errors: " + ", ".join(f"{k} {v['run_errors']}" for k, v in summary["variants"].items())]
    return "\n".join(lines) + "\n"


def write_summary(run_dir: Path) -> dict[str, Any]:
    summary = summarize(read_results(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run_dir / "summary.md").write_text(format_summary(summary, run_dir.name), encoding="utf-8")
    return summary


CSV_FIELDS = ["run_id", "id", "category", "kind", "variant", "model", "correct", "has_answer_line",
              "latency_ms", "api_ms", "turns", "cost_usd", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_write_tokens", "tool_calls", "tool_errors", "tools_used",
              "expected", "extracted", "reason", "question", "ts"]


def export_csv(evals_dir: Path, out_path: Path) -> int:
    """Every run's results in one flat CSV (one row per question x variant), for Tableau."""
    rows = []
    for run_dir in sorted(p for p in evals_dir.iterdir() if p.is_dir()):
        for r in read_results(run_dir):
            rows.append({**{k: r.get(k) for k in CSV_FIELDS}, "run_id": run_dir.name,
                         "tools_used": ";".join(r.get("tools_used", [])), "correct": int(r["correct"]),
                         "has_answer_line": int(r["has_answer_line"])})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def new_run_id(model: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{model.replace('claude-', '')}"
