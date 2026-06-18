"""Regression test for the Path-B self-heal + SID-loss forensics (2026-06-18).

POST_RECOVERY_EDIT_OVERRIDE="User-directed test for the Path-B self-heal fix; authored in (IDLE,autonomous) alongside the recovery-gate.sh change before /start. Audited to world/post-recovery-edits.jsonl."

Bug 1 (self-heal): recovery-gate.sh Path B (_check_state_corruption) used to
ALWAYS demote a (state=RUNNING + running-session-id missing + no stop-requested
+ no compact-pending) agent to IDLE. That killed the loop of a DEMONSTRABLY-
ALIVE runner that had merely lost its running-session-id mid-run to an upstream
deleter (3rd occurrence 2026-06-18 bravo). The fix consults runner-dead-check.sh
and, when the runner is alive (rc=1) AND latest-session-id is present, RESTORES
running-session-id from latest-session-id instead of recovering — keeping the
loop alive.

Bug 2 (forensics): Path B now captures the discriminating context to
sid-loss-forensics.jsonl on EVERY trigger (self_heal OR recover). The
highest-value datum is runner_token_present (still-present ⇒ NOT a recovery
manifest-clear ⇒ the unidentified upstream deleter).

Strategy: static structural assertions on recovery-gate.sh (mirrors
test_recovery_ordering_invariant.py — the gate is impractical to invoke in
isolation without agent-state side effects). These lock down the decision
STRUCTURE so a future refactor cannot silently revert to always-recover.

Run: python -m pytest core/scripts/tests/test_recovery_gate_self_heal.py
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GATE = PROJECT_ROOT / "core" / "scripts" / "recovery-gate.sh"


def _gate_text() -> str:
    return GATE.read_text(encoding="utf-8")


def _check_state_corruption_body() -> str:
    """Extract the body of _check_state_corruption() (def → matching close).

    Simple brace-less bash function extraction: from the line
    `_check_state_corruption() {` to the next top-level `}` at column 0.
    """
    text = _gate_text()
    m = re.search(r"^_check_state_corruption\(\) \{$", text, re.MULTILINE)
    assert m, "_check_state_corruption() function not found in recovery-gate.sh"
    start = m.end()
    # The function's closing brace is the first `^}` after start.
    close = re.search(r"^\}$", text[start:], re.MULTILINE)
    assert close, "could not find closing brace of _check_state_corruption()"
    return text[start : start + close.start()]


def test_path_b_consults_liveness_oracle() -> None:
    """Path B must consult runner-dead-check.sh before deciding (the fix's core)."""
    body = _check_state_corruption_body()
    assert "runner-dead-check.sh" in body, (
        "_check_state_corruption must call runner-dead-check.sh as its liveness "
        "oracle — without it the self-heal cannot distinguish alive vs dead and "
        "reverts to the blunt always-recover bug."
    )
    assert re.search(r"rdc_rc=\$\?", body), "must capture runner-dead-check rc into rdc_rc"


def test_self_heal_branch_restores_sid_without_recovering() -> None:
    """On rc=1 + latest present, Path B restores running-session-id and must NOT
    demote state (no _perform_recovery, no session-state-set IDLE in the heal
    branch)."""
    body = _check_state_corruption_body()
    # The self-heal guard: alive (rc==1) AND a SID to restore.
    assert re.search(r'-n "\$latest".*"\$rdc_rc"\s*-eq\s*1', body) or re.search(
        r'"\$rdc_rc"\s*-eq\s*1.*-n "\$latest"', body
    ), "self-heal guard must require rdc_rc==1 AND non-empty latest-session-id"
    # The restore: write running-session-id from latest, atomically (.tmp + mv).
    assert "running-session-id.tmp" in body and re.search(
        r"mv .*running-session-id\.tmp.*running-session-id", body
    ), "self-heal must atomically restore running-session-id from latest (.tmp + mv)"
    assert "_record_self_heal" in body, "self-heal must be audited via _record_self_heal"

    # Structural ordering: the self-heal `return 0` (loop preserved) must appear
    # BEFORE the final recover fallthrough (_perform_recovery at the bottom).
    heal_idx = body.find("_record_self_heal")
    recover_idx = body.rfind("_perform_recovery")
    assert heal_idx != -1 and recover_idx != -1
    assert heal_idx < recover_idx, (
        "self-heal must be reached BEFORE the recover fallthrough — otherwise "
        "the live runner is killed before the heal can fire."
    )


def test_recover_fallthrough_preserved() -> None:
    """Genuinely-dead / no-latest / oracle-error must still recover (unchanged)."""
    body = _check_state_corruption_body()
    assert "_perform_recovery" in body, "recover fallthrough (_perform_recovery) must remain"
    # The original cause string is preserved for the recover path.
    assert "state corruption: state=RUNNING, running-session-id missing" in body


def test_forensics_captured_on_both_decisions() -> None:
    """Bug 2: forensics must be captured for BOTH self_heal and recover."""
    body = _check_state_corruption_body()
    assert body.count("_capture_sid_loss_forensics") >= 2, (
        "forensics must be captured on BOTH the self_heal and recover branches"
    )
    assert '"self_heal"' in body and '"recover"' in body, (
        "both decisions must be passed to _capture_sid_loss_forensics for audit"
    )


def test_forensics_helper_records_runner_token_discriminator() -> None:
    """The forensics helper must record runner_token_present — the key datum
    distinguishing a manifest-clear (removes both SID+token) from the
    unidentified upstream deleter (removes only the SID)."""
    text = _gate_text()
    m = re.search(r"^_capture_sid_loss_forensics\(\) \{$", text, re.MULTILINE)
    assert m, "_capture_sid_loss_forensics() helper not found"
    close = re.search(r"^\}$", text[m.end():], re.MULTILINE)
    helper = text[m.end() : m.end() + close.start()]
    assert "runner-token" in helper, "forensics must probe runner-token presence"
    assert "runner_token_present" in helper, "forensics JSON must record runner_token_present"
    assert "sid-loss-forensics.jsonl" in helper, "forensics must append to sid-loss-forensics.jsonl"


def test_self_heal_does_not_break_ordering_invariant() -> None:
    """The self-heal must not introduce a session-state-set IDLE or
    manifest-clear in _check_state_corruption (those belong only to
    _perform_recovery; introducing them here would both demote the loop and
    confuse the g-115-683 ordering-invariant test)."""
    body = _check_state_corruption_body()
    assert not re.search(r"session-state-set\.sh\"?\s+IDLE", body), (
        "self-heal path must NOT call session-state-set.sh IDLE (loop stays RUNNING)"
    )
    # Only comment references to manifest-clear are allowed; no invocation.
    invocation_lines = [
        L for L in body.splitlines()
        if "session-manifest-clear.sh" in L and not L.lstrip().startswith("#")
    ]
    assert not invocation_lines, (
        "self-heal path must NOT invoke session-manifest-clear.sh; "
        f"found: {invocation_lines}"
    )


if __name__ == "__main__":
    test_path_b_consults_liveness_oracle()
    test_self_heal_branch_restores_sid_without_recovering()
    test_recover_fallthrough_preserved()
    test_forensics_captured_on_both_decisions()
    test_forensics_helper_records_runner_token_discriminator()
    test_self_heal_does_not_break_ordering_invariant()
    print("ALL PASS — Path-B self-heal + SID-loss forensics structure locked down")
