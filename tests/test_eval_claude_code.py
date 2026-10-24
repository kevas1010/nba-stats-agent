import json
import sys
from pathlib import Path

import pytest

from nba_agent.agent.loop import build_system_prompt
from nba_agent.evals.claude_code import ClaudeCodeRunner, build_command, clean_env, parse_stream

FAKE_CLI = [sys.executable, str(Path(__file__).with_name("fake_claude_cli.py"))]

TRANSCRIPT = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5",
     "mcp_servers": [{"name": "nba-stats", "status": "connected"}]},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "t1", "name": "mcp__nba-stats__query_stats", "input": {"sql": "select * from raw.x"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "table not allowed"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "mcp__nba-stats__query_stats", "input": {"sql": "select 51"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "51"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "They won 51.\nANSWER: 51",
     "duration_ms": 8200, "duration_api_ms": 7400, "num_turns": 3, "total_cost_usd": 0.0412,
     "usage": {"input_tokens": 1200, "output_tokens": 310, "cache_read_input_tokens": 9000,
               "cache_creation_input_tokens": 4100},
     "modelUsage": {"claude-opus-5-5": {}}},
]


def test_parse_stream_collects_tools_usage_and_reply():
    r = parse_stream([json.dumps(e) for e in TRANSCRIPT] + ["not json noise"])
    assert r.reply.endswith("ANSWER: 51") and not r.is_error
    assert r.mcp_status == "connected"
    assert [(c["name"], c["is_error"]) for c in r.tool_calls] == [("query_stats", True), ("query_stats", False)]
    assert (r.num_turns, r.duration_ms, r.cost_usd) == (3, 8200, 0.0412)
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == (1200, 310, 9000, 4100)
    assert r.models == ["claude-opus-5-5"]


def test_parse_stream_reports_auth_failure():
    r = parse_stream([json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                  "result": "Invalid API key · Please run /login", "total_cost_usd": 0})])
    assert r.is_error and "login" in r.error


def test_command_isolates_the_run(tmp_path):
    cmd = build_command("claude", "q?", model="claude-opus-5-5", variant="rag",
                        config_path=tmp_path / "mcp.json", max_budget_usd=0.5)
    arg = lambda flag: cmd[cmd.index(flag) + 1]  # noqa: E731
    assert cmd[:3] == ["claude", "-p", "q?"]
    assert arg("--tools") == "" and arg("--setting-sources") == ""
    assert arg("--permission-mode") == "dontAsk" and "--strict-mcp-config" in cmd
    assert arg("--system-prompt") == build_system_prompt(rulebook=True)
    assert arg("--allowedTools").split() == [
        "mcp__nba-stats__query_stats", "mcp__nba-stats__get_schedule", "mcp__nba-stats__search_rulebook"]
    assert arg("--max-budget-usd") == "0.50" and "--disallowedTools" not in cmd


def test_no_rag_variant_removes_the_rulebook(tmp_path):
    cmd = build_command("claude", "q?", model="m", variant="no_rag", config_path=tmp_path / "c.json",
                        max_budget_usd=1)
    arg = lambda flag: cmd[cmd.index(flag) + 1]  # noqa: E731
    assert "search_rulebook" not in arg("--allowedTools")
    assert arg("--disallowedTools") == "mcp__nba-stats__search_rulebook"
    assert arg("--system-prompt") == build_system_prompt(rulebook=False)


def test_clean_env_strips_desktop_session_vars():
    env = clean_env({"PATH": "/bin", "ANTHROPIC_BASE_URL": "http://localhost:1", "CLAUDE_CODE_SESSION_ID": "x",
                     "CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/home/k"})
    assert env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/home/k", "PYTHONIOENCODING": "utf-8"}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "script.json"
    script.write_text(json.dumps(TRANSCRIPT), encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://desktop-proxy")
    return record


def test_runner_end_to_end_with_fake_cli(fake_cli, tmp_path):
    runner = ClaudeCodeRunner(model="claude-opus-5-5", db_path=tmp_path / "nba.duckdb",
                              model_cache=tmp_path / "models", claude_bin=FAKE_CLI)
    r = runner.run("How many games did the Knicks win?")
    assert r.reply.endswith("ANSWER: 51") and r.cost_usd == 0.0412 and r.wall_ms > 0

    seen = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert "ANTHROPIC_BASE_URL" not in seen["env"]
    assert seen["stdin_closed"]
    assert Path(seen["cwd"]) == runner.workdir and not (runner.workdir / "CLAUDE.md").exists()
    config = json.loads(runner.config_path.read_text(encoding="utf-8"))["mcpServers"]["nba-stats"]
    assert config["args"] == ["-m", "nba_agent.mcp_server.server"]
    assert Path(config["env"]["DUCKDB_PATH"]).is_absolute()


def test_runner_reports_nonzero_exit(fake_cli, tmp_path, monkeypatch):
    (tmp_path / "script.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "2")
    r = ClaudeCodeRunner(model="m", db_path=tmp_path / "x", model_cache=tmp_path, claude_bin=FAKE_CLI).run("q")
    assert r.is_error and r.error


def test_runner_times_out(tmp_path):
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    r = ClaudeCodeRunner(model="m", db_path=tmp_path / "x", model_cache=tmp_path, claude_bin=slow,
                         timeout_s=0.5).run("q")
    assert r.is_error and "timed out" in r.error
