"""test_goal_duplication_gate_partner_in_flight.py — regression test for .

Verifies that the goal-duplication gate detects cross-agent in-flight scope
collisions: a non-self agent has claimed and is mid-execution on a goal
whose title overlaps the scope of the proposed (new) goal.

Closes the cross-agent collision class observed 2026-05-11 alpha session-65
(g-115-633, g-115-634, g-248-85 — rb-846): three independent agents
converged on bit-identical implementations of DIFFERENT goal-ids that shared
scope. The recent_completions check only catches duplication AFTER the
partner finishes; this new partner_in_flight check fires DURING.

Cases covered:
  1. partner in_flight with HIGHLY OVERLAPPING title → would_block=True
     (canonical: bravo working on execution-diary observer gate while
     alpha is about to file the same)
  2. partner in_flight with DIFFERENT scope → would_block=False
     (filler signal doesn't trip the gate)
  3. No partners in_flight (all null) → would_block=False (skip path)
  4. ONLY SELF has in_flight (partners null) → would_block=False
     (self-in-flight is not evidence of partner work)
  5. partner in_flight on the SAME GOAL-ID as proposed → would_block=False
     (id reuse is a different bug; partner-claim filter handles it at
     selection time, not filing time)
  6. Multiple partners, one overlapping → would_block=True
     (still fires even with mixed signal)

Test isolation strategy (g-115-1376): redirect MIND_WORLD to a tmp directory
so the real world/team-state.yaml + board/findings.jsonl are never touched —
the gate's _resolve_world_dir() honors MIND_WORLD as a test-override, so the
partner_in_flight check reads the seeded tmp fixtures. The gate's project_root
still resolves to the real repo (PROJECT_ROOT/agents/*/aspirations.jsonl is
read live), but the fixtures use uniquely-tagged synthetic identifiers
("g-pif-…") that cannot collide with anything in real queues. Replaces the
prior live-file backup/restore harness (rb-1547 seed-clobber race).

Mirrors the gold-standard pending_queue variant. Run via:

    py -3 core/scripts/tests/test_goal_duplication_gate_partner_in_flight.py

Looks for "PASS (6/6 cases)" in stdout. Also pytest-collectable via
test_partner_in_flight_gate().
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


def _seed_state(tmp_world: Path, *, bravo_inflight=None, zeta_inflight=None,
                alpha_inflight=None, recent_completions=None):
    """Write a minimal team-state.yaml (into tmp_world) with controlled
    in_flight values.

    None for any agent slot → that agent's in_flight is null.
    bravo_inflight/zeta_inflight/alpha_inflight: dict with goal_id/title/phase/claimed_at.

    Also blanks tmp_world/board/findings.jsonl so insight_triggers check can't
    fire on stale fixtures. recent_completions seeded with non-overlapping
    filler so _check_recent_completions stays clean (we're isolating the
    in_flight check).
    """
    if recent_completions is None:
        recent_completions = [
            {
                "goal_id": "g-pif-filler-1",
                "completed_by": "bravo",
                "completed_at": _now_iso(-3),
                "key_finding": "Unrelated database migration.",
            },
            {
                "goal_id": "g-pif-filler-2",
                "completed_by": "zeta",
                "completed_at": _now_iso(-4),
                "key_finding": "Unrelated frontend rendering audit.",
            },
        ]

    agent_status = {
        "alpha": {
            "last_active": _now_iso(0),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": alpha_inflight,
        },
        "bravo": {
            "last_active": _now_iso(-0.1),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": bravo_inflight,
        },
        "zeta": {
            "last_active": _now_iso(-0.1),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": zeta_inflight,
        },
    }

    team_state = {
        "strategic_focus": {
            "primary": None, "rationale": None,
            "set_by": None, "set_at": None, "acknowledged_by": [],
        },
        "active_blockers": [],
        "recent_completions": recent_completions,
        "agent_status": agent_status,
        "critical_blockers": [],
    }
    ts_path = tmp_world / "team-state.yaml"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ts_path, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)

    # Empty findings.jsonl so insight_triggers check is clean.
    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(findings_path, "w", encoding="utf-8") as f:
        f.write("")


def _run_gate(goal: dict, tmp_world: Path, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(tmp_world)
    # Hermetic agent-queue scan (): keep live agent queues out
    # of the wrapper's pending_queue check (rb-3784 corpus coupling).
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
    # : pin local backend so load_rows_authoritative delegates to
    # load_rows deterministically (no S3), independent of the ambient
    # STORAGE_BACKEND on an own-cloud box (guard-955).
    env["STORAGE_BACKEND"] = "local"
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


def _find_check(result, name):
    for c in result.get("checks", []):
        if c.get("name") == name:
            return c
    return None


def main() -> int:
    failures = []

    # Create tmp world dir; cleaned up in finally.
    tmp_world = Path(tempfile.mkdtemp(prefix="pif-test-"))

    try:
        # ── Case 1: partner in_flight with overlapping scope → BLOCK ─────
        _seed_state(tmp_world, bravo_inflight={
            "goal_id": "g-pif-bravo-001",
            "title": "Apply: execution-diary observer-session gate write paths",
            "phase": "4",
            "claimed_at": _now_iso(-0.2),
        })
        case1 = {
            "title": "Idea: execution-diary observer-session gate for cmd_append",
            "description": (
                "Add observer-session guard to execution-diary cmd_append. "
                "Skip writes when MIND_SID differs from running-session-id."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",
        }
        r1 = _run_gate(case1, tmp_world)
        pif1 = _find_check(r1, "partner_in_flight")
        if pif1 is None:
            failures.append("CASE 1: partner_in_flight check missing from result")
        elif pif1.get("passed") is not False:
            failures.append(
                f"CASE 1: partner_in_flight should have failed. "
                f"reason={pif1.get('reason')} matches={pif1.get('matches')}"
            )
        elif r1.get("would_block") is not True:
            failures.append(f"CASE 1: would_block should be True. result={r1.get('reason')}")

        # ── Case 2: partner in_flight with DIFFERENT scope → PASS ────────
        _seed_state(tmp_world, bravo_inflight={
            "goal_id": "g-pif-bravo-002",
            "title": "Reflect: hippocampal replay over resolved hypotheses",
            "phase": "4",
            "claimed_at": _now_iso(-0.2),
        })
        case2 = {
            "title": "Idea: execution-diary observer-session gate for cmd_append",
            "description": (
                "Add observer-session guard to execution-diary cmd_append. "
                "Skip writes when MIND_SID differs from running-session-id."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",
        }
        r2 = _run_gate(case2, tmp_world)
        pif2 = _find_check(r2, "partner_in_flight")
        if pif2 is None:
            failures.append("CASE 2: partner_in_flight check missing")
        elif pif2.get("passed") is not True:
            failures.append(
                f"CASE 2: partner_in_flight should have passed (different scope). "
                f"reason={pif2.get('reason')} matches={pif2.get('matches')}"
            )

        # ── Case 3: No partners in_flight (all null) → PASS ──────────────
        _seed_state(tmp_world)  # all in_flight default to None
        case3 = {
            "title": "Idea: execution-diary observer-session gate",
            "description": "Add observer-session guard to execution-diary writes.",
            "participants": ["agent"],
            "source": "world",
        }
        r3 = _run_gate(case3, tmp_world)
        pif3 = _find_check(r3, "partner_in_flight")
        if pif3 is None:
            failures.append("CASE 3: partner_in_flight check missing")
        elif pif3.get("passed") is not True:
            failures.append(
                f"CASE 3: partner_in_flight should have passed (no in_flight). "
                f"reason={pif3.get('reason')}"
            )
        elif "no partners in_flight" not in (pif3.get("reason") or ""):
            failures.append(f"CASE 3: reason mismatch — got {pif3.get('reason')}")

        # ── Case 4: ONLY SELF in_flight → PASS ───────────────────────────
        # Self-in-flight should not trigger the check; only partners count.
        _seed_state(tmp_world, alpha_inflight={
            "goal_id": "g-pif-self-001",
            "title": "Apply: execution-diary observer-session gate write paths",
            "phase": "4",
            "claimed_at": _now_iso(-0.1),
        })
        case4 = {
            "title": "Idea: execution-diary observer-session gate for another path",
            "description": "Generic execution-diary observer guard work.",
            "participants": ["agent"],
            "source": "world",
        }
        r4 = _run_gate(case4, tmp_world)
        pif4 = _find_check(r4, "partner_in_flight")
        if pif4 is None:
            failures.append("CASE 4: partner_in_flight check missing")
        elif pif4.get("passed") is not True:
            failures.append(
                f"CASE 4: partner_in_flight should have passed (self-only in_flight). "
                f"reason={pif4.get('reason')} matches={pif4.get('matches')}"
            )

        # ── Case 5: Partner in_flight on SAME goal-id as proposed → PASS ─
        # Id reuse is a different bug — partner-claim filter handles it at
        # selection time, not at filing time. This check should skip.
        _seed_state(tmp_world, bravo_inflight={
            "goal_id": "g-pif-same-001",
            "title": "Apply: execution-diary observer-session gate identical scope",
            "phase": "4",
            "claimed_at": _now_iso(-0.2),
        })
        case5 = {
            "id": "g-pif-same-001",  # same id as partner's in_flight
            "title": "Apply: execution-diary observer-session gate identical scope",
            "description": "Whatever the partner has, we have.",
            "participants": ["agent"],
            "source": "world",
        }
        r5 = _run_gate(case5, tmp_world)
        pif5 = _find_check(r5, "partner_in_flight")
        if pif5 is None:
            failures.append("CASE 5: partner_in_flight check missing")
        elif pif5.get("passed") is not True:
            failures.append(
                f"CASE 5: partner_in_flight should have skipped same-id. "
                f"reason={pif5.get('reason')} matches={pif5.get('matches')}"
            )

        # ── Case 6: Multiple partners, one overlapping → BLOCK ───────────
        _seed_state(
            tmp_world,
            bravo_inflight={
                "goal_id": "g-pif-bravo-006",
                "title": "Reflect: micro-hypothesis sweep",  # not overlapping
                "phase": "4",
                "claimed_at": _now_iso(-0.2),
            },
            zeta_inflight={
                "goal_id": "g-pif-zeta-006",
                "title": "Apply: execution-diary observer-session gate write paths",
                "phase": "4",
                "claimed_at": _now_iso(-0.1),
            },
        )
        case6 = {
            "title": "Idea: execution-diary observer-session gate cmd_append",
            "description": "Observer guard for execution-diary writes.",
            "participants": ["agent"],
            "source": "world",
        }
        r6 = _run_gate(case6, tmp_world)
        pif6 = _find_check(r6, "partner_in_flight")
        if pif6 is None:
            failures.append("CASE 6: partner_in_flight check missing")
        elif pif6.get("passed") is not False:
            failures.append(
                f"CASE 6: partner_in_flight should have failed (zeta overlaps). "
                f"reason={pif6.get('reason')} matches={pif6.get('matches')}"
            )
        elif pif6.get("matches"):
            matched_agents = [m.get("agent") for m in pif6["matches"]]
            if "zeta" not in matched_agents:
                failures.append(
                    f"CASE 6: expected zeta in matches, got {matched_agents}"
                )
            if "bravo" in matched_agents:
                failures.append(
                    "CASE 6: bravo should NOT match (different scope)"
                )

    finally:
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (6/6 cases)")
    return 0


def test_partner_in_flight_gate():
    """Pytest entry point () — runs the 6-case suite (tmp-world
    isolated) and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
