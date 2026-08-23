"""Unit + CLI tests for session-manifest-write-gate.py — g-115-840 + g-115-6405.

Asserts the gate honors `type: dir` manifest entries end-to-end (g-115-840) AND
`glob: true` fnmatch-pattern entries (g-115-6405). Closes two half-
implementations: writes under registered dir-type subtrees were blocked
(g-115-784, verified 2026-05-16), and files registered via a glob pattern were
blocked because the gate exact-matched the literal pattern string while
snapshot/clear/orphan-scan honored it as an fnmatch pattern (g-115-6405).

Cases:
  1. scratch-child-file:        <agent>/session/scratch/foo.txt → allowed (registered_dir)
  2. scratch-grandchild-file:   <agent>/session/scratch/sub/foo.txt → allowed (registered_dir)
  3. scratch-sibling-file:      <agent>/session/scratch-other/foo.txt → blocked (control)
  4. unregistered-deep-path:    <agent>/session/foo/bar/baz.txt → blocked (control)
  5. glob-registered:           <agent>/session/body-heartbeat-<sid>.json → allowed (registered_glob)
  6. glob-control:              <agent>/session/body-heartbeat.json → blocked (no -<sid> segment)
  7. unregistered-message:      block message names sync_tiers + wedge consequence
  8. mode-dispatch:             assistant→warn/block, reader→info/allow, unknown→info/allow
  9. out-of-scope:              path outside session/ → allowed (out_of_scope)

Plus unit tests for the helpers:
  - _load_manifest_entries returns (file_names, dir_names, glob_patterns, sync_tiers)
  - _path_under_registered_dir matches at any depth, exact-segment match
  - _find_owning_agent returns the 3-tuple (agent, basename, ancestor_dirs)

Pattern: importlib + sys.path (matches test_defer_recheck_patterns.py) so the
hyphen-named script loads under a hyphen-free attribute name. Subprocess
invocations mimic the production hook
(`session-manifest-write-gate-hook.py` shells out via subprocess.run).

domain-leak-exempt: pedagogical examples use agent name "delta" matching the
canonical g-115-784 probe trace — keeps the scenario faithful to the
incident this test pins.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SCRIPTS = REPO_ROOT / "core" / "scripts"
GATE_PATH = CORE_SCRIPTS / "session-manifest-write-gate.py"

sys.path.insert(0, str(CORE_SCRIPTS))


def _import_gate():
    """Load session-manifest-write-gate.py via importlib (hyphen-free attribute)."""
    spec = importlib.util.spec_from_file_location(
        "session_manifest_write_gate_mod", GATE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for session-manifest-write-gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture: minimal tmp PROJECT_ROOT with manifest + agent dir + agent-mode
# ---------------------------------------------------------------------------

def _build_tmp_repo(tmp_path: Path, agent_mode: str = "autonomous"):
    """Build a tmp repo with a session-manifest registering a file entry, a
    dir-type entry, plus an agent dir with local-paths.conf + agent-mode."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Manifest with one file-type entry, one dir-type entry, one "no type" entry
    # (default to file). Matches the real manifest shape.
    manifest_dir = repo / "core" / "config"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "session-manifest.yaml"
    manifest_path.write_text(
        "files:\n"
        "  - file: working-memory.yaml\n"
        "    writer: agent\n"
        "    sync_tier: continuity\n"
        "  - file: scratch\n"
        "    type: dir\n"
        "    writer: agent\n"
        "    recovery_action: clear\n"
        "    sync_tier: machine_local\n"
        "  - file: no-type-entry.txt\n"
        "    writer: agent\n"
        "    sync_tier: ephemeral\n"
        "  - file: body-heartbeat-*.json\n"
        "    glob: true\n"
        "    writer: agent\n"
        "    sync_tier: continuity\n",
        encoding="utf-8",
    )

    # Agent dir with the sanity-check local-paths.conf
    # Phase 2.5.D: agent dirs live under agents/ parent.
    (repo / "agents").mkdir(exist_ok=True)
    agent = repo / "agents" / "delta"
    (agent / "session").mkdir(parents=True)
    (agent / "local-paths.conf").write_text(
        "WORLD_PATH=/tmp/world\nMETA_PATH=/tmp/meta\n", encoding="utf-8",
    )
    (agent / "session" / "agent-mode").write_text(agent_mode, encoding="utf-8")

    return repo, manifest_path


