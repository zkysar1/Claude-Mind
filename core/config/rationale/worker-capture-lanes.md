# Rationale: Worker capture lanes — why four slots, and what each measurement showed

Referenced from `.claude/skills/worker-loop/SKILL.md` Phases 3.5 (spark_capture),
3.6 (exp_capture), 3.65 (hyp_capture), 3.66 (encoding_capture) and the shared
`load_bearing` field. Each section below is that phase's WHY commentary, moved
here verbatim on 2026-09-23 (g-115-8214) so the skill fits under the 65,536 B
injection ceiling. The rules stayed at their call sites; what moved is the
provenance and the design argument for keeping four separate lanes.

## Phase 3.5 — why a worker captures instead of encoding (g-306-176)

Phase 3.5 — SPARK CAPTURE (g-306-176). The one learning act a worker performs.
Skipping the reducer-only phases means skipping aspirations-spark Phase 6.5 and
aspirations-state-update Step 8, which is where rb entries, guardrails, gotchas,
forge-gaps, pattern outcomes and experience files are created. Those handlers
need the EXECUTING session's in-context experience, which the reducer never
had — so on the worker path they are not merely deferred, they are structurally
unreachable (specimen g-315-518: worker executed, hypothesis resolved, commit
pushed, ZERO learning artifacts). This step is the hand-off: the worker RECORDS
the observation; the reducer RUNS the handlers over it at generalize-down.

