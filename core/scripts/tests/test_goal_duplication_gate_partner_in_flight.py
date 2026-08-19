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
  7. Vocabulary-only overlap (2+ BARE PLAIN WORDS, no path/compound/identifier)
     → passed=True AND demoted_count>=1 (g-115-3424). Before that change this
     check hard-blocked on raw unweighted overlap at N>=2, so this case
     BLOCKED — it is red against the old predicate. The demoted_count
     assertion is load-bearing: passing with demoted_count==0 would mean the
     case never exercised the demotion branch, a vacuous green that would keep
     passing if the hardening were reverted.
  8. Bare-word overlap PLUS a shared file path → would_block=True (g-115-3424).
     The demotion is scoped to overlaps that are only generic vocabulary; a
     structural co-signal must still hard-block, or the hardening would have
     disabled the check rather than sharpened it.
 12. Partner claim held ONLY in `in_flight_bodies` (reducer surface null),
     overlapping scope + structural co-signal → would_block=True (g-306-301).
     RED before that change: `in_flight` is reducer-owned and a non-reducer
     Body writes the body-keyed row instead, so the peer set was empty and the
     check returned "no partners in_flight" — a silent open gate. Measured
     2026-08-16: 1 of 7 live fleet claims visible.
 13. Body-keyed claim with VOCABULARY-ONLY overlap → passed=True AND
     demoted_count>=1 (g-306-301). The load-bearing companion to case 12: it
     pins that the g-115-3424 co-signal hardening governs Body-sourced entries
     too. Widening a peer set without this assertion is how a fix re-opens the
     false-positive class the hardening closed.

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

