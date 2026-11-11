"""Test harness for adw.py.

adw.py lives at the repo root and is dependency-free; we import it as a module.
These test deps (pytest, hypothesis) live only in the gitignored .venv and are
NEVER imported by adw.py itself (enforced by test_zero_dep.py).
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest


def _load_adw():
    spec = importlib.util.spec_from_file_location("adw", ROOT / "adw.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adw = _load_adw()


@pytest.fixture
def adw_mod():
    return adw


@pytest.fixture
def git_repo(tmp_path):
    """A tmp git repo with one commit — the minimum adw.py requires."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def base_state(adw_mod, git_repo, tmp_path):
    """A minimal running state dict + its run_dir, as cmd_run would build."""
    run_dir = tmp_path / "run"
    (run_dir / "gates").mkdir(parents=True)
    h = adw_mod.head(git_repo)
    state = {
        "run": "run", "task": "do the thing", "workflow": "demo",
        "worker": "demo", "workspace": str(git_repo), "branch": None,
        "origin_repo": str(git_repo), "base": h, "head": h,
        "step": "build", "status": "running", "attempts": {},
        "gate_hashes": {}, "cost_usd": 0.0, "feedback": None,
        "stream_types": {}, "started_at": adw_mod.stamp(), "history": [],
    }
    return state, run_dir
