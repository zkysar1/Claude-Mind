---
name: worker-loop
description: >-
  The simplified per-Body execution loop a forked WORKER Body runs (Mind/Body
  convergence Phase 2, asp-306). select -> claim -> execute -> RE-ENTER for the
  next work unit; closes only when work is exhausted or the reducer is gone.
  SKIPS the reducer-only phases (verify / encode / reflect / state-update /
  learning-gate); the single reducer applies those to all Bodies' merged state
  at generalize-down.
user-invocable: false
minimum_mode: autonomous
companion_scripts:
  - core/scripts/worker_execute.py
  - core/scripts/worker_reducer_liveness.py
  - core/scripts/goal-selector.sh
  - core/scripts/aspirations-claim.sh
  - core/scripts/runner-claim.sh
conventions:
  - session-state
  - goal-selection
---

# /worker-loop — Worker-Body Simplified Execution Loop (Phase 2A)

A **worker Body** is a forked instance of a Mind (keyed by `unitKey` = its
session SID) that is NOT the reducer. It runs a deliberately thin loop —
**select -> claim -> execute -> re-enter** — one work unit per pass, repeating
until work is exhausted or its reducer is gone, leaving its divergent
working-memory for the single reducer to merge later. It does NOT verify,
encode, reflect, update state, run the learning gate, evolve, or do completion
review. Those are **reducer-only**: the one Body holding `running-session-id`
(the reducer) applies them to the MERGED state of every Body at generalize-down
(Phase 1C `body-merge.py`, run from `aspirations-consolidate` Step -1). Running
encode/reflect per-worker would create N reducers — the defect the convergence
forbids.

<!-- POST_RECOVERY_EDIT_OVERRIDE="user-directed fresh-eyes doc fix from a live assistant session; on-disk mode file wrongly reads autonomous (anomaly filed as world goal), no loop is running" -->
**Activation status (updated 2026-08-05, fresh-eyes review):** ACTIVATION IS
LANDED — g-306-119-a (/start worker auto-join branch), g-306-119-b (close-body
staging+push), g-306-119-c (baseline-aware merge consume) and g-306-125
(safety rails) are all completed, and a live worker executed a real goal via
this loop (g-315-518 soak, DESKTOP-O91DLK2). The prior "until Phase 2C wires
fork-activation" wording predated those landings. Still OPEN before trusting
multi-body at scale: g-306-120 (cross-box activation dry-run), g-306-126
(live two-box soak), g-306-128 (kill-tests), and g-306-131 (three fail-safe
inversions in the reducer-liveness poll this loop runs every cycle). Design
SSOT: the `mind-engine-identity-bridge` tree node (Phase 2).

## The phase split (authoritative: `worker_execute.py`)

The phase contract is owned by `core/scripts/worker_execute.py`, NOT duplicated
here, so the worker and its tests agree on one source of truth:

```
Bash: py -3 core/scripts/worker_execute.py phases               # -> select claim execute
Bash: py -3 core/scripts/worker_execute.py reducer-only-phases  # the phases this loop SKIPS
Bash: py -3 core/scripts/worker_execute.py should-run-phase <p> # exit 0 = run, exit 1 = skip
```

A worker runs ONLY `select`, `claim`, `execute`. Every reducer-only phase
(`verify`, `spark`, `complete-review`, `state-update`, `evolution`,
`learning-gate`, `productivity-check`) returns `skip`.

## The lifecycle split + the no-transcription rule (g-306-212)

The phase split above answers "which PHASES does a worker run". It does not
answer "what does a worker do at each session LIFECYCLE stage", and for a long
time nothing did — so every lifecycle asymmetry was discovered by surprise, one
at a time: prime never runs for workers (g-306-211), the per-body heartbeat
cannot write on an IDLE worker box (g-306-208), compact restore rejected
body-keyed checkpoints (g-306-174). Same defect class each time: a reducer
lifecycle stage with **no declared worker disposition**.

`worker_execute.py` now owns `LIFECYCLE_DISPOSITIONS` — every session stage
mapped to exactly one of `shared-component` / `scoped-call` / `worker-only` /
`reducer-only-by-design`. An undeclared stage is refused at import, so the
asymmetry surfaces while someone is editing the file rather than at 3am.

```
Bash: py -3 core/scripts/worker_execute.py lifecycle        # the table, one row per stage
Bash: py -3 core/scripts/worker_execute.py lifecycle-gaps   # exit 0 = complete, 1 = undeclared
```

