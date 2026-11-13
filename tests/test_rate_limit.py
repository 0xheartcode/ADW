"""#4 - park cleanly on a usage/rate limit; never retry into the wall.

The subscription signal is the literal line `Claude AI usage limit reached|<epoch>`
(verified against the real claude CLI's documented output). Live limits cannot be
triggered safely, so the orchestration paths are exercised with a stub claude
binary emitting that exact line -> labelled stub-verified in the e2e log.
"""
from helpers import (
    RESULT_OK,
    git_repo,
    limits,
    make_run,
    register_stub,
    write_stub_worker,
)

RESET = 1799999999


def test_detect_subscription_line_with_epoch(adw_mod):
    hit, reset = adw_mod.rate_limit_reset(f"Claude AI usage limit reached|{RESET}")
    assert hit and reset == RESET
    hit2, reset2 = adw_mod.rate_limit_reset("Claude AI usage limit reached")
    assert hit2 and reset2 is None


def test_building_a_ratelimiter_does_not_trip(adw_mod):
    # The full phrase is required - ordinary feature work must never false-trip.
    assert adw_mod.rate_limit_reset("implement rate limiting on /login") == (False, None)
    assert adw_mod.rate_limit_reset("added a 429 rate-limit response") == (False, None)


def test_detect_structured_429_and_subtype(adw_mod):
    assert adw_mod.result_rate_limited({"api_error_status": 429})[0]
    assert adw_mod.result_rate_limited({"subtype": "error_usage_limit"})[0]
    assert adw_mod.result_rate_limited({"subtype": "success"}) == (False, None)


def test_run_agent_synthesizes_rate_limited_on_nonzero_exit(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "rl.py",
        f"print('Claude AI usage limit reached|{RESET}')\nsys.exit(1)\n")
    register_stub(adw_mod, "rl", stub)
    state, _ = make_run(adw_mod, repo, run_dir, "demo", "rl")
    # A non-zero exit with no result event would normally raise; a detected
    # limit must instead come back as a clean, distinct result.
    res = adw_mod.run_agent("do x", state, run_dir,
                            {"max_cost": 5.0, "max_attempt_cost": 1.0})
    assert res["rate_limited"] is True
    assert res["is_error"] is True
    assert res["reset_at"] == RESET


def test_orchestrate_parks_without_retrying(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "rl.py",
        f"print('Claude AI usage limit reached|{RESET}')\nsys.exit(1)\n")
    register_stub(adw_mod, "rl", stub)
    state, steps = make_run(adw_mod, repo, run_dir, "demo", "rl")
    out = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert out["status"] == "parked"
    assert "usage/rate limit" in out["stopped_because"]
    assert out["pending"]["actions"] == ["resume", "reject"]
    assert out["pending"]["reset_at"] == RESET
    # The crux: it did NOT retry the agent into the wall.
    assert out["attempts"]["build"] == 1


def test_rate_limited_run_is_resumable(adw_mod, tmp_path, monkeypatch):
    """After the limit resets, resuming with a working worker completes."""
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "rl.py",
        f"print('Claude AI usage limit reached|{RESET}')\nsys.exit(1)\n")
    register_stub(adw_mod, "rl", stub)
    state, steps = make_run(adw_mod, repo, run_dir, "demo", "rl")
    parked = adw_mod.orchestrate(state, steps, run_dir, limits())
    assert parked["status"] == "parked"
    # Simulate the reset: a working worker + resume from where it parked.
    parked["worker"] = "demo"
    parked["status"] = "running"
    parked.pop("stopped_because", None)
    parked.pop("pending", None)
    out = adw_mod.orchestrate(parked, parked["steps"], run_dir, limits())
    assert out["status"] == "done"


def test_false_positive_result_is_not_rate_limited(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "ok.py",
        "print('working on the rate limiting feature')\n" + RESULT_OK)
    register_stub(adw_mod, "ok", stub)
    state, _ = make_run(adw_mod, repo, run_dir, "demo", "ok")
    res = adw_mod.run_agent("do x", state, run_dir,
                            {"max_cost": 5.0, "max_attempt_cost": 1.0})
    assert not res.get("rate_limited")