Looks for "PASS (13/13 cases)" in stdout. Also pytest-collectable via
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
                alpha_inflight=None, recent_completions=None,
                bravo_bodies=None):
    """Write a minimal team-state.yaml (into tmp_world) with controlled
    in_flight values.

    None for any agent slot → that agent's in_flight is null.
    bravo_inflight/zeta_inflight/alpha_inflight: dict with goal_id/title/phase/claimed_at.

    bravo_bodies (g-306-301): dict of {sid: body_row} written to bravo's
    `in_flight_bodies`. This is the surface a NON-REDUCER Body writes — the
    reducer stamp is SKIPPED when running-session-id != MIND_SID, so the two
    shapes are mutually exclusive per Body. Seeding it WITHOUT bravo_inflight
    is the realistic worker-partner fixture: `in_flight` null, claim live.

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

    # : ATTACH the body map only when seeded. The live store DELETES
    # `in_flight_bodies` once it empties (, clear_body_row), so a
    # fixture that unconditionally wrote `{}` would pin a shape the real store
    # never produces — and would quietly make every other case exercise a
    # not-in-the-wild input.
    if bravo_bodies is not None:
        agent_status["bravo"]["in_flight_bodies"] = bravo_bodies

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

        # ── Case 7 (): vocabulary-only overlap → DEMOTE, not block ──
        # Two BARE PLAIN WORDS shared with a partner's live title is topical
        # coincidence, not a claim conflict. Canonical FPs:  vs
        #  on [amplify, report]; guard-2742's 2026-08-11 case on
        # [lambda, mount]. Before  this check hard-blocked on raw
        # unweighted overlap at N>=2, so this case BLOCKED — that is the
        # red-against-the-old-predicate property.
        _seed_state(tmp_world, bravo_inflight={
            "goal_id": "g-pif-bravo-007",
            "title": "Apply: efs mount for the credit precheck lambda",
            "phase": "4",
            "claimed_at": _now_iso(-0.2),
        })
        case7 = {
            "title": "Investigate: lambda execution role policy and mount audit",
            "description": (
                "Read the lambda role policy. Report which mount points are "
                "reachable and whether the policy grants more than it needs."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        r7 = _run_gate(case7, tmp_world)
        pif7 = _find_check(r7, "partner_in_flight")
        if pif7 is None:
            failures.append("CASE 7: partner_in_flight check missing")
        elif pif7.get("passed") is not True:
            failures.append(
                f"CASE 7: vocabulary-only overlap should be DEMOTED, not blocked. "
                f"reason={pif7.get('reason')} matches={pif7.get('matches')}"
            )
        elif not pif7.get("demoted_count"):
            # Passing with demoted_count==0 means the case never exercised the
            # demotion branch at all (no hits) — a vacuous green that would keep
            # passing if the hardening were reverted. Assert the branch RAN.
            failures.append(
                f"CASE 7: expected demoted_count>=1 (the demotion branch must "
                f"have run); got {pif7.get('demoted_count')!r}. "
                f"reason={pif7.get('reason')}"
            )

        # ── Case 8 (): bare words + shared FILE PATH → still BLOCK ──
        # The demotion is scoped to overlaps that are ONLY generic vocabulary.
        # A shared file path is structural evidence of the same work and must
        # still hard-block, or the  hardening would have disabled the
        # check rather than sharpened it.
        _seed_state(tmp_world, bravo_inflight={
            "goal_id": "g-pif-bravo-008",
            "title": ("Apply: mount and lambda cleanup in "
                      "core/scripts/gates/goal_duplication.py"),
            "phase": "4",
            "claimed_at": _now_iso(-0.2),
        })
        case8 = {
            "title": "Investigate: lambda mount audit",
            "description": (
                "Audit core/scripts/gates/goal_duplication.py for the lambda "
                "and mount handling paths."
            ),
            "participants": ["agent"],
            "source": "world",
        }
        r8 = _run_gate(case8, tmp_world)
        pif8 = _find_check(r8, "partner_in_flight")
        if pif8 is None:
            failures.append("CASE 8: partner_in_flight check missing")
        elif pif8.get("passed") is not False:
            failures.append(
                f"CASE 8: shared file path must still HARD-block. "
                f"reason={pif8.get('reason')} matches={pif8.get('matches')}"
            )

        # ── Cases 9-11 (, re-homed at the 2026-08-12 cc-07 fork
        # reconcile): the three REAL false-blocks this hardening was measured
        # against. Case 7 proves the demotion branch fires on a synthetic pair;
        # these pin it to the actual incident token-shapes, which are the ones
        # that reached production. A synthetic fixture and a replayed incident
        # are not interchangeable — the incident is what establishes that the
        # shape occurs in the wild.
        #
        # Two independent Bodies fixed this check in the same fork window
        # ( here,  upstream) and converged on the same
        # discriminator. The upstream implementation was adopted as the
        # superset; these fixtures are the coverage the retired side uniquely
        # carried, so they are re-homed rather than dropped. Assertions follow
        # the ADOPTED  contract (passed + demoted_count), not the
        # retired side's reason-string wording.
        #   CASE 9  —  (Python ledger bug) blocked by foxtrot's
        #              (Java header test) on ['defect', 'omitted'].
        #   CASE 10 —  (email classifier) blocked by zeta's
        #              (block-signal reconciliation) on
        #             ['signal', 'sweep'].
        #   CASE 11 — a USER DIRECTIVE refused on ['session', 'single'] during
        #             the  inbox drain. An inbound directive exists
        #             in exactly ONE place, so the drain is its only conversion
        #             into durable queue state; this refusal would have
        #             discarded a written user approval silently. It survived
        #             only because the draining agent overrode the gate.
        live_incident_fps = [
            ("CASE 9", {
                "goal_id": "g-pif-bravo-009",
                "title": ("Add SidecarProxyVerticle Authorization-header "
                          "test for the omitted defect path"),
            }, {
                "title": ("Fix: pending-deploys ledger omitted a defect "
                          "on rollback"),
                "description": ("The ledger defect is omitted when the deploy "
                                "record rolls back; a Python bug in the "
                                "pending-deploys ledger."),
            }),
            ("CASE 10", {
                "goal_id": "g-pif-bravo-010",
                "title": "Reconcile blocked-goal block-signal during the sweep",
            }, {
                "title": "Fix: email-classifier signal misrouted in alert sweep",
                "description": ("alert-sweep.sh classifies the wrong signal "
                                "during the sweep."),
            }),
            ("CASE 11", {
                "goal_id": "g-pif-bravo-011",
                "title": "Investigate single-session OOM during a long session",
            }, {
                "title": "Directive: approve foxtrot OOM remedies, resize VM",
                "description": ("user approves both remedies; we must run "
                                "multi day sessions, not a single session."),
            }),
        ]
        for label, partner, proposed in live_incident_fps:
            partner = dict(partner, phase="4", claimed_at=_now_iso(-0.2))
            _seed_state(tmp_world, bravo_inflight=partner)
            proposed = dict(proposed, participants=["agent"], source="world")
            r_inc = _run_gate(proposed, tmp_world)
            pif_inc = _find_check(r_inc, "partner_in_flight")
            if pif_inc is None:
                failures.append(f"{label}: partner_in_flight check missing")
            elif pif_inc.get("passed") is not True:
                failures.append(
                    f"{label}: generic-English overlap must DEMOTE, not block. "
                    f"reason={pif_inc.get('reason')} "
                    f"matches={pif_inc.get('matches')}"
                )
            elif not pif_inc.get("demoted_count"):
                # Same vacuous-green guard as CASE 7: passing with
                # demoted_count==0 means the overlap never reached the
                # demotion branch, so the case would keep passing if the
                # hardening were reverted.
                failures.append(
                    f"{label}: expected demoted_count>=1 (the demotion branch "
                    f"must have run); got {pif_inc.get('demoted_count')!r}. "
                    f"reason={pif_inc.get('reason')}"
                )

        # ── Cases 12-13 (): Body-keyed partner claims ───────────
        # `in_flight` is REDUCER-OWNED; a non-reducer Body writes
        # `in_flight_bodies.<sid>` on a mutually exclusive branch. Before this
        # fix the check read only `in_flight`, so a WORKER partner was invisible
        # and the peer set silently emptied — measured 2026-08-16 at 1 visible
        # of 7 live claims fleet-wide. Both fixtures set in_flight=None to model
        # the real worker-partner shape (claim live, reducer surface null).
        #
        # CASE 12 is RED against the pre-fix predicate (peer set empty →
        # "no partners in_flight" → passed). CASE 13 is the load-bearing
        # companion: it proves the  co-signal hardening still governs
        # Body-sourced entries. Without it, this fix could be "verified" by a
        # test that only ever exercises the hard-block path — which is exactly
        # how widening a peer set turns into re-opening the FP class the
        # hardening closed.

        # ── Case 12: body-keyed claim, structural co-signal → BLOCK ──────
        _seed_state(tmp_world, bravo_inflight=None, bravo_bodies={
            "sid-pif-body-aaa": {
                "goal_id": "g-pif-bravo-012",
                "title": "Apply: execution-diary observer-session gate write paths",
                "phase": "4",
                "claimed_at": _now_iso(-0.2),
            },
        })
        case12 = {
            "title": "Idea: execution-diary observer-session gate for cmd_append",
            "description": (
                "Add observer-session guard to execution-diary cmd_append. "
                "Skip writes when MIND_SID differs from running-session-id."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",
        }
        r12 = _run_gate(case12, tmp_world)
        pif12 = _find_check(r12, "partner_in_flight")
        if pif12 is None:
            failures.append("CASE 12: partner_in_flight check missing from result")
        elif pif12.get("passed") is not False:
            failures.append(
                f"CASE 12: a Body-keyed partner claim must be VISIBLE and block "
                f"on a structural co-signal (pre-g-306-301 this peer set was "
                f"empty). reason={pif12.get('reason')} "
                f"matches={pif12.get('matches')}"
            )
        elif not any(m.get("agent") == "bravo" and m.get("goal_id") == "g-pif-bravo-012"
                     for m in (pif12.get("matches") or [])):
            # Guards against passing for the wrong reason: the block must name
            # the BODY row, not some other peer entry.
            failures.append(
                f"CASE 12: expected the bravo body row g-pif-bravo-012 in "
                f"matches; got {pif12.get('matches')}"
            )

        # ── Case 13: body-keyed claim, vocabulary-only → DEMOTE not block ─
        _seed_state(tmp_world, bravo_inflight=None, bravo_bodies={
            "sid-pif-body-bbb": {
                "goal_id": "g-pif-bravo-013",
                "title": "Reconcile blocked-goal block-signal during the sweep",
                "phase": "4",
                "claimed_at": _now_iso(-0.2),
            },
        })
        case13 = {
            "title": "Fix: email-classifier signal misrouted in alert sweep",
            "description": (
                "alert-sweep.sh classifies the wrong signal during the sweep."
            ),
            "participants": ["agent"],
            "source": "world",
            "origin_signal": "parent_aspiration:asp-115",
        }
        r13 = _run_gate(case13, tmp_world)
        pif13 = _find_check(r13, "partner_in_flight")
        if pif13 is None:
            failures.append("CASE 13: partner_in_flight check missing")
        elif pif13.get("passed") is not True:
            failures.append(
                f"CASE 13: vocabulary-only overlap against a BODY row must be "
                f"DEMOTED, not blocked — the co-signal hardening must govern "
                f"Body-sourced entries too. reason={pif13.get('reason')} "
                f"matches={pif13.get('matches')}"
            )
        elif not pif13.get("demoted_count"):
            # Same vacuous-green guard as CASE 7: passing with demoted_count==0
            # would mean the body row never reached the demotion branch at all
            # (e.g. it was never enumerated), so the case would keep passing if
            # this whole fix were reverted.
            failures.append(
                f"CASE 13: expected demoted_count>=1 (the body row must have "
                f"reached the demotion branch); got "
                f"{pif13.get('demoted_count')!r}. reason={pif13.get('reason')}"
            )

    finally:
        if tmp_world.exists():
            shutil.rmtree(tmp_world, ignore_errors=True)

    if failures:
        print(f"FAIL ({len(failures)} cases)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS (13/13 cases)")
    return 0


def test_partner_in_flight_gate():
    """Pytest entry point () — runs the full case suite (tmp-world
    isolated) and asserts all cases pass. Deliberately not a case COUNT: the
    prior wording said "6-case" while the suite carried 11, so a reader
    debugging a failure was told the wrong population."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
