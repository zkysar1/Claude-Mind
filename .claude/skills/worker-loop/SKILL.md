---
name: worker-loop
description: >-
  The simplified per-Body execution loop a forked WORKER Body runs (Mind/Body
  convergence Phase 2, asp-306). select -> claim -> execute -> RE-ENTER for the
  next work unit; closes only when work is exhausted or the reducer is gone.
  Records the status it judged for each unit through the shared close writer
  (Phase 4a), then SKIPS the reducer-only phases (the LLM verify phase / encode /
  reflect / state-update / learning-gate); the single reducer applies those to
  all Bodies' merged state at generalize-down.
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
`learning-gate`, `productivity-check`) returns `skip`. `verify` there means the
LLM phase (/aspirations-verify: hypothesis outcomes, Q1/Q2/Q3, dependent
unblocking); the MECHANICAL status write for the unit the worker itself executed
is Phase 4a below — a scoped call to the shared close writer, sanctioned by the
`REDUCER_ONLY_PHASES` note in `worker_execute.py` (2026-08-16, g-115-6337).

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
IF the value is in the CLOSED SET — 'closed-pending-merge' / 'merged' /
  'closed-stale': this Body already CLOSED. Do NOT run a work unit, do NOT
  re-arm the net; end the turn with a Bash echo naming the body_state read.
  (The stop-hook worker-net stands down on the same manifest read —
  gate=worker-net-body-closed — so this turn-end is ALLOWed, not trapped.)
IF the value is 'parked': this Body is RESUMABLE and did NOT close (g-306-291).
  Do NOT end the turn here. Skip the closure exit, continue through the
  preamble, and let the Phase 0.5 poll decide resume-or-re-park — that poll is
  the ONLY place the decision is made, so this gate never re-derives it.
  TEST THE CLOSED SET, NEVER 'not active'. The old predicate here was
  "anything other than active", correct while every non-active state was
  terminal — so introducing `parked` would have made this gate refuse every
  unit of a Body that is deliberately alive, which is byte-for-byte the durable
  close g-306-291 exists to remove. Same latent trap sat in the resurrection
  prompt (fixed, pinned) and in the stop-hook worker-net (fixed, 5th valve).
  Any state this gate does not recognise resolves toward RUNNING, because a
  wrong close is the unrecoverable direction and a wrong continue is not.
IF the manifest is MISSING while the fork WM exists: treat as OPEN (never
  invent a close from an absent record) and continue.

# Phase -0.5 — LIGHT PRIME (g-306-211). TWO TIERS, and the split is the point:
# the IDENTITY half runs once per worker session, the RECENCY half runs on EVERY
# re-entry. The worker path never primed at all: /start's W-steps jump
# fork->Skill(worker-loop), and /prime runs only for reducer/reader/assistant. The
# worker is NOT context-blind — the execute protocol retrieves per-goal at 4
# points — but per-goal CATEGORY retrieval structurally misses cross-cutting
# MECHANISM-indexed guardrails (the measured two-query dilution class,
# retrieval-triggers.md "Why TWO queries"), and Self's Decision Authority never
# loads at all. This phase closes exactly that gap and nothing more.
#
# Scoped CALLs to the SAME surfaces /prime reads (no-transcription contract,
# guard-2676): Read + world-cat.sh + guardrails-read.sh + reasoning-bank-read.sh.
# Do NOT transcribe /prime's other steps here — board sweeps, insights,
# peer-surface, and category loading are deliberately SKIPPED (per-goal retrieval
# covers that terrain); the worker stays thin.
#
# WHY THE RECENCY HALF IS UNCONDITIONAL (g-306-298, landing the 2026-08-13 USER
# DIRECTIVE addendum on g-306-211, quoted): "scope now includes PER-UNIT refresh,
# not only once-per-session prime ... workers must not run multi-hour sessions on
# entry-time rails." This phase used to sit ENTIRELY behind the sentinel, so every
# unit after the first ran on rails loaded at session entry. Measured on cc-07
# 2026-08-16: a light-prime-done sentinel dated 2026-08-15 17:59 with the session
# still executing units at 12:xx the next day — ~18.5h on entry-time rails,
# against a directive whose stated rationale is measured unit gaps of 15-92 min.
#
# THE COST IS WHY IT CAN BE UNCONDITIONAL RATHER THAN CADENCED. Measured on cc-02
# (zeta, uname -r 6.8.0-137-generic): reasoning-bank-read.sh --recent 0.059s,
# guardrails-read.sh --summary 0.093s — ~0.15s per unit, against a Phase -0.3 that
# runs a real `git fetch` + `git merge`. That is "cost-proportional to the pull",
# which is the bound the directive itself sets. Note guardrails-read.sh has NO
# --since/delta flag (verified before naming it, guard-359: the arg loop accepts
# --id / --category / --active / --summary only), so the "guardrail-index delta"
# the directive names is served by re-reading the whole-corpus one-line index —
# which is exactly what --summary already is.
#
# ORDERING IS LOAD-BEARING: the recency slice runs BEFORE the sentinel test, not
# inside its else-branch. Phase 5 re-invokes this skill after every work unit and
# the deadman/autocompact paths re-enter it too, so every entry path passes this
# point and therefore gets the refresh (guard-3448: a gate is only as broad as its
# entry points). Moving these two calls below the test re-creates the exact bypass
# this change exists to remove.
#
# READ-ONLY BY CONTRACT: both calls are read wrappers — no counter-bumping flags,
# no reducer-side writes, no WM mutation. Do not add any.
Bash: bash core/scripts/reasoning-bank-read.sh --recent   # rb recency window — EVERY re-entry
Bash: bash core/scripts/guardrail-manifest.sh   # whole-corpus id manifest (100% coverage, no rule text), EVERY re-entry; expand via --id/--category before acting (guard-1421)
# IDENTITY half — once per SESSION. The guard is a sentinel in the per-session dir
# (L1-sanctioned scratch, path-resolution.md).
Bash: test -f "agents/$MIND_AGENT/sessions/$MIND_SID/light-prime-done" && echo "light-prime: identity half already done this session"
IF the sentinel EXISTS: skip to Phase -0.4 (the recency slice above ALREADY RAN —
  re-reading identity every unit would pay the cost the session-scoping exists to
  avoid).
ELSE (first pass this session):
    Read: agents/$MIND_AGENT/self.md          # identity + Decision Authority
    Bash: bash core/scripts/world-cat.sh program.md   # The Program — shared purpose; the directive's fourth named input, and the one the once-per-session half was missing
    Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/light-prime-done"
