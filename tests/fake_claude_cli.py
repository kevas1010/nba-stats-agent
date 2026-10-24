"""Stands in for the `claude` CLI in tests: replays a scripted stream-json transcript.

FAKE_CLAUDE_SCRIPT points to a JSON list of events to print. FAKE_CLAUDE_RECORD,
if set, receives the argv and environment this process was started with.
Both reach this process because the runner only strips ANTHROPIC_*/CLAUDE_* vars.
"""

import json
import os
import sys

if record := os.environ.get("FAKE_CLAUDE_RECORD"):
    with open(record, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
                   "stdin_closed": sys.stdin.read() == ""}, f)

with open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8") as f:
    events = json.load(f)
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
