#!/usr/bin/env python3
"""Dogfood harness: build throwaway sandboxes and drive REAL adw.py runs.

Usage: python dogfood/run.py <scenario|all>

Scenarios are added as issues land. Each returns True on success. Logs go to
dogfood/logs/<scenario>.log and are referenced from commit bodies / gli notes.
Stdlib only, like adw.py — no third-party imports here either.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADW = ROOT / "adw.py"
TMP = ROOT / "dogfood" / "tmp"
LOGS = ROOT / "dogfood" / "logs"


def _sh(cmd, cwd=None, env=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _git_init(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    _sh(["git", "init", "-q", "."], cwd=repo)
    _sh(["git", "config", "user.email", "dogfood@adw"], cwd=repo)
    _sh(["git", "config", "user.name", "dogfood"], cwd=repo)


def _commit(repo: Path, msg="init"):
    _sh(["git", "add", "-A"], cwd=repo)
    _sh(["git", "commit", "-qm", msg], cwd=repo)


def _fresh(name: str) -> Path:
    d = TMP / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def _log(name: str, text: str):
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{name}.log").write_text(text)


def _banner(name):
    print(f"\n=== dogfood: {name} ===")


# --------------------------------------------------------------------------
def scenario_demo() -> bool:
    """Free deterministic worker: exercises retry, breaker, feedback, park."""
    _banner("demo")
    repo = _fresh("demo")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    r = _sh([sys.executable, str(ADW), "run", "demo", "--workflow", "demo",
             "--worker", "demo", "--repo", str(repo), "--auto-approve"], cwd=ROOT)
    out = r.stdout + r.stderr
    _log("demo", out)
    ok = r.returncode == 0 and "DONE" in out
    print(f"demo: {'OK' if ok else 'FAIL'} (exit {r.returncode})")
    return ok


def scenario_logic() -> bool:
    """Tiny Python project gated by unittest (deep gate: logic)."""
    _banner("logic")
    repo = _fresh("logic")
    _git_init(repo)
    (repo / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_adder.py").write_text(
        "import unittest\nfrom adder import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    _commit(repo)
    # Gate the sandbox with a stdlib unittest run (real exit-code gate).
    r = _sh([sys.executable, "-m", "unittest", "discover", "-s", str(repo)], cwd=repo)
    out = f"$ python -m unittest discover\nexit={r.returncode}\n{r.stdout}{r.stderr}"
    _log("logic", out)
    ok = r.returncode == 0
    print(f"logic gate: {'OK' if ok else 'FAIL'} (exit {r.returncode})")
    return ok


SCENARIOS = {
    "demo": scenario_demo,
    "logic": scenario_logic,
}


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(SCENARIOS) if which == "all" else [which]
    results = {}
    for n in names:
        fn = SCENARIOS.get(n)
        if not fn:
            print(f"unknown scenario {n!r}; have: {', '.join(SCENARIOS)}")
            return 2
        try:
            results[n] = fn()
        except Exception as exc:  # noqa: BLE001 - dogfood must report, not crash
            print(f"{n}: EXCEPTION {exc}")
            results[n] = False
    print("\n--- dogfood summary ---")
    for n, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
