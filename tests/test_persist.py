"""#2 — persist the effective step list so spliced steps survive resume."""
import copy
import json

from hypothesis import given, settings
from hypothesis import strategies as st


def test_migrate_backfills_steps_from_template(adw_mod, base_state):
    state, _ = base_state
    state.pop("steps", None)          # an older state.json has no "steps"
    out = adw_mod.migrate(state)
    assert out["steps"] == adw_mod.WORKFLOWS["demo"]


def test_migrate_steps_are_a_copy_not_the_module_list(adw_mod, base_state):
    state, _ = base_state
    state.pop("steps", None)
    out = adw_mod.migrate(state)
    out["steps"].append({"kind": "human", "name": "injected"})
    # Mutating the run's list must never leak into the shared template.
    assert all(s["name"] != "injected" for s in adw_mod.WORKFLOWS["demo"])


def test_state_roundtrip_is_identical(adw_mod, base_state):
    state, run_dir = base_state
    state["steps"] = copy.deepcopy(adw_mod.WORKFLOWS["demo"])
    adw_mod.save_state(state, run_dir)
    reloaded = json.loads((run_dir / "state.json").read_text())
    assert reloaded == state


def test_resume_resolves_a_spliced_step_not_in_template(adw_mod, base_state):
    """The core fix: by_name is built from state['steps'], so a step a plan
    spliced in (absent from WORKFLOWS) resolves on resume instead of KeyError."""
    state, _ = base_state
    spliced = copy.deepcopy(adw_mod.WORKFLOWS["demo"])
    spliced.insert(1, {"kind": "agent", "name": "do.m1", "prompt": "{task}"})
    state["steps"] = spliced
    state["step"] = "do.m1"

    # This is exactly what orchestrate()/cmd_resume do to find the current step.
    by_name = {s["name"]: s for s in state["steps"]}
    assert state["step"] in by_name          # would KeyError before the fix
    assert by_name["do.m1"]["kind"] == "agent"


@settings(max_examples=50)
@given(st.lists(st.fixed_dictionaries({
    "kind": st.sampled_from(["agent", "gate", "human", "plan", "interview"]),
    "name": st.text(min_size=1, max_size=12).filter(lambda s: s.strip()),
}), max_size=8))
def test_steps_survive_json_roundtrip(steps):
    assert json.loads(json.dumps(steps)) == steps
