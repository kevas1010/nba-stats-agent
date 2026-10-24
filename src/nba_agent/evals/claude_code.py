"""Run one question through Claude Code headless (`claude -p`) with the nba-stats MCP server.

This uses your Claude Code sign-in instead of an API key, so evals cost
nothing beyond your plan's usage. Each run is isolated:

- the agent's own system prompt replaces Claude Code's default one, so the
  model sees the same instructions as `nba-ask`;
- built-in tools (files, shell, web) are off and only the MCP tools are allowed;
- no user/project settings, hooks or plugins load (`--setting-sources ""`), and
  the run happens in an empty scratch directory with no CLAUDE.md;
- the desktop app's session variables (ANTHROPIC_*, CLAUDE_*) are stripped so
  the child CLI authenticates with its own sign-in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nba_agent.agent.loop import build_system_prompt

SERVER_NAME = "nba-stats"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
ALL_TOOLS = ("query_stats", "get_schedule", "search_rulebook")
VARIANTS = {"rag": ALL_TOOLS, "no_rag": ("query_stats", "get_schedule")}
# Keep a custom config dir if you use one; drop everything else the desktop session sets.
_KEEP_ENV = {"CLAUDE_CONFIG_DIR"}


@dataclass
class RunResult:
    reply: str = ""
    is_error: bool = False
    error: str = ""
    subtype: str = ""
    wall_ms: float = 0.0
    duration_ms: float | None = None
    duration_api_ms: float | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    models: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    mcp_status: str = ""


def find_claude() -> str:
    """NBA_CLAUDE_BIN, then `claude` on PATH, then the standalone install in ~/.local/bin."""
    if env := os.getenv("NBA_CLAUDE_BIN"):
        return env
    if found := shutil.which("claude"):
        return found
    for name in ("claude.exe", "claude"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Claude Code CLI not found; install it or set NBA_CLAUDE_BIN")


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v for k, v in (base if base is not None else os.environ).items()
        if not k.upper().startswith(("ANTHROPIC_", "CLAUDE")) or k.upper() in _KEEP_ENV
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def mcp_config(db_path: Path, model_cache: Path) -> dict[str, Any]:
    return {"mcpServers": {SERVER_NAME: {
        "command": sys.executable,
        "args": ["-m", "nba_agent.mcp_server.server"],
        "env": {
            "DUCKDB_PATH": str(db_path.resolve()),
            "NBA_MODEL_CACHE": str(model_cache.resolve()),
            "PYTHONIOENCODING": "utf-8",
        },
    }}}


def build_command(
    claude_bin: str | list[str],
    prompt: str,
    *,
    model: str,
    variant: str,
    config_path: Path,
    max_budget_usd: float,
) -> list[str]:
    allowed = [TOOL_PREFIX + t for t in VARIANTS[variant]]
    blocked = [TOOL_PREFIX + t for t in ALL_TOOLS if t not in VARIANTS[variant]]
    cmd = [*([claude_bin] if isinstance(claude_bin, str) else claude_bin),
           "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--model", model,
           "--system-prompt", build_system_prompt(rulebook=variant == "rag"),
           "--mcp-config", str(config_path), "--strict-mcp-config",
           "--tools", "",
           "--allowedTools", " ".join(allowed),
           "--permission-mode", "dontAsk",
           "--setting-sources", "",
           "--no-session-persistence",
           "--max-budget-usd", f"{max_budget_usd:.2f}"]
    if blocked:
        cmd += ["--disallowedTools", " ".join(blocked)]
    return cmd


def parse_stream(lines: list[str]) -> RunResult:
    """Fold Claude Code's stream-json events into one RunResult."""
    result = RunResult()
    pending: dict[str, dict[str, Any]] = {}
    texts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            for server in event.get("mcp_servers", []):
                if server.get("name") == SERVER_NAME:
                    result.mcp_status = server.get("status", "")
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    call = {"id": block.get("id"), "name": block.get("name", "").removeprefix(TOOL_PREFIX),
                            "input": block.get("input", {}), "is_error": False}
                    pending[call["id"]] = call
                    result.tool_calls.append(call)
                elif block.get("type") == "text":
                    texts.append(block.get("text", ""))
        elif kind == "user":
            content = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    if block.get("tool_use_id") in pending:
                        pending[block["tool_use_id"]]["is_error"] = True
        elif kind == "result":
            usage = event.get("usage") or {}
            result.reply = event.get("result") or "\n".join(texts)
            result.is_error = bool(event.get("is_error"))
            result.subtype = event.get("subtype", "")
            result.duration_ms = event.get("duration_ms")
            result.duration_api_ms = event.get("duration_api_ms")
            result.num_turns = event.get("num_turns")
            result.cost_usd = event.get("total_cost_usd")
            result.input_tokens = usage.get("input_tokens", 0) or 0
            result.output_tokens = usage.get("output_tokens", 0) or 0
            result.cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
            result.cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0
            result.models = sorted((event.get("modelUsage") or {}).keys())
            if result.is_error:
                result.error = result.reply
    if not result.reply and texts:
        result.reply = "\n".join(texts)
    for call in result.tool_calls:
        call.pop("id", None)
    return result


class ClaudeCodeRunner:
    def __init__(
        self,
        *,
        model: str,
        variant: str = "rag",
        db_path: Path = Path("data/nba.duckdb"),
        model_cache: Path = Path("data/models"),
        claude_bin: str | list[str] | None = None,
        timeout_s: float = 300,
        max_budget_usd: float = 1.0,
    ) -> None:
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {sorted(VARIANTS)}")
        self.model = model
        self.variant = variant
        self.claude_bin = claude_bin or find_claude()
        self.timeout_s = timeout_s
        self.max_budget_usd = max_budget_usd
        # An empty scratch directory: no CLAUDE.md or project settings for Claude Code to pick up.
        self.workdir = Path(tempfile.mkdtemp(prefix="nba-eval-"))
        self.config_path = self.workdir / "mcp.json"
        self.config_path.write_text(json.dumps(mcp_config(db_path, model_cache)), encoding="utf-8")

    def run(self, prompt: str) -> RunResult:
        cmd = build_command(self.claude_bin, prompt, model=self.model, variant=self.variant,
                            config_path=self.config_path, max_budget_usd=self.max_budget_usd)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, cwd=self.workdir, env=clean_env(), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return RunResult(is_error=True, error=f"timed out after {self.timeout_s:.0f}s",
                             wall_ms=round((time.perf_counter() - started) * 1000, 1))
        result = parse_stream(proc.stdout.splitlines())
        result.wall_ms = round((time.perf_counter() - started) * 1000, 1)
        if proc.returncode != 0 and not result.error:
            result.is_error = True
            result.error = (proc.stderr or proc.stdout).strip()[-500:] or f"exit code {proc.returncode}"
        return result
