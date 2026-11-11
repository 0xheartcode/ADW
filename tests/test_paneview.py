"""#11 — ADW exposes a paneview JSON seam so panedrive can drive/assert it."""
import json
import shutil
import subprocess


def test_seam_written_with_scalar_leaves(adw_mod, base_state, tmp_path, monkeypatch):
    state, run_dir = base_state
    state.update(status="parked", step="review", cost_usd=0.0, turns=3,
                 stopped_because="awaiting review at 'review'",
                 pending={"actions": ["approve", "reject"]})
    seam = tmp_path / "seam.json"
    monkeypatch.setenv("PANEDRIVE_STATE", str(seam))
    adw_mod.save_state(state, run_dir)

    data = json.loads(seam.read_text())
    assert data["status"] == "parked"
    assert data["step"] == "review"
    assert data["turns"] == 3
    assert data["pending_actions"] == "approve,reject"
    # Flat object of scalars (the seam contract) - no nested containers.
    assert all(not isinstance(v, (dict, list)) for v in data.values())


def test_no_seam_when_env_unset(adw_mod, base_state, monkeypatch):
    state, run_dir = base_state
    monkeypatch.delenv("PANEDRIVE_STATE", raising=False)
    monkeypatch.delenv("ADW_SEAM", raising=False)
    adw_mod.save_state(state, run_dir)          # must not raise, writes no seam
    assert not (run_dir / "seam.json").exists()


def test_seam_passes_panedrive_validate_seam(adw_mod, base_state, tmp_path, monkeypatch):
    if not shutil.which("panedrive"):
        return
    state, run_dir = base_state
    seam = tmp_path / "seam.json"
    monkeypatch.setenv("PANEDRIVE_STATE", str(seam))
    adw_mod.save_state(state, run_dir)
    r = subprocess.run(["panedrive", "validate-seam", str(seam)],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
