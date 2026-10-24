"""Bridge between the nba-stats MCP server and Claude tool definitions."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server import MCPServer


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool
    latency_ms: float


def stdio_server_params() -> StdioServerParameters:
    """Launch the MCP server as a subprocess, the same way Claude Desktop or Claude Code would.

    The full environment is passed through so DUCKDB_PATH and NBA_MODEL_CACHE
    reach the server (the SDK's default only forwards a minimal set).
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "nba_agent.mcp_server.server"],
        cwd=os.getcwd(),
        env=dict(os.environ),
    )


class MCPToolbox:
    """Lists the server's tools for Claude and runs the calls Claude makes."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._tools: list[dict[str, Any]] | None = None

    @classmethod
    @asynccontextmanager
    async def connect(cls, server: MCPServer | None = None) -> AsyncIterator[MCPToolbox]:
        """Connect to an in-process server (tests) or spawn `nba-mcp` over stdio (default)."""
        async with Client(server if server is not None else stdio_server_params()) as client:
            yield cls(client)

    async def tool_params(self) -> list[dict[str, Any]]:
        """Claude tool definitions, sorted by name so the prompt-cache prefix stays byte-identical."""
        if self._tools is None:
            listed = (await self._client.list_tools()).tools
            self._tools = [
                {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                for t in sorted(listed, key=lambda t: t.name)
            ]
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()
        result = await self._client.call_tool(name, arguments)
        latency_ms = (time.perf_counter() - started) * 1000

        if result.structured_content is not None and not result.is_error:
            content = json.dumps(result.structured_content, ensure_ascii=False, default=str)
        else:
            content = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return ToolOutcome(content=content, is_error=bool(result.is_error), latency_ms=round(latency_ms, 1))
