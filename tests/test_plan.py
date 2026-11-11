"""#1 — plan step: dynamic step emission and splice."""
import pytest
from helpers import (
    RESULT_OK,
    git_repo,
    limits,
    make_run,
    register_stub,
    write_stub_worker,
)


# ---- expand_milestones -------------------------------------------------
def test_expand_milestones_shape(adw_mod):
    out = adw_mod.expand_milestones([
        {"id": "a", "prompt": "do a", "gate": ["true"]},
        {"id": "b", "prompt": "do b", "gate": "ruff check ."},
    ])
    names = [s["name"] for s in out]
    assert names == ["do.a", "ok.a", "do.b", "ok.b", "review"]
    assert out[0]["kind"] == "agent" and "{task}" in out[0]["prompt"]
    assert out[1] == {"kind": "gate", "name": "ok.a", "cmd": ["true"],
                      "on_fail": "do.a", "max": 3}
    assert out[3]["cmd"] == ["ruff", "check", "."]      # string gate shlex-split
    assert out[-1] == {"kind": "human", "name": "review"}


def test_expand_milestones_rejects_bad_input(adw_mod):
    with pytest.raises(ValueError):
        adw_mod.expand_milestones([])
    with pytest.raises(ValueError):
        adw_mod.expand_milestones("not a list")
    with pytest.raises(KeyError):
        adw_mod.expand_milestones([{"id": "a", "prompt": "x"}])   # no gate
    with pytest.raises(ValueError):
        adw_mod.expand_milestones([{"id": "a", "prompt": "x", "gate": []}])


# ---- read_plan ---------------------------------------------------------
def test_read_plan_moves_file_out_of_workspace(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (repo / "plan.json").write_text('[{"id":"m1","prompt":"p","gate":["true"]}]')
    ms = adw_mod.read_plan({"workspace": str(repo)}, run_dir)
    assert ms[0]["id"] == "m1"
    assert (run_dir / "plan.json").exists()          # copied for the record
    assert not (repo / "plan.json").exists()         # removed from the diff


def test_read_plan_missing_raises(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        adw_mod.read_plan({"workspace": str(repo)}, run_dir)


# ---- validate accepts plan kind ---------------------------------------
def test_validate_accepts_plan_and_checks_task(adw_mod):
    good = [{"kind": "plan", "name": "p", "prompt": "{task} plan it", "max": 2}]
    assert adw_mod.validate(good, "demo") == []
    bad = [{"kind": "plan", "name": "p", "prompt": "no placeholder", "max": 2}]
    assert any("{task}" in e for e in adw_mod.validate(bad, "demo"))


# ---- full splice integration (free demo-plan worker) -------------------
def test_plan_splices_and_runs_to_done(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-plan", "demo-plan")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "done"
    assert [s["name"] for s in out["steps"]] == \
        ["plan", "do.m1", "ok.m1", "do.m2", "ok.m2", "review"]
    # Both milestones actually executed (the do steps wrote attempt files).
    assert len(list(repo.glob("attempt-*.txt"))) == 2
    assert (run_dir / "plan.json").exists()


# ---- a bad plan is handled exactly like a failed gate ------------------
def test_bad_plan_retries_then_parks(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "badplan.py",
        "pathlib.Path('plan.json').write_text('not valid json at all')\n" + RESULT_OK)
    register_stub(adw_mod, "badplan", stub)
    state, steps = make_run(adw_mod, repo, run_dir, "demo-plan", "badplan")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "parked"
    assert "valid plan" in out["stopped_because"]
    assert out["attempts"]["plan"] == 2              # bounded by max=2


def test_plan_rejected_once_then_succeeds(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    # Garbage on attempt 1 (no attempt-marker file yet), valid plan on attempt 2.
    stub = write_stub_worker(
        tmp_path / "flaky.py",
        "mark = pathlib.Path('.tried')\n"
        "if mark.exists():\n"
        "    pathlib.Path('plan.json').write_text(json.dumps("
        "[{'id':'m1','prompt':'do','gate':['true']}]))\n"
        "else:\n"
        "    mark.write_text('1'); pathlib.Path('plan.json').write_text('{bad')\n"
        + RESULT_OK)
    register_stub(adw_mod, "flaky", stub)
    state, steps = make_run(adw_mod, repo, run_dir, "demo-plan", "flaky")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "done"
    assert out["attempts"]["plan"] == 2
    assert [s["name"] for s in out["steps"]] == ["plan", "do.m1", "ok.m1", "review"]