# Fail-open: if any read errors, note it out loud and continue — a worker that
# cannot prime still executes (per-goal retrieval remains), it just loses the
# cross-cutting index this phase exists to load. Never let the prime stop the
# cycle.

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
# THIS MOVES HEAD, so it VOIDS any full-suite run still executing (tree-moved
# outranks every verdict; the detector is HEAD-MOVEMENT, not merge — Phase 3.8's
# own COMMIT FIRST voids it too, g-115-7943). A SUITE MUST FINISH INSIDE THE UNIT
# THAT LAUNCHED IT. --no-push suppresses the PUSH, never the merge.
# Rationale (WHY a suite must finish inside its unit): core/config/rationale/suite-run-voided-by-loop-merge.md
Bash: bash core/scripts/iteration-push.sh --no-push
# Fail-soft BY CONTRACT: it exits 0 without --strict, so a network blip, a dirty
# core/ file, or a true cross-machine conflict degrades to "resume on local code"
# and is logged LOUDLY rather than stopping the cycle. Never branch on this rc.
# BRANCH ON ITS STDOUT for the ESCALATION directives. TWO shapes reach here and
# retrying can NEVER clear either, so "resume on local code" becomes PERMANENT
# staleness: a repeating content CONFLICT (g-306-315; cc-08 ran 85 commits
# behind retrying one conflict every cycle while the blocked merge concealed
# the peer fix g-306-308) and a repeating integrate DEFER on a dirty shared
# file (g-115-6934; cc-08 39 behind while origin already carried the fix).
# Fail-soft retry is right for transient shapes and wrong for these. Detection
# is bash-owned (guard-399) in iteration-push.sh's shape-aware defer-streak
# file, printing ONCE per streak; this branch owns the RESPONSE, same for both.
IF stdout contains "— ESCALATION REQUIRED (g-":
    # Do BOTH, then CONTINUE the cycle — the escalation IS the fix path; local
    # code stays runnable, and hand-resolving a shared-store wedge mid-goal is
    # exactly the improvised git the no-transcription contract forbids.
    1. Post the directive's one-line summary (the blocking path(s) it names,
       behind=N, this box's hostname) to the coordination board:
       board-post.sh --channel coordination --type escalation --tags
       "merge-wedge,<the g-NNN in the headline>". The board survives the
       partition the blocked merge IS (guard-997), so peers see the wedge even
       though this box's store writes cannot reach them.
    2. Append an sq-013 observation to the spark_capture WM slot (the sanctioned
       worker relay, Phase 3.5 shape) naming those path(s), behind count, box,
       and streak since-stamp — the reducer's spark replay files the Unblock so
       the wedge gets an OWNER, not just visibility.
    Do NOT stop the loop, do NOT git-merge or clear files by hand, and do NOT
    re-escalate on later cycles — the directive prints once per streak by
    design; its absence means either no repeat or already escalated.

