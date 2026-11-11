#!/usr/bin/env python3
"""Dogfood harness: build throwaway sandboxes and drive REAL adw.py runs.

Usage: python dogfood/run.py <scenario|all>

Scenarios are added as issues land. Each returns True on success. Logs go to
dogfood/logs/<scenario>.log and are referenced from commit bodies / gli notes.
Stdlib only, like adw.py — no third-party imports here either.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
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


def scenario_crash_resume() -> bool:
    """#1/#2: splice a plan, SIGKILL ADW mid-milestone, resume -> no KeyError."""
    _banner("crash-resume")
    repo = _fresh("crash")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    task = "crash-resume-proof"
    env = {**os.environ, "ADW_DEMO_SLEEP": "2"}   # widen the do-step window
    # Spawn in its own session so we can kill the whole process group hard.
    proc = subprocess.Popen(
        [sys.executable, str(ADW), "run", task, "--workflow", "demo-plan",
         "--worker", "demo-plan", "--repo", str(repo), "--async"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True)
    run_dir, killed_at = None, None
    deadline = time.time() + 30
    while time.time() < deadline:
        if run_dir is None:
            hits = sorted(Path(ROOT, "runs").glob(f"*{task}*"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            run_dir = hits[0] if hits else None
        if run_dir and (run_dir / "state.json").exists():
            try:
                step = json.loads((run_dir / "state.json").read_text())["step"]
            except (json.JSONDecodeError, KeyError):
                step = None
            if step and step.startswith("do."):
                killed_at = step
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                break
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    if not killed_at:
        _log("crash-resume", "FAILED to catch the run mid-milestone")
        print("crash-resume: FAIL (never caught a do.* step)")
        return False

    # The killed process left state mid-run. Resume must finish with no KeyError.
    r = subprocess.run(
        [sys.executable, str(ADW), "resume", str(run_dir), "--async"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    out = r.stdout + r.stderr
    final = json.loads((run_dir / "state.json").read_text())
    log = (f"killed at step: {killed_at}\n"
           f"resume exit: {r.returncode}\nfinal status: {final['status']}\n"
           f"final step: {final['step']}\n"
           f"steps: {[s['name'] for s in final['steps']]}\n\n{out}")
    _log("crash-resume", log)
    # Parked at review (==done bar the human nod) with every milestone present,
    # and no KeyError/Traceback anywhere.
    ok = ("KeyError" not in out and "Traceback" not in out
          and final["status"] in ("parked", "done")
          and final["step"] == "review"
          and "do.m2" in [s["name"] for s in final["steps"]])
    print(f"crash-resume: {'OK' if ok else 'FAIL'} "
          f"(killed at {killed_at}, resumed to {final['status']}/{final['step']})")
    return ok


def scenario_subscription() -> bool:
    """#3: real claude-code subscription worker (no API key). Opt-in - spends
    plan usage. Set ADW_DOGFOOD_SUBSCRIPTION=1 to run it."""
    _banner("subscription")
    if os.environ.get("ADW_DOGFOOD_SUBSCRIPTION") != "1":
        print("subscription: SKIP (set ADW_DOGFOOD_SUBSCRIPTION=1 to run; "
              "last real run recorded in dogfood/logs/subscription-worker.log)")
        return True
    if not shutil.which("claude"):
        print("subscription: SKIP (claude not on PATH)")
        return True
    repo = _fresh("subscription")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    r = _sh([sys.executable, str(ADW), "run",
             "Create a file named greeting.txt whose only contents are the word hello",
             "--workflow", "chore", "--worker", "claude-code",
             "--repo", str(repo), "--max-cost", "2"], cwd=ROOT, timeout=300)
    out = r.stdout + r.stderr
    _log("subscription", out)
    ok = r.returncode == 0 and "DONE" in out and (repo / "greeting.txt").exists()
    print(f"subscription: {'OK' if ok else 'FAIL'} (exit {r.returncode})")
    return ok


SCENARIOS = {
    "demo": scenario_demo,
    "logic": scenario_logic,
    "crash-resume": scenario_crash_resume,
    "subscription": scenario_subscription,
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