@pytest.fixture
def gate_fixture(tmp_path: Path, monkeypatch):
    """Reload the gate module against a tmp PROJECT_ROOT + manifest."""
    repo, manifest_path = _build_tmp_repo(tmp_path, agent_mode="autonomous")
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    # Reload the module so PROJECT_ROOT-bound module-level constants pick up
    # the tmp repo (the gate caches Path(PROJECT_ROOT) at import time).
    mod = _import_gate()
    monkeypatch.setattr(mod, "PROJECT_ROOT", str(repo))
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest_path)
    return mod, repo


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------

def test_load_manifest_entries_splits_by_type(gate_fixture):
    """g-115-840 + g-115-6405: file / dir / glob entries split into separate
    collections, and the distinct sync_tiers are returned sorted. The legacy
    `_load_manifest_basenames` collapsed file/dir; before g-115-6405 the glob
    pattern leaked into file_names as a literal string that never matched.
    """
    mod, _ = gate_fixture
    files, dirs, globs, tiers = mod._load_manifest_entries()
    assert files == {"working-memory.yaml", "no-type-entry.txt"}, (
        "no-type entries must default to file-type; the glob pattern must NOT "
        "leak into file_names"
    )
    assert dirs == {"scratch"}, "scratch is the sole dir-type entry"
    assert globs == ["body-heartbeat-*.json"], "glob:true entries go to glob_patterns"
    assert tiers == ["continuity", "ephemeral", "machine_local"], (
        "sync_tiers are the sorted distinct declared tiers (drives the block message)"
    )


def test_load_manifest_entries_fail_open_on_missing(gate_fixture, monkeypatch):
    """Missing manifest → (None, None) so caller fails open."""
    mod, _ = gate_fixture
    monkeypatch.setattr(mod, "MANIFEST_PATH", Path("/nonexistent/manifest.yaml"))
    result = mod._load_manifest_entries()
    assert result == (None, None, None, None)


def test_path_under_registered_dir_matches_any_depth():
    """g-115-840 helper: matches at any path depth, exact-segment match."""
    mod = _import_gate()
    # Direct child
    assert mod._path_under_registered_dir(("scratch",), {"scratch"}) is True
    # Grandchild
    assert mod._path_under_registered_dir(("scratch", "sub"), {"scratch"}) is True
    # Deep nesting
    assert mod._path_under_registered_dir(
        ("scratch", "a", "b", "c"), {"scratch"}
    ) is True
    # Non-matching siblings
    assert mod._path_under_registered_dir(("scratch-other",), {"scratch"}) is False
    # Empty ancestors (file directly under session/)
    assert mod._path_under_registered_dir((), {"scratch"}) is False
    # Empty dir_names
    assert mod._path_under_registered_dir(("scratch",), set()) is False


def test_find_owning_agent_returns_ancestor_dirs(gate_fixture):
    """g-115-840: the resolver must surface intermediate segments so the
    dir-match check has data to compare against."""
    mod, repo = gate_fixture

    # File directly under session/ → empty ancestors
    p = repo / "agents" / "delta" / "session" / "working-memory.yaml"
    agent, base, ancestors = mod._find_owning_agent(p)
    assert agent == "delta"
    assert base == "working-memory.yaml"
    assert ancestors == ()

    # File under scratch/ → ("scratch",)
    p = repo / "agents" / "delta" / "session" / "scratch" / "foo.txt"
    agent, base, ancestors = mod._find_owning_agent(p)
    assert agent == "delta"
    assert base == "foo.txt"
    assert ancestors == ("scratch",)

    # File under scratch/sub/ → ("scratch", "sub")
    p = repo / "agents" / "delta" / "session" / "scratch" / "sub" / "foo.txt"
    agent, base, ancestors = mod._find_owning_agent(p)
    assert agent == "delta"
    assert base == "foo.txt"
    assert ancestors == ("scratch", "sub")

    # Out-of-scope → (None, None, None)
    p = repo / "core" / "scripts" / "foo.py"
    assert mod._find_owning_agent(p) == (None, None, None)


# ---------------------------------------------------------------------------
# Integration tests — in-process main() invocation
# ---------------------------------------------------------------------------
# Subprocess-style invocation can't override the gate's module-level
# PROJECT_ROOT constant (computed from _paths.py import-time location).
# We exercise main() directly with monkeypatched module constants so the
# gate sees the tmp repo. main() ends in sys.exit(); we catch SystemExit.


