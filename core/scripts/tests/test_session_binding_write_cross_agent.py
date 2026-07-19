"""test_session_binding_write_cross_agent.py — cross-agent stale-binding retirement.

g-115-1814: when a Claude Code terminal re-binds a SID to a new agent
(`/start bravo` -> `/stop bravo` -> `/start alpha` in one window), the prior
agent's `sessions/<SID>/binding.yaml` was left behind. `_session_binding.
resolve_binding` scans `agents/*/sessions/<SID>/binding.yaml` in filesystem
`iterdir()` order and returns the FIRST match, so the stale binding could
SHADOW the live one — bash-agent-inject then injected the wrong MIND_AGENT
(observed: an alpha SID resolving to bravo=IDLE).

The fix extends `write_binding(..., retire_legacy=True)` to also remove any
OTHER agent's `binding.yaml` for the same SID, guaranteeing exactly one
binding.yaml per SID so the resolver's iterdir order is irrelevant. /start
passes --retire-legacy at each of its 4 binding sites, so the fix reaches the
incident path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _session_binding import resolve_binding  # noqa: E402


# session-binding-write.py is hyphenated — load it via importlib for its funcs.
def _load_writer_module():
    spec = importlib.util.spec_from_file_location(
        "session_binding_write", CORE_SCRIPTS / "session-binding-write.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WRITER = _load_writer_module()

SID_A = "11111111-1111-1111-1111-111111111111"
SID_B = "22222222-2222-2222-2222-222222222222"


def _mk_agent(root: Path, name: str) -> None:
    """Create agents/<name>/ with the local-paths.conf write_binding requires."""
    adir = root / "agents" / name
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "local-paths.conf").write_text(
        "WORLD_DIR=/tmp/w\nMETA_DIR=/tmp/m\n", encoding="utf-8"
    )


def _binding_path(root: Path, agent: str, sid: str) -> Path:
    return root / "agents" / agent / "sessions" / sid / "binding.yaml"


# ── Core incident: re-bind retires the stale cross-agent binding ────────────

def test_rebind_retires_stale_cross_agent_binding(tmp_path):
    """/start bravo then /start alpha (same SID) must leave ONLY alpha's binding."""
    _mk_agent(tmp_path, "alpha")
    _mk_agent(tmp_path, "bravo")

    # 1. /start bravo — writes bravo's binding for SID_A (no retire needed yet).
    WRITER.write_binding(SID_A, "bravo", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    assert _binding_path(tmp_path, "bravo", SID_A).is_file()

    # 2. /start alpha re-binds the SAME SID with --retire-legacy (as /start does).
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=True, project_root=tmp_path)

    # Alpha's binding is the live one; bravo's stale binding is retired.
    assert _binding_path(tmp_path, "alpha", SID_A).is_file()
    assert not _binding_path(tmp_path, "bravo", SID_A).exists()


def test_resolve_after_rebind_returns_new_agent(tmp_path):
    """End-to-end: resolve_binding must return alpha (no bravo shadow)."""
    _mk_agent(tmp_path, "alpha")
    _mk_agent(tmp_path, "bravo")
    WRITER.write_binding(SID_A, "bravo", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=True, project_root=tmp_path)
    b = resolve_binding(SID_A, tmp_path)
    assert b is not None
    assert b.agent == "alpha"


def test_keeps_own_binding(tmp_path):
    """The just-written agent's own binding is never removed by retirement."""
    _mk_agent(tmp_path, "alpha")
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=True, project_root=tmp_path)
    assert _binding_path(tmp_path, "alpha", SID_A).is_file()


def test_retirement_scoped_to_sid(tmp_path):
    """A DIFFERENT SID's binding under another agent must NOT be touched."""
    _mk_agent(tmp_path, "alpha")
    _mk_agent(tmp_path, "bravo")
    # bravo legitimately runs a SEPARATE session (SID_B) in another terminal.
    WRITER.write_binding(SID_B, "bravo", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    # alpha binds SID_A with retire-legacy — must not disturb bravo's SID_B.
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=True, project_root=tmp_path)
    assert _binding_path(tmp_path, "bravo", SID_B).is_file()
    assert _binding_path(tmp_path, "alpha", SID_A).is_file()


def test_no_retirement_without_flag(tmp_path):
    """Without retire_legacy, cross-agent bindings persist (gate documented)."""
    _mk_agent(tmp_path, "alpha")
    _mk_agent(tmp_path, "bravo")
    WRITER.write_binding(SID_A, "bravo", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    # No --retire-legacy → the stale bravo binding is NOT retired.
    assert _binding_path(tmp_path, "bravo", SID_A).is_file()


# ── Helper unit tests (fail-open + return value) ───────────────────────────

def test_retire_helper_returns_retired_paths(tmp_path):
    _mk_agent(tmp_path, "alpha")
    _mk_agent(tmp_path, "bravo")
    WRITER.write_binding(SID_A, "bravo", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    retired = WRITER._retire_cross_agent_bindings(tmp_path, SID_A, keep_agent="alpha")
    assert len(retired) == 1
    assert retired[0].parent.parent.parent.name == "bravo"


def test_retire_helper_fail_open_no_agents_root(tmp_path):
    """Missing agents/ dir → returns [] without raising."""
    assert WRITER._retire_cross_agent_bindings(tmp_path, SID_A, keep_agent="alpha") == []


def test_retire_helper_skips_keep_agent(tmp_path):
    _mk_agent(tmp_path, "alpha")
    WRITER.write_binding(SID_A, "alpha", "autonomous",
                         retire_legacy=False, project_root=tmp_path)
    retired = WRITER._retire_cross_agent_bindings(tmp_path, SID_A, keep_agent="alpha")
    assert retired == []
    assert _binding_path(tmp_path, "alpha", SID_A).is_file()
