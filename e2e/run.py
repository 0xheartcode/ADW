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
    # Unique task per invocation so the glob matches ONLY this run - an old
    # crash-resume run dir (already parked at review) must not be latched onto
    # before this run's dir exists.
    task = f"crash-resume-proof-{int(time.time() * 1000)}"
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


def scenario_rate_limit() -> bool:
    """#4: full ADW CLI hits a usage limit (stub-verified) -> parks, no retry,
    then resumes cleanly once the limit clears."""
    _banner("rate-limit")
    repo = _fresh("ratelimit")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    env = {**os.environ, "ADW_DEMO_RATELIMIT": "1"}
    r = _sh([sys.executable, str(ADW), "run", "rl-proof", "--workflow", "demo",
             "--worker", "demo", "--repo", str(repo), "--async"], cwd=ROOT, env=env)
    out = r.stdout + r.stderr
    run_dir = max(Path(ROOT, "runs").glob("*rl-proof*"),
                  key=lambda p: p.stat().st_mtime)
    st = json.loads((run_dir / "state.json").read_text())
    parked_ok = (st["status"] == "parked" and st["attempts"].get("build") == 1
                 and "usage/rate limit" in st.get("stopped_because", ""))
    # Now the limit has cleared: resume with the normal worker; must complete.
    r2 = _sh([sys.executable, str(ADW), "resume", str(run_dir), "--async",
              "--approve"], cwd=ROOT)
    # First resume re-enters build (worker ok now) then runs the gate to a park
    # at review; approve the review to finish. Drive to done.
    r3 = _sh([sys.executable, str(ADW), "resume", str(run_dir), "--async"], cwd=ROOT)
    final = json.loads((run_dir / "state.json").read_text())
    for _ in range(4):
        if final["status"] == "done":
            break
        _sh([sys.executable, str(ADW), "resume", str(run_dir), "--async",
             "--approve"], cwd=ROOT)
        final = json.loads((run_dir / "state.json").read_text())
    _log("rate-limit", f"STUB-VERIFIED (real claude usage-limit line format).\n"
         f"parked_ok={parked_ok} attempts={st['attempts']} "
         f"stopped_because={st.get('stopped_because')}\n"
         f"resume1 exit={r2.returncode} resume2 exit={r3.returncode}\n"
         f"final status={final['status']}\n\n{out}")
    ok = parked_ok and final["status"] == "done"
    print(f"rate-limit: {'OK (stub-verified)' if ok else 'FAIL'} "
          f"(parked no-retry={parked_ok}, final={final['status']})")
    return ok


def scenario_ceiling() -> bool:
    """#8: the real CLI parks at the turn ceiling (demo gate retries build 3x)."""
    _banner("ceiling")
    repo = _fresh("ceiling")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    r = _sh([sys.executable, str(ADW), "run", "ceiling-proof", "--workflow", "demo",
             "--worker", "demo", "--repo", str(repo), "--async",
             "--max-turns", "2"], cwd=ROOT)
    out = r.stdout + r.stderr
    run_dir = max(Path(ROOT, "runs").glob("*ceiling-proof*"),
                  key=lambda p: p.stat().st_mtime)
    st = json.loads((run_dir / "state.json").read_text())
    _log("ceiling", f"status={st['status']} turns={st.get('turns')} "
         f"stopped_because={st.get('stopped_because')}\n\n{out}")
    ok = (st["status"] == "parked" and "turn ceiling" in st.get("stopped_because", "")
          and st.get("turns", 0) > 2)
    print(f"ceiling: {'OK' if ok else 'FAIL'} "
          f"(status={st['status']}, turns={st.get('turns')})")
    return ok


