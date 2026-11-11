"""Shared test helpers: build runnable states and stub workers."""
import copy
import stat
import subprocess
import sys
from pathlib import Path


def limits(**over):
    base = {"max_cost": 5.0, "max_wall": 600.0, "max_attempt_cost": 1.0,
            "max_turns": 10_000, "auto_approve": True, "noninteractive": False}
    base.update(over)
    return base


def make_run(adw, repo: Path, run_dir: Path, workflow: str, worker: str,
             task="do the thing"):
    """Build a fresh running state + steps the way cmd_run does."""
    (run_dir / "gates").mkdir(parents=True, exist_ok=True)
    steps = copy.deepcopy(adw.WORKFLOWS[workflow])
    h = adw.head(repo)
    state = {
        "run": run_dir.name, "task": task, "workflow": workflow,
        "worker": worker, "workspace": str(repo), "branch": None,
        "origin_repo": str(repo), "base": h, "head": h,
        "step": steps[0]["name"], "status": "running", "attempts": {},
        "gate_hashes": {}, "cost_usd": 0.0, "feedback": None,
        "stream_types": {}, "started_at": adw.stamp(), "history": [],
        "steps": steps,
    }
    adw.save_state(state, run_dir)
    return state, steps


def write_stub_worker(path: Path, body: str) -> Path:
    """Write an executable python stub worker that takes the prompt as argv[1]."""
    path.write_text("#!/usr/bin/env python3\nimport sys, json, pathlib, os\n"
                    "prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def register_stub(adw, name: str, script: Path, stream="claude-json"):
    adw.WORKERS[name] = {"cmd": [sys.executable, str(script), "{prompt}"],
                         "stream": stream}


def git_repo(tmp: Path) -> Path:
    repo = tmp / "ws"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


# A claude-shaped success result tail every stub appends.
RESULT_OK = (
    "print(json.dumps({'type':'result','subtype':'success',"
    "'is_error':False,'total_cost_usd':0.0}))\n")
