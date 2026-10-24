"""Scripted stand-in for AsyncAnthropic so agent tests run offline and free."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(id_: str, name: str, **arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=arguments)


def usage(inp=100, out=20, cache_read=0, cache_write=0, iterations=None) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
        iterations=iterations,
    )


def message(content, stop_reason, model="claude-opus-5-5", use=None, stop_details=None) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content), stop_reason=stop_reason, model=model,
        usage=use or usage(), stop_details=stop_details,
    )


class FakeClaude:
    """Returns scripted messages in order and records every request."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        if not self._responses:
            raise AssertionError("agent made more model calls than were scripted")
        return self._responses.pop(0)
