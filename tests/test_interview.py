"""#5 — interview step: pin acceptance tests before build, at a real TTY."""
import os
import pty
import sys

from helpers import git_repo, limits, make_run

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_interview_parks_when_async(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-interview", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits(noninteractive=True))
    assert out["status"] == "parked"
    assert "needs a human" in out["stopped_because"]


def test_interview_parks_without_tty(adw_mod, tmp_path):
    # pytest has no controlling TTY on stdin, so even interactive mode must park
    # rather than block or silently skip.
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-interview", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits(noninteractive=False))
    assert out["status"] == "parked"
    assert "needs a human" in out["stopped_because"]


def test_interview_writes_and_commits_tests_over_a_real_pty(adw_mod, tmp_path, monkeypatch):
    """Drive the interactive path over a real pty with the stub interviewer:
    it must write the acceptance test, commit it, and let the gate pass."""
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-interview", "demo")
    monkeypatch.setenv(
        "ADW_INTERVIEW_CMD",
        f"{sys.executable} {os.path.join(HERE, 'demo', 'interviewer_stub.py')}")

    # Give orchestrate a real tty on stdin by running it in a pty child.
    pid, fd = pty.fork()
    if pid == 0:                                   # child: has a controlling tty
        # pytest swaps sys.stdin for a non-tty capture wrapper; restore the real
        # fd 0 (the pty slave) so sys.stdin.isatty() reflects the terminal.
        sys.stdin = os.fdopen(0)
        try:
            adw_mod.orchestrate(state, steps, run_dir, limits())
        finally:
            os._exit(0)
    os.write(fd, b"login redirects to /home\n")    # the human's answer
    try:
        while True:
            try:
                if not os.read(fd, 1024):
                    break
            except OSError:
                break
    finally:
        os.waitpid(pid, 0)

    final = __import__("json").loads((run_dir / "state.json").read_text())
    assert (repo / "test_accept.py").exists()      # criteria written
    assert final["status"] == "done"               # gate passed, review approved
    # The acceptance test was committed (run history == git history).
    log = adw_mod.git(repo, "log", "--oneline")
    assert "interview" in log
