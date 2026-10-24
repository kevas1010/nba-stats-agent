"""nba-ask: ask the NBA stats agent a question.

    nba-ask "Did the Knicks play faster after the All-Star break?"
    nba-ask -v "When does the shot clock reset to 14?"    # show each tool call
    nba-ask                                               # interactive
    nba-ask --report                                      # latency/cost summary of logged runs

Every run is appended to data/logs/agent_runs.jsonl (override with
NBA_AGENT_LOG or --log; disable with --no-log).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from nba_agent.agent.loop import DEFAULT_EFFORT, DEFAULT_MAX_STEPS, DEFAULT_MODEL, Agent, AgentResult
from nba_agent.agent.mcp_tools import MCPToolbox
from nba_agent.agent.runlog import append_run, default_log_path, read_runs, summarize

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _percentile(values: list[float], pct: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def format_trace(result: AgentResult, record: dict[str, Any], verbose: bool = False) -> str:
    cost = f"${record['cost_usd']:.4f}" if record["cost_usd"] is not None else "cost n/a"
    lines = [
        f"-- {record['steps']} steps, {record['tool_calls']} tool calls, "
        f"{record['latency_ms'] / 1000:.1f}s (model {record['model_latency_ms'] / 1000:.1f}s, "
        f"tools {record['tool_latency_ms'] / 1000:.2f}s summed; parallel calls overlap)",
        f"-- {record['input_tokens']:,} in / {record['output_tokens']:,} out tokens "
        f"({record['cache_read_tokens']:,} cache read, {record['cache_write_tokens']:,} cache write), "
        f"{cost}, {result.model} ({result.effort})",
    ]
    for s in record["step_records"]:
        tools = [c for c in result.tool_calls if c.step == s["step"]]
        names = ", ".join(f"{c.name}{' [error]' if c.is_error else ''} {c.latency_ms:.0f}ms" for c in tools)
        lines.append(
            f"   step {s['step']}: {s['latency_ms']:.0f}ms, {s['input_tokens']:,}+{s['cache_read_tokens']:,} cached in, "
            f"{s['output_tokens']:,} out -> {s['stop_reason']}{': ' + names if names else ''}"
        )
        if verbose:
            for c in tools:
                lines.append(f"      {c.name} {json.dumps(c.input, ensure_ascii=False)}")
    if result.stop_reason not in ("end_turn",):
        lines.append(f"-- stopped: {result.stop_reason}")
    return "\n".join(lines)


def report(path: Path) -> str:
    runs = read_runs(path)
    if not runs:
        return f"no runs logged at {path}"
    latency = [r["latency_ms"] / 1000 for r in runs]
    costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    tool_ms = [t["latency_ms"] for r in runs for t in r["tool_records"]]
    by_tool: dict[str, list[float]] = {}
    for r in runs:
        for t in r["tool_records"]:
            by_tool.setdefault(t["name"], []).append(t["latency_ms"])
    lines = [
        f"{len(runs)} runs in {path}",
        f"latency  p50 {_percentile(latency, 50):.1f}s   p95 {_percentile(latency, 95):.1f}s   max {max(latency):.1f}s",
        f"steps    mean {statistics.mean(r['steps'] for r in runs):.1f}   "
        f"tool calls mean {statistics.mean(r['tool_calls'] for r in runs):.1f}   "
        f"tool errors {sum(r['tool_errors'] for r in runs)}",
        f"tokens   mean {statistics.mean(r['input_tokens'] for r in runs):,.0f} in / "
        f"{statistics.mean(r['output_tokens'] for r in runs):,.0f} out, "
        f"cache read {sum(r['cache_read_tokens'] for r in runs):,} total",
    ]
    if costs:
        lines.append(f"cost     mean ${statistics.mean(costs):.4f}/question   total ${sum(costs):.4f}"
                     + (f"   ({len(runs) - len(costs)} unpriced)" if len(costs) < len(runs) else ""))
    if tool_ms:
        lines.append("tools    " + "   ".join(
            f"{name} p50 {_percentile(ms, 50):.0f}ms (n={len(ms)})" for name, ms in sorted(by_tool.items())))
    stops: dict[str, int] = {}
    for r in runs:
        stops[r["stop_reason"]] = stops.get(r["stop_reason"], 0) + 1
    lines.append("stops    " + ", ".join(f"{k} {v}" for k, v in sorted(stops.items())))
    return "\n".join(lines)


async def _ask_all(questions: list[str] | None, args: argparse.Namespace) -> int:
    client = anthropic.AsyncAnthropic()
    async with MCPToolbox.connect() as toolbox:
        agent = Agent(client, toolbox, model=args.model, effort=args.effort, max_steps=args.max_steps)

        async def ask(question: str) -> None:
            result = await agent.ask(question)
            record = summarize(result)
            print(result.answer or "(no answer)")
            print()
            print(format_trace(result, record, args.verbose))
            if not args.no_log:
                append_run(record, args.log)

        if questions:
            for q in questions:
                await ask(q)
            return 0

        print("Ask about NBA stats, schedules or rules. Empty line or Ctrl+C to quit.")
        while True:
            try:
                question = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not question:
                return 0
            await ask(question)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # Answers and rulebook passages contain em-dashes and curly quotes; Windows consoles default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="*", help="question to ask; omit for interactive mode")
    parser.add_argument("--model", default=os.getenv("NBA_AGENT_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--effort", choices=EFFORTS, default=os.getenv("NBA_AGENT_EFFORT") or DEFAULT_EFFORT)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("-v", "--verbose", action="store_true", help="show each tool call's input")
    parser.add_argument("--log", type=Path, default=None, help=f"run log path (default {default_log_path()})")
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--report", action="store_true", help="summarize logged runs and exit")
    args = parser.parse_args(argv)

    if args.report:
        print(report(args.log or default_log_path()))
        return 0

    question = " ".join(args.question).strip()
    try:
        return asyncio.run(_ask_all([question] if question else None, args))
    except Exception as exc:
        message = friendly_error(exc)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1


def _leaf(exc: BaseException) -> BaseException:
    """The MCP client runs inside an anyio task group, which wraps errors in ExceptionGroups."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def friendly_error(exc: BaseException) -> str | None:
    err = _leaf(exc)
    # With no key, token or profile, the SDK raises TypeError when building request headers.
    if isinstance(err, (anthropic.AuthenticationError, anthropic.CredentialsError)) or (
        isinstance(err, TypeError) and "authentication method" in str(err)
    ):
        return "No valid Anthropic credentials. Set ANTHROPIC_API_KEY in .env (see .env.example)."
    if isinstance(err, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API; wait a moment and retry."
    if isinstance(err, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API; check your connection."
    if isinstance(err, anthropic.APIStatusError):
        return f"Anthropic API error {err.status_code}: {err.message}"
    return None


if __name__ == "__main__":
    sys.exit(main())
