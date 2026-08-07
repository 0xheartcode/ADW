#!/usr/bin/env python3
"""
adw.py - an Agentic Developer Workflow runner.

    run a nondeterministic worker, then a deterministic gate,
    and let the gate's exit code decide what happens next.

    adw.py run "add rate limiting to /login"
    adw.py run --worker aider --workflow chore "drop the unused utils module"
    adw.py list
    adw.py show runs/2026-07-24T14-22-01_add-rate-limiting
    adw.py resume runs/2026-07-24T14-22-01_add-rate-limiting
    adw.py resume runs/... --from build     # rewind and re-execute forward
    adw.py graph feature                    # mermaid diagram of a workflow

Zero third-party dependencies. Keep it that way as long as you can.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS_DIR = Path("runs")
INDEX = RUNS_DIR / "index.jsonl"

# Never belongs in an attempt commit, whatever the worker. A gate that runs
# pytest creates these every single time, and they drown the review diff.
ARTIFACTS = ["__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache"]

GATE_HEAD = 2000  # chars from the start of gate output (first traceback)
GATE_TAIL = 2000  # chars from the end (pytest's summary)
AGENT_TIMEOUT = 900
GATE_TIMEOUT = 600

# Exit codes a wrapper (the butler bot) branches on. 0 = green the whole way;
# EXIT_PARKED = the run stopped cleanly and is waiting for a human decision -
# not a crash. Anything else is a usage/error exit. 75 is sysexits' EX_TEMPFAIL,
# "temporary failure, try again later", which is exactly what a park is.
EXIT_PARKED = 75


def exit_code(state: dict) -> int:
    return {"done": 0, "parked": EXIT_PARKED}.get(state["status"], 1)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Ten lines instead of python-dotenv, because the no-dependencies rule is
    worth more than the convenience. KEY=value, # comments, no interpolation.

    Already-exported variables win, so `FOO=x adw.py run ...` still overrides
    the file. Endpoints and keys live here so they stay out of git.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()  # before WORKERS, which reads the environment at import time

# Anything matching these gets redacted before it touches a log file.
SECRET_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


# ---------------------------------------------------------------------------
# WORKERS
#
# A worker is a CLI that edits files in a directory. That's the entire
# contract - which is why this is a dict and not a plugin system.
# {prompt} and {budget} are substituted at call time.
#
# A worker whose cmd has no {budget} placeholder runs uncapped; run_agent
# warns once when that happens.
# ---------------------------------------------------------------------------

WORKERS = {
    "claude": {
        "cmd": [
            "claude", "-p", "{prompt}",
            "--output-format", "stream-json", "--verbose",
            # Two different flags that sound like one. --tools decides which
            # tools EXIST; --allowedTools only pre-approves ones that already
            # do. Passing --allowedTools alone sandboxes nothing - the worker
            # keeps full Bash and just stops asking. You want both.
            "--tools", "Read,Write,Edit,Glob,Grep,Bash",
            "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
            "--permission-mode", "acceptEdits",
            # Keep the operator's laptop out of the run. Without these the
            # worker inherits your personal MCP servers and user-level hooks,
            # so cost and behaviour drift with local config and attempt 2
            # is no longer comparable to attempt 3. The workspace's own
            # project settings still apply - those are part of the repo.
            "--strict-mcp-config",
            "--setting-sources", "project",
            # Bounds ONE attempt. The global ceiling is only checked between
            # steps, so without this a single attempt can sail past it.
            "--max-budget-usd", "{budget}",
        ],
        "stream": "claude-json",
    },
    # Point aider at any OpenAI-compatible endpoint - llama.cpp, vLLM, or a
    # LiteLLM proxy in front of them. Endpoint and key come from .env, not
    # from here: this file is the shareable part, your box's address is not.
    #
    # --no-auto-commit because snapshot() owns the commits; two things
    # committing to one repo makes the per-attempt history unreadable.
    # --no-stream and --no-pretty because this output goes to a log file, not
    # a terminal, and ANSI repaint codes make events.jsonl unreadable.
    "aider": {
        "cmd": [
            "aider", "--message", "{prompt}", "--yes-always", "--no-auto-commit",
            "--model", os.environ.get("ADW_MODEL", "openai/local"),
            "--openai-api-base",
            os.environ.get("ADW_API_BASE", "http://localhost:4000/v1"),
            "--no-stream", "--no-pretty", "--no-check-update",
            "--no-show-model-warnings", "--no-gitignore",
        ],
        # Scratch files this worker drops in the repo. snapshot() keeps them
        # out of the attempt commits so the review diff is only the work.
        "artifacts": [".aider*"],
        "requires_env": ["OPENAI_API_KEY"],
        "stream": "plain",
    },
    "codex": {
        "cmd": ["codex", "exec", "{prompt}"],
        "stream": "plain",
    },
    # Costs nothing, never varies. Lets you exercise the retry loop, the
    # breakers and the counters without an LLM in the way. See demo/.
    "demo": {
        "cmd": [sys.executable, str(HERE / "demo/worker.py"), "{prompt}"],
        "stream": "claude-json",
    },
}


# ---------------------------------------------------------------------------
# WORKFLOWS - this list IS the diagram. Boxes are agents, diamonds are gates.
# ---------------------------------------------------------------------------

WORKFLOWS = {
    "feature": [
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nImplement this. Write tests for what you add."},
        {"kind": "gate", "name": "test",
         "cmd": ["pytest", "-x", "-q"], "on_fail": "build", "max": 3},
        {"kind": "gate", "name": "lint",
         "cmd": ["ruff", "check", "."], "on_fail": "build", "max": 2},
        {"kind": "human", "name": "review"},
    ],
    "chore": [
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nKeep the change minimal and mechanical."},
        {"kind": "gate", "name": "lint",
         "cmd": ["ruff", "check", "."], "on_fail": "build", "max": 2},
    ],
    # An LLM reviewing the agent's diff - as a GATE, not a new step kind.
    # The judge's exit code is the routing and its stdout is the feedback, so
    # "one agent hands work to another" needs no machinery beyond what a
    # failing pytest already uses. Expensive model writes, cheap one reviews.
    "review": [
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nImplement this. Write tests for what you add."},
        {"kind": "gate", "name": "test",
         "cmd": ["pytest", "-x", "-q"], "on_fail": "build", "max": 3},
        {"kind": "gate", "name": "judge",
         "cmd": [sys.executable, str(HERE / "demo/judge.py"), "{base}"],
         "on_fail": "build", "max": 2},
        {"kind": "human", "name": "review"},
    ],
    # Red/green. The `red` gate is inverted: it passes only when the new test
    # FAILS, which proves the test actually tests something before anything
    # is implemented against it. Two agents in sequence, handing off through
    # the filesystem - which is the honest message bus here.
    "tdd": [
        {"kind": "agent", "name": "spec",
         "prompt": "{task}\n\nWrite ONLY a failing test for this. Do not "
                   "implement anything yet."},
        {"kind": "gate", "name": "red", "invert": True,
         "cmd": ["pytest", "-x", "-q"], "on_fail": "spec", "max": 2},
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nA failing test already exists. Make it pass "
                   "without weakening the test."},
        {"kind": "gate", "name": "green",
         "cmd": ["pytest", "-x", "-q"], "on_fail": "build", "max": 3},
        {"kind": "gate", "name": "lint",
         "cmd": ["ruff", "check", "."], "on_fail": "build", "max": 2},
        {"kind": "human", "name": "review"},
    ],
    # Run with --worker demo. Free, deterministic, offline.
    "demo": [
        {"kind": "agent", "name": "build", "prompt": "{task}"},
        {"kind": "gate", "name": "check",
         "cmd": [sys.executable, str(HERE / "demo/gate.py")], "on_fail": "build", "max": 4},
        {"kind": "human", "name": "review"},
    ],
    # Same, but the gate never changes its mind - trips the loop detector.
    "demo-stuck": [
        {"kind": "agent", "name": "build", "prompt": "{task}"},
        {"kind": "gate", "name": "check",
         "cmd": [sys.executable, str(HERE / "demo/gate_stuck.py")], "on_fail": "build", "max": 5},
    ],
}


# ---------------------------------------------------------------------------
# VALIDATION - runs before anything spends a token
# ---------------------------------------------------------------------------

def validate(steps: list, worker: str) -> list:
    errors = []
    names = [s.get("name") for s in steps]

    if worker not in WORKERS:
        errors.append(f"unknown worker {worker!r}")
    elif not shutil.which(WORKERS[worker]["cmd"][0]):
        errors.append(f"worker binary not on PATH: {WORKERS[worker]['cmd'][0]}")
    else:
        for var in WORKERS[worker].get("requires_env", []):
            if not os.environ.get(var):
                errors.append(f"worker {worker!r} needs {var} - set it in .env")

    if len(set(names)) != len(names):
        errors.append("duplicate step names")

    for s in steps:
        kind = s.get("kind")
        if kind not in ("agent", "gate", "human"):
            errors.append(f"{s.get('name')}: bad kind {kind!r}")
        if kind == "gate":
            # on_fail of None is legal and means "failure here is fatal". Not
            # every failure is the agent's to fix - a missing binary or a
            # broken endpoint will fail identically no matter how many times
            # you pay it to try again.
            if s.get("on_fail") is not None and s["on_fail"] not in names:
                errors.append(f"{s['name']}: on_fail -> {s['on_fail']!r} is not a step")
            if s.get("on_fail") is not None and (
                    not isinstance(s.get("max"), int) or s["max"] < 1):
                errors.append(f"{s['name']}: needs an integer max >= 1")
            if not shutil.which(s["cmd"][0]):
                errors.append(f"{s['name']}: command not on PATH: {s['cmd'][0]}")
        if kind == "agent" and "{task}" not in s.get("prompt", ""):
            errors.append(f"{s['name']}: prompt never references {{task}}")

    return errors


def check_repo(path: Path) -> list:
    """snapshot(), transition() and run_human() all assume a git repo with at
    least one commit. Find that out here, not three minutes into a paid run."""
    if not path.is_dir():
        return [f"{path} is not a directory"]
    if subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                      capture_output=True, check=False).returncode:
        return [f"{path} is not a git repository"]
    if subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=path,
                      capture_output=True, check=False).returncode:
        return [f"{path} has no commits yet - commit something first"]
    return []


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def now() -> dt.datetime:
    """Local time, but tz-aware. A bare datetime.now() writes timestamps into
    state.json that can't be compared across a DST boundary or a machine."""
    return dt.datetime.now().astimezone()


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def scrub(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def save_state(state: dict, run_dir: Path) -> None:
    """Written after EVERY transition. This is what makes runs resumable and
    hand-editable: kill the process, edit state.json, restart."""
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))


