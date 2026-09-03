#!/usr/bin/env python3
"""worker_execute.py -- Phase 2A: worker-body simplified execution contract.

Mind/Body convergence (asp-306, g-306-69). A WORKER Body of a Mind runs a
SIMPLIFIED per-Body loop: select -> claim -> execute, then STOPS. It SKIPS the
reducer-only phases (verify / encode / reflect / state-update / learning-gate /
evolution / complete-review / productivity-check) -- those are the SINGLE
reducer's responsibility, applied later to all Bodies' MERGED state at
generalize-down (Phase 1C body-merge.py, run from aspirations-consolidate
Step -1). Running encode/reflect per-worker is the "N reducers = a defect"
invariant the convergence forbids.

This module is the DETERMINISTIC, TESTABLE contract the worker-loop skill
(.claude/skills/worker-loop/SKILL.md) consults:
  - WORKER_PHASES / REDUCER_ONLY_PHASES -- the phase split.
  - worker_should_run_phase(phase)      -- the phase gate (a worker runs
    select/claim/execute, skips the reducer-only phases).
  - LIFECYCLE_DISPOSITIONS              -- the SESSION-LIFECYCLE split (g-306-212):
    every session stage mapped to exactly one declared worker disposition, so a
    lifecycle asymmetry fails loudly at edit time instead of surfacing by
    surprise. See "lifecycle contract" below.
  - SKILL_LIFECYCLE_STAGE / skill_eligibility(skill) -- the SKILL bridge
    (g-115-5664): a goal record names its skill ("/replay --sharp-wave"), a
    third vocabulary that nothing mapped to the two tables above, so a worker
    could be handed a goal whose skill IS a reducer-only lifecycle stage. The
    disposition is DERIVED from LIFECYCLE_DISPOSITIONS, never restated.
  - worker_wm_path(agent, unit_key)     -- the worker writes ONLY its own forked
    Body WM, reusing the Phase-1A reducer-aware activation signal (the forked
    body-WM-file's existence). Mirrors mind_api/src/agent_paths.py::wm_path so
    the CLI worker and the daemon agree on a Body's WM target.

Backward-compat: with one Body (the reducer), or no unit_key, worker_wm_path
collapses to the agent-wide WM -- inert until a 2nd Body forks (the same
dormant-until-2nd-body property as every Phase-1 plumbing tier). select + claim
themselves are NOT reimplemented here -- the worker-loop skill REUSES the
existing goal-selector.sh + aspirations-claim.sh (claimed_by stays the
mindKey/agent-name); this module owns only the phase split + WM target that are
NEW to the worker path.

Design SSOT: world/knowledge/tree/intelligence/ayoai-architecture/
universal-environment-abstraction/mind-engine-identity-bridge.md (Phase 2).
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import subprocess
import sys
from pathlib import Path

# _paths is the SSOT for the agent-dir layout constants. Importing them (rather
# than hardcoding the "agents"/"sessions"/"session" segments) makes
# worker_wm_path auto-track an AGENTS_PARENT_DIR / SESSIONS_DIRNAME rename, per
# CLAUDE.md "Agent-dir Resolution". Only PROJECT_ROOT, AGENT_NAME, and the three
# layout constants are imported -- NOT AGENT_DIR (which is None when MIND_AGENT
# is unset, e.g. under pytest), so this import is safe in every context.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _paths import (  # noqa: E402
    PROJECT_ROOT,
    AGENT_NAME,
    AGENTS_PARENT_DIR,
    SESSIONS_DIRNAME,
    SESSION_DIRNAME,
)

# --------------------------- phase contract ---------------------------

# The phases a WORKER Body runs -- the simplified per-Body path (design: "a
# simplified per-Body select->claim->execute that SKIPS verify/encode/reflect").
#
# `verify-own-unit` (, owner directive 2026-09-03) is the FOURTH phase
# and the only one that is not select/claim/execute. It is the LLM verification
# of THE UNIT THIS BODY JUST EXECUTED -- its hypothesis outcome, its Q1/Q2/Q3
# escalation, and the unblocking of goals whose `blocked_by` named it. It is NOT
# the reducer's `verify` phase, which stays in REDUCER_ONLY_PHASES below as the
# RESIDUE (the reducer's own units, plus the sampled completion review that
# checks this self-grading).
#
# WHY THIS ONE MOVED AND THE OTHERS DID NOT -- the convergence invariant is
# untouched. "One Mind, one encoder" is about SHARED KNOWLEDGE: tree nodes, the
# reasoning bank, guardrails, reflection. Verification writes GOAL STATE
# (status, escalations, blocked_by clears), which is per-goal and already
# per-Body-owned -- worker-loop Phase 4a has recorded the caller-declared status
# through the shared close writer since 2026-08-16 (). So this is the
# largest per-goal reducer cost carrying the least convergence risk: the reducer
# was re-deriving the judgment from notes it did not write, for 50+ worker
# closures a day. Encoding, reflection, state-update, evolution and the learning
# gate all REMAIN reducer-only and must not follow this one out.
WORKER_PHASES = ("select", "claim", "execute", "verify-own-unit")

# The phases a worker SKIPS -- reducer-only (encode / reflect / consolidate). The
# single reducer applies these to the MERGED state of ALL Bodies at
# generalize-down, NOT per-Body. Names match the aspirations-loop-digest phase
# labels so the worker-loop skill can gate by the same identifiers the full loop
# uses.
REDUCER_ONLY_PHASES = frozenset({
    # "verify" here is Phase 5 as an LLM PHASE -- /aspirations-verify's hypothesis
    # outcomes, Q1/Q2/Q3 escalation, streak tracking, dependent-goal unblocking.
    # It is NOT the mechanical status write. Since 2026-08-16 a worker records the
    # status IT judged for the unit it just executed by a scoped call to the shared
    # close writer (worker-loop Phase 4a -> `iteration-close.sh --phase verify`,
    # i.e. do_verify: status/completed_date/outcome_class, complete-by for
    # recurring, board post, goal-scoped in_flight clear). Measured before that
    # change (alpha reducer, cc-04, ): 360 of 361 open claims were
    # finished work left at in-progress "for the reducer", and no reducer lane
    # ever flipped one -- worker_retrospective.py has no close lane and
    # body-merge.py only names ids. Capturing the caller-declared outcome is
    # part of executing; running the LLM verify phase is the phase. Do not read
    # Phase 4a as a violation of this entry.
    #
    # SPLIT 2026-09-03 (): the per-unit half of this phase moved to the
    # worker as `verify-own-unit` (see WORKER_PHASES). What REMAINS here is the
    # RESIDUE, and it is genuinely reducer-only:
    #   - verification of units the REDUCER itself executed;
    #   - the SAMPLED completion review over worker-verified closures, which is
    #     the check on self-grading -- a Body cannot be the sole grader of its
    #     own work, so the sampling stays with the other Body by construction;
    #   - any cross-unit judgment that needs MERGED state (streak tracking
    #     across Bodies), which by definition only exists after generalize-down.
    # A worker asking `should-run-phase verify` still gets False, and that is
    # correct: it runs `verify-own-unit`, a different phase with a narrower
    # scope. Do not "simplify" by making a worker run `verify` -- the scope
    # difference IS the safety property (guard-4638: a worker closing a goal
    # does not mean its code landed on main, so verify-own-unit must never
    # claim landing).
    "verify",             # Phase 5   -- outcome verification (LLM phase, reducer residue)
    # A worker does NOT run the spark PHASE -- it creates no rb/guardrail/tree
    # artifact (that would make it an Nth reducer). It DOES record raw spark
    # observations into the `spark_capture` WM slot during execute
    # (worker-loop Phase 3.5, ); the reducer replays them through the
    # real handlers at aspirations-spark Phase 6.5 after generalize-down.
    # Capturing an observation is part of executing; running the handlers is
    # the phase. Do not read Phase 3.5 as a violation of this entry.
    "spark",              # Phase 6   -- immediate learning / encoding
    "complete-review",    # Phase 7   -- aspiration completion review
    "state-update",       # Phase 8   -- tree encoding / journal / state
    "evolution",          # Phase 9   -- strategy evolution
    "learning-gate",      # Phase 9.5 -- reflection / learning audit
    "productivity-check",  # Phase 12  -- productivity gate
})


def worker_should_run_phase(phase: str) -> bool:
    """True iff a worker Body runs `phase` itself.

    A worker runs EXACTLY its three simplified phases (select/claim/execute) and
    skips every reducer-only phase. Any phase outside WORKER_PHASES -- including
    the reducer-only set AND any unrecognised phase -- returns False
    (conservative: a worker never runs a phase not explicitly granted to it).
    """
    return phase in WORKER_PHASES


# --------------------------- lifecycle contract () ---------------------------
#
# WHY THIS TABLE EXISTS
# The phase split above has an SSOT. The session LIFECYCLE did not, and every
# lifecycle asymmetry found so far was discovered BY SURPRISE, each by a
# different route: prime never runs for workers (, user question
# 2026-08-04); the per-body heartbeat cannot write on an IDLE worker box
# (, suite red + live claim-pop trace); compact restore rejected
# body-keyed checkpoints (, live autocompact close). One defect class:
# a reducer lifecycle stage with NO DECLARED worker disposition.
#
# THE NO-TRANSCRIPTION RULE (the contract this table enforces)
# A worker capability is a scoped CALL into the shared component -- a mode or
# flag INSIDE that component -- NEVER a transcription of its steps into
# worker-loop text. A transcription is a second copy that drifts silently when
# the component evolves, and nothing fails when it does. So `scoped-call` names
# the EXISTING component plus the mode; it never names a reimplementation.
# The loop COUNT stays two by measured necessity (wf_ea3e054b, 50:1 unification
# cost). This contract is what keeps the CODE count at one per capability.
#
# WHAT THIS TABLE CANNOT DO -- read this before trusting a green check.
# The completeness check asserts the TABLE covers CANONICAL_LIFECYCLE_STAGES.
# Both live in THIS file, so it cannot detect a stage that exists in the reducer
# loop but was never added to the canonical list. That residual gap is real and
# is named here rather than papered over (guard-2582: a static checker's silence
# about your file is not coverage until you confirm the file is in its
# population). What DOES have teeth is PHASE_LIFECYCLE_STAGE below: it couples
# the table to WORKER_PHASES / REDUCER_ONLY_PHASES, which are themselves
# consumed by the live gate -- so adding a PHASE without declaring its lifecycle
# stage fails at import, not at 3am.

SHARED_COMPONENT = "shared-component"
SCOPED_CALL = "scoped-call"
WORKER_ONLY = "worker-only"
REDUCER_ONLY_BY_DESIGN = "reducer-only-by-design"

DISPOSITION_KINDS = frozenset({
    SHARED_COMPONENT, SCOPED_CALL, WORKER_ONLY, REDUCER_ONLY_BY_DESIGN,
})

# Goal-id shape per CLAUDE.md § ID Formats: g-NNN-NN with 2-4 trailing digits.
# \Z, NOT $ (guard-1283): in Python `$` also matches immediately BEFORE a
# trailing newline, so "\n" satisfied a `$`-anchored .match() and was
# accepted as a valid goal id. \Z anchors at true end-of-string. Probed on both
# tables that share this regex, so a stray newline from a captured command
# substitution can no longer enter either as a well-formed id.
_GOAL_ID_RE = re.compile(r"^g-\d{1,4}-\d{2,4}[a-z]?(-[a-z])?\Z")


# DELIBERATELY namedtuple, NOT @dataclass. This module is loaded by
# `importlib.util.spec_from_file_location` + `exec_module` in the test tree
# (test_worker_execute.py's `_load`), which does NOT register the module in
# sys.modules. Under `from __future__ import annotations` every annotation is a
# string, and dataclasses resolves those via `sys.modules[cls.__module__]` to
# detect ClassVar/InitVar -- which is None under that loader, so `@dataclass`
# raises AttributeError at IMPORT and takes the whole file's collection with it.
# Measured while building this table: it broke test_worker_execute.py, a suite
# this change never touched. namedtuple needs no annotation machinery, and gives
# frozen + __eq__ + __repr__ for free.
_LifecycleFields = collections.namedtuple(
    "_LifecycleFields", "kind target why mode pending_goal")


class LifecycleDisposition(_LifecycleFields):
    """One declared worker disposition for one session-lifecycle stage.

    Fields:
      kind          one of DISPOSITION_KINDS
      target        component name (shared-component/scoped-call) or the anchor
      why           why THIS disposition and not another
      mode          REQUIRED iff kind == scoped-call; forbidden otherwise
      pending_goal  set when the disposition is DECLARED but NOT YET BUILT

    A GAP is UNREPRESENTABLE: every field is validated at construction, so a row
    with an unknown kind, an empty target, an empty rationale, or a scoped-call
    missing its mode raises ValueError when the module is imported. There is no
    sentinel for "undeclared" -- the only way to have no disposition is to have
    no row, which the completeness check refuses separately.

    `pending_goal` exists so the table can state the contract without asserting,
    in the indicative, that the code already honours it. An aspirational row
    written as a fact is worse than no row: it reads as evidence to everyone
    downstream and nothing ever re-checks it.
    """

    __slots__ = ()

    def __new__(cls, kind, target, why, mode=None, pending_goal=None):
        if kind not in DISPOSITION_KINDS:
            raise ValueError(
                f"unknown disposition kind {kind!r}; "
                f"must be one of {sorted(DISPOSITION_KINDS)}")
        if not (target or "").strip():
            raise ValueError(f"{kind}: target must be non-empty")
        if not (why or "").strip():
            raise ValueError(f"{kind}({target}): why must be non-empty")
        if kind == SCOPED_CALL and not (mode or "").strip():
            raise ValueError(
                f"scoped-call({target}): mode is REQUIRED -- a scoped call must "
                f"name the mode/flag INSIDE the shared component, else it is "
                f"indistinguishable from a transcription")
        if kind != SCOPED_CALL and mode is not None:
            raise ValueError(
                f"{kind}({target}): mode is meaningful only for scoped-call")
        # pending_goal was the ONE unvalidated field until fresh-eyes on this
        # goal's own diff (F-001) — it accepted 12345 and a list while the
        # docstring above claimed every field was validated. It is the field
        # whose entire purpose is honesty about what has NOT shipped, so an
        # unvalidated one is the worst of the five: junk here reads downstream
        # as a real tracker. Shape-check only; whether the goal EXISTS is not
        # checkable here (no store access at import).
        if pending_goal is not None:
            if not isinstance(pending_goal, str) or not _GOAL_ID_RE.match(pending_goal):
                raise ValueError(
                    f"{kind}({target}): pending_goal must be None or a goal id "
                    f"like 'g-306-211', got {pending_goal!r}")
        return super().__new__(cls, kind, target, why, mode, pending_goal)


# The canonical session-lifecycle stage list. Hand-maintained -- see "WHAT THIS
# TABLE CANNOT DO" above. `reducer-iteration` is the one stage not spelled out
# in the  filing: it is where the per-iteration reducer phases
# (verify / complete-review / state-update / evolution / learning-gate) live, and
# it is present so PHASE_LIFECYCLE_STAGE below can map EVERY phase to a stage
# rather than leaving five phases uncovered.
CANONICAL_LIFECYCLE_STAGES = (
    "prime",
    "boot-continuity",
    "select",
    "claim",
    "execute",
    "spark-capture",
    "verify-own-unit",
    "heartbeat-liveness",
    "compact-checkpoint",
    "compact-restore",
    "stop-hook-gates",
    "close-staging",
    "consolidate-merge",
    "replay",
    "reducer-iteration",
    "productivity-stop",
)

LIFECYCLE_DISPOSITIONS = {
    "prime": LifecycleDisposition(
        kind=SCOPED_CALL, target="prime",
        mode="light, TWO TIERS: identity (Self + Program) once per worker session behind "
             "the light-prime-done sentinel; recency (rb --recent + guardrail index) on "
             "EVERY re-entry, ahead of that sentinel",
        why="A worker reasons with the same identity and rails as its reducer, so it needs "
            "prime's INPUT side; it must not run prime's reducer-side state writes. The "
            "per-unit recency tier exists because a session-scoped prime alone leaves every "
            "unit after the first on entry-time rails -- measured at ~18.5h on cc-07 "
            "2026-08-16, against a user directive (g-306-211 addendum) whose rationale is "
            "unit gaps of 15-92 min. Landed by g-306-298; worker-loop/SKILL.md Phase -0.5 "
            "is the implementation."),
    "boot-continuity": LifecycleDisposition(
        kind=WORKER_ONLY, target="worker-loop Phase -0 body-identity re-verify",
        why="/boot is the reducer's session entry and touches running-session-id and "
            "persona state, which a worker must never claim. The worker's continuity is "
            "its own re-entry check: Phase -0 re-verifies the forked body-WM signal on "
            "every pass (guard-517/guard-463 role-gated re-entry)."),
    "select": LifecycleDisposition(
        kind=SHARED_COMPONENT, target="goal-selector.sh",
        why="A worker selects exactly like the reducer -- same scorer, same candidate set. "
            "There is no worker-specific selection logic and there must not be one."),
    "claim": LifecycleDisposition(
        kind=SHARED_COMPONENT, target="aspirations-claim.sh",
        why="claimed_by stays the mindKey/agent-name, so the claim contract is identical. "
            "claimed_by_sid (stamped by aspirations-claim.sh, g-115-3176) is what "
            "distinguishes the Bodies, and it is written by the shared script, not by a "
            "worker-side variant."),
    "execute": LifecycleDisposition(
        kind=SCOPED_CALL, target="aspirations-execute", mode="Phases 3.9-4.5 only",
        why="The worker DOES the work through the existing execute protocol, entered via "
            "load-execute-protocol.sh. The phase window is the scope; the protocol text is "
            "not copied into the worker loop."),
    "spark-capture": LifecycleDisposition(
        kind=WORKER_ONLY, target="spark_capture WM slot (worker-loop Phase 3.5)",
        why="Only the executing Body holds the in-context experience the spark handlers "
            "need, so the observation cannot be reconstructed later -- but a worker that "
            "CREATES an rb/guardrail/tree artifact is an Nth reducer. Capture is therefore "
            "worker-only and the handlers stay reducer-only; the reducer replays the slot "
            "through aspirations-spark Phase 6.5 after generalize-down (g-306-176)."),
    "heartbeat-liveness": LifecycleDisposition(
        kind=WORKER_ONLY, target="worker_reducer_liveness.py (worker-loop Phase 0.5)",
        why="Inverted from the reducer's: heartbeat-tick.sh writes the agent-wide "
            "runner-heartbeat + team-state last_active, which a worker must not touch. The "
            "worker instead POLLS its reducer and winds down when it is gone. NEVER-PROMOTE "
            "-- no poll result yields 'become the reducer' (g-306-125/g-306-208)."),
    "compact-checkpoint": LifecycleDisposition(
        kind=SHARED_COMPONENT, target="precompact-checkpoint.py",
        why="Same writer for both roles; the per-Body split lives INSIDE the component, at "
            "_paths.body_state_path, which routes to sessions/<sid>/ when the forked "
            "body-WM exists and falls back to session/ otherwise (g-306-136). A worker-side "
            "copy of the checkpoint logic is exactly what the no-transcription rule forbids."),
    "compact-restore": LifecycleDisposition(
        kind=SHARED_COMPONENT, target="compact-restore-slots.py",
        why="Reader twin of the writer above, through the same body_state_path rail. The "
            "g-306-174 surprise was a reader that rejected body-keyed checkpoints its "
            "writer produced -- a writer/reader asymmetry inside ONE stage, which is why "
            "checkpoint and restore are declared as separate rows rather than one."),
    "stop-hook-gates": LifecycleDisposition(
        kind=SHARED_COMPONENT, target="stop-hook.sh (Gate 0 + the per-Body branch above it)",
        why="One hook serves both roles and self-routes, so the per-Body split lives INSIDE "
            "the component exactly as compact-checkpoint's does. What makes this stage a trap "
            "is that Gate 0 is runner-KEYED: it answers 'is this the reducer?', and a worker "
            "is a no-runner box BY DEFINITION -- so anything nested inside Gate 0's branches "
            "is unreachable for the very role it was written for. That is why the per-Body "
            "branch must sit ABOVE Gate 0's early exits, not inside one. It sat inside the "
            "sid-mismatch branch until g-306-214, so a cross-box worker with no local "
            "running-session-id got NEITHER the resurrection net nor the close producer; the "
            "measured soak-#2 trace is in stop-hook.sh's branch comment, kept there rather "
            "than restated here. A worker-side stop hook is what no-transcription forbids."),
    "close-staging": LifecycleDisposition(
        kind=WORKER_ONLY, target="body-closing sentinel + stop-hook Phase 2B staging",
        why="The reducer closes through /stop and aspirations-graceful-stop. A worker has no "
            "stop obligations to discharge -- it stages its divergent WM for the reducer and "
            "stops. The sentinel is written ONLY on a genuine close (SELECT found no work, or "
            "reducer-liveness wind-down), never at end of a work unit (g-306-70)."),
    "consolidate-merge": LifecycleDisposition(
        kind=REDUCER_ONLY_BY_DESIGN, target="aspirations-consolidate Step -1 (body-merge.py)",
        why="Generalize-down is the definition of the reducer role: one Body merges ALL "
            "Bodies' divergent state. A worker running it would be a second reducer, which is "
            "the invariant the whole convergence forbids."),
    "replay": LifecycleDisposition(
        kind=REDUCER_ONLY_BY_DESIGN, target="Worker Spark Replay block (aspirations-spark Phase 6.5)",
        why="Replay is the CONSUMING half of spark-capture and runs after body-merge, so it "
            "requires merged state that only the reducer holds. It reuses the existing Phase "
            "6.5 handlers rather than a worker-side encoder -- the capture/replay split is "
            "the no-transcription rule applied to learning."),
    "verify-own-unit": LifecycleDisposition(
        kind=SCOPED_CALL,
        # DECLARED, NOT YET WIRED -- and this field is the only honest way to say
        # so. The phase and its disposition exist and are machine-checkable
        # (`should-run-phase verify-own-unit` exits 0), but worker-loop Phase 4a
        # does NOT yet invoke the verify skill.
        #
        # THE PREREQUISITE IS NOW MET (2026-09-03,  part1a): the scope
        # mode this row names EXISTS -- /aspirations-verify takes a `scope` input
        # of "full" (default) or "own-unit", documented in its Inputs section.
        # Until part1a it did not, and a worker told to invoke an UNSCOPED verify
        # would have run the reducer-side cross-Body parts (streak tracking) --
        # the Nth-reducer defect. What remains is only the worker-loop Phase 4a
        # invocation, which is kept a SEPARATE increment because it changes the
        # live loop under every worker Body in the fleet at once.
        #
        # `pending_goal` therefore STAYS until that invocation lands: this row
        # states the contract without asserting the code honours it (see the
        # LifecycleDisposition docstring: "an aspirational row written as a fact
        # is worse than no row"). test_worker_lifecycle_contract.py pins the
        # marker by name and goal id and is written to FAIL when the wiring
        # lands, so whoever wires it removes both in the same change.
        pending_goal="g-306-417",
        target="aspirations-verify",
        mode="own-unit only: the hypothesis outcome, Q1/Q2/Q3 escalation and "
             "blocked_by-clears for the ONE goal this Body just executed "
             "(worker-loop Phase 4a, before the mechanical close). Cross-Body "
             "streaks and the sampled review of these closures stay reducer-side.",
        why="A worker verifies the ONE unit it executed and nothing else: that unit's "
            "hypothesis outcome, its Q1/Q2/Q3 escalation, and the unblocking of goals "
            "whose blocked_by named it. SCOPED_CALL and not WORKER_ONLY is the whole "
            "design -- per guard-1867 the worker INVOKES the existing verify skill "
            "rather than transcribing its steps into worker-loop, so there is one "
            "implementation and it cannot drift. The reducer keeps the residue "
            "(its own units, cross-Body streaks, and the SAMPLED review of these "
            "self-graded closures) under reducer-iteration below. Two rails bound "
            "what this may assert: guard-4638 -- closing is not landing, so it must "
            "never claim the code reached main; and guard-3034 -- an outside-world "
            "reading (PR mergeable, CI green, a live endpoint) is a TIMESTAMPED "
            "observation, not a settled fact, so it is recorded with its instant "
            "and the reducer re-reads it rather than trusting it."),
    "reducer-iteration": LifecycleDisposition(
        kind=REDUCER_ONLY_BY_DESIGN,
        target="verify (residue) / complete-review / state-update / evolution / learning-gate",
        why="The per-iteration encode+reflect block. Applied ONCE to the MERGED state of all "
            "Bodies at generalize-down, not per-Body. This stage is the lifecycle twin of "
            "REDUCER_ONLY_PHASES minus spark and productivity-check, which have their own "
            "rows because their worker dispositions differ. The STATUS FLIP of the worker's "
            "own goal is not this stage: worker-loop Phase 4a records the caller-declared "
            "outcome through the shared close writer (iteration-close.sh --phase verify, "
            "the mechanical do_verify), because a completion left at in-progress reaches no "
            "reducer lane and no selector -- see the REDUCER_ONLY_PHASES 'verify' note."),
    "productivity-stop": LifecycleDisposition(
        kind=REDUCER_ONLY_BY_DESIGN, target="productivity-stop-gate.sh",
        why="A worker's close condition is work-exhaustion or reducer-death, never a "
            "productivity score -- and productivity-stop-gate is one of the few authorized "
            "writers of stop-requested, an agent-wide singleton a worker must not touch."),
}

# Couples the lifecycle table to the PHASE table above. Every phase the live gate
# knows about MUST name the lifecycle stage it belongs to, so adding a phase
# without declaring its lifecycle disposition fails at import. This is the half of
# the contract with real teeth -- see "WHAT THIS TABLE CANNOT DO".
PHASE_LIFECYCLE_STAGE = {
    "select": "select",
    "claim": "claim",
    "execute": "execute",
    "verify-own-unit": "verify-own-unit",
    "spark": "spark-capture",
    "verify": "reducer-iteration",
    "complete-review": "reducer-iteration",
    "state-update": "reducer-iteration",
    "evolution": "reducer-iteration",
    "learning-gate": "reducer-iteration",
    "productivity-check": "productivity-stop",
}


# --------------------------- skill eligibility () ---------------------------
#
# WHY A FOURTH TABLE, AND WHY THE ORIGINATING GOAL'S PREMISE WAS WRONG.
#  was filed asserting "the disposition table already knows which
# skills are reducer-only, so the missing piece is a lookup, not a new list."
# MEASURED while implementing it: FALSE. The tables above are keyed by PHASE and
# by LIFECYCLE STAGE. A goal record's `skill` field is a THIRD vocabulary
# ("/replay --sharp-wave"), and nothing anywhere bridged it to the other two. So
# the lookup had no table to look in. This is that bridge -- and it is a bridge,
# not a second list of reducer-only things: the DISPOSITION is still read from
# LIFECYCLE_DISPOSITIONS, so flipping a stage's kind moves every skill mapped to
# it automatically and no reducer-only fact is stated twice (guard-2676).
#
# MEASURED 2026-08-10 (alpha worker Body, hostname cc-08, uname -r
# 6.8.0-136-generic). goal-selector.sh offered  "Run hippocampal replay"
# as the sanctioned top pick, with the drain-lane banner reading verbatim "This
# IS the sanctioned top pick — claim it without a deviation code." That goal
# carries skill "/replay --sharp-wave"; LIFECYCLE_DISPOSITIONS["replay"] is
# REDUCER_ONLY_BY_DESIGN; /replay's body calls guardrails-add.sh. A worker that
# followed the banner would have written guardrails derived from its own
# UNMERGED state -- the Nth-reducer defect the whole convergence forbids -- and
# nothing in the loop prompted the check. It was caught only by opening the
# skill before claiming.
#
# WHERE THIS DELIBERATELY DOES *NOT* LIVE. The obvious fix is to filter inside
# goal-selector when the caller is a worker. LIFECYCLE_DISPOSITIONS["select"]
# forbids exactly that, in as many words: "A worker selects exactly like the
# reducer -- same scorer, same candidate set. There is no worker-specific
# selection logic and there must not be one." So the scorer stays untouched and
# byte-identical for both roles, and the eligibility question is answered HERE,
# by the module that already owns the role split, and consulted by worker-loop
# Phase 1. The consulting loop walks DOWN the ranked list rather than burning a
# cycle, so a refusal costs one lookup and never a select pass.

# skill -> the lifecycle stage that skill IS. Disposition is derived, never
# restated. Keys are the bare skill token; args ("--sharp-wave") are stripped by
# normalize_skill before lookup.
SKILL_LIFECYCLE_STAGE = {
    "/replay": "replay",
    # The reducer-iteration row's own `why` calls itself "the per-iteration
    # encode+reflect block", so reflect belongs to it by that row's own words
    # even though the target string lists only the five aspirations-* phases.
    "/reflect": "reducer-iteration",
    # Encodes to FOUR stores (guardrails-add.sh, reasoning-bank-add.sh,
    # tree-update.sh, meta-set.sh -- measured by grep, not inferred). It is the
    # heaviest encoder reachable from a goal's skill field.
    "/review-hypotheses": "reducer-iteration",
    "/aspirations-verify": "reducer-iteration",
    "/aspirations-complete-review": "reducer-iteration",
    "/aspirations-state-update": "reducer-iteration",
    "/aspirations-evolve": "reducer-iteration",
    "/aspirations-learning-gate": "reducer-iteration",
    "/aspirations-consolidate": "consolidate-merge",
    # Added 2026-08-11 (alpha, hostname cc-08, uname -r 6.8.0-137-generic) by
    # the route this table documents: a worker met the fail-open, judged it by
    # hand, and wrote the answer down so the next one does not have to.
    #  ("drain 15 accumulated temp/ working docs ... + purge 7 stale
    # ephemera") was offered at rank 2 of 960 carrying starvation_boost 4.00,
    # skill field EMPTY -- so skill_eligibility returned the "maps to no
    # lifecycle stage" green and proved nothing. The skill is named only in the
    # goal DESCRIPTION ("Invoke /drain-temp"), which no bridge reads.
    # Its own front matter settles it: it "encodes its reusable value into the
    # right store (knowledge tree / reasoning bank / guardrails / experience)",
    # i.e. three of the four stores /review-hypotheses is refused for, and it is
    # invoked from aspirations-precheck -- a phase workers skip outright. The
    # material it encodes is the Body's OWN accumulated working notes, which is
    # the unmerged-state half of the Nth-reducer defect rather than the
    # /tree-style "content supplied in the goal" carve-out below.
    "/drain-temp": "reducer-iteration",
}

# ── DO NOT POPULATE A GOAL'S `skill` FIELD JUST TO WIN THIS FENCE () ──
#
# The remedy this table advertises -- "add the skill to SKILL_LIFECYCLE_STAGE" --
# is safe ONLY for a goal that ALREADY carries the right skill. The tempting next
# step, for the 919-of-938 goals that carry NONE, is to fill the field in so the
# bridge can finally answer. That is NOT a metadata edit and it is not free:
#
#   `skill` IS THE EXECUTOR'S DISPATCH KEY, not an inert tag.
#   .claude/skills/aspirations-execute/SKILL.md: `result = invoke goal.skill
#   with goal.args`. The "Misroute guard" 27 lines above it SETS goal.skill
#   precisely to redirect dispatch, which is independent corroboration.
#
# So stamping a skill onto a skill-less goal changes WHAT THAT GOAL RUNS. On the
#  recurring cognitive goals this was proposed for, the cadences are 2.67h
# / 40.5h / 48h, and the nearest skills are all WIDER than the goal: 
# ("Generate hypotheses from recent work") names the sq-009 formation protocol,
# but /aspirations-spark documents no sq-009-only entry point, so the stamp would
# run the ENTIRE spark phase every 2.67h to buy a refusal. That is guard-4618 in
# its purest form -- a change that makes the detector go green by altering the
# thing being detected -- and it is strictly worse than the miss it fixes.
#
# Measured 2026-08-24 (alpha worker Body, cc-07) while executing ,
# which PROPOSED exactly this. Also measured there: of 's five skill-less
# recurring goals only THREE are reducer-only at all ( archival sweep and
#  shared-storage domain refresh are legitimate worker work), and
# `intended_agent`
# cannot substitute -- its values are agent NAMES and goal-schemas.md calls it
# advisory, "without restricting access".
#
# The real fix is a goal-level marker with NO dispatch semantics, owned by
# . Route skill-less reducer-only goals there; do not stamp `skill`.

# THE PINNED NEGATIVES, and they are the load-bearing half of this table
# (guard-2860: "the test proving the carve-out works cannot fail in the
# dangerous direction; the load-bearing ones are the exclusions").
#
# The tempting rule is "a skill that calls an encoding script is reducer-only".
# That rule is WRONG and would strand real worker work. Both rows below call
# encoding scripts or sit near the reducer's vocabulary, and both are legitimate
# worker work:
#
#   /tree                     calls tree-update.sh, and a worker uses it for
#                             GOAL-DIRECTED artifact creation from content
#                             supplied in the goal. Measured the same session:
#                              encoded a principal directive carried
#                             in full in the goal record. The forbidden thing is
#                             LOOP-PHASE encoding over the Body's OWN unmerged
#                             experience -- a different act that happens to share
#                             a script.
#   /agent-completion-report  zero encoding calls, user-invocable: true,
#                             minimum_mode: reader. The reducer-only phase is
#                             complete-review, whose skill is
#                             /aspirations-complete-review -- a DIFFERENT skill.
#                             Refusing this one on name-similarity is the exact
#                             sloppiness this set exists to prevent.
#
# Membership here is asserted DISJOINT from SKILL_LIFECYCLE_STAGE at import, so
# a future edit cannot quietly add a skill to both and have the refusal silently
# win. Prose would not have survived that edit; this does.
SKILL_ELIGIBLE_DESPITE_ENCODING = frozenset({
    "/tree",
    "/agent-completion-report",
})

_SkillEligibilityFields = collections.namedtuple(
    "_SkillEligibilityFields", "eligible skill stage disposition reason")


def normalize_skill(skill: "str | None") -> "str | None":
    """The bare skill token from a goal's `skill` field, or None.

    Goal records carry the invocation, not the name: "/replay --sharp-wave",
    "/review-hypotheses --resolve". Lookup is on the first whitespace-delimited
    token. A leading slash is added when absent so "replay" and "/replay" agree
    -- goal records have been observed carrying both shapes, and a bridge that
    matched only one would refuse half the population it was written for.
    """
    parts = str(skill or "").split()
    if not parts:
        return None
    token = parts[0]
    return token if token.startswith("/") else "/" + token


def skill_eligibility(skill: "str | None") -> _SkillEligibilityFields:
    """Whether a WORKER Body may claim a goal carrying `skill`.

    UNKNOWN SKILLS ARE ELIGIBLE, and the fail direction is deliberate. 919 of
    938 live candidates carry no skill at all (measured cc-08 2026-08-10), so a
    fail-closed default would refuse a worker essentially everything and strand
    the role outright. The refusal set is therefore a POSITIVE list whose
    cardinality is a property of THIS CODE and not of whatever happens to be in
    the queue (guard-2860's test, applied in its tightening mirror).

    The residual risk that leaves is real and is named rather than papered over:
    a reducer-only skill nobody added to the bridge reads as eligible. That is
    exactly the status quo this change improves on -- it is not made worse by
    the default -- and it is why worker-loop Phase 1 still instructs a human-eyes
    check rather than treating a green here as proof.
    """
    norm = normalize_skill(skill)
    if norm is None:
        return _SkillEligibilityFields(
            True, None, None, None,
            "NOT EVALUATED -- the goal names no skill, and this bridge is "
            "SKILL-keyed, so it CANNOT ANSWER whether the GOAL is reducer-only. "
            "This is NOT a cleared check. Eligibility stays True because "
            "fail-open is deliberate (919 of 938 live candidates carry no "
            "skill; fail-closed would strand the worker role) -- so the "
            "decision is YOURS, not this bridge's. Before claiming, read the "
            "goal's verification outcomes and description -- with "
            "`bash core/scripts/aspirations-read.sh --source <world|agent> "
            "--id <asp-id>` (prints the whole aspiration; find the goal by id; "
            "there is NO per-goal reader script and NO direct JSONL read): "
            "if the work ENCODES "
            "to the tree / reasoning bank / guardrails, RESOLVES a hypothesis, "
            "drains a capture lane, consumes worker refs, pushes main, or "
            "writes the agent-wide working-memory.yaml, it is REDUCER-ONLY -- "
            "release it and take the next candidate. "
            "(g-115-6523; guard-1760 class: a checker must not report what it "
            "declined to look at as a pass.)")
    if norm in SKILL_ELIGIBLE_DESPITE_ENCODING:
        return _SkillEligibilityFields(
            True, norm, None, None,
            f"{norm} is a PINNED worker-eligible skill: it touches encoding "
            f"scripts or reducer-adjacent naming but is goal-directed work, not "
            f"a reducer lifecycle stage")
    stage = SKILL_LIFECYCLE_STAGE.get(norm)
    if stage is None:
        return _SkillEligibilityFields(
            True, norm, None, None,
            f"{norm} maps to no lifecycle stage -- eligible by default (the "
            f"bridge is a positive list of reducer-only skills; see "
            f"skill_eligibility.__doc__ for why unknown is not fail-closed)")
    disp = LIFECYCLE_DISPOSITIONS[stage]
    if disp.kind == REDUCER_ONLY_BY_DESIGN:
        return _SkillEligibilityFields(
            False, norm, stage, disp.kind,
            f"{norm} IS lifecycle stage {stage!r}, declared "
            f"{REDUCER_ONLY_BY_DESIGN} ({disp.target}). A worker running it "
            f"would encode from its own unmerged state -- the Nth-reducer "
            f"defect. Leave the goal for the reducer and take the next "
            f"candidate.")
    return _SkillEligibilityFields(
        True, norm, stage, disp.kind,
        f"{norm} IS lifecycle stage {stage!r}, declared {disp.kind} -- "
        f"worker-eligible")



# ── GOAL-LEVEL ROLE DECLARATION () ──────────────────────────────────
#
# `skill_eligibility` above is SKILL-keyed, and 1,411 of 1,447 live candidates
# carry no skill at all (97.5%, measured cc-09 2026-09-03; 919/938 = 98.0% when
# the defect was relayed from cc-07 2026-08-23). So for ~98% of the queue that
# bridge structurally CANNOT answer the question a worker actually has -- is
# THIS GOAL reducer-only? -- and says so honestly rather than reporting a pass.
# This field closes that gap from the GOAL side.
#
# THREE DESIGN CONSTRAINTS, each measured before this was written. Do not
# "simplify" past any of them:
#
# 1. ROLE-VALUED, NOT BOOLEAN. The class is BIDIRECTIONAL: of the 3 genuinely
#    role-unsatisfiable defers measured fleet-wide (zeta, cc-02, 2026-08-24,
#    over 2860 goals / 2219 non-terminal / 154 deferred), TWO need a WORKER
#    ( needs a Body reading its own box-local WM;  needs a
#    worker on a named box) and only ONE needs the reducer. A boolean
#    `reducer_only` cannot express the majority direction, and once the bridge
#    reads a boolean, widening it becomes a migration instead of one extra word.
#
# 2. NO DISPATCH SEMANTICS. This is why the field is new rather than a reuse of
#    `skill`. `skill` IS the executor's dispatch key
#    (aspirations-execute: `result = invoke goal.skill with goal.args`), so
#    stamping a skill on a skill-less goal to win this fence changes WHAT THE
#    GOAL RUNS -- on  it would run the entire spark phase every 2.67h to
#    buy a refusal (guard-4618 / guard-4978 / rb-9926, and the warning already
#    sits on SKILL_LIFECYCLE_STAGE above). This field is read HERE and nowhere
#    that dispatches.
#
# 3. `intended_agent` CANNOT SUBSTITUTE. Its values are agent NAMES
#    (alpha/bravo/either/null), not roles, and goal-schemas.md calls it advisory
#    -- "the goal-selector may use it to bias scoring WITHOUT RESTRICTING
#    ACCESS". A biasing hint cannot fence anything.
#
# THE SELECTOR STAYS ROLE-BLIND. Nothing here is called from goal-selector:
# LIFECYCLE_DISPOSITIONS["select"] and guard-2783 both forbid role-conditional
# logic in a component BOTH roles run, so the scorer remains byte-identical for
# a worker and a reducer. This is a CLAIM-time fence, not a scoring one -- which
# is also why the drain lane can still promote a reducer-only goal to top pick
# (measured three times on ): the lane moves ORDER, this moves the
# claim decision, and the worker is expected to consult this and skip.
#
# KNOWN LIMIT, stated rather than papered over: an UNSET field is
# indistinguishable from "not reducer-only", so this improves the FUTURE corpus
# and leaves the existing one exactly as it was. That is an argument for a
# back-fill plan, not for skipping the field (and it is why the unset branch
# below preserves the old behaviour byte-for-byte instead of tightening).

EXECUTABLE_BY_ROLE_VALUES = ("worker", "reducer", "any")


def goal_eligibility(skill: "str | None",
                     executable_by_role: "str | None" = None
                     ) -> _SkillEligibilityFields:
    """Whether a WORKER Body may claim a goal, GOAL-level declaration first.

    Reads `executable_by_role` when it carries a recognised value and falls back
    to the skill-keyed bridge otherwise. Never raises on a bad value: an
    unrecognised role degrades to the skill bridge and NAMES itself in the
    reason, because a typo must not silently fence a goal in either direction.
    """
    role = (executable_by_role or "").strip().lower() or None

    if role == "reducer":
        return _SkillEligibilityFields(
            False, normalize_skill(skill), None, REDUCER_ONLY_BY_DESIGN,
            "goal declares executable_by_role='reducer' -- a WORKER must not "
            "claim it. This is a GOAL-level declaration, decisive and "
            "independent of the skill field. Leave it for the reducer and take "
            "the next candidate. (g-115-7372)")

    verdict = skill_eligibility(skill)

    if role == "worker":
        # A GOAL-level 'worker' declaration must NOT unlock a skill the skill
        # fence refuses. That combination is a CONTRADICTION, not an override:
        # the skill fence is structural (running /reflect on a worker encodes
        # from unmerged state -- the Nth-reducer defect the convergence forbids)
        # while this field is filer-supplied metadata, and metadata must never
        # relax a structural ownership predicate (guard-2860's direction). It
        # should not arise legitimately either -- the measured worker-needing
        # cases (box-local WM, a named box) are all skill-LESS -- so when it
        # does arise it is a mis-filing, and the useful behaviour is to refuse
        # AND say both halves out loud so the record gets corrected.
        if not verdict.eligible:
            return verdict._replace(reason=(
                "CONTRADICTION: the goal declares executable_by_role='worker' "
                "but its skill is refused by the skill fence, so the "
                "declaration is IGNORED and the goal is treated as "
                "reducer-only. Metadata does not relax a structural fence. "
                "Fix the goal record rather than the fence. Skill verdict: "
                + verdict.reason))
        return _SkillEligibilityFields(
            True, normalize_skill(skill), verdict.stage, verdict.disposition,
            "goal declares executable_by_role='worker' -- worker-eligible, and "
            "positively so: this value also ROUTES a goal that only a Body with "
            "box-local state can satisfy, which is the majority direction of "
            "the measured role-unsatisfiable class. (g-115-7372)")

    if role == "any":
        return verdict._replace(
            reason="goal declares executable_by_role='any' (explicitly not "
                   "role-fenced), so the SKILL bridge decides: " + verdict.reason)

    if role is not None:
        return verdict._replace(
            reason=(f"goal carries an UNRECOGNISED executable_by_role="
                    f"{executable_by_role!r} (expected one of "
                    f"{'/'.join(EXECUTABLE_BY_ROLE_VALUES)}) -- IGNORED, and "
                    f"this is NOT a cleared check. Falling back to the SKILL "
                    f"bridge: ") + verdict.reason)

    return verdict._replace(
        reason="goal carries no executable_by_role declaration (the common "
               "case -- the field is new, so the existing corpus is unset and "
               "unset is NOT evidence of anything). Falling back to the SKILL "
               "bridge: " + verdict.reason)


# --------------------------- carrier contract () ---------------------------
#
# WHY A THIRD TABLE
# The two tables above answer "which PHASES does a worker run" and "what is each
# LIFECYCLE STAGE's worker disposition". Neither answers the question that
# actually strands work: **when a worker produces output, what carries it to the
# reducer?** That question has no SSOT, and the gap is invisible from both
# existing tables because it is indexed by neither phase nor stage.
#
# MEASURED (, filed by alpha REDUCER on cc-04, 2026-08-08). 
# carries a WORKER outcome_note (alpha, cc-07, 2026-08-07T18:35) reporting its
# fix COMPLETE: 3 SKILL.md edits, a new verify-learning check, a 4-case mutation
# proof. ZERO of those artifacts exist on cc-04 -- absent, not merely unmerged.
# The worker's WM reached the reducer (body-merge.py), its spark observations
# reached the reducer (spark_capture), and its FILE EDITS reached nothing.
#
# NOTE THE CONTRACT ALREADY SAID SO, in the indicative, and nobody could see it:
# LIFECYCLE_DISPOSITIONS["close-staging"] reads "it stages its divergent WM for
# the reducer and stops". WM. A framework file edit is divergent state that is
# not WM, so it was outside the declared carrier all along -- correctly
# described and structurally unenforced. That is precisely the shape
# LIFECYCLE_DISPOSITIONS was built to make impossible for stages, applied to
# output classes instead.
#
# WHAT THIS TABLE CANNOT DO -- the same residual gap the lifecycle table names,
# for the same reason: CANONICAL_OUTPUT_CLASSES lives in THIS file, so the
# completeness check cannot detect an output class a worker produces that was
# never added to the canonical list. Adding a class is a judgment call made
# here; the check only enforces that every DECLARED class has a carrier
# (guard-2582 -- a static checker's silence about your file is not coverage
# until you confirm the file is in its population).

STAGED_ARTIFACT = "staged-artifact"      # bytes the reducer drains (body-merge)
WM_SLOT = "wm-slot"                      # merged by per-slot policy
SHARED_STORE = "shared-store"            # own-cloud-synced world/ or meta/
NO_CARRIER = "no-carrier"                # DECLARED as unreachable -- see below

GIT_REF = "git-ref"                      # per-Body namespaced ref, pushed + consumed
UPSTREAM_REMOTE = "upstream-remote"      # a repo's OWN origin, pushed per post-execution Step 2
IN_PLACE_AT_DESTINATION = "in-place-at-destination"  # applied ON the target system; nothing to transport
CARRIER_KINDS = frozenset({
    STAGED_ARTIFACT, WM_SLOT, SHARED_STORE, GIT_REF, UPSTREAM_REMOTE,
    IN_PLACE_AT_DESTINATION, NO_CARRIER,
})

_CarrierFields = collections.namedtuple(
    "_CarrierFields", "kind target why pending_goal")


class CarrierDisposition(_CarrierFields):
    """How ONE class of worker output reaches the reducer -- or that it cannot.

    Fields:
      kind          one of CARRIER_KINDS
      target        the concrete mechanism (script, slot, path), never a plan
      why           why THIS carrier and not another
      pending_goal  REQUIRED iff kind == no-carrier; forbidden otherwise

    NO_CARRIER is the load-bearing kind and the reason this table is worth
    having. It does not mean "unknown" -- it is an ASSERTION that this output
    class currently reaches nothing, and it must name the goal tracking the
    carrier. That inverts the failure: a worker output class with no carrier is
    now a DECLARED fact a reader can act on, instead of an absence nobody can
    see. Silence is what stranded g-115-5147.

    Mirrors LifecycleDisposition deliberately -- same namedtuple-not-dataclass
    constraint (see that class for the importlib/`from __future__` reason), same
    construction-time validation, so a GAP IS UNREPRESENTABLE: there is no
    sentinel for "undeclared", and the only way to have no disposition is to
    have no row, which carrier_gaps() refuses separately.
    """

    __slots__ = ()

    def __new__(cls, kind, target, why, pending_goal=None):
        # isinstance BEFORE the membership test, same reason as pending_goal
        # below (guard-3075): `kind not in CARRIER_KINDS` is a hash lookup, so
        # an unhashable kind (list, dict) raises TypeError -- not the ValueError
        # this class documents for every field. Found by fresh-eyes probing THIS
        # constructor one field over from the ordering bug it had just fixed:
        # the same defect class, in the same method, twice.
        if not isinstance(kind, str) or kind not in CARRIER_KINDS:
            raise ValueError(
                f"unknown carrier kind {kind!r}; "
                f"must be one of {sorted(CARRIER_KINDS)}")
        if not (target or "").strip():
            raise ValueError(f"{kind}: target must be non-empty")
        if not (why or "").strip():
            raise ValueError(f"{kind}({target}): why must be non-empty")
        # SHAPE FIRST, then requiredness. Ordering is load-bearing, not style:
        # the requiredness checks below use `(pending_goal or "").strip()`, which
        # raises AttributeError -- NOT ValueError -- on a non-string. The class
        # docstring promises every field raises ValueError, so a caller catching
        # the documented contract would miss an int and let junk into the table.
        # Caught by probing this constructor branch-by-branch rather than
        # asserting it worked; it is the same defect LifecycleDisposition's
        # pending_goal carried until fresh-eyes (see that class), reproduced one
        # table over, which is why the order is spelled out here.
        if pending_goal is not None and (
                not isinstance(pending_goal, str)
                or not _GOAL_ID_RE.match(pending_goal)):
            raise ValueError(
                f"{kind}({target}): pending_goal must be a goal id like "
                f"'g-306-263', got {pending_goal!r}")
        if kind == NO_CARRIER and not (pending_goal or "").strip():
            raise ValueError(
                f"no-carrier({target}): pending_goal is REQUIRED -- declaring an "
                f"output class unreachable without naming the goal that fixes it "
                f"is how the g-306-263 defect stayed invisible")
        if kind != NO_CARRIER and pending_goal is not None:
            raise ValueError(
                f"{kind}({target}): pending_goal is meaningful only for no-carrier; "
                f"a carrier that exists is not pending")
        return super().__new__(cls, kind, target, why, pending_goal)


# The output classes a worker Body can produce. Hand-maintained -- see "WHAT
# THIS TABLE CANNOT DO" above.
CANONICAL_OUTPUT_CLASSES = (
    "working-memory",
    "spark-observation",
    "goal-record",
    "shared-store-file",
    "framework-file-edit",
    "local-git-commit",
    "product-repo-commit",
    "remote-host-config-edit",
)

# POINTER DISCIPLINE for pending_goal: it names the goal that BUILDS the
# carrier, never the goal that FOUND the gap. Those are different goals and the
# finder closes first --  declared this table and closed the same day,
# so pointing a row at it would have left a dangling reference in the one field
# whose entire job is telling a reader who to chase. A no-carrier row is a
# promise that someone owns the fix; a pointer to a completed goal quietly
# converts that promise into a dead end that still reads as tracked.
OUTPUT_CLASS_CARRIERS = {
    "working-memory": CarrierDisposition(
        kind=STAGED_ARTIFACT, target="body-merge.py generalize_down (staged body WM + baseline)",
        why="The Body's forked WM is staged at close and drained by the reducer's "
            "aspirations-consolidate Step -1 under per-slot merge policies. This is the "
            "one carrier that has always worked, and it is why WM-shaped output was never "
            "the thing that stranded."),
    "spark-observation": CarrierDisposition(
        kind=WM_SLOT, target="spark_capture WM slot (worker-loop Phase 3.5)",
        why="Rides the working-memory carrier above, then the reducer replays it through "
            "the real handlers at aspirations-spark Phase 6.5. Declared separately because "
            "it is the case that proves the pattern: an output class a worker cannot "
            "process itself still reaches the reducer, because someone built it a carrier."),
    "goal-record": CarrierDisposition(
        kind=SHARED_STORE, target="world/aspirations.jsonl via the daemon claim/update endpoints",
        why="Goal state is written through the daemon to the own-cloud-synced world store, "
            "so a worker's claim, status change and outcome_note are visible fleet-wide "
            "without any Body-to-Body transfer."),
    "shared-store-file": CarrierDisposition(
        kind=SHARED_STORE, target="world/ and meta/ (own-cloud synced)",
        why="Both roots are backed by the storage backend rather than git, so a worker's "
            "writes there are authoritative for every Body immediately. This is exactly "
            "the property core/ and .claude/ do NOT have."),
    "framework-file-edit": CarrierDisposition(
        kind=GIT_REF, target="refs/workers/<agent>/<sid> — pushed by iteration-push.sh "
                             "--push-worker-ref, consumed by worker-ref-consume.sh",
        why="WAS measured unreachable (g-306-263) and is now carried (g-306-264). world/ and "
            "meta/ are backend-synced; core/ and .claude/ are the git repo, which the backend "
            "does not carry — so the carrier had to be git itself. The worker pushes HEAD to a "
            "ref namespaced by its own sid; the reducer fetches refs/workers/ and merges "
            "explicitly. --no-push stays the default and the shared branch stays the reducer's "
            "alone: that rule's rationale is contention on shared store files, and a ref whose "
            "path contains the sid has exactly ONE writer by construction, so the rationale "
            "does not reach it. All three of the design's blocking unknowns were measured on a "
            "real worker box rather than inferred (cc-07, 2026-08-08): the box authenticates to "
            "the Mind remote over SSH; a real push to refs/workers/* is accepted and branch "
            "protection does not reach it; and no consumer existed, which is why one shipped in "
            "the same change. Consumption is REPORT-ONLY by default — this goal rejected the "
            "patch-slot carrier because a framework change applying to drifted context is worse "
            "than one that is lost, and an auto-merge would re-open that door."),
    "local-git-commit": CarrierDisposition(
        kind=GIT_REF, target="refs/workers/<agent>/<sid> — same ref as framework-file-edit "
                             "(pushing HEAD carries the commits and their contents together)",
        why="Kept as its own ROW even though it now shares one carrier with "
            "framework-file-edit, because the two still FAIL differently and the table's job "
            "is to say what reaches the reducer per output class, not to enumerate mechanisms: "
            "an uncommitted edit dies with the box, while a local commit survives locally and "
            "is recoverable by anyone who looks. Merging the rows would hide that an edit left "
            "UNCOMMITTED is still stranded — the ref carries HEAD, so anything not committed "
            "before the push is not carried. That is the one residual failure mode of this "
            "carrier and it is easier to see with the row intact."),
    "product-repo-commit": CarrierDisposition(
        kind=UPSTREAM_REMOTE,
        target="the SIBLING repo's OWN origin — pushed per world/conventions/"
               "post-execution.md Step 2 (pull-before / push-after, every mode)",
        why="A worker's product-repo work lands in a DIFFERENT git repo, which no "
            "refs/workers/<agent>/<sid> ref can carry: --push-worker-ref pushes THIS "
            "repo's HEAD and knows nothing about a sibling checkout. So the carrier is "
            "the sibling's own origin, and the failure mode is the commit that was made "
            "and never pushed — it survives locally, reads as done in the goal record, "
            "and is invisible to everyone including the reducer. Measured 2026-08-09 on "
            "cc-08 (g-115-4651): the fix was committed, the goal was CLOSED, and the "
            "repo sat ahead=1 behind=1; it was caught only by checking the push state by "
            "hand, because check-outputs did not know this class and exited 2 — the "
            "'unlisted class is not thereby carried' case this table exists to prevent, "
            "reproduced one repo over. Its own ROW rather than folded into "
            "local-git-commit because the two fail in DIFFERENT repositories: pushing "
            "the Mind worker ref carries neither the sibling commit nor any signal that "
            "one is pending, so a green framework-file-edit check says nothing here.\n"
            "            Where a deploy hold forbids merging (guard-3139), a DRAFT PR is "
            "still a COMPLETE carrier — the reducer can see and review it; only the merge "
            "waits. So a hold is never a reason to leave the commit unpushed, which is the "
            "failure this row names. (Restored 2026-08-11 by the g-115-2473 evil-merge "
            "audit: this clause was dropped by merge 46d59e2eb, an undocumented sync merge "
            "where two Bodies had independently written this same row and git took one "
            "side cleanly. The ROW survived — only this clause did not, which is why a "
            "raw at-HEAD token check reads the loss as benign supersession.)"),
    "remote-host-config-edit": CarrierDisposition(
        kind=IN_PLACE_AT_DESTINATION,
        target="the TARGET HOST's own filesystem, plus whatever archive convention that "
               "host keeps (e.g. zakpod1:/home/zak/config-archive/) — the goal's "
               "outcome_note is the only fleet-visible record",
        why="A worker edit to a file on a remote host — an inference pod's nginx config, a "
            "systemd unit, an engine env file — is applied AT its destination, so unlike "
            "every git-carried class there is nothing to transport and no 'finished work "
            "stranded on the worker box' failure. That is precisely why it is NOT "
            "no-carrier, and why reusing upstream-remote would be wrong: that kind means a "
            "REPO's own origin, and a host filesystem is not a repo.\n"
            "            What this class does not have is any fleet-visible COPY. No git "
            "ref carries it, no own-cloud store holds it, and world/changelog.jsonl never "
            "sees it. So the failure mode inverts: the edit survives and its EXPLANATION "
            "does not. A re-image, a config-management run, or a restore-from-backup "
            "silently reverts work that every Body still reads as complete — the g-4638 "
            "shape (completion and landing are separate events) with the landing on a box "
            "outside the repo entirely.\n"
            "            TWO OBLIGATIONS FOLLOW, and they are this row's whole point: "
            "archive to the target host's own convention BEFORE editing (so the box itself "
            "carries a recovery layer), and put the before/after diff in the outcome_note "
            "(so the fleet carries the reason). Measured 2026-08-24, g-326-629: an alpha "
            "worker on cc-08 edited zakpod1:/etc/nginx/conf.d/llama-lb.conf and "
            "check-outputs had no class for it at all — the same shape as g-306-263, where "
            "an unlisted class was not thereby carried."),
}


def carrier_gaps() -> "list[str]":
    """Every way the carrier contract is currently incomplete, as messages.

    Empty list == every declared output class names a carrier or an explicit
    no-carrier row. NOTE what this does NOT assert: a no-carrier row is a
    complete DECLARATION, not a working carrier. `unreachable_output_classes()`
    is the query for "what is still stranded" -- keeping them separate is
    deliberate, so that declaring the truth never looks like fixing it.
    """
    gaps = []
    declared = set(OUTPUT_CLASS_CARRIERS)
    canonical = set(CANONICAL_OUTPUT_CLASSES)

    for cls_name in CANONICAL_OUTPUT_CLASSES:
        if cls_name not in declared:
            gaps.append(f"output class {cls_name!r} has NO carrier row")
    for cls_name in sorted(declared - canonical):
        gaps.append(
            f"carrier row {cls_name!r} is not in CANONICAL_OUTPUT_CLASSES")
    return gaps


def unreachable_output_classes() -> "list[str]":
    """Output classes a worker can produce that currently reach the reducer via
    NOTHING, each with the goal tracking its carrier.

    The consumer-facing half of the table: a worker-loop (or a reviewer reading
    a worker's outcome_note) can ask this instead of inferring reachability from
    an absence. Sorted for stable output.
    """
    return [
        f"{name} -> no carrier (tracked by {row.pending_goal})"
        for name, row in sorted(OUTPUT_CLASS_CARRIERS.items())
        if row.kind == NO_CARRIER
    ]


def git_ref_delivery(produced, agent=None, sid=None) -> "tuple[str, str]":
    """For GIT_REF-carried classes, ask whether the ref ACTUALLY LANDED on origin.

    `stranded_outputs()` below answers "is there a CHANNEL for this class?" by
    reading the carrier table. That is a question about design, and on
    2026-08-16 it answered `carried (6 output class(es))` for a commit that
    never left the box: the merge deferred, iteration-push.sh soft_exit'd
    before reaching its push block, and the table -- correctly, on its own
    terms -- still reported a carrier. Phase 3.7's stated purpose is catching
    exactly the case where an artifact reaches the reducer via nothing, so a
    table-only answer is not enough for the classes whose carrier is a push
    that can silently fail (g-115-6368).

    Returns (verdict, detail):
      "n/a"        no GIT_REF class was named -- nothing to verify
      "verified"   the remote ref equals local HEAD
      "stranded"   the remote ref is ABSENT or does not equal HEAD
      "unverified" the check could not run (identity unresolved, no remote,
                   git unavailable, network error). Deliberately NOT "verified":
                   an unrunnable check is ignorance, never an all-clear.
    """
    git_classes = sorted(
        c for c in set(produced)
        if OUTPUT_CLASS_CARRIERS.get(c) and OUTPUT_CLASS_CARRIERS[c].kind == GIT_REF
    )
    if not git_classes:
        return "n/a", ""

    agent = agent or os.environ.get("MIND_AGENT") or AGENT_NAME
    sid = sid or os.environ.get("MIND_SID")
    if not agent or not sid:
        return "unverified", (
            f"agent/sid unresolved (MIND_AGENT={agent!r}, MIND_SID={sid!r}) — "
            "cannot name the ref"
        )

    wref = f"refs/workers/{agent}/{sid}"

    def _git(*args):
        return subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            capture_output=True, text=True, timeout=30,
        )

    try:
        head = _git("rev-parse", "HEAD")
        if head.returncode != 0:
            return "unverified", f"git rev-parse HEAD failed: {head.stderr.strip()[:160]}"
        head_sha = head.stdout.strip()
        remote = _git("ls-remote", "origin", wref)
        if remote.returncode != 0:
            return "unverified", f"git ls-remote failed: {remote.stderr.strip()[:160]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return "unverified", f"{type(exc).__name__}: {exc}"

    line = remote.stdout.strip()
    if not line:
        return "stranded", (
            f"{wref} is ABSENT on origin; local HEAD is {head_sha[:9]}. "
            f"Affected classes: {', '.join(git_classes)}"
        )
    remote_sha = line.split()[0]
    if remote_sha != head_sha:
        return "stranded", (
            f"{wref} points at {remote_sha[:9]} but local HEAD is {head_sha[:9]}. "
            f"Affected classes: {', '.join(git_classes)}"
        )
    return "verified", f"{wref} == HEAD {head_sha[:9]}"


def stranded_outputs(produced) -> "list[str]":
    """Of the output classes THIS work unit produced, the ones that reach the
    reducer via nothing. Empty list == every named output has a carrier.

    This is the ENFORCEMENT half of the table, and the reason the table is not
    just documentation. `unreachable_output_classes()` answers "what is broken
    in general", which a worker can read and still report complete; this answers
    "is what I just did going to survive", which it cannot. A declaration with
    no consumer is indistinguishable from a sweep that always returns clean
    (reclaim-routed-work.md) -- the measured precedent is complexity_budget.py,
    which sat with zero callers for seven weeks while a rule cited it as live.

    Raises KeyError on an unknown class rather than ignoring it: a caller naming
    an output class this table has never heard of is exactly the case where
    silence is most expensive, because an unlisted class is how the original
    defect hid. Fail loud at the call site instead of returning a reassuring [].
    """
    unknown = [c for c in produced if c not in OUTPUT_CLASS_CARRIERS]
    if unknown:
        raise KeyError(
            f"unknown output class(es) {sorted(unknown)}; known: "
            f"{sorted(OUTPUT_CLASS_CARRIERS)}. An output class that is not in "
            f"the table is not thereby carried -- add a row (g-306-263).")
    return [
        f"{c} -> no carrier (tracked by {OUTPUT_CLASS_CARRIERS[c].pending_goal})"
        for c in sorted(set(produced))
        if OUTPUT_CLASS_CARRIERS[c].kind == NO_CARRIER
    ]


def _assert_carrier_contract() -> None:
    """Refuse to import with an incomplete carrier contract.

    Import-time for the same reason as _assert_lifecycle_contract: both tables
    live in THIS file, so the only way to trip it is to be editing it, and the
    failure lands in the editing session rather than on a worker box at 3am.
    """
    gaps = carrier_gaps()
    if gaps:
        raise ValueError(
            "worker_execute carrier contract incomplete (g-306-263):\n  "
            + "\n  ".join(gaps))


def lifecycle_gaps() -> "list[str]":
    """Every way the lifecycle contract is currently incomplete, as messages.

    Empty list == the contract holds. Three failure modes, all of which have
    actually happened to the phase/stage tables in some form:
      - a canonical stage with no disposition row (the g-306-212 defect class);
      - a disposition row for a stage that is not canonical (a rename that
        updated one side only);
      - a live phase whose lifecycle stage is undeclared or points at a stage
        that does not exist.
    """
    gaps = []
    declared = set(LIFECYCLE_DISPOSITIONS)
    canonical = set(CANONICAL_LIFECYCLE_STAGES)

    for stage in CANONICAL_LIFECYCLE_STAGES:
        if stage not in declared:
            gaps.append(f"stage {stage!r} has NO disposition row")
    for stage in sorted(declared - canonical):
        gaps.append(f"disposition row {stage!r} is not in CANONICAL_LIFECYCLE_STAGES")

    for phase in sorted(set(WORKER_PHASES) | set(REDUCER_ONLY_PHASES)):
        stage = PHASE_LIFECYCLE_STAGE.get(phase)
        if stage is None:
            gaps.append(
                f"phase {phase!r} declares no lifecycle stage "
                f"(add it to PHASE_LIFECYCLE_STAGE)")
        elif stage not in canonical:
            gaps.append(
                f"phase {phase!r} maps to stage {stage!r}, which is not canonical")

    # The skill bridge () is part of THIS contract, not a separate one:
    # it is a third key into the same disposition table, so it fails here rather
    # than behind a command someone has to remember to run.
    for skill, stage in sorted(SKILL_LIFECYCLE_STAGE.items()):
        if stage not in canonical:
            gaps.append(
                f"skill {skill!r} maps to stage {stage!r}, which is not canonical")
        elif stage not in declared:
            gaps.append(
                f"skill {skill!r} maps to stage {stage!r}, which has no "
                f"disposition row")
        if normalize_skill(skill) != skill:
            gaps.append(
                f"skill key {skill!r} is not in normalized form "
                f"({normalize_skill(skill)!r}) -- lookups would never match it")
    # Disjointness is the pin that makes the negatives survive a future edit.
    both = sorted(SKILL_ELIGIBLE_DESPITE_ENCODING & set(SKILL_LIFECYCLE_STAGE))
    for skill in both:
        gaps.append(
            f"skill {skill!r} is in BOTH SKILL_LIFECYCLE_STAGE and "
            f"SKILL_ELIGIBLE_DESPITE_ENCODING -- the refusal would silently win "
            f"over an explicit worker-eligible pin")
    for skill in sorted(SKILL_ELIGIBLE_DESPITE_ENCODING):
        if normalize_skill(skill) != skill:
            gaps.append(
                f"pinned-eligible skill {skill!r} is not in normalized form "
                f"({normalize_skill(skill)!r}) -- the pin would never match")
    return gaps


def _assert_lifecycle_contract() -> None:
    """Refuse to import with an incomplete lifecycle contract.

    Import-time rather than test-time on purpose: CANONICAL_LIFECYCLE_STAGES,
    LIFECYCLE_DISPOSITIONS and PHASE_LIFECYCLE_STAGE all live in THIS file, so
    the only way to trip this is to be editing it -- the failure lands in the
    editing session, before the commit, and cannot surprise a third party. The
    test suite and the /verify-learning check assert the same predicate from
    outside, because an in-file assertion alone would pass vacuously if someone
    deleted it (guard-2582).
    """
    gaps = lifecycle_gaps()
    if gaps:
        raise ValueError(
            "worker_execute lifecycle contract incomplete (g-306-212):\n  "
            + "\n  ".join(gaps))


_assert_lifecycle_contract()
_assert_carrier_contract()


# --------------------------- WM routing ---------------------------

def _agents_root(project_root: Path) -> Path:
    return (project_root / AGENTS_PARENT_DIR) if AGENTS_PARENT_DIR else project_root


def worker_wm_path(agent: str, unit_key: "str | None" = None,
                   project_root: "Path | None" = None) -> Path:
    """The working-memory path a WORKER Body writes -- reducer-aware per-Body.

    Mirrors mind_api/src/agent_paths.py::wm_path: routes to the forked Body WM
    (`sessions/<unit_key>/working-memory.yaml`) when that file EXISTS (the
    Phase-1A activation signal -- only a NON-reducer Body ever forks one), else
    the agent-wide WM (`session/working-memory.yaml`). So a worker writes ONLY
    its own Body WM once forked, and a reducer / single-runner / no-unit_key
    collapses to the agent-wide WM (dormant until a 2nd Body forks).

    `project_root` overrides PROJECT_ROOT for tests (the body-merge.py pattern).
    """
    pr = project_root or PROJECT_ROOT
    agent_dir = _agents_root(pr) / agent
    if unit_key:
        body_wm = agent_dir / SESSIONS_DIRNAME / unit_key / "working-memory.yaml"
        if body_wm.exists():
            return body_wm
    return agent_dir / SESSION_DIRNAME / "working-memory.yaml"


# --------------------------- CLI ---------------------------

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Worker-body execution contract (Phase 2A, asp-306).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("phases", help="print the worker's simplified phase sequence")
    sub.add_parser("reducer-only-phases", help="print the phases a worker skips")
    sp = sub.add_parser("should-run-phase",
                        help="exit 0 (+print 'run') if a worker runs <phase>, else exit 1 (+'skip')")
    sp.add_argument("phase")
    wp = sub.add_parser("wm-path", help="print the worker's WM target path")
    wp.add_argument("--agent", default=AGENT_NAME or "")
    wp.add_argument("--unit-key", default=None)
    sub.add_parser("lifecycle",
                   help="print the session-lifecycle disposition table (one row per stage)")
    sub.add_parser("lifecycle-gaps",
                   help="exit 0 (+'complete') if every lifecycle stage is declared, "
                        "else exit 1 (+one line per gap)")
    sub.add_parser("carriers",
                   help="print the output-class carrier table (one row per class)")
    sub.add_parser("carrier-gaps",
                   help="exit 0 (+'complete') if every output class declares a carrier, "
                        "else exit 1 (+one line per gap)")
    sub.add_parser("unreachable",
                   help="print output classes that reach the reducer via NOTHING; "
                        "exit 1 when any exist, 0 (+'none') when all are carried")
    p_chk = sub.add_parser("check-outputs",
                           help="given the output classes THIS work unit produced, "
                                "exit 1 if any cannot reach the reducer (the "
                                "worker-loop Phase 3.7 gate), 0 if all are carried, "
                                "2 on an unknown class")
    p_chk.add_argument("classes", nargs="+",
                       help=f"one or more of: {', '.join(CANONICAL_OUTPUT_CLASSES)}")
    p_chk.add_argument("--verify-delivery", action="store_true",
                       help="for git-ref-carried classes, ALSO check the remote ref "
                            "actually equals local HEAD (exit 1 if it does not). "
                            "Run this AFTER iteration-push.sh --push-worker-ref, never "
                            "before: at Phase 3.7 the push has not happened yet and HEAD "
                            "is legitimately ahead of the ref, so it would always fire. "
                            "Without this flag the check reads the carrier TABLE only "
                            "and says so (g-115-6368).")
    p_skill = sub.add_parser("skill-eligible",
                             help="exit 0 (+'eligible') if a WORKER Body may claim a "
                                  "goal carrying <skill>, exit 1 (+'reducer-only') if "
                                  "that skill IS a reducer-only lifecycle stage "
                                  "(worker-loop Phase 1 gate)")
    # REMAINDER, not "*", and the smoke test is what forced it: the PRODUCTION
    # arg shape is the goal record's skill field verbatim -- "/replay
    # --sharp-wave" -- and under nargs="*" argparse claims "--sharp-wave" as an
    # unknown OPTION and exits 2 on the one input this command exists to judge
    # (guard-920: replicate the literal production arg shape, not the
    # contract-ideal one). REMAINDER takes everything after the subcommand
    # verbatim, so both the quoted single-arg form and the bare multi-token form
    # work.
    p_skill.add_argument("skill", nargs=argparse.REMAINDER, default=[],
                         help="the goal's skill field, args and all "
                              "(e.g. /replay --sharp-wave); empty means no skill")
    p_goal = sub.add_parser("goal-eligible",
                            help="exit 0 (+'eligible') if a WORKER Body may claim a "
                                 "goal, reading the GOAL-level executable_by_role "
                                 "declaration first and falling back to the "
                                 "skill-keyed bridge (g-115-7372). Answers for the "
                                 "~98%% of candidates that carry no skill.")
    p_goal.add_argument("--role", default=None,
                        help="the goal's executable_by_role field verbatim "
                             "(worker|reducer|any); omit when unset")
    # REMAINDER for the same guard-920 reason as skill-eligible above: the
    # production arg shape is the skill field verbatim, args and all. --role
    # must precede it, because REMAINDER swallows everything from the first
    # positional onward.
    p_goal.add_argument("skill", nargs=argparse.REMAINDER, default=[],
                        help="the goal's skill field, args and all; empty means no skill")
    sub.add_parser("reducer-only-skills",
                   help="print every skill a worker must not claim, with the "
                        "lifecycle stage each one IS")
    args = ap.parse_args(argv)

    if args.cmd == "phases":
        print(" ".join(WORKER_PHASES))
        return 0
    if args.cmd == "reducer-only-phases":
        print(" ".join(sorted(REDUCER_ONLY_PHASES)))
        return 0
    if args.cmd == "should-run-phase":
        run = worker_should_run_phase(args.phase)
        print("run" if run else "skip")
        return 0 if run else 1
    if args.cmd == "wm-path":
        if not args.agent:
            print("error: --agent required (no MIND_AGENT in env)", file=sys.stderr)
            return 2
        print(str(worker_wm_path(args.agent, args.unit_key)))
        return 0
    if args.cmd == "lifecycle":
        for stage in CANONICAL_LIFECYCLE_STAGES:
            d = LIFECYCLE_DISPOSITIONS[stage]
            kind = f"{d.kind}({d.target}"
            kind += f", mode={d.mode})" if d.mode else ")"
            pend = f"  [PENDING {d.pending_goal}]" if d.pending_goal else ""
            print(f"{stage:<20} {kind}{pend}")
        return 0
    if args.cmd == "lifecycle-gaps":
        gaps = lifecycle_gaps()
        if not gaps:
            print(f"complete ({len(CANONICAL_LIFECYCLE_STAGES)} stages declared)")
            return 0
        for g in gaps:
            print(g)
        return 1
    if args.cmd == "carriers":
        for cls_name in CANONICAL_OUTPUT_CLASSES:
            c = OUTPUT_CLASS_CARRIERS[cls_name]
            pend = f"  [NO CARRIER -- tracked by {c.pending_goal}]" if c.pending_goal else ""
            print(f"{cls_name:<22} {c.kind}({c.target}){pend}")
        return 0
    if args.cmd == "carrier-gaps":
        gaps = carrier_gaps()
        if not gaps:
            print(f"complete ({len(CANONICAL_OUTPUT_CLASSES)} output classes declared)")
            return 0
        for g in gaps:
            print(g)
        return 1
    if args.cmd == "unreachable":
        rows = unreachable_output_classes()
        if not rows:
            print("none")
            return 0
        for r in rows:
            print(r)
        return 1
    if args.cmd == "check-outputs":
        try:
            stranded = stranded_outputs(args.classes)
        except KeyError as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 2
        if not stranded:
            # The carrier TABLE is satisfied, which answers "is there a channel
            # for this class" -- a question about DESIGN.
            #
            # WHY DELIVERY VERIFICATION IS OPT-IN AND NOT THE DEFAULT (measured,
            # ): worker-loop calls this at Phase 3.7, which runs BEFORE
            # Phase 3.8 pushes the carrier. At that moment HEAD is LEGITIMATELY
            # ahead of the ref on every healthy unit -- the push has not happened
            # yet -- so a default-on delivery check reports STRANDED always, and
            # a check that always fires is a check nobody reads. It would also
            # make this CLI's exit code depend on ambient repo state, which is
            # the live-state coupling that makes a test unsatisfiable against any
            # real box.
            #
            # So the default STATES what it checked (the table, not delivery) and
            # --verify-delivery asks the stronger question. Use the flag AFTER
            # the push, never before it.
            n = len(set(args.classes))
            if not getattr(args, "verify_delivery", False):
                print(f"carried ({n} output class(es)) -- carrier TABLE only")
                print(
                    "NOTE: this answers 'is there a channel for this output class', NOT "
                    "'did the artifact land'. For the git-ref classes, re-run with "
                    "--verify-delivery AFTER iteration-push.sh --push-worker-ref.",
                    file=sys.stderr,
                )
                return 0
            verdict, detail = git_ref_delivery(args.classes)
            if verdict == "stranded":
                print(f"STRANDED: {detail}")
                print(
                    "The carrier table has a channel for these classes, but the ref did "
                    "NOT land. DO NOT report this work unit complete: run "
                    "`bash core/scripts/iteration-push.sh --push-worker-ref` and re-check; "
                    "if it still fails, record the stranding in the goal's outcome_note "
                    "and leave the goal in-progress.",
                    file=sys.stderr,
                )
                return 1
            if verdict == "verified":
                print(f"carried ({n} output class(es); git-ref delivery VERIFIED: {detail})")
            elif verdict == "unverified":
                print(f"carried ({n} output class(es)) -- carrier TABLE only")
                print(
                    f"NOTE: git-ref delivery NOT verified ({detail}). This answers "
                    "'is there a channel for this class', not 'did the artifact land'.",
                    file=sys.stderr,
                )
            else:  # "n/a" -- no git-carried class named
                print(f"carried ({n} output class(es))")
            return 0
        # Deliberately loud and prescriptive: the caller is a worker about to
        # report COMPLETE, and the whole defect was that this moment passed in
        # silence. Name the remedy, not just the fault.
        for s in stranded:
            print(s)
        print("DO NOT report this work unit complete -- the artifact cannot reach "
              "the reducer. Record the stranding in the goal's outcome_note and "
              "leave the goal in-progress.", file=sys.stderr)
        return 1
    if args.cmd == "skill-eligible":
        verdict = skill_eligibility(" ".join(args.skill))
        print("eligible" if verdict.eligible else "reducer-only")
        # The reason goes to stderr so `$(... skill-eligible ...)` captures the
        # one-word verdict cleanly while a human (or a loop transcript) still
        # sees WHY. A silent skip is the half of this fix that would rot.
        print(verdict.reason, file=sys.stderr)
        return 0 if verdict.eligible else 1
    if args.cmd == "goal-eligible":
        verdict = goal_eligibility(" ".join(args.skill), args.role)
        print("eligible" if verdict.eligible else "reducer-only")
        print(verdict.reason, file=sys.stderr)
        return 0 if verdict.eligible else 1
    if args.cmd == "reducer-only-skills":
        for skill, stage in sorted(SKILL_LIFECYCLE_STAGE.items()):
            disp = LIFECYCLE_DISPOSITIONS[stage]
            if disp.kind == REDUCER_ONLY_BY_DESIGN:
                print(f"{skill:<32} {stage:<20} {disp.kind}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
