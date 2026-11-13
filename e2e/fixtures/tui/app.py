#!/usr/bin/env python3
"""A tiny line-driven TUI that exposes a paneview JSON state seam, so panedrive
can drive it headlessly and assert on real state (gli #9, the deep TUI gate).

Reads commands (one per line) from stdin, keeps a counter, and writes the seam
{count,last,ready} atomically to the path in argv[1] after every change. This is
the CORRECT version; the dogfood also builds a BROKEN copy to prove the gate can
actually fail. Stdlib only.
"""
import json
import os
import sys
import tempfile

STATE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PANEDRIVE_STATE", "state.json")
STEP = int(os.environ.get("TUI_STEP", "1"))      # 1 = correct; 2 = broken


def write(count, last):
    d = os.path.dirname(os.path.abspath(STATE)) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump({"count": count, "last": last, "ready": True}, f)
    os.replace(tmp, STATE)


def main():
    count, last = 0, ""
    write(count, last)
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "inc":
            count += STEP
            last = "inc"
        elif cmd == "quit":
            break
        else:
            last = cmd
        write(count, last)


if __name__ == "__main__":
    main()