**The no-transcription rule.** A worker capability is a scoped **CALL** into the
shared component — a mode or flag *inside* that component — **never a
transcription of its steps into this file**. Two loops is a measured necessity
(`wf_ea3e054b`, 50:1 unification cost); two *copies of a capability* is not. A
transcription is a second implementation that drifts silently when the component
evolves, and **nothing fails when it does** — which is exactly why the rule has
to be written down rather than left to notice. So when this skill needs a
reducer capability, add the mode to the component and call it; do not restate
its steps here. That is why `## The phase split` above points at
`worker_execute.py` instead of listing the phases, and why Phase 3 enters the
existing execute protocol through `load-execute-protocol.sh` rather than
reproducing Phases 3.9–4.5.

## The loop

```
# Phase -0: confirm this Body is a worker, not the reducer — AND still OPEN.
# A worker has a forked body-WM-file (sessions/<unitKey>/working-memory.yaml);
# the reducer does not (it stays on the agent-wide WM). If this Body has no
# forked WM file it is the reducer/observer -> do NOT run the worker loop; the
# full /aspirations loop is the reducer's path.
#
# CLOSURE GATE (2026-08-09, cc-08 04:39->04:49): role alone is NOT enough — the
# fork file SURVIVES a genuine close, and the body-closing sentinel is CONSUMED
# by the stop-hook at close, so neither can tell an open worker from a closed
# one. The DURABLE record is the manifest's body_state. A closed Body that runs
# another work unit diverges AFTER its WM snapshot was staged, and
# close-body-on-genuine re-marks nothing on a second close ('not-active' noop)
# — the new work would be SILENTLY LOST at generalize-down.
Bash: grep "^body_state:" "agents/$MIND_AGENT/sessions/$MIND_SID/body-manifest.yaml"
IF the value is anything other than 'active' (closed-pending-merge / merged /
  closed-stale): this Body already CLOSED. Do NOT run a work unit, do NOT
  re-arm the net; end the turn with a Bash echo naming the body_state read.
  (The stop-hook worker-net stands down on the same manifest read —
  gate=worker-net-body-closed — so this turn-end is ALLOWed, not trapped.)
IF the manifest is MISSING while the fork WM exists: treat as OPEN (never
  invent a close from an absent record) and continue.

# Phase -0.4 — LIVENESS TICK (g-306-227). A scoped CALL to the SHARED heartbeat
# writer — never a worker-local reimplementation (no-transcription contract,
# guard-2676 / g-306-212). Runs at the top of EVERY cycle, before the liveness
# poll and before any claim, so a Body that is alive but between units still
# reports fresh.
#
# WHY IT EXISTS: heartbeat-tick.sh writes BOTH the same-box
# sessions/<SID>/body-heartbeat AND the syncable session/body-heartbeat-<SID>.json
# carrier — the only signals that let the reducer's stranded-claim sweep tell
# "worker alive, mid-unit" from "claim abandoned". This loop never called it, so a
# cross-box worker had NO liveness signal of any kind, and any claim it held past
# stranded-claim-sweep's 120-minute foreign-SID grace was popped mid-execution.
# Measured 2026-08-05 on cc-07 (both files absent 17 min into an active unit);
# retro-cause of the g-315-518 pop on 2026-08-04, which was closed against the
# writer. g-306-208 HAD already fixed the writer's ordering so the body write
# precedes the agent-state=IDLE refusal — correct, and inert, because nothing
# called it. Its own tests stayed green through the whole defect (guard-1943:
# pinning the writer says nothing about the wiring).
Bash: bash core/scripts/heartbeat-tick.sh
# EXPECT rc=2 ON A WORKER AND DO NOT TREAT IT AS FAILURE: a worker box is IDLE by
# design, and the tick refuses the agent-wide RUNNING-only work with exit 2 —
# AFTER writing both per-Body heartbeats, which is the whole point of the call.
# Fail-open by contract: never branch on this rc, never let it stop the cycle.

# Phase -0.3 — PULL LATEST FRAMEWORK (g-306-233). A scoped CALL to the SAME
# component the reducer uses, in its pull-only mode — never a hand-rolled
# git sequence (no-transcription contract, guard-2676 / g-306-212).
#
# WHY IT EXISTS: a WORKER never pulled, at all, ever. iteration-push.sh is the
# only thing in the framework that does a `git fetch` + `git merge --no-edit`,
# and when this phase was written its ONLY caller was iteration-close.sh's
# do_productivity_check — which this loop deliberately skips. So a reducer stays
# current every iteration while a worker runs whatever
# code it had when someone last pulled BY HAND. Measured 2026-08-06: cc-07 was
# 112 commits behind, then 30 four hours later, then 51 — while cc-04 never
# exceeded 2. Every framework fix shipped during a worker's life never reached
# it, INCLUDING the two liveness fixes written that same day to protect workers.
#
# CORRECTED g-115-3262 (2026-08-10): it is no longer the only caller, and the
# line number this comment used to cite had already drifted — hence the function
# name above instead. Five call sites now: this phase, worker-loop's
# --push-worker-ref, aspirations-graceful-stop, aspirations-execute Phase 4
# entry, and do_productivity_check. The reasoning above is unchanged: the
# execute-phase call was added BECAUSE close-only left the REDUCER's own tree a
# median of 7 commits behind mid-iteration (n=333, .git/iteration-push.log),
# which is the same defect this phase fixes for a worker.
#
# Placed at the TOP of the cycle, between units and before any claim, so a merge
# can never land under a goal that is mid-execution.
#
# --no-push is deliberate and is the whole difference from the reducer's call:
# "fetch + integrate, then STOP before the push decision". A worker pulls so it
# is never behind; pushing the shared tree stays the reducer's job, so two Bodies
# of one agent never contend on the same store files.
#
# No commit step is needed first, though a worker tree is normally dirty with its
# own store appends: iteration-push.sh self-heals a dirty tree in-run by
# COMMITTING agents/<self>/* churn pathspec-limited (g-115-2249) and retrying the
# merge once. Its fetch is independently throttled (FETCH_INTERVAL_MIN, stateless
# via FETCH_HEAD mtime), so calling it every cycle costs nothing on most cycles.
Bash: bash core/scripts/iteration-push.sh --no-push
# Fail-soft BY CONTRACT: it exits 0 without --strict, so a network blip, a dirty
# core/ file, or a true cross-machine conflict degrades to "resume on local code"
# and is logged LOUDLY rather than stopping the cycle. Never branch on this rc.

# Phase -0.2 — WATCHDOG TICK (g-306-240). A scoped CALL to the SAME probe engine
# the reducer uses, in its role-filtered mode — never a worker-local detector
# (no-transcription contract, guard-2676 / g-306-212).
#
# THERE WAS BRIEFLY A SECOND COPY OF THIS PHASE, at -0.35, and how it got here is
# worth one paragraph because the mechanism is invisible by construction. Two
# agents implemented g-306-240 concurrently on different boxes: one wrote the
# worker-side probe FILTER, the other wrote the peer-side WorkerStallProbe. In
# core/scripts/agent-watchdog.py the two edits landed in the same region, so git
# raised a CONFLICT and it was resolved deliberately. In THIS file they landed in different
# regions, so git auto-merged both cleanly — producing two phases that ran the
# same command twice and disagreed about what it covered (the -0.35 copy claimed
# "6 OF 10 PROBES" including BackgroundJobProbe, which the filter excludes). The
# conflict was the LUCKY case: it announced itself. The clean auto-merge is the
# dangerous one. When resolving a duplicate-implementation collision, grep the
# whole tree for the other file(s) the same pair of commits touched — the ones
# that merged WITHOUT complaint are where the damage hides.
#
# WHY IT EXISTS: agent-watchdog.py --tick had exactly ONE caller in the tree,
# iteration-close.sh:2554, and this loop deliberately skips iteration-close. So
# NO watchdog probe had EVER run on a worker box — not DaemonHealthProbe, not
# MirrorWedgeProbe, none. Same structural fact as g-306-233 (workers never
# pulled) and g-306-235; fourth instance of reducer protections not reaching the
# second orchestrator.
#
# WHAT IT COVERS, stated plainly because partial coverage read as total is the
# failure this goal was filed about. The tick runs the five BOX-LEVEL probes:
# daemon-health, mirror-wedge, freshness, clock-skew, memory-headroom. On a
# worker box those had zero monitoring before this line existed.
#
# WHAT IT DOES NOT COVER — do not let this line be mistaken for stall detection:
#   - The five reducer-shaped probes are FILTERED OUT, not merely inert. A worker
#     is agent-state=IDLE + agent-mode=autonomous BY DESIGN and writes no
#     runner-heartbeat and no running-session-id, so classify_stalled returns
#     None at its first guard and HeartbeatProbe false-fires `heartbeat_missing`
#     (both measured on cc-08). Enabling them would install five probes that
#     cannot fire, which reads as coverage.
#   - THIS WORKER'S OWN STALL. The tick runs at the top of a cycle — between work
#     units — when the loop is alive and progressing by construction. A tick
#     inside the loop dies with the loop, so it cannot observe the auth-loss /
#     process-death class (the cc-08 2026-08-06 incident, ~92 min, found by a
#     human sweep). That needs an out-of-process or peer-side observer reading
#     the syncable session/body-heartbeat-<SID>.json carrier.
#     THAT OBSERVER NOW EXISTS: WorkerStallProbe, which runs on the REDUCER and
#     is deliberately excluded from WORKER_SAFE_PROBES (a worker running it would
#     be watching itself with a detector whose whole premise is out-of-process
#     observation). It reads this Body's carrier from the store of record and
#     alerts when it goes stale WHILE this Body still holds a claim. Which makes
#     the Phase -0.4 heartbeat tick above not merely a liveness courtesy: it is
#     the signal that detector consumes, and skipping it makes this Body
#     invisible to the only thing watching it.
#   - Nor can a diary-staleness threshold be added here to fix that: on a worker
#     the execution-diary records ONE entry per GOAL at claim time, so diary
#     staleness and unit duration are the SAME quantity. Measured consecutive
#     gaps on cc-08 were 34/56/92/28/15 min, where the 92 was a real stall and
#     the rest were healthy work — no threshold separates them.
Bash: py -3 core/scripts/agent-watchdog.py --tick
# Fail-open: advisory only. It announces the role filter on stderr every run so a
# filtered tick is never mistaken for a full one. Never branch on this rc.

# Phase 0.5 — REDUCER-LIVENESS POLL (g-306-125 mechanism 2). Runs at the top of
# EVERY select cycle, before any claim. A worker whose reducer has died keeps
# claiming and executing goals whose results nobody will ever merge — the
# reducer is the only Body that runs generalize-down — so the work is silently
# discarded and the goals are held from the rest of the fleet.
Bash: py -3 core/scripts/worker_reducer_liveness.py
# rc 0 = CONTINUE to SELECT. rc 1 = WIND DOWN: take the SAME genuine-close path
# Phase 1 uses (touch the body-closing sentinel, then STOP) — do not invent a
# second close mechanism; the stop-hook consumes the sentinel and
# body-manifest.py close-body-on-genuine does the stage+push+mark.
#   Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"
# then STOP. The JSON on stdout carries {verdict, reason, rc, consecutive_errors}
# — quote `reason` in the stop message so the wind-down cause is legible.
#
# NEVER-PROMOTE is the invariant the poll turns on: no rc yields "become the
# reducer". Every ambiguous signal resolves toward wind-down, because winding
# down loses nothing (the Body's divergent WM is staged for the reducer) while
# continuing without a reducer accumulates work that is thrown away.
#
# A single transient failure (daemon/DDB error) does NOT wind down — that would
# let one daemon blip kill every worker in the fleet at once. Transients
# accumulate to `error_threshold` (default 3 consecutive); any LIVE poll resets
# the count.
#
# MEASURED LIMIT, stated because the design asks for more than the endpoint can
# give: GET /v1/admin/runner-claims returns exactly
# {agent, machine_id, agent_state, heartbeat_at} — there is NO runner_token, so
# the design's "OR runner_token changed" is NOT implemented. `machine_id` is the
# takeover proxy and catches a reducer that stale-breaks in from ANOTHER box; a
# SAME-BOX reducer restart (new token, same machine_id) is invisible to this
# poll and reports CONTINUE. Closing that needs the endpoint to expose the token.

# Phase 1 — SELECT (reuse the existing scorer; a worker selects like the reducer)
Bash: goal-selector.sh
Pick the top eligible unclaimed goal (drop any goal a partner is in_flight on).
IF no goal: this is a GENUINE close — the worker has exhausted its work. Write the
  body-closing sentinel so the stop-hook (Phase-2B producer) marks this Body
  closed-pending-merge for the reducer to merge at generalize-down, then STOP:
    Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"
  (The reducer generates work, not the worker. A worker does not INVENT an agenda —
   but "never files a goal" is too strong and was ruled on: see "May a worker file a
   goal?" below. Do NOT file here regardless; SELECT finding nothing is the close
   edge, not a moment to manufacture work.)

# Phase 2 — CLAIM (claimed_by stays the mindKey/agent-name — same claim contract)
Bash: aspirations-claim.sh <goal-id> <agent>
IF claim conflict: abort this goal, loop to SELECT.

# Phase 3 — EXECUTE (the existing execute protocol; the worker DOES the work)
Bash: load-execute-protocol.sh -> Read -> follow Phase 3.9 .. 4.5 ONLY.
# The worker writes ONLY its own forked Body WM. wm-*.sh already route to the
# Body WM when BODY_WM_PATH is injected (Phase 1A); worker_execute.worker_wm_path
# is the matching CLI resolver. Do NOT touch the agent-wide WM.

# Phase 3.5 — SPARK CAPTURE (g-306-176). The one learning act a worker performs.
# Skipping the reducer-only phases means skipping aspirations-spark Phase 6.5 and
# aspirations-state-update Step 8, which is where rb entries, guardrails, gotchas,
# forge-gaps, pattern outcomes and experience files are created. Those handlers
# need the EXECUTING session's in-context experience, which the reducer never
# had — so on the worker path they are not merely deferred, they are structurally
# unreachable (specimen g-315-518: worker executed, hypothesis resolved, commit
# pushed, ZERO learning artifacts). This step is the hand-off: the worker RECORDS
# the observation; the reducer RUNS the handlers over it at generalize-down.
#
# Apply the SAME judgment aspirations-spark Phase 6.5 applies — a reusable
# reasoning pattern, a safety lesson, an operational gotcha, a capability gap.
# A routine goal with no new insight captures NOTHING; an empty slot is the
# correct output of an unremarkable work unit, not a failure.
#
# Do NOT create the rb/guardrail/tree artifact here. A worker that encodes is an
# Nth reducer, which is the invariant the convergence forbids.
FOR EACH spark-worthy observation from this work unit (usually 0 or 1, rarely >2):
    Bash: echo '{"goal_id":"<goal-id>","category":"<goal.category>","observation":"<what was learned, in enough detail for the reducer to encode from without this session>","sq_trigger":"<sq-NNN or null>"}' | bash core/scripts/wm-append.sh spark_capture
# goal_id is REQUIRED, and not only for attribution: body-merge unions array
# slots by CONTENT HASH, so two workers whose observations happen to read
# identically would collapse into one entry and the second goal's learning would
# vanish silently. The goal_id makes the hashes differ.
# The write routes to the Body WM via BODY_WM_PATH like every other wm-*.sh call
# here — no special-casing, and no agent-wide WM write.

# Phase 3.6 — EXPERIENCE CAPTURE (g-306-199). Sibling of 3.5, and the reason it
# is SEPARATE rather than another field on the spark entry: a spark is a reusable
# LESSON the reducer encodes into rb/guardrail/tree, while this is the execution
# NARRATIVE it encodes an experience .md from. Merging them would force one
# consumer to re-derive a classification the writer already knew.
#
# UNLIKE 3.5, THIS IS NOT CONDITIONAL. A spark is written only when the unit
# produced a reusable insight (often zero); an exp_capture entry is written for
# EVERY executed goal, including routine ones — the experience archive is a
# record of what happened, and "nothing surprising happened" is a legitimate and
# useful narrative. Capturing only interesting units would bias the archive
# toward drama and silently lose the baseline it is measured against.
#
# Do NOT write the experience .md here. Encoding is reducer-only-by-design
# (worker_execute.LIFECYCLE_DISPOSITIONS), and the reducer half is a CALL into
# the existing writers — experience-add.sh and experience-archive-goal.sh — never
# a reimplementation (no-transcription contract, guard-2676).
Bash: echo '{"goal_id":"<goal-id>","category":"<goal.category>","execution_summary":"<2-3 sentences: what was done and what it produced>","outcome_class":"<deep|routine>","key_decisions":["<decision + why, one per entry>"],"surprise_level":<0-10>,"verbatim_anchors":["<exact error codes / paths / hashes / commit shas — the strings a future reader would grep for>"]}' | bash core/scripts/wm-append.sh exp_capture
# verbatim_anchors is the field that makes this worth more than reconstructing
# from the goal record: exact strings die with the session that saw them, and a
# reducer writing the .md later cannot recover an error code it never observed.
# goal_id is REQUIRED for the same content-hash reason as 3.5 — two routine units
# whose summaries read identically would otherwise collapse into one entry and
# silently lose the second goal's experience.

# Phase 3.7 — CARRIER CHECK (g-306-263). Numbered 3.7, NOT 3.9: Phase 3 above
# delegates to the EXECUTE PROTOCOL's "Phase 3.9 .. 4.5", so a worker-loop phase
# also called 3.9 would put two different Phase 3.9s in one file, thirty lines
# apart, naming different documents. Sits between 3.5 (spark capture) and 4 (end
# of work unit) in this loop's own numbering.
#
# The hand-off named in Phase 4 below is
# NOT universal: it carries the worker's WM and its goal record, and it carries
# NOTHING ELSE. A framework file edit made on a worker box reaches the reducer
# via no channel at all — measured on g-115-5147, whose finished fix sat on
# cc-07 and was 0% present on cc-04, reported COMPLETE the whole time. Nothing
# was broken; there was simply no carrier, and no moment at which that was said
# out loud. This is that moment.
#
# Name the output classes this work unit actually produced and ask the table.
# `worker_execute.py carriers` lists the classes; do NOT guess a name — an
# unknown class exits 2 rather than reassuring you, because an unlisted class is
# exactly how the original defect hid.
Bash: py -3 core/scripts/worker_execute.py check-outputs <class> [<class>...]
# rc 0 = every named output reaches the reducer -> continue to Phase 4.
# rc 1 = at least one is STRANDED. Do NOT mark the goal completed. Record the
#        stranding in the goal's outcome_note (name the class and the tracking
#        goal the command printed) and leave the goal in-progress, so the work
#        is visibly unfinished rather than falsely closed on a box nobody reads.
# rc 2 = you named a class the table does not know -> add a row before closing.
#
# The check is a scoped CALL into the shared component, never a transcription of
# its logic (guard-2676): the table lives in worker_execute.py, and a copy of its
# contents here would drift the first time a carrier lands.

# Phase 3.8 — CARRIER PUSH (g-306-264). Phase 3.7 asks whether this unit's output
# can reach the reducer; this is the step that MAKES it reach for the two classes
# git carries. Run it whenever this unit touched core/**, .claude/** or CLAUDE.md,
# or made any local commit you want the reducer to see. Harmless otherwise — it
# pushes the same HEAD the previous unit pushed.
#
# COMMIT FIRST. The ref carries HEAD, so anything uncommitted is NOT carried, and
# that is the one residual failure mode of this carrier (it is why
# local-git-commit keeps its own row in OUTPUT_CLASS_CARRIERS rather than being
# folded into framework-file-edit).
Bash: bash core/scripts/iteration-push.sh --push-worker-ref
# Pushes HEAD to refs/workers/<agent>/<sid>, then STOPS — it never touches the
# shared branch. This does NOT contradict Phase -0.3's --no-push: that flag's
# rationale is contention on shared store files, and a ref whose path contains
# this Body's sid has exactly ONE writer by construction, so the rationale does
# not reach it. Fail-soft like every other iteration-push call — never branch on
# the rc, and never let a failed push stop the cycle.
#
# The REDUCER side is `bash core/scripts/worker-ref-consume.sh` (fetch + report;
# --merge <ref> to take one). A worker does NOT run the consumer: merging another
# Body's framework edits into the shared tree is a reducer act, and report-only
# is deliberate — a framework change that applies to drifted context is worse
# than one that is lost.

# Phase 4 — END OF WORK UNIT. Do NOT run verify / spark / state-update /
# learning-gate / productivity-check. The worker's divergent WM + the in-progress/completed goal
# record are the hand-off; the reducer merges them at generalize-down and runs
# the encode/reflect/consolidate phases over the merged result.
#
# team-state in_flight is handled FOR you at genuine close — do NOT clear it here
# (g-306-132-d). Phase 2's claim WRITES in_flight, and skipping verify means
# skipping iteration-close do_verify Step 3, the only place the normal path clears
# it. The stop-hook now calls worker_close_in_flight_clear.py after a genuine
# close (result marked/marked-push-failed). It clears ONLY when the goal named by
# the live in_flight row carries THIS Body's claimed_by_sid: in_flight is
# AGENT-keyed with no sid, so a worker and its reducer share one row and an
# unconditional clear would blank a live reducer's row — worse than the stale row
# it fixes. Adding a second clear on this path would defeat that ownership test.
# Do NOT write the body-closing sentinel here — finishing ONE work-unit is NOT a
# genuine close; Phase 5 re-enters this loop for the next unit, and a sentinel
# left here would make a turn-end between units mark the Body closed
# prematurely, losing later divergence (g-306-70). The sentinel is written ONLY
# when SELECT finds no work (Phase 1) — the unambiguous genuine close. A worker
# that ends abruptly without reaching Phase 1 (crash, terminal closed) leaves no
# sentinel; cleanup-stale-bindings then stages its WM via the stale-binding path,
# so no divergence is lost either way.

# Phase 5 — CONTINUE (the loop edge; added 2026-08-03 after the gap fired live).
# v1 said "the driver may re-invoke this loop" — but no driver exists; the worker
# one-shotted after its first goal (g-315-518 soak, DESKTOP-O91DLK2). The loop
# re-invokes ITSELF: the terminal tool call of a completed work unit is
# Skill(worker-loop), which re-enters at Phase -0 (re-verifying worker identity —
# guard-517/guard-463 class: role-gated re-entry) and runs the Phase 0.5
# reducer-liveness poll before any new claim. NEVER Skill(aspirations) — that is
# the reducer's full-loop re-entry. Every close path (Phase 0.5 wind-down,
# Phase 1 genuine close, user stop) still ends the turn with a Bash call after
# its sentinel work, exactly as before — self-continuation never overrides a
# close edge.
```

