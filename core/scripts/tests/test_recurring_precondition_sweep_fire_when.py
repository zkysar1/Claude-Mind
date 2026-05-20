"""test_recurring_precondition_sweep_fire_when.py — Magic Wand #4 regression.

Verifies recurring-precondition-sweep.py advances lastAchievedAt when
fire_when fails AND the time gate has elapsed. The sweep treats fire_when
as an extra structured precondition (Magic Wand #4, alpha session-60,
2026-05-07). Without this advance, overdue_ratio inflates unboundedly
on recurring goals whose upstream signal is consistently absent.

Pattern: subprocess + tempdir test agent (mirrors
test_recurring_loop_state_mutate.py). _paths.py derives PROJECT_ROOT
from script location and is NOT overridable, so the test agent must
live at the real PROJECT_ROOT.

Test runs sweep with --dry-run so it does NOT mutate live state — we
just check that stdout reports the advance for the goal whose
fire_when fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SWEEP_PY = CORE_SCRIPTS / "recurring-precondition-sweep.py"

sys.path.insert(0, str(CORE_SCRIPTS))
from _paths import agent_dir as _agent_dir  # noqa: E402


def _build_aspiration_jsonl(path: Path, goals: list[dict]) -> None:
    """Write a one-line aspirations.jsonl with the supplied goals."""
    import json
    asp = {
        "id": "asp-test",
        "title": "Test",
        "status": "active",
        "scope": "maintenance",
        "goals": goals,
    }
    path.write_text(json.dumps(asp) + "\n", encoding="utf-8")


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def main() -> int:
    failures = []
    agent_name = f"_mw4-sweep-test-{uuid.uuid4().hex[:8]}"
    agent_dir = _agent_dir(agent_name)

    # Ensure a fresh existing path + a guaranteed-non-existent path for fire_when.
    with tempfile.NamedTemporaryFile(suffix=".fire-when-sweep", delete=False) as tmp:
        existing_path = tmp.name
    nonexistent_path = existing_path + ".missing"

    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
        asp_file = agent_dir / "aspirations.jsonl"

        # Three recurring goals to exercise:
        # 1. fire_when fails AND past time gate → SHOULD advance
        # 2. fire_when passes → should NOT advance (gate cleared, let it fire)
        # 3. fire_when fails BUT time gate not elapsed → should NOT advance
        goals = [
            {
                "id": "g-fail-elapsed",
                "title": "Recurring with failing fire_when, past gate",
                "status": "pending",
                "priority": "MEDIUM",
                "recurring": True,
                "interval_hours": 24,
                "lastAchievedAt": _hours_ago_iso(48),  # 48h > 24h interval
                "fire_when": {
                    "type": "file_check",
                    "path": nonexistent_path,
                    "condition": "exists",
                },
            },
            {
                "id": "g-pass-elapsed",
                "title": "Recurring with passing fire_when, past gate",
                "status": "pending",
                "priority": "MEDIUM",
                "recurring": True,
                "interval_hours": 24,
                "lastAchievedAt": _hours_ago_iso(48),
                "fire_when": {
                    "type": "file_check",
                    "path": existing_path,
                    "condition": "exists",
                },
            },
            {
                "id": "g-fail-fresh",
                "title": "Recurring with failing fire_when, gate not elapsed",
                "status": "pending",
                "priority": "MEDIUM",
                "recurring": True,
                "interval_hours": 24,
                "lastAchievedAt": _hours_ago_iso(1),  # 1h < 24h interval
                "fire_when": {
                    "type": "file_check",
                    "path": nonexistent_path,
                    "condition": "exists",
                },
            },
        ]
        _build_aspiration_jsonl(asp_file, goals)

        env = os.environ.copy()
        env["MIND_AGENT"] = agent_name
        proc = subprocess.run(
            [sys.executable, str(SWEEP_PY), "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env,
        )

        out = proc.stdout

        # Assertion 1: the failing-elapsed goal must be advanced (DRY-RUN line).
        if "g-fail-elapsed" not in out or "advanced" not in out:
            failures.append(
                f"[FAIL] failing fire_when past gate not advanced.\n"
                f"  stdout: {out!r}\n  stderr: {proc.stderr!r}"
            )
        else:
            # Also verify it identifies fire_when as the failing gate kind.
            if "fire_when" not in out:
                failures.append(
                    f"[FAIL] advance log missing fire_when gate kind.\n"
                    f"  stdout: {out!r}"
                )
            else:
                print("  [PASS] g-fail-elapsed: advanced with gate_kind=fire_when")

        # Assertion 2: the passing goal must NOT be advanced.
        if "g-pass-elapsed" in out:
            failures.append(
                f"[FAIL] passing fire_when goal incorrectly advanced.\n  stdout: {out!r}"
            )
        else:
            print("  [PASS] g-pass-elapsed: NOT advanced (gate clear)")

        # Assertion 3: failing-but-not-elapsed must NOT be advanced (time gate
        # check happens before fire_when evaluation).
        if "g-fail-fresh" in out:
            failures.append(
                f"[FAIL] failing fire_when within interval incorrectly advanced.\n"
                f"  stdout: {out!r}"
            )
        else:
            print("  [PASS] g-fail-fresh: NOT advanced (within interval)")

    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)
        Path(existing_path).unlink(missing_ok=True)

    print()
    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("All 3 sweep fire_when cases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