Why `sq_trigger: "sq-013"` exists (the routing key's history):

sq_trigger is the ROUTING KEY on the reducer side, not decoration. Two values
matter most (2026-08-16, goal-completion audit D1):
  "sq-013" — this observation is WORK someone must own (a defect, a follow-up,
             a capability gap, a dependency): the Case-B relay of the filing
             ruling below. The reducer's Worker Spark Replay runs the sq-013
             work-discovery handler over sq-013 relays and FILES the goal
             (dedup first). Shape the observation as a filing, not a musing:
             what is wrong / needed, where (path:line, script, store), the
             evidence you measured, and a one-line suggested title. Before
this date the relay reached only the lesson handlers (rb /
guardrail / gotcha) and NEVER became a goal — the ruling's
"loses nothing but time" was false; it lost the work.

## Phase 3.6 — why experience capture is its own slot (g-306-199)

Phase 3.6 — EXPERIENCE CAPTURE (g-306-199). Sibling of 3.5, and the reason it
is SEPARATE rather than another field on the spark entry: a spark is a reusable
LESSON the reducer encodes into rb/guardrail/tree, while this is the execution
NARRATIVE it encodes an experience .md from. Merging them would force one
consumer to re-derive a classification the writer already knew.

## Phase 3.65 — why hypothesis evidence is its own slot, and why the worker never resolves (g-306-200)

Phase 3.65 — HYPOTHESIS-EVIDENCE CAPTURE (g-306-200). Third capture lane, and
the one with the narrowest trigger. Numbered 3.65 rather than appended after 3.8
because it belongs with its siblings: 3.5/3.6/3.65 all WRITE to the Body WM,
while 3.7/3.8 are about getting outputs OFF this box.

WHY IT IS A SEPARATE SLOT rather than a field on the 3.6 entry: an exp_capture
entry is a narrative the reducer encodes an experience FROM, whereas this is
EVIDENCE INPUT to the EXISTING /review-hypotheses resolution protocol, keyed to
a specific hypothesis_id. Merging them would force that protocol to re-derive a
classification the writer already knew.

THE WORKER DOES NOT RESOLVE, AND THAT IS THE WHOLE DESIGN. Resolution runs the
full protocol on the reducer (pipeline-move.sh / pipeline-update-field.sh); a
worker resolving from its own unmerged state is the Nth-reducer defect. Supplying
evidence and resolving are different acts, and only the first is yours — which is
also what makes the no-double-resolution guard expressible: the reducer can see
that evidence was already supplied for a hypothesis before it resolves
independently.

## Phase 3.66 — the measured yield of encoding_capture, and when to retire it (g-306-202)

CONDITIONAL, like 3.5 and 3.65 — but do NOT expect it to be usually empty. That
expectation was written into this comment when the lane shipped, on a RETROSPECTIVE
count (alpha, cc-08, 2026-08-11: 0 of 6 spark observations from the units BEFORE
this lane existed were tree-worthy), and the author's own next three units
falsified it: 4 encoding_captures in 3 units, on the same box, the same day.
The retrospective count was biased by construction — those observations were
WRITTEN as sparks, by an agent with nowhere else to put them, so counting how
many "were really facts" measures the old lane's framing, not this lane's yield.
Beware re-deriving an expectation from data collected before the thing existed.

The retirement condition still stands, unchanged and worth keeping (it is what
stops this lane being defended out of sunk cost): if a later audit finds it
genuinely empty across many sessions while tree nodes keep being encoded from
goal records, then goal records are the real bridge and this lane should be
RETIRED, not defended (learning-philosophy.md rule 5). What changed is only the
prior — current evidence points the other way, so an empty lane is a signal to
look at, not the expected default.

Why `evidence` and `supersedes` are the fields that matter:

`evidence` is what makes this worth more than an assertion: the reducer writes
the node later and cannot re-measure what it never observed, and a tree node
asserting a fact with no traceable measurement is the drift these captures exist
to prevent.
`supersedes` is the field that earns this lane its keep. A fact that CORRECTS an
encoded belief is the highest-value thing a worker can hand up, and it is
precisely what a free-text spark buries — the reducer would have to notice the
contradiction on its own, which is the re-derivation this split exists to avoid.

Why slot registration is load-bearing, and why it is two files:

REGISTRATION IS LOAD-BEARING, AND IT IS TWO FILES, NOT ONE: encoding_capture is
in ARRAY_SLOTS in BOTH core/scripts/wm.py AND mind_api/src/endpoints/wm_write.py.
An unregistered slot is NOT refused (wm-append accepts any string as a slot name,
rc=0, no validation) — it is silently NULLED by cmd_maintain's scalar eviction at
120 min while the Body waits for consolidation, so the loss lands exactly where
nobody is watching.
The DAEMON copy is the LIVE one (guard-742/547): wrappers are daemon-only, so
wm-append routes to wm_write.py and the eviction predicate that decides survival
is read from THERE. Editing wm.py alone changes NOTHING at runtime while looking
entirely correct in the diff — measured while adding this lane, and caught only
because test_wm_reset_cadence.py::test_shared_wm_constants_parity_with_daemon
failed. Adding a fifth lane means editing BOTH sets; that parity test is what
makes forgetting loud, so never skip it when touching this.

## `load_bearing` — what the flag buys, measured (g-306-293, g-306-361)

It buys two things, and the second is why the field exists at all:
  1. PRIORITY MERGE. core/scripts/capture_fast_lane.py runs on the reducer once
     per iteration (iteration-close --phase productivity-check) and copies
     flagged entries into the reducer WM WITHOUT waiting for consolidation. It
     reads ACTIVE Bodies too, which the full generalize_down cannot: that pass
     enumerates only closed-pending-merge Bodies, so an active worker's captures
     are invisible to the reducer however often consolidation runs.
  2. EVICTION EXEMPTION. At cap, wm append FIFO-drops the OLDEST entry — exactly
     the one that has waited longest, i.e. the one a priority lane exists to
     rescue. Flagged entries sort last and are popped last. Measured on ONE
     active Body (alpha, cc-08, 2026-08-15, 21 units): 237 entries destroyed
     (spark 144, exp 74, hyp 19) against caps of 50/20/10 — ~74% of everything
     spark_capture was handed. Second instance of the g-306-289 measurement
     (215 on cc-07), so this is the rule, not an outlier.

KNOWN COST, ACCEPTED (g-306-361): at saturation an honest UNFLAGGED append
destroys an unrecoverable peer, a flagged one a carried duplicate. Flag
honestly anyway. Measured: re-ordering the victim moves ~10% of losses; the
lever is that a Body lane never DRAINS (the carrier copies, never clears).

## The lanes' field notes, as the skill carried them in full

The skill keeps each note's rule in one or two lines. The full text was:

Phase 3.5, goal_id:

goal_id is REQUIRED, and not only for attribution: body-merge unions array
slots by CONTENT HASH, so two workers whose observations happen to read
identically would collapse into one entry and the second goal's learning would
vanish silently. The goal_id makes the hashes differ.
The write routes to the Body WM via BODY_WM_PATH like every other wm-*.sh call
here — no special-casing, and no agent-wide WM write.

Phase 3.6, why the lane is unconditional, and its fields:

UNLIKE 3.5, THIS IS NOT CONDITIONAL. A spark is written only when the unit
produced a reusable insight (often zero); an exp_capture entry is written for
EVERY executed goal, including routine ones — the experience archive is a
record of what happened, and "nothing surprising happened" is a legitimate and
useful narrative. Capturing only interesting units would bias the archive
toward drama and silently lose the baseline it is measured against.

verbatim_anchors is the field that makes this worth more than reconstructing
from the goal record: exact strings die with the session that saw them, and a
reducer writing the .md later cannot recover an error code it never observed.
goal_id is REQUIRED for the same content-hash reason as 3.5 — two routine units
whose summaries read identically would otherwise collapse into one entry and
silently lose the second goal's experience.

Phase 3.65, its fields:

hypothesis_id MUST name a real pipeline.jsonl record — check before writing.
An id that matches nothing is worse than no entry: it survives the merge, reaches
the reducer, and cannot be joined to anything, so it reads as a broken protocol
rather than as a worker mistake.

goal_id is REQUIRED for the same content-hash reason as 3.5/3.6, and it does more
work here: two units supplying evidence on the SAME hypothesis_id would otherwise
be at risk of collapsing into one entry, which is exactly the case where losing
the second observation most distorts the resolution.
suggested_resolution is a READ, not a verdict. The reducer may disagree with it
on the same evidence; if this field ever starts being applied as-is, the lane has
become a second resolver and the guard above has failed.

Phase 3.66, why a worker never writes the tree node:

Do NOT write the tree node here, and do not reach for /tree. Tree encoding is
aspirations-state-update Step 8, reducer-only-by-design; a worker that encodes
from its own unmerged state is the Nth-reducer defect. Note /tree IS pinned
worker-eligible in SKILL_ELIGIBLE_DESPITE_ENCODING — that pin is for
goal-directed artifact creation from content supplied IN THE GOAL, and using it
to encode your own session's findings is exactly the misread it warns about.

## Cross-references

- `.claude/skills/worker-loop/SKILL.md` Phases 3.5–3.66 — the lanes these sections explain
- `worker-spark-replay-bounded-drain.md` — the reducer side that consumes spark_capture
- `capture-carrier-delivery-check.md` — Phase 3.7's check that these captures reached the store
- learning-philosophy.md rule 5 — subtraction is learning (the 3.66 retirement condition)