## What a worker MUST NOT do

- Run any reducer-only phase (the gate above returns `skip` for all of them).
- Write the agent-wide working memory (`session/working-memory.yaml`) — a worker
  writes ONLY `sessions/<unitKey>/working-memory.yaml`.
- Set/clear `running-session-id` or claim the reducer role.
- Run consolidation — that is the reducer's job.
- Generate new aspirations. Goals are NOT a flat prohibition — see the ruling below.

## May a worker file a goal? (RULED 2026-08-06, g-306-250)

The prior contract was one sentence — "a worker never fabricates goals" — and it did
not settle the live cases. Four instances accumulated where a worker measured
something real and had no sanctioned move, and three separate agents recorded "this
deserves its own goal" without filing one. This is the ruling; do not re-derive it.

**The rule's purpose is to stop a worker inventing an AGENDA the reducer never
approved.** Every case below is decided against that purpose, not against the
sentence.

| Case | Example | Ruling |
|---|---|---|
| **A. Preserves sanctioned scope** — a successor carrying the unfinished remainder of the goal you are closing | g-335-901 | **FILE.** Filing nothing DROPS work already discovered under work the reducer already approved. That is not a new agenda; it is the same one, unfinished. |
| **B. New scope, observable by any Body** — a framework defect, a mislabelled field, a stale predicate | the agent-queue claim defect (g-306-238) | **DO NOT FILE. Relay** via `wm-append.sh spark_capture`, and post to the findings board if it is time-sensitive. A reducer or partner can see this too, so the relay loses nothing but time. |
| **C. New scope, MACHINE-LOCAL** — only observable from this box: its store, filesystem, process table, installed binaries, local git state | the `.history` wiring fix (g-115-644); `unzip` absent on this box (g-335-869) | **FILE.** No reducer and no partner can EVER observe a worker box's local state, so the relay is the ONLY channel and its loss is unrecoverable — nobody can rediscover what only this box can see. |

