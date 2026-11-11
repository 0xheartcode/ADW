"""#9 — deep-gate recipes (tui/sim) + the gate contract they rely on."""
import shutil

from helpers import git_repo, make_run


def test_tui_and_sim_workflows_are_defined(adw_mod):
    for name in ("tui", "sim"):
        assert name in adw_mod.WORKFLOWS
        kinds = [s["kind"] for s in adw_mod.WORKFLOWS[name]]
        assert kinds[0] == "agent" and "gate" in kinds
    # The pinned gate argv (from source survey) is what each workflow ships.
    sim_gate = next(s for s in adw_mod.WORKFLOWS["sim"] if s["kind"] == "gate")
    assert sim_gate["cmd"] == ["simharness", "assert"]
    tui_gate = next(s for s in adw_mod.WORKFLOWS["tui"] if s["kind"] == "gate")
    assert tui_gate["cmd"][0] == "sh" and "panedrive run" in tui_gate["cmd"][2]


def test_validate_fails_fast_on_missing_gate_binary(adw_mod):
    steps = [
        {"kind": "agent", "name": "build", "prompt": "{task}"},
        {"kind": "gate", "name": "g", "cmd": ["definitely-not-a-real-binary-xyz"],
         "on_fail": "build", "max": 2},
    ]
    errs = adw_mod.validate(steps, "demo")
    assert any("not on PATH" in e for e in errs)


def test_run_gate_exit_code_contract(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, _ = make_run(adw_mod, repo, run_dir, "demo", "demo")
    passed, _ = adw_mod.run_gate({"name": "t", "cmd": ["true"]}, state, run_dir, 1)
    assert passed is True
    failed, _ex = adw_mod.run_gate({"name": "f", "cmd": ["false"]}, state, run_dir, 1)
    assert failed is False


def test_sim_workflow_validates_when_simharness_present(adw_mod):
    if not shutil.which("simharness"):
        return
    assert adw_mod.validate(adw_mod.WORKFLOWS["sim"], "demo") == []


def test_tui_workflow_validates_when_panedrive_present(adw_mod):
    if not shutil.which("panedrive"):
        return
    assert adw_mod.validate(adw_mod.WORKFLOWS["tui"], "demo") == []
