"""detect_project_type() + the advisory `detect` subcommand.

Tiny temp repos (just the marker files) exercise each branch and the
most-specific-wins precedence. detect is advisory: it never touches a run.
"""
from types import SimpleNamespace


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def test_sim_wins(adw_mod, tmp_path):
    _touch(tmp_path / "simharness.config.ts")
    assert adw_mod.detect_project_type(tmp_path) == "sim"


def test_sim_beats_everything(adw_mod, tmp_path):
    # even with tui/rust/feature markers present, sim is most specific
    _touch(tmp_path / "simharness.config.ts")
    _touch(tmp_path / "app.py")
    _touch(tmp_path / "ui.pds")
    _touch(tmp_path / "Cargo.toml")
    _touch(tmp_path / "test_x.py")
    assert adw_mod.detect_project_type(tmp_path) == "sim"


def test_tui_needs_both_app_and_pds(adw_mod, tmp_path):
    _touch(tmp_path / "app.py")
    _touch(tmp_path / "ui.pds")
    assert adw_mod.detect_project_type(tmp_path) == "tui"


def test_app_py_alone_is_not_tui(adw_mod, tmp_path):
    _touch(tmp_path / "app.py")
    assert adw_mod.detect_project_type(tmp_path) == "chore"


def test_pds_alone_is_not_tui(adw_mod, tmp_path):
    _touch(tmp_path / "ui.pds")
    assert adw_mod.detect_project_type(tmp_path) == "chore"


def test_tui_beats_rust_and_feature(adw_mod, tmp_path):
    _touch(tmp_path / "app.py")
    _touch(tmp_path / "ui.pds")
    _touch(tmp_path / "Cargo.toml")
    _touch(tmp_path / "test_x.py")
    assert adw_mod.detect_project_type(tmp_path) == "tui"


def test_rust(adw_mod, tmp_path):
    _touch(tmp_path / "Cargo.toml")
    assert adw_mod.detect_project_type(tmp_path) == "rust"


def test_rust_beats_feature(adw_mod, tmp_path):
    _touch(tmp_path / "Cargo.toml")
    _touch(tmp_path / "test_x.py")
    assert adw_mod.detect_project_type(tmp_path) == "rust"


def test_feature_from_test_prefix(adw_mod, tmp_path):
    _touch(tmp_path / "test_core.py")
    assert adw_mod.detect_project_type(tmp_path) == "feature"


def test_feature_from_test_suffix(adw_mod, tmp_path):
    _touch(tmp_path / "core_test.py")
    assert adw_mod.detect_project_type(tmp_path) == "feature"


def test_chore_is_the_fallback(adw_mod, tmp_path):
    _touch(tmp_path / "README.md")
    _touch(tmp_path / "main.py")
    assert adw_mod.detect_project_type(tmp_path) == "chore"


def test_empty_repo_is_chore(adw_mod, tmp_path):
    assert adw_mod.detect_project_type(tmp_path) == "chore"


def test_accepts_str_path(adw_mod, tmp_path):
    _touch(tmp_path / "Cargo.toml")
    assert adw_mod.detect_project_type(str(tmp_path)) == "rust"


def test_every_result_is_a_real_workflow(adw_mod, tmp_path):
    # the guess must be a workflow `run --workflow` would accept
    assert adw_mod.detect_project_type(tmp_path) in adw_mod.WORKFLOWS


def test_cmd_detect_prints_name(adw_mod, tmp_path, capsys):
    _touch(tmp_path / "app.py")
    _touch(tmp_path / "ui.pds")
    rc = adw_mod.cmd_detect(SimpleNamespace(repo=tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "tui"


def test_cmd_detect_default_repo_is_cwd(adw_mod, tmp_path, capsys, monkeypatch):
    _touch(tmp_path / "Cargo.toml")
    monkeypatch.chdir(tmp_path)
    rc = adw_mod.cmd_detect(SimpleNamespace(repo="."))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "rust"