Case B's cost is real but bounded and was measured: g-306-238 sat from 09:55 to
22:26 (12.5h) waiting on a reducer. Case C's cost is unbounded — a dropped
machine-local finding is not late, it is gone. That asymmetry is what splits B
from C, and it is why "new scope" alone is the wrong discriminator.

Three obligations on any goal a worker files:

1. **Mark it.** Put `filed by <agent> worker Body on <hostname>` in the
   description with the case letter (A or C) and why. A worker-filed goal the
   reducer disagrees with is recoverable by skipping it — but only if the reducer
   can SEE that a worker filed it.
2. **Dedup first** (guard-1204): run a search that does NOT key only on your own
   phrasing. Three of this ruling's four instances were rediscoveries.
3. **Never file case B by relabelling it C.** The test is not "could I have found
   this only because I was here" — it is "could a reducer on another box observe
   this same thing at all?" If yes, it is B.

Note this ruling ACTS ON A ROLE, so per guard-2783 state the complement
explicitly: **the reducer is unconstrained by all of the above.** It files freely
in every case; nothing here narrows it. The rule exists only because a worker's
divergent state is merged later and its agenda would arrive unreviewed.

## Return Protocol

See `.claude/rules/return-protocol.md` — the last action of any turn MUST be a
tool call, not a text summary. This skill is the WORKER's orchestrator, so it
re-enters ITSELF. Three terminal shapes, selected by what just happened:

**A worker has its own deadman net (g-306-239, 2026-08-06).** Until then it had
none: this file contained zero mentions of ScheduleWakeup, and the terminal-pair
in `.claude/rules/return-protocol.md` was written for the REDUCER alone. So a
worker turn ending on trailing TEXT — the exact failure the rule above exists to
prevent, and one the Stop hook cannot reliably catch (rb-629/guard-454: Claude
Code does not fire Stop on a text-only turn-end) — was dead PERMANENTLY, with
nothing to re-invoke it. The signature would be **a dead LOOP inside a live
PROCESS**, which no process-liveness check sees.

**This gap is STRUCTURAL, not witnessed — do not cite an incident for it.** It
rests on the grep (this file had 0 mentions of ScheduleWakeup), and **no worker
text-death has ever been observed.** g-306-239 was originally filed citing a
cc-08/foxtrot outage as the measured instance; that attribution was **retracted
before any work began** — cc-08 had lost its Claude Code LOGIN and sat at an idle
prompt because it could not authenticate. The tell is `CTX: 0%/0% [fresh]`: a
text-death PRESERVES context, so a fresh context means a session restart, not a
return-protocol violation. Re-login resumed the loop immediately, which a
text-death would not do.

So: do NOT use cc-08 as the regression scenario, and do NOT expect this net to
prevent an auth-loss stall — ScheduleWakeup cannot fire a turn when the CLI has
no valid login. The sibling DETECTION gap (worker stalls are invisible to the
watchdog, whose `--tick` has exactly one caller in `iteration-close.sh`, which
workers skip) covers all stall causes including auth loss.

