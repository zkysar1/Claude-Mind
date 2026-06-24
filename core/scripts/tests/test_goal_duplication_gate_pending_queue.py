"""test_goal_duplication_gate_pending_queue.py — regression test for .

Verifies the pending_queue check added to goal-duplication-gate that closes
the missing-CORPUS gap surfaced by the 4-way l1-skew duplicate cluster
(g-115-743 + g-115-776/778/779; bravo s75 2026-05-15). The other five
checks (recent_completions, partner_in_flight, git_log_48h,
insight_triggers, target_state) scan completed/in-flight/git/board/target
sources but NEVER read the pending or in-progress queue — so a proposal
whose semantic twin was already PENDING (not completed, not in-flight, not
yet in git, not the subject of an active insight_trigger) slipped cleanly.

Match strategies covered:
  P1 origin_signal exact match            → BLOCK (symptom-keyed identity)
  P2 file-path overlap                    → BLOCK (structural co-signal)
  P3 plain-words overlap                  → DEMOTE (advisory only — mirrors
                                            recent_completions structural-
                                            co-signal discipline)
  P4 unrelated pending goal               → PASS  (no overlap)
  P5 COMPLETED goal in queue with overlap → PASS  (status filter excludes
                                            completed; that path is the
                                            existing recent_completions
                                            check's territory)
  P6 empty queue                          → PASS  (no candidates)

Test isolation strategy: redirect MIND_WORLD to a tmp directory so the
real world/aspirations.jsonl is never touched (the file can be multi-MB
and modification is risky). The gate's project_root still resolves to the
real repo, so PROJECT_ROOT/agents/*/aspirations.jsonl is read live — but
the test fixtures use uniquely-tagged synthetic identifiers
("pendqtest12345-…") that cannot collide with anything in real queues.

Mirrors the standalone-script convention used by sibling tests
(partner_in_flight, structural_co_signal, insight_trigger, review_request).
Run via:

    py -3 core/scripts/tests/test_goal_duplication_gate_pending_queue.py

Looks for "PASS (6/6 cases)" in stdout. Wired into verify-learning at
Section PQ (filed via the Maintain follow-up to g-115-783).
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
    import yaml  # type: ignore
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

GATE_PY = CORE_SCRIPTS / "goal-duplication-gate.py"

# Unique synthetic tag — guards against any collision with real agent queue
# scans (agent dirs are read live; only world is redirected to tmp).
TAG = "pendqtest12345"


def _now_iso(offset_hours: float = 0) -> str:
    return (datetime.now() +
            timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_world(tmp_world: Path, aspirations_records: list,
                team_state: dict | None = None,
                findings_lines: list | None = None):
    """Write minimal world fixtures into the tmp dir:
      - tmp_world/aspirations.jsonl with the supplied aspiration records
      - tmp_world/team-state.yaml with a minimal benign agent_status
      - tmp_world/board/findings.jsonl (empty unless overridden)
    """
    asp_path = tmp_world / "aspirations.jsonl"
    asp_path.parent.mkdir(parents=True, exist_ok=True)
    with open(asp_path, "w", encoding="utf-8") as f:
        for rec in aspirations_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if team_state is None:
        team_state = {
            "strategic_focus": {
                "primary": None, "rationale": None,
                "set_by": None, "set_at": None, "acknowledged_by": [],
            },
            "active_blockers": [],
            "recent_completions": [],
            "agent_status": {
                "alpha": {
                    "last_active": _now_iso(0),
                    "current_focus": "",
                    "session_goals_completed": 0,
                    "live_phase": "between-phases",
                    "in_flight": None,
                },
            },
            "critical_blockers": [],
        }
    ts_path = tmp_world / "team-state.yaml"
    with open(ts_path, "w", encoding="utf-8") as f:
        yaml.dump(team_state, f, default_flow_style=False, sort_keys=False)

    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(findings_path, "w", encoding="utf-8") as f:
        for line in (findings_lines or []):
            f.write(line + "\n")


def _run_gate(goal: dict, tmp_world: Path, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(tmp_world)
    proc = subprocess.run(
        [sys.executable, str(GATE_PY)],
        input=json.dumps(goal),
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"goal-duplication-gate exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return json.loads(proc.stdout)


def _find_check(result, name):
    for c in result.get("checks", []):
        if c.get("name") == name:
            return c
    return None


def _mk_aspiration(asp_id: str, goals: list) -> dict:
    """Wrap goals in a minimal aspiration envelope matching the
    aspirations.jsonl shape."""
    return {
        "id": asp_id,
        "title": f"Test aspiration {asp_id}",
        "status": "active",
        "scope": "sprint",
        "goals": goals,
    }


def main() -> int:
    failures = []

    # Create tmp world dir; cleaned up in finally.
    tmp_world = Path(tempfile.mkdtemp(prefix=f"pendq-test-{TAG}-"))

    try:
        # ── P1: origin_signal exact match → BLOCK ────────────────────────
        # An existing pending goal with origin_signal X. The proposed goal
        # carries the same origin_signal X. Symptom-keyed identity = strong
        # match, hard block. Canonical incident shape: a board-driven
        # filer (e.g., alert-sweep, sq-013 handler) emits the same
        # symptom-keyed origin_signal twice across iterations because the
        # other 5 corpora don't see the first filing yet.
        existing_origin = f"idea:{TAG}-p1-origin-gap"
        seed_p1 = _mk_aspiration("asp-pendq-p1", [{
            "id": "g-pendq-p1-existing",
            "title": "Idea: pendq p1 placeholder unrelated prose",
            "description": "Some pending work description text here.",
            "status": "pending",
            "origin_signal": existing_origin,
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p1])

        case_p1 = {
            "title": "Idea: pendq p1 completely different title prose",
            "description": ("Different description body to ensure ONLY "
                            "the origin_signal match strategy fires, not "
                            "the structural overlap path."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": existing_origin,
        }
        rp1 = _run_gate(case_p1, tmp_world)
        pq1 = _find_check(rp1, "pending_queue")
        if pq1 is None:
            failures.append("P1: pending_queue check missing from result")
        elif pq1.get("passed") is not False:
            failures.append(
                f"P1: pending_queue should have failed (origin_signal exact). "
                f"reason={pq1.get('reason')} matches={pq1.get('matches')}"
            )
        else:
            strategies = {m.get("match_strategy")
                          for m in pq1.get("matches") or []}
            if "origin_signal" not in strategies:
                failures.append(
                    f"P1: expected origin_signal in match_strategy set, "
                    f"got strategies={strategies}"
                )

        # ── P2: file-path overlap → BLOCK ────────────────────────────────
        # No origin_signal collision; structural co-signal via shared
        # file path. Mirrors the structural_co_signal G2 case for
        # the recent_completions check, applied to the pending queue.
        synthetic_path = f"core/scripts/{TAG}-p2-synthetic.py"
        seed_p2 = _mk_aspiration("asp-pendq-p2", [{
            "id": "g-pendq-p2-existing",
            "title": f"Investigate: {TAG} p2 work in {synthetic_path}",
            "description": (f"Existing pending goal references "
                            f"{synthetic_path} and cache layering."),
            "status": "pending",
            "origin_signal": f"investigate:{TAG}-p2-existing",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p2])

        case_p2 = {
            "title": f"Investigate: new work touching {synthetic_path}",
            "description": (f"Refactor {synthetic_path} cache handling "
                            f"with attention to the {TAG} pattern."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"investigate:{TAG}-p2-proposed-distinct",
        }
        rp2 = _run_gate(case_p2, tmp_world)
        pq2 = _find_check(rp2, "pending_queue")
        if pq2 is None:
            failures.append("P2: pending_queue check missing from result")
        elif pq2.get("passed") is not False:
            failures.append(
                f"P2: pending_queue should have failed (file-path block). "
                f"reason={pq2.get('reason')} matches={pq2.get('matches')}"
            )
        else:
            matches = pq2.get("matches") or []
            if not any(m.get("file_path_hits") for m in matches):
                failures.append(
                    f"P2: expected file_path_hits in matches, got "
                    f"{[m.get('file_path_hits') for m in matches]}"
                )

        # ── P3: plain-words overlap → DEMOTE (advisory only) ─────────────
        # Strong vocabulary overlap (cache + streaming + buffer) but NO
        # file path and NO structured identifier in the hit keywords.
        # Mirrors structural_co_signal G1 — must demote to advisory.
        seed_p3 = _mk_aspiration("asp-pendq-p3", [{
            "id": "g-pendq-p3-existing",
            "title": "Refactor cache streaming buffer monitoring metrics",
            "description": ("Existing pending goal about cache streaming "
                            "buffer handling in the monitoring metrics "
                            "stack — pure plain-words overlap."),
            "status": "pending",
            "origin_signal": f"idea:{TAG}-p3-existing-distinct",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p3])

        case_p3 = {
            "title": "Optimize cache streaming buffer in metrics monitoring",
            "description": ("Tune cache streaming buffer concurrency in "
                            "the metrics monitoring pipeline for the "
                            "throughput stack."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p3-proposed-distinct",
        }
        rp3 = _run_gate(case_p3, tmp_world)
        pq3 = _find_check(rp3, "pending_queue")
        if pq3 is None:
            failures.append("P3: pending_queue check missing from result")
        elif pq3.get("passed") is not True:
            failures.append(
                f"P3: pending_queue should have passed (plain-words demote). "
                f"reason={pq3.get('reason')} matches={pq3.get('matches')}"
            )
        else:
            advisories = pq3.get("advisories") or []
            strong_only = [a for a in advisories
                           if a.get("strong_keyword_only")]
            if not strong_only:
                failures.append(
                    f"P3: expected strong_keyword_only advisory (demoted "
                    f"overlap), got advisories={advisories}"
                )

        # ── P4: unrelated pending goal → PASS ────────────────────────────
        # Pending goal in queue with no overlap. Gate stays quiet.
        seed_p4 = _mk_aspiration("asp-pendq-p4", [{
            "id": "g-pendq-p4-existing",
            "title": "Unrelated pending work about something completely "
                     "different from the proposed goal.",
            "description": ("Database migration for the user-preferences "
                            "table schema."),
            "status": "pending",
            "origin_signal": f"investigate:{TAG}-p4-existing-distinct",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p4])

        case_p4 = {
            "title": f"Idea: {TAG} p4 proposed work distinct topic",
            "description": (f"Proposed goal about {TAG} p4 with no "
                            f"overlap to the migration goal."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p4-proposed-distinct",
        }
        rp4 = _run_gate(case_p4, tmp_world)
        pq4 = _find_check(rp4, "pending_queue")
        if pq4 is None:
            failures.append("P4: pending_queue check missing from result")
        elif pq4.get("passed") is not True:
            failures.append(
                f"P4: pending_queue should have passed (no overlap). "
                f"reason={pq4.get('reason')} matches={pq4.get('matches')}"
            )

        # ── P5: COMPLETED goal in queue with overlap → PASS ──────────────
        # The pending_queue scan filters status NOT in (pending,
        # in-progress). Completed goals in the queue must NOT block —
        # that surface is already covered by the recent_completions check
        # against team-state. Test that status filter works.
        synthetic_path_p5 = f"core/scripts/{TAG}-p5-completed.py"
        seed_p5 = _mk_aspiration("asp-pendq-p5", [{
            "id": "g-pendq-p5-existing",
            "title": f"Apply: work in {synthetic_path_p5}",
            "description": (f"Already-completed work in "
                            f"{synthetic_path_p5}. "
                            f"Should be invisible to pending_queue scan."),
            "status": "completed",
            "origin_signal": f"apply:{TAG}-p5-existing-distinct",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p5])

        case_p5 = {
            "title": f"Idea: new work touching {synthetic_path_p5}",
            "description": (f"Proposed goal that would overlap with a "
                            f"completed goal in {synthetic_path_p5} — "
                            f"but the completed goal must not block via "
                            f"pending_queue."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p5-proposed-distinct",
        }
        rp5 = _run_gate(case_p5, tmp_world)
        pq5 = _find_check(rp5, "pending_queue")
        if pq5 is None:
            failures.append("P5: pending_queue check missing from result")
        elif pq5.get("passed") is not True:
            failures.append(
                f"P5: pending_queue should have passed (completed-status "
                f"goals filtered out). reason={pq5.get('reason')} "
                f"matches={pq5.get('matches')}"
            )

        # ── P6: empty world queue → PASS ─────────────────────────────────
        # No aspirations in queue. Gate stays quiet (no candidates).
        _seed_world(tmp_world, [])

        case_p6 = {
            "title": f"Idea: {TAG} p6 proposed work empty-queue case",
            "description": (f"Proposed goal {TAG} p6 with empty world "
                            f"aspirations queue."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p6-proposed-distinct",
        }
        rp6 = _run_gate(case_p6, tmp_world)
        pq6 = _find_check(rp6, "pending_queue")
        if pq6 is None:
            failures.append("P6: pending_queue check missing from result")
        elif pq6.get("passed") is not True:
            failures.append(
                f"P6: pending_queue should have passed (empty queue). "
                f"reason={pq6.get('reason')} matches={pq6.get('matches')}"
            )

        # ── P7: decomposition siblings (same parent origin) → PASS ────────
        # 6: filing the 2nd+ child of one parent shares the parent's
        # origin_signal ("decomposition:<parent>") BY DESIGN. The siblings are
        # DISTINCT deliverables (low structural overlap). Strategy 1 must NOT
        # exact-match-block them (the prefix is exempt); Strategy 2 must not
        # fire on the low overlap. WITHOUT the fix this case BLOCKS via
        # origin_signal. Canonical incident:  vs .
        sib_origin = f"decomposition:g-{TAG}-parent"
        seed_p7 = _mk_aspiration("asp-pendq-p7", [{
            "id": "g-pendq-p7-existing",
            "title": f"Apply: {TAG}-aaa writer persistence path",
            "description": (f"First decomposition child: records persisted at "
                            f"the {TAG}-aaa observation site."),
            "status": "pending",
            "origin_signal": sib_origin,
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p7])

        case_p7 = {
            "title": f"Apply: {TAG}-bbb divergence reflection pass",
            "description": (f"Second decomposition child, distinct deliverable: "
                            f"fires a {TAG}-bbb revision when a mismatch appears."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": sib_origin,
        }
        rp7 = _run_gate(case_p7, tmp_world)
        pq7 = _find_check(rp7, "pending_queue")
        if pq7 is None:
            failures.append("P7: pending_queue check missing from result")
        elif pq7.get("passed") is not True:
            failures.append(
                f"P7: distinct decomposition siblings sharing the parent "
                f"origin_signal should PASS (sibling exemption). "
                f"reason={pq7.get('reason')} matches={pq7.get('matches')}"
            )
        else:
            strategies = {m.get("match_strategy")
                          for m in pq7.get("matches") or []}
            if "origin_signal" in strategies:
                failures.append(
                    f"P7: origin_signal must NOT be a match strategy for "
                    f"decomposition siblings, got strategies={strategies}"
                )

        # ── P8: TRUE-duplicate decomposition siblings → BLOCK (Strategy 2) ─
        # The exemption only skips Strategy 1 (origin_signal). A genuine
        # duplicate child (same parent origin AND shared file path) must STILL
        # block via the structural overlap path — proving the exemption does
        # not open a real-dup hole (6 verification outcome 2).
        dup_origin = f"decomposition:g-{TAG}-parent2"
        dup_path = f"core/scripts/{TAG}-p8-shared.py"
        seed_p8 = _mk_aspiration("asp-pendq-p8", [{
            "id": "g-pendq-p8-existing",
            "title": f"Apply: refactor {dup_path} retry handling",
            "description": (f"First child reworks {dup_path} retry/backoff "
                            f"handling for the {TAG}-p8 path."),
            "status": "pending",
            "origin_signal": dup_origin,
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p8])

        case_p8 = {
            "title": f"Apply: fix retry handling in {dup_path}",
            "description": (f"Duplicate child touching {dup_path} retry/backoff "
                            f"for the {TAG}-p8 path."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": dup_origin,
        }
        rp8 = _run_gate(case_p8, tmp_world)
        pq8 = _find_check(rp8, "pending_queue")
        if pq8 is None:
            failures.append("P8: pending_queue check missing from result")
        elif pq8.get("passed") is not False:
            failures.append(
                f"P8: TRUE-duplicate decomposition siblings (shared file path) "
                f"must STILL block via Strategy 2. "
                f"reason={pq8.get('reason')} matches={pq8.get('matches')}"
            )
        else:
            strategies = {m.get("match_strategy")
                          for m in pq8.get("matches") or []}
            if "origin_signal" in strategies:
                failures.append(
                    f"P8: block must come from structural overlap, NOT "
                    f"origin_signal (exempt for decomposition), got {strategies}"
                )

    finally:
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (8/8 cases)")
    return 0


def test_pending_queue_gate():
    """Pytest entry point (5) — runs the 6-case suite (already
    tmp-world isolated) and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
