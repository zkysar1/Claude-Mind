"""test_capability_gate_suggest_unblock.py — regression test for .

Asserts the --suggest-unblock flag emits the four new fields when both
(1) the flag is set AND (2) would_block is True. Also verifies the
backwards-compat invariant: when the flag is unset, output schema is
unchanged (no unblock_* fields appear in the JSON).

Design notes (mirrors g-257-02 decomposition rationale):
- Title uses the first-in-source-order action verb from failure_reason
  (the thing the agent must DO), NOT the matched-capability keyword.
  When failure_reason = "deploy needs human" the gate may match "human"
  against an NPC capability, but the agent's required action is "deploy"
  — the title reflects that.
- matched_capability carries the gate's match info verbatim (source,
  skill, matched_keyword) so callers needing the gate's signal can use
  it independently of the title's action verb.
- The gate does NOT call aspirations-add-goal.sh — single-writer rule
  (rb-403). The caller files the Unblock goal atomically with the defer
  using the spec emitted here.

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


def _run_gate(failure_reason: str, suggest_unblock: bool = False,
              for_goal_id: str | None = None,
              intended_participants: str = "user") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed_json)."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", intended_participants,
        "--output", "json",
    ]
    if suggest_unblock:
        cmd.append("--suggest-unblock")
    if for_goal_id:
        cmd.extend(["--for-goal-id", for_goal_id])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


def main() -> int:
    failures = []
    cases_run = 0

    # Case 1: canonical verification test from  outcome 4.
    # failure_reason "deploy needs human" → unblock_title must contain "deploy".
    # In this codebase "deploy" doesn't itself match any capability row, but
    # "human" matches analyze-npc-behavior. The title uses "deploy" because
    # it's the first action verb in source-text order (action-verb design).
    cases_run += 1
    rc, d = _run_gate("deploy needs human", suggest_unblock=True)
    title = d.get("unblock_title") or ""
    if not d.get("would_block"):
        failures.append(
            f"case1 canonical: expected would_block=True (test depends on "
            f"capability match firing); got would_block={d.get('would_block')}"
        )
    if d.get("unblock_suggested") is not True:
        failures.append(
            f"case1: unblock_suggested expected True, got {d.get('unblock_suggested')!r}"
        )
    if "deploy" not in title:
        failures.append(
            f"case1: unblock_title expected to contain 'deploy', got {title!r}"
        )
    for required_field in ("unblock_title", "unblock_description", "matched_capability"):
        if required_field not in d:
            failures.append(f"case1: missing field {required_field}")
    mc = d.get("matched_capability") or {}
    if not isinstance(mc, dict) or not mc.get("matched_keyword"):
        failures.append(
            f"case1: matched_capability expected dict with matched_keyword, got {mc!r}"
        )
    print(f"  [{'PASS' if 'deploy' in title and d.get('unblock_suggested') else 'FAIL'}] "
          f"canonical: rc={rc} title={title!r} matched_kw={mc.get('matched_keyword')!r}")

    # Case 2: --for-goal-id is interpolated into the title.
    cases_run += 1
    rc, d = _run_gate("deploy needs human", suggest_unblock=True, for_goal_id="g-115-149")
    title2 = d.get("unblock_title") or ""
    if "deploy" not in title2 or "g-115-149" not in title2:
        failures.append(
            f"case2: title expected to contain both 'deploy' and 'g-115-149', got {title2!r}"
        )
    print(f"  [{'PASS' if 'deploy' in title2 and 'g-115-149' in title2 else 'FAIL'}] "
          f"for-goal-id: title={title2!r}")

    # Case 3: backwards-compat — without --suggest-unblock, none of the four
    # new fields appear. This is verification outcome 3 (). A future
    # editor that defaults --suggest-unblock to True would break this assertion.
    cases_run += 1
    rc, d = _run_gate("deploy needs human", suggest_unblock=False)
    extra_fields = [k for k in
                    ("unblock_suggested", "unblock_title",
                     "unblock_description", "matched_capability")
                    if k in d]
    if extra_fields:
        failures.append(
            f"case3 backwards-compat: expected no unblock_* fields without "
            f"--suggest-unblock, found {extra_fields}"
        )
    # And the existing schema is intact.
    for required in ("matches", "would_block", "narrative_framing_detected",
                     "keywords_extracted", "sources_scanned"):
        if required not in d:
            failures.append(
                f"case3: existing field {required} missing — schema regression"
            )
    print(f"  [{'PASS' if not extra_fields else 'FAIL'}] backwards-compat: "
          f"extra_fields={extra_fields}")

    # Case 4: --suggest-unblock + would_block=False → only unblock_suggested=False
    # appears, no other unblock_* fields. Tests the negative branch.
    cases_run += 1
    rc, d = _run_gate("completely unrelated nonsense xyzzy",
                      suggest_unblock=True)
    if d.get("would_block"):
        failures.append(
            f"case4: expected would_block=False (no capability match for "
            f"'xyzzy'), got True; matches={d.get('matches')[:1] if d.get('matches') else []}"
        )
    if d.get("unblock_suggested") is not False:
        failures.append(
            f"case4: unblock_suggested expected False (no block), got "
            f"{d.get('unblock_suggested')!r}"
        )
    for not_expected in ("unblock_title", "unblock_description", "matched_capability"):
        if not_expected in d:
            failures.append(
                f"case4: {not_expected} should NOT be present when would_block=False, "
                f"found {d.get(not_expected)!r}"
            )
    print(f"  [{'PASS' if d.get('unblock_suggested') is False and 'unblock_title' not in d else 'FAIL'}] "
          f"no-match-no-fields: unblock_suggested={d.get('unblock_suggested')!r}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nAll {cases_run} suggest-unblock cases verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