def migrate(state: dict) -> dict:
    """Fill in keys added after a run was started, so `resume` and `show`
    keep working on state.json files written by an older adw.py."""
    ws = state["workspace"]
    state.setdefault("stream_types", {})
    state.setdefault("origin_repo", ws)
    state.setdefault("head", head(ws))
    state.setdefault("base", state["head"])
    return state


def transition(state: dict, to_step: str, why: str, run_dir: Path) -> None:
    state["history"].append({
        "at": stamp(),
        "from": state["step"],
        "to": to_step,
        "why": why,
        # Cached by snapshot() rather than shelling out to git on every
        # transition - three gates in the same second used to mean three
        # identical rev-parse calls.
        "commit": state.get("head", ""),
        "cost_usd": state["cost_usd"],
    })
    state["step"] = to_step
    save_state(state, run_dir)
    print(f"  -> {to_step}  ({why})")


# ---------------------------------------------------------------------------
# GIT
# ---------------------------------------------------------------------------

def git(cwd, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False)
    return proc.stdout


def head(cwd) -> str:
    return git(cwd, "rev-parse", "--short", "HEAD").strip()


def dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain").strip())


def snapshot(state: dict, label: str) -> str:
    """Commit whatever the agent just did. Makes run history == git history,
    so you can diff attempt 2 against attempt 3.

    Minus the worker's own litter: aider writes .aider.chat.history.md and a
    tags cache into the repo, and a plain `git add -A` commits all of it. The
    human gate then shows four changed files of which one is the actual work.
    """
    ws = state["workspace"]
    litter = ARTIFACTS + WORKERS[state["worker"]].get("artifacts", [])
    excludes = [f":(exclude){p}" for p in litter]
    git(ws, "add", "-A", "--", ".", *excludes)
    if not git(ws, "diff", "--cached", "--name-only").strip():
        return ""
    git(ws, "commit", "-m", f"adw: {label}", "--no-verify")
    state["head"] = head(ws)
    return state["head"]