# Phase -0.25 — PULL LATEST PRODUCT REPOS (g-306-370). A scoped CALL to the
# SAME estate pull the reducer path already mandates in the domain's
# pre-execution convention — never a hand-rolled fetch loop (no-transcription
# contract, guard-2676). That convention step is NOT removed; this is a SECOND
# entry point, because a gate is only as broad as its entry points (guard-3448).
#
# WHY IT EXISTS: the capability shipped 2026-08-20 and was wired ONLY into that
# convention — reachable from this loop solely by a five-link prose chain
# (Phase 3 -> load-execute-protocol.sh -> digest Phase 3.9 -> load-conventions
# -> the pull step), executed by a READER rather than a runner. This loop's own
# -0.5/-0.4/-0.3/-0.2 phases are literal Bash calls and run every unit; that
# chain did not. MEASURED 2026-08-26: a live worker session reached this point
# with 23 of 61 product checkouts behind origin, one by 13 commits — a repo the
# same session had ALREADY read source from. Earlier the same defect left a
# checkout 3 commits/3 days stale, returned pre-fix source to a grep, and nearly
# shipped a redundant change into an auto-deploying repo. Same shape as
# g-306-233 (a worker never pulled the FRAMEWORK), one surface over.
Bash: py -3 core/scripts/product-repo-freshness.py --pull
# Fail-open, and never branch on this rc. The network fetch is throttled per
# repo (stateless via FETCH_HEAD mtime), so the steady state is a rev-list per
# repo and nothing else. rc=2 `unrecognized arguments: --pull` means this box
# predates the flag: say so in one line and continue — do NOT hand-roll a
# substitute pull.
# READ THE OFF-DEFAULT LINES. A checkout parked on a feature branch is the one
# class this pull deliberately does NOT fix (fast-forwarding it advances the
# feature branch while the tree still lacks the default branch's content), so
# confirm any negative about such a repo's contents against its default branch
# before asserting it.

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
# rc 0 = CONTINUE to SELECT (and if this Body is PARKED, RESUME it first — see
#   below). rc 1 = PARK, which is a WIND-DOWN AND NOT A CLOSE (g-306-291).
#
# WHY PARK RATHER THAN CLOSE. The wind-down DECISION is right and unchanged: with
# no reducer, claiming and executing accumulates work nobody will ever merge. Its
# TERMINALITY was the defect. Measured 2026-08-14/15: a reducer stalled 15.7h, the
# worker closed durably, and when the reducer RETURNED THE WORKER STAYED CLOSED —
# `body_state: closed-pending-merge` makes Phase -0's gate refuse every further
# unit and only a user-only /start reopens it. Recovery cost is one human /start
# PER worker, and the incident's own email says it verbatim: "I cannot run it from
# a closed worker Body here." This spec's own words for that cost class: a wrong
# close "parks the remaining queue on a human who does not know they are needed"
# — quoted from the pre-g-306-291 wording, where `park` still meant STRAND. It
# means the opposite here. The close path now says "strands" for exactly that
# reason; this quote is left verbatim because it is a quote.
#
#   Bash: py -3 core/scripts/body-manifest.py park --sid "$MIND_SID" --agent "$MIND_AGENT"
#   Bash: python3 core/scripts/stop-reason-record.py --path worker-body-parked \
#           --reason "<the poll's reason field>" --agent "$MIND_AGENT"
# then arm an hourly re-poll and STOP:
#   ScheduleWakeup(prompt=<the park-resume prompt>, delaySeconds=3600)   # 3600 is the tool's clamp
# The JSON on stdout carries {verdict, reason, rc, consecutive_errors} — quote
# `reason` in the stop message so the wind-down cause is legible.
#
# THE RECORDER CALL IS NOT OPTIONAL BOOKKEEPING — it is what stops the fleet
# sweeper emailing the user that this box is DEAD. Measured against the installed
# `world/scripts/fleet-liveness-sweep.py`: `classify()` returns EXPECTED_IDLE on
# `session/last-stop-reason` BEFORE it reaches the heartbeat-age branch, and its
# `--stale-min` default is 45 while a park re-polls at 3600s. 60 > 45, so with no
# reason file a correctly-parked Body is DEAD_LOOP for ~15 minutes of every hour.
# That is the exact inversion the parking work exists to remove: the user must
# read "parked awaiting reducer", never "dead".
# It never emails — `worker-body-parked` is in the recorder's NO_NOTIFY_PATHS and
# is enforced INSIDE record(), so forgetting a flag here cannot mail the user
# hourly about a box that is fine and self-resuming.
#
# DO NOT STAGE THE WM WHEN PARKING, and this reverses what the goal that filed
# this change asked for ("the SAME durable handoff as today: board post, staged
# WM, pushed ref"). Staging queues the Body for merge, and a Body that is queued
# for merge and then keeps working is the exact hazard close-body-on-genuine
# exists to prevent, in its own words: the reducer "merges + marks `merged`, then
# the worker keeps diverging into a now-merged manifest that the sessions-pass
# never revisits." A parked Body intends to resume BY CONSTRUCTION. It is also
# pointless — the trigger for parking is that no reducer exists, so there is
# nothing to merge into for the whole park. Divergence is still safe: an abrupt
# death mid-park stages via the stale-binding path, and an EXPIRED park runs the
# ordinary genuine-close path, which stages and pushes through the one existing
# writer. `park` never stages; only a real close does.
#
# ON RE-ENTRY (the wakeup fires): Phase -0 sees `parked`, which is RESUMABLE and
# must not be read as closed. Re-run this poll. rc=0 ->
#   Bash: py -3 core/scripts/body-manifest.py resume --sid "$MIND_SID" --agent "$MIND_AGENT"
#   Bash: python3 core/scripts/stop-reason-record.py --clear --agent "$MIND_AGENT"
# and proceed to SELECT. rc=1 -> re-park (idempotent; the ORIGINAL parked_at is
# preserved, so the cap measures the whole park and not the last poll) and re-arm.
#
# CLEARING THE REASON FILE ON RESUME IS THE HALF THAT IS EASY TO DROP, and
# dropping it disables a detector rather than merely leaving litter. Every OTHER
# writer of that file stops the loop for good and hands recovery to `/start`,
# which clears it via `session-manifest-clear.sh` — a park→resume never goes
# through `/start`, so nothing else would ever remove it. A resumed Body would
# then work normally for days while the sweeper kept reading EXPECTED_IDLE,
# suppressing the alert for a LATER genuine death. `--clear` is idempotent and
# never raises, so it is safe on a resume that never parked through the recorder.
#
# THE PARK IS CAPPED at body-manifest.PARK_MAX_HOURS (60h). When
# `py -3 core/scripts/body-manifest.py park-expired --sid ... --agent ...` exits 0,
# stop re-parking and take the GENUINE close path below (touch the body-closing
# sentinel), recording it as a REAL stop — this one DOES email, because from a
# closed Body `/start` is user-only and a human now genuinely has to act:
#   Bash: python3 core/scripts/stop-reason-record.py --path worker-park-expired \
#           --reason "parked <N>h with no reducer; cap reached" --agent "$MIND_AGENT"
# A reducer absent that long is a human matter and the wind-down board post
# already went out. Expiry FAILS TOWARD STAYING PARKED: an unreadable or
# missing `parked_at` reports not-expired, because a wrong close is the
# unrecoverable direction and a long park costs only an hourly poll.
#
# ROUTE IT BEFORE YOU STOP (g-115-5274). Quoting `reason` in the stop message
# tells the terminal, and the terminal is about to end — so on rc=1 the fleet
# loses its reducer and the ONLY externally-visible trace is workers quietly
# ceasing to appear. Post FIRST, then park, then arm, then stop:
#   Bash: echo "<reason>, this Body PARKED awaiting reducer (hourly re-poll, auto-resume)" \
#           | bash core/scripts/board-post.sh \
#           --channel coordination --type finding --tags reducer-stall,body-parked
# POST ON THE FIRST PARK ONLY, not on every hourly re-park — an unconditional post
# emits ~24 identical messages per day per worker into the channel a human reads to
# find out the reducer is gone, which buries the signal it exists to raise. The
# manifest already carries the state; `park` returning `already-parked` is the tell.
# Say PARKED, not "winding down": the two words route a reader to opposite actions,
# and the whole point of g-306-291 is that nobody needs to be summoned to /start
# this Body — it resumes itself the moment a reducer returns.
# WHY THIS IS THE RIGHT PLACE and not a new detector: measured 2026-08-08, the
# reducer is ALREADY observable cross-box by two independent means — its per-SID
# body-heartbeat carrier (heartbeat-tick.sh:95-97 writes it for EVERY Body
# INCLUDING the reducer, and it is peer-readable), and the runner-claim endpoint,
# which publishes agent_state + heartbeat age cross-box and which THIS VERY POLL
# already reads every cycle. Detection was never the gap. The gap is that no
# layer converts an observation into something a human or a queue sees: Layer C
# (trailing-text-detector.py) has exactly ONE runtime caller, stop-hook.sh:476,
# and the Stop hook is what a text-death prevents from firing; and the peer-side
# WorkerStallProbe runs ON the reducer, so when the REDUCER is what died the
# reporter is the corpse. This line is the one place a live process holds the
# fact that the reducer is gone.
# The board post is a Bash call, so it does not disturb either terminal shape.
# THE TWO SHAPES DIFFER AND THE DIFFERENCE IS THE WHOLE FEATURE: a genuine CLOSE
# ends on a Bash echo and does NOT arm a wakeup (nothing should wake a closed
# Body); a PARK ends on `ScheduleWakeup(<park-resume prompt>, 3600)` and nothing
# after it, because that wakeup IS the auto-resume. A parked turn that forgets to
# arm is indistinguishable from a closed one and needs the same human /start this
# change exists to remove. Fail-open: a failed post must never block the park.
#
# NEVER-PROMOTE is the invariant the poll turns on: no rc yields "become the
# reducer". Every ambiguous signal resolves toward wind-down, because winding
# down loses nothing (the Body's divergent WM stays live and unstaged across the
# park, and reaches the reducer at the eventual close — genuine, expired, or the
# stale-binding path) while continuing without a reducer accumulates work that is
# thrown away. Parking makes this cheaper still: the cost of a WRONG wind-down
# used to be a human /start per worker and is now one hourly poll, so the bias
# toward winding down is safer than it was when this line was written.
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
#
# SKILL ELIGIBILITY (g-115-5664). "Eligible" includes the goal's SKILL, and the
# scorer does not know that. Measured 2026-08-10 on cc-08: goal-selector offered
# g-001-05 "Run hippocampal replay" (skill `/replay --sharp-wave`) as the top
# pick, with the drain-lane banner reading verbatim "This IS the sanctioned top
# pick — claim it without a deviation code." `/replay` calls guardrails-add.sh
# and LIFECYCLE_DISPOSITIONS["replay"] is reducer-only-by-design, so a worker
# following that banner writes guardrails from its OWN UNMERGED state — the
# Nth-reducer defect — and the artifacts land in the shared world with nothing
# marking them pre-merge. It was caught only by opening the skill before
# claiming; nothing in this loop prompted that.
#
# So: for each candidate IN RANK ORDER, ask the contract before claiming.
Bash: py -3 core/scripts/worker_execute.py skill-eligible <the goal's skill field, verbatim>
# rc 0 = eligible -> proceed to CLAIM. rc 1 = reducer-only -> SKIP THIS GOAL and
# take the NEXT candidate in the same pass (the reason is on stderr — say it out
# loud in the turn; a silent skip is the half of this that would rot). Do NOT
# burn a select cycle per refusal, and do NOT file anything: the goal is not
# broken, it is simply the reducer's, and it stays visible to the reducer where
# it belongs.
#
# The check is a scoped CALL into the shared component (guard-2676). The refusal
# list is NOT duplicated here and must not be: worker_execute derives it from
# LIFECYCLE_DISPOSITIONS, so a stage whose disposition changes moves its skills
# with it. `reducer-only-skills` prints the current set if you want to see it.
#
# WHY THIS IS NOT IN goal-selector, where the starvation half would also be
# fixed: LIFECYCLE_DISPOSITIONS["select"] forbids worker-specific selection logic
# outright, and guard-2783 forbids role-conditional behavior in a component BOTH
# roles run. The scorer stays byte-identical for both roles.
#
# A GREEN ANSWER IS NOT A PROOF, AND ON A SKILL-LESS GOAL THERE IS NO ANSWER.
# The bridge is SKILL-keyed, so a goal with no skill (919 of 938 candidates) now
# returns "NOT EVALUATED ... NOT a cleared check" at rc 0: eligible stays True
# because fail-closed would strand the role, but the bridge DECLINED to judge and
# the call is YOURS (g-115-6523). On that answer — and whenever a named skill
# looks like loop-phase encoding over YOUR OWN unmerged experience — read the
# goal's verification outcomes and description BEFORE claiming. Work that ENCODES
# to tree/reasoning-bank/guardrails, RESOLVES a hypothesis, drains a capture lane,
# consumes worker refs, pushes main, or writes the agent-wide working-memory.yaml
# is REDUCER-ONLY: release it, take the next candidate. SKILL_LIFECYCLE_STAGE is
# the remedy for a NAMED skill only — a skill-less goal has no key to add. The
# line that matters is loop-phase encoding (forbidden) vs goal-directed artifact
# creation from content supplied in the goal (`/tree` is pinned for that reason).
IF no goal: this is a GENUINE close — the worker has exhausted its work. Write the
  body-closing sentinel so the stop-hook (Phase-2B producer) marks this Body
  closed-pending-merge for the reducer to merge at generalize-down, then STOP:
    Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"

  THE CLOSE CONDITIONS ARE EXHAUSTIVE — THERE ARE EXACTLY TWO, and they are the
  only two places in this file that write the sentinel: THIS one (SELECT returned
  no eligible goal) and Phase 0.5's EXPIRED park. A user stop is the third path
  and is not yours to initiate. Anything else is an INVENTED stop condition
  (guard-3479).

  NARROWED, NOT WIDENED, BY g-306-291 — read this before assuming the count grew.
  The second condition used to be Phase 0.5's rc=1 wind-down ITSELF. rc=1 now
  PARKS (resumable, hourly re-poll), so it no longer closes anything; only the
  60h park CAP closes, and it closes through this same sentinel. So there are
  still exactly two sentinel writers and the invented-close conservatism is
  untouched — parking is available ONLY on rc=1, and it must never acquire a soft
  edge for anything else. SELECT-exhausted (this branch) still closes DURABLY and
  immediately: a worker with no work is finished, not waiting on anything.
  ** CONTEXT PRESSURE IS NOT A CLOSE CONDITION. ** Not "context is filling up",
  not "the session has run long", not "I have done N units", not "the next goal
  will not fit". Autocompact exists to make a long session survivable and this
  loop is built to run indefinitely, so nearly-out-of-context is a reason to
  enter the next unit, not to stop. This mirrors stop-hook-compliance.md rules
  3-4, which said it for the AUTONOMOUS loop only — nothing carried it to this
  close path until 2026-08-11, when a Body closed itself after 12 units with
  ~930 candidates still in SELECT and a live reducer, then wrote a persuasive
  board post explaining why. That post is the SIGNATURE of the defect, not
  evidence against it: a capacity close always feels responsible from inside.
  IF A SPECIFIC GOAL GENUINELY WILL NOT FIT, the move is to RELEASE THE CLAIM
  UNSTARTED and keep looping — `aspirations-release.sh <goal-id> --source
  <world|agent>`, then VERIFY by re-reading that the record shows
  status=pending / claimed_by=None (the release echo is not proof). Never close
  the Body for it.
  WHY THIS IS NOT A RECOVERABLE MISTAKE, which is what makes it worth a
  paragraph: the sentinel stages this Body's WM snapshot, Phase -0's closure
  gate then REFUSES every further unit on this SID, and `body_state:
  closed-pending-merge` is DURABLE — so nothing you can do re-opens it. Resuming
  requires a fresh `/start`, which is user-only. A wrong close does not cost an
  iteration; it STRANDS the remaining queue on a human who does not know they
  are needed. (The original wording was "parks the remaining queue" — reworded
  by g-306-291 because `park` now names the RESUMABLE state two blocks up, and
  the whole point of that state is that it strands nobody. Two opposite meanings
  for one word, in the file that defines it, is a reading trap.)
  (The reducer generates work, not the worker. A worker does not INVENT an agenda —
   but "never files a goal" is too strong and was ruled on: see "May a worker file a
   goal?" below. Do NOT file here regardless; SELECT finding nothing is the close
   edge, not a moment to manufacture work.)

