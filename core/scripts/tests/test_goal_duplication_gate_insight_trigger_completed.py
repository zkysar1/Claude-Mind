"""test_goal_duplication_gate_insight_trigger_completed.py — 5.

Regression test for the Completed-Maintain skip in _check_insight_triggers.

The goal-duplication gate has a Completed-Maintain carve-out in 4 of its 5
file-overlap checks: partner_in_flight (g-115-2477), git_log (g-115-836 /
g-115-1813), target_state, and pending_queue all short-circuit to passed=True
when `status == "completed"` and the title starts with "Maintain:" — a completed
Maintain filing RECORDS work that already happened, so file overlap is a
completion signal, not a NEW duplicate. _check_insight_triggers lacked that
skip, so a completed-Maintain filing whose files are the subject of an active
insight_trigger got false-blocked (needing --override-duplication).

Cases (direct unit call on _check_insight_triggers — isolates THIS check):
  A. pending goal + overlapping active insight_trigger      → passed=False (real signal kept)
  B. completed Maintain goal + same overlap                 → passed=True  (the fix; FAILS pre-fix)
  C. completed NON-Maintain goal + same overlap             → passed=False (skip is Maintain-narrow, matches siblings)
  D. completed Maintain goal + NO overlap                   → passed=True  (trivially)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.goal_duplication import _check_insight_triggers  # noqa: E402

AFFECTED = "core/scripts/some-target.sh"
SELF_AGENT = "alpha"


def _seed_world(tmp: Path):
    fp = tmp / "board" / "findings.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    finding = {
        "id": "msg-gdg2685-bravo",
        "channel": "findings",
        "type": "finding",
        "author": "bravo",  # non-self
        "tags": ["insight_trigger", "severity:constrains", f"affects:{AFFECTED}"],
        "timestamp": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "body": "synthetic insight_trigger for g-115-2685",
    }
    fp.write_text(json.dumps(finding) + "\n", encoding="utf-8")


def _check(goal, file_paths, tmp):
    return _check_insight_triggers(goal, set(file_paths), SELF_AGENT, tmp, expected_paths=set())


def _run():
    failures = []
    tmp = Path(tempfile.mkdtemp(prefix="gdg2685-"))
    try:
        _seed_world(tmp)

        # A: pending goal overlapping the trigger → blocked (real signal preserved)
        a = _check({"status": "pending", "title": "Refactor some-target.sh"}, [AFFECTED], tmp)
        if a["passed"] is not False:
            failures.append(f"A pending+overlap should block, got passed={a['passed']} ({a['reason']})")

        # B: completed Maintain goal overlapping the trigger → skipped (THE FIX)
        b = _check({"status": "completed", "title": "Maintain: adjusted some-target.sh phase ordering"},
                   [AFFECTED], tmp)
        if b["passed"] is not True:
            failures.append(f"B completed-Maintain+overlap should SKIP, got passed={b['passed']} ({b['reason']})")

        # C: completed NON-Maintain goal overlapping the trigger → still blocked (skip is Maintain-narrow)
        c = _check({"status": "completed", "title": "Refactor some-target.sh"}, [AFFECTED], tmp)
        if c["passed"] is not False:
            failures.append(f"C completed-non-Maintain+overlap should still block, got passed={c['passed']}")

        # D: completed Maintain goal, no overlap → passes trivially
        d = _check({"status": "completed", "title": "Maintain: unrelated"},
                   ["core/scripts/other-file.py"], tmp)
        if d["passed"] is not True:
            failures.append(f"D completed-Maintain+no-overlap should pass, got passed={d['passed']}")

        return failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_insight_trigger_completed_maintain_skip():
    failures = _run()
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    fails = _run()
    if fails:
        print(f"TEST FAIL ({len(fails)}):", file=sys.stderr)
        for f in fails:
            print("  " + f, file=sys.stderr)
        sys.exit(1)
    print("TEST PASS: 4 cases (pending-blocks, completed-Maintain-skips, "
          "completed-non-Maintain-blocks, no-overlap-passes)")
    sys.exit(0)