def drop_worktree(state: dict, keep: bool) -> None:
    """Remove the checkout after a clean run. The BRANCH survives - the
    commits are the deliverable, the working directory is scratch space.
    A failed run keeps its checkout, because that's when you want to poke."""
    if not state.get("branch"):
        return
    if keep or state["status"] != "done":
        print(f"  worktree kept at {state['workspace']}  (branch {state['branch']})")
        return
    proc = subprocess.run(["git", "worktree", "remove", state["workspace"]],
                          cwd=state["origin_repo"], capture_output=True, text=True,
                          check=False)
    if proc.returncode:
        print(f"  worktree NOT removed: {proc.stderr.strip()[:160]}")
    else:
        print(f"  worktree removed - work is on branch {state['branch']}")


# ---------------------------------------------------------------------------
# THE NONDETERMINISTIC WORKER
# ---------------------------------------------------------------------------

def run_agent(prompt: str, state: dict, run_dir: Path, limits: dict) -> dict:
    """Run one attempt. Returns the worker's final `result` event ({} if the
    worker doesn't emit one), so the orchestrator can tell a clean stop from
    a crash."""
    spec = WORKERS[state["worker"]]

    # Never let one attempt outspend what's left of the whole run.
    cap = min(limits["max_attempt_cost"],
              max(0.0, limits["max_cost"] - state["cost_usd"]))
    if not any("{budget}" in a for a in spec["cmd"]):
        print(f"     [warn] worker {state['worker']!r} has no per-attempt cost cap")
    # Substitute the trusted value first: the prompt carries gate output, and
    # gate output is arbitrary text that could itself contain "{budget}".
    cmd = [a.replace("{budget}", f"{cap:.4f}") for a in spec["cmd"]]
    cmd = [a.replace("{prompt}", prompt) for a in cmd]

    result = {}
    with (run_dir / "events.jsonl").open("a") as events:
        events.write(json.dumps({
            "type": "adw.agent_start",
            "at": stamp(),
            "step": state["step"],
            "budget_usd": round(cap, 4),
            "prompt": scrub(prompt),
        }) + "\n")

        proc = subprocess.Popen(
            cmd, cwd=state["workspace"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            # The worker must not share our stdin: claude waits 3s for input
            # it will never use, and would happily swallow the y/n that the
            # human gate is about to ask for.
            stdin=subprocess.DEVNULL,
        )

        # `for line in proc.stdout` blocks in the iterator, so the old
        # proc.wait(timeout=...) after the loop could never fire and a silent
        # worker hung forever. Poll with a deadline instead.
        deadline = time.time() + AGENT_TIMEOUT
        while True:
            left = deadline - time.time()
            if left <= 0:
                proc.kill()
                raise RuntimeError(f"worker exceeded AGENT_TIMEOUT ({AGENT_TIMEOUT}s)")
            if not select.select([proc.stdout], [], [], min(left, 5.0))[0]:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            events.write(scrub(line if line.endswith("\n") else line + "\n"))
            events.flush()
            if spec["stream"] == "claude-json":
                event = _narrate(line, state, run_dir)
                if event.get("type") == "result":
                    result = event
            else:
                print(f"     {line.rstrip()[:100]}")
        proc.wait()

    # A worker that reported why it stopped isn't a crash - the caller decides
    # what to do about it. Only an unexplained non-zero exit is fatal.
    if proc.returncode != 0 and not result:
        raise RuntimeError(f"worker exited {proc.returncode} - see {run_dir}/events.jsonl")
    return result


def _narrate(line: str, state: dict, run_dir: Path) -> dict:
    """One-line summary of interesting events; accumulate cost.

    Returns the parsed event, or {} if the line wasn't JSON. Unparseable
    lines go to stream-errors.log instead of vanishing: when a flag or CLI
    version changes shape, the symptom is an empty timeline and no error,
    and that is miserable to debug from nothing.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError as exc:
        with (run_dir / "stream-errors.log").open("a") as f:
            f.write(f"{stamp()} {exc}\n"
                    f"    {scrub(line.rstrip())[:500]}\n")
        return {}

    kind = event.get("type", "?")
    # A tally of what the worker actually emitted. If `show` reports 40
    # events and zero of them are 'assistant', your parser and your CLI
    # disagree - that's the mismatch this exists to make visible.
    state["stream_types"][kind] = state["stream_types"].get(kind, 0) + 1

    if kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                print(f"     [tool] {block.get('name')}")
    elif kind == "result":
        cost = event.get("total_cost_usd") or 0
        state["cost_usd"] = round(state.get("cost_usd", 0) + cost, 4)
        note = f"  [{event['subtype']}]" if event.get("is_error") else ""
        print(f"     [done] ${cost:.4f}  (total ${state['cost_usd']:.4f}){note}")
    return event


# ---------------------------------------------------------------------------
# THE DETERMINISTIC GATE
# ---------------------------------------------------------------------------

def run_gate(step: dict, state: dict, run_dir: Path, attempt: int):
    """Returns (passed, excerpt). The agent cannot route around this and
    cannot argue with the exit code. That is where reliability comes from."""
    # {base} is the commit the run started from, so a reviewing gate sees the
    # whole run rather than only the most recent attempt.
    cmd = [a.replace("{base}", state.get("base", "HEAD~1")) for a in step["cmd"]]
    print(f"  [gate] {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=state["workspace"],
                              capture_output=True, text=True,
                              timeout=GATE_TIMEOUT, check=False)
        out, passed = proc.stdout + proc.stderr, proc.returncode == 0
    except subprocess.TimeoutExpired:
        out, passed = f"timed out after {GATE_TIMEOUT}s", False

    # An inverted gate passes when the command FAILS. The one use worth having
    # is red/green: prove the new test actually fails before implementing
    # against it, so a vacuously-passing test can't sneak through.
    if step.get("invert"):
        passed = not passed

    out = scrub(out)
    (run_dir / "gates" / f"{step['name']}.{attempt}.txt").write_text(out)
    note = " (inverted)" if step.get("invert") else ""
    print(f"     {'PASS' if passed else 'FAIL'}{note}"
          f"  -> gates/{step['name']}.{attempt}.txt")

    # Head AND tail: pytest puts the summary at the end, the first traceback
    # near the top, and the middle is usually noise.
    excerpt = out if len(out) <= GATE_HEAD + GATE_TAIL else (
        out[:GATE_HEAD] + "\n\n[... trimmed ...]\n\n" + out[-GATE_TAIL:]
    )
    return passed, excerpt


def run_human(state: dict, limits: dict) -> str:
    """The Approve / Reject node. This is what makes it supervised.

    Returns "approve", "reject", or "park". Diffs the whole run (base..HEAD),
    not HEAD~1: one attempt is rarely the interesting unit, and HEAD~1 explodes
    on a repo with a single commit.
    """
    print("\n" + git(state["workspace"], "diff", "--stat", f"{state['base']}..HEAD"))

    if limits["auto_approve"]:
        print("  [human] --auto-approve: accepted unreviewed")
        return "approve"
    if limits["noninteractive"] or not sys.stdin.isatty():
        # No human at the keyboard. Do NOT silently reject (that turns the
        # supervision gate into a rubber stamp of "no") and do NOT block. Park:
        # record the pending decision, exit clean, and let the butler notify a
        # human who answers with `resume --approve` / `resume --reject`.
        print("  [human] no terminal attached - parking for review.")
        return "park"

    print("  [human] approve? (y = accept, n = reject, d = show full diff)")
    while True:
        try:
            answer = input("  > ").strip().lower()
        except EOFError:
            print("\n  [human] stdin closed - parking for review.")
            return "park"
        if answer == "d":
            print(git(state["workspace"], "diff", f"{state['base']}..HEAD"))
        elif answer in ("y", "n"):
            return "approve" if answer == "y" else "reject"


# ---------------------------------------------------------------------------
# THE ORCHESTRATOR
#
# A pointer into a list, a dict of attempt counts, and a loop.
# Everything above is plumbing; this is the control flow.
# ---------------------------------------------------------------------------

def orchestrate(state: dict, steps: list, run_dir: Path, limits: dict) -> dict:
    by_name = {s["name"]: s for s in steps}
    order = [s["name"] for s in steps]
    started = time.time()

    while state["status"] == "running":
        # --- circuit breakers, checked before every step -------------------
        if state["cost_usd"] > limits["max_cost"]:
            return _stop(state, run_dir, "over budget "
                         f"(${state['cost_usd']:.2f} > ${limits['max_cost']:.2f})")
        if time.time() - started > limits["max_wall"]:
            return _stop(state, run_dir, "over wall-clock limit")

        step = by_name[state["step"]]
        # Counts every execution of this step, for the whole run. It used to
        # be reset when a gate looped back, which made the log claim "build
        # attempt 1" three times over and left `show` unable to tell you how
        # often the agent actually ran.
        n = state["attempts"].get(step["name"], 0) + 1
        state["attempts"][step["name"]] = n
        save_state(state, run_dir)
        print(f"\n[{step['name']}] attempt {n}")

        if step["kind"] == "agent":
            prompt = step["prompt"].format(task=state["task"])
            if state["feedback"]:
                # THE important line. A retry is not a re-roll of the same
                # prompt - it is the same prompt plus the reason it failed.
                prompt += f"\n\nThe previous attempt failed:\n{state['feedback']}"
                state["feedback"] = None
            try:
                result = run_agent(prompt, state, run_dir, limits)
            except (RuntimeError, OSError) as exc:
                # A worker that won't start or won't finish is a stop, not a
                # traceback: state.json must not be left saying "running".
                snapshot(state, f"{step['name']} attempt {n} (worker failed)")
                return _stop(state, run_dir, f"worker failed: {exc}")
            # Commit before deciding anything: a worker that stopped early
            # still leaves work on disk, and unattributed changes are worse
            # than a commit labelled with the attempt that made them.
            sha = snapshot(state, f"{step['name']} attempt {n}")
            print(f"     [commit] {sha or 'no changes'}")
            if result.get("is_error"):
                return _stop(state, run_dir,
                             f"worker stopped: {result.get('subtype', 'unknown')}")
            _advance(state, step, order, run_dir)

        elif step["kind"] == "gate":
            passed, excerpt = run_gate(step, state, run_dir, n)
            digest = hashlib.sha1(excerpt.encode()).hexdigest()[:12]

            if passed:
                _advance(state, step, order, run_dir)
            elif step.get("on_fail") is None:
                return _stop(state, run_dir, f"{step['name']} failed (no retry path)")
            elif state["gate_hashes"].get(step["name"]) == digest:
                # Identical failure twice running: the agent is oscillating,
                # not converging. Burning the remaining attempts costs money
                # and buys nothing.
                return _stop(state, run_dir, f"{step['name']} failed identically twice")
            elif n >= step["max"]:
                return _stop(state, run_dir, f"{step['name']} exhausted {n} attempts")
            else:
                state["gate_hashes"][step["name"]] = digest
                state["feedback"] = excerpt
                transition(state, step["on_fail"], f"{step['name']} failed", run_dir)

        else:  # human
            decision = run_human(state, limits)
            if decision == "approve":
                _advance(state, step, order, run_dir)
            elif decision == "reject":
                _reject(state, steps, run_dir, f"rejected at {step['name']}")
            else:  # park - a human decides out-of-band
                return _park(state, run_dir, f"awaiting review at '{step['name']}'",
                             {"actions": ["approve", "reject"], "step": step["name"]})

    return state


def _advance(state, step, order, run_dir) -> None:
    i = order.index(step["name"])
    if i + 1 < len(order):
        transition(state, order[i + 1], f"{step['name']} ok", run_dir)
    else:
        state["status"] = "done"
        # A resumed run that recovers must stop advertising why it once
        # stopped, or `show` reports "done  over budget" forever.
        state.pop("stopped_because", None)
        state["ended_at"] = stamp()
        save_state(state, run_dir)
        print("\nDONE")


def _park(state, run_dir, why, pending) -> dict:
    """Every non-green ending lands here: a clean stop with a reason and the
    concrete decision that would unblock it, recorded in state.json. The run is
    resumable; the butler reads `stopped_because` + `pending` to ask a human."""
    state["status"] = "parked"
    state["stopped_because"] = why
    state["pending"] = pending
    state["ended_at"] = stamp()
    save_state(state, run_dir)
    print(f"\nPARKED: {why}  (actions: {', '.join(pending['actions'])})")
    return state


def _reject(state, steps, run_dir, reason) -> None:
    """Route a rejection back to the first agent with the reason as feedback -
    the very same mechanism a failing gate uses to make the agent try again."""
    first_agent = next((s["name"] for s in steps if s["kind"] == "agent"), None)
    state["feedback"] = reason
    state["gate_hashes"] = {}
    transition(state, first_agent, reason, run_dir)


def _stop(state, run_dir, why) -> dict:
    """A breaker/gate/worker stop. Unified with human parks: it is a park whose
    actions are resume (try again) or reject (send it back with a reason)."""
    return _park(state, run_dir, why, {"actions": ["resume", "reject"]})


# ---------------------------------------------------------------------------
# INTROSPECTION
# ---------------------------------------------------------------------------

def cmd_list(_) -> int:
    if not INDEX.exists():
        print("no runs yet")
        return 0
    for line in INDEX.read_text().splitlines():
        r = json.loads(line)
        mark = {"done": "ok  ", "failed": "FAIL",
                "parked": "PARK"}.get(r["status"], "??  ")
        print(f"{mark} ${r['cost_usd']:>6.2f}  {r['run']:<44} {r['task'][:50]}")
    return 0


def cmd_show(args) -> int:
    """Reconstruct a past run from what it left on disk."""
    run_dir = Path(args.run)
    state = json.loads((run_dir / "state.json").read_text())

    print(f"task     {state['task']}")
    print(f"status   {state['status']}  {state.get('stopped_because', '')}")
    if state.get("pending"):
        print(f"pending  resume with: {' / '.join(state['pending'].get('actions', []))}")
    print(f"worker   {state['worker']}   workflow {state['workflow']}")
    print(f"cost     ${state['cost_usd']:.4f}")
    print(f"branch   {state.get('branch') or '-'}")
    print("attempts " + "  ".join(f"{k}={v}" for k, v in state["attempts"].items()))
    if state.get("stream_types"):
        print("stream   " + "  ".join(f"{k}={v}"
                                      for k, v in state["stream_types"].items()))
    errs = run_dir / "stream-errors.log"
    if errs.exists():
        n = sum(1 for line in errs.read_text().splitlines() if line[:4].isdigit())
        print(f"WARNING  {n} unparseable worker line(s) -> {errs}")
    print()

    print("timeline")
    for h in state["history"]:
        print(f"  {h['at']}  {h['from']:>10} -> {h['to']:<10} "
              f"{h['commit']:<9} ${h['cost_usd']:.3f}  {h['why']}")

    print("\ngate results")
    for log in sorted((run_dir / "gates").glob("*.txt")):
        first = next((l for l in log.read_text().splitlines() if l.strip()), "")
        print(f"  {log.name:<24} {first[:70]}")

    if args.tools:
        print("\ntool calls")
        for line in (run_dir / "events.jsonl").read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "assistant":
                for b in e.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        arg = str(b.get("input", {}))[:60]
                        print(f"  {b['name']:<12} {arg}")

    ws, base = state["workspace"], state.get("base", "HEAD")
    print(f"\n  diff:    git -C {ws} log --oneline {base}..HEAD")
    print(f"  compare: git -C {ws} diff <sha1> <sha2>")
    return 0


def cmd_graph(args) -> int:
    """Your workflow as a mermaid diagram - boxes for agents, diamonds for gates."""
    steps = WORKFLOWS[args.workflow]
    print("flowchart LR")
    for i, s in enumerate(steps):
        shape = (f'  {s["name"]}[{s["name"]}]' if s["kind"] == "agent"
                 else f'  {s["name"]}{{{s["name"]}}}' if s["kind"] == "gate"
                 else f'  {s["name"]}(({s["name"]}))')
        print(shape)
        if i + 1 < len(steps):
            label = "|pass|" if s["kind"] == "gate" else ""
            print(f'  {s["name"]} -->{label} {steps[i + 1]["name"]}')
        if s.get("on_fail"):
            print(f'  {s["name"]} -->|fail| {s["on_fail"]}')
    return 0


# ---------------------------------------------------------------------------
# ENTRY POINTS
# ---------------------------------------------------------------------------

def _limits(args) -> dict:
    return {
        "max_cost": args.max_cost,
        "max_wall": args.max_wall * 60,
        "max_attempt_cost": args.max_attempt_cost,
        "auto_approve": args.auto_approve,
        # Park human gates instead of prompting, even with a tty attached. The
        # butler drives runs this way so nothing ever blocks on stdin.
        "noninteractive": getattr(args, "async_", False),
    }


def cmd_run(args) -> int:
    repo = args.repo.expanduser().resolve()
    steps = WORKFLOWS[args.workflow]

    errors = validate(steps, args.worker) + check_repo(repo)
    if errors:
        print("flow is invalid:")
        for e in errors:
            print(f"  - {e}")
        return 2

    if dirty(repo) and not args.force:
        print("working tree is dirty. commit, stash, or pass --force.")
        return 2

    started = now().strftime("%Y-%m-%dT%H-%M-%S")
    slug = "".join(c if c.isalnum() else "-" for c in args.task.lower())[:40].strip("-")
    run_dir = RUNS_DIR / f"{started}_{slug}"
    (run_dir / "gates").mkdir(parents=True, exist_ok=True)

    workspace, branch = repo, None
    if args.worktree:
        branch = f"adw/{run_dir.name}"
        workspace = repo.parent / f"{repo.name}-{run_dir.name}"
        subprocess.run(["git", "worktree", "add", "-b", branch, str(workspace)],
                       cwd=repo, check=True)

    state = {
        "run": run_dir.name, "task": args.task, "workflow": args.workflow,
        "worker": args.worker, "workspace": str(workspace), "branch": branch,
        "origin_repo": str(repo), "base": head(workspace), "head": head(workspace),
        "step": steps[0]["name"], "status": "running", "attempts": {},
        "gate_hashes": {}, "cost_usd": 0.0, "feedback": None, "stream_types": {},
        "started_at": stamp(),
        "history": [],
    }
    print(f"run: {run_dir}\nworkspace: {workspace}\nbase: {state['base']}")
    save_state(state, run_dir)

    state = orchestrate(state, steps, run_dir, _limits(args))
    _index(state)
    drop_worktree(state, args.keep_worktree)
    print(f"\n${state['cost_usd']:.4f}   adw.py show {run_dir}")
    return exit_code(state)


def cmd_resume(args) -> int:
    """Continue a stopped run - or rewind and re-execute forward.

    Note: rerunning is NOT replay. The worker is nondeterministic, so you get
    a different run to compare against the first, not the same one back.
    """
    run_dir = Path(args.run)
    state = migrate(json.loads((run_dir / "state.json").read_text()))
    steps = WORKFLOWS[state["workflow"]]

    errors = check_repo(Path(state["workspace"]))
    if errors:
        print("cannot resume:")
        for e in errors:
            print(f"  - {e}")
        return 2

    by = {s["name"]: s for s in steps}
    order = [s["name"] for s in steps]
    state["status"] = "running"
    state.pop("stopped_because", None)
    state.pop("pending", None)

    if args.approve or args.reject:
        # Answering a parked human review out-of-band (what the butler sends).
        cur = state["step"]
        if args.approve:
            if by.get(cur, {}).get("kind") != "human":
                print(f"--approve only applies at a human review step "
                      f"(this run is parked at {cur!r})")
                return 2
            _advance(state, by[cur], order, run_dir)
        else:  # --reject "<reason>": send it back to the agent with the reason
            _reject(state, steps, run_dir, f"rejected: {args.reject}")
    elif args.from_step:
        if args.from_step not in order:
            print(f"no step named {args.from_step!r}")
            return 2
        state["step"] = args.from_step
        state["attempts"][args.from_step] = 0
        state["gate_hashes"] = {}
    save_state(state, run_dir)

    print(f"resuming at [{state['step']}] with ${state['cost_usd']:.4f} already spent")
    state = orchestrate(state, steps, run_dir, _limits(args))
    _index(state)
    drop_worktree(state, args.keep_worktree)
    return exit_code(state)


def _index(state: dict) -> None:
    """One row per run, newest state wins. Append-only would list a resumed
    run once per resume, each with a different verdict."""
    RUNS_DIR.mkdir(exist_ok=True)
    row = {k: state[k] for k in ("run", "task", "status", "cost_usd", "workflow")}
    rows = []
    if INDEX.exists():
        rows = [json.loads(l) for l in INDEX.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["run"] != row["run"]] + [row]
    INDEX.write_text("".join(json.dumps(r) + "\n" for r in rows))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def budget(p):
        p.add_argument("--max-cost", type=float, default=5.0, help="USD ceiling")
        p.add_argument("--max-wall", type=float, default=30, help="minutes")
        p.add_argument("--max-attempt-cost", type=float, default=1.0,
                       help="USD ceiling for a single agent attempt")
        p.add_argument("--auto-approve", action="store_true",
                       help="accept human gates unreviewed (unattended runs)")
        p.add_argument("--async", dest="async_", action="store_true",
                       help="never block on a human gate - park for out-of-band "
                            "review (how the butler runs it)")
        p.add_argument("--keep-worktree", action="store_true",
                       help="keep the worktree checkout after a clean run")

    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--workflow", default="feature", choices=list(WORKFLOWS))
    r.add_argument("--worker", default="claude", choices=list(WORKERS))
    r.add_argument("--repo", default=".", type=Path)
    r.add_argument("--worktree", action="store_true")
    r.add_argument("--force", action="store_true", help="allow a dirty tree")
    budget(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume")
    s.add_argument("run")
    s.add_argument("--from", dest="from_step", help="rewind to this step first")
    s.add_argument("--approve", action="store_true",
                   help="approve a parked human review and continue")
    s.add_argument("--reject", metavar="REASON",
                   help="reject a parked review; REASON is fed back to the agent")
    budget(s)
    s.set_defaults(func=cmd_resume)

    w = sub.add_parser("show")
    w.add_argument("run")
    w.add_argument("--tools", action="store_true", help="include tool calls")
    w.set_defaults(func=cmd_show)

    sub.add_parser("list").set_defaults(func=cmd_list)

    g = sub.add_parser("graph")
    g.add_argument("workflow", choices=list(WORKFLOWS))
    g.set_defaults(func=cmd_graph)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
