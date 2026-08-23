"""Pre-init import safety for daemon-imported core/scripts modules ().

The daemon's endpoints/load_all() imports tree.py, retrieve.py and (via
aspirations_write's from-import of ACTIVE_AGENTS) gates/capability_route.py at
process start. On a pre-init deployment — a pristine clone with no
local-paths.conf, no .mind-data/, no agents/ dir — `_paths.WORLD_DIR` is None
(the deliberate hard-cut anti-cruft design, _paths.py:215) and the agents root
does not exist. Module-level derivations from either used to TypeError /
FileNotFoundError at import, killing the daemon before it could serve the very
init endpoints that would have CREATED the world (measured on a zc-03
/opt/coach-mind clone, 2026-08-21).

Fix shape being pinned here, per module:
  - _agents: cross-agent root scans tolerate an absent agents root (OSError →
    the documented "absent" result), so `_active_agents()` returns () pre-init.
  - gates/capability_route: ACTIVE_AGENTS served lazily via PEP 562 module
    __getattr__ — a plain import never scans the filesystem.
  - tree: TREE_PATH computed at ACCESS time (_tree_path()), raising a loud
    RuntimeError when WORLD_DIR is None (loud-at-USE preserved), with a
    globals-shadow so tests' `tree.TREE_PATH = x` setattr contract still works.
  - retrieve: store-path constants are None-guarded at import (matching the
    module's own EXP_PATH/EI_PATH house pattern); consumers None-guard.

The subprocess unit pins the actual pre-init import behavior (constants bake
at import time, and an in-process importlib.reload would poison sibling tests
that hold references into these modules).
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
for _p in (_SCRIPTS, _SCRIPTS / "gates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _agents  # noqa: E402
import _paths  # noqa: E402
import capability_route as cr  # noqa: E402
import retrieve as _retrieve  # noqa: E402
import tree as _tree  # noqa: E402


# --- _agents: absent agents root ------------------------------------------


def test_resolve_world_team_state_absent_root(tmp_path):
    """No agents/ dir at all == team-state absent == None, not OSError."""
    assert _agents._resolve_world_team_state(tmp_path) is None


# --- gates/capability_route: lazy ACTIVE_AGENTS ---------------------------


def test_capability_route_active_agents_is_lazy():
    """A plain import must not eagerly bind ACTIVE_AGENTS (no FS scan at
    import); attribute access serves it via module __getattr__ and returns
    the tuple shape consumers iterate."""
    assert "ACTIVE_AGENTS" not in vars(cr), (
        "ACTIVE_AGENTS is eagerly bound at import again — the pre-init "
        "daemon regression g-367-03 fixed"
    )
    agents = cr.ACTIVE_AGENTS  # goes through __getattr__
    assert isinstance(agents, tuple)


def test_capability_route_unknown_attr_still_raises():
    """PEP 562 __getattr__ must not swallow genuine attribute errors."""
    try:
        cr.__getattr__("NOT_A_REAL_ATTRIBUTE")
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for unknown attribute")


# --- tree: access-time TREE_PATH ------------------------------------------


def test_tree_path_raises_loud_when_world_unconfigured(monkeypatch):
    """WORLD_DIR None + no shadow → the documented loud RuntimeError."""
    monkeypatch.delitem(_tree.__dict__, "TREE_PATH", raising=False)
    monkeypatch.setattr(_paths, "WORLD_DIR", None)
    try:
        _tree._tree_path()
    except RuntimeError as e:
        assert "world not configured" in str(e)
    else:
        raise AssertionError("expected RuntimeError when WORLD_DIR is None")


def test_tree_path_globals_shadow_contract(monkeypatch):
    """Tests that setattr `tree.TREE_PATH = x` (the pre-existing test
    contract, e.g. test_remove_child_orphan_gate) must keep winning over
    the lazy accessor."""
    monkeypatch.setitem(_tree.__dict__, "TREE_PATH", "/shadow/_tree.yaml")
    assert _tree._tree_path() == "/shadow/_tree.yaml"
    assert _tree.TREE_PATH == "/shadow/_tree.yaml"


def test_tree_path_resolves_from_paths_when_configured(monkeypatch):
    monkeypatch.delitem(_tree.__dict__, "TREE_PATH", raising=False)
    monkeypatch.setattr(_paths, "WORLD_DIR", Path("/w"))
    expect = str(Path("/w") / "knowledge" / "tree" / "_tree.yaml")
    assert _tree._tree_path() == expect
    assert _tree.TREE_PATH == expect  # PEP 562 external-reader route


# --- retrieve: None-guarded framework sources -----------------------------


def test_framework_file_sources_tolerates_none_world_dir(monkeypatch):
    """The world-conventions tier skips silently when the dir is None
    (pre-init), same as when it is missing on disk (fresh world)."""
    monkeypatch.setattr(_retrieve, "FRAMEWORK_WORLD_CONVENTIONS_DIR", None)
    sources = list(_retrieve._framework_file_sources())
    assert sources, "rules + core-convention tiers must still yield"
    assert all(tier in ("rule", "core-convention") for _, tier in sources)


# --- subprocess: the real pre-init import ---------------------------------


def test_preinit_import_subprocess():
    """Import retrieve + tree in a fresh interpreter with WORLD_DIR forced to
    None BEFORE they import — the exact pre-init daemon condition. Pins:
    import succeeds, retrieve constants are None (falsy — the documented
    consumer contract, e.g. embedding-index-build's `if R.TREE_PATH else {}`),
    and tree fails loud only at USE."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_SCRIPTS)!r})
        import _paths
        _paths.WORLD_DIR = None
        import retrieve
        for name in ("TREE_PATH", "RB_PATH", "GUARD_PATH", "SIGS_PATH",
                     "BELIEFS_PATH", "FRAMEWORK_WORLD_CONVENTIONS_DIR"):
            v = getattr(retrieve, name)
            assert v is None, f"{{name}} should be None pre-init, got {{v!r}}"
        src = list(retrieve._framework_file_sources())
        assert src, "rules + core tiers should still yield pre-init"
        assert all(t in ("rule", "core-convention") for _, t in src)
        import tree
        try:
            tree._tree_path()
            raise SystemExit("expected RuntimeError at use")
        except RuntimeError as e:
            assert "world not configured" in str(e)
        tree.TREE_PATH = "shadow"
        assert tree._tree_path() == "shadow"
        print("PREINIT-OK")
        """
    )
    env = {**os.environ, "STORAGE_BACKEND": "local"}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, (
        f"pre-init import failed rc={proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "PREINIT-OK" in proc.stdout
