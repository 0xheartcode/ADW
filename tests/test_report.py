"""#13 summary command + #14 subscription usage reporting."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from helpers import git_repo, limits, make_run


def test_wall_seconds(adw_mod):
    now = dt.datetime.now().astimezone()
    state = {"started_at": now.isoformat(),
             "ended_at": (now + dt.timedelta(seconds=42)).isoformat()}
    assert 41 < adw_mod.wall_seconds(state) < 43
    assert adw_mod.wall_seconds({}) == 0.0


def test_summary_of_parked_run(adw_mod, tmp_path, capsys):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo-plan", "demo-plan")
    adw_mod.orchestrate(state, steps, run_dir,
                        limits(noninteractive=True, auto_approve=False))
    adw_mod.cmd_summary(SimpleNamespace(run=str(run_dir)))
    out = capsys.readouterr().out
    assert "PARKED" in out
    assert "turns" in out and "wall" in out
    assert "do.m1x1" in out                     # spliced steps in the summary
    assert "resume   adw.py resume" in out


def test_usage_aggregates_index(adw_mod, tmp_path, monkeypatch, capsys):
    idx = tmp_path / "index.jsonl"
    idx.write_text(
        json.dumps({"run": "a", "task": "t", "status": "done", "cost_usd": 0.5,
                    "workflow": "demo", "turns": 4}) + "\n" +
        json.dumps({"run": "b", "task": "t", "status": "parked", "cost_usd": 0,
                    "workflow": "demo", "turns": 2, "park_kind": "thrash"}) + "\n")
    monkeypatch.setattr(adw_mod, "INDEX", Path(idx))
    adw_mod.cmd_usage(None)
    out = capsys.readouterr().out
    assert "runs     2" in out
    assert "done=1" in out and "parked=1" in out
    assert "turns    6" in out
    assert "thrash=1" in out


def test_show_includes_turns_and_wall(adw_mod, tmp_path, capsys):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo", "demo")
    adw_mod.orchestrate(state, steps, run_dir, limits())
    adw_mod.cmd_show(SimpleNamespace(run=str(run_dir), tools=False))
    out = capsys.readouterr().out
    assert "turns" in out and "wall" in out
