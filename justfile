# adw.py task runner. Invocation shortcuts only - flow definitions live in
# WORKFLOWS inside adw.py, and there is deliberately no second place to look.

_default:
    @just --list

# Run a task with the claude worker (the normal case)
run task workflow="feature":
    ./adw.py run "{{task}}" --workflow {{workflow}}

# Run against the local endpoint in .env instead of the paid API
local task workflow="feature":
    ./adw.py run "{{task}}" --worker aider --workflow {{workflow}}

# Run in an isolated worktree, so several runs can go at once
parallel task workflow="feature":
    ./adw.py run "{{task}}" --workflow {{workflow}} --worktree

resume run *ARGS:
    ./adw.py resume {{run}} {{ARGS}}

show run *ARGS:
    ./adw.py show {{run}} {{ARGS}}

list:
    ./adw.py list

# Mermaid source for a workflow - paste into anything that renders it
graph workflow="feature":
    @./adw.py graph {{workflow}}

# Free, offline, deterministic: exercises retry, feedback and the human gate
demo:
    #!/usr/bin/env bash
    set -euo pipefail
    d=$(mktemp -d); cd "$d"; echo seed > seed.txt
    git init -q . && git add -A && git commit -qm init
    cd - >/dev/null
    ./adw.py run "demo" --workflow demo --worker demo --repo "$d" --auto-approve

# Same, but the gate never changes its mind - should stop after two
demo-stuck:
    #!/usr/bin/env bash
    set -euo pipefail
    d=$(mktemp -d); cd "$d"; echo seed > seed.txt
    git init -q . && git add -A && git commit -qm init
    cd - >/dev/null
    ./adw.py run "stuck" --workflow demo-stuck --worker demo --repo "$d" || true

# A guided first run: watch ADW drive the free demo workflow end to end
tutorial:
    #!/usr/bin/env bash
    set -euo pipefail
    cat <<'EOF'

    ============================================================
     ADW tutorial - a guided first run (free, offline, no API key)
    ============================================================
    ADW runs a nondeterministic worker, then a deterministic GATE,
    and lets the gate's exit code decide the next move: advance,
    retry-with-feedback, or park for a human.

    This drives the built-in `demo` workflow against a fresh throwaway
    git repo, with the deterministic `demo` worker. Watch the output for:
      [<step>] attempt N    a step starting (a worker, or a gate)
      [gate] ... PASS/FAIL  the deterministic check deciding what happens next
      [commit] <sha>        every attempt is committed, pass or fail
      DONE / PARKED         how the run ended
    EOF
    read -rp "Press Enter to start the run... " _
    d=$(mktemp -d); cd "$d"; echo seed > seed.txt
    git init -q . && git add -A && git commit -qm init
    cd - >/dev/null
    ./adw.py run "tutorial" --workflow demo --worker demo --repo "$d" --auto-approve
    cat <<'EOF'

    ------------------------------------------------------------
    The run left a full record under runs/. Explore it:
      just list                   every run and how it ended
      ./adw.py show runs/<dir>    the timeline, attempt by attempt
      just graph demo             the workflow as a mermaid diagram
    See a run PARK instead of finish (a gate that never passes):
      just demo-stuck             stops after two identical failures
    Next: `just e2e` drives adw.py against real sandboxes (the e2e tier).
    ------------------------------------------------------------
    EOF

# Like `tutorial`, but YOU answer the human review gate (needs a real terminal)
tutorial-interview:
    #!/usr/bin/env bash
    set -euo pipefail
    cat <<'EOF'

    This run PAUSES at the human review gate and asks you to approve/reject.
    Same free `demo` worker as `just tutorial` - no API key, nothing to spend.
    EOF
    read -rp "Press Enter to start... " _
    d=$(mktemp -d); cd "$d"; echo seed > seed.txt
    git init -q . && git add -A && git commit -qm init
    cd - >/dev/null
    ./adw.py run "tutorial" --workflow demo --worker demo --repo "$d"

# adw.py should pass the gates it enforces
check:
    ruff check --isolated adw.py demo/
    python3 -m py_compile adw.py

# Everything that costs nothing
test: check demo demo-stuck

# Dev test suite (pytest+hypothesis live in .venv; adw.py stays zero-dep)
test-py:
    .venv/bin/python -m pytest tests/ -q

# Enforce the zero-dependency runtime invariant
check-deps:
    .venv/bin/python -m pytest tests/test_zero_dep.py -q

# Run ADW for real against throwaway sandboxes
e2e scenario="all":
    .venv/bin/python e2e/run.py {{scenario}}
