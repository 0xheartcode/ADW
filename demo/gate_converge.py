"""A fake gate that genuinely CONVERGES: fewer failures each attempt, then pass.

3 failed -> 2 failed -> 1 failed -> pass. The failure signature shrinks every
attempt, so the thrash detector must NOT trip; the run should reach the gate's
pass and finish. Proves the detector does not punish real progress.
"""
import pathlib
import sys

c = pathlib.Path(".conv-count")
n = int(c.read_text()) + 1 if c.exists() else 1
c.write_text(str(n))

remaining = max(0, 4 - n)
if remaining == 0:
    print("all checks pass")
    sys.exit(0)
print(f"{remaining} failed")
sys.exit(1)
