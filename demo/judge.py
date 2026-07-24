"""An LLM reviewing another agent's work - as a gate, not a new step kind.

This is the point of the whole file. A gate is any command that exits 0 or 1.
It does not have to be a test runner. Point one at an LLM and you have an
agent reviewing another agent's diff, with no new machinery in adw.py: the
verdict is the exit code, and the critique lands on stdout, which adw.py
already feeds into the next prompt as feedback.

So "can an agent send things to other agents?" - yes, and this is the channel.
Exit code for control flow, stdout for content.

Reads ADW_API_BASE / ADW_MODEL / OPENAI_API_KEY from .env. Deliberately uses
a cheap local model: expensive model writes, cheap model reviews.

Usage in a workflow:

    {"kind": "gate", "name": "judge",
     "cmd": [sys.executable, str(HERE / "demo/judge.py"), "HEAD~1"],
     "on_fail": "build", "max": 2}
"""

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

# In-flow, adw.py has already loaded .env and we inherit it. Read it anyway so
# this is also runnable by hand from any directory.
_ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for _raw in _ENV.read_text().splitlines():
        _line = _raw.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

RUBRIC = """You review code diffs. You output a verdict in a fixed format.

Think silently. Do NOT narrate your reasoning, do not weigh options out
loud, and never revise a verdict once written. Output exactly this:

VERDICT: PASS
<one paragraph, at most 5 sentences>

or:

VERDICT: FAIL
<one paragraph of actionable direction, at most 5 sentences>

FAIL only for real defects: bugs, broken edge cases, swallowed errors, or a
change that plainly does not do what was asked. Do NOT fail for style,
naming, or missing docstrings - a linter covers those and you cost more
than a linter. When the diff fixes a defect, that is a PASS.

Your paragraph is fed verbatim to a coding agent as its next instruction,
so write direction, not a report.
"""


def verdict_of(text: str) -> bool | None:
    """True=pass, False=fail, None=couldn't tell.

    Takes the LAST verdict line, not the first: a small model that ignores
    the format and reasons out loud tends to correct itself on the way down,
    and its final answer is the one it means.
    """
    seen = None
    for line in text.splitlines():
        stripped = line.strip().upper().removeprefix("**").lstrip("# ")
        if stripped.startswith("VERDICT:"):
            token = stripped.removeprefix("VERDICT:").strip().strip("*").split()
            if token and token[0] in ("PASS", "FAIL"):
                seen = token[0] == "PASS"
        elif stripped in ("PASS", "FAIL", "PASS.", "FAIL."):
            seen = stripped.startswith("PASS")
    return seen


def ask(diff: str) -> str:
    base = os.environ.get("ADW_API_BASE")
    key = os.environ.get("OPENAI_API_KEY")
    # A judge is a different job from a writer: it needs to follow a format
    # and not ramble. Override separately from ADW_MODEL.
    model = os.environ.get(
        "ADW_JUDGE_MODEL",
        os.environ.get("ADW_MODEL", "openai/local")).removeprefix("openai/")
    if not base or not key:
        return "VERDICT: PASS\njudge not configured (no ADW_API_BASE/OPENAI_API_KEY)"

    body = json.dumps({
        "model": model,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": RUBRIC},
            {"role": "user", "content": f"Diff under review:\n\n{diff[:12000]}"},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.load(resp)
    return payload["choices"][0]["message"]["content"].strip()


def main() -> int:
    against = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    diff = subprocess.run(["git", "diff", against, "--", ".",
                           ":(exclude).aider*", ":(exclude)attempt-*.txt"],
                          capture_output=True, text=True, check=False).stdout
    if not diff.strip():
        print("VERDICT: PASS\nnothing to review")
        return 0

    try:
        verdict = ask(diff)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        # The judge being unreachable is not the agent's fault and it will
        # not be fixed by rewriting the code. Pair this with on_fail: None
        # so the run stops instead of paying for pointless retries.
        print(f"judge unavailable: {exc}")
        return 2

    print(verdict)
    passed = verdict_of(verdict)
    if passed is None:
        # No parseable verdict. Treat as a fail so the critique still reaches
        # the agent, but say plainly that the judge misbehaved.
        print("\n[judge] no VERDICT: line found - treating as FAIL")
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