# Phase 2 — CLAIM (claimed_by stays the mindKey/agent-name — same claim contract)
Bash: aspirations-claim.sh <goal-id> <agent>
IF claim conflict: abort this goal, loop to SELECT.

# Phase 2.9 — READ THE RECORD YOU WERE JUST HANDED (g-115-6695). The claim
# response IS the full goal record. The loop's only OTHER prompt to touch
# these fields is the one that WRITES them (3.9), so skipping it puts the read
# after work it would prevent. Workers are MOST claims.
READ EVERY narrative field: `outcome_note`, `outcome_notes` (plural, guard-3512),
  `progress_note`, `description`. An empty outcome_note is NOT an untouched goal
  — prior work hides in progress_note (g-364-54). Treat it as a measurement to
  VERIFY, not repeat; if it landed, close or release per Phase 4a.
# Rationale: core/config/rationale/worker-claim-outcome-note-read.md
# Phase 2.95 — UNIT CLAIM (g-306-322). The machine-checkable half of 2.9: a goal whose
# own text says one unit per pass is NON-TERMINAL, so each Body claims it, does
# ONE unit, and RELEASES. The GOAL claim is free between units and nothing
# records which UNIT is in flight — two Bodies built the same template and one
# full unit was wasted. A better handoff note cannot fix this (it is a MAGNET:
# it steers every reader to the same unit).
IF the goal's text instructs one-unit-per-pass ("one at a time", "one PR each",
  multi-unit) — BEFORE writing any code, name your unit and claim it:
  Bash: bash core/scripts/unit-claim.sh acquire <goal-id> <unit-token>
  rc=1 REFUSED — another Body holds that unit. Pick a DIFFERENT unit; if none is
  free, release the goal claim. Never --force past a live holder. Release your
  unit when the unit ends (either close edge) at Phase 4a:
  Bash: bash core/scripts/unit-claim.sh release <goal-id> <unit-token>

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
# sq_trigger is the ROUTING KEY on the reducer side, not decoration. Two values
# matter most (2026-08-16, goal-completion audit D1):
#   "sq-013" — this observation is WORK someone must own (a defect, a follow-up,
#              a capability gap, a dependency): the Case-B relay of the filing
#              ruling below. The reducer's Worker Spark Replay runs the sq-013
#              work-discovery handler over sq-013 relays and FILES the goal
#              (dedup first). Shape the observation as a filing, not a musing:
#              what is wrong / needed, where (path:line, script, store), the
#              evidence you measured, and a one-line suggested title. Before
#              this date the relay reached only the lesson handlers (rb /
#              guardrail / gotcha) and NEVER became a goal — the ruling's
#              "loses nothing but time" was false; it lost the work.
#   null / another sq — a lesson (how to work, a gotcha, a pattern): the
#              learning handlers encode it; no goal is filed from it.
# A finding that is BOTH a lesson and work gets TWO entries with the two
# triggers; the reducer routes each. One entry cannot carry both.
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

