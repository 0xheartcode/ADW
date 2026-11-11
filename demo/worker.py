"""A fake worker: edits a file, emits claude-shaped stream-json, costs $0.

The point is that the orchestrator can be tested without paying an LLM and
without nondeterminism. Every retry path, breaker and counter in adw.py was
verified through this. It also demonstrates the whole worker contract: a
worker is a CLI that changes files in a directory, and nothing else.

Deliberately emits two unparseable lines so the stream-errors.log sidecar
has something to catch.
"""

import json
import os
import pathlib
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""

# Stub for the rate-limit park (gli #4): when ADW_DEMO_RATELIMIT is set, emit the
# real subscription usage-limit line and exit non-zero, the way claude does when
# a Max/Pro plan hits its limit. Lets the full CLI be driven through the park
# path offline (stub-verified). Normal runs leave this unset and are unaffected.
if os.environ.get("ADW_DEMO_RATELIMIT"):
    reset = os.environ.get("ADW_DEMO_RATELIMIT_RESET", "1799999999")
    print(f"Claude AI usage limit reached|{reset}")
    sys.exit(1)
n = len(list(pathlib.Path().glob("attempt-*.txt"))) + 1
pathlib.Path(f"attempt-{n}.txt").write_text(prompt)

print(json.dumps({"type": "system", "subtype": "init", "tools": []}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write", "input": {"file": f"attempt-{n}.txt"}},
]}}))
print("this line is not json at all")
print('{"type": "assistant", "message": {')
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": False, "total_cost_usd": 0.0}))