def scenario_interview() -> bool:
    """#5: panedrive drives the TTY-gated interview headlessly (real keys into a
    real PTY), the stub interviewer writes acceptance tests, they are committed
    and the gate passes. Also verifies --async never blocks (it parks)."""
    _banner("interview")
    panedrive = shutil.which("panedrive")
    if not panedrive:
        print("interview: SKIP (panedrive not on PATH)")
        return True
    repo = _fresh("interview")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    pds = ROOT / "dogfood" / "scripts" / "interview.pds"
    stub = ROOT / "demo" / "interviewer_stub.py"
    env = {**os.environ, "ADW_INTERVIEW_CMD": f"{sys.executable} {stub}"}
    # Replay the recorded .pds against ADW's interview, in a real PTY. panedrive
    # spawns the child in $HOME, so run it from ROOT (adw.py writes runs/ under
    # the cwd) via a bash wrapper.
    child = (f"cd {ROOT} && exec {sys.executable} {ADW} run itask --workflow "
             f"demo-interview --worker demo --repo {repo} --auto-approve")
    r = _sh([panedrive, "run", str(pds), "--backend", "pty", "--allow-exit", "--",
             "bash", "-c", child], cwd=ROOT, env=env, timeout=90)
    run_dir = max(Path(ROOT, "runs").glob("*itask*"), key=lambda p: p.stat().st_mtime)
    st = json.loads((run_dir / "state.json").read_text())
    driven_ok = (st["status"] == "done" and (repo / "test_accept.py").exists())

    # --async must PARK the interview, never block on stdin.
    repo2 = _fresh("interview-async")
    _git_init(repo2)
    (repo2 / "seed.txt").write_text("seed\n")
    _commit(repo2)
    r2 = _sh([sys.executable, str(ADW), "run", "iasync", "--workflow",
              "demo-interview", "--worker", "demo", "--repo", str(repo2),
              "--async"], cwd=ROOT, timeout=30)
    run_dir2 = max(Path(ROOT, "runs").glob("*iasync*"), key=lambda p: p.stat().st_mtime)
    st2 = json.loads((run_dir2 / "state.json").read_text())
    async_ok = st2["status"] == "parked" and "needs a human" in st2.get("stopped_because", "")

    _log("interview", f"panedrive exit={r.returncode} driven status={st['status']} "
         f"test_accept={ (repo / 'test_accept.py').exists() }\n"
         f"async exit={r2.returncode} async status={st2['status']} "
         f"stopped_because={st2.get('stopped_because')}\n\n{r.stdout}{r.stderr}")
    ok = driven_ok and async_ok
    print(f"interview: {'OK' if ok else 'FAIL'} "
          f"(panedrive-driven={driven_ok}, async-parks={async_ok})")
    return ok


FIX = ROOT / "dogfood" / "fixtures"


def _gate_exit(argv, cwd, env=None):
    return _sh(argv, cwd=cwd, env=env, timeout=90).returncode


def scenario_issue_sink() -> bool:
    """#12: a parked unattended run files a gli issue in the target repo."""
    _banner("issue-sink")
    if not shutil.which("gli"):
        print("issue-sink: SKIP (gli not on PATH)")
        return True
    repo = _fresh("sink")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    _sh(["gli", "init"], cwd=repo)
    _sh([sys.executable, str(ADW), "run", "sinkrun", "--workflow", "demo-thrash",
         "--worker", "demo", "--repo", str(repo), "--file-issues"], cwd=ROOT)
    ls = _sh(["gli", "ls", "--state", "open"], cwd=repo).stdout
    _log("issue-sink", f"target-repo issues:\n{ls}")
    ok = "ADW parked" in ls and "park:thrash" in ls
    print(f"issue-sink: {'OK' if ok else 'FAIL'} (issue filed in target repo)")
    return ok


def scenario_paneview() -> bool:
    """#11: panedrive drives ADW's own paneview seam and asserts its park state."""
    _banner("paneview")
    panedrive = shutil.which("panedrive")
    if not panedrive:
        print("paneview: SKIP (panedrive not on PATH)")
        return True
    repo = _fresh("paneview")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    seam = (TMP / "adw.seam.json")
    pds = ROOT / "dogfood" / "scripts" / "adw-seam.pds"
    child = (f"cd {ROOT} && exec {sys.executable} {ADW} run seamtask --workflow "
             f"demo --worker demo --repo {repo} --async")
    r = _sh([panedrive, "run", str(pds), "--backend", "pty", "--allow-exit",
             "--state", str(seam), "--", "bash", "-c", child],
            cwd=ROOT, env={**os.environ, "PANEDRIVE_STATE": str(seam)}, timeout=60)
    out = r.stdout + r.stderr
    _log("paneview", f"panedrive exit={r.returncode}\nseam={seam.read_text() if seam.exists() else 'MISSING'}\n\n{out}")
    ok = r.returncode == 0
    print(f"paneview: {'OK' if ok else 'FAIL'} (panedrive asserted ADW seam, exit {r.returncode})")
    return ok


