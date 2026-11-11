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
import copy
import datetime as dt
import hashlib
import json
import os
import re
import select
import shlex
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

# A plan step asks the worker to write this file (in the workspace, its cwd);
# the orchestrator reads it, copies it into the run dir for the record, and
# deletes it before snapshotting so it never lands in the review diff.
PLAN_FILE = "plan.json"

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
    # Claude Code on a Max/Pro SUBSCRIPTION login - no ANTHROPIC_API_KEY. `claude
    # -p` uses whatever auth is configured, so requires_env is empty. There is no
    # {budget}: a subscription is not dollar-metered, so --max-budget-usd does not
    # apply; run_agent warns once and runs uncapped, bounded by plan usage and by
    # the turn/wall ceilings instead (see --max-turns). --output-format json emits
    # one result event _narrate reads. --dangerously-skip-permissions is required:
    # -p has no TTY to answer prompts, and containment is the worktree + snapshot
    # + gate + human-review diff, not the permission prompt. Pairs with the
    # rate-limit park (a subscription limit must park, not retry into the wall).
    "claude-code": {
        "cmd": [
            "claude", "-p", "--dangerously-skip-permissions",
            "--output-format", "json", "{prompt}",
        ],
        "stream": "claude-json",
        "requires_env": [],
    },
    # Costs nothing, never varies. Lets you exercise the retry loop, the
    # breakers and the counters without an LLM in the way. See demo/.
    "demo": {
        "cmd": [sys.executable, str(HERE / "demo/worker.py"), "{prompt}"],
        "stream": "claude-json",
    },
    # Like demo, but it also writes a valid plan.json when asked to plan, so the
    # plan step and its splice can be exercised offline. See demo/plan_worker.py.
    "demo-plan": {
        "cmd": [sys.executable, str(HERE / "demo/plan_worker.py"), "{prompt}"],
        "stream": "claude-json",
    },
}


# ---------------------------------------------------------------------------
# WORKFLOWS - this list IS the diagram. Boxes are agents, diamonds are gates.
# ---------------------------------------------------------------------------

