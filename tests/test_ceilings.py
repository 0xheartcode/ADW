"""#8 — turn/wall ceilings (the dollar ceiling is blind under a subscription)."""
import time

from helpers import git_repo, limits, make_run, register_stub, write_stub_worker
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---- breaker() unit + property ----------------------------------------
def test_breaker_none_when_under_all_ceilings(adw_mod):
    state = {"cost_usd": 0.0, "turns": 5}
    assert adw_mod.breaker(state, limits(max_turns=10), time.time()) is None


def test_breaker_turn_ceiling(adw_mod):
    state = {"cost_usd": 0.0, "turns": 11}
    r = adw_mod.breaker(state, limits(max_turns=10), time.time())
    assert r and "turn ceiling" in r


def test_breaker_cost_ceiling(adw_mod):
    state = {"cost_usd": 9.0, "turns": 0}
    r = adw_mod.breaker(state, limits(max_cost=5.0), time.time())
    assert r and "over budget" in r


def test_breaker_wall_ceiling(adw_mod):
    state = {"cost_usd": 0.0, "turns": 0}
    r = adw_mod.breaker(state, limits(max_wall=0.0), time.time() - 1)
    assert r == "over wall-clock limit"


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(turns=st.integers(0, 100_000), max_turns=st.integers(0, 100_000))
def test_turn_ceiling_fires_iff_exceeded(adw_mod, turns, max_turns):
    # Cost under budget, wall not exceeded: the turn ceiling fires iff turns>max.
    state = {"cost_usd": 0.0, "turns": turns}
    lim = {"max_cost": 1e12, "max_turns": max_turns, "max_wall": 1e12}
    r = adw_mod.breaker(state, lim, time.time())
    assert (r is not None and "turn ceiling" in r) == (turns > max_turns)


# ---- integration: a real run parks at each ceiling --------------------
def test_run_parks_at_turn_ceiling(adw_mod, tmp_path):
    # demo worker = one assistant turn per build; the demo gate fails twice then
    # passes, so build runs 3x -> turns reach 3. max_turns=2 must park.
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    state, steps = make_run(adw_mod, repo, run_dir, "demo", "demo")
    out = adw_mod.orchestrate(state, steps, run_dir, limits(max_turns=2))
    assert out["status"] == "parked"
    assert "turn ceiling" in out["stopped_because"]
    assert out["turns"] > 2


def test_run_parks_at_wall_ceiling(adw_mod, tmp_path):
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "slow.py",
        "import time\ntime.sleep(0.3)\n"
        "print(json.dumps({'type':'result','subtype':'success',"
        "'is_error':False,'total_cost_usd':0.0}))\n")
    register_stub(adw_mod, "slow", stub)
    state, steps = make_run(adw_mod, repo, run_dir, "demo", "slow")
    out = adw_mod.orchestrate(state, steps, run_dir, limits(max_wall=0.1))
    assert out["status"] == "parked"
    assert out["stopped_because"] == "over wall-clock limit"


def test_turns_accumulate_from_num_turns(adw_mod, tmp_path):
    """A worker reporting num_turns (the subscription shape) feeds the ceiling."""
    repo = git_repo(tmp_path)
    run_dir = tmp_path / "run"
    stub = write_stub_worker(
        tmp_path / "turny.py",
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'total_cost_usd':0.0,'num_turns':7}))\n")
    register_stub(adw_mod, "turny", stub)
    state, _ = make_run(adw_mod, repo, run_dir, "demo", "turny")
    adw_mod.run_agent("x", state, run_dir, {"max_cost": 5.0, "max_attempt_cost": 1.0})
    assert state["turns"] == 7