def _invoke_main(mod, target_path, capsys):
    """Run main(argv=[target, --output, json]) with the gate module's
    PROJECT_ROOT + MANIFEST_PATH already monkeypatched to the tmp repo.
    Returns (exit_code, parsed_json)."""
    with pytest.raises(SystemExit) as exc_info:
        mod.main(argv=[str(target_path), "--output", "json"])
    captured = capsys.readouterr()
    payload = (captured.out or "").strip()
    data = json.loads(payload) if payload else {}
    return exc_info.value.code, data


def test_scratch_child_file_passes(gate_fixture, capsys):
    """Case 1: <agent>/session/scratch/foo.txt — registered dir → allowed."""
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "scratch" / "probe-payload-staging.txt"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 0, f"expected exit 0 (allowed), got {rc}; data={data}"
    assert data["allowed"] is True
    assert data["reason"] == "registered_dir:scratch", (
        f"expected registered_dir:scratch reason, got {data['reason']}"
    )
    assert data["agent"] == "delta"
    assert data["basename"] == "probe-payload-staging.txt"


def test_scratch_grandchild_file_passes(gate_fixture, capsys):
    """Case 2: <agent>/session/scratch/sub/foo.txt — deeper nesting still allowed."""
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "scratch" / "sub" / "deep.txt"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 0, f"expected exit 0 (allowed), got {rc}; data={data}"
    assert data["allowed"] is True
    assert data["reason"] == "registered_dir:scratch"
    assert data["basename"] == "deep.txt"


def test_scratch_sibling_file_blocks(gate_fixture, capsys):
    """Case 3: <agent>/session/scratch-other/foo.txt — similar name, NOT registered.
    Control: exact-segment match, no prefix-fuzziness allowed.
    """
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "scratch-other" / "foo.txt"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 1, f"expected exit 1 (blocked), got {rc}; data={data}"
    assert data["allowed"] is False
    assert data["severity"] == "block"
    assert "unregistered file 'foo.txt'" in data["reason"]


def test_unregistered_deep_path_blocks(gate_fixture, capsys):
    """Case 4: <agent>/session/foo/bar/baz.txt — fully unregistered.
    Control: depth alone does not buy access; only registered dir segments do.
    """
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "foo" / "bar" / "baz.txt"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 1, f"expected exit 1 (blocked), got {rc}; data={data}"
    assert data["allowed"] is False
    assert data["severity"] == "block"


# ---------------------------------------------------------------------------
# Regression: existing exact-file registration still wins
# ---------------------------------------------------------------------------

def test_registered_file_still_allowed(gate_fixture, capsys):
    """Regression: file-type entries continue to allow with reason='registered'
    (NOT registered_dir). File-specific registration takes precedence in
    dispatch order.
    """
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "working-memory.yaml"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 0
    assert data["allowed"] is True
    assert data["reason"] == "registered", (
        f"file-registered reason should not change, got {data['reason']}"
    )


# ---------------------------------------------------------------------------
# g-115-6405 — glob: true fnmatch entries + enriched message + mode dispatch
# ---------------------------------------------------------------------------

def test_glob_registered_file_passes(gate_fixture, capsys):
    """Case 5: a basename matching a glob:true fnmatch pattern is registered and
    must pass with reason registered_glob:<pattern>. Before the fix the gate
    exact-matched the literal pattern string and falsely blocked real files
    (dogfooded: body-heartbeat-<sid>.json blocked in autonomous mode)."""
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "body-heartbeat-sess42.json"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 0, f"expected exit 0 (allowed), got {rc}; data={data}"
    assert data["allowed"] is True
    assert data["reason"] == "registered_glob:body-heartbeat-*.json", (
        f"expected registered_glob reason, got {data['reason']}"
    )
    assert data["basename"] == "body-heartbeat-sess42.json"


def test_glob_control_nonmatching_blocks(gate_fixture, capsys):
    """Case 6 (control): a basename that does NOT match any glob pattern still
    blocks. `body-heartbeat.json` lacks the `-<sid>` segment that
    `body-heartbeat-*.json` requires, so fnmatch rejects it — proving glob
    matching is precise, not a blanket prefix allow."""
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "body-heartbeat.json"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 1, f"expected exit 1 (blocked), got {rc}; data={data}"
    assert data["allowed"] is False
    assert data["severity"] == "block"
    assert "unregistered file 'body-heartbeat.json'" in data["reason"]