WORKFLOWS = {
    # The long-horizon flow: pin acceptance tests at an interview, decompose into
    # gated milestones at a plan step (which splices them in, ending in a human
    # review), and let the gates - not the agent - decide "done". Keep each agent
    # step a fresh worker burst; the harness (runs/ + resume) carries length.
    "long": [
        {"kind": "interview", "name": "interview",
         "prompt": "{task}\n\nInterview me until the acceptance criteria are "
                   "unambiguous, then write them as runnable tests (pytest, a "
                   "panedrive .pds, or a simharness config - whatever the deep "
                   "gate for this project enforces). Do not implement."},
        {"kind": "plan", "name": "plan", "max": 2,
         "prompt": "{task}\n\nGiven the committed acceptance tests, break this "
                   "into ordered milestones. Write plan.json: a JSON list of "
                   "{{id, prompt, gate}} where gate is an argv the milestone "
                   "must pass. Do not implement yet."},
        # the plan step splices gated milestones here, ending in a human review.
    ],
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
    # DEEP GATES (gli #9). The loop is only as honest as the gate is deep: a
    # shallow gate certifies a mock. These drive the project for real.
    #
    # tui: a real browser for terminal UIs. panedrive presses real keys into the
    # app in a PTY and asserts on its JSON state seam. The gate runs with
    # cwd=workspace; panedrive's PTY child starts in $HOME, so the convention is
    # $PWD-absolute paths (app.py + acceptance.pds + the .adw-tui.state.json seam
    # in the repo root). Structured --json output feeds the thrash detector.
    "tui": [
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nImplement this terminal UI. Expose a paneview JSON "
                   "state seam and keep acceptance.pds passing."},
        {"kind": "gate", "name": "e2e", "on_fail": "build", "max": 3,
         "cmd": ["sh", "-c",
                 ('panedrive run "$PWD/acceptance.pds" --backend pty --json '
                  '--state "$PWD/.adw-tui.state.json" -- '
                  'python3 "$PWD/app.py" "$PWD/.adw-tui.state.json"')]},
        {"kind": "human", "name": "review"},
    ],
    # sim: a deterministic, seeded simulation gate. `simharness assert` reads the
    # project's simharness.config.ts and exits 0/1; its --json names the broken
    # invariant, which the thrash detector reads as a shrinking signature.
    "sim": [
        {"kind": "agent", "name": "build",
         "prompt": "{task}\n\nImplement this against the SimAdapter so "
                   "simharness assert passes."},
        {"kind": "gate", "name": "sim",
         "cmd": ["simharness", "assert"], "on_fail": "build", "max": 3},
        {"kind": "human", "name": "review"},
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
    # Interview first (writes acceptance tests), then a gate that enforces they
    # exist. Drive the TTY with ADW_INTERVIEW_CMD=python3 demo/interviewer_stub.py.
    "demo-interview": [
        {"kind": "interview", "name": "interview",
         "prompt": "{task}\n\nInterview me, then write acceptance tests."},
        {"kind": "gate", "name": "accept",
         "cmd": ["test", "-f", "test_accept.py"], "on_fail": None, "max": 1},
        {"kind": "human", "name": "review"},
    ],
    # Fails differently every attempt but never closer - the thrash detector
    # should park it with "not converging" well before `max`.
    "demo-thrash": [
        {"kind": "agent", "name": "build", "prompt": "{task}"},
        {"kind": "gate", "name": "check",
         "cmd": [sys.executable, str(HERE / "demo/gate_thrash.py")],
         "on_fail": "build", "max": 8, "thrash_window": 2},
    ],
    # Genuinely converges (3->2->1->pass): the detector must NOT trip it.
    "demo-converge": [
        {"kind": "agent", "name": "build", "prompt": "{task}"},
        {"kind": "gate", "name": "check",
         "cmd": [sys.executable, str(HERE / "demo/gate_converge.py")],
         "on_fail": "build", "max": 8, "thrash_window": 2},
        {"kind": "human", "name": "review"},
    ],
    # A plan step and nothing else: it emits gated milestones that get spliced in
    # and run, ending in a human review. Run with --worker demo-plan, offline.
    "demo-plan": [
        {"kind": "plan", "name": "plan", "max": 2,
         "prompt": "{task}\n\nBreak this into ordered milestones. Write "
                   "plan.json: a JSON list of {{id, prompt, gate}} where gate "
                   "is an argv the milestone must pass. Do not implement yet."},
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
        if kind not in ("agent", "gate", "human", "plan", "interview"):
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
        # A plan step is an agent that emits steps; it carries {task} like any
        # agent prompt. An interview step (human + agent at a TTY) does too.
        if kind in ("agent", "plan", "interview") and "{task}" not in s.get("prompt", ""):
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


def write_seam(state: dict) -> None:
    """Expose a paneview JSON state seam (scalar leaves only) so panedrive can
    drive and assert ADW itself headlessly - the same deterministic verification
    ADW gives every other TUI. A no-op unless $PANEDRIVE_STATE/$ADW_SEAM is set,
    so normal runs pay nothing. Any error here must never break a run.
    """
    path = os.environ.get("ADW_SEAM") or os.environ.get("PANEDRIVE_STATE")
    if not path:
        return
    pending = state.get("pending") or {}
    seam = {
        "status": state.get("status", ""),
        "step": state.get("step", ""),
        "cost_usd": state.get("cost_usd", 0.0),
        "turns": state.get("turns", 0),
        "stopped_because": state.get("stopped_because", ""),
        # kept scalar (comma-joined) so the seam is a flat object of scalars.
        "pending_actions": ",".join(pending.get("actions", [])),
    }
    try:
        p = Path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(seam))
        os.replace(tmp, p)                   # atomic, per the seam contract
    except OSError:
        pass


def save_state(state: dict, run_dir: Path) -> None:
    """Written after EVERY transition. This is what makes runs resumable and
    hand-editable: kill the process, edit state.json, restart."""
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))
    write_seam(state)


def migrate(state: dict) -> dict:
    """Fill in keys added after a run was started, so `resume` and `show`
    keep working on state.json files written by an older adw.py."""
    ws = state["workspace"]
    state.setdefault("stream_types", {})
    state.setdefault("origin_repo", ws)
    state.setdefault("head", head(ws))
    state.setdefault("base", state["head"])
    state.setdefault("turns", 0)
    state.setdefault("thrash", {})
    # The effective step list is the source of truth for resume. Older runs
    # (and runs that never spliced) are backfilled from the static template, so
    # a resume always has a list that contains state["step"] - including any
    # steps a plan step spliced in (see expand_milestones / orchestrate).
    state.setdefault("steps", copy.deepcopy(WORKFLOWS.get(state["workflow"], [])))
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