# Phase 3.65 — HYPOTHESIS-EVIDENCE CAPTURE (g-306-200). Third capture lane, and
# the one with the narrowest trigger. Numbered 3.65 rather than appended after 3.8
# because it belongs with its siblings: 3.5/3.6/3.65 all WRITE to the Body WM,
# while 3.7/3.8 are about getting outputs OFF this box.
#
# CONDITIONAL, like 3.5 and unlike 3.6. Write an entry ONLY when execution
# surfaced evidence bearing on a hypothesis that ALREADY EXISTS in
# world/pipeline.jsonl — a prediction this unit confirmed, contradicted, or
# sharpened. Most units surface none; an empty lane is the correct output of a
# unit that touched no hypothesis, and inventing one to fill the slot is worse
# than silence because it manufactures evidence the reducer will act on.
#
# WHY IT IS A SEPARATE SLOT rather than a field on the 3.6 entry: an exp_capture
# entry is a narrative the reducer encodes an experience FROM, whereas this is
# EVIDENCE INPUT to the EXISTING /review-hypotheses resolution protocol, keyed to
# a specific hypothesis_id. Merging them would force that protocol to re-derive a
# classification the writer already knew.
#
# THE WORKER DOES NOT RESOLVE, AND THAT IS THE WHOLE DESIGN. Resolution runs the
# full protocol on the reducer (pipeline-move.sh / pipeline-update-field.sh); a
# worker resolving from its own unmerged state is the Nth-reducer defect. Supplying
# evidence and resolving are different acts, and only the first is yours — which is
# also what makes the no-double-resolution guard expressible: the reducer can see
# that evidence was already supplied for a hypothesis before it resolves
# independently.
#
# hypothesis_id MUST name a real pipeline.jsonl record — check before writing.
# An id that matches nothing is worse than no entry: it survives the merge, reaches
# the reducer, and cannot be joined to anything, so it reads as a broken protocol
# rather than as a worker mistake.
Bash: echo '{"goal_id":"<goal-id>","hypothesis_id":"<YYYY-MM-DD_slug from world/pipeline.jsonl>","evidence_summary":"<what execution actually showed, in enough detail for the reducer to resolve from without this session>","surprise_level":<0-10>,"confirms_or_contradicts":"<confirms|contradicts|partial>","suggested_resolution":"<your read, explicitly NON-binding — the reducer runs the full protocol>"}' | bash core/scripts/wm-append.sh hyp_capture
# goal_id is REQUIRED for the same content-hash reason as 3.5/3.6, and it does more
# work here: two units supplying evidence on the SAME hypothesis_id would otherwise
# be at risk of collapsing into one entry, which is exactly the case where losing
# the second observation most distorts the resolution.
# suggested_resolution is a READ, not a verdict. The reducer may disagree with it
# on the same evidence; if this field ever starts being applied as-is, the lane has
# become a second resolver and the guard above has failed.

