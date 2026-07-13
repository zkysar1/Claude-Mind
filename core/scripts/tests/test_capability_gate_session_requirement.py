"""test_capability_gate_session_requirement.py — regression test for .

Asserts the SESSION_REQUIREMENT_PATTERN code path correctly classifies
"requires X session" / "needs X session" / "blocked on X session" /
"verification requires X session" framings into agent_provisionable
(refuse defer + Unblock-suggestion path) or user_keystroke_required
(pass with narrative_framing_detected log).

The 2026-05-06 motivating leak was goals g-115-369 and g-271-12 sitting
frozen 4-14 days each on defer_reasons that named RUN-mode sessions as
required when RUN-mode is agent-provisionable per capability-routing.md
"Game session — RUN-mode" row. The existing capability-keyword scan
caught some such defers via incidental keyword matches but missed
phrasings whose only signal was the session-requirement narrative.

g-248-79 design:
- Agent-provisionable session_req → block (mirrors keyword_block path,
  with synthesized matched_capability when no keyword also matched).
- user_keystroke_required session_req → pass + log subtype
  session_requirement_user_keystroke. Exempts incidental keyword matches
  (symmetric to USER_ONLY_PRECONDITION_SUBSTRINGS exemption) so a defer
  naming a genuine user-keystroke session does not produce a false-
  positive Unblock.
- No "session" word → no match, falls through to existing logic.

Pattern: same subprocess + sys.path import shape as
test_capability_gate_narrative.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PY = CORE_SCRIPTS / "capability-gate.py"
# PR 7a/5 extracted the pattern helpers out of the CLI shim into the gates
# package — import them from there (the shim no longer defines them).
GATES_CAPABILITY_PY = CORE_SCRIPTS / "gates" / "capability.py"

CASES = [
    # 1. Multi-NPC RUN session — agent-provisionable, must block.
    {
        "id": "block-multi-npc-run",
        "failure_reason": "requires multi-NPC RUN session >=8min",
        "expected_classification": "agent_provisionable",
        "should_block": True,
        "expect_unblock_filed": True,
    },
    # 2. Fresh game session for cell accumulation — agent-provisionable.
    # No imperative-verb match in failure_reason, so unblock_title falls
    # back to "start" (the canonical verb for start-session capability).
    {
        "id": "block-fresh-game-cells",
        "failure_reason": "needs fresh game session with >=100 cells",
        "expected_classification": "agent_provisionable",
        "should_block": True,
        "expect_unblock_filed": True,
        "expected_title_action": "start",
    },
    # 3. blocked-on phrasing — agent-provisionable.
    {
        "id": "block-blocked-on-run",
        "failure_reason": "blocked on RUN session for cell accumulation",
        "expected_classification": "agent_provisionable",
        "should_block": True,
        "expect_unblock_filed": True,
    },
    # 4. Verification-requires phrasing — agent-provisionable.
    {
        "id": "block-verification-requires",
        "failure_reason": "verification requires server-only NPC session",
        "expected_classification": "agent_provisionable",
        "should_block": True,
        "expect_unblock_filed": True,
    },
    # 5. PLAY session with character-spawn keystroke marker — user-only.
    # The keystroke marker exempts the incidental keyword matches that
    # would otherwise have blocked.
    {
        "id": "pass-play-character-spawn",
        "failure_reason": (
            "needs PLAY session with character spawn via F5-click for "
            "Player_* records in EFS Intent/"
        ),
        "expected_classification": "user_keystroke_required",
        "should_block": False,
        "expect_unblock_filed": False,
    },
    # 6. RUN session containing keystroke marker — user-only (any keystroke
    # marker in full text flips classification).
    {
        "id": "pass-run-with-epress",
        "failure_reason": (
            "requires server-only NPC session for E-press interaction"
        ),
        "expected_classification": "user_keystroke_required",
        "should_block": False,
        "expect_unblock_filed": False,
    },
    # 7. No "session" word — no match, no session_req signal. Existing
    # capability-keyword logic still applies independently.
    {
        "id": "noop-no-session-word",
        "failure_reason": "4-week telemetry window before evaluation",
        "expected_classification": None,
        "should_block": False,
        "expect_unblock_filed": False,
    },
    # 8. No session word, even with keystroke marker — no match.
    {
        "id": "noop-keystroke-without-session",
        "failure_reason": "requires player E-press interaction",
        "expected_classification": None,
        # NOTE: this case is NOT a session-requirement assertion. The
        # existing capability-keyword scan may or may not block on
        # incidental "player"/"press" matches against capability-routing
        # rows — that's pre-existing behavior unrelated to . We
        # only assert that session_requirement_detected is False here.
        "should_block": None,  # don't assert (orthogonal to this test)
        "expect_unblock_filed": None,
    },
]


def _run_gate(failure_reason: str, with_suggest: bool = True,
              for_goal_id: str = "g-test") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed_json)."""
    cmd = [
        sys.executable,
        str(GATE_PY),
        "--failure-reason",
        failure_reason,
        "--intended-participants",
        "user",
        "--output",
        "json",
    ]
    if with_suggest:
        cmd.extend(["--suggest-unblock", "--for-goal-id", for_goal_id])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


