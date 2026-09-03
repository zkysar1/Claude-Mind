"""Reap stale `in_flight_bodies` rows left behind by unclean Body deaths ().

THE ASYMMETRY THIS CLOSES
-------------------------
The reducer `in_flight` row has reclamation for the unclean-death case —
`stranded-claim-sweep` runs every iteration and `recovery-gate` handles the
zombie path. Its body-keyed sibling `in_flight_bodies.<sid>` had none: the only
reclaimer, `worker_close_in_flight_clear.clear_body_row`, runs on a CLEAN
turn-end. A Body that dies without one (crash, kill, or the text-death-under-
API-storm shape in `.claude/rules/schedule-wakeup-correctness.md`) leaves a LIVE
row carrying `goal_id` and `claimed_at`, permanently.

MEASURED 2026-08-08 (bravo, hostname cc-05, uname -r 6.8.0-136-generic): four
live body rows fleet-wide, the oldest 66.3h (`alpha/41baa470…`, goal g-306-227).
The filing goal predicted the population would be ~0 and said to re-price the
moment it was not; it is not.

BLAST RADIUS, MEASURED — AND SMALLER THAN THE FILING ASSUMED
------------------------------------------------------------
Two consumers read these rows, and only one of them is reachable here:

  `goal-pickup-coordination-check._partner_in_flight` — LIVE. It filters only on
  `name != me`, sorts by `claimed_at` descending and returns `candidates[0]`,
  with no age/cutoff/staleness guard anywhere in the function (read whole,
  L1224-1296). A phantom wins whenever it is the NEWEST claim — i.e. during
  fleet-quiet windows. It never expires on its own.

  `_cross_agent_attribution_filter.filter_paths` Source 1 — NOT reachable on a
  single-resident box. `_body_epochs` genuinely has NO age cutoff (the
  `_max_age_sec`/`_entry_is_stale` machinery applies only to Source 2), which is
  what the filing suspected. But Source 1 is gated behind
  `partners = _discover_known_agents(project_root) - {self}`, and that helper
  requires a per-agent `local-paths.conf`, which on any single-resident box
  exists only for the resident agent. Measured on cc-05: `known_agents ==
  {'bravo'}` -> `partners == set()` -> `partner_epochs == {}`. A controlled
  A/B/C/D (real 66.3h phantom present vs removed, crossed with self-claim held
  vs absent) returned IDENTICAL kept/dropped in all four cells.

  So the filing's hypothesis — "if body epochs are already aged out there, the
  harm is confined to the pickup gate" — reaches the right conclusion by the
  wrong route, and ONLY on a single-resident box. On a box with 2+ resident
  agents Source 1 becomes reachable and the phantom matters there too, where
  `min()` makes an OLD phantom beat every fresher legitimate claim. Do not
  generalise the "inert" reading past the box it was measured on.

WHY THIS IS A REAPER AND NOT A NEW WRITER
-----------------------------------------
The removal primitive already exists: `clear_body_row(agent, sid)` ->
`POST /v1/team-state/clear-body-row`, idempotent, key-deleting, residency-gated.
This module contributes only the DECISION of which rows are dead.

WHY THE OWNING AGENT MUST RUN IT
--------------------------------
`merge_team_state_shard` reconciles a diverged shard by whole-snapshot
last-writer-wins on `last_active`, so a non-owner pruner's write beats the
owner's fresher state. A predicate applied by the OWNING agent survives the
merge. This module therefore decides only about rows on the caller's own shard;
the integration passes nothing else. (Same finding that ruled out candidate (b)
in g-306-186.)

WHY IT CONSUMES AN EXISTING CARRIER VERDICT RATHER THAN FORMING ITS OWN
-----------------------------------------------------------------------
Two liveness opinions about one Body are worse than one. The verdict vocabulary
here is `stranded-claim-sweep._body_carrier_verdict`'s, which already reads the
carrier from the store of record (guard-980), already distinguishes
`absent`/`unreadable` from `stale` (guard-2418), and already implements guard-358
(a carrier cannot vouch for a body that did not write it). Re-deriving any of
that would be the second opinion this module exists to avoid.

WHY IT MUST NOT KEY ON THE SHARD OBJECT'S WRITE TIME
----------------------------------------------------
Under the Mind/Body split any Body on a box can write the shard while the MIND
is dead, so the object write time proves only that something on that machine
wrote (g-306-132-e, and the same asymmetry `check-team-state-before-silent.md`
rule 6 turns on). The freshness signal is the syncable
`session/body-heartbeat-<SID>.json` carrier.

FAIL-SAFE DIRECTION
-------------------
A wrongly-reaped row does NOT self-heal: rows are written by
`team-state-in-flight.sh` at CLAIM time, not on every tick, so a live Body whose
row was reaped stays invisible for the rest of its goal — and invisible partner
work is precisely the guard-741 hazard. Every ambiguous signal therefore KEEPS.
`absent` and `carrier-sid-mismatch` are KEEP even though neither establishes
life: not establishing life is not the same as establishing death, and only the
latter licenses a reap.

Note what the one reaping branch already guarantees: it requires
`holds_live_claim == False`, i.e. the SID owns no non-terminal claim in EITHER
queue a Body of this agent can claim into — the world queue and the owning
agent's own queue. Reaping such a row cannot orphan in-flight work, because
there is none to orphan. That invariant is what makes the reap safe; every KEEP
branch exists because it is not satisfied.

The scope of `claims` is load-bearing, not incidental, and this paragraph
formerly stated it in the premise and dropped it from the conclusion (g-306-270).
The caller built the map from the world queue alone, so a Body whose only claim
sat in the agent queue read as holding NO claim and became reapable — the
invariant was true of a narrower store than the sentence claimed. Any future
caller that narrows `claims` re-opens that hole silently, because this module
cannot see where the map came from: `decide` is pure and takes the map as given.
So a caller MUST supply claims spanning both queues (`worker_stall
.read_claims_union`), and MUST decline the reap outright when either half is
unreadable rather than passing a partial map — an absent claim and an unread
claim are indistinguishable HERE, and only one of them is safe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Verdict tokens. Kept distinct rather than collapsed to reap/keep for the
# guard-2418 reason its siblings state: several take the same action today, but
# merging them would erase the signal that tells a future reader WHICH guard is
# holding — and they imply different follow-ups (a fleet of `no-carrier` means
# the carrier pipeline is broken; a fleet of `alive` means it is working).
R_REAP = "reap"                               # stale + no live claim -> orphan
R_REAP_TERMINAL_GOAL = "reap-terminal-goal"   # the row's goal is FINISHED
K_SELF_SID = "self-sid"                       # this process's own row
K_NO_CARRIER = "no-carrier"                   # carrier absent -> death unproven
K_ALIVE = "alive"                             # carrier fresh and ours
K_STALLED_WITH_CLAIM = "stalled-with-claim"   # WorkerStallProbe's territory
K_UNREADABLE = "unreadable"                   # carrier present, no usable ts
K_SID_MISMATCH = "carrier-sid-mismatch"       # guard-358
K_NULL_RESIDUE = "null-residue"               # pre- null-valued key

#: The verdicts that mutate. TWO, since  item 3 -- and they reap on
#: DIFFERENT evidence, which is why the second is not folded into the first:
#: `R_REAP` concludes the BODY is gone (a stale carrier), while
#: `R_REAP_TERMINAL_GOAL` concludes the WORK is gone (the store says the goal is
#: finished) and says nothing at all about the Body, which is usually alive and
#: simply moved on. Keeping the tokens distinct keeps the two populations
#: countable in `verdict_counts`, which is how the next decision about either
#: predicate gets made (guard-2293).
REAPING_VERDICTS = frozenset({R_REAP, R_REAP_TERMINAL_GOAL})

#: Deliberately LONGER than the sweep's own `--carrier-fresh-minutes`. That
#: threshold governs whether to HOLD A CLAIM, which is reversible on the next
#: sweep; this one governs whether to DELETE A ROW, which is not (see FAIL-SAFE
#: above). Where the two disagree, the extra hours cost one more cycle of a
#: phantom nobody was reading; the opposite error costs a live Body its
#: visibility for a whole goal.
DEFAULT_REAP_STALE_MINUTES = 180.0

#: Carrier-verdict tokens from `stranded-claim-sweep._body_carrier_verdict`.
#: Named rather than inlined so a rename there fails loudly here.
CV_FRESH_CORRECT = "fresh-correct"
CV_FRESH_WRONG = "fresh-wrong"
CV_STALE = "stale"
CV_ABSENT = "absent"
CV_UNREADABLE = "unreadable"


def is_reaping(verdict: str) -> bool:
    """Single source of truth for which verdicts mutate. Callers must not
    re-derive this with their own string comparison — that is how a ninth
    verdict added later silently starts or stops reaping (`worker_stall`'s
    `is_alerting` carries the same warning for the same reason)."""
    return verdict in REAPING_VERDICTS


def decide_row(
    sid: str,
    row: Any,
    carrier_verdict: Optional[str],
    carrier_evidence: Optional[Dict[str, Any]] = None,
    holds_live_claim: bool = False,
    self_sid: Optional[str] = None,
    goal_is_terminal: Optional[bool] = None,
) -> Dict[str, Any]:
    """Pure per-row decision — every branch reachable with no daemon and no I/O.

    `goal_is_terminal` is TRI-STATE and the third state is load-bearing
    (g-306-412 item 3): True = the queue SAYS this row's goal is finished,
    False = the queue was read and the goal is not terminal, None = NOT
    MEASURED (no goal_id on the row, the store was unreadable, or a caller that
    predates the parameter). Only True reaps. A `bool` here would collapse
    "unmeasured" into "not terminal", which is the harmless direction -- but it
    would also let a future caller silently opt the whole predicate out while
    still type-checking, so the absence is kept nameable (guard-2418)."""
    ev = carrier_evidence or {}
    out: Dict[str, Any] = {
        "sid": sid,
        "goal_id": row.get("goal_id") if isinstance(row, dict) else None,
        "claimed_at": row.get("claimed_at") if isinstance(row, dict) else None,
        "carrier_verdict": carrier_verdict,
        "carrier_age_minutes": ev.get("carrier_age_minutes"),
        "holds_live_claim": bool(holds_live_claim),
        # Emitted as the tri-state it is, never coerced: a `false` and a `null`
        # here mean "the queue said not-terminal" and "nobody looked", and a
        # reader triaging why a row survived needs to tell those apart.
        "goal_is_terminal": goal_is_terminal,
    }

    # Order matters: the most certain KEEPs come first, so an ambiguous carrier
    # can never talk us past a definite hold.
    if not isinstance(row, dict):
        # Pre- null residue. NOT reaped: it carries no goal_id and no
        # claimed_at, so it is not a phantom claim (every consumer
        # isinstance-guards it), and the clear-body-row endpoint already sweeps
        # null siblings on first use. Reported so that drain stays observable.
        out["verdict"] = K_NULL_RESIDUE
        return out

    if self_sid and sid == self_sid:
        # The running session's own row. Its carrier is fresh by construction,
        # so the alive branch would cover it — but relying on that would make
        # self-preservation depend on this process's own heartbeat having ticked
        # recently, which is exactly what breaks under the API-storm shape this
        # defect is about. Belt and braces, deliberately.
        out["verdict"] = K_SELF_SID
        return out

    if goal_is_terminal is True:
        # THE WORK IS OVER, whatever the Body is doing ( item 3).
        #
        # Every branch below this one asks "is the BODY alive", and until now
        # that was the only question asked. It cannot reach a row whose Body is
        # perfectly alive and has simply MOVED ON: the carrier is fresh, so the
        # row is `alive` and kept forever, while the goal it names finished
        # hours ago. That row is not live contention -- it is a phantom claim,
        # and `goal-pickup-coordination-check._partner_in_flight` picks the
        # NEWEST body row with no staleness guard of any kind, so a phantom
        # wins outright whenever it is the newest.
        #
        # MEASURED 2026-09-03 on the live shard: alpha carried 7 body rows, two
        # of them naming goals `completed` on 2026-09-01 ( claimed
        # 14:19,  claimed 20:14) -- 37h and 31h of phantom. Neither was
        # reachable by the carrier predicate: `CV_ABSENT` returns K_NO_CARRIER
        # and `CV_FRESH_CORRECT` returns K_ALIVE, so the ONLY reaping path was
        # `CV_STALE` + no live claim, and neither row was ever in that state.
        #
        # ORDERED AFTER `self_sid` DELIBERATELY, not by oversight. A row of the
        # RUNNING session naming a finished goal is a miss in the CLEAN-close
        # path (`worker_close_in_flight_clear`), and that is where it should be
        # fixed; reaping it here would mask the defect and would weaken the
        # belt-and-braces self-preservation the API-storm shape motivated.
        #
        # ORDERED BEFORE EVERY CARRIER BRANCH because this is the stronger
        # evidence: a terminal status is a FACT the store asserts, while every
        # carrier verdict is an inference from an mtime. Only `True` reaches
        # here -- `None` (unmeasured) and `False` fall through untouched, so a
        # caller that cannot answer the question changes nothing.
        out["verdict"] = R_REAP_TERMINAL_GOAL
        return out

    # guard-358, both spellings. `fresh-wrong` is the explicit token; a STALE
    # carrier written by a different sid collapses to plain `stale` upstream but
    # still sets `carrier_sid` in the evidence, so check that too or a mismatched
    # stale carrier would be read as a death certificate for a body it never
    # described.
    #
    # PRESENCE, not truth (). The sole producer
    # (stranded-claim-sweep._body_carrier_verdict) writes this key on
    # `str(doc.get("sid") or "") != sid`, so it fires for an UNIDENTIFIED writer
    # too and stores the empty string. Under the old `ev.get("carrier_sid")` that
    # row was falsy and fell through to R_REAP, while the same empty sid arriving
    # as `fresh-wrong` was kept by the clause on the left — i.e. an anonymous
    # carrier was distrusted when FRESH and trusted when STALE, which is backwards
    # for a DELETE path and is the exact asymmetry the comment above says this
    # check prevents. The key's PRESENCE is the signal; its value is only ever a
    # display prefix (`[:8]`).
    if carrier_verdict == CV_FRESH_WRONG or "carrier_sid" in ev:
        out["verdict"] = K_SID_MISMATCH
        return out

    if carrier_verdict == CV_ABSENT:
        out["verdict"] = K_NO_CARRIER
        return out
    if carrier_verdict == CV_FRESH_CORRECT:
        out["verdict"] = K_ALIVE
        return out
    if carrier_verdict == CV_STALE:
        out["verdict"] = K_STALLED_WITH_CLAIM if holds_live_claim else R_REAP
        return out

    # CV_UNREADABLE and anything unrecognised. An unmapped verdict lands here,
    # never on R_REAP: a sixth token added upstream must not silently acquire
    # the power to delete rows.
    out["verdict"] = K_UNREADABLE
    return out


def _goal_terminal(row: Any, terminal_goal_ids: Optional[set]):
    """Tri-state: is THIS row's goal in a terminal status? ( item 3)

    `None` whenever the question was not answerable -- the caller passed no id
    set (it could not read the queues, or predates the parameter), or the row
    carries no `goal_id` to look up. Never guesses: an id absent from a set that
    was genuinely read means the goal is NOT terminal, which is `False`, and the
    two are kept apart because only one of them may ever reach a delete.
    """
    if terminal_goal_ids is None:
        return None
    if not isinstance(row, dict):
        return None
    gid = row.get("goal_id")
    if not gid:
        return None
    return str(gid) in terminal_goal_ids


def decide(
    rows: Dict[str, Any],
    carrier_verdicts: Dict[str, Any],
    claims_by_sid: Dict[str, str],
    self_sid: Optional[str] = None,
    terminal_goal_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Decide over one agent's whole `in_flight_bodies` map. Pure.

    `carrier_verdicts` maps sid -> (verdict, evidence), the exact return shape of
    `_body_carrier_verdict`. `rows` MUST be the caller's OWN rows — see the
    owning-agent note in the module docstring. This function cannot enforce that
    (it cannot see whose shard it was handed); the integration is responsible and
    its test pins it.
    """
    decisions: List[Dict[str, Any]] = []
    for sid in sorted(rows or {}):
        cv = (carrier_verdicts or {}).get(sid) or (None, {})
        verdict, evidence = (cv if isinstance(cv, tuple) else (cv, {}))
        decisions.append(
            decide_row(
                sid=sid,
                row=(rows or {})[sid],
                carrier_verdict=verdict,
                carrier_evidence=evidence,
                holds_live_claim=(claims_by_sid or {}).get(sid) is not None,
                self_sid=self_sid,
                goal_is_terminal=_goal_terminal(
                    (rows or {})[sid], terminal_goal_ids),
            )
        )
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d["verdict"]] = counts.get(d["verdict"], 0) + 1
    return {
        "scanned": len(decisions),
        "reapable": [d for d in decisions if is_reaping(d["verdict"])],
        "decisions": decisions,
        "verdict_counts": counts,
    }
