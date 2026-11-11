"""Enforce the runtime invariant: adw.py and demo/ import only the stdlib.

This is what lets the test suite use pytest/hypothesis (dev-only) without
breaking adw.py's "Zero third-party dependencies" promise (adw.py:16).
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STDLIB = set(sys.stdlib_module_names)


def _top_level_imports(path: Path):
    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_adw_imports_only_stdlib():
    mods = _top_level_imports(ROOT / "adw.py")
    non_std = {m for m in mods if m not in STDLIB and m != "adw"}
    assert not non_std, f"adw.py imports non-stdlib modules: {non_std}"


def test_demo_imports_only_stdlib():
    for py in (ROOT / "demo").glob("*.py"):
        mods = _top_level_imports(py)
        non_std = {m for m in mods if m not in STDLIB}
        assert not non_std, f"{py.name} imports non-stdlib modules: {non_std}"
