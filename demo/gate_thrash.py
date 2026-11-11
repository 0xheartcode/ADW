"""A fake gate that fails DIFFERENTLY every time but never gets closer.

Different text each attempt (so the identical-twice detector never fires), yet
a constant failure signature ("3 failed"). This is the case the thrash detector
exists to catch: oscillation without convergence. adw.py should park with
"not converging" rather than burning the whole retry budget.
"""
import pathlib
import sys

c = pathlib.Path(".thrash-count")
n = int(c.read_text()) + 1 if c.exists() else 1
c.write_text(str(n))

print(f"attempt {n}: a different wrong value {n * 7 + 1} this time")
print("3 failed")          # constant signature -> never shrinks
sys.exit(1)
