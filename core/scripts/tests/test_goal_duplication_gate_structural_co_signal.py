"""test_goal_duplication_gate_structural_co_signal.py — regression test for
the goal-duplication-gate structural-co-signal invariant (commits 9db5384 +
6e1563e, 2026-05-16).

Verifies that `_check_recent_completions` requires a STRUCTURAL co-signal
for any hard block:

    has_specific = bool(hit_paths) or any(
        re.search(r"[-_0-9]", k) for k in hit_kws)

A purely-plain-words strong overlap (e.g. "cache" + "streaming" both 1.0 idf,
weighted >= 1.5) is demoted to an advisory; it never hard-blocks. A
file-path hit OR a hit keyword carrying a hyphen/underscore/digit
(structured identifier — rb-335) is required for the block to fire.

Cases covered:
  G1 plain-words overlap → DEMOTE (rc.passed=True, strong_keyword_only
     advisory present)
  G2 file-path overlap → BLOCK (rc.passed=False, matches non-empty)
  G3 structured-identifier hit_kw overlap → BLOCK (rc.passed=False,
     matches non-empty)
  G4 generic-token inflation FP (g-115-1415) → DEMOTE: one structural topic
     token (loop_state) + generic vocab (global/exists/populated/recurring/
     class/close) must NOT block once the generics are stopwords (N drops to 1)
  G5 over-suppression guard → BLOCK: two structural identifiers
     (loop_state + iteration-checkpoint) still block after generic stopwording
  G6 directive-routing goal (origin_signal=user_directive) → EXEMPT
     (rc.passed=True, reason names the exemption; g-115-1674)
  G7 cross-agent handoff goal (handoff_to set) → EXEMPT (same skip; g-115-23)
  G8 generic-VERB inflation FP (g-115-1726): generic English verbs
     (cause/confirmed/every/hardening) + the hyphenated plain word re-run must
     DEMOTE (not block) once they are stopwords. Session-93 ground truth:
     g-115-1725 vs g-001-10, g-115-1727 vs g-001-33 (5 override entries).
     re-run was the has_specific hyphen co-signal that turned a zero-file-path
     generic-verb overlap into a hard block.
  G9 over-suppression guard for the G8 verb stopwords: a genuine 2-identifier
     dup (loop_state + g-115-999) carrying cause/confirmed still BLOCKS,
     proving the verb stopwording did not suppress real duplicate detection.
  G10 exclusion-context file-path FP (g-115-2207) → DEMOTE: a path named ONLY
     in an "excluding retrieve.sh" clause is not aboutness, so it must NOT be a
     structural co-signal (canonical incident g-115-2206: g-115-760's
     "feature-path-excluded for retrieve.sh" false-blocked a Maintain goal).
  G11 recall / over-suppression guard for g-115-2207: the SAME overlap with
     retrieve.sh named POSITIVELY (sole co-signal) still BLOCKS — the
     exclusion-context disqualifier must not suppress genuine file-path dups
     (guard-958 adversarial genuine-positive control).
  G12 recurring keyword-vacuum completion (g-248-114) → DEMOTE: a recurring
     completion matched on hyphenated-compound keywords only (env-server /
     end-to-end, [-_0-9] structured) with ZERO file-path hits is a keyword
     vacuum — demoted to a recurring_vacuum_exempt advisory, never a block
     (false-block class: g-335-103/104/105 vs the g-115-23 recurring sweep).
  G13 NON-recurring keyword-vacuum (g-248-114 control) → BLOCK: the identical
     vacuum shape with a non-recurring completion still hard-blocks, proving
     the exemption is scoped to recurring completions.
  G14 recurring completion + REAL file-path hit (g-248-114 control) → BLOCK:
     a recurring completion sharing a real file path (hit_paths non-empty) is
     NOT a vacuum → still hard-blocks, proving the exemption requires empty
     hit_paths.

Test isolation strategy (g-115-1375, 2026-06-09): redirect MIND_WORLD to a
tmp directory and seed team-state.yaml THERE, so the live shared
world/team-state.yaml is NEVER read or written. This removes the
seed-clobber race (rb-1547): previously this test backed up / seeded /
restored the LIVE team-state.yaml, which flaked when a partner agent wrote
the file concurrently in the window between _seed_state and the gate
subprocess read, AND the restore step risked clobbering that partner write.
The gate reads no META and resolves team-state from world_dir (← MIND_WORLD;
verified gate reads no meta env), so MIND_WORLD-only isolation is sufficient
(mirrors the gold-standard test_goal_duplication_gate_pending_queue.py).

Pytest-collectable via the thin `test_*` wrapper at the bottom; also runnable
standalone via `py -3 core/scripts/tests/test_goal_duplication_gate_structural_co_signal.py`.
Companion to test_goal_duplication_gate_partner_in_flight.py. Filed by
g-115-838 (verify-learning Section CO smoke check; sq-018 lens, 2026-05-16
bravo encode-session Lane 5).
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


_FILLERS = [
    "Updated documentation index sections and renumbered nested anchors.",
    "Adjusted thread pool sizing within the orchestrator container layer.",
    "Reviewed boundary diagrams across the deployment service registry.",
    "Pruned outdated entries from the historical reference catalog index.",
    "Tightened logging verbosity flags around the watcher polling loop.",
    "Renamed stale references inside the inventory manifest collection.",
    "Reorganized chapter outlines under the curriculum reference set.",
]


def _seed_state(tmp_world: Path, target_entry):
    """Write tmp_world/team-state.yaml with one TARGET recent_completion entry
    plus filler entries that share no vocabulary with the target. Filler raises
    IDF for the target's terms (g-248-12 rare-identifier path). Also writes an
    empty tmp_world/board/findings.jsonl (insight_triggers clean) and an empty
    tmp_world/aspirations.jsonl (pending_queue scan finds nothing). NEVER
    touches the live world (rb-1547 seed-clobber fix).

    target_entry: dict with goal_id, completed_by, completed_at, key_finding.
    """
    recent_completions = [target_entry]
    for i, kf in enumerate(_FILLERS):
        recent_completions.append({
            "goal_id": f"g-scs-noise-{i:02d}",
            "completed_by": "bravo",
            "completed_at": _now_iso(-6 - i),
            "key_finding": kf,
        })
    agent_status = {
        "alpha": {
            "last_active": _now_iso(0),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": None,
        },
        "bravo": {
            "last_active": _now_iso(-0.1),
            "current_focus": "",
            "session_goals_completed": 0,
            "live_phase": "between-phases",
            "in_flight": None,
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

    # Empty findings.jsonl so insight_triggers stays clean.
    findings_path = tmp_world / "board" / "findings.jsonl"
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(findings_path, "w", encoding="utf-8") as f:
        f.write("")

    # Empty aspirations.jsonl so the pending_queue check has a file to scan
    # (finds nothing — these cases assert only on recent_completions).
    with open(tmp_world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write("")


def _seed_recurring(tmp_world: Path, recurring_goal_ids):
    """Overwrite tmp_world/aspirations.jsonl (which _seed_state leaves empty)
    with one aspiration whose goals carry recurring:true for each id in
    recurring_goal_ids. The g-248-114 exemption keys off a COMPLETION whose
    goal_id is recurring — the gate's _recurring_goal_ids(world_dir) reads this
    file to build the recurring set. Call AFTER _seed_state (which resets
    aspirations.jsonl to empty).
    """
    asp = {
        "id": "asp-scs-recur",
        "title": "SCS recurring-vacuum fixture",
        "status": "active",
        "goals": [
            {"id": gid, "title": f"Recurring sweep {gid}",
             "status": "completed", "recurring": True}
            for gid in recurring_goal_ids
        ],
    }
    with open(tmp_world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp) + "\n")


def _run_gate(goal: dict, tmp_world: Path, agent: str = "alpha") -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
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


def _find_check(result, name):
    for c in result.get("checks", []):
        if c.get("name") == name:
            return c
    return None


def main() -> int:
    failures = []

    tmp_world = Path(tempfile.mkdtemp(prefix="scs-test-"))

    try:
        # ── G1: plain-words overlap → DEMOTE (no hard block) ─────────────
        # Recent completion's key_finding shares plain English words with
        # the proposed goal. No file paths, no -_0-9 in shared keywords.
        # Expected: strong=True, has_specific=False -> advisory only.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G1",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Refactored cache and streaming buffers in monitoring "
                "throughout the metrics stack."
            ),
        })
        case_g1 = {
            "title": "Optimize cache and streaming behavior",
            "description": (
                "Tune the cache buffers and streaming concurrency "
                "settings in the metrics pipeline."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg1 = _run_gate(case_g1, tmp_world)
        rc1 = _find_check(rg1, "recent_completions")
        if rc1 is None:
            failures.append("G1: recent_completions check missing from result")
        elif rc1.get("passed") is not True:
            failures.append(
                f"G1: recent_completions should have passed (plain-words demote). "
                f"reason={rc1.get('reason')} matches={rc1.get('matches')}"
            )
        else:
            advisories = rc1.get("advisories") or []
            strong_only = [a for a in advisories if a.get("strong_keyword_only")]
            if not strong_only:
                failures.append(
                    "G1: expected strong_keyword_only advisory (demoted overlap), "
                    f"got advisories={advisories}"
                )

        # ── G2: file-path overlap → BLOCK ────────────────────────────────
        # Proposed goal mentions a file the recent completion also touched
        # (key_finding contains the file path). has_specific = bool(hit_paths)
        # = True -> hard block.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G2",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Fixed cache concurrency bug in core/scripts/buffer-handler.py "
                "after the recent refactor."
            ),
        })
        case_g2 = {
            "title": "Optimize buffer-handler.py concurrency",
            "description": (
                "Refactor cache and concurrency layers in "
                "core/scripts/buffer-handler.py."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg2 = _run_gate(case_g2, tmp_world)
        rc2 = _find_check(rg2, "recent_completions")
        if rc2 is None:
            failures.append("G2: recent_completions check missing from result")
        elif rc2.get("passed") is not False:
            failures.append(
                f"G2: recent_completions should have failed (file-path block). "
                f"reason={rc2.get('reason')} matches={rc2.get('matches')}"
            )
        else:
            matches = rc2.get("matches") or []
            if not matches:
                failures.append("G2: expected matches non-empty on file-path block")
            elif not any(m.get("file_path_hits") for m in matches):
                failures.append(
                    f"G2: expected file_path_hits in matches, got "
                    f"{[m.get('file_path_hits') for m in matches]}"
                )

        # ── G3: structured-identifier hit_kw → BLOCK ─────────────────────
        # No file paths, but a keyword carrying hyphens+digits (a goal-id)
        # is shared. has_specific = re.search(r"[-_0-9]", k) -> True ->
        # hard block. This is the rare-identifier path (rb-335).
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G3",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Closed g-115-100 by adjusting recurring cadence in the "
                "scheduler module."
            ),
        })
        case_g3 = {
            "title": "Recurring cadence work referencing g-115-100 patterns",
            "description": (
                "Apply cadence adjustments referenced in the g-115-100 "
                "outcome record."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg3 = _run_gate(case_g3, tmp_world)
        rc3 = _find_check(rg3, "recent_completions")
        if rc3 is None:
            failures.append("G3: recent_completions check missing from result")
        elif rc3.get("passed") is not False:
            failures.append(
                f"G3: recent_completions should have failed (structured-id block). "
                f"reason={rc3.get('reason')} matches={rc3.get('matches')}"
            )
        else:
            matches = rc3.get("matches") or []
            if not matches:
                failures.append("G3: expected matches non-empty on structured-id block")
            else:
                has_structured_kw = False
                for m in matches:
                    for k in (m.get("keyword_hits") or []):
                        if any(ch in k for ch in "-_0123456789"):
                            has_structured_kw = True
                            break
                    if has_structured_kw:
                        break
                if not has_structured_kw:
                    failures.append(
                        f"G3: expected at least one keyword_hit with -_0-9, got "
                        f"{[m.get('keyword_hits') for m in matches]}"
                    )

        # ── G4: generic-token inflation FP (5) → DEMOTE ──────────
        # Two file-path-DISTINCT framework goals share ONE structural topic
        # token (loop_state, underscore) PLUS generic framework vocabulary
        # (global/exists/populated/recurring/class/close). Before the
        # 5 stopword expansion each generic counted as a unique_hit,
        # pushing N>=2 + weighted>=1.5 alongside the single structural
        # co-signal -> false hard block on distinct work (alpha session 92:
        # 4 such FPs; bravo: 2). After the fix the generics are stopwords, so
        # only loop_state remains (N=1) -> advisory, never a block. A single
        # shared structural token is intentionally NOT enough (MIN_UNIQUE_HITS=2).
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G4",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Adjusted loop_state global default so the populated flag "
                "exists for recurring class close events in the scheduler."
            ),
        })
        case_g4 = {
            "title": "Investigate: loop_state populated check",
            "description": (
                "Verify loop_state global value exists and is populated "
                "across the recurring class close path in a different module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg4 = _run_gate(case_g4, tmp_world)
        rc4 = _find_check(rg4, "recent_completions")
        if rc4 is None:
            failures.append("G4: recent_completions check missing from result")
        elif rc4.get("passed") is not True:
            failures.append(
                "G4: recent_completions should PASS (generic-token FP demote, "
                "g-115-1415). loop_state is the only real co-signal; "
                "global/exists/populated/recurring/class/close are generic "
                f"stopwords. reason={rc4.get('reason')} matches={rc4.get('matches')}"
            )

        # ── G5: genuine 2-identifier dup still BLOCKS after the 5 ─
        # stopword expansion (over-suppression guard). Two goals sharing TWO
        # structural identifiers (loop_state + iteration-checkpoint) keep
        # N>=2 even after the generic tokens (recurring/close) are stopworded
        # -> the legit duplicate signal is preserved, not collateral damage.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G5",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Fixed loop_state and iteration-checkpoint sync in the "
                "recurring close path."
            ),
        })
        case_g5 = {
            "title": "Investigate: loop_state iteration-checkpoint divergence",
            "description": (
                "Resolve loop_state vs iteration-checkpoint divergence "
                "during the recurring close window."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg5 = _run_gate(case_g5, tmp_world)
        rc5 = _find_check(rg5, "recent_completions")
        if rc5 is None:
            failures.append("G5: recent_completions check missing from result")
        elif rc5.get("passed") is not False:
            failures.append(
                "G5: recent_completions should BLOCK (two structural ids "
                "loop_state + iteration-checkpoint survive generic stopwording). "
                f"reason={rc5.get('reason')} matches={rc5.get('matches')}"
            )

        # ── G6: directive-routing goal (origin_signal=user_directive) → EXEMPT
        # Same file-path overlap as G2 (which BLOCKS), but this goal is a user
        # directive — its description necessarily recaps the target agent's
        # domain work, so completed-overlap is a structural FP. 4
        # exempts it -> recent_completions SKIPPED (passed, reason names the
        # exemption, matches empty). The shared file-path co-signal WOULD block
        # a non-directive goal (see G2), so this proves the EXEMPTION, not mere
        # absence of overlap.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G6",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Fixed cache concurrency bug in core/scripts/buffer-handler.py "
                "after the recent refactor."
            ),
        })
        case_g6 = {
            "title": "Directive: optimize buffer-handler.py concurrency for delta",
            "description": (
                "Routing to delta per the user directive: refactor cache and "
                "concurrency layers in core/scripts/buffer-handler.py."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "user_directive",
        }
        rg6 = _run_gate(case_g6, tmp_world)
        rc6 = _find_check(rg6, "recent_completions")
        if rc6 is None:
            failures.append("G6: recent_completions check missing from result")
        elif rc6.get("passed") is not True:
            failures.append(
                "G6: directive goal (origin_signal=user_directive) should be "
                f"EXEMPT (passed). reason={rc6.get('reason')} matches={rc6.get('matches')}"
            )
        elif "directive" not in (rc6.get("reason") or "").lower():
            failures.append(
                "G6: expected exemption reason to name 'directive', got "
                f"reason={rc6.get('reason')}"
            )
        elif rc6.get("matches"):
            failures.append(
                f"G6: exemption must yield no matches, got {rc6.get('matches')}"
            )

        # ── G7: cross-agent handoff goal (handoff_to set) → EXEMPT ────────
        # Same file-path overlap as G2, exempt because handoff_to is set (the
        # Bravo-handoff incident shape, ). recent_completions SKIPPED.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G7",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Fixed cache concurrency bug in core/scripts/buffer-handler.py "
                "after the recent refactor."
            ),
        })
        case_g7 = {
            "title": "Apply: buffer-handler.py concurrency fix",
            "description": (
                "Refactor cache and concurrency layers in "
                "core/scripts/buffer-handler.py."
            ),
            "participants": ["agent"],
            "source": "world",
            "handoff_to": "delta",
        }
        rg7 = _run_gate(case_g7, tmp_world)
        rc7 = _find_check(rg7, "recent_completions")
        if rc7 is None:
            failures.append("G7: recent_completions check missing from result")
        elif rc7.get("passed") is not True:
            failures.append(
                "G7: handoff goal (handoff_to set) should be EXEMPT (passed). "
                f"reason={rc7.get('reason')} matches={rc7.get('matches')}"
            )
        elif ("directive" not in (rc7.get("reason") or "").lower()
              and "handoff" not in (rc7.get("reason") or "").lower()):
            failures.append(
                "G7: expected exemption reason to name directive/handoff, got "
                f"reason={rc7.get('reason')}"
            )

        # ── G8: generic-VERB inflation FP (6) → DEMOTE ────────────
        # The recent_completions twin of G4, for generic English VERBS/adverbs
        # rather than framework state-vocab. Two semantically-unrelated goals
        # share ONLY common English words (cause/confirmed/every/hardening) plus
        # the hyphenated plain word "re-run". Before the 6 stopword
        # expansion "re-run" matched the has_specific hyphen co-signal
        # (re.search(r"[-_0-9]") — line 368) and the five generics pushed
        # N>=2 + weighted>=1.5, so a ZERO-file-path generic-verb overlap
        # HARD-blocked (session-93 ground truth: 5 vs echo ,
        # 7 vs delta  — 5 override-ledger entries). After the
        # fix all five are stopwords -> no shared keyword survives -> PASS.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G8",
            "completed_by": "echo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Every calibration hypothesis was confirmed after the recent "
                "re-run; the root cause of the drift needed hardening."
            ),
        })
        case_g8 = {
            "title": "Investigate: changelog contention cause and hardening",
            "description": (
                "Confirmed every changelog write can re-run under contention; "
                "trace the cause and apply hardening in a different module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg8 = _run_gate(case_g8, tmp_world)
        rc8 = _find_check(rg8, "recent_completions")
        if rc8 is None:
            failures.append("G8: recent_completions check missing from result")
        elif rc8.get("passed") is not True:
            failures.append(
                "G8: recent_completions should PASS (generic-verb FP demote, "
                "g-115-1726). cause/confirmed/every/hardening/re-run are generic "
                "English stopwords, not duplicate-work evidence; zero file-path "
                f"hits. reason={rc8.get('reason')} matches={rc8.get('matches')}"
            )

        # ── G9: over-suppression guard for the 6 verb stopwords ──
        # A GENUINE 2-identifier duplicate that ALSO carries the newly-added
        # generic verbs must still BLOCK — proving the verb stopwording did not
        # suppress real duplicate-work detection. Shared structural ids
        # loop_state (underscore) +  (goal-id, digits) survive
        # stopwording -> N>=2 -> hard block, even though cause/confirmed are
        # present and demoted.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G9",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Confirmed the cause of the loop_state desync referenced in "
                "g-115-999 and applied the fix."
            ),
        })
        case_g9 = {
            "title": "Investigate: loop_state divergence from g-115-999",
            "description": (
                "Confirmed cause of loop_state divergence tracked in g-115-999; "
                "resolve it in the restore path."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg9 = _run_gate(case_g9, tmp_world)
        rc9 = _find_check(rg9, "recent_completions")
        if rc9 is None:
            failures.append("G9: recent_completions check missing from result")
        elif rc9.get("passed") is not False:
            failures.append(
                "G9: recent_completions should BLOCK (loop_state + g-115-999 "
                "survive the g-115-1726 verb stopwording; two structural ids). "
                f"reason={rc9.get('reason')} matches={rc9.get('matches')}"
            )

        # ── G10: exclusion-context file-path FP (7) → DEMOTE ─────
        # The file-path twin of G4/G8, for a path named ONLY in a NEGATIVE
        # (exclusion) context. Both goals share strong PLAIN-word overlap
        # (fallback/coverage/sweep/audit/excluding — no -_0-9) so strong=True,
        # and BOTH name retrieve.sh in an "excluding retrieve.sh" clause. Before
        # the 7 exclusion-context disqualifier, retrieve.sh entered the
        # proposed goal's file_paths -> hit_paths=[retrieve.sh] -> has_specific
        # -> HARD block (canonical incident 6: 's
        # "feature-path-excluded for retrieve.sh" false-blocked a Maintain goal
        # at weighted 7.53). After the fix the exclusion-scoped path is dropped
        # from the co-signal set -> has_specific=False (no structured keyword
        # among the plain shared words) -> DEMOTE to advisory, never a block.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G10",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Ran the fallback coverage sweep audit, excluding retrieve.sh "
                "throughout the pass."
            ),
        })
        case_g10 = {
            "title": "Maintain: fallback coverage sweep audit",
            "description": (
                "Recurring fallback coverage sweep audit, excluding retrieve.sh "
                "from the scan."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg10 = _run_gate(case_g10, tmp_world)
        rc10 = _find_check(rg10, "recent_completions")
        if rc10 is None:
            failures.append("G10: recent_completions check missing from result")
        elif rc10.get("passed") is not True:
            failures.append(
                "G10: recent_completions should PASS (exclusion-context file-path "
                "FP demote, g-115-2207). retrieve.sh is named ONLY in an "
                "'excluding retrieve.sh' clause, so it is not aboutness and must "
                "not be a structural co-signal. reason="
                f"{rc10.get('reason')} matches={rc10.get('matches')}"
            )
        else:
            fp_detected = rg10.get("file_paths_detected") or []
            if "retrieve.sh" in fp_detected:
                failures.append(
                    "G10: retrieve.sh must be dropped from file_paths_detected "
                    f"(exclusion-context), got {fp_detected}"
                )

        # ── G11: recall / over-suppression guard for 7 ───────────
        # The adversarial genuine-POSITIVE control (guard-958): the SAME plain
        # overlap, but retrieve.sh is named POSITIVELY ("audit on retrieve.sh")
        # and IS the sole structural co-signal. It must STILL BLOCK — proving
        # the exclusion-context disqualifier did not suppress real file-path
        # duplicate detection. A path mentioned in aboutness context survives
        # the filter (recall preserved).
        _seed_state(tmp_world, {
            "goal_id": "g-scs-filler-G11",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Patched the fallback coverage sweep audit directly in "
                "retrieve.sh last pass."
            ),
        })
        case_g11 = {
            "title": "Maintain: fallback coverage sweep audit",
            "description": (
                "Recurring fallback coverage sweep audit performed on "
                "retrieve.sh directly."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg11 = _run_gate(case_g11, tmp_world)
        rc11 = _find_check(rg11, "recent_completions")
        if rc11 is None:
            failures.append("G11: recent_completions check missing from result")
        elif rc11.get("passed") is not False:
            failures.append(
                "G11: recent_completions should BLOCK (positive-context "
                "retrieve.sh is the sole structural co-signal; the g-115-2207 "
                "filter must NOT suppress genuine file-path dups). reason="
                f"{rc11.get('reason')} matches={rc11.get('matches')}"
            )
        else:
            matches = rc11.get("matches") or []
            if not any("retrieve.sh" in (m.get("file_path_hits") or [])
                       for m in matches):
                failures.append(
                    "G11: expected retrieve.sh in file_path_hits (recall), got "
                    f"{[m.get('file_path_hits') for m in matches]}"
                )

        # ── G12: recurring keyword-vacuum completion → DEMOTE () ──
        # A recurring/reflection COMPLETION (its goal_id carries recurring:true
        # in the aspirations queue) matched on hyphenated-compound KEYWORDS ONLY
        # (env-server + end-to-end — both trip has_specific via [-_0-9]) with
        # ZERO shared file path is a "keyword vacuum": generic domain vocab, not
        # duplicate work. Before  the two structured compounds pushed
        # N>=2 + weighted>=1.5 + has_specific=True → HARD block, false-blocking
        # new-capability goals (canonical incident: /104/105 blocked by
        # the  recurring sweep +  reflection). After the fix the
        # completion's recurring goal_id + empty hit_paths → recurring_vacuum →
        # DEMOTE to an advisory (recurring_vacuum_exempt), never a block.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-recur-vac",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Recurring sweep confirmed the env-server end-to-end reconnect "
                "handshake stayed healthy across the cycle."
            ),
        })
        _seed_recurring(tmp_world, ["g-scs-recur-vac"])
        case_g12 = {
            "title": "Investigate: env-server end-to-end reconnect gap",
            "description": (
                "Add an env-server end-to-end reconnect probe in a new "
                "monitoring module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg12 = _run_gate(case_g12, tmp_world)
        rc12 = _find_check(rg12, "recent_completions")
        if rc12 is None:
            failures.append("G12: recent_completions check missing from result")
        elif rc12.get("passed") is not True:
            failures.append(
                "G12: recent_completions should PASS (recurring keyword-vacuum "
                "demote, g-248-114). The completion goal_id is recurring and the "
                "env-server/end-to-end match has ZERO file-path hits — a keyword "
                f"vacuum, not duplicate work. reason={rc12.get('reason')} "
                f"matches={rc12.get('matches')}"
            )
        else:
            advisories = rc12.get("advisories") or []
            if not any(a.get("recurring_vacuum_exempt") for a in advisories):
                failures.append(
                    "G12: expected a recurring_vacuum_exempt advisory (demoted "
                    f"recurring completion), got advisories={advisories}"
                )

        # ── G13: NON-recurring keyword-vacuum → BLOCK ( control) ──
        # Identical vacuum shape to G12, but the completion goal_id is NOT
        # recurring (aspirations queue left empty by _seed_state, no
        # _seed_recurring). recurring_vacuum=False → the structured co-signal
        # still HARD-blocks. Proves the exemption is scoped to recurring
        # completions and did not blanket-suppress the structured-vacuum path.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-nonrecur-vac",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Refactor confirmed the env-server end-to-end reconnect "
                "handshake stayed healthy across the run."
            ),
        })
        # (no _seed_recurring — g-scs-nonrecur-vac is not in the recurring set)
        case_g13 = {
            "title": "Investigate: env-server end-to-end reconnect gap",
            "description": (
                "Add an env-server end-to-end reconnect probe in a new "
                "monitoring module."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg13 = _run_gate(case_g13, tmp_world)
        rc13 = _find_check(rg13, "recent_completions")
        if rc13 is None:
            failures.append("G13: recent_completions check missing from result")
        elif rc13.get("passed") is not False:
            failures.append(
                "G13: recent_completions should BLOCK (NON-recurring keyword "
                "vacuum — the g-248-114 exemption is scoped to recurring "
                "completions only; a non-recurring structured co-signal still "
                f"hard-blocks). reason={rc13.get('reason')} "
                f"matches={rc13.get('matches')}"
            )

        # ── G14: recurring completion + REAL file-path hit → BLOCK ────────
        #  control: the completion IS recurring, but it shares a real
        # FILE PATH (core/scripts/env-reconnect.py) with the proposed goal, so
        # hit_paths is non-empty → recurring_vacuum=False → still HARD-blocks.
        # Proves the exemption requires the VACUUM condition (empty hit_paths);
        # a recurring completion doing genuine shared-file work is NOT exempt.
        _seed_state(tmp_world, {
            "goal_id": "g-scs-recur-path",
            "completed_by": "bravo",
            "completed_at": _now_iso(-2),
            "key_finding": (
                "Recurring sweep patched core/scripts/env-reconnect.py during "
                "the env-server end-to-end reconnect pass."
            ),
        })
        _seed_recurring(tmp_world, ["g-scs-recur-path"])
        case_g14 = {
            "title": "Investigate: env-server reconnect fix",
            "description": (
                "Patch core/scripts/env-reconnect.py for the env-server "
                "end-to-end reconnect path."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        rg14 = _run_gate(case_g14, tmp_world)
        rc14 = _find_check(rg14, "recent_completions")
        if rc14 is None:
            failures.append("G14: recent_completions check missing from result")
        elif rc14.get("passed") is not False:
            failures.append(
                "G14: recent_completions should BLOCK (recurring completion "
                "sharing a REAL file path core/scripts/env-reconnect.py — "
                "hit_paths non-empty → recurring_vacuum=False → not exempt). "
                f"reason={rc14.get('reason')} matches={rc14.get('matches')}"
            )
        else:
            matches = rc14.get("matches") or []
            if not any("core/scripts/env-reconnect.py" in (m.get("file_path_hits") or [])
                       for m in matches):
                failures.append(
                    "G14: expected core/scripts/env-reconnect.py in file_path_hits, "
                    f"got {[m.get('file_path_hits') for m in matches]}"
                )

    finally:
        shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (14/14 cases)")
    return 0


def test_structural_co_signal_gate():
    """Pytest entry point (5) — runs the 14-case suite in an isolated
    tmp world and asserts all cases pass."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
