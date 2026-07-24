# Handover: adw.py

## What this is

`adw.py` is a personal Agentic Developer Workflow runner. One sentence:

> run a nondeterministic worker (a coding agent CLI), then a deterministic gate
> (pytest / ruff / mypy), and let the gate's exit code decide what happens next.

A workflow is a list of steps. Three kinds:

- `agent` - spawns a coding-agent CLI in the workspace. Nondeterministic.
- `gate` - runs a command. Exit 0 = advance. Non-zero = jump back to `on_fail`,
  feeding the failure output into the next agent prompt as context.
- `human` - prints the diff, waits for approve/reject.

The retry is the core idea: it is **not** a re-roll of the same prompt, it is the
same prompt plus the reason the last attempt failed.

See `README.md` for operating it. This file is the maintainer's doc.

## Design constraints - do not violate these

1. **Zero third-party dependencies.** Stdlib only. If you think you need a
   package, propose it and explain why stdlib won't do; don't just add it.
2. **Single file.** Do not split into a package. If it exceeds ~700 lines,
   say so and ask before restructuring. *(Currently ~894 - see Open questions.)*
3. **The worker contract is a subprocess.** A worker is a CLI that edits files
   in a directory. That's it. Do NOT introduce a plugin API, base classes, or
   an SDK abstraction. Adding a worker means adding a dict entry to `WORKERS`.
4. **Do not add a workflow framework.** No LangGraph, no Prefect, no Temporal,
   no CrewAI. The orchestrator is a `while` loop over a list with a pointer.
   This was decided deliberately. If you think the control flow has outgrown it,
   say so in prose - do not act on it.
5. **State is plain JSON.** Everything in `state` must be serializable. No
   subprocess handles, no open files, no iterators.
6. **Every state transition writes `state.json`.** That's what makes runs
   resumable and hand-editable.
7. **Readability over cleverness.** This file is a map of the author's own
   system; they need to understand it at a glance in six months.

## Status

Subcommands: `run`, `resume`, `show`, `list`, `graph`.

**Task 1 (verify it runs) - done.** Verified end-to-end on real repos against
the `claude` worker and against a local OpenAI-compatible endpoint via `aider`.
Gate failure, feedback, recovery, per-attempt commits, resume, and `show` all
confirmed working. The flags and the stream-json event shape in the original
were already correct; nothing needed fixing there.

**Task 2 (cleanup and hardening) - done.** Plus four issues found during Task 1:

- `--allowedTools` never restricted anything - it only pre-approves. The worker
  kept full Bash. Now passes `--tools` as well, which is the actual sandbox.
- The worker inherited the operator's MCP servers and user-level hooks. Now
  isolated with `--strict-mcp-config` and `--setting-sources project`. Cut
  observed cost per attempt roughly in half as a side effect.
- The agent attempt counter was reset on every gate loop-back, so the log
  claimed "build attempt 1" repeatedly and `show` couldn't tell you how often
  the agent actually ran. Now monotonic.
- One attempt could blow past `--max-cost`, because the breaker is only checked
  between steps. Now also capped per attempt via `--max-budget-usd`, bounded by
  whatever is left of the global ceiling.

Also fixed along the way: `stopped_because` persisting after a successful
resume; `list` showing a duplicate row per resume; `AGENT_TIMEOUT` being dead
code (a silent worker hung forever); the worker sharing our stdin and eating
the human gate's input; `git add -A` committing worker litter and `__pycache__`
into attempt commits, which poisoned the review diff.

**Untested:** the `codex` worker.

## Task 3 - only if asked

Move `WORKFLOWS` out of the source and into YAML/JSON files under `flows/`, so a
workflow is a shareable, diffable file. Keep the current dict as the fallback
default. **Do not do this unless the author explicitly asks** - the plan is to
run the hardcoded version on ~20 real tasks first so the schema reflects real
needs rather than guesses.

## Open questions for the author

- **Line count.** ~894 lines, over the ~700 ceiling in constraint 2. Most of the
  growth is comments explaining non-obvious flag choices rather than logic. Trim
  the prose, move the demo/review/tdd workflows out, or raise the ceiling?
- **`aider` cost tracking.** Its output isn't structured, so `cost_usd` stays 0
  for local runs. Fine while the endpoint is free; wrong if it ever isn't.

## Explicitly out of scope

Do not build, propose, or scaffold any of these without being asked:

- A web UI or dashboard. `adw.py show` is the interface.
- A plugin system for blocks.
- Automatic model fallback on failure (it hides the signal).
- Parallel steps *within* one flow (parallel *runs* via worktrees is fine).
- A package, PyPI publish, entry_points, or a name/brand.
