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
the would_block verdict and reason match expectation.

Test isolation strategy (g-115-1376): redirect MIND_WORLD to a tmp directory
so the real world/board/findings.jsonl + team-state.yaml are never touched —
the gate's _resolve_world_dir() honors MIND_WORLD as a test-override.
Replaces the prior live-file backup/restore harness (which additionally
unlinked the live findings.jsonl when no backup existed — rb-1547 seed-clobber
/ data-loss risk).

Run via:

    py -3 core/scripts/tests/test_goal_duplication_gate_insight_trigger.py

Also pytest-collectable via test_insight_trigger_gate().
"""

from __future__ import annotations

import json
import os
import shutil
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

GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"


def _now_iso(offset_hours: float = 0) -> str:
    """ISO timestamp shifted by offset_hours from now (negative = past)."""
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_state(tmp_world: Path):
    """Write a minimal findings.jsonl (one fresh insight_trigger from bravo
    affecting iter-close-test.sh) and a minimal team-state.yaml into tmp_world.
    Designed so the gate's insight_trigger check fires on overlapping file
    paths but recent_completions stays clean — tests can isolate the
    file_path skip behavior.
    """
    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
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
    with open(findings_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(finding) + "\n")

    # Seed multiple non-overlapping recent_completions so IDF can distinguish
    # rare identifiers (rare_identifier_foo9, unique_marker_baz7) from common
    # vocabulary. With N=1 corpus, every term has weight 0 and the gate's
    # WEIGHT_THRESHOLD=1.5 floor can't fire. Bigger corpus = real IDF separation.
    # The tokens carry underscores+digits ON PURPOSE (): the
    # recent_completions has_specific co-signal is [_0-9] — hyphen-only
    # fixtures (the pre-2026-07-11 rare-identifier-foo shape) are ordinary
    # compound prose, demoted to advisory, and CASE 3's keyword block would
    # never fire. Do not "simplify" them back to hyphenated forms.
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
                    "rare_identifier_foo9 unique_marker_baz7 fixed."
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
    ts_path = tmp_world / "team-state.yaml"
    with open(ts_path, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)


def _run_gate(goal: dict, tmp_world: Path) -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(tmp_world)
    # Hermetic agent-queue scan (5): keep live agent queues out
    # of the wrapper's pending_queue check (rb-3784 corpus coupling).
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
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
    failures = []

    tmp_world = Path(tempfile.mkdtemp(prefix="iat-test-"))

    try:
        _seed_state(tmp_world)

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
        r1 = _run_gate(case1, tmp_world)
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
                "rare_identifier_foo9 and unique_marker_baz7 cleanup."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",  # NOT a response prefix
        }
        r2 = _run_gate(case2, tmp_world)
        if r2.get("would_block") is not True:
            failures.append(f"CASE 2: non-response goal NOT blocked (expected block). reason={r2.get('reason')}")
        if r2.get("expected_coverage_paths"):
            failures.append("CASE 2: expected_coverage_paths populated for non-response goal (should be empty)")

        # ── Case 3: response goal + KEYWORD overlap → BLOCK on keywords ────
        # Force keyword-heavy match on the seeded recent_completion.
        case3 = {
            "title": "Idea: investigate rare_identifier_foo9 and unique_marker_baz7",
            "description": (
                "Generic file investigation for rare_identifier_foo9 unique_marker_baz7 "
                "ordering — heavy keyword overlap with bravo's prior completion. "
                "No file_path; keyword duplicate detection should fire."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:bravo-iat-test-keyword-overlap",
        }
        r3 = _run_gate(case3, tmp_world)
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
        r4 = _run_gate(case4, tmp_world)
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
        findings_path = tmp_world / "board" / "findings.jsonl"
        with open(findings_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(finding_self) + "\n")

        case5 = {
            "title": "Idea: tighten iter-close-test.sh phase ordering",
            "description": (
                f"Improve {target_path} phase ordering — rare_identifier_foo9 "
                "unique_marker_baz7 adjustment per upstream finding."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:alpha-self-trigger-response",
        }
        r5 = _run_gate(case5, tmp_world)
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
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)


def test_insight_trigger_gate():
    """Pytest entry point (6) — runs the 5-case suite (tmp-world
    isolated) and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
