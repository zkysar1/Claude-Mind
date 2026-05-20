"""test_capability_gate_user_only_precondition.py — regression test for  + .

Asserts two interacting layers of capability-gate behavior:

  Layer 1 (g-115-372): the gate exempts keyword matches when failure_reason
  explicitly names a known user-only precondition that has NO registered
  agent-provisionable cure (the substring is in
  USER_ONLY_PRECONDITION_SUBSTRINGS but maps to None in
  USER_ONLY_PRECONDITION_CURES). Without the exemption, the gate produced
  wasteful auto-Unblock cycles for goals correctly deferred on user-only
  subsets of agent-capable forged skills. Three observed misroutes
  motivated this exemption (iter-15..17):
    - g-115-371: processor-launch matched 'processor' but blocker was
                 insufficient_session_data
    - g-260-20:  roblox-studio matched 'roblox' but blocker was
                 domain_session_required (M8 AccessoryToolBridge)
    - g-260-21:  roblox-studio matched 'roblox' but blocker was
                 domain_session_required (M7 ProximityPromptPickupHandler)

  Layer 2 (g-248-79): when the matched precondition substring DOES have a
  registered agent cure, the cure registry overrides the exemption — the
  gate fires (would_block=True) AND emits a cure-driven Unblock title.
  Two observed silent-cure-blindness incidents motivated the override:
    - g-115-369: deferred 4 days on insufficient_session_data; cure is
                 roblox-studio.sh start-session --mode RUN per
                 capability-routing.md "Game session — RUN-mode" row.
    - g-271-12:  deferred 10 days on active_sessions=0 / multi-NPC RUN;
                 same cure as g-115-369. (Also caught by g-248-79's
                 session-requirement regex; this test isolates the
                 cure-registry path independent of the regex.)

Test cases below mirror those failure_reasons. Layer 1 cases (no cure)
must still exempt; Layer 2 cases (cure exists) must now block; control
case must still block.

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

CASES = [
    # --- Layer 1: exempt cases (no cure registered) ---
    {
        "id": "exempt-roblox-studio-no-cure",
        "failure_reason": (
            "precondition_unmet:domain_session_required — M7 "
            "ProximityPromptPickupHandler is a server.lua script (no callable "
            "runSmokeTest); behavioral verification needs Roblox Studio "
            "runtime + player E-press interaction"
        ),
        "expected_substring": "domain_session_required",
        "should_block": False,
        "should_have_matches": True,  # 'roblox' keyword still matches
        "expected_cure_action": None,
    },
    # --- Layer 2: cure-overrides-exemption cases ---
    {
        "id": "cure-overrides-insufficient-session-data",
        "failure_reason": (
            "precondition_unmet:insufficient_session_data — newest game "
            "session has only 15 cells (<100 threshold). "
            "evaluate-processor-launch.sh skipped Processor launch."
        ),
        "expected_substring": "insufficient_session_data",
        "should_block": True,  # cure registered: start-session
        "should_have_matches": True,  # 'processor' keyword still matches
        "expected_cure_action": (
            "start RUN-mode session via "
            "roblox-studio.sh start-session --mode RUN"
        ),
    },
    {
        "id": "cure-overrides-active-sessions-zero",
        "failure_reason": (
            "Bridge probe at 2026-05-02T05:18 returned plugin_connected=true, "
            "active_sessions=0 — bridge is online but no live Studio session "
            "for behavior verification"
        ),
        "expected_substring": "active_sessions=0",
        "should_block": True,  # cure registered: start-session
        "should_have_matches": True,
        "expected_cure_action": (
            "start session via roblox-studio.sh start-session"
        ),
    },
    {
        #  cure-only path: matches the actual  defer text.
        # match_count=0 (no capability keyword in the text), but cure registry
        # fires alone. This is the case bravo's session-requirement regex
        # CANNOT catch because the text uses no requires/needs verb.
        "id": "cure-only-block-no-keyword-match",
        "failure_reason": (
            "precondition_unmet:insufficient_session_data — newest game "
            "session has only 15 cells (<100 threshold). Re-test exemption "
            "after capability-gate.py update."
        ),
        "expected_substring": "insufficient_session_data",
        "should_block": True,
        "should_have_matches": False,  # text has no capability keyword
        "expected_cure_action": (
            "start RUN-mode session via "
            "roblox-studio.sh start-session --mode RUN"
        ),
    },
    # --- Layer 3: exempted-keyword + session-req routes to session-req title ---
    # Regression test for the unblock_payload precedence bug: when matches is
    # non-empty but exempted by user_only_precondition (cure_action=None) AND
    # session_req_block fires (agent-provisionable RUN-mode), the Unblock
    # title MUST come from the session-req synthesis path ("start"), NOT from
    # the keyword path (would have been "studio"). Pre-fix, the elif chain
    # entered `elif matches:` because matches was truthy and produced
    # "Unblock: studio for g-test" — sending the agent toward the wrong
    # capability. Post-fix the condition is `elif keyword_block:` which
    # correctly accounts for the user_only exemption and falls to the
    # session-req synthesis path.
    {
        "id": "exempted-keyword-routes-to-session-req",
        "failure_reason": (
            "precondition_unmet:studio_session_required — verification "
            "needs RUN-mode session with >=100 cells of NPC behavior data"
        ),
        "expected_substring": "studio_session_required",
        "should_block": True,           # session_req_block drives the block
        "should_have_matches": True,    # 'studio' / 'session' / 'data' tokens hit
        "expected_cure_action": None,   # studio_session_required → None in CURES
        "expected_unblock_title_verb": "start",  # NOT "studio" — bug regression
    },
    # --- Control: no precondition match, capability keyword block fires ---
    {
        "id": "control-still-blocks",
        "failure_reason": "deploy needs human approval to push to main",
        "expected_substring": None,
        "should_block": True,
        "should_have_matches": True,
        "expected_cure_action": None,
    },
]


def _run_gate(failure_reason: str) -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed_json).

    Always passes --suggest-unblock so the response includes unblock_title for
    blocking cases — required by the misattribution regression case (the bug
    surfaced in the unblock_title, not in would_block).
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE_PY),
            "--failure-reason",
            failure_reason,
            "--intended-participants",
            "user",
            "--suggest-unblock",
            "--for-goal-id",
            "g-test",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


def main() -> int:
    failures = []

    for case in CASES:
        cid = case["id"]
        reason = case["failure_reason"]
        rc, payload = _run_gate(reason)

        # Check would_block
        actual_block = bool(payload.get("would_block"))
        if actual_block != case["should_block"]:
            failures.append(
                f"[FAIL] {cid}: would_block={actual_block}, "
                f"expected={case['should_block']}"
            )
            continue

        # Check matches present (when expected — some cure-only cases assert
        # match_count==0 to exercise the cure-without-keyword-match path).
        actual_match_count = payload.get("match_count", 0)
        if case["should_have_matches"] and actual_match_count == 0:
            failures.append(
                f"[FAIL] {cid}: expected keyword matches but got 0"
            )
            continue
        if not case["should_have_matches"] and actual_match_count > 0:
            failures.append(
                f"[FAIL] {cid}: expected NO keyword matches but got "
                f"{actual_match_count}"
            )
            continue

        # Check cure_action expectation (). When non-None, the cure
        # registry must have resolved a verb-phrase. When None, the registry
        # must NOT have resolved anything for any matched substring.
        actual_cure = payload.get("cure_action")
        if case["expected_cure_action"] != actual_cure:
            failures.append(
                f"[FAIL] {cid}: cure_action={actual_cure!r}, "
                f"expected={case['expected_cure_action']!r}"
            )
            continue

        # Check unblock_title verb (regression for the exempted-keyword
        # misattribution bug). Only enforced when the case sets
        # expected_unblock_title_verb — title format is
        # "Unblock: <verb>[ <rest>] for <goal-id>" and we assert the verb
        # word that follows "Unblock: ". Pre-fix this would be the
        # exempted matched_keyword (e.g. "studio"); post-fix it must be
        # the session-req canonical verb ("start").
        expected_verb = case.get("expected_unblock_title_verb")
        if expected_verb is not None:
            actual_title = payload.get("unblock_title", "")
            expected_prefix = f"Unblock: {expected_verb}"
            if not actual_title.startswith(expected_prefix):
                failures.append(
                    f"[FAIL] {cid}: unblock_title={actual_title!r} "
                    f"does not start with {expected_prefix!r}"
                )
                continue

        # Check exemption substring detection (when expected)
        if case["expected_substring"] is not None:
            detected = payload.get("user_only_preconditions_detected", False)
            substrs = payload.get("user_only_precondition_substrings", [])
            if not detected:
                failures.append(
                    f"[FAIL] {cid}: user_only_preconditions_detected=False"
                )
                continue
            if case["expected_substring"] not in substrs:
                failures.append(
                    f"[FAIL] {cid}: expected substring "
                    f"'{case['expected_substring']}' not in {substrs}"
                )
                continue
            print(
                f"  [PASS] {cid}: rc={rc} would_block={actual_block} "
                f"match_count={actual_match_count} "
                f"user_only_substrs={substrs} "
                f"cure_action={actual_cure!r}"
            )
        else:
            # Control case — should_block=True with no exemption
            if payload.get("user_only_preconditions_detected"):
                failures.append(
                    f"[FAIL] {cid}: control case unexpectedly detected "
                    f"user-only precondition: "
                    f"{payload.get('user_only_precondition_substrings')}"
                )
                continue
            print(
                f"  [PASS] {cid}: rc={rc} would_block={actual_block} "
                f"match_count={actual_match_count} "
                f"(control, no exemption fired)"
            )

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)}/{len(CASES)} test(s) failed")
        return 1
    print(f"All {len(CASES)} user-only-precondition cases verified "
          f"(1 exempt-no-cure, 3 cure-overrides, 1 exempted-keyword "
          f"routes-to-session-req, 1 control).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