def _import_helpers():
    """Import session-requirement helpers from gates/capability.py (the
    post-7a/5 home; the hyphenated CLI shim re-exports nothing)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "capability_gate", GATES_CAPABILITY_PY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load spec for {GATES_CAPABILITY_PY}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return (
        mod._match_session_requirement_patterns,
        mod._classify_session_requirement,
    )


def main() -> int:
    failures = []
    match_session, classify_session = _import_helpers()

    for case in CASES:
        cid = case["id"]
        reason = case["failure_reason"]
        expected_cls = case["expected_classification"]
        should_block = case["should_block"]
        expect_unblock = case["expect_unblock_filed"]
        expected_title_action = case.get("expected_title_action")

        # Helper-level direct check
        helper_matches = match_session(reason)
        helper_cls = None
        if helper_matches:
            classifications = [
                classify_session(cx, reason) for _, cx in helper_matches
            ]
            helper_cls = (
                "user_keystroke_required"
                if "user_keystroke_required" in classifications
                else "agent_provisionable"
            )
        if helper_cls != expected_cls:
            failures.append(
                f"{cid}: helper classification mismatch — expected "
                f"{expected_cls!r}, got {helper_cls!r}"
            )

        # Subprocess gate check
        rc, payload = _run_gate(reason)
        gate_cls = payload.get("session_requirement_classification")
        gate_detected = payload.get("session_requirement_detected")
        gate_would_block = payload.get("would_block")
        unblock_suggested = payload.get("unblock_suggested", False)

        if gate_cls != expected_cls:
            failures.append(
                f"{cid}: gate session_requirement_classification mismatch "
                f"— expected {expected_cls!r}, got {gate_cls!r}"
            )
        if expected_cls is not None and not gate_detected:
            failures.append(
                f"{cid}: expected session_requirement_detected=True, got "
                f"{gate_detected!r}"
            )
        if expected_cls is None and gate_detected:
            failures.append(
                f"{cid}: expected session_requirement_detected=False, got "
                f"{gate_detected!r}"
            )

        # would_block / unblock assertions (skip when not asserted)
        if should_block is not None:
            expected_rc = 1 if should_block else 0
            if rc != expected_rc:
                failures.append(
                    f"{cid}: expected exit {expected_rc} "
                    f"(should_block={should_block}), got {rc}; "
                    f"reason={payload.get('reason', '')[:80]!r}"
                )
            if gate_would_block is not should_block:
                failures.append(
                    f"{cid}: expected would_block={should_block}, got "
                    f"{gate_would_block!r}"
                )
        if expect_unblock is not None:
            if unblock_suggested is not expect_unblock:
                failures.append(
                    f"{cid}: expected unblock_suggested={expect_unblock}, "
                    f"got {unblock_suggested!r}"
                )

        # Title-action check (only when explicitly named on the case)
        if expected_title_action and unblock_suggested:
            actual_title = payload.get("unblock_title", "")
            if expected_title_action not in actual_title.lower():
                failures.append(
                    f"{cid}: expected title to contain "
                    f"{expected_title_action!r}, got {actual_title!r}"
                )

        case_ok = (
            helper_cls == expected_cls
            and gate_cls == expected_cls
            and (should_block is None or gate_would_block is should_block)
            and (expect_unblock is None or unblock_suggested is expect_unblock)
        )
        status = "PASS" if case_ok else "FAIL"
        cls_label = expected_cls or "no_match"
        print(
            f"  [{status}] {cid} ({cls_label}): rc={rc} "
            f"would_block={gate_would_block} cls={gate_cls!r}"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"\nAll {len(CASES)} session-requirement cases verified "
        f"(4 block, 2 keystroke-pass, 2 no-match)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