def scenario_deepgates() -> bool:
    """#9: each deep gate PASSES on correct code and FAILS on broken (a gate that
    cannot fail certifies nothing), and the tui/sim workflows run through ADW."""
    _banner("deepgates")
    results = {}
    log = []

    # -- logic gate (unittest) --
    repo = _fresh("dg-logic")
    _git_init(repo)
    (repo / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_adder.py").write_text(
        "import unittest\nfrom adder import add\n"
        "class T(unittest.TestCase):\n"
        "    def test(self):\n        self.assertEqual(add(2, 3), 5)\n")
    ok = _gate_exit([sys.executable, "-m", "unittest", "discover", "-s", str(repo)], repo)
    # Break it. Different length + clear __pycache__ so Python can't reuse the
    # stale (correct) bytecode when mtime-second and size happen to match.
    (repo / "adder.py").write_text("def add(a, b):  # intentionally broken\n    return a - b\n")
    shutil.rmtree(repo / "__pycache__", ignore_errors=True)
    bad = _gate_exit([sys.executable, "-m", "unittest", "discover", "-s", str(repo)], repo)
    results["logic"] = ok == 0 and bad != 0
    log.append(f"logic: correct_exit={ok} broken_exit={bad}")

    # -- tui gate (panedrive, our tool) --
    if shutil.which("panedrive"):
        ws = _fresh("dg-tui")
        _git_init(ws)
        shutil.copy(FIX / "tui" / "app.py", ws / "app.py")
        shutil.copy(FIX / "tui" / "acceptance.pds", ws / "acceptance.pds")
        gate = ["sh", "-c",
                ('panedrive run "$PWD/acceptance.pds" --backend pty --state '
                 '"$PWD/.adw-tui.state.json" -- python3 "$PWD/app.py" '
                 '"$PWD/.adw-tui.state.json"')]
        ok = _gate_exit(gate, ws)
        bad = _gate_exit(gate, ws, env={**os.environ, "TUI_STEP": "2"})
        results["tui-gate"] = ok == 0 and bad != 0
        log.append(f"tui-gate: correct_exit={ok} broken_exit={bad}")

        # tui WORKFLOW end-to-end through ADW (app pre-seeded correct).
        wf = _fresh("dg-tui-wf")
        _git_init(wf)
        shutil.copy(FIX / "tui" / "app.py", wf / "app.py")
        shutil.copy(FIX / "tui" / "acceptance.pds", wf / "acceptance.pds")
        _commit(wf)
        _sh([sys.executable, str(ADW), "run", "tuiwf", "--workflow", "tui",
             "--worker", "demo", "--repo", str(wf), "--auto-approve"], cwd=ROOT)
        st = json.loads((max(Path(ROOT, "runs").glob("*tuiwf*"),
                             key=lambda p: p.stat().st_mtime) / "state.json").read_text())
        results["tui-workflow"] = st["status"] == "done"
        log.append(f"tui-workflow: status={st['status']}")
    else:
        log.append("tui: SKIP (panedrive not on PATH)")

    # -- sim gate (simharness, our tool) --
    if shutil.which("simharness"):
        ws = _fresh("dg-sim")
        shutil.copytree(FIX / "sim", ws, dirs_exist_ok=True)
        _git_init(ws)
        ok = _gate_exit(["simharness", "assert"], ws)
        cfg = ws / "simharness.config.ts"
        cfg.write_text(cfg.read_text().replace(
            "r2.final.extra.kills > 0", "r2.final.extra.kills > 999999"))
        bad = _gate_exit(["simharness", "assert"], ws)
        results["sim-gate"] = ok == 0 and bad != 0
        log.append(f"sim-gate: correct_exit={ok} broken_exit={bad}")

        wf = _fresh("dg-sim-wf")
        shutil.copytree(FIX / "sim", wf, dirs_exist_ok=True)
        _git_init(wf)
        _commit(wf)
        _sh([sys.executable, str(ADW), "run", "simwf", "--workflow", "sim",
             "--worker", "demo", "--repo", str(wf), "--auto-approve"], cwd=ROOT)
        st = json.loads((max(Path(ROOT, "runs").glob("*simwf*"),
                             key=lambda p: p.stat().st_mtime) / "state.json").read_text())
        results["sim-workflow"] = st["status"] == "done"
        log.append(f"sim-workflow: status={st['status']}")
    else:
        log.append("sim: SKIP (simharness not on PATH - build with 'make compile')")

    _log("deepgates", "\n".join(log))
    ok = all(results.values())
    print(f"deepgates: {'OK' if ok else 'FAIL'}  {results}")
    return ok


