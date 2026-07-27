"""Regression test for the handoff key_outcomes contract ().

BACKGROUND. Three sources agree that `key_outcomes` lives NESTED at
`session_summary.key_outcomes`:
  1. the schema example in core/config/conventions/handoff-working-memory.md
  2. boot/SKILL.md's auto-continuation status line, the ONLY consumer, which
     renders `{session_summary.key_outcomes}`
  3. aspirations-consolidate Step 9's own fallback wording ("the prior single
     linear key_outcomes list") — that prior list was already the nested one

`handoff-yaml-build.py::_assemble()` is a FIXED allowlist that passes
`session_summary` through whole but has no top-level `key_outcomes` slot. On
2026-07-26 a consolidation emitted key_outcomes at TOP level: the payload
validated cleanly, 17 fields were written, `flags` came back empty, and the
seven per-cluster journal summaries were silently discarded — never reaching
the next session's boot. Silent, so it survived.

WHAT THIS PINS:
  A. the nested round-trip — session_summary.key_outcomes reaches handoff.yaml
     (fails if session_summary ever stops passing through whole)
  B. a top-level key_outcomes is REPORTED in `dropped_keys` + a stderr WARN
     rather than vanishing (fails if the drop goes silent again)
  C. a clean payload reports dropped_keys == [] and no WARN — so the detector
     cannot pass by crying wolf on every call

Runs the real script in a subprocess against a tmp --output-path. Does NOT
touch live agent state.

Run:
  py -3 -m pytest core/scripts/tests/test_handoff_key_outcomes_roundtrip.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = CORE_ROOT.parent
SCRIPT = CORE_ROOT / "scripts" / "handoff-yaml-build.py"

CLUSTERS = [
    "[infra] Fixed the mirror wedge on cc-02",
    "[framework] Re-baselined sig-005 after its mechanism correction",
    "[product] Drained three asp-335 parity goals",
]


def _payload(**extra):
    """Minimal schema-valid payload; `extra` merges at TOP level."""
    p = {
        "session_number": 116,
        "next_focus": "continue asp-335 parity work",
        "first_action": {"goal_id": "g-115-3385", "reason": "top-ranked"},
        "session_summary": {"goals_completed": 4, "goals_failed": 0},
    }
    p.update(extra)
    return p


def _run(payload, out_path):
    """Invoke the real builder. STORAGE_BACKEND=local per guard-955: this test
    writes via _fileops, and on an own-cloud box an unpinned backend derives the
    S3 key from customer_prefix+env_id+filename — NOT the tmp --output-path — so
    a tmp write would collide with the PRODUCTION handoff key."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--output-path", str(out_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env={**_base_env(), "STORAGE_BACKEND": "local"},
    )


def _base_env():
    import os

    return dict(os.environ)


@pytest.fixture()
def out_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "handoff.yaml"


def test_nested_key_outcomes_round_trips(out_path):
    """A. The canonical path survives assembly."""
    payload = _payload(
        session_summary={
            "goals_completed": 4,
            "goals_failed": 0,
            "key_outcomes": CLUSTERS,
        }
    )
    proc = _run(payload, out_path)
    assert proc.returncode == 0, f"builder failed: {proc.stdout}\n{proc.stderr}"
    assert out_path.exists(), "handoff.yaml was not written"

    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert "session_summary" in written
    assert written["session_summary"].get("key_outcomes") == CLUSTERS, (
        "session_summary.key_outcomes did not round-trip — _assemble() must pass "
        "session_summary through WHOLE (g-115-3385)"
    )
    # The nested form is not an 'unknown key' — it must not be reported dropped.
    assert json.loads(proc.stdout).get("dropped_keys") == []


def test_top_level_key_outcomes_is_reported_not_silent(out_path):
    """B. The exact 2026-07-26 mistake is now LOUD.

    Top-level key_outcomes is still not persisted (it is not in the schema),
    but it must never again disappear without a signal.
    """
    payload = _payload(key_outcomes=CLUSTERS)
    proc = _run(payload, out_path)
    assert proc.returncode == 0, f"builder failed: {proc.stdout}\n{proc.stderr}"

    out = json.loads(proc.stdout)
    assert "key_outcomes" in out.get("dropped_keys", []), (
        "top-level key_outcomes was dropped SILENTLY — the whole point of "
        "g-115-3385 is that an unrecognized top-level key is reported"
    )
    assert "dropped_keys" in out.get("flags", [])
    assert "WARN" in proc.stderr and "key_outcomes" in proc.stderr, (
        "expected a stderr WARN naming the dropped key"
    )

    # And confirm it genuinely is absent from the written file, so the test
    # documents real behavior rather than an aspiration.
    written = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert "key_outcomes" not in written


def test_clean_payload_reports_no_dropped_keys(out_path):
    """C. The detector must stay quiet on well-formed input."""
    proc = _run(_payload(), out_path)
    assert proc.returncode == 0, f"builder failed: {proc.stdout}\n{proc.stderr}"
    out = json.loads(proc.stdout)
    assert out.get("dropped_keys") == []
    assert out.get("flags") == []
    assert "WARN" not in proc.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
