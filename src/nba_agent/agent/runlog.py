"""Per-run telemetry: one JSON line per question with tokens, cost and latency.

The log feeds the eval harness and the Tableau dashboard later, so every field
is flat or a list of flat records.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nba_agent.agent.loop import AgentResult
from nba_agent.agent.pricing import step_cost, total_cost

DEFAULT_LOG_PATH = Path("data/logs/agent_runs.jsonl")


def default_log_path() -> Path:
    return Path(os.getenv("NBA_AGENT_LOG", DEFAULT_LOG_PATH))


def summarize(result: AgentResult) -> dict[str, Any]:
    cost = total_cost(result.steps)
    model_ms = sum(s.latency_ms for s in result.steps)
    tool_ms = [c.latency_ms for c in result.tool_calls]
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": result.question,
        "answer": result.answer,
        "stop_reason": result.stop_reason,
        "model": result.model,
        "effort": result.effort,
        "steps": len(result.steps),
        "tool_calls": len(result.tool_calls),
        "tool_errors": sum(c.is_error for c in result.tool_calls),
        "tools_used": sorted({c.name for c in result.tool_calls}),
        "latency_ms": result.latency_ms,
        "model_latency_ms": round(model_ms, 1),
        "tool_latency_ms": round(sum(tool_ms), 1),
        "tool_latency_p50_ms": round(statistics.median(tool_ms), 1) if tool_ms else None,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "step_records": [
            {**asdict(s), "cost_usd": round(c, 6) if (c := step_cost(s)) is not None else None}
            for s in result.steps
        ],
        "tool_records": [asdict(c) for c in result.tool_calls],
    }


def append_run(record: dict[str, Any], path: Path | None = None) -> Path:
    path = path or default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return path


def read_runs(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or default_log_path()
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