# The distinctive line Claude Code prints when a Max/Pro subscription hits its
# usage limit, optionally followed by |<reset-epoch>. Keyed on the FULL phrase so
# a worker that is merely *building* a rate-limiter never trips this.
USAGE_LIMIT_RE = re.compile(r"Claude AI usage limit reached(?:\|(\d+))?", re.IGNORECASE)


def rate_limit_reset(text: str):
    """(hit, reset_epoch|None) for the subscription usage-limit signal in text."""
    m = USAGE_LIMIT_RE.search(text or "")
    if m:
        return True, (int(m.group(1)) if m.group(1) else None)
    return False, None


def result_rate_limited(result: dict):
    """A usage/rate limit surfaced in a parsed result event (subscription or a
    429 under an API key). Returns (hit, reset_epoch|None)."""
    if not isinstance(result, dict):
        return False, None
    sub = str(result.get("subtype", "")).lower()
    if "usage_limit" in sub or "rate_limit" in sub or "rate limit" in sub:
        return True, None
    api = result.get("api_error_status")
    if api in (429, "429") or (isinstance(api, str) and "limit" in api.lower()):
        return True, None
    return rate_limit_reset(str(result.get("result", "")))


def rate_limit_reason(result: dict) -> str:
    reset = result.get("reset_at")
    when = ""
    if reset:
        when = (" - resume after "
                + dt.datetime.fromtimestamp(reset).astimezone().isoformat(timespec="minutes"))
    return "worker hit a usage/rate limit; parked without retry" + when


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
    rl_hit, rl_reset = False, None
    turns_seen = 0                               # assistant turns, for workers
    with (run_dir / "events.jsonl").open("a") as events:  # that report no count
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
            if not rl_hit:                       # earliest reset epoch wins
                rl_hit, rl_reset = rate_limit_reset(line)
            if spec["stream"] == "claude-json":
                event = _narrate(line, state, run_dir)
                if event.get("type") == "result":
                    result = event
                elif event.get("type") == "assistant":
                    turns_seen += 1
            else:
                print(f"     {line.rstrip()[:100]}")
        proc.wait()

    # Count model turns for the run-level ceiling. The subscription worker's
    # result reports num_turns directly; stream workers that don't get the
    # assistant-message count as a stand-in. Either way it feeds max_turns.
    state["turns"] = state.get("turns", 0) + (result.get("num_turns") or turns_seen)

    # A usage/rate limit is a clean, resumable stop - never a crash, and the
    # orchestrator must park it WITHOUT retrying into the wall. Detect it from
    # the raw stream (the subscription line) or a structured result (a 429),
    # synthesise a distinct result, and skip the crash path on a non-zero exit.
    if not rl_hit:
        rl_hit, rl_reset = result_rate_limited(result)
    if rl_hit:
        return {**result, "is_error": True, "subtype": "usage_limit",
                "rate_limited": True, "reset_at": rl_reset}

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


# A big sentinel so panedrive's "how far did it get" maps into a shrink-smaller
# scale (further progress -> smaller signature) without going negative.
_PANEDRIVE_STEPS = 1000