# Phase 3.66 — ENCODING CAPTURE (g-306-202). Fourth and last capture lane, and
# the hardest to tell apart from 3.5 — so lead with the discriminator rather than
# the rationale. The axis is learning-routing.md's, not a stylistic one:
#   3.5  spark_capture     = a LESSON about HOW TO WORK    -> reducer routes to rb / guardrail
#   3.66 encoding_capture  = a FACT about THE WORLD        -> reducer routes to a tree node
# "The producer's count is not the consumer's enumeration" is a spark. "InboxWatch
# counts one bucket, so agentInboxCount under-reports by 41" is an encoding. The
# same work unit routinely yields one, both, or neither.
#
# CONDITIONAL, like 3.5 and 3.65 — but do NOT expect it to be usually empty. That
# expectation was written into this comment when the lane shipped, on a RETROSPECTIVE
# count (alpha, cc-08, 2026-08-11: 0 of 6 spark observations from the units BEFORE
# this lane existed were tree-worthy), and the author's own next three units
# falsified it: 4 encoding_captures in 3 units, on the same box, the same day.
# The retrospective count was biased by construction — those observations were
# WRITTEN as sparks, by an agent with nowhere else to put them, so counting how
# many "were really facts" measures the old lane's framing, not this lane's yield.
# Beware re-deriving an expectation from data collected before the thing existed.
#
# The retirement condition still stands, unchanged and worth keeping (it is what
# stops this lane being defended out of sunk cost): if a later audit finds it
# genuinely empty across many sessions while tree nodes keep being encoded from
# goal records, then goal records are the real bridge and this lane should be
# RETIRED, not defended (learning-philosophy.md rule 5). What changed is only the
# prior — current evidence points the other way, so an empty lane is a signal to
# look at, not the expected default.
#
# Do NOT write the tree node here, and do not reach for /tree. Tree encoding is
# aspirations-state-update Step 8, reducer-only-by-design; a worker that encodes
# from its own unmerged state is the Nth-reducer defect. Note /tree IS pinned
# worker-eligible in SKILL_ELIGIBLE_DESPITE_ENCODING — that pin is for
# goal-directed artifact creation from content supplied IN THE GOAL, and using it
# to encode your own session's findings is exactly the misread it warns about.
FOR EACH tree-worthy domain fact this unit established (usually 0):
    Bash: echo '{"goal_id":"<goal-id>","category":"<goal.category>","fact":"<what is now known to be TRUE about the world, stated so a reader who was not here can act on it>","evidence":"<the measurement that establishes it — command, output, count, or path>","suggested_node":"<tree path if you know one, else null — NON-binding, the reducer decides placement>","supersedes":"<node/claim this corrects, or null>"}' | bash core/scripts/wm-append.sh encoding_capture
# goal_id is REQUIRED for the same content-hash reason as 3.5/3.6/3.65.
# `evidence` is what makes this worth more than an assertion: the reducer writes
# the node later and cannot re-measure what it never observed, and a tree node
# asserting a fact with no traceable measurement is the drift these captures exist
# to prevent.
# `supersedes` is the field that earns this lane its keep. A fact that CORRECTS an
# encoded belief is the highest-value thing a worker can hand up, and it is
# precisely what a free-text spark buries — the reducer would have to notice the
# contradiction on its own, which is the re-derivation this split exists to avoid.
# REGISTRATION IS LOAD-BEARING, AND IT IS TWO FILES, NOT ONE: encoding_capture is
# in ARRAY_SLOTS in BOTH core/scripts/wm.py AND mind_api/src/endpoints/wm_write.py.
# An unregistered slot is NOT refused (wm-append accepts any string as a slot name,
# rc=0, no validation) — it is silently NULLED by cmd_maintain's scalar eviction at
# 120 min while the Body waits for consolidation, so the loss lands exactly where
# nobody is watching.
# The DAEMON copy is the LIVE one (guard-742/547): wrappers are daemon-only, so
# wm-append routes to wm_write.py and the eviction predicate that decides survival
# is read from THERE. Editing wm.py alone changes NOTHING at runtime while looking
# entirely correct in the diff — measured while adding this lane, and caught only
# because test_wm_reset_cadence.py::test_shared_wm_constants_parity_with_daemon
# failed. Adding a fifth lane means editing BOTH sets; that parity test is what
# makes forgetting loud, so never skip it when touching this.

# ---- `load_bearing` — the ONE optional field all four capture lanes share ----
# (g-306-293.) Add `"load_bearing": true` to ANY capture entry in 3.5/3.6/3.65/3.66
# when it SUPERSEDES or CONTRADICTS an existing encoded conclusion, or unblocks a
# queued decision. Omit it otherwise — the default is false and most entries are.
#
# It buys two things, and the second is why the field exists at all:
#   1. PRIORITY MERGE. core/scripts/capture_fast_lane.py runs on the reducer once
#      per iteration (iteration-close --phase productivity-check) and copies
#      flagged entries into the reducer WM WITHOUT waiting for consolidation. It
#      reads ACTIVE Bodies too, which the full generalize_down cannot: that pass
#      enumerates only closed-pending-merge Bodies, so an active worker's captures
#      are invisible to the reducer however often consolidation runs.
#   2. EVICTION EXEMPTION. At cap, wm append FIFO-drops the OLDEST entry — exactly
#      the one that has waited longest, i.e. the one a priority lane exists to
#      rescue. Flagged entries sort last and are popped last. Measured on ONE
#      active Body (alpha, cc-08, 2026-08-15, 21 units): 237 entries destroyed
#      (spark 144, exp 74, hyp 19) against caps of 50/20/10 — ~74% of everything
#      spark_capture was handed. Second instance of the g-306-289 measurement
#      (215 on cc-07), so this is the rule, not an outlier.
#
# DO NOT FLAG EVERYTHING. The cap holds when every entry is flagged (a cap a
# writer can defeat is not a cap), so blanket-flagging preserves nothing: it
# restores FIFO among the flagged and destroys the triage signal. ~1 of 6 is
# healthy. The flag is honor-system; a mis-flag costs priority, nothing else.
#
# KNOWN COST, ACCEPTED (g-306-361): at saturation an honest UNFLAGGED append
# destroys an unrecoverable peer, a flagged one a carried duplicate. Flag
# honestly anyway. Measured: re-ordering the victim moves ~10% of losses; the
# lever is that a Body lane never DRAINS (the carrier copies, never clears).

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
# THIS MERGES TOO (fetch + integrate BEFORE the push), so it voids a live
# full-suite run exactly as Phase -0.3 does — the flags differ on PUSH, never on
# MERGE. Same constraint, same rationale file as Phase -0.3.
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

