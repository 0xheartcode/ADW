"""#1/#2 — resume at EVERY step boundary of a spliced workflow (no KeyError,
no lost/duplicated steps). A crash can only leave two consistent shapes:
  - mid-plan: step=='plan', steps==[plan]      (splice+transition save together)
  - post-splice: step==do/ok/review, steps==spliced
so the matrix respects that invariant.
"""
import copy

import pytest
from helpers import git_repo, limits, make_run

SPLICED = ["plan", "do.m1", "ok.m1", "do.m2", "ok.m2", "review"]


def _spliced_steps(adw):
    """The effective step list after demo-plan splices, derived from a real run."""
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    repo = git_repo(tmp)
    run_dir = tmp / "run"
    state, steps = make_run(adw, repo, run_dir, "demo-plan", "demo-plan")
    out = adw.orchestrate(state, steps, run_dir, limits())
    assert [s["name"] for s in out["steps"]] == SPLICED
    return out["steps"]


@pytest.mark.parametrize("boundary", SPLICED)
def test_resume_at_each_boundary_completes(adw_mod, tmp_path, boundary):
    spliced = _spliced_steps(adw_mod)
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, _steps = make_run(adw_mod, repo, run_dir, "demo-plan", "demo-plan")

    if boundary == "plan":
        # Crash before the splice: unspliced list, step at plan.
        state["step"] = "plan"
    else:
        # Crash after the splice: persisted effective list carries the spliced
        # steps, step parked at this boundary. This is the shape that KeyErrored
        # before #2.
        state["steps"] = copy.deepcopy(spliced)
        state["step"] = boundary
        state["attempts"] = {}
    adw_mod.save_state(state, run_dir)

    # Resume == rebuild by_name/order from state["steps"] and run forward.
    out = adw_mod.orchestrate(state, state["steps"], run_dir, limits())
    assert out["status"] == "done", f"resume at {boundary} did not finish"
    # review was reached and the run ended cleanly.
    assert out["step"] == "review"
    assert "stopped_because" not in out


def test_uninterrupted_equals_resumed_tail(adw_mod, tmp_path):
    """Resuming at do.m2 runs exactly the m2 tail (m1 already done upstream)."""
    spliced = _spliced_steps(adw_mod)
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, _ = make_run(adw_mod, repo, run_dir, "demo-plan", "demo-plan")
    state["steps"] = copy.deepcopy(spliced)
    state["step"] = "do.m2"
    state["attempts"] = {"plan": 1, "do.m1": 1, "ok.m1": 1}
    adw_mod.save_state(state, run_dir)
    out = adw_mod.orchestrate(state, state["steps"], run_dir, limits())
    assert out["status"] == "done"
    # Only the m2 do step ran here (m1 was upstream), so one attempt file.
    assert len(list(repo.glob("attempt-*.txt"))) == 1
