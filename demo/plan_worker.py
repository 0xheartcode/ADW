"""A fake worker that can PLAN and DO, for exercising the plan step offline.

Branch on the prompt, like a real worker would on intent:
- a plan prompt (mentions "milestone"/"plan.json") -> write a two-milestone
  plan.json whose gates are deterministic and always available on PATH;
- anything else (a do.<id> step) -> write an attempt file, like demo/worker.py.

Costs $0, fully deterministic. Set ADW_DEMO_SLEEP=<secs> to make the DO steps
dawdle, so an e2e run can kill the process mid-milestone and test resume.
Emits claude-shaped stream-json so run_agent/_narrate parse a clean result.
"""
import json
import os
import pathlib
import sys
import time

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
is_plan = "milestone" in prompt.lower() or "plan.json" in prompt.lower()

if is_plan:
    # Two gated milestones. `true` is on every PATH and always exits 0, so the
    # splice+chain is what is under test here, not the gate's depth.
    plan = [
        {"id": "m1", "prompt": "Create step_one.txt with the text one.",
         "gate": ["true"]},
        {"id": "m2", "prompt": "Create step_two.txt with the text two.",
         "gate": ["true"]},
    ]
    pathlib.Path("plan.json").write_text(json.dumps(plan, indent=2))
    tool = "Write(plan.json)"
else:
    sleep = float(os.environ.get("ADW_DEMO_SLEEP", "0") or "0")
    if sleep:
        time.sleep(sleep)
    n = len(list(pathlib.Path().glob("attempt-*.txt"))) + 1
    pathlib.Path(f"attempt-{n}.txt").write_text(prompt)
    tool = f"Write(attempt-{n}.txt)"

print(json.dumps({"type": "system", "subtype": "init", "tools": []}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Write", "input": {"file": tool}},
]}}))
print(json.dumps({"type": "result", "subtype": "success",
                  "is_error": False, "total_cost_usd": 0.0}))
