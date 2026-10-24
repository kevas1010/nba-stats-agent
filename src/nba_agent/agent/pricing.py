"""Estimate USD cost from token usage.

Rates are Anthropic first-party API prices per million tokens (as of
2026-10-06). Cache writes use the 5-minute TTL rate (1.25x input). Models that
a refusal fallback may route to are included so those turns are priced too.
"""

from __future__ import annotations

from dataclasses import dataclass

from nba_agent.agent.loop import StepRecord


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Rates] = {
    "claude-opus-5-5": Rates(4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": Rates(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": Rates(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5-5": Rates(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Rates(0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": Rates(10.00, 50.00, 0.25, 12.50),
}


def token_cost(model: str | None, input_tokens: int, output_tokens: int,
               cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """USD for one call, or None if the model's price isn't known."""
    rates = PRICES.get(model or "")
    if rates is None:
        return None
    return (
        input_tokens * rates.input
        + output_tokens * rates.output
        + cache_read_tokens * rates.cache_read
        + cache_write_tokens * rates.cache_write
    ) / 1_000_000


def step_cost(step: StepRecord) -> float | None:
    """Cost of one API call; when a fallback ran, each model's share is priced at its own rate."""
    if step.iterations:
        parts = [
            token_cost(it["model"], it["input_tokens"], it["output_tokens"],
                       it["cache_read_tokens"], it["cache_write_tokens"])
            for it in step.iterations
        ]
        return None if any(p is None for p in parts) else sum(parts)
    return token_cost(step.model, step.input_tokens, step.output_tokens,
                      step.cache_read_tokens, step.cache_write_tokens)


def total_cost(steps: list[StepRecord]) -> float | None:
    costs = [step_cost(s) for s in steps]
    return None if any(c is None for c in costs) else sum(costs)