Do NOT reach for the reducer's `<<autonomous-loop-dynamic>>` sentinel — it is
both forbidden and inert here. It resolves to the AUTONOMOUS loop instructions
(guard-517/guard-463 forbid a worker entering those), and the aspirations loop
requires agent-state RUNNING while a worker box is IDLE by design, so a
resurrected turn would refuse at Phase -1.5 rather than resume. The worker arms a
NATURAL-LANGUAGE prompt instead — sanctioned by
`.claude/rules/schedule-wakeup-correctness.md`, and it clears
`schedule-wakeup-gate.py`, whose predicate refuses only prompts STARTING with
"/" whose first token is not "/loop" (verified: the emitted prompt returns
`is_bad_slash_prefix == False`, against a `/aspirations loop` control returning
`True`).

The directive text is NOT written out here on purpose. guard-2676 (the
no-transcription contract) requires a scoped CALL to a shared component —
`core/scripts/deadman-directive.sh` — so the delay, the opt-out flag, and the
closure-check-then-arm ordering live in exactly one place for this loop and
cannot drift from it the way a transcription would.

That component serves the WORKER only. It shipped with a `--role reducer` branch
that had zero callers, and g-306-241 RETIRED it rather than wiring the three
reducer emitters to it: `iteration-close.sh` carries the rb-4345 single-shot-net
lesson in full where the shared branch carried one terse sentence, and
`iteration-close-reminder.py` keys its deep-recurring branch on
`recurring-close.sh`'s literal emitted text, which this component never produced
— so wiring would have downgraded the live imperative AND silently broken that
detector. `--role reducer` now refuses at rc=2 with an explanation. Read this as
the scope of the guarantee above, not as a gap: guard-2676 governs how a WORKER
capability is built, and this call site IS that capability. The three conditions
that would make a shared reducer directive worth extracting are recorded in the
script's own header.

