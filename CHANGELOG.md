# Changelog

All notable changes to `adw.py` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-13

First public release: the long-horizon milestone. `adw.py` is a single,
zero-dependency Python file that runs a nondeterministic worker, then a
deterministic gate, and lets the gate's exit code decide what happens next.

### Added

Core loop and resume
- `plan` step kind: an agent step that emits further steps, validated and
  spliced into the live run (gli #1).
- Persist the effective (spliced) step list so a resumed run picks up the
  real plan, not just the template (gli #2).

Workers
- `claude-code` subscription worker: run under a Claude subscription with no
  API key and no per-attempt dollar budget (gli #3).

Safety rails
- Park cleanly on a usage/rate limit instead of retrying into the wall (gli #4).
- Turn ceiling for the run, and all circuit breakers factored into one
  `breaker()` (the bound that bites under a subscription, where cost is blind)
  (gli #8).
- Thrash detector: stop when the structured failure signature stops shrinking,
  so a run that fails differently but no closer does not burn the budget (gli #7).

Workflows
- `interview` step: pin acceptance criteria as runnable tests before any build,
  co-written by a human and an interactive agent (gli #5).
- The long-horizon workflow: interview -> plan -> milestones -> deep gates ->
  review -> done (gli #6).
- Deep-gate workflows `tui` and `sim`, gated by our own headless tooling (gli #9).
- `csd` Rust architectural deep gate with a convergence signal (gli #15).

Introspection and DX
- `paneview` JSON seam so a driver can run and assert ADW headlessly (gli #11).
- `graph` renders a run's actual spliced steps, not just the template (gli #10).
- Park/resume UX: copy-paste resume hints and a park-reason taxonomy, with a
  `list --park-kind` filter (gli #17).
- `summary` command and subscription usage reporting (gli #13, #14).
- `detect` command: suggest a workflow from the project type (advisory; an
  explicit `--workflow` still wins) (gli #16).
- `--version` flag.
- `just tutorial` and `just tutorial-interview`: a guided first run on the free
  `demo` workflow.

Butler
- `gli` issue sink: file one issue on park, update it on re-park, close it on
  done (opt-in via `--file-issues`) (gli #12).

### Changed
- The end-to-end harness moved from `dogfood/` to `e2e/`; the runner is now
  `just e2e` (was `just dogfood`).

[0.1.0]: https://github.com/0xheartcode/ADW/releases/tag/v0.1.0