# Phase 3.9 — CLOSURE EVIDENCE (g-115-5158). A scoped CALL to the SAME producer
# the reducer uses — never a worker-local write (no-transcription contract,
# guard-2676 / g-306-212).
#
# WHY IT EXISTS: closure evidence is the `outcome_note` field, and exactly one
# thing produced it on the close path — iteration-close do_verify Step 3, which
# a worker skipped entirely until 2026-08-16 (Phase 4a below calls do_verify
# with a ONE-LINE --summary; it passes --no-supersede on the worker path, so
# THIS phase stays the rich-narrative producer and 4a only backfills when
# this phase did not run — g-115-6633). Before that a worker had
# NO producer at all. Its notes were written by hand or not at all, which means
# the rate was DISPOSITIONAL, not mechanical. Measured 2026-08-09 on asp-115: the
# live worker sat at 48/48 and every other SID at 24/26, so no asymmetry was
# visible — and that is exactly the trap. One disciplined agent's 100% says
# nothing about the next Body, and arming any enforcement gate on outcome_note
# would have refused 100% of worker closures while passing reducer ones,
# MANUFACTURING the disparity it was meant to remove. Fifth instance of the
# inheritance gap (workers never pulled, skill-dedup, deadman, watchdog --tick).
#
# RUNS AFTER 3.7/3.8 ON PURPOSE. Phase 3.7's STRANDED branch also writes
# outcome_note, and this helper is write-if-absent/never-clobber — so placing
# this before 3.7 would silently prevent a stranding from ever being recorded.
# Ordered this way, a stranding note wins and this call declines, which is the
# correct precedence: a stranded output is the more urgent artifact.
Bash: bash core/scripts/closure-evidence-write.sh --goal <goal-id> --source world \
        --summary-file <path to the narrative you already wrote> \
        --prefix "[worker-loop] close:"
# --summary-file, not --summary: an inline summary goes through the shell and a
# narrative containing $(...) or backticks is mangled before the script sees it
# (pinned by test_verify_summary_to_outcome_note.py). Write the narrative to a
# file under agents/<agent>/temp/ and pass the path.
# NEVER CLOBBER: if you already wrote the outcome_note directly this unit (the
# habit this phase replaces), the call announces the skip and changes nothing —
# it is idempotent and safe to run either way.
# Fail-open by contract: it always exits 0 and the LLM must not branch on the rc.
# It does NOT set status, does NOT close the goal, and does NOT clear in_flight
# (g-306-132-d) — the status write is Phase 4a's job, immediately below.

# Phase 4 — CLOSE THE WORK UNIT: record the outcome on the goal, then hand off.
#
# 4a. RECORD THE STATUS YOU JUDGED, through the SHARED close writer. Until
#     2026-08-16 this phase said only "do not run verify" and left the goal at
#     in-progress "for the reducer to close at generalize-down" — but no reducer
#     lane ever flipped a worker goal's status (worker_retrospective.py has no
#     close lane; body-merge.py only NAMES ids), so every worker completion
#     stayed in-progress forever. Measured 2026-08-16 (alpha reducer, cc-04):
#     360 of 361 open alpha claims were finished work nobody closed, held by 7
#     worker SIDs, 261 by dead bodies; the parent aspirations never completed, no
#     successor goals were generated, and goal-selector's SKIP_STATUSES hid every
#     one of them from every Body (g-115-6337; guard-4000 class — a KEEP that
#     never consults age grows without bound).
#     The status flip is NOT the LLM verify skill (/aspirations-verify Q1/Q2/Q3
#     stays reducer-only per LIFECYCLE_DISPOSITIONS "reducer-iteration"). It is
#     the MECHANICAL close writer, do_verify in iteration-close.sh, entered as a
#     scoped call (guard-2676: call the component, never transcribe it). That
#     one call already routes recurring goals through aspirations-complete-by.sh,
#     stamps completed_date + outcome_class, posts the "Completed:" board message
#     the reducer and partners read, and clears in_flight ONLY when the row names
#     THIS goal (--if-goal compare-and-swap, so a live reducer's row is never
#     blanked). Its checkpoint write routes to THIS Body's sessions/<sid>/ dir
#     (body_state_path), not the agent-wide one.
Bash: bash core/scripts/iteration-close.sh --phase verify --goal <goal-id> \
        --status <completed|blocked|skipped> --source <world|agent> \
        --outcome <deep|routine> --summary "<one line: what this unit did>"
#     --status is YOUR judgement of the unit you just executed — the same
#     caller-declared contract the reducer honours (do_verify refuses to infer it
#     from disk). Pick by what happened, and note that two of the three are
#     TERMINAL — a wrong pick here loses work, not time:
#       completed — the goal's verification outcomes are met by evidence your
#                   Phase 3.9 note cites. The ONLY status that means "done".
#       blocked   — the unit hit an unfixable external blocker AND CREATE_BLOCKER
#                   (execute protocol Phase 4.0/4.1e) already put blocker evidence
#                   on the record. Without a blocker_ref / blocked_by the daemon
#                   REFUSES the write (400 blocker_ref_required_for_blocked_status)
#                   and do_verify aborts loudly — that refusal is correct; file the
#                   blocker first, or take the release path below.
#       skipped   — the goal is MOOT (premise false, already done elsewhere,
#                   superseded) and should never be executed. Terminal. It is
#                   NOT the status for "I did not finish".
#     DID NOT FINISH, GOAL STILL VALID (a partial unit, a precondition that
#     failed mid-way, a gate you found but cannot pass): do NOT call 4a at all.
#     RELEASE the claim so the next Body starts from your note —
#       Bash: bash core/scripts/aspirations-release.sh <goal-id> --source <world|agent>
#     and when a NAMED gate remains (elapsed time, a deploy, a partner's leg),
#     write the structured defer in the SAME step, never a bare release —
#     g-115-5177: a bare release re-arms finished work at rank 1 on fresh
#     metadata:
#       Bash: bash core/scripts/aspirations-update-goal.sh --source <world|agent> <goal-id> defer_reason "precondition_unmet: <the gate, short>"
#     And if Phase 3.7 returned rc 1 (STRANDED) it has ALREADY told you to leave
#     the goal in-progress with the stranding recorded: obey it and SKIP 4a.
#     --outcome: routine for a presence-check / cadence unit, deep for anything
#     that changed code, framework or knowledge — the reducer's own rule.
#     --summary is one line for the board post + diary breadcrumb; the full
#     narrative already landed as outcome_note in Phase 3.9 (do_verify's note
#     write is write-if-absent, so it declines and nothing is clobbered).
#     Daemon-side close gates still apply to the status write (uncommitted-work /
#     missing-artifact / residual). Pass the matching --override-* flag ONLY with
#     a real justification, exactly as the reducer would — never to get past it.
#     do_verify's terminal stdout line is body-aware: as a worker you get
#     "NEXT (worker Body): ... do NOT invoke Skill(aspirations-spark)" — your
#     spark obligation was Phase 3.5 spark_capture. If you ever see the
#     reducer's "Phase 6 spark REQUIRED" wording instead, BODY_ROLE was not in
#     your env for that call; still do not run the spark phase (4c below).
#
# 4b. HAND-OFF ROW. Append the completion row body-merge.py reads
#     (`_completed_goal_ids` -> `merged_goal_ids` -> worker_retrospective.py).
#     Without it merged_goal_ids is ALWAYS empty and the consolidate Step -0.9
#     retrospective (team-state / journal / findings / experience / imp@k lanes)
#     has nothing to run over — this file carried 0 references to the slot
#     before 2026-08-16, so that lane had never fired once. Same row shape as
#     aspirations-state-update Step 3 (goal-selector reads these keys — do not
#     rename); omit work_class when the goal record has none. Only for
#     status=completed. Routes to the Body WM (BODY_WM_PATH), never agent-wide.
Bash: echo '{"goal_id":"<goal-id>","aspiration_id":"<aspiration-id>","recurring":<true|false>[,"work_class":"<class>"]}' | bash core/scripts/wm-append.sh goals_completed_this_session
#
# 4c. Do NOT run spark / state-update / learning-gate / productivity-check. The
#     worker's divergent WM + the now-CLOSED goal record are the hand-off; the
#     reducer merges the WM at generalize-down and runs the encode/reflect/
#     consolidate phases over the merged result.
#
# team-state in_flight: 4a's --if-goal clear is the ONLY clear you perform, and
# it fires only when the shared row names this goal. Do NOT add an unconditional
# clear (g-306-132-d): Phase 2's claim WRITES in_flight, and in_flight is
# AGENT-keyed with no sid, so a worker and its reducer share one row — an
# unconditional clear would blank a live reducer's row, worse than the stale row
# it fixes. The stop-hook additionally calls worker_close_in_flight_clear.py after
# a genuine close (result marked/marked-push-failed); it clears ONLY when the goal
# named by the live in_flight row carries THIS Body's claimed_by_sid, and a second
# hand-rolled clear on this path would defeat that ownership test.
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
# the reducer's full-loop re-entry. Every CLOSE path (Phase 1 genuine close, an
# EXPIRED park, user stop) still ends the turn with a Bash call after its sentinel
# work, exactly as before — self-continuation never overrides a close edge. The
# Phase 0.5 PARK is not a close and takes the third shape: it ends on
# `ScheduleWakeup(<park-resume prompt>, 3600)` and nothing after it.
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
| **B. New scope, observable by any Body** — a framework defect, a mislabelled field, a stale predicate | the agent-queue claim defect (g-306-238) | **DO NOT FILE. Relay** via `wm-append.sh spark_capture` **with `sq_trigger: "sq-013"`** and a filing-shaped observation (Phase 3.5), and post to the findings board if it is time-sensitive. The reducer's Worker Spark Replay runs the sq-013 handler over sq-013 relays and files the goal at its next iteration (2026-08-16); a relay WITHOUT that trigger reaches only the lesson handlers and never becomes work. Since the reducer's replay is the consumer, the relay costs time (measured 12.5h on g-306-238) — not the work. |
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

