"""Tests for blocker-create-gate check 5: credential-source enumeration.

g-248-111 — credentials-required blockers must enumerate per-source identities
so a self-serviceable grant (the pq-s3-deleteobject 86h human-gating incident,
guard-1160) is caught before the blocker is written. Check 1 (canonical_probe)
fails OPEN for human-only types, so credentials-required previously bypassed
every self-service verification; this check restores one.

The check refuses when: enumeration is missing; fewer than 2 sources; any
un-probed source (probed != true); any self-serviceable source (denied != true);
or two sources resolve to the same identity (pseudo-independence).
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates import blocker_create  # noqa: E402


def _cred_check(blocker: dict) -> dict:
    """Run evaluate() and return the credential_enumeration check result."""
    result = blocker_create.evaluate(
        blocker, probe_command=None, world_dir=None, agent_name="",
    )
    for c in result["checks"]:
        if c["name"] == "credential_enumeration":
            return c
    raise AssertionError("credential_enumeration check absent from evaluate() output")


def _complete_denied_enum():
    """Two distinct, probed, all-denied sources with distinct identities."""
    return [
        {"source": "env", "identity": "arn:aws:iam::1:user/ci-bot", "probed": True, "denied": True},
        {"source": "default_chain", "identity": "arn:aws:iam::1:role/task", "probed": True, "denied": True},
    ]


# --- The goal's three explicit verification scenarios ------------------------

def test_missing_enumeration_refuses():
    """credentials-required without credential_source_enumeration → refuse."""
    blocker = {"type": "credentials-required", "failure_reason": "need S3 DeleteObject grant"}
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert "credential_source_enumeration" in check["reason"]


def test_complete_all_denied_passes():
    """Complete enumeration, all sources probed + denied → check 5 passes."""
    blocker = {"type": "credentials-required", "credential_source_enumeration": _complete_denied_enum()}
    check = _cred_check(blocker)
    assert check["passed"] is True, check["reason"]


def test_pseudo_independence_refuses():
    """Two sources resolving to the SAME identity → refuse (pseudo-independence)."""
    blocker = {
        "type": "credentials-required",
        "credential_source_enumeration": [
            {"source": "env", "identity": "arn:aws:iam::1:root", "probed": True, "denied": True},
            {"source": "default_chain", "identity": "arn:aws:iam::1:root", "probed": True, "denied": True},
        ],
    }
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert "pseudo-independent" in check["reason"].lower()


# --- The two conditions the design adds to make the gate actually bite -------

def test_unprobed_source_refuses():
    """A listed source with probed != true → refuse (untested self-service path)."""
    enum = _complete_denied_enum()
    enum[1] = {**enum[1], "probed": False}
    blocker = {"type": "credentials-required", "credential_source_enumeration": enum}
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert "un-probed" in check["reason"].lower()


def test_self_serviceable_source_refuses():
    """A source that is NOT denied (can perform) → refuse — the pq-s3 failure mode."""
    enum = _complete_denied_enum()
    enum[1] = {**enum[1], "denied": False}  # default_chain (e.g. root) CAN perform
    blocker = {"type": "credentials-required", "credential_source_enumeration": enum}
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert "self-serviceable" in check["reason"].lower()


def test_single_source_refuses():
    """Fewer than 2 enumerated sources → refuse (cannot establish independence)."""
    blocker = {
        "type": "credentials-required",
        "credential_source_enumeration": [
            {"source": "env", "identity": None, "probed": True, "denied": True},
        ],
    }
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert ">=2" in check["reason"] or "need >=2" in check["reason"]


def test_malformed_entry_refuses():
    """An entry missing required keys → refuse (malformed)."""
    blocker = {
        "type": "credentials-required",
        "credential_source_enumeration": [
            {"source": "env", "identity": None, "probed": True, "denied": True},
            {"source": "default_chain"},  # missing probed + denied
        ],
    }
    check = _cred_check(blocker)
    assert check["passed"] is False
    assert "malformed" in check["reason"].lower()


def test_all_denied_with_null_identities_passes():
    """Absent sources (identity null) that are probed + denied still pass."""
    blocker = {
        "type": "credentials-required",
        "credential_source_enumeration": [
            {"source": "env", "identity": None, "probed": True, "denied": True},
            {"source": "instance_role", "identity": None, "probed": True, "denied": True},
        ],
    }
    check = _cred_check(blocker)
    assert check["passed"] is True, check["reason"]


# --- Non-target types are untouched ------------------------------------------

def test_non_credentials_type_skipped():
    """A non-credentials-required blocker skips check 5 entirely."""
    blocker = {"type": "infrastructure", "failure_reason": "service down"}
    check = _cred_check(blocker)
    assert check["passed"] is True
    assert "skipped" in check["reason"].lower()


# --- Gate-level: missing enumeration blocks the whole gate -------------------

def test_gate_blocks_credentials_required_missing_enumeration():
    """Full evaluate(): a credentials-required blocker missing enumeration would_block."""
    blocker = {"type": "credentials-required", "failure_reason": "need a grant"}
    result = blocker_create.evaluate(blocker, probe_command=None, world_dir=None, agent_name="")
    assert result["would_block"] is True
    assert "credential_enumeration" in [c["name"] for c in result["checks"]]


def test_gate_override_bypasses_credential_check():
    """--override-blocker-gate flips would_block False even when check 5 fails."""
    blocker = {"type": "credentials-required", "failure_reason": "need a grant"}
    result = blocker_create.evaluate(
        blocker, probe_command=None, override_blocker_gate="verified false positive",
        world_dir=None, agent_name="",
    )
    assert result["would_block"] is False
    assert result["override_applied"] == "verified false positive"
