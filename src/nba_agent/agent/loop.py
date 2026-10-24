"""Multi-step tool-use loop: Claude plans, calls MCP tools, reads results, answers.

The loop is written by hand (not the SDK's beta tool runner) so every model
call and every tool call can be timed and its token usage recorded.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from nba_agent.agent.mcp_tools import MCPToolbox

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_STEPS = 10
MAX_TOKENS = 16000
# Server-side refusal fallback: if a safety classifier declines, the API re-runs
# the request on a fallback model it picks by refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

def build_system_prompt(rulebook: bool = True) -> str:
    """The agent's instructions; rulebook=False is the no-RAG eval baseline.

    Kept free of anything that changes per request (dates, IDs) so tools +
    system form a stable, cacheable prefix. Today's date goes in the user message.
    """
    tools = [
        "- query_stats: read-only SQL over player and team box scores for the last three "
        "completed seasons (regular season and playoffs), including pace, ratings and "
        "pre/post All-Star splits.",
        "- get_schedule: the current season's schedule and final scores.",
    ]
    if rulebook:
        tools.append("- search_rulebook: passages from the current Official NBA Playing Rules.")
    rules_line = (
        "For rules answers, cite the rule, section and page from search_rulebook. "
        if rulebook else "For rules questions, answer from your own knowledge of the NBA rules. "
    )
    return (
        f"You answer questions about the NBA using {'three' if rulebook else 'two'} tools:\n\n"
        + "\n".join(tools)
        + "\n\nGround every number in tool results; don't answer stats from memory. Break "
        "comparative questions into the queries they need, and check that a result "
        "actually covers the question (right team, season, season type) before relying "
        "on it. If a query fails, read the error, fix the SQL and try again.\n\n"
        "Answer in a few sentences. State the key numbers with their units and the "
        f"seasons they cover. {rules_line}If the data can't answer the question, say "
        "what's missing instead of guessing."
    )


SYSTEM_PROMPT = build_system_prompt()


@dataclass
class StepRecord:
    """One Messages API call."""

    step: int
    model: str
    stop_reason: str | None
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # Per-model token breakdown when a fallback ran; empty otherwise.
    iterations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolCallRecord:
    step: int
    name: str
    input: dict[str, Any]
    is_error: bool
    latency_ms: float
    result_chars: int


@dataclass
class AgentResult:
    question: str
    answer: str
    stop_reason: str
    model: str
    effort: str
    steps: list[StepRecord]
    tool_calls: list[ToolCallRecord]
    latency_ms: float

    @property
    def input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)

    @property
    def output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)

    @property
    def cache_read_tokens(self) -> int:
        return sum(s.cache_read_tokens for s in self.steps)

    @property
    def cache_write_tokens(self) -> int:
        return sum(s.cache_write_tokens for s in self.steps)


def _usage_iterations(usage: Any) -> list[dict[str, Any]]:
    out = []
    for it in getattr(usage, "iterations", None) or []:
        out.append({
            "type": it.type,
            "model": getattr(it, "model", None),
            "input_tokens": it.input_tokens or 0,
            "output_tokens": it.output_tokens or 0,
            "cache_read_tokens": getattr(it, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(it, "cache_creation_input_tokens", 0) or 0,
        })
    return out


def _text(content: list[Any]) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


class Agent:
    def __init__(
        self,
        client: Any,  # anthropic.AsyncAnthropic, or a test double with the same shape
        toolbox: MCPToolbox,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_steps: int = DEFAULT_MAX_STEPS,
        today: date | None = None,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.model = model
        self.effort = effort
        self.max_steps = max_steps
        self.today = today

    async def _create(self, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        return await self.client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            # Caches tools + system + the conversation so far; each step re-reads it at ~10% cost.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    async def _run_tools(self, step: int, blocks: list[Any]) -> tuple[list[dict[str, Any]], list[ToolCallRecord]]:
        """Run every tool_use block from one response concurrently; results go back in one message."""
        outcomes = await asyncio.gather(*(self.toolbox.call(b.name, dict(b.input)) for b in blocks))
        results, records = [], []
        for block, outcome in zip(blocks, outcomes):
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": outcome.content}
            if outcome.is_error:
                result["is_error"] = True
            results.append(result)
            records.append(ToolCallRecord(
                step=step, name=block.name, input=dict(block.input), is_error=outcome.is_error,
                latency_ms=outcome.latency_ms, result_chars=len(outcome.content),
            ))
        return results, records

    async def ask(self, question: str) -> AgentResult:
        today = self.today or date.today()
        tools = await self.toolbox.tool_params()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"Today's date (US Eastern): {today.isoformat()}\n\n{question}"}
        ]
        steps: list[StepRecord] = []
        tool_calls: list[ToolCallRecord] = []
        answer, stop, model = "", "max_steps", self.model
        last_content: list[Any] = []
        started = time.perf_counter()

        for step in range(1, self.max_steps + 1):
            call_started = time.perf_counter()
            response = await self._create(tools, messages)
            usage = response.usage
            model = response.model
            steps.append(StepRecord(
                step=step,
                model=response.model,
                stop_reason=response.stop_reason,
                latency_ms=round((time.perf_counter() - call_started) * 1000, 1),
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                iterations=_usage_iterations(usage),
            ))
            # Append the full content (thinking, fallback and tool_use blocks included):
            # the history stays append-only, which thinking-block replay requires.
            messages.append({"role": "assistant", "content": response.content})
            last_content = response.content

            if response.stop_reason == "tool_use":
                blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
                results, records = await self._run_tools(step, blocks)
                tool_calls.extend(records)
                messages.append({"role": "user", "content": results})
                continue
            if response.stop_reason == "pause_turn":
                continue  # the assistant turn is already appended; re-send to resume

            stop = response.stop_reason or "unknown"
            answer = _text(response.content)
            if stop == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                answer = answer or f"(The model declined to answer{f': {category}' if category else ''}.)"
            break
        else:
            # Out of steps mid-investigation: return whatever the model last said.
            answer = _text(last_content)

        return AgentResult(
            question=question,
            answer=answer,
            stop_reason=stop,
            model=model,
            effort=self.effort,
            steps=steps,
            tool_calls=tool_calls,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