def test_unregistered_message_names_tiers_and_wedge(gate_fixture, capsys):
    """Case 7: the block message must name the valid sync_tiers (derived from
    the manifest, not hardcoded — guard-426) AND the wedge consequence, so it
    tells the writer HOW to register rather than only that the write failed."""
    mod, repo = gate_fixture
    target = repo / "agents" / "delta" / "session" / "unregistered-probe.json"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 1
    assert data["allowed"] is False
    reason = data["reason"]
    for tier in ("continuity", "ephemeral", "machine_local"):
        assert tier in reason, f"block message must name sync_tier '{tier}'; got: {reason}"
    assert "wedge" in reason or "invisible" in reason, (
        f"block message must state the wedge consequence; got: {reason}"
    )
    # The legacy prefix the hook + existing consumers rely on is preserved.
    assert "unregistered file 'unregistered-probe.json'" in reason


def test_out_of_scope_path_passes(gate_fixture, capsys):
    """Case 9: a path outside any agent's session/ is out-of-scope → allowed,
    reason out_of_scope, agent/basename null."""
    mod, repo = gate_fixture
    target = repo / "core" / "scripts" / "foo.py"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == 0
    assert data["allowed"] is True
    assert data["reason"] == "out_of_scope"
    assert data["agent"] is None


@pytest.mark.parametrize(
    "mode,expect_rc,expect_allowed,expect_sev",
    [
        ("assistant", 1, False, "warn"),   # assistant → warn, still blocks the write
        ("reader", 0, True, "info"),       # reader → info, fail-open allow
        ("bogus-mode", 0, True, "info"),   # unknown → info, fail-open allow
    ],
)
def test_unregistered_mode_dispatch(
    tmp_path, monkeypatch, capsys, mode, expect_rc, expect_allowed, expect_sev
):
    """Case 8: unregistered dispatch by agent-mode. Only autonomous (block) is
    covered by the block-case tests above; this pins the other three branches —
    assistant warns (rc 1, still refused), reader/unknown fail open (rc 0)."""
    repo, manifest_path = _build_tmp_repo(tmp_path, agent_mode=mode)
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    mod = _import_gate()
    monkeypatch.setattr(mod, "PROJECT_ROOT", str(repo))
    monkeypatch.setattr(mod, "MANIFEST_PATH", manifest_path)
    target = repo / "agents" / "delta" / "session" / "unregistered-probe.json"
    rc, data = _invoke_main(mod, target, capsys)
    assert rc == expect_rc, f"mode={mode}: expected rc {expect_rc}, got {rc}; data={data}"
    assert data["allowed"] is expect_allowed
    assert data["severity"] == expect_sev


if __name__ == "__main__":
    # Allow running standalone (no pytest fixtures). Runs only the unit helpers
    # — the integration tests depend on pytest's capsys + monkeypatch fixtures.
    # For the integration suite, run: pytest mind_api/tests/test_session_manifest_write_gate.py
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo, manifest_path = _build_tmp_repo(Path(td))
        mod = _import_gate()
        mod.PROJECT_ROOT = str(repo)
        mod.MANIFEST_PATH = manifest_path

        files, dirs, globs, tiers = mod._load_manifest_entries()
        assert files == {"working-memory.yaml", "no-type-entry.txt"}, files
        assert dirs == {"scratch"}, dirs
        assert globs == ["body-heartbeat-*.json"], globs
        assert tiers == ["continuity", "ephemeral", "machine_local"], tiers
        print("PASS test_load_manifest_entries_splits_by_type (standalone)")

        assert mod._path_under_registered_dir(("scratch",), {"scratch"}) is True
        assert mod._path_under_registered_dir(("scratch", "sub"), {"scratch"}) is True
        assert mod._path_under_registered_dir(("scratch-other",), {"scratch"}) is False
        assert mod._path_under_registered_dir((), {"scratch"}) is False
        print("PASS test_path_under_registered_dir_matches_any_depth (standalone)")

        # Verify _find_owning_agent (re-create local-paths.conf check)
        p = repo / "agents" / "delta" / "session" / "scratch" / "foo.txt"
        agent, base, ancestors = mod._find_owning_agent(p)
        assert agent == "delta" and base == "foo.txt" and ancestors == ("scratch",)
        print("PASS test_find_owning_agent_returns_ancestor_dirs (standalone)")

    print("OK (standalone): 3/3 helper tests passed. Run pytest for the full suite.")
