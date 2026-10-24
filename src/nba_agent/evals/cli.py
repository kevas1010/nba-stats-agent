"""nba-eval: run the golden set through Claude Code headless and report accuracy, latency and cost.

    nba-eval run --dry-run                 # resolve every expected answer; no Claude calls
    nba-eval run --limit 3                 # smoke test: first 3 questions, RAG only for non-rules
    nba-eval run                           # full set: RAG on all 50, no-RAG baseline on rules questions
    nba-eval run --resume <run_id>         # continue an interrupted run
    nba-eval report [run_id]               # summary table (default: latest run)
    nba-eval export                        # data/evals/eval_results.csv for Tableau

Runs use your Claude Code sign-in (`claude` then /login), not an API key.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from nba_agent.evals.claude_code import VARIANTS, ClaudeCodeRunner
from nba_agent.evals.golden import DEFAULT_GOLDEN_PATH, load_golden, resolve_expected
from nba_agent.evals.harness import (
    EVALS_DIR, export_csv, format_summary, new_run_id, plan_runs, read_results, run_eval, summarize, write_summary,
)

DEFAULT_MODEL = "claude-opus-5-5"


def _select(questions, args):
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q.id in wanted]
    if args.category:
        questions = [q for q in questions if q.category in args.category.split(",")]
    if args.limit:
        questions = questions[: args.limit]
    return questions


def cmd_run(args) -> int:
    db = Path(os.getenv("DUCKDB_PATH", "data/nba.duckdb"))
    questions = _select(load_golden(args.golden), args)
    variants = [v for v in args.variants.split(",") if v]
    if unknown := set(variants) - set(VARIANTS):
        print(f"unknown variants: {sorted(unknown)}", file=sys.stderr)
        return 2
    plan = plan_runs(questions, variants, baseline_all=args.baseline_all)

    if args.dry_run:
        for q in questions:
            expected = resolve_expected(q, db)
            shown = expected if expected is not None else f"facts {list(q.must_include) + [list(g) for g in q.any_of]}, Rule {q.cite_rule}"
            print(f"{q.id:<10} {q.category:<12} {q.kind:<6} -> {shown}")
        print(f"\n{len(questions)} questions, {len(plan)} runs planned ({', '.join(variants)})")
        return 0

    run_id = args.resume or new_run_id(args.model)
    run_dir = EVALS_DIR / run_id
    print(f"run {run_id}: {len(plan)} runs on {args.model} -> {run_dir}")
    run_eval(
        plan, run_dir,
        lambda variant: ClaudeCodeRunner(model=args.model, variant=variant, db_path=db,
                                         max_budget_usd=args.budget_per_question, timeout_s=args.timeout),
        model=args.model, today=date.today().isoformat(), db_path=db,
    )
    summary = write_summary(run_dir)
    print()
    print(format_summary(summary, run_id))
    return 0


def _latest_run() -> Path | None:
    runs = sorted(p for p in EVALS_DIR.glob("*") if (p / "results.jsonl").exists())
    return runs[-1] if runs else None


def cmd_report(args) -> int:
    run_dir = EVALS_DIR / args.run_id if args.run_id else _latest_run()
    if run_dir is None or not (run_dir / "results.jsonl").exists():
        print("no eval runs found", file=sys.stderr)
        return 1
    print(format_summary(summarize(read_results(run_dir)), run_dir.name))
    if args.failures:
        for r in read_results(run_dir):
            if not r["correct"]:
                print(f"- {r['id']} [{r['variant']}]: {r['reason']}\n  answer: {r['extracted'][:200]!r}")
    return 0


def cmd_export(args) -> int:
    n = export_csv(EVALS_DIR, args.out)
    print(f"wrote {n} rows to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the golden set")
    run.add_argument("--model", default=os.getenv("NBA_EVAL_MODEL") or DEFAULT_MODEL)
    run.add_argument("--variants", default="rag,no_rag", help="comma-separated: rag, no_rag")
    run.add_argument("--baseline-all", action="store_true", help="run no_rag on every question, not just rules")
    run.add_argument("--ids", help="comma-separated question ids")
    run.add_argument("--category", help="comma-separated categories")
    run.add_argument("--limit", type=int)
    run.add_argument("--resume", metavar="RUN_ID")
    run.add_argument("--budget-per-question", type=float, default=1.0, help="Claude Code --max-budget-usd cap")
    run.add_argument("--timeout", type=float, default=300, help="seconds per question")
    run.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN_PATH)
    run.add_argument("--dry-run", action="store_true", help="resolve expected answers only")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="summarize a run")
    report.add_argument("run_id", nargs="?")
    report.add_argument("--failures", action="store_true", help="list every wrong answer")
    report.set_defaults(func=cmd_report)

    export = sub.add_parser("export", help="CSV of all runs for Tableau")
    export.add_argument("--out", type=Path, default=EVALS_DIR / "eval_results.csv")
    export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
