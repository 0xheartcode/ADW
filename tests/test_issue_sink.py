"""#12 - gli as the butler issue sink: file on park, update, close on done."""
import shutil
import subprocess

from helpers import git_repo


def _gli(repo, *args):
    return subprocess.run(["gli", *args], cwd=repo, capture_output=True,
                          text=True, check=False)


def _state(repo, run_dir):
    (run_dir / "gates").mkdir(parents=True, exist_ok=True)
    return {
        "run": "r1", "task": "do the thing", "workflow": "demo", "worker": "demo",
        "workspace": str(repo), "origin_repo": str(repo), "status": "parked",
        "park_kind": "thrash", "stopped_because": "check not converging",
        "pending": {"actions": ["resume", "reject"]},
    }


def test_sink_noop_when_disabled(adw_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("ADW_FILE_ISSUES", raising=False)
    state = {"origin_repo": str(tmp_path), "pending": {}, "run": "x",
             "task": "t", "workflow": "demo", "worker": "demo", "workspace": str(tmp_path)}
    adw_mod.file_or_update_issue(state, tmp_path)   # must not crash, no issue
    assert "issue_id" not in state


def test_sink_files_updates_and_closes(adw_mod, tmp_path, monkeypatch):
    if not shutil.which("gli"):
        return
    repo = git_repo(tmp_path)
    _gli(repo, "init")
    monkeypatch.setenv("ADW_FILE_ISSUES", "1")
    run_dir = tmp_path / "run"
    state = _state(repo, run_dir)

    adw_mod.file_or_update_issue(state, run_dir)
    assert state.get("issue_id")                    # one issue filed
    first = state["issue_id"]
    opened = _gli(repo, "ls", "--state", "open").stdout
    assert "ADW parked" in opened and "park:thrash" in opened

    # Re-park: updates the SAME issue, never a duplicate.
    adw_mod.file_or_update_issue(state, run_dir)
    assert state["issue_id"] == first
    count = sum(1 for ln in _gli(repo, "ls", "--state", "open").stdout.splitlines()
                if "ADW parked" in ln)
    assert count == 1

    # Done: close the sink issue.
    adw_mod.close_issue_if_any(state)
    assert "ADW parked" not in _gli(repo, "ls", "--state", "open").stdout
