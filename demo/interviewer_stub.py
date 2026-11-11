"""A stand-in for the interactive interview agent, for driving/testing the
interview step without real `claude`. Point ADW_INTERVIEW_CMD at this.

Behaves like the real thing in shape: it reads the human's answer from the TTY,
then WRITES the acceptance criteria as a runnable test (test_accept.py) into the
workspace, which the normal gates then enforce. The prompt is argv[1].
"""
import pathlib
import sys

sys.stdout.write("Acceptance criteria for this task? (one line) > ")
sys.stdout.flush()
answer = sys.stdin.readline().strip() or "it works"

pathlib.Path("test_accept.py").write_text(
    "def test_acceptance():\n"
    f"    # criteria agreed at interview: {answer}\n"
    "    assert True\n")
print(f"\nWrote test_accept.py from: {answer!r}")
