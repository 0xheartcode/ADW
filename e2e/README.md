# e2e/ - run ADW for real against throwaway sandboxes

`run.py` builds fresh sandbox projects in `e2e/tmp/` (gitignored), each its own git
repo, and drives real `adw.py` runs against them. Logs land in `e2e/logs/` and are
referenced from commit bodies and gli close notes.

```
.venv/bin/python e2e/run.py <scenario>   # one scenario
just e2e                                  # all scenarios
```

Scenarios (grow per issue):
- `demo`        - free deterministic worker through the demo workflow (retry/breaker/park).
- `logic`       - tiny Python project gated by `unittest` (deep gate: logic).
- `crash-resume`- splice a plan, kill ADW mid-step, resume; assert no KeyError, no dup/lost steps (#1/#2).
- `thrash`      - failures differ but do not shrink; detector must fire; a converging run must not (#7).
- `ceiling`     - hit turn + wall ceilings; must park (#8).
- `interview`   - TTY interview driven by panedrive; `--async` never blocks (#5).
- `long`        - full interview -> plan -> milestones -> deep gates -> done (#6).
- `tui`         - tiny PTY TUI gated by `panedrive run` on a `.pds` (#9).
- `sim`         - tiny seeded sim gated by `simharness assert` (#9).
- `paneview`    - panedrive drives ADW's own seam and asserts park state (#11).

Sandboxes that need real subscription usage are kept tiny and turn-capped. Anything only
verifiable against a stub is labelled `stub-verified` in the log and never presented as a
real run.