def _json_objects(text: str):
    """Yield dict objects from a gate's output. panedrive --json and simharness
    --json each emit a single-line JSON object, so a line-by-line parse is
    enough and never chokes on the surrounding human text."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def failure_signature(excerpt: str) -> int:
    """A numeric size of a gate failure where SMALLER == closer to passing, so a
    converging agent drives it down. Read from STRUCTURED gate output first
    (simharness/panedrive JSON, pytest summary), with a coarse line-count
    fallback. This is the signal the thrash detector requires to keep shrinking.
    """
    for obj in _json_objects(excerpt):
        if isinstance(obj.get("asserts"), list):            # simharness assert
            return sum(1 for a in obj["asserts"] if not a.get("ok", True))
        if isinstance(obj.get("breaks"), list):             # simharness fuzz
            return len(obj["breaks"])
        if "steps" in obj and "ok" in obj:                  # panedrive run
            fs = obj.get("failed_step")
            if fs is not None:
                return max(0, _PANEDRIVE_STEPS - int(fs))   # further = smaller
            return sum(1 for s in obj["steps"] if not s.get("ok", True))
    m = re.search(r"(\d+) failed", excerpt)                 # pytest-style
    if m:
        return int(m.group(1))
    return len(excerpt.splitlines())                        # coarse fallback


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
# CIRCUIT BREAKERS
# ---------------------------------------------------------------------------

def breaker(state: dict, limits: dict, started: float):
    """The run-level ceilings, checked before every step. Returns a stop reason
    or None. A run can overshoot by at most one step - the same contract the
    cost ceiling always had. max_cost bounds metered workers; max_turns is the
    ceiling that actually bites under a subscription, where cost_usd is blind.
    """
    if state["cost_usd"] > limits["max_cost"]:
        return (f"over budget (${state['cost_usd']:.2f} > "
                f"${limits['max_cost']:.2f})")
    max_turns = limits.get("max_turns")
    if max_turns is not None and state.get("turns", 0) > max_turns:
        return f"over turn ceiling ({state.get('turns', 0)} > {max_turns})"
    if time.time() - started > limits["max_wall"]:
        return "over wall-clock limit"
    return None


# ---------------------------------------------------------------------------
# DYNAMIC STEP EMISSION (the plan step)
#
# A plan step decomposes {task} into milestones and splices a gated step pair
# per milestone into the running workflow. This is the one primitive that turns
# one task into a long, gated chain. The planner's output is UNTRUSTED: it is
# run through the same validate() every static workflow passes, and a bad plan
# is treated exactly like a failed gate (feedback + bounded retries + park).
# ---------------------------------------------------------------------------

def expand_milestones(milestones: list) -> list:
    """Turn [{id, prompt, gate}, ...] into gated steps, ending in a review.

    Each milestone becomes an agent step (do.<id>) and a gate (ok.<id>) that
    routes back to that agent on failure - the identical shape a hand-written
    workflow uses, so no new orchestration machinery is needed downstream.
    Raises KeyError/TypeError on a malformed milestone; the caller treats that
    as a rejected plan.
    """
    if not isinstance(milestones, list) or not milestones:
        raise ValueError("plan must be a non-empty JSON list of milestones")
    out = []
    for m in milestones:
        mid = str(m["id"])
        gate = m["gate"]
        gate = shlex.split(gate) if isinstance(gate, str) else list(gate)
        if not gate:
            raise ValueError(f"milestone {mid!r}: empty gate")
        out += [
            {"kind": "agent", "name": f"do.{mid}",
             "prompt": m["prompt"] + "\n\nOverall goal: {task}"},
            {"kind": "gate", "name": f"ok.{mid}",
             "cmd": gate, "on_fail": f"do.{mid}", "max": 3},
        ]
    return out + [{"kind": "human", "name": "review"}]


def read_plan(state: dict, run_dir: Path) -> list:
    """Read the worker-written plan from the workspace, copy it into the run dir
    for the record, and remove it from the workspace so it is never committed.

    Returns the parsed milestones list. Raises OSError/JSONDecodeError on a
    missing or unparseable plan - the caller treats that as a rejected plan.
    """
    src = Path(state["workspace"]) / PLAN_FILE
    if not src.exists():
        raise FileNotFoundError(
            f"{PLAN_FILE} not found - the plan step must write it to the repo root")
    raw = src.read_text()
    (run_dir / PLAN_FILE).write_text(raw)   # the record show/graph can read
    src.unlink()                            # keep it out of the snapshot diff
    return json.loads(raw)


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
        tripped = breaker(state, limits, started)
        if tripped:
            return _stop(state, run_dir, tripped)

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
            if result.get("rate_limited"):
                # Park, do NOT loop back: retrying only hits the same wall.
                return _park(state, run_dir, rate_limit_reason(result),
                             {"actions": ["resume", "reject"],
                              "reset_at": result.get("reset_at")})
            if result.get("is_error"):
                return _stop(state, run_dir,
                             f"worker stopped: {result.get('subtype', 'unknown')}")
            _advance(state, step, order, run_dir)

        elif step["kind"] == "plan":
            # An agent step that EMITS steps. Same retry-with-feedback contract
            # as a normal agent; its product (plan.json) is then gated.
            prompt = step["prompt"].format(task=state["task"])
            if state["feedback"]:
                prompt += f"\n\nThe previous attempt failed:\n{state['feedback']}"
                state["feedback"] = None
            try:
                result = run_agent(prompt, state, run_dir, limits)
            except (RuntimeError, OSError) as exc:
                snapshot(state, f"{step['name']} attempt {n} (worker failed)")
                return _stop(state, run_dir, f"worker failed: {exc}")
            if result.get("rate_limited"):
                snapshot(state, f"{step['name']} attempt {n}")
                return _park(state, run_dir, rate_limit_reason(result),
                             {"actions": ["resume", "reject"],
                              "reset_at": result.get("reset_at")})
            if result.get("is_error"):
                snapshot(state, f"{step['name']} attempt {n}")
                return _stop(state, run_dir,
                             f"worker stopped: {result.get('subtype', 'unknown')}")
            # Read+move plan.json BEFORE the snapshot so it never hits the diff.
            # The plan is UNTRUSTED, so run it through the same validate() every
            # static workflow passes; a bad plan is handled like a failed gate.
            try:
                milestones = read_plan(state, run_dir)
                new_steps = expand_milestones(milestones)
                errs = validate(steps + new_steps, state["worker"])
            except (OSError, json.JSONDecodeError, KeyError,
                    TypeError, ValueError) as exc:
                milestones, new_steps, errs = None, None, [f"{PLAN_FILE} unusable: {exc}"]
            snapshot(state, f"{step['name']} attempt {n}")
            if errs:
                state["feedback"] = "plan rejected:\n" + "\n".join(errs)
                if n >= step.get("max", 2):
                    return _stop(state, run_dir,
                                 "planner could not produce a valid plan")
                continue                       # retry the plan step
            i = order.index(step["name"])
            steps[i + 1:i + 1] = new_steps     # splice into the live list...
            state["steps"] = steps             # ...which is state["steps"]
            by_name = {s["name"]: s for s in steps}
            order = [s["name"] for s in steps]
            save_state(state, run_dir)
            transition(state, new_steps[0]["name"],
                       f"{len(milestones)} milestones", run_dir)

        elif step["kind"] == "interview":
            # Pin "done" as tests BEFORE any build: a human + an interactive agent
            # co-write the acceptance criteria as runnable tests, which the normal
            # gates then enforce. Needs a real keyboard - no TTY, or --async, must
            # park (never silently skip). This is interactive (NOT -p). The
            # ADW_INTERVIEW_CMD seam lets a driver/test stand in for `claude`.
            if limits.get("noninteractive") or not sys.stdin.isatty():
                return _stop(state, run_dir,
                             "interview needs a human at the keyboard")
            cmd = shlex.split(os.environ.get("ADW_INTERVIEW_CMD", "claude"))
            cmd.append(step["prompt"].format(task=state["task"]))
            subprocess.run(cmd, cwd=state["workspace"], check=False)
            snapshot(state, "interview")         # commit the criteria for the gates
            _advance(state, step, order, run_dir)

        elif step["kind"] == "gate":
            passed, excerpt = run_gate(step, state, run_dir, n)
            digest = hashlib.sha1(excerpt.encode()).hexdigest()[:12]

            if passed:
                _advance(state, step, order, run_dir)
            elif step.get("on_fail") is None:
                return _stop(state, run_dir, f"{step['name']} failed (no retry path)")
            else:
                # Convergence check. The failure SIGNATURE (smaller = closer to
                # passing, read from structured gate output when available) must
                # keep shrinking. If it stops improving for thrash_window
                # attempts the agent is thrashing - fails differently but no
                # closer - and burning the rest of the budget buys nothing.
                sig = failure_signature(excerpt)
                th = state.setdefault("thrash", {}).setdefault(
                    step["name"], {"best": None, "stale": 0})
                if th["best"] is None or sig < th["best"]:
                    th["best"], th["stale"] = sig, 0
                else:
                    th["stale"] += 1

                if state["gate_hashes"].get(step["name"]) == digest:
                    return _stop(state, run_dir,
                                 f"{step['name']} failed identically twice")
                if th["stale"] >= step.get("thrash_window", 2):
                    return _stop(state, run_dir,
                                 f"{step['name']} not converging "
                                 f"(failure signature stuck at {th['best']})")
                if n >= step["max"]:
                    return _stop(state, run_dir,
                                 f"{step['name']} exhausted {n} attempts")
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
        state.pop("park_kind", None)
        state["ended_at"] = stamp()
        close_issue_if_any(state)                # close the sink issue, if any
        save_state(state, run_dir)
        print("\nDONE")


def _issue_sink_on() -> bool:
    """Opt-in: ADW_FILE_ISSUES=1 (set by --file-issues) and gli on PATH."""
    return bool(os.environ.get("ADW_FILE_ISSUES")) and bool(shutil.which("gli"))


def file_or_update_issue(state: dict, run_dir: Path) -> None:
    """Butler issue sink: on a park, file ONE gli issue in the target repo (or
    update the existing one on a re-park), so an unattended failure becomes a
    triaged work item. Idempotent via state["issue_id"]; never breaks a run."""
    if not _issue_sink_on():
        return
    repo = state.get("origin_repo", state["workspace"])
    hint = resume_hint(run_dir, state.get("pending", {}))
    body = (f"ADW run parked.\n\nrun: {state['run']}\nkind: "
            f"{state.get('park_kind', '?')}\nreason: {state.get('stopped_because', '')}\n"
            f"resume: {hint}\nworkflow: {state['workflow']}  worker: {state['worker']}")
    try:
        if state.get("issue_id"):
            note = (f"re-parked [{state.get('park_kind', '?')}]: "
                    f"{state.get('stopped_because', '')}\nresume: {hint}")
            subprocess.run(["gli", "comment", state["issue_id"], note],
                           cwd=repo, capture_output=True, text=True, check=False)
            return
        title = f"ADW parked: {state['task'][:50]} [{state.get('park_kind', '?')}]"
        r = subprocess.run(["gli", "create", title, "-l", "adw", "-l",
                            f"park:{state.get('park_kind', 'other')}", "-d", body, "-q"],
                           cwd=repo, capture_output=True, text=True, check=False)
        issue_id = r.stdout.strip()
        if r.returncode == 0 and issue_id:
            state["issue_id"] = issue_id
            print(f"  filed gli issue {issue_id} in {repo}")
    except OSError:
        pass


def close_issue_if_any(state: dict) -> None:
    """When a run that once parked reaches done, close its sink issue."""
    if not _issue_sink_on() or not state.get("issue_id"):
        return
    repo = state.get("origin_repo", state["workspace"])
    try:
        subprocess.run(["gli", "close", state["issue_id"], "--reason",
                        f"ADW run {state['run']} completed"],
                       cwd=repo, capture_output=True, text=True, check=False)
        print(f"  closed gli issue {state['issue_id']}")
    except OSError:
        pass


def classify_park(why: str) -> str:
    """Group a park reason into a small taxonomy, for filtering and reporting."""
    w = why.lower()
    if "rate limit" in w or "usage" in w:
        return "rate-limit"
    if "not converging" in w or "identically twice" in w:
        return "thrash"
    if "plan" in w and ("reject" in w or "valid plan" in w or "unusable" in w):
        return "bad-plan"
    if "budget" in w or "ceiling" in w or "wall-clock" in w:
        return "breaker"
    if "awaiting review" in w:
        return "review"
    if "interview needs" in w:
        return "interview"
    if "worker" in w:
        return "worker"
    return "other"


def resume_hint(run_dir: Path, pending: dict) -> str:
    """The exact copy-paste command that unblocks this park."""
    actions = pending.get("actions", [])
    flag = " --approve" if "approve" in actions else ""
    return f"adw.py resume {run_dir}{flag}"


def _park(state, run_dir, why, pending) -> dict:
    """Every non-green ending lands here: a clean stop with a reason, the concrete
    decision that would unblock it, and a copy-paste resume command, all recorded
    in state.json. The run is resumable; the butler reads stopped_because +
    pending + park_kind to ask a human (or file an issue)."""
    state["status"] = "parked"
    state["stopped_because"] = why
    state["pending"] = pending
    state["park_kind"] = classify_park(why)
    state["ended_at"] = stamp()
    file_or_update_issue(state, run_dir)         # butler sink (opt-in, may set issue_id)
    save_state(state, run_dir)
    print(f"\nPARKED [{state['park_kind']}]: {why}")
    print(f"  actions: {', '.join(pending['actions'])}")
    print(f"  resume:  {resume_hint(run_dir, pending)}")
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

def cmd_list(args) -> int:
    if not INDEX.exists():
        print("no runs yet")
        return 0
    want = getattr(args, "park_kind", None)
    for line in INDEX.read_text().splitlines():
        r = json.loads(line)
        if want and r.get("park_kind") != want:
            continue
        mark = {"done": "ok  ", "failed": "FAIL",
                "parked": "PARK"}.get(r["status"], "??  ")
        pk = f"  [{r['park_kind']}]" if r.get("park_kind") else ""
        print(f"{mark} ${r['cost_usd']:>6.2f}  {r['run']:<44} {r['task'][:40]}{pk}")
    return 0


def wall_seconds(state: dict) -> float:
    """Wall-clock seconds between started_at and ended_at (or now)."""
    try:
        a = dt.datetime.fromisoformat(state["started_at"])
        b = dt.datetime.fromisoformat(state.get("ended_at") or stamp())
        return max(0.0, (b - a).total_seconds())
    except (KeyError, ValueError):
        return 0.0


def cmd_summary(args) -> int:
    """A one-screen run summary: status, usage, steps, park + resume, diff stat.
    Distinct from the verbose `show`; built for a butler notification body."""
    run_dir = Path(args.run)
    state = migrate(json.loads((run_dir / "state.json").read_text()))
    print(f"{state['status'].upper()}  {state['run']}")
    print(f"  task     {state['task'][:70]}")
    print(f"  flow     {state['workflow']} / {state['worker']}")
    print(f"  usage    turns {state.get('turns', 0)}   "
          f"cost ${state['cost_usd']:.4f}   wall {wall_seconds(state):.0f}s")
    if state.get("attempts"):
        print("  steps    " + "  ".join(f"{k}x{v}"
                                        for k, v in state["attempts"].items()))
    if state["status"] == "parked":
        print(f"  parked   [{state.get('park_kind', '?')}] "
              f"{state.get('stopped_because', '')}")
        print(f"  resume   {resume_hint(run_dir, state.get('pending', {}))}")
    stat = git(state["workspace"], "diff", "--stat",
               f"{state.get('base', 'HEAD')}..HEAD").strip()
    if stat:
        print("  diff\n    " + stat.replace("\n", "\n    "))
    return 0


def cmd_usage(_) -> int:
    """Aggregate usage across all runs: status/park-reason counts and total
    turns - the plan-usage picture a subscription dollar figure cannot give."""
    if not INDEX.exists():
        print("no runs yet")
        return 0
    rows = [json.loads(line) for line in INDEX.read_text().splitlines() if line.strip()]
    status, parks = {}, {}
    turns = cost = 0
    for r in rows:
        status[r["status"]] = status.get(r["status"], 0) + 1
        turns += r.get("turns", 0)
        cost += r.get("cost_usd", 0)
        if r.get("park_kind"):
            parks[r["park_kind"]] = parks.get(r["park_kind"], 0) + 1
    print(f"runs     {len(rows)}   "
          + "  ".join(f"{k}={v}" for k, v in sorted(status.items())))
    print(f"turns    {turns}   cost ${cost:.2f}")
    if parks:
        print("parks    " + "  ".join(f"{k}={v}" for k, v in
                                      sorted(parks.items(), key=lambda kv: -kv[1])))
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
    # Under a subscription cost is unreliable, so turns + wall are the real
    # usage signals; keep cost too for metered workers.
    print(f"cost     ${state['cost_usd']:.4f}   turns {state.get('turns', 0)}"
          f"   wall {wall_seconds(state):.0f}s")
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


def _node_id(name: str) -> str:
    """mermaid node ids must be identifier-safe; spliced names like `do.m1` have
    dots that break the parser. Sanitise the id, keep the real name as the label."""
    return re.sub(r"\W", "_", name)


def cmd_graph(args) -> int:
    """A workflow as a mermaid diagram - boxes for agent/plan/interview, diamonds
    for gates, circles for human review. Given a RUN DIR instead of a workflow
    name, graph its persisted effective steps, so the spliced milestones of a real
    long run show up, not just the static template."""
    target = args.target
    run_state = Path(target) / "state.json"
    if run_state.exists():
        steps = migrate(json.loads(run_state.read_text()))["steps"]
        print(f"%% {Path(target).name} (effective steps)")
    elif target in WORKFLOWS:
        steps = WORKFLOWS[target]
    else:
        print(f"no workflow or run named {target!r}; "
              f"workflows: {', '.join(WORKFLOWS)}")
        return 2

    print("flowchart LR")
    for i, s in enumerate(steps):
        nid, name, kind = _node_id(s["name"]), s["name"], s["kind"]
        if kind == "gate":
            print(f'  {nid}{{{name}}}')          # diamond
        elif kind == "human":
            print(f'  {nid}(({name}))')          # circle
        else:                                     # agent, plan, interview
            print(f'  {nid}[{name}]')            # box
        if i + 1 < len(steps):
            label = "|pass|" if kind == "gate" else ""
            print(f'  {nid} -->{label} {_node_id(steps[i + 1]["name"])}')
        if s.get("on_fail"):
            print(f'  {nid} -->|fail| {_node_id(s["on_fail"])}')
    return 0


# ---------------------------------------------------------------------------
# ENTRY POINTS
# ---------------------------------------------------------------------------

def _limits(args) -> dict:
    # The issue sink reads this env (so _park/_advance need no limits plumbing).
    if getattr(args, "file_issues", False):
        os.environ["ADW_FILE_ISSUES"] = "1"
    return {
        "max_cost": args.max_cost,
        "max_wall": args.max_wall * 60,
        "max_turns": args.max_turns,
        "max_attempt_cost": args.max_attempt_cost,
        "auto_approve": args.auto_approve,
        # Park human gates instead of prompting, even with a tty attached. The
        # butler drives runs this way so nothing ever blocks on stdin.
        "noninteractive": getattr(args, "async_", False),
    }


def cmd_run(args) -> int:
    repo = args.repo.expanduser().resolve()
    # Deep-copy the template so a plan step can splice into this run's list
    # without mutating the module-level WORKFLOWS shared by other runs.
    steps = copy.deepcopy(WORKFLOWS[args.workflow])

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
        "gate_hashes": {}, "thrash": {}, "cost_usd": 0.0, "feedback": None,
        "stream_types": {}, "turns": 0,
        "started_at": stamp(),
        "history": [],
        # The effective step list, persisted so a resumed run - including one a
        # plan step has spliced new steps into - rebuilds by_name/order from
        # this rather than the static template, and never KeyErrors on resume.
        "steps": steps,
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
    # Resume from the persisted effective list, not the static template: this is
    # what carries spliced plan steps across a kill/resume (see migrate()).
    steps = state["steps"]

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
    row["turns"] = state.get("turns", 0)
    if state.get("park_kind"):
        row["park_kind"] = state["park_kind"]
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
        p.add_argument("--max-turns", type=int, default=1000,
                       help="ceiling on total worker turns - the bound that "
                            "bites under a subscription, where cost is blind")
        p.add_argument("--max-attempt-cost", type=float, default=1.0,
                       help="USD ceiling for a single agent attempt")
        p.add_argument("--auto-approve", action="store_true",
                       help="accept human gates unreviewed (unattended runs)")
        p.add_argument("--async", dest="async_", action="store_true",
                       help="never block on a human gate - park for out-of-band "
                            "review (how the butler runs it)")
        p.add_argument("--keep-worktree", action="store_true",
                       help="keep the worktree checkout after a clean run")
        p.add_argument("--file-issues", action="store_true",
                       help="file/close a gli issue in the target repo on "
                            "park/done (butler issue sink; needs gli)")

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

    sm = sub.add_parser("summary")
    sm.add_argument("run")
    sm.set_defaults(func=cmd_summary)

    sub.add_parser("usage").set_defaults(func=cmd_usage)

    lst = sub.add_parser("list")
    lst.add_argument("--park-kind", dest="park_kind",
                     help="show only parked runs of this kind "
                          "(breaker|rate-limit|thrash|bad-plan|review|worker|interview)")
    lst.set_defaults(func=cmd_list)

    g = sub.add_parser("graph")
    g.add_argument("target", help="a workflow name, or a run dir to graph its "
                                  "actual (spliced) steps")
    g.set_defaults(func=cmd_graph)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
