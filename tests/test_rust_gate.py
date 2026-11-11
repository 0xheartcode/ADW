"""#15 — csd Rust architectural deep gate + convergence signal."""
import shutil


def test_rust_workflow_pinned_argv(adw_mod):
    assert "rust" in adw_mod.WORKFLOWS
    gate = next(s for s in adw_mod.WORKFLOWS["rust"] if s["kind"] == "gate")
    assert gate["cmd"] == ["csd", "diff", "--base", "{base}", "--list"]


def test_rust_workflow_validates_when_csd_present(adw_mod):
    if not shutil.which("csd"):
        return
    assert adw_mod.validate(adw_mod.WORKFLOWS["rust"], "demo") == []


def test_failure_signature_csd_delta_shrinks(adw_mod):
    viol = ("+ fn crate::high::dummy\n~ fn crate::low::base\n"
            "+ edge crate::low -> crate::high (uses)")
    partial = "~ fn crate::low::base\n+ edge crate::low -> crate::high (uses)"
    fixed = "~ fn crate::low::base"
    assert adw_mod.failure_signature(viol) == 3
    assert adw_mod.failure_signature(partial) == 2
    assert adw_mod.failure_signature(fixed) == 1
    assert (adw_mod.failure_signature(fixed)
            < adw_mod.failure_signature(partial)
            < adw_mod.failure_signature(viol))
