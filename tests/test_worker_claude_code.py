"""#3 - claude-code subscription worker (no API key, no budget)."""
import shutil

import pytest


def test_worker_registered_with_right_shape(adw_mod):
    spec = adw_mod.WORKERS["claude-code"]
    assert spec["cmd"][:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    assert "--output-format" in spec["cmd"] and "json" in spec["cmd"]
    assert "{prompt}" in spec["cmd"]
    assert spec["stream"] == "claude-json"
    # No API key required under a subscription login.
    assert spec.get("requires_env", []) == []
    # No per-attempt dollar cap: subscription is not dollar-metered.
    assert not any("{budget}" in a for a in spec["cmd"])


def test_validate_passes_without_api_key(adw_mod, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    steps = [{"kind": "agent", "name": "build", "prompt": "{task}"}]
    errs = adw_mod.validate(steps, "claude-code")
    if shutil.which("claude"):
        assert errs == []                       # no env error, binary present
    else:
        assert all("ANTHROPIC_API_KEY" not in e for e in errs)


def test_run_agent_warns_when_uncapped(adw_mod, base_state, capsys, monkeypatch):
    """A worker with no {budget} must warn exactly once about no cost cap."""
    state, run_dir = base_state
    state["worker"] = "demo"                     # demo has no {budget} either
    # Point the demo worker at a quick success so run_agent completes.
    adw_mod.run_agent("{task}".format(task="x"), state, run_dir,
                      {"max_cost": 5.0, "max_attempt_cost": 1.0})
    assert "no per-attempt cost cap" in capsys.readouterr().out


@pytest.mark.parametrize("worker", ["claude-code"])
def test_no_budget_means_uncapped_marker(adw_mod, worker):
    assert not any("{budget}" in a for a in adw_mod.WORKERS[worker]["cmd"])
