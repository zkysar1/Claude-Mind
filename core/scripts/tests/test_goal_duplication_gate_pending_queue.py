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
  P9 schema-token-only overlap            → PASS  (g-248-98: verification-
                                            schema vocab demoted to stopwords;
                                            no real content/[_0-9] co-signal
                                            remains — P7/P8 cover decomposition
                                            siblings)

Test isolation strategy: redirect MIND_WORLD to a tmp directory so the
real world/aspirations.jsonl is never touched, AND redirect
MIND_AGENTS_ROOT to tmp_world/agents so live agent queues are never
scanned either (g-115-2461 — before the override, tmp-world runs swept
PROJECT_ROOT/agents/*/aspirations.jsonl live, so test determinism depended
on real queue contents). The synthetic TAG remains as defense-in-depth.
P16 proves the isolation: the scan reads ONLY the override root.

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

# Unique synthetic tag — defense-in-depth against fixture collisions (agent
# queues are tmp-redirected via MIND_AGENTS_ROOT since 1; the tag
# predates that and stays as a second isolation layer).
TAG = "pendqtest12345"


def _now_iso(offset_hours: float = 0) -> str:
    return (datetime.now() +
            timedelta(hours=offset_hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_world(tmp_world: Path, aspirations_records: list,
                team_state: dict | None = None,
                findings_lines: list | None = None,
                fillers: bool = True):
    """Write minimal world fixtures into the tmp dir:
      - tmp_world/aspirations.jsonl with the supplied aspiration records
      - tmp_world/team-state.yaml with a minimal benign agent_status
      - tmp_world/board/findings.jsonl (empty unless overridden)

    fillers (g-115-2461): by default append 4 distinct-topic filler goals so
    the candidate corpus reaches n>=5 and the IDF floor stays positive.
    Before the MIND_AGENTS_ROOT hermeticity fix, LIVE agent queues silently
    provided this corpus (n~200+) — every structural-overlap case scored 0.0
    the moment the scan went hermetic, proving the coupling. P6 passes
    fillers=False to keep its empty-queue path genuinely empty.
    """
    asp_path = tmp_world / "aspirations.jsonl"
    asp_path.parent.mkdir(parents=True, exist_ok=True)
    filler_recs = []
    if fillers:
        filler_goals = [
            ("g-pendq-fill-1", "Database migration for user preferences table",
             "Alter the preferences schema and backfill rows."),
            ("g-pendq-fill-2", "Refactor authentication middleware session handling",
             "Rework the session middleware token lifecycle."),
            ("g-pendq-fill-3", "Optimize rendering pipeline shader compilation",
             "Cache compiled shaders across render passes."),
            ("g-pendq-fill-4", "Investigate memory leak in websocket connection pool",
             "Trace the leaking connection pool allocations."),
        ]
        filler_recs = [_mk_aspiration("asp-pendq-fill", [
            {"id": gid, "title": t, "description": d, "status": "pending",
             "origin_signal": f"idea:pendq-fill-{i}", "participants": ["agent"]}
            for i, (gid, t, d) in enumerate(filler_goals, 1)
        ])]
    with open(asp_path, "w", encoding="utf-8") as f:
        for rec in list(aspirations_records) + filler_recs:
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
    # Hermetic agent-queue scan (1): point the gate's per-agent
    # enumeration at a test-controlled root instead of live agents/.
    env["MIND_AGENTS_ROOT"] = str(tmp_world / "agents")
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
        # fillers=False: this case tests the genuinely-empty-queue path.
        _seed_world(tmp_world, [], fillers=False)

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

        # ── P9: schema-token-only overlap → PASS () ──────────────
        # Two UNRELATED goals whose only shared vocabulary is verification-
        # SCHEMA tokens (command_check / condition / python3 / scripts /
        # block / file_check). Before , command_check (underscore),
        # python3 (digit) and file_check (underscore) satisfied the
        # has_specific [_0-9] structured-identifier co-signal — turning a
        # schema-vocab-ONLY overlap into a HARD block. The candidate corpus
        # is title+description PROSE, where these schema tokens are rare
        # (df=1) → high IDF → they LOOK like rare identifiers and clear the
        # idf_floor. Four distinct filler goals seed n=5 so idf_floor =
        # log(5/2) > 0 and the df=1 schema tokens qualify as co-signals in
        # the UN-fixed gate (regression proof). After the  stoplist
        # expansion these tokens are demoted out of `keywords` entirely, so
        # the proposed goal shares nothing with the pending goal → PASS.
        # Evidence: echo s101 2026-07-10 — FOUR unrelated adds blocked in
        # one day ( ×3 ARC-leaderboard recurring,  ×1
        # constitutional-tripwire recurring), keyword_hits {check, command,
        # command_check, condition, python3, scripts, block}, file_path_hits
        # empty every time. The 4th block fired on the very Idea goal
        # describing this bug.
        schema_phrase = ("command_check condition python3 scripts block "
                         "file_check")
        seed_p9 = _mk_aspiration("asp-pendq-p9", [
            {
                "id": "g-pendq-p9-match",
                "title": "Recurring: verify ARC leaderboard submission cadence",
                "description": (
                    "Existing pending recurring goal about the ARC "
                    "leaderboard submission cadence. Verified via a "
                    + schema_phrase + " sequence. Distinct topic: "
                    "leaderboard submission cadence tracking."),
                "status": "pending",
                "origin_signal": f"idea:{TAG}-p9-existing-leaderboard",
                "participants": ["agent"],
            },
            # Fillers: inflate the candidate corpus to n=5 (so idf_floor>0)
            # while keeping the schema tokens at df=1 (they appear only in
            # g-pendq-p9-match's prose). Each is a distinct topic that shares
            # NOTHING with the proposed goal.
            {"id": "g-pendq-p9-f1", "status": "pending",
             "title": "Database migration for user preferences table",
             "description": "Alter the preferences schema and backfill rows.",
             "origin_signal": f"idea:{TAG}-p9-f1", "participants": ["agent"]},
            {"id": "g-pendq-p9-f2", "status": "pending",
             "title": "Refactor authentication middleware session handling",
             "description": "Rework the session middleware token lifecycle.",
             "origin_signal": f"idea:{TAG}-p9-f2", "participants": ["agent"]},
            {"id": "g-pendq-p9-f3", "status": "pending",
             "title": "Optimize rendering pipeline shader compilation",
             "description": "Cache compiled shaders across render passes.",
             "origin_signal": f"idea:{TAG}-p9-f3", "participants": ["agent"]},
            {"id": "g-pendq-p9-f4", "status": "pending",
             "title": "Investigate memory leak in websocket connection pool",
             "description": "Trace the leaking connection pool allocations.",
             "origin_signal": f"idea:{TAG}-p9-f4", "participants": ["agent"]},
        ])
        _seed_world(tmp_world, [seed_p9])

        case_p9 = {
            "title": "Recurring: verify constitutional tripwire integrity",
            "description": ("Proposed recurring goal about constitutional "
                            "tripwire integrity monitoring — a completely "
                            "different subsystem from ARC leaderboards."),
            # Schema vocab enters via the verification block (the authoritative
            # bug path: _extract_signals prefers verification.outcomes+checks).
            "verification": {
                "outcomes": ["tripwire integrity monitoring is confirmed"],
                "checks": [
                    {"type": "command_check",
                     "condition": "python3 scripts block file_check runs"},
                ],
            },
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p9-proposed-tripwire",
        }
        rp9 = _run_gate(case_p9, tmp_world)
        pq9 = _find_check(rp9, "pending_queue")
        if pq9 is None:
            failures.append("P9: pending_queue check missing from result")
        elif pq9.get("passed") is not True:
            failures.append(
                "P9: schema-token-only overlap must PASS after g-248-98 "
                "(command_check/python3/condition/scripts/block/file_check "
                "demoted to stopwords — no real content or [_0-9] identifier "
                f"co-signal remains). reason={pq9.get('reason')} "
                f"matches={pq9.get('matches')}"
            )
        elif rp9.get("would_block") is True:
            failures.append(
                "P9: gate must not block a schema-token-only overlap "
                f"(would_block=True). failing={rp9.get('reason')}"
            )

        # ── P10: discovered_by parent → DEMOTE (lineage advisory) ─────────
        # 6: a follow-up goal filed FROM a discovery goal cites its
        # parent's file path + vocabulary BY DESIGN. Without the lineage
        # exemption this shape hard-blocks via Strategy 2 (observed: echo
        #  blocked against its own discoverer ). The same
        # overlap WITHOUT the lineage link still blocks (P8 proves that).
        p10_path = f"core/scripts/{TAG}-p10-shared.py"
        seed_p10 = _mk_aspiration("asp-pendq-p10", [{
            "id": "g-pendq-p10-parent",
            "title": f"Investigate: {TAG}-p10 anomaly in {p10_path}",
            "description": (f"Discovery goal probing {p10_path} retry "
                            f"handling for the {TAG}-p10 anomaly."),
            "status": "pending",
            "origin_signal": f"investigate:{TAG}-p10-parent",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p10])

        case_p10 = {
            "title": f"Apply: fix retry handling in {p10_path}",
            "description": (f"Follow-up from the discovery goal: apply the "
                            f"{TAG}-p10 retry fix in {p10_path}."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"apply:{TAG}-p10-child-distinct",
            "discovered_by": "g-pendq-p10-parent",
        }
        rp10 = _run_gate(case_p10, tmp_world)
        pq10 = _find_check(rp10, "pending_queue")
        if pq10 is None:
            failures.append("P10: pending_queue check missing from result")
        elif pq10.get("passed") is not True:
            failures.append(
                f"P10: discovered_by-parent overlap must DEMOTE to advisory, "
                f"not block. reason={pq10.get('reason')} "
                f"matches={pq10.get('matches')}"
            )
        else:
            lineage = [a for a in (pq10.get("advisories") or [])
                       if a.get("lineage_exempt") == "discovered_by-parent"
                       and a.get("goal_id") == "g-pendq-p10-parent"]
            if not lineage:
                failures.append(
                    f"P10: expected lineage_exempt=discovered_by-parent "
                    f"advisory for g-pendq-p10-parent, got "
                    f"advisories={pq10.get('advisories')}"
                )

        # ── P11: precondition dependency → DEMOTE (lineage advisory) ──────
        # A goal whose verification.preconditions names a pending goal
        # NECESSARILY describes the same artifact (observed: echo 
        # blocked against its declared prerequisite ).
        p11_path = f"core/scripts/{TAG}-p11-shared.py"
        seed_p11 = _mk_aspiration("asp-pendq-p11", [{
            "id": "g-pendq-p11-dep",
            "title": f"Apply: build {TAG}-p11 exporter in {p11_path}",
            "description": (f"Prerequisite goal building the {TAG}-p11 "
                            f"exporter in {p11_path}."),
            "status": "pending",
            "origin_signal": f"apply:{TAG}-p11-dep",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p11])

        case_p11 = {
            "title": f"Apply: wire {TAG}-p11 exporter output from {p11_path}",
            "description": (f"Downstream goal consuming the {TAG}-p11 "
                            f"exporter built in {p11_path}."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"apply:{TAG}-p11-consumer-distinct",
            # NOTE: _extract_signals PREFERS verification.outcomes+checks
            # over title/description when a verification block exists — the
            # shared vocabulary must live HERE or the overlap never reaches
            # the strong+specific blocking branch this case exercises.
            "verification": {
                "outcomes": [f"{TAG}-p11 exporter output from {p11_path} "
                             f"is wired downstream"],
                "preconditions": [
                    {"goal_id": "g-pendq-p11-dep",
                     "goal_completed_after": "2026-01-01T00:00:00"},
                ],
            },
        }
        rp11 = _run_gate(case_p11, tmp_world)
        pq11 = _find_check(rp11, "pending_queue")
        if pq11 is None:
            failures.append("P11: pending_queue check missing from result")
        elif pq11.get("passed") is not True:
            failures.append(
                f"P11: precondition-dependency overlap must DEMOTE to "
                f"advisory, not block. reason={pq11.get('reason')} "
                f"matches={pq11.get('matches')}"
            )
        else:
            lineage = [a for a in (pq11.get("advisories") or [])
                       if a.get("lineage_exempt") == "precondition-dependency"
                       and a.get("goal_id") == "g-pendq-p11-dep"]
            if not lineage:
                failures.append(
                    f"P11: expected lineage_exempt=precondition-dependency "
                    f"advisory for g-pendq-p11-dep, got "
                    f"advisories={pq11.get('advisories')}"
                )

        # ── P12: same-discoverer siblings → DEMOTE (lineage advisory) ─────
        # Two children filed by ONE discovery pass share its vocabulary but
        # carry DISTINCT origin_signals (observed: echo 2 blocked
        # against same-discoverer lane siblings). Contrast P8: same shape
        # WITHOUT the shared discovered_by still blocks.
        p12_path = f"core/scripts/{TAG}-p12-shared.py"
        seed_p12 = _mk_aspiration("asp-pendq-p12", [{
            "id": "g-pendq-p12-sib",
            "title": f"Apply: harden {TAG}-p12 parser in {p12_path}",
            "description": (f"First child of the discovery pass: harden "
                            f"the {TAG}-p12 parser in {p12_path}."),
            "status": "pending",
            "origin_signal": f"apply:{TAG}-p12-sibling-a",
            "participants": ["agent"],
            # Real goal-id grammar (g-NNN-NN) — the sibling exemption is
            # guarded to goal-id-SHAPED discoverers, so a letters-only
            # fixture label here would (correctly) not qualify.
            "discovered_by": "g-888-77",
        }])
        _seed_world(tmp_world, [seed_p12])

        case_p12 = {
            "title": f"Apply: extend {TAG}-p12 parser tests for {p12_path}",
            "description": (f"Second child of the same discovery pass: "
                            f"extend {TAG}-p12 parser tests in {p12_path}."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"apply:{TAG}-p12-sibling-b",
            "discovered_by": "g-888-77",
        }
        rp12 = _run_gate(case_p12, tmp_world)
        pq12 = _find_check(rp12, "pending_queue")
        if pq12 is None:
            failures.append("P12: pending_queue check missing from result")
        elif pq12.get("passed") is not True:
            failures.append(
                f"P12: same-discoverer siblings with distinct origin_signals "
                f"must DEMOTE to advisory, not block. "
                f"reason={pq12.get('reason')} matches={pq12.get('matches')}"
            )
        else:
            lineage = [a for a in (pq12.get("advisories") or [])
                       if a.get("lineage_exempt") == "same-discoverer-sibling"
                       and a.get("goal_id") == "g-pendq-p12-sib"]
            if not lineage:
                failures.append(
                    f"P12: expected lineage_exempt=same-discoverer-sibling "
                    f"advisory for g-pendq-p12-sib, got "
                    f"advisories={pq12.get('advisories')}"
                )

        # ── P13: origin_signal names the candidate → DEMOTE (lineage) ─────
        # A follow-up whose origin_signal EMBEDS the source goal's id (the
        # "idea:g-NNN-NN-<slug>" filing convention) necessarily overlaps that
        # source goal (observed: zeta  blocked against , the
        # in-progress goal named in its origin_signal).
        p13_path = f"core/scripts/{TAG}-p13-shared.py"
        seed_p13 = _mk_aspiration("asp-pendq-p13", [{
            "id": "g-pendq-p13-src",
            "title": f"Investigate: {TAG}-p13 identity gap in {p13_path}",
            "description": (f"In-progress source goal probing the "
                            f"{TAG}-p13 identity gap in {p13_path}."),
            "status": "in-progress",
            "origin_signal": f"investigate:{TAG}-p13-src",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p13])

        case_p13 = {
            "title": f"Idea: align {TAG}-p13 identity handling in {p13_path}",
            "description": (f"Filed from the in-progress source goal: align "
                            f"{TAG}-p13 identity handling in {p13_path}."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "idea:g-pendq-p13-src-identity-alignment",
        }
        rp13 = _run_gate(case_p13, tmp_world)
        pq13 = _find_check(rp13, "pending_queue")
        if pq13 is None:
            failures.append("P13: pending_queue check missing from result")
        elif pq13.get("passed") is not True:
            failures.append(
                f"P13: origin-signal-lineage overlap must DEMOTE to "
                f"advisory, not block. reason={pq13.get('reason')} "
                f"matches={pq13.get('matches')}"
            )
        else:
            lineage = [a for a in (pq13.get("advisories") or [])
                       if a.get("lineage_exempt") == "origin-signal-lineage"
                       and a.get("goal_id") == "g-pendq-p13-src"]
            if not lineage:
                failures.append(
                    f"P13: expected lineage_exempt=origin-signal-lineage "
                    f"advisory for g-pendq-p13-src, got "
                    f"advisories={pq13.get('advisories')}"
                )

        # ── P14: goal-id PREFIX collision → still BLOCK (no exemption) ─────
        # Goal ids have 2-4 digit suffixes, so bare substring matching would
        # let candidate g-...-1 claim lineage inside an origin_signal naming
        # g-...-10 (live corpus:  inside "idea:1-slug").
        # The boundary rule (id not followed by a digit) must keep this a
        # hard block — exempting it would let a true duplicate through.
        p14_path = f"core/scripts/{TAG}-p14-shared.py"
        seed_p14 = _mk_aspiration("asp-pendq-p14", [{
            "id": "g-pendq-p14-1",
            "title": f"Apply: refactor {p14_path} retry handling",
            "description": (f"Existing goal reworking {p14_path} retry "
                            f"handling for the {TAG}-p14 path."),
            "status": "pending",
            "origin_signal": f"apply:{TAG}-p14-existing",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p14])

        case_p14 = {
            "title": f"Apply: fix retry handling in {p14_path}",
            "description": (f"Duplicate touching {p14_path} retry handling "
                            f"for the {TAG}-p14 path."),
            "participants": ["agent"],
            "source": "world",
            # Embeds g-pendq-p14-10 — candidate g-pendq-p14-1 is a PREFIX of
            # that id, NOT its lineage.
            "origin_signal": "idea:g-pendq-p14-10-follow-up",
        }
        rp14 = _run_gate(case_p14, tmp_world)
        pq14 = _find_check(rp14, "pending_queue")
        if pq14 is None:
            failures.append("P14: pending_queue check missing from result")
        elif pq14.get("passed") is not False:
            failures.append(
                f"P14: prefix-colliding origin_signal (g-pendq-p14-10 vs "
                f"candidate g-pendq-p14-1) must NOT be lineage-exempt — "
                f"expected BLOCK. reason={pq14.get('reason')} "
                f"advisories={pq14.get('advisories')}"
            )

        # ── P15: free-form shared discovered_by → still BLOCK ─────────────
        # The sibling exemption is guarded to goal-id-shaped discoverers.
        # Two unrelated filings sharing a producer LABEL (e.g. "user") with
        # distinct origin_signals must not exempt each other.
        p15_path = f"core/scripts/{TAG}-p15-shared.py"
        seed_p15 = _mk_aspiration("asp-pendq-p15", [{
            "id": "g-pendq-p15-existing",
            "title": f"Apply: harden {TAG}-p15 parser in {p15_path}",
            "description": (f"Existing goal hardening the {TAG}-p15 parser "
                            f"in {p15_path}."),
            "status": "pending",
            "origin_signal": f"apply:{TAG}-p15-a",
            "participants": ["agent"],
            "discovered_by": "user",
        }])
        _seed_world(tmp_world, [seed_p15])

        case_p15 = {
            "title": f"Apply: harden {TAG}-p15 parser checks in {p15_path}",
            "description": (f"Duplicate hardening of the {TAG}-p15 parser "
                            f"in {p15_path}."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"apply:{TAG}-p15-b",
            "discovered_by": "user",
        }
        rp15 = _run_gate(case_p15, tmp_world)
        pq15 = _find_check(rp15, "pending_queue")
        if pq15 is None:
            failures.append("P15: pending_queue check missing from result")
        elif pq15.get("passed") is not False:
            failures.append(
                f"P15: free-form shared discovered_by ('user') must NOT "
                f"grant the sibling exemption — expected BLOCK. "
                f"reason={pq15.get('reason')} "
                f"advisories={pq15.get('advisories')}"
            )

        # ── P16: agent-queue hermeticity — scan follows MIND_AGENTS_ROOT ──
        # Two real-shaped agent roots: the override target (tmp_world/agents,
        # what _run_gate points at) and a decoy root the gate is NOT pointed
        # at. The visible goal must surface as an agent-source candidate; the
        # decoy goal must be absent from the entire check result — proving
        # the scan reads ONLY the override root (1).
        p16_path = f"core/scripts/{TAG}-p16-shared.py"
        for root_name, gid in (("agents", "g-pendq-p16-visible"),
                               ("agents-decoy", "g-pendq-p16-decoy")):
            adir = tmp_world / root_name / "testagent"
            adir.mkdir(parents=True, exist_ok=True)
            rec = _mk_aspiration(f"asp-pendq-p16-{root_name}", [{
                "id": gid,
                "title": f"Apply: rework {p16_path} retry handling",
                "description": (f"Agent-queue goal touching {p16_path} "
                                f"for the {TAG}-p16 path."),
                "status": "pending",
                "origin_signal": f"apply:{TAG}-p16-{gid}",
                "participants": ["agent"],
            }])
            with open(adir / "aspirations.jsonl", "w", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _seed_world(tmp_world, [])  # empty world queue — agent scan only

        case_p16 = {
            "title": f"Apply: fix retry handling in {p16_path}",
            "description": (f"Duplicate touching {p16_path} retry handling "
                            f"for the {TAG}-p16 path."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"apply:{TAG}-p16-proposed-distinct",
        }
        rp16 = _run_gate(case_p16, tmp_world)
        pq16 = _find_check(rp16, "pending_queue")
        blob16 = json.dumps(pq16 or {})
        if pq16 is None:
            failures.append("P16: pending_queue check missing from result")
        elif pq16.get("passed") is not False:
            failures.append(
                f"P16: goal seeded in the OVERRIDE agents root must block "
                f"via agent-source structural overlap. "
                f"reason={pq16.get('reason')} matches={pq16.get('matches')}"
            )
        elif "agent:testagent" not in blob16 or "g-pendq-p16-visible" not in blob16:
            failures.append(
                f"P16: expected agent:testagent / g-pendq-p16-visible in "
                f"check result, got {blob16[:300]}"
            )
        if pq16 is not None and "g-pendq-p16-decoy" in blob16:
            failures.append(
                "P16: decoy goal from a NON-pointed agents root leaked into "
                "the scan — hermeticity broken"
            )

        # ── P17-P20: completed-Maintain carve-out (7) ───────────
        # P17: status=completed Maintain proposal with STRUCTURAL overlap
        # (file-path + identifier keywords) vs a pending goal, AND a partner
        # in_flight sharing generic keywords → BOTH pending_queue and
        # partner_in_flight must PASS (carve-out: completed records cannot
        # race live work; only exact-duplicate records block).
        seed_cm = _mk_aspiration("asp-pendq-cm", [{
            "id": "g-pendq-cm-pending",
            "title": f"Idea: harden {TAG}-widget-sync.py retry ladder",
            "description": (f"Pending work touching core/scripts/{TAG}-widget-"
                            f"sync.py and the g-115-9999 retry ladder "
                            f"metrics_probe_77 lane."),
            "status": "pending",
            "origin_signal": f"idea:{TAG}-widget-sync-hardening",
            "participants": ["agent"],
        }])
        ts_partner = {
            "strategic_focus": {
                "primary": None, "rationale": None,
                "set_by": None, "set_at": None, "acknowledged_by": [],
            },
            "active_blockers": [],
            "recent_completions": [],
            "agent_status": {
                "zeta": {
                    "last_active": _now_iso(0),
                    "current_focus": "",
                    "session_goals_completed": 0,
                    "live_phase": "phase-4",
                    "in_flight": {
                        "goal_id": "g-pendq-cm-partner",
                        "title": (f"Apply: {TAG}-widget-sync.py retry ladder "
                                  f"overhaul metrics_probe_77"),
                        "phase": 4,
                        "claimed_at": _now_iso(-0.2),
                    },
                },
            },
            "critical_blockers": [],
        }
        _seed_world(tmp_world, [seed_cm], team_state=ts_partner)
        case_p17 = {
            "title": "Maintain: widget-sync retry ladder doc corrected inline",
            "description": (f"Inline correction already shipped to "
                            f"core/scripts/{TAG}-widget-sync.py — retry "
                            f"ladder g-115-9999 metrics_probe_77 evidence."),
            "participants": ["agent"],
            "source": "world",
            "status": "completed",
            "origin_signal": f"maintain:{TAG}-widget-sync-doc-fix",
        }
        rp17 = _run_gate(case_p17, tmp_world)
        pq17 = _find_check(rp17, "pending_queue")
        if pq17 is None:
            failures.append("P17: pending_queue check missing from result")
        elif pq17.get("passed") is not True:
            failures.append(
                f"P17: completed-Maintain with structural-only overlap must "
                f"PASS (g-115-2477 carve-out). reason={pq17.get('reason')} "
                f"matches={pq17.get('matches')}")
        elif "g-115-2477" not in (pq17.get("reason") or ""):
            failures.append(
                f"P17: pass reason should cite the g-115-2477 carve-out. "
                f"reason={pq17.get('reason')}")
        pif17 = _find_check(rp17, "partner_in_flight")
        if pif17 is None:
            failures.append("P17b: partner_in_flight check missing")
        elif pif17.get("passed") is not True:
            failures.append(
                f"P17b: completed-Maintain must skip partner_in_flight even "
                f"with keyword overlap vs live partner work. "
                f"reason={pif17.get('reason')}")
        elif "g-115-2477" not in (pif17.get("reason") or ""):
            failures.append(
                f"P17b: skip reason should cite g-115-2477. "
                f"reason={pif17.get('reason')}")

        # P18: SAME structural overlap but status=pending → still BLOCKS
        # (the carve-out is scoped to completed-Maintain only).
        case_p18 = dict(case_p17)
        case_p18["title"] = "Idea: widget-sync retry ladder follow-up"
        case_p18["status"] = "pending"
        case_p18["origin_signal"] = f"idea:{TAG}-widget-sync-followup"
        rp18 = _run_gate(case_p18, tmp_world)
        pq18 = _find_check(rp18, "pending_queue")
        if pq18 is None:
            failures.append("P18: pending_queue check missing from result")
        elif pq18.get("passed") is not False:
            failures.append(
                f"P18: pending-status goal with the same structural overlap "
                f"must still BLOCK. reason={pq18.get('reason')}")

        # P19: completed-Maintain with EXACT origin_signal duplicate →
        # still BLOCKS (exact-duplicate record detection retained).
        seed_cm2 = _mk_aspiration("asp-pendq-cm2", [{
            "id": "g-pendq-cm2-pending",
            "title": "Maintain: unrelated placeholder title here",
            "description": "Different prose entirely.",
            "status": "pending",
            "origin_signal": f"maintain:{TAG}-exact-dup-key",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_cm2])
        case_p19 = {
            "title": "Maintain: some other correction record",
            "description": "Distinct description body with no shared tokens.",
            "participants": ["agent"],
            "source": "world",
            "status": "completed",
            "origin_signal": f"maintain:{TAG}-exact-dup-key",
        }
        rp19 = _run_gate(case_p19, tmp_world)
        pq19 = _find_check(rp19, "pending_queue")
        if pq19 is None:
            failures.append("P19: pending_queue check missing from result")
        elif pq19.get("passed") is not False:
            failures.append(
                f"P19: exact origin_signal duplicate must still BLOCK for "
                f"completed-Maintain. reason={pq19.get('reason')}")

        # P20: completed-Maintain with EXACT normalized-title duplicate
        # (different origin_signal) → BLOCKS via title_exact strategy.
        case_p20 = {
            "title": "Maintain:  unrelated   placeholder title here",
            "description": "No token overlap with the candidate description.",
            "participants": ["agent"],
            "source": "world",
            "status": "completed",
            "origin_signal": f"maintain:{TAG}-different-key-entirely",
        }
        rp20 = _run_gate(case_p20, tmp_world)
        pq20 = _find_check(rp20, "pending_queue")
        blob20 = json.dumps(pq20 or {})
        if pq20 is None:
            failures.append("P20: pending_queue check missing from result")
        elif pq20.get("passed") is not False:
            failures.append(
                f"P20: exact-normalized-title duplicate must BLOCK for "
                f"completed-Maintain. reason={pq20.get('reason')}")
        elif "title_exact" not in blob20:
            failures.append(
                f"P20: expected title_exact match strategy, got {blob20[:250]}")

        # ── P21: bare-filename-only overlap → DEMOTE (advisory) ───────────
        # Regression guard for 3 (mirrors the git_log 6
        # bare-basename floor). A PROSE proposal whose ONLY file-path hit is a
        # bare filename with no directory component (retrieve.sh, SKILL.md,
        # README.md, CLAUDE.md) shares VOCABULARY, not a work-target path —
        # dozens of topically-unrelated goals mention it. Before the fix this
        # bare hit counted as the has_specific co-signal and HARD-blocked
        # (canonical: 3's own Investigate filing false-blocked on
        # "retrieve.sh"). Post-fix only a directory-QUALIFIED path (contains
        # "/") is specific enough to block; a bare-filename-only overlap
        # demotes to a visible strong_keyword_only advisory. Distinct from P3
        # (plain-words, ZERO file paths): P21 HAS a file-path hit that must NOT
        # upgrade to a hard block. An _extract_signals probe confirms
        # "retrieve.sh" survives extraction into file_paths with no "/".
        seed_p21 = _mk_aspiration("asp-pendq-p21", [{
            "id": "g-pendq-p21-existing",
            "title": "Refactor retrieve.sh reranking cadence telemetry dashboard",
            "description": ("Existing pending goal about retrieve.sh reranking "
                            "cadence in the telemetry dashboard throughput "
                            "stack — bare filename plus plain-words overlap, no "
                            "directory-qualified path."),
            "status": "pending",
            "origin_signal": f"idea:{TAG}-p21-existing-distinct",
            "participants": ["agent"],
        }])
        _seed_world(tmp_world, [seed_p21])

        case_p21 = {
            "title": "Optimize retrieve.sh reranking cadence in telemetry dashboard",
            "description": ("Tune retrieve.sh reranking cadence concurrency in "
                            "the telemetry dashboard throughput stack for the "
                            "pipeline."),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": f"idea:{TAG}-p21-proposed-distinct",
        }
        rp21 = _run_gate(case_p21, tmp_world)
        pq21 = _find_check(rp21, "pending_queue")
        if pq21 is None:
            failures.append("P21: pending_queue check missing from result")
        elif pq21.get("passed") is not True:
            failures.append(
                f"P21: bare-filename-only overlap must DEMOTE to advisory, not "
                f"HARD block (g-115-2563). reason={pq21.get('reason')} "
                f"matches={pq21.get('matches')}"
            )
        else:
            advisories = pq21.get("advisories") or []
            strong_only = [a for a in advisories
                           if a.get("strong_keyword_only")]
            if not strong_only:
                failures.append(
                    f"P21: expected strong_keyword_only advisory (bare-filename "
                    f"overlap demoted, no directory-qualified co-signal), got "
                    f"advisories={advisories}"
                )

    finally:
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (21/21 cases)")
    return 0


def test_pending_queue_gate():
    """Pytest entry point (5) — runs the full pending-queue case
    suite (already tmp-world isolated) and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