def scenario_rust() -> bool:
    """#15: the csd architectural gate PASSES on a clean change and FAILS when a
    layering violation is introduced (exit 1), using our own csd."""
    _banner("rust")
    if not shutil.which("csd"):
        print("rust: SKIP (csd not on PATH)")
        return True
    repo = _fresh("rust")
    shutil.copytree(FIX / "rust", repo, dirs_exist_ok=True)
    _git_init(repo)
    _commit(repo, "clean base")
    base = _sh(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    gate = ["csd", "diff", "--base", base, "--list"]
    clean_exit = _gate_exit(gate, repo)
    # Introduce a layering violation: low (bottom) now depends on high (top).
    (repo / "src" / "low.rs").write_text(
        "use crate::high;\npub fn base() -> i32 { high::dummy(); 1 }\n")
    (repo / "src" / "high.rs").write_text(
        "use crate::low; pub fn go() { let _ = low::base(); } pub fn dummy() {}\n")
    _commit(repo, "introduce layering violation")
    viol_exit = _gate_exit(gate, repo)
    _log("rust", f"csd clean_exit={clean_exit} violation_exit={viol_exit}")
    ok = clean_exit == 0 and viol_exit != 0
    print(f"rust: {'OK' if ok else 'FAIL'} "
          f"(clean={clean_exit}, violation={viol_exit})")
    return ok


def scenario_long() -> bool:
    """#6: the whole long workflow end-to-end, offline - interview (panedrive
    over a real PTY) -> plan/splice -> milestones -> review -> done."""
    _banner("long")
    panedrive = shutil.which("panedrive")
    if not panedrive:
        print("long: SKIP (panedrive not on PATH)")
        return True
    repo = _fresh("long")
    _git_init(repo)
    (repo / "seed.txt").write_text("seed\n")
    _commit(repo)
    pds = ROOT / "dogfood" / "scripts" / "long.pds"
    stub = ROOT / "demo" / "interviewer_stub.py"
    env = {**os.environ, "ADW_INTERVIEW_CMD": f"{sys.executable} {stub}"}
    child = (f"cd {ROOT} && exec {sys.executable} {ADW} run longtask --workflow "
             f"long --worker demo-plan --repo {repo} --auto-approve")
    r = _sh([panedrive, "run", str(pds), "--backend", "pty", "--allow-exit", "--",
             "bash", "-c", child], cwd=ROOT, env=env, timeout=120)
    run_dir = max(Path(ROOT, "runs").glob("*longtask*"), key=lambda p: p.stat().st_mtime)
    st = json.loads((run_dir / "state.json").read_text())
    names = [s["name"] for s in st["steps"]]
    _log("long", f"panedrive exit={r.returncode} status={st['status']}\n"
         f"steps={names}\ntest_accept={(repo / 'test_accept.py').exists()}\n"
         f"plan.json={(run_dir / 'plan.json').exists()}\n\n{r.stdout}{r.stderr}")
    ok = (st["status"] == "done"
          and names == ["interview", "plan", "do.m1", "ok.m1", "do.m2", "ok.m2", "review"]
          and (repo / "test_accept.py").exists())
    print(f"long: {'OK' if ok else 'FAIL'} (status={st['status']}, steps={len(names)})")
    return ok


def scenario_thrash() -> bool:
    """#7: a non-converging run parks 'not converging'; a converging one finishes."""
    _banner("thrash")

    def _run(name, flow, extra):
        repo = _fresh(name)
        _git_init(repo)
        (repo / "seed.txt").write_text("seed\n")
        _commit(repo)
        _sh([sys.executable, str(ADW), "run", name, "--workflow", flow,
             "--worker", "demo", "--repo", str(repo), *extra], cwd=ROOT)
        rd = max(Path(ROOT, "runs").glob(f"*{name}*"), key=lambda p: p.stat().st_mtime)
        return json.loads((rd / "state.json").read_text())

    thr = _run("thrash-bad", "demo-thrash", ["--async"])
    conv = _run("thrash-good", "demo-converge", ["--auto-approve"])
    _log("thrash", f"non-converging: status={thr['status']} "
         f"stopped_because={thr.get('stopped_because')} attempts={thr['attempts']}\n"
         f"converging: status={conv['status']} attempts={conv['attempts']}\n")
    ok = (thr["status"] == "parked" and "not converging" in thr.get("stopped_because", "")
          and thr["attempts"]["check"] < 8 and conv["status"] == "done")
    print(f"thrash: {'OK' if ok else 'FAIL'} "
          f"(bad={thr['status']}/{thr.get('stopped_because','')[:20]}, good={conv['status']})")
    return ok


SCENARIOS = {
    "demo": scenario_demo,
    "logic": scenario_logic,
    "crash-resume": scenario_crash_resume,
    "subscription": scenario_subscription,
    "rate-limit": scenario_rate_limit,
    "ceiling": scenario_ceiling,
    "thrash": scenario_thrash,
    "interview": scenario_interview,
    "long": scenario_long,
    "deepgates": scenario_deepgates,
    "paneview": scenario_paneview,
    "issue-sink": scenario_issue_sink,
    "rust": scenario_rust,
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
