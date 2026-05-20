"""test_goal_duplication_gate_insight_trigger.py — regression test for .

Verifies that the goal-duplication gate skips file_path overlap detection
when the proposed goal's origin_signal indicates a response to an active
insight_trigger from a non-self agent.

Cases covered:
  1. RESPONSE GOAL + matching insight_trigger overlap → would_block=False
     (the rb-591 fix: file_path-only false positives cleared)
  2. NON-RESPONSE GOAL (origin_signal lacks response prefix) + same overlap
     → would_block=True (gate behavior unchanged for de-novo goals)
  3. RESPONSE GOAL + KEYWORD-heavy duplicate → would_block=True
     (file_path skip does NOT weaken the keyword-based duplicate check)
  4. RESPONSE GOAL + NO overlap → would_block=False (trivially)
  5. RESPONSE GOAL + insight_trigger from SAME agent (self) → not exempted
     (a trigger we authored isn't evidence of peer coverage)

The test seeds a synthetic findings.jsonl + team-state recent_completions
ring, runs goal-duplication-gate.py against staged goal JSON, and asserts
the would_block verdict and reason match expectation. Live world files
backed up + restored.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import WORLD_DIR  # type: ignore

FINDINGS_PATH = WORLD_DIR / "board" / "findings.jsonl"
TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"
GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

# Backup paths — same PID suffix so cleanup matches
PID = os.getpid()
FINDINGS_BACKUP = FINDINGS_PATH.with_suffix(f".jsonl.iat-test-backup.{PID}")
TEAM_STATE_BACKUP = TEAM_STATE_PATH.with_suffix(f".yaml.iat-test-backup.{PID}")


def _now_iso(offset_hours: float = 0) -> str:
    """ISO timestamp shifted by offset_hours from now (negative = past)."""
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_state():
    """Write a minimal findings.jsonl (one fresh insight_trigger from bravo
    affecting iter-close-test.sh) and a minimal team-state.yaml with no
    recent_completions. Designed so the gate's insight_trigger check fires
    on overlapping file paths but recent_completions stays clean — tests
    can isolate the file_path skip behavior.
    """
    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    finding = {
        "id": "msg-iat-test-bravo",
        "channel": "findings",
        "type": "finding",
        "author": "bravo",
        "tags": [
            "insight_trigger",
            "severity:constrains",
            "affects:core/scripts/iter-close-test.sh",
            "fresh-eyes-code",
        ],
        "timestamp": _now_iso(-1),  # 1h ago
        "body": "Synthetic finding for g-115-289 regression test",
    }
    with open(FINDINGS_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(finding) + "\n")

    # Seed multiple non-overlapping recent_completions so IDF can distinguish
    # rare identifiers (rare-identifier-foo, unique-marker-baz) from common
    # vocabulary. With N=1 corpus, every term has weight 0 and the gate's
    # WEIGHT_THRESHOLD=1.5 floor can't fire. Bigger corpus = real IDF separation.
    team_state = {
        "strategic_focus": {
            "primary": None, "rationale": None,
            "set_by": None, "set_at": None, "acknowledged_by": [],
        },
        "active_blockers": [],
        "recent_completions": [
            {
                "goal_id": "g-iat-prior",
                "completed_by": "bravo",
                "completed_at": _now_iso(-2),
                "key_finding": (
                    "Modified core/scripts/iter-close-test.sh phase ordering — "
                    "rare-identifier-foo unique-marker-baz fixed."
                ),
            },
            # Filler entries — diverse so IDF gives high weight to terms that
            # appear in only the iat-prior entry. Each filler covers distinct
            # vocabulary that won't collide with the iat-prior content.
            {
                "goal_id": "g-iat-filler-1",
                "completed_by": "bravo",
                "completed_at": _now_iso(-3),
                "key_finding": "Database connection pool tuning — query latency improved.",
            },
            {
                "goal_id": "g-iat-filler-2",
                "completed_by": "bravo",
                "completed_at": _now_iso(-4),
                "key_finding": "API rate limiting retry policy — exponential backoff.",
            },
            {
                "goal_id": "g-iat-filler-3",
                "completed_by": "bravo",
                "completed_at": _now_iso(-5),
                "key_finding": "Frontend rendering performance audit — virtual scrolling.",
            },
            {
                "goal_id": "g-iat-filler-4",
                "completed_by": "bravo",
                "completed_at": _now_iso(-6),
                "key_finding": "Memory leak triage — websocket subscription cleanup.",
            },
        ],
        "agent_status": {},
        "critical_blockers": [],
    }
    with open(TEAM_STATE_PATH, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)


def _run_gate(goal: dict) -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(WORLD_DIR)
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:200]}"
        )
    return json.loads(proc.stdout)


def main() -> int:
    # Backup live files
    if FINDINGS_PATH.exists():
        FINDINGS_BACKUP.write_bytes(FINDINGS_PATH.read_bytes())
    if TEAM_STATE_PATH.exists():
        TEAM_STATE_BACKUP.write_bytes(TEAM_STATE_PATH.read_bytes())

    failures = []

    try:
        _seed_state()

        # Use a unique file path that won't collide with any real recent
        # completions. iter-close-test.sh is deliberately synthetic.
        target_path = "core/scripts/iter-close-test.sh"

        # ── Case 1: response goal + file_path-only overlap → PASS ─────────
        # Description references target_path but uses VOCABULARY DISJOINT from
        # the seeded recent_completions. This isolates the file_path skip:
        # without my fix, the gate would block on path; with the fix and
        # disjoint keywords, it passes.
        case1 = {
            "title": f"Idea: harden {target_path} response handling",
            "description": (
                f"Generic improvements to {target_path} response handling. "
                "Address upstream review concerns. No keyword content "
                "matching prior completion vocabulary."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:bravo-iat-test-response",
        }
        r1 = _run_gate(case1)
        if r1.get("would_block") is not False:
            failures.append(f"CASE 1: response goal blocked unexpectedly. reason={r1.get('reason')}")
        if not r1.get("expected_coverage_paths"):
            failures.append("CASE 1: expected_coverage_paths empty (should contain iter-close-test.sh)")

        # ── Case 2: NON-response goal + path overlap (with rare kws) → BLOCK ─
        # Same path AND rare keywords from seed. Without response prefix,
        # expected_paths is empty so BOTH file_path and keyword overlaps
        # contribute to weighted score → blocks at WEIGHT_THRESHOLD.
        case2 = {
            "title": "Refactor iter-close-test.sh phase ordering",
            "description": (
                f"Standalone refactor of {target_path}. "
                "rare-identifier-foo and unique-marker-baz cleanup."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",  # NOT a response prefix
        }
        r2 = _run_gate(case2)
        if r2.get("would_block") is not True:
            failures.append(f"CASE 2: non-response goal NOT blocked (expected block). reason={r2.get('reason')}")
        if r2.get("expected_coverage_paths"):
            failures.append("CASE 2: expected_coverage_paths populated for non-response goal (should be empty)")

        # ── Case 3: response goal + KEYWORD overlap → BLOCK on keywords ────
        # Force keyword-heavy match on the seeded recent_completion.
        case3 = {
            "title": "Idea: investigate rare-identifier-foo and unique-marker-baz",
            "description": (
                "Generic file investigation for rare-identifier-foo unique-marker-baz "
                "ordering — heavy keyword overlap with bravo's prior completion. "
                "No file_path; keyword duplicate detection should fire."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:bravo-iat-test-keyword-overlap",
        }
        r3 = _run_gate(case3)
        if r3.get("would_block") is not True:
            failures.append(f"CASE 3: keyword duplicate not blocked (expected block). reason={r3.get('reason')}")

        # ── Case 4: response goal + NO overlap → PASS ──────────────────────
        case4 = {
            "title": "Idea: completely-different-name-xyz cleanup",
            "description": "No overlap with any seeded finding or completion.",
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:bravo-iat-test-no-overlap",
        }
        r4 = _run_gate(case4)
        if r4.get("would_block") is not False:
            failures.append(f"CASE 4: no-overlap goal blocked. reason={r4.get('reason')}")

        # ── Case 5: SELF-authored insight_trigger should NOT exempt overlap ─
        # Re-seed findings with author=alpha (self) — the trigger is OUR
        # finding so peer-coverage doesn't apply.
        finding_self = {
            "id": "msg-iat-test-self",
            "channel": "findings",
            "type": "finding",
            "author": "alpha",  # self
            "tags": [
                "insight_trigger",
                "severity:constrains",
                "affects:core/scripts/iter-close-test.sh",
            ],
            "timestamp": _now_iso(-1),
        }
        with open(FINDINGS_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(finding_self) + "\n")

        case5 = {
            "title": "Idea: tighten iter-close-test.sh phase ordering",
            "description": (
                f"Improve {target_path} phase ordering — rare-identifier-foo "
                "unique-marker-baz adjustment per upstream finding."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:alpha-self-trigger-response",
        }
        r5 = _run_gate(case5)
        # When the trigger author is self, expected_coverage_paths is empty;
        # the recent_completions check fires on file_path + keyword overlap.
        if r5.get("expected_coverage_paths"):
            failures.append("CASE 5: self-authored trigger should not populate expected_coverage_paths")
        if r5.get("would_block") is not True:
            failures.append(f"CASE 5: self-trigger response NOT blocked. reason={r5.get('reason')}")

        if failures:
            print(f"TEST FAIL ({len(failures)} case(s)):", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1

        print("TEST PASS: 5 cases — response w/ overlap (pass), non-response w/ overlap (block),")
        print("           response w/ keyword dup (block), response w/o overlap (pass),")
        print("           self-trigger response (block — peer-coverage requires non-self).")
        return 0
    finally:
        # Restore live files
        if FINDINGS_BACKUP.exists():
            FINDINGS_PATH.write_bytes(FINDINGS_BACKUP.read_bytes())
            FINDINGS_BACKUP.unlink()
        elif FINDINGS_PATH.exists():
            FINDINGS_PATH.unlink()
        if TEAM_STATE_BACKUP.exists():
            TEAM_STATE_PATH.write_bytes(TEAM_STATE_BACKUP.read_bytes())
            TEAM_STATE_BACKUP.unlink()


if __name__ == "__main__":
    sys.exit(main())
