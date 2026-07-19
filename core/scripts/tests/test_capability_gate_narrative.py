"""test_capability_gate_narrative.py — regression test for .

Asserts the 3 leak cases from the 2026-04-21 priority-review batch are now
correctly handled by capability-gate.py after g-255-02 added narrative-pattern
detection. Filed by g-255-04 (decomposed from g-255-01).

g-255-02 design: narrative-pattern matches alone do NOT block (would route
out false positives on legitimate user-judgment requests); they are LOGGED
to gate-firings.jsonl with `narrative_framing_detected: true` so audits
can spot drift. Hard block fires when narrative AND a capability keyword
both match — that's the signal that a known agent-capable action is being
narrated as a user-leg.

The three 2026-04-21 narratives split across both branches:

  g-115-148 (Restart=always policy decision)  → block:   narrative + "always"
                                                          (bridge-server cap row)
  g-226-57  (log statement placement)         → pass+log: narrative only,
                                                          no capability kw
                                                          in the synthetic
  g-115-129 (commit batch before push)        → block:   narrative + "commit"
                                                          + "push" (deploy cap)

The pass+log branch is still a regression-relevant signal — pre-g-255-02
the narrative_framing_detected tag did not exist in gate telemetry at
all. Asserting the tag fires for the narrative-only case proves the
detection code path is wired even when the gate doesn't block.

Pattern: same subprocess + sys.path import shape as test_infra_health_race.py.
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
    {
        "id": "g-115-148",
        "failure_reason": (
            "user approves the systemd Restart=always vs Restart=no policy "
            "decision before we land the unit-file change"
        ),
        "expected_narrative_substr": "user approves",
        # Flipped True→False (3 triage, 2026-07-16): the block relied
        # on the sole generic-prose token 'always' (from "Restart=always")
        # accidentally matching the bridge-server capability row — semantically
        # unrelated vocabulary, exactly the class the  sole-token
        # precision rule (2026-07-13) suppresses. No systemd/unit-file
        # capability exists in the routing table, so per the  design
        # documented above ("narrative-pattern matches alone do NOT block"),
        # the correct current behavior is pass+log. The genuine-recall control
        # for narrative+capability blocking is  below (commit/push
        # imperative verbs still qualify as sole tokens).
        "should_block": False,
    },
    {
        "id": "g-226-57",
        "failure_reason": (
            "waiting for user decision on log statement placement in "
            "dispatchRecoveryAction — the agent must approve which line gets "
            "the new logger.info() call"
        ),
        "expected_narrative_substr": "waiting for user decision",
        # narrative-only match — no agent-capable keyword in the synthetic.
        # Per  design: pass with narrative_framing_detected logged.
        "should_block": False,
    },
    {
        "id": "g-115-129",
        "failure_reason": (
            "user must approve the rb-329-family commit batch before push to "
            "main — pending user sign-off on the diff"
        ),
        "expected_narrative_substr": "user must",
        "should_block": True,
    },
]


def _run_gate(failure_reason: str) -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed_json)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE_PY),
            "--failure-reason",
            failure_reason,
            "--intended-participants",
            "user",
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


def _import_match_narrative_patterns():
    """Import _match_narrative_patterns from the gates.capability module.

    The function moved out of the capability-gate.py CLI wrapper into the
    gates/capability.py module during the gates/ refactor (g-115-1872 era),
    so the old importlib load of GATE_PY (the hyphenated CLI wrapper) raised
    `AttributeError: module 'capability_gate' has no attribute
    '_match_narrative_patterns'`. core/scripts is on sys.path (line 41) and
    gates/capability.py has no hyphen, so a normal package import is the
    correct access path — no importlib workaround needed. (g-001-334)
    """
    from gates.capability import _match_narrative_patterns
    return _match_narrative_patterns


def main() -> int:
    failures = []
    match_narrative = _import_match_narrative_patterns()

    for case in CASES:
        gid = case["id"]
        reason = case["failure_reason"]
        expected_substr = case["expected_narrative_substr"]
        should_block = case["should_block"]

        rc, payload = _run_gate(reason)
        would_block = payload.get("would_block")

        # Check 1+2: subprocess exit code + would_block matches expected branch
        expected_rc = 1 if should_block else 0
        if rc != expected_rc:
            failures.append(
                f"{gid}: expected exit {expected_rc} "
                f"(should_block={should_block}), got {rc}; payload={payload}"
            )
        if would_block is not should_block:
            failures.append(
                f"{gid}: expected would_block={should_block}, got {would_block!r}"
            )

        # Check 3: narrative-pattern detection fired (asserts ALL three cases,
        # both block and pass-with-log branches, exercise the new code path)
        narrative_hits = match_narrative(reason)
        if not narrative_hits:
            failures.append(
                f"{gid}: expected ≥1 narrative-pattern hit (e.g. "
                f"'{expected_substr}'), got [] — narrative detection silent"
            )
        elif expected_substr not in narrative_hits:
            failures.append(
                f"{gid}: expected '{expected_substr}' in hits, got "
                f"{narrative_hits}"
            )

        case_ok = (
            rc == expected_rc
            and would_block is should_block
            and narrative_hits
            and expected_substr in (narrative_hits or [])
        )
        status = "PASS" if case_ok else "FAIL"
        branch = "block" if should_block else "pass+log"
        print(
            f"  [{status}] {gid} ({branch}): rc={rc} would_block={would_block} "
            f"narrative_hits={narrative_hits}"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"\nAll {len(CASES)} narrative cases verified "
        f"(2 block, 1 pass-with-narrative-log)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
