"""A fake gate that always fails identically - trips the loop detector.

Proves adw.py stops after two identical failures rather than burning the
remaining attempts (and your money) on an agent that is oscillating.
"""

import sys

print("IDENTICAL FAILURE, every single time")
sys.exit(1)