| Just finished | Terminal tool call |
|---|---|
| A work unit (Phase 4 done, no close condition) | **The deadman PAIR** — run `bash core/scripts/deadman-directive.sh --role worker` and emit exactly the two batched calls it prints: `ScheduleWakeup(<natural-language resurrection prompt>, 600)` THEN `Skill(worker-loop)` as the LAST call. `Skill(worker-loop)` is still the primary re-entry (NEVER `Skill(aspirations)` — reducer-only, guard-517/guard-463; and never a bare Bash echo — the pre-2026-08-03 text said "hand control back to its driver", naming a driver that does not exist, and the Body silently one-shotted after its first goal, g-315-518 soak). The ScheduleWakeup is a NET behind it, not a substitute. |
| A close path — Phase 0.5 wind-down or Phase 1 genuine close (sentinel just touched), or a user stop | Bash echo stating the close reason. **Do NOT arm the net here** — the turn genuinely ends; stop-hook Phase 2B consumes the sentinel and stages the WM. A net armed by the PREVIOUS work unit is still pending and will fire ~600s later; that firing is benign because THREE layers read the DURABLE closure record, `sessions/<SID>/body-manifest.yaml` `body_state`: the resurrection prompt checks it FIRST and declines to resume (and does not re-arm) when it is not `active`; Phase -0's closure gate refuses a work unit on the same read; and the stop-hook worker-net stands down on it (`gate=worker-net-body-closed`). The `body-closing` SENTINEL cannot serve this purpose — close-body-on-genuine CONSUMES it on every genuine-close branch, so after a completed close its absence is indistinguishable from "no close ever happened". (The pre-2026-08-09 prompt read exactly that as "resume", and the worker-net BLOCKed every post-close turn-end into a second sentinel ceremony — measured cc-08 04:39→04:49.) This is the worker's equivalent of the reducer's safe landing (whose resurrected turn self-aborts at Phase -1.5 on `agent-state != RUNNING`). |
| Consulted for the phase split only (no work unit ran) | The `worker_execute.py` Bash call whose output answered the question. |
