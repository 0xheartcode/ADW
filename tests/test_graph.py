"""#10 — graph renders a run's actual (spliced) steps, not just the template."""
from types import SimpleNamespace


def test_graph_of_spliced_run(adw_mod, base_state, capsys):
    state, run_dir = base_state
    state["steps"] = [
        {"kind": "plan", "name": "plan", "prompt": "{task}"},
        {"kind": "agent", "name": "do.m1", "prompt": "{task}"},
        {"kind": "gate", "name": "ok.m1", "cmd": ["true"], "on_fail": "do.m1", "max": 3},
        {"kind": "human", "name": "review"},
    ]
    adw_mod.save_state(state, run_dir)
    rc = adw_mod.cmd_graph(SimpleNamespace(target=str(run_dir)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "effective steps" in out
    assert "do_m1[do.m1]" in out              # sanitised id, real-name label
    assert "ok_m1{ok.m1}" in out              # gate diamond
    assert "ok_m1 -->|fail| do_m1" in out     # on_fail edge
    assert "review((review))" in out          # human circle


def test_graph_static_workflow_with_plan_and_interview(adw_mod, capsys):
    adw_mod.cmd_graph(SimpleNamespace(target="long"))
    out = capsys.readouterr().out
    assert "interview[interview]" in out       # interview renders as a box
    assert "plan[plan]" in out                 # plan renders as a box


def test_graph_unknown_target_errors(adw_mod, capsys):
    rc = adw_mod.cmd_graph(SimpleNamespace(target="no-such-thing"))
    assert rc == 2
    assert "no workflow or run" in capsys.readouterr().out


def test_graph_sanitizes_ids(adw_mod):
    assert adw_mod._node_id("do.m1") == "do_m1"
    assert adw_mod._node_id("ok.m-2") == "ok_m_2"