**WITNESSED 2026-08-18 — this paragraph said the opposite until then, and the
correction is the point.** From g-306-239's filing (2026-08-06) until now the
gap rested on the grep alone (this file had 0 mentions of ScheduleWakeup) and
this line read "**no worker text-death has ever been observed**". That is now
FALSE. An alpha WORKER Body on cc-07 (SID d1aec55b, goal g-250-351) died on a
trailing-text turn-end and was resurrected by its own net — so the failure mode
is real, the net WORKS, and neither half was known before. The net's LATENCY is
the residual defect: armed at `delaySeconds=600`, it delivered **6h49m** later.
That latency finding is owned by **g-115-6629** — do not re-file it. What this
file owes a reader is only the corrected fact: cite THIS incident, not cc-08.

The cc-08 retraction below still stands and is still load-bearing — a witnessed
text-death does not retroactively make an auth-loss stall into one, and the
`CTX: 0%/0% [fresh]` tell is how you tell them apart. g-306-239 was originally
filed citing a cc-08/foxtrot outage as the measured instance; that attribution
was **retracted
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
| A **PARK** — Phase 0.5 rc=1, reducer gone, park not expired (g-306-291) | **`ScheduleWakeup(<park-resume prompt>, 3600)` ALONE, as the last call, with NO `Skill(worker-loop)` after it.** This is the one terminal shape that is neither the pair nor a bare echo, and the asymmetry is deliberate in both directions. No Skill: re-entering now would re-run the poll that just said "no reducer" and spin. No 600s net either — **the platform keeps ONE pending wakeup (replace-slot), so the park poll IS the net**; arming both would leave whichever came second, and a 600s worker-net firing on a parked Body is the wedge the resurrection prompt now branches for explicitly. A park turn that forgets to arm is indistinguishable from a close and needs exactly the human `/start` this change exists to remove — and in THAT case only, the previous unit's 600s net is still in the slot (unreplaced, precisely because this turn never armed over it) and becomes a real backstop rather than a nuisance: `deadman-directive.sh` teaches it to read `parked` as RESUMABLE and re-arm at 3600 (pinned by `test_worker_prompt_treats_parked_as_resumable_not_closed`). On a park that DOES arm correctly there is no 600s net left to worry about — the 3600s poll replaced it. The stop-hook worker-net stands down on the parked manifest (`gate=worker-net-body-parked`), so this turn-end is ALLOWed rather than BLOCKed into a sentinel ceremony that would durably close the Body. |
| A close path — Phase 1 genuine close (sentinel just touched), an EXPIRED park, or a user stop | Bash echo stating the close reason. **Do NOT arm the net here** — the turn genuinely ends; stop-hook Phase 2B consumes the sentinel and stages the WM. A net armed by the PREVIOUS work unit is still pending and will fire ~600s later; that firing is benign because THREE layers read the DURABLE closure record, `sessions/<SID>/body-manifest.yaml` `body_state`: the resurrection prompt checks it FIRST and declines to resume (and does not re-arm) when it is in the CLOSED SET — `closed-pending-merge` / `merged` / `closed-stale`, NOT merely "not `active`", since `parked` is non-active and resumable; Phase -0's closure gate refuses a work unit on the same read; and the stop-hook worker-net stands down on it (`gate=worker-net-body-closed`). The `body-closing` SENTINEL cannot serve this purpose — close-body-on-genuine CONSUMES it on every genuine-close branch, so after a completed close its absence is indistinguishable from "no close ever happened". (The pre-2026-08-09 prompt read exactly that as "resume", and the worker-net BLOCKed every post-close turn-end into a second sentinel ceremony — measured cc-08 04:39→04:49.) This is the worker's equivalent of the reducer's safe landing (whose resurrected turn self-aborts at Phase -1.5 on `agent-state != RUNNING`). |
| Consulted for the phase split only (no work unit ran) | The `worker_execute.py` Bash call whose output answered the question. |
