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
dogfood scenario="all":
    .venv/bin/python dogfood/run.py {{scenario}}
