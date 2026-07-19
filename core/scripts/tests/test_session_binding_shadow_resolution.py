"""test_session_binding_shadow_resolution.py — 4.

Resolver-side defense for the binding-shadow class. The g-115-1814 fix guards
the CREATION side (retire stale cross-agent binding.yaml at /start), but the
resolver `_try_phase26_binding_with_reason` had NO defense when a shadow slips
past that retirement (e.g. a foreign agent's binding.yaml for a colliding SID
arrives via own-cloud sync AFTER /start ran). The pre-fix resolver returned the
FIRST match in `ar.iterdir()` order — filesystem-nondeterministic — so identity
resolution silently misattributed (observed 2026-07-19 02:30: an alpha session
resolved to echo, stamping completed_by=echo).

The fix: collect ALL valid matches; when 2+ exist (shadow), resolve
DETERMINISTICALLY to the freshest session (newest started_at = most recent
/start = current owner per --retire-legacy semantics), tie-broken by mtime then
agent name, and append a greppable record to core/logs/binding-shadow.jsonl.

Cases:
  A. happy path (1 binding)          → resolves to that agent (regression guard)
  B. 0 bindings                      → None (regression guard)
  C. shadow, alpha started_at newer  → alpha wins (NOT iterdir-first)
  D. shadow, echo started_at newer   → echo wins (proves started_at-driven, not name)
  E. determinism                     → identical result across repeated calls
  F. shadow log                      → core/logs/binding-shadow.jsonl records the event
  G. shadow, missing started_at      → still deterministic (mtime/name tie-break, no crash)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _session_binding import resolve_binding, _try_phase26_binding_with_reason  # noqa: E402

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _mk_agent(root: Path, name: str) -> None:
    adir = root / "agents" / name
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "local-paths.conf").write_text(
        "WORLD_DIR=/tmp/w\nMETA_DIR=/tmp/m\n", encoding="utf-8"
    )


def _write_binding(root: Path, agent: str, sid: str, started_at: str) -> Path:
    """Write a valid binding.yaml directly for full control over started_at."""
    _mk_agent(root, agent)
    bpath = root / "agents" / agent / "sessions" / sid / "binding.yaml"
    bpath.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"session_id: {sid}",
        f"agent: {agent}",
        "mode: autonomous",
    ]
    if started_at:
        lines.append(f"started_at: {started_at}")
    lines.append("started_by: claude-code")
    bpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bpath


# ── A. Happy path (regression guard — collect-then-return must not break 1-match) ──

def test_single_binding_resolves_unchanged(tmp_path):
    _write_binding(tmp_path, "alpha", SID, "2026-07-19T10:00:00")
    b = resolve_binding(SID, tmp_path)
    assert b is not None
    assert b.agent == "alpha"
    assert b.mode == "autonomous"
    assert b.started_at == "2026-07-19T10:00:00"


# ── B. No bindings (regression guard) ──

def test_no_binding_resolves_none(tmp_path):
    _mk_agent(tmp_path, "alpha")  # agent exists but no binding.yaml for SID
    b, reason = _try_phase26_binding_with_reason(SID, tmp_path)
    assert b is None
    assert reason == "binding-yaml-missing"


# ── C. Shadow: freshest started_at wins (alpha newer) ──

def test_shadow_alpha_newer_wins(tmp_path):
    _write_binding(tmp_path, "echo", SID, "2026-07-18T02:00:00")   # stale
    _write_binding(tmp_path, "alpha", SID, "2026-07-19T02:30:00")  # live (newer)
    b = resolve_binding(SID, tmp_path)
    assert b is not None
    assert b.agent == "alpha", f"freshest (alpha) must win, got {b.agent}"


# ── D. Shadow: freshest started_at wins (echo newer) — proves it's started_at-driven ──

def test_shadow_echo_newer_wins(tmp_path):
    _write_binding(tmp_path, "alpha", SID, "2026-07-18T02:00:00")  # stale
    _write_binding(tmp_path, "echo", SID, "2026-07-19T02:30:00")   # live (newer)
    b = resolve_binding(SID, tmp_path)
    assert b is not None
    assert b.agent == "echo", f"freshest (echo) must win, got {b.agent}"


# ── E. Determinism across repeated calls ──

def test_shadow_resolution_deterministic(tmp_path):
    _write_binding(tmp_path, "echo", SID, "2026-07-18T02:00:00")
    _write_binding(tmp_path, "alpha", SID, "2026-07-19T02:30:00")
    results = {resolve_binding(SID, tmp_path).agent for _ in range(8)}
    assert results == {"alpha"}, f"resolution must be stable, saw {results}"


# ── F. Shadow log written ──

def test_shadow_logs_event(tmp_path):
    _write_binding(tmp_path, "echo", SID, "2026-07-18T02:00:00")
    _write_binding(tmp_path, "alpha", SID, "2026-07-19T02:30:00")
    resolve_binding(SID, tmp_path)
    log = tmp_path / "core" / "logs" / "binding-shadow.jsonl"
    assert log.is_file(), "shadow must be logged for creation-side gap visibility"
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["event"] == "binding-shadow-resolved"
    assert rec["sid"] == SID
    assert rec["chosen_agent"] == "alpha"
    assert set(rec["shadow_agents"]) == {"alpha", "echo"}


# ── G. Shadow with missing started_at → still deterministic, no crash ──

def test_shadow_missing_started_at_deterministic(tmp_path):
    _write_binding(tmp_path, "alpha", SID, "")  # no started_at
    _write_binding(tmp_path, "echo", SID, "")   # no started_at
    r1 = resolve_binding(SID, tmp_path)
    r2 = resolve_binding(SID, tmp_path)
    assert r1 is not None and r2 is not None
    assert r1.agent == r2.agent, "must be deterministic even without started_at"
    # With equal (empty) started_at and mtime, tie-break falls to agent name
    # descending → 'echo' > 'alpha'.
    assert r1.agent == "echo", f"name tie-break should pick echo, got {r1.agent}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
