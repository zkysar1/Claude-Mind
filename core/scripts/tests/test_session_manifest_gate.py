"""Coverage for session-manifest-gate.sh — the LAST of the 4 pre-commit gates that
had no test anywhere (g-115-4399; re-measured 2026-08-29: 4 uncovered of 13).

This gate is fail-CLOSED by design: if an entry lacks a valid `sync_tier`, the
runtime loader treats the whole session/ tree as machine-local. So a gate that
stops refusing does not merely allow a bad manifest through -- it lets a session
file silently fall back to not-synced (knowledge loss) or a liveness file silently
sync (phantom runner). The induced-fault cases below are the load-bearing half;
the valid-manifest case passes against a completely dead gate. guard-5501, rb-6205.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _bash_helpers import BASH

GATE = Path(__file__).resolve().parents[1] / "session-manifest-gate.sh"
REAL_MANIFEST = Path(__file__).resolve().parents[2] / "config" / "session-manifest.yaml"

VALID = """files:
  - file: session/agent-state
    sync_tier: machine_local
  - file: session/handoff.yaml
    sync_tier: continuity
  - file: session/scratch.tmp
    sync_tier: ephemeral
"""


def _run(path: Path):
    return subprocess.run([BASH, str(GATE), "--file", str(path)],
                          capture_output=True, text=True, timeout=60)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "manifest.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_valid_manifest_passes(tmp_path):
    """NEGATIVE CONTROL -- passes against a dead gate too, so it proves nothing alone."""
    assert _run(_write(tmp_path, VALID)).returncode == 0


def test_the_real_manifest_passes(tmp_path):
    """Positive control on PRODUCTION data: the shipped manifest must satisfy its own
    gate. A failure here is a live defect, not a test bug."""
    if not REAL_MANIFEST.is_file():
        pytest.skip("session-manifest.yaml not present")
    r = _run(REAL_MANIFEST)
    assert r.returncode == 0, f"the shipped manifest FAILS its own gate:\n{r.stdout}\n{r.stderr}"


def test_malformed_yaml_is_refused(tmp_path):
    """INDUCED FAULT -- check 1."""
    assert _run(_write(tmp_path, "files:\n  - file: x\n   sync_tier: [unclosed\n")).returncode != 0


def test_entry_missing_file_key_is_refused(tmp_path):
    """INDUCED FAULT -- check 2."""
    assert _run(_write(tmp_path, "files:\n  - sync_tier: continuity\n")).returncode != 0


def test_entry_missing_sync_tier_is_refused(tmp_path):
    """INDUCED FAULT -- check 3, absent. This is the exact shape that makes the runtime
    loader fail closed over the WHOLE tree."""
    assert _run(_write(tmp_path, "files:\n  - file: session/handoff.yaml\n")).returncode != 0


def test_invalid_sync_tier_value_is_refused(tmp_path):
    """INDUCED FAULT -- check 3, present but not in the allowed set. Distinct from
    absence: a typo'd tier reads as configured while behaving as unconfigured."""
    body = "files:\n  - file: session/handoff.yaml\n    sync_tier: continuty\n"
    assert _run(_write(tmp_path, body)).returncode != 0
