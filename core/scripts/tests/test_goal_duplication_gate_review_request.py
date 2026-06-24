"""test_goal_duplication_gate_review_request.py — regression test for .

Verifies that goal-duplication-gate's `_expected_coverage_paths` extension
covers cross-agent coordination/review-request posts with affects:<file>
tags, in addition to the original findings/insight_trigger source.

Cases covered:
  1. RESPONSE GOAL + matching review-request affects: tag → would_block=False
     (the g-115-359 fix: review-request posts populate expected_coverage_paths
      so a reviewer's response Investigate goal clears without --override-duplication)
  2. NON-RESPONSE GOAL + same review-request → would_block=True
     (gate behavior unchanged — only response goals get the carve-out)
  3. RESPONSE GOAL + review-request from SAME agent (self) → not exempted
     (peer-coverage requires non-self author; same rule as insight_trigger source)
  4. RESPONSE GOAL + STALE review-request (>24h) → not exempted
     (cutoff applies to coordination scan as well; matches findings cutoff)
  5. RESPONSE GOAL + WRONG TYPE coordination post (status, not review-request)
     → not exempted (type filter excludes non-review-request posts)
  6. RESPONSE GOAL + UNION of insight_trigger + review-request → BOTH paths
     contribute (regression check that both sources still scan after refactor)

Test isolation strategy (g-115-1376): redirect MIND_WORLD to a tmp directory
so the real world/board/findings.jsonl, board/coordination.jsonl, and
team-state.yaml are never touched — the gate's _resolve_world_dir() honors
MIND_WORLD as a test-override. Replaces the prior live-file backup/restore
harness (which additionally unlinked the live board files when no backup
existed — rb-1547 seed-clobber / data-loss risk).

Run via:

    py -3 core/scripts/tests/test_goal_duplication_gate_review_request.py

Also pytest-collectable via test_review_request_gate().
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
    return (datetime.now() + timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_clean_state(tmp_world: Path):
    """Empty findings.jsonl + empty coordination.jsonl + filler team-state
    (all inside tmp_world). Each test case re-seeds the relevant board file
    in-place.
    """
    board_dir = tmp_world / "board"
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "findings.jsonl").write_text("", encoding="utf-8")
    (board_dir / "coordination.jsonl").write_text("", encoding="utf-8")

    team_state = {
        "strategic_focus": {
            "primary": None, "rationale": None,
            "set_by": None, "set_at": None, "acknowledged_by": [],
        },
        "active_blockers": [],
        "recent_completions": [
            {
                "goal_id": "g-rrt-prior",
                "completed_by": "alpha",
                "completed_at": _now_iso(-2),
                "key_finding": (
                    "Modified core/scripts/rrt-target.sh — distinct-tag-alpha "
                    "distinct-tag-beta added."
                ),
            },
            {
                "goal_id": "g-rrt-filler-1",
                "completed_by": "alpha",
                "completed_at": _now_iso(-3),
                "key_finding": "Database connection pool tuning.",
            },
            {
                "goal_id": "g-rrt-filler-2",
                "completed_by": "alpha",
                "completed_at": _now_iso(-4),
                "key_finding": "API rate limiting retry policy.",
            },
            {
                "goal_id": "g-rrt-filler-3",
                "completed_by": "alpha",
                "completed_at": _now_iso(-5),
                "key_finding": "Frontend rendering performance audit.",
            },
            {
                "goal_id": "g-rrt-filler-4",
                "completed_by": "alpha",
                "completed_at": _now_iso(-6),
                "key_finding": "Memory leak triage — websocket cleanup.",
            },
        ],
        "agent_status": {},
        "critical_blockers": [],
    }
    with open(tmp_world / "team-state.yaml", "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)


def _write_review_request(tmp_world: Path, author: str, ts_offset_hours: float, affects_paths):
    affects_tags = [f"affects:{p}" for p in affects_paths]
    rec = {
        "id": f"msg-rrt-{author}-{int(ts_offset_hours * 100)}",
        "channel": "coordination",
        "type": "review-request",
        "author": author,
        "timestamp": _now_iso(ts_offset_hours),
        "tags": ["review", *affects_tags],
        "text": f"Review request synthetic — affects {','.join(affects_paths)}",
    }
    with open(tmp_world / "board" / "coordination.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _write_status_post(tmp_world: Path, author: str, ts_offset_hours: float, affects_paths):
    """Same affects: tags but type=status — should be excluded by type filter."""
    affects_tags = [f"affects:{p}" for p in affects_paths]
    rec = {
        "id": f"msg-rrt-status-{author}-{int(ts_offset_hours * 100)}",
        "channel": "coordination",
        "type": "status",
        "author": author,
        "timestamp": _now_iso(ts_offset_hours),
        "tags": ["status", *affects_tags],
        "text": f"Status — affects {','.join(affects_paths)}",
    }
    with open(tmp_world / "board" / "coordination.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _write_insight_trigger(tmp_world: Path, author: str, ts_offset_hours: float, affects_paths):
    affects_tags = [f"affects:{p}" for p in affects_paths]
    rec = {
        "id": f"msg-rrt-it-{author}-{int(ts_offset_hours * 100)}",
        "channel": "findings",
        "type": "finding",
        "author": author,
        "timestamp": _now_iso(ts_offset_hours),
        "tags": ["insight_trigger", "severity:informs", *affects_tags],
        "text": f"Insight trigger synthetic — affects {','.join(affects_paths)}",
    }
    with open(tmp_world / "board" / "findings.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _run_gate(goal: dict, tmp_world: Path, self_agent: str = "bravo") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = self_agent
    env["MIND_WORLD"] = str(tmp_world)
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
    target_path = "core/scripts/rrt-target.sh"

    tmp_world = Path(tempfile.mkdtemp(prefix="rrt-test-"))

    try:
        # ── Case 1: response goal + fresh review-request from peer → PASS ──
        _seed_clean_state(tmp_world)
        _write_review_request(tmp_world, "alpha", -1, [target_path])
        case1 = {
            "title": f"Investigate: review {target_path} from alpha commit",
            "description": (
                f"Cross-agent review of alpha's recent change to {target_path}. "
                "Generic review wording disjoint from prior completion vocabulary."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "investigate:review-alpha-msg-rrt-1",
        }
        r1 = _run_gate(case1, tmp_world, self_agent="bravo")
        if r1.get("would_block") is not False:
            failures.append(f"CASE 1: response goal blocked unexpectedly. reason={r1.get('reason')}")
        if not r1.get("expected_coverage_paths"):
            failures.append("CASE 1: expected_coverage_paths empty (should contain rrt-target.sh from review-request)")
        elif target_path not in r1.get("expected_coverage_paths", []):
            failures.append(f"CASE 1: expected_coverage_paths missing {target_path}: {r1.get('expected_coverage_paths')}")

        # ── Case 2: NON-response goal + same review-request → BLOCK ─────────
        _seed_clean_state(tmp_world)
        _write_review_request(tmp_world, "alpha", -1, [target_path])
        case2 = {
            "title": f"Refactor {target_path} cleanup",
            "description": (
                f"Standalone refactor of {target_path}. distinct-tag-alpha "
                "distinct-tag-beta cleanup."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",
        }
        r2 = _run_gate(case2, tmp_world, self_agent="bravo")
        if r2.get("would_block") is not True:
            failures.append(f"CASE 2: non-response goal NOT blocked (expected block). reason={r2.get('reason')}")
        if r2.get("expected_coverage_paths"):
            failures.append("CASE 2: expected_coverage_paths populated for non-response goal (should be empty)")

        # ── Case 3: SELF-authored review-request should NOT exempt ─────────
        _seed_clean_state(tmp_world)
        _write_review_request(tmp_world, "bravo", -1, [target_path])  # self
        case3 = {
            "title": f"Investigate: own review of {target_path}",
            "description": (
                f"Self-authored review of {target_path} — distinct-tag-alpha "
                "distinct-tag-beta investigation."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "investigate:self-review-msg-rrt",
        }
        r3 = _run_gate(case3, tmp_world, self_agent="bravo")
        if r3.get("expected_coverage_paths"):
            failures.append(f"CASE 3: self-authored review-request should not populate expected_coverage_paths: {r3.get('expected_coverage_paths')}")
        if r3.get("would_block") is not True:
            failures.append(f"CASE 3: self-authored review NOT blocked. reason={r3.get('reason')}")

        # ── Case 4: STALE review-request (>24h) → not exempted ──────────────
        _seed_clean_state(tmp_world)
        _write_review_request(tmp_world, "alpha", -48, [target_path])  # 48h ago, beyond 24h cutoff
        case4 = {
            "title": f"Investigate: stale review of {target_path}",
            "description": (
                f"Review of {target_path} — distinct-tag-alpha distinct-tag-beta "
                "investigation following stale signal."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "investigate:stale-msg-rrt",
        }
        r4 = _run_gate(case4, tmp_world, self_agent="bravo")
        if r4.get("expected_coverage_paths"):
            failures.append(f"CASE 4: stale review-request (>24h) should not populate expected_coverage_paths: {r4.get('expected_coverage_paths')}")

        # ── Case 5: WRONG TYPE (status, not review-request) → not exempted ──
        _seed_clean_state(tmp_world)
        _write_status_post(tmp_world, "alpha", -1, [target_path])  # type=status, not review-request
        case5 = {
            "title": f"Investigate: response to status post about {target_path}",
            "description": (
                f"Investigation triggered by status post about {target_path}. "
                "distinct-tag-alpha distinct-tag-beta investigation."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "investigate:status-post-rrt",
        }
        r5 = _run_gate(case5, tmp_world, self_agent="bravo")
        if r5.get("expected_coverage_paths"):
            failures.append(f"CASE 5: type=status post should not populate expected_coverage_paths (only review-request): {r5.get('expected_coverage_paths')}")

        # ── Case 6: UNION of both sources — insight_trigger + review-request ──
        _seed_clean_state(tmp_world)
        path_a = "core/scripts/rrt-target-a.sh"
        path_b = "core/scripts/rrt-target-b.sh"
        _write_insight_trigger(tmp_world, "alpha", -1, [path_a])
        _write_review_request(tmp_world, "alpha", -1, [path_b])
        case6 = {
            "title": f"Investigate: cross-cutting review of {path_a} and {path_b}",
            "description": (
                f"Investigation covering both {path_a} (cited by insight_trigger) "
                f"and {path_b} (cited by review-request). Generic disjoint wording."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "investigate:union-rrt-it",
        }
        r6 = _run_gate(case6, tmp_world, self_agent="bravo")
        ecp6 = r6.get("expected_coverage_paths", [])
        if path_a not in ecp6:
            failures.append(f"CASE 6: insight_trigger source missing from union. ecp={ecp6}")
        if path_b not in ecp6:
            failures.append(f"CASE 6: review-request source missing from union. ecp={ecp6}")

        if failures:
            print(f"TEST FAIL ({len(failures)} case(s)):", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            return 1

        print("TEST PASS: 6 cases —")
        print("  1. response + fresh peer review-request    → cleared (no override)")
        print("  2. non-response + review-request           → blocked (carve-out gated by origin_signal)")
        print("  3. response + self-authored review-request → blocked (peer-coverage requires non-self)")
        print("  4. response + stale review-request (>24h)  → not exempted (cutoff applies)")
        print("  5. response + type=status post             → not exempted (type filter)")
        print("  6. response + insight_trigger + review-request → both sources unioned")
        return 0
    finally:
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)


def test_review_request_gate():
    """Pytest entry point (6) — runs the 6-case suite (tmp-world
    isolated) and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
