"""#6 — the long workflow: interview -> plan/splice -> milestones -> review."""
import json
import os
import pty
import sys

from helpers import git_repo, limits, make_run

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_long_workflow_validates(adw_mod):
    assert "long" in adw_mod.WORKFLOWS
    kinds = [s["kind"] for s in adw_mod.WORKFLOWS["long"]]
    assert kinds == ["interview", "plan"]
    assert adw_mod.validate(adw_mod.WORKFLOWS["long"], "demo-plan") == []


def test_long_end_to_end_over_a_pty(adw_mod, tmp_path, monkeypatch):
    """The whole chain offline: interview (stub over a real pty) -> plan writes
    plan.json -> splice -> do.m1/ok.m1/do.m2/ok.m2 -> review -> done."""
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "long", "demo-plan")
    monkeypatch.setenv(
        "ADW_INTERVIEW_CMD",
        f"{sys.executable} {os.path.join(HERE, 'demo', 'interviewer_stub.py')}")

    pid, fd = pty.fork()
    if pid == 0:
        sys.stdin = os.fdopen(0)
        try:
            adw_mod.orchestrate(state, steps, run_dir, limits())
        finally:
            os._exit(0)
    os.write(fd, b"the feature must pass its acceptance tests\n")
    try:
        while True:
            try:
                if not os.read(fd, 1024):
                    break
            except OSError:
                break
    finally:
        os.waitpid(pid, 0)

    final = json.loads((run_dir / "state.json").read_text())
    assert final["status"] == "done"
    assert [s["name"] for s in final["steps"]] == [
        "interview", "plan", "do.m1", "ok.m1", "do.m2", "ok.m2", "review"]
    assert (repo / "test_accept.py").exists()          # interview product
    assert (run_dir / "plan.json").exists()            # plan product
