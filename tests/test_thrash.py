"""#7 — thrash detector keyed on a shrinking structured failure signature."""
import json

from helpers import git_repo, limits, make_run


# ---- failure_signature reads structured gate output -------------------
def test_signature_simharness_assert(adw_mod):
    out = json.dumps({"ok": False, "asserts": [
        {"ok": True, "name": "a"}, {"ok": False, "name": "b"},
        {"ok": False, "name": "c"}]})
    assert adw_mod.failure_signature(out) == 2


def test_signature_simharness_fuzz_breaks(adw_mod):
    assert adw_mod.failure_signature(json.dumps({"ok": False, "breaks": ["x", "y"]})) == 2


def test_signature_panedrive_failed_step_further_is_smaller(adw_mod):
    near = json.dumps({"ok": False, "failed_step": 2, "steps": [1, 2, 3]})
    far = json.dumps({"ok": False, "failed_step": 6, "steps": [1, 2, 3, 4, 5, 6, 7]})
    assert adw_mod.failure_signature(far) < adw_mod.failure_signature(near)


def test_signature_panedrive_count_not_ok(adw_mod):
    out = json.dumps({"ok": False, "steps": [{"ok": True}, {"ok": False}]})
    assert adw_mod.failure_signature(out) == 1


def test_signature_pytest_and_fallback(adw_mod):
    assert adw_mod.failure_signature("=== 3 failed, 2 passed ===") == 3
    assert adw_mod.failure_signature("line one\nline two\nline three") == 3


# ---- the detector: thrash parks, convergence does not -----------------
def test_non_converging_run_parks(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-thrash", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "parked"
    assert "not converging" in out["stopped_because"]
    # It parked early, well before the max of 8.
    assert out["attempts"]["check"] < 8


def test_converging_run_is_not_tripped(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-converge", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "done"          # shrinking signature reached pass


def test_identical_failure_still_shortcircuits(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-stuck", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "parked"
    assert "identically twice" in out["stopped_because"]
