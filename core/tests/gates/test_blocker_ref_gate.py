"""Behavior tests for gates.blocker_ref (PR 7e/2).

Pure validation: validate(raw, now=...) -> (ok, dict_or_error). No I/O.
log_unstructured_override is tested in mind_api/tests/ where a real
world_dir + locked_append_jsonl harness is available.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.blocker_ref import (
    validate,
    BLOCKER_REF_TYPES,
    BLOCKER_REF_TTL_HOURS,
    log_unstructured_override,
)


FROZEN_NOW = datetime(2026, 5, 13, 12, 0, 0)


# ---------------------------------------------------------------------------
# Happy path — full validation + derived fields
# ---------------------------------------------------------------------------

def test_validate_minimal_payload_fills_derived_fields():
    raw = json.dumps({"type": "infrastructure", "external_id": "svc-123"})
    ok, ref = validate(raw, now=FROZEN_NOW)
    assert ok is True
    assert ref["type"] == "infrastructure"
    assert ref["external_id"] == "svc-123"
    assert ref["state_hash"] is None
    assert ref["created_at"] == "2026-05-13T12:00:00"
    # 120h TTL for infrastructure → 2026-05-18T12:00:00
    assert ref["expires_at"] == "2026-05-18T12:00:00"


def test_validate_accepts_decoded_dict_input():
    """Daemon path passes the dict directly (no JSON intermediate)."""
    ok, ref = validate(
        {"type": "partner-response", "external_id": "msg-42"},
        now=FROZEN_NOW,
    )
    assert ok is True
    # partner-response TTL is 72h
    assert ref["expires_at"] == "2026-05-16T12:00:00"


def test_validate_strips_external_id_whitespace():
    ok, ref = validate({"type": "user_action", "external_id": "  cmd-99  "})
    assert ok is True
    assert ref["external_id"] == "cmd-99"


def test_validate_preserves_caller_supplied_expires_at():
    """Explicit expires_at must not be overwritten by TTL derivation."""
    ok, ref = validate(
        {"type": "infrastructure", "external_id": "x",
         "expires_at": "2099-01-01T00:00:00"},
        now=FROZEN_NOW,
    )
    assert ok is True
    assert ref["expires_at"] == "2099-01-01T00:00:00"


def test_validate_preserves_state_hash():
    ok, ref = validate(
        {"type": "external-service", "external_id": "probe-1",
         "state_hash": "abc123"},
        now=FROZEN_NOW,
    )
    assert ok is True
    assert ref["state_hash"] == "abc123"


# ---------------------------------------------------------------------------
# Per-type TTL — single source of truth check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("btype,hours", [
    ("partner-response", 72),
    ("external-service", 24),
    ("user_action", 120),
    ("infrastructure", 120),
    ("resource", 120),
    ("credentials-required", 120),
    ("security-trust", 120),
    ("physical-hardware", 120),
])
def test_ttl_per_type(btype, hours):
    assert BLOCKER_REF_TTL_HOURS[btype] == hours


def test_every_type_has_a_ttl():
    """If a type is added without a TTL, the auto-derivation crashes —
    catch that drift at the data layer instead of at the call site."""
    for btype in BLOCKER_REF_TYPES:
        assert btype in BLOCKER_REF_TTL_HOURS, (
            f"BLOCKER_REF_TYPES has {btype!r} but BLOCKER_REF_TTL_HOURS does not"
        )


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

def test_empty_string_fails():
    ok, err = validate("")
    assert ok is False
    assert "required" in err


def test_none_fails():
    ok, err = validate(None)
    assert ok is False


def test_invalid_json_fails():
    ok, err = validate("{not json")
    assert ok is False
    assert "not valid JSON" in err


def test_non_object_json_fails():
    ok, err = validate("[]")
    assert ok is False
    assert "must be a JSON object" in err


def test_unknown_type_fails():
    ok, err = validate({"type": "made-up", "external_id": "x"})
    assert ok is False
    assert "type must be one of" in err


def test_missing_external_id_fails():
    ok, err = validate({"type": "infrastructure"})
    assert ok is False
    assert "external_id" in err


def test_empty_external_id_fails():
    ok, err = validate({"type": "infrastructure", "external_id": ""})
    assert ok is False


def test_whitespace_only_external_id_fails():
    ok, err = validate({"type": "infrastructure", "external_id": "   "})
    assert ok is False


def test_non_string_external_id_fails():
    ok, err = validate({"type": "infrastructure", "external_id": 42})
    assert ok is False
    assert "non-empty string" in err


def test_non_string_state_hash_fails():
    ok, err = validate(
        {"type": "infrastructure", "external_id": "x", "state_hash": 99}
    )
    assert ok is False
    assert "state_hash" in err


# ---------------------------------------------------------------------------
# log_unstructured_override — best-effort audit ledger
# ---------------------------------------------------------------------------

def test_log_override_writes_record(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    result = log_unstructured_override(
        world,
        goal_id="g-001-99",
        defer_reason_text="blocked on user feedback",
        justification="genuine human approval required",
        agent_name="alpha",
    )
    assert result is not None
    log = world / "blocker-gate-overrides.jsonl"
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "alpha"
    assert rec["goal_id"] == "g-001-99"
    assert rec["justification"] == "genuine human approval required"
    assert rec["which_checks_bypassed"] == ["blocker_ref_required"]


def test_log_override_skips_silently_when_world_dir_none():
    """world_dir=None must return None without raising — daemon path can
    receive an unbound agent and the override must still complete."""
    result = log_unstructured_override(
        None,
        goal_id="g-001-99",
        defer_reason_text="x",
        justification="y",
        agent_name="alpha",
    )
    assert result is None


def test_log_override_truncates_long_defer_text(tmp_path):
    """Long defer_reason should be truncated to 200 chars in the record
    (prevents pathological audit-log bloat)."""
    world = tmp_path / "world"
    world.mkdir()
    long_text = "x" * 500
    log_unstructured_override(
        world,
        goal_id="g-001-99",
        defer_reason_text=long_text,
        justification="j",
        agent_name="a",
    )
    log = world / "blocker-gate-overrides.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert len(rec["defer_reason"]) == 200
