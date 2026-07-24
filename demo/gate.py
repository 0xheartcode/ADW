"""A fake gate: fails twice with different output, then passes.

Different output each time on purpose - identical failures would trip the
loop detector, which is a different code path (see `just demo-stuck`).

Counter lives in the workspace so each demo run starts fresh.
"""

import pathlib
import sys

counter = pathlib.Path(".demo-gate-count")
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))

if n <= 2:
    print(f"FAILURE {n}: expected foo, got bar")
    print(f"  the gate has now run {n} time(s) and is not happy yet")
    sys.exit(1)
print("all good")
