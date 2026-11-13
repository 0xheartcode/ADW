"""#17 - park/resume UX: resume hints, a park-reason taxonomy, list filter."""
import json
from pathlib import Path
from types import SimpleNamespace


def test_classify_park_taxonomy(adw_mod):
    c = adw_mod.classify_park
    assert c("worker hit a usage/rate limit; parked without retry") == "rate-limit"
    assert c("check not converging (failure signature stuck at 3)") == "thrash"
    assert c("check failed identically twice") == "thrash"
    assert c("planner could not produce a valid plan") == "bad-plan"
    assert c("over turn ceiling (3 > 2)") == "breaker"
    assert c("over wall-clock limit") == "breaker"
    assert c("awaiting review at 'review'") == "review"
    assert c("interview needs a human at the keyboard") == "interview"


def test_resume_hint_review_has_approve_flag(adw_mod, tmp_path):
    rd = tmp_path / "runs" / "r1"
    review = adw_mod.resume_hint(rd, {"actions": ["approve", "reject"]})
    other = adw_mod.resume_hint(rd, {"actions": ["resume", "reject"]})
    assert review.endswith("--approve")
    assert "resume" in other and "--approve" not in other


def test_park_records_kind_and_prints_resume(adw_mod, base_state, capsys):
    state, run_dir = base_state
    adw_mod._park(state, run_dir, "over wall-clock limit", {"actions": ["resume", "reject"]})
    assert state["park_kind"] == "breaker"
    out = capsys.readouterr().out
    assert "PARKED [breaker]" in out and "resume:  adw.py resume" in out


def test_list_filters_by_park_kind(adw_mod, tmp_path, monkeypatch, capsys):
    idx = tmp_path / "index.jsonl"
    idx.write_text(
        json.dumps({"run": "a", "task": "t", "status": "parked", "cost_usd": 0,
                    "workflow": "demo", "park_kind": "thrash"}) + "\n" +
        json.dumps({"run": "b", "task": "t", "status": "parked", "cost_usd": 0,
                    "workflow": "demo", "park_kind": "rate-limit"}) + "\n")
    monkeypatch.setattr(adw_mod, "INDEX", Path(idx))
    adw_mod.cmd_list(SimpleNamespace(park_kind="thrash"))
    out = capsys.readouterr().out
    assert "a " in out and "b " not in out and "[thrash]" in out
