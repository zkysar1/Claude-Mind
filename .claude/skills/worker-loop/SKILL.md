---
name: worker-loop
description: >-
  The simplified per-Body execution loop a forked WORKER Body runs (Mind/Body
  convergence Phase 2, asp-306). select -> claim -> execute -> RE-ENTER for the
  next work unit; parks (resumable) when work or the reducer is gone, closes
  only when a park expires.
  Verifies and closes the ONE unit it just executed (Phase 4a: /aspirations-verify
  scope=own-unit, then the shared close writer), then SKIPS the reducer-only
  phases (the cross-Body verify residue / encode / reflect / state-update /
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
**select -> claim -> execute -> re-enter** — one work unit per pass, parking
(resumable, hourly re-poll) when work or its reducer is gone, leaving its
divergent working-memory for the single reducer to merge later. It verifies ONLY
the unit it just executed (Phase 4a, `scope=own-unit`); it does NOT encode,
reflect, update state, run the learning gate, evolve, or do completion review.
Those are **reducer-only**: the one Body holding `running-session-id`
(the reducer) applies them to the MERGED state of every Body at generalize-down
(Phase 1C `body-merge.py`, run from `aspirations-consolidate` Step -1). Running
encode/reflect per-worker would create N reducers — the defect the convergence
forbids.

**Activation status:** ACTIVATION IS LANDED (a live worker executed a real goal
via this loop). Dated history, and the multi-body items still open as of
2026-08-05: core/config/rationale/worker-loop-contract.md. Design SSOT: the
`mind-engine-identity-bridge` tree node (Phase 2).

## The phase split (authoritative: `worker_execute.py`)

The phase contract is owned by `core/scripts/worker_execute.py`, NOT duplicated
here, so the worker and its tests agree on one source of truth:

```
Bash: py -3 core/scripts/worker_execute.py phases               # the phases this loop RUNS
Bash: py -3 core/scripts/worker_execute.py reducer-only-phases  # the phases this loop SKIPS
Bash: py -3 core/scripts/worker_execute.py should-run-phase <p> # exit 0 = run, exit 1 = skip
```

A worker runs ONLY those. `verify-own-unit` is WIRED as of g-306-417: Phase 4a
invokes `/aspirations-verify` with `scope=own-unit` for the goal it just
executed, then closes it. Every reducer-only phase (`verify`, `spark`,
`complete-review`, `state-update`, `evolution`, `learning-gate`,
`productivity-check`) still returns `skip` — `verify` there is the CROSS-BODY
residue (streaks, the sampled review of these self-graded closures): a
different scope, not a contradiction.
Rationale: core/config/rationale/worker-verify-own-unit.md

## The lifecycle split + the no-transcription rule (g-306-212)

The phase split above answers "which PHASES does a worker run"; this answers
"what does a worker do at each session LIFECYCLE stage". The defect class it
prevents is a reducer lifecycle stage with **no declared worker disposition**,
found one surprise at a time (history: core/config/rationale/worker-loop-contract.md).

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
# Phase -0-stop: USER STOP STANDS DOWN — FIRST, before role, closure and the
# park orbit (g-115-9461). A worker /stop arms sessions/<SID>/stop-requested;
# nothing on the RE-ENTRY path read it, so an armed wakeup resumed a stopped
# Body. Incident, measurements, and why park-due needs no second copy:
# core/config/rationale/worker-stop-standdown.md
Bash: test -n "$MIND_SID" && test -f "agents/$MIND_AGENT/sessions/$MIND_SID/stop-requested" && echo "user-stop" || echo "no-stop-or-unreadable:sid=${MIND_SID:-EMPTY}"
IF `user-stop`: END THE TURN on a Bash echo naming the file read. Do NOT re-arm
  the net, do NOT poll, do NOT SELECT, do NOT run a work unit. stop-hook.sh:387
  ALLOWs it (gate=worker-net-stop-requested-session), so it is not trapped.
IF `no-stop-or-unreadable`: continue below — but `sid=EMPTY` means the predicate
  COULD NOT EVALUATE, not that no stop exists (guard-6178); the two labels differ
  so an un-evaluatable check can never read as an all-clear. Continuing is still
  right (CLOSED-SET: an unrecognised state resolves toward RUNNING).

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
  Do NOT end the turn here. FIRST ask if the full re-poll is DUE — the park orbit
  backs off on consecutive parks (g-357-51 part 4):
  Bash: py -3 core/scripts/body-manifest.py park-due --sid "$MIND_SID" --agent "$MIND_AGENT"
  rc=1 (not due; last stdout line = seconds left): re-arm ScheduleWakeup(<the
  Phase 0.5 park prompt>, delaySeconds=min(remaining,3600), noop=false, reason="park re-poll") and END the turn on
  a Bash echo — no preamble, no poll, no SELECT. rc=0 (due, or any error — fail
  toward polling): skip the closure exit, continue through the preamble: Phase
  0.5 re-polls the reducer and SELECT decides — a claim RESUMES (Phase 2), no
  goal re-parks (Phase 1). This gate never re-derives that.
  TEST THE CLOSED SET, NEVER 'not active': `parked` is non-active and alive
  (why: core/config/rationale/worker-cycle-preamble.md). Any state this gate
  does not recognise resolves toward RUNNING, because a wrong close is the
  unrecoverable direction and a wrong continue is not.
IF the manifest is MISSING while the fork WM exists: treat as OPEN (never
  invent a close from an absent record) and continue.

# Phase -0.5 — LIGHT PRIME (g-306-211). TWO TIERS, and the split is the point:
# the IDENTITY half runs once per worker session, the RECENCY half runs on EVERY
# re-entry. /prime never runs for a worker, and per-goal CATEGORY retrieval
# structurally misses cross-cutting MECHANISM-indexed guardrails and Self's
# Decision Authority. This phase closes exactly that gap and nothing more.
#
# Scoped CALLs to the SAME surfaces /prime reads (no-transcription contract,
# guard-2676): Read + world-cat.sh + guardrails-read.sh + reasoning-bank-read.sh.
# Do NOT transcribe /prime's other steps here — board sweeps, insights,
# peer-surface, and category loading are deliberately SKIPPED (per-goal retrieval
# covers that terrain); the worker stays thin.
#
# THE RECENCY HALF IS UNCONDITIONAL (g-306-298, a USER DIRECTIVE: no multi-hour
# sessions on entry-time rails, against measured unit gaps of 15-92 min), and it
# costs ~0.15s per unit.
# Rationale (WHY, measured): core/config/rationale/worker-cycle-preamble.md
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
# "worker alive, mid-unit" from "claim abandoned". Without them a claim held past
# the sweep's 120-minute foreign-SID grace is popped mid-execution.
# Rationale (WHY, measured): core/config/rationale/worker-cycle-preamble.md
Bash: bash core/scripts/heartbeat-tick.sh
# EXPECT rc=2 ON A WORKER AND DO NOT TREAT IT AS FAILURE: a worker box is IDLE by
# design, and the tick refuses the agent-wide RUNNING-only work with exit 2 —
# AFTER writing both per-Body heartbeats, which is the whole point of the call.
# Fail-open by contract: never branch on this rc, never let it stop the cycle.

# Phase -0.3 — PULL LATEST FRAMEWORK (g-306-233). A scoped CALL to the SAME
# component the reducer uses, in its pull-only mode — never a hand-rolled
# git sequence (no-transcription contract, guard-2676 / g-306-212).
#
# WHY IT EXISTS: before this phase a WORKER never pulled at all (iteration-push.sh
# ran only from reducer paths this loop skips), so no framework fix shipped
# during a worker's life ever reached it.
# Rationale (WHY, measured): core/config/rationale/worker-cycle-preamble.md
#
# Placed at the TOP of the cycle, between units and before any claim, so a merge
# can never land under a goal that is mid-execution.
#
# --no-push is deliberate and is the whole difference from the reducer's call:
# "fetch + integrate, then STOP before the push decision". A worker pulls so it
# is never behind; pushing the shared tree stays the reducer's job, so two Bodies
# of one agent never contend on the same store files.
#
# No commit step is needed first: iteration-push.sh self-heals a dirty tree
# (g-115-2249) and throttles its own fetch, so calling it every cycle is cheap.
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
# staleness: a repeating content CONFLICT (g-306-315) and a repeating integrate
# DEFER on a dirty shared file (g-115-6934). Detection is bash-owned (guard-399)
# and prints ONCE per streak; this branch owns the RESPONSE, same for both.
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
# WHY IT EXISTS: a prose chain through the execute protocol is READ, not run; a
# literal Bash call here runs every unit. Stale checkouts have returned pre-fix
# source to a grep.
# Rationale (WHY, measured): core/config/rationale/worker-cycle-preamble.md
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
# WHY IT EXISTS: agent-watchdog.py --tick ran only from iteration-close.sh, which
# this loop skips, so no watchdog probe had ever run on a worker box.
#
# WHAT IT COVERS, stated plainly because partial coverage read as total is the
# failure this goal was filed about. The tick runs the six BOX-LEVEL probes in
# WORKER_SAFE_PROBES (its banner prints the live set): daemon-health,
# mirror-wedge, freshness, clock-skew, memory-headroom, git-drift.
#
# WHAT IT DOES NOT COVER — do not let this line be mistaken for stall detection:
#   - The five reducer-shaped probes are FILTERED OUT: on an IDLE-by-design
#     worker they cannot fire or they false-fire, and an inert probe reads as
#     coverage.
#   - THIS WORKER'S OWN STALL: a tick inside the loop dies with the loop. The
#     observer is WorkerStallProbe on the REDUCER, which reads this Body's
#     Phase -0.4 heartbeat carrier. Skip that tick and this Body is invisible
#     to the only thing watching it.
#   - Do NOT add a diary-staleness threshold here: on a worker, diary staleness
#     and unit duration are the same quantity, so no threshold separates a
#     stall from healthy work.
# Rationale (WHY, measured, and the duplicate-phase collision): core/config/rationale/worker-cycle-preamble.md
Bash: py -3 core/scripts/agent-watchdog.py --tick
# Fail-open: advisory only. It announces the role filter on stderr every run so a
# filtered tick is never mistaken for a full one. Never branch on this rc.

# Phase -0.15 — GATE-FIRINGS SPOOL FLUSH (g-306-432). A scoped CALL to the SAME
# flusher the reducer uses (guard-2676). Its ONLY caller was iteration-close's
# reducer-only do_productivity_check, so under own-cloud EVERY worker gate firing
# stayed stranded in the machine-local spool and invisible fleet-wide.
# Self-throttling BY CONTRACT (--min-interval-seconds 300, --burst-records 200),
# so call it EVERY cycle and never hand-roll a cadence — a caller-side interval
# check would be a second, drifting copy of a bound the script already owns.
# Rationale + measurements: core/config/rationale/worker-gate-firings-flush.md
Bash: bash core/scripts/gate-firings-flush.sh
# Fail-open; never branch on this rc. VERIFY IN THE SPOOL, never in the shared
# firings store — an unmoved destination mtime is not evidence (guard-4040).
# SIBLING LANE, same defect, same contract (g-358-79). Wired at BOTH
# orchestrators because productivity-check is reducer-only, so a flush wired
# there alone is dead on every worker box (guard-3448: assert reachability).
Bash: bash core/scripts/trigger-firings-flush.sh

# Phase 0.5 — REDUCER-LIVENESS POLL (g-306-125 mechanism 2). Runs at the top of
# EVERY select cycle, before any claim. A worker whose reducer has died keeps
# claiming and executing goals whose results nobody will ever merge — the
# reducer is the only Body that runs generalize-down — so the work is silently
# discarded and the goals are held from the rest of the fleet.
Bash: py -3 core/scripts/worker_reducer_liveness.py
# rc 0 = CONTINUE to SELECT — even when this Body is PARKED. The poll does NOT
#   resume it; a CLAIM does (Phase 2), so `parked_at` keeps measuring the whole
#   wait (guard-4184: the cap is a patience cap, and `resume` clears the stamp).
# rc 1 = PARK, which is a WIND-DOWN AND NOT A CLOSE (g-306-291).
# The JSON on stdout carries {verdict, reason, rc, consecutive_errors} — quote
# `reason` in the stop message so the wind-down cause is legible.
# Rationale (WHY park rather than close, why no staging, why the recorder call,
#   why resume-at-claim, the terminal shapes): core/config/rationale/worker-park.md
#
# THE PARK SEQUENCE (rc=1 here; Phase 1 re-enters it on no eligible goal with its
# own reason and tags). Post FIRST — the terminal is about to end and this line is
# the one place a live process holds the fact — then park, record, arm, STOP:
#   Bash: py -3 core/scripts/body-manifest.py park --sid "$MIND_SID" --agent "$MIND_AGENT"
#     `parked` = first park; `already-parked` = idempotent re-park, ORIGINAL
#     parked_at preserved. POST ON THE FIRST PARK ONLY (`already-parked` is the
#     tell — an unconditional post buries the signal under ~24 copies a day):
#   Bash: echo "<reason>, this Body PARKED awaiting reducer (hourly re-poll, auto-resume)" \
#           | bash core/scripts/board-post.sh \
#           --channel coordination --type finding --tags reducer-stall,body-parked
#   Bash: python3 core/scripts/stop-reason-record.py --path worker-body-parked \
#           --reason "<the poll's reason field>" --agent "$MIND_AGENT"
#     NOT optional bookkeeping: it is what keeps the fleet sweeper reading
#     EXPECTED_IDLE instead of emailing the user that this box is DEAD. The path
#     is in the recorder's NO_NOTIFY_PATHS, enforced inside record().
#   Bash: py -3 core/scripts/recovery_yank.py check --agent "$MIND_AGENT"
#     rc=0 = RECOVERY-YANK: recovery-gate.sh demoted the reducer, no user did
#     (g-357-51 part 3). ESCALATE ONCE per yank (escalated_before=false): post
#     the finding (tags recovery-yank,escalation), then
#     Notify the user about the recovery yank. (Check world/forged-skills.yaml
#     for a skill whose triggers match "notify the user" and invoke it with a
#     short subject and message. If no matching skill is registered, fall back
#     to a `participants: [agent, user]` goal via aspirations-add-goal.sh. Never
#     block on notification failure.) Then `--mark-escalated`. rc=1 (user-stop)
#     / rc=2 (none): park quietly.
#   Tool (not Bash): ScheduleWakeup(prompt="Parked worker Body: re-enter
#     /worker-loop at Phase -0 (manifest: parked = RESUMABLE), re-run the
#     Phase 0.5 poll; SELECT on rc 0 (a claim resumes); rc 1 re-parks.",
#     delaySeconds=3600, noop=false, reason="park re-poll")
# and NOTHING after it — that wakeup IS the auto-resume; a park turn that forgets
# to arm is indistinguishable from a close. DO NOT STAGE THE WM (`park` never
# stages, only a real close does). Say PARKED, not "winding down". Fail-open: a
# failed post must never block the park.
#
# ON RE-ENTRY (the wakeup fires): Phase -0 owns it — park-due, then this poll.
#
# THE PARK IS CAPPED at body-manifest.PARK_MAX_HOURS (60h), whichever trigger
# parked it: when `py -3 core/scripts/body-manifest.py park-expired --sid ...
# --agent ...` exits 0, stop re-parking and take the GENUINE close in Phase 1
# (a REAL stop that DOES email — from a closed Body `/start` is user-only).
# Expiry FAILS TOWARD STAYING PARKED (unreadable `parked_at` = not-expired).
# NEVER-PROMOTE: no rc yields "become the reducer"; every ambiguous signal
# resolves toward wind-down. A single transient poll failure does NOT wind
# down — transients accumulate to `error_threshold` (3); any LIVE poll resets.
# Takeover detection: machine_id + claim token fingerprint (g-306-224).

# Phase 1 — SELECT (reducer's scorer, g-375-06)
Bash: goal-selector.sh select --top 10
Pick the top eligible unclaimed goal (none? --top 40); drop any in a partner's
in_flight OR in_flight_bodies — a WORKER is in the LATTER ONLY (g-306-276).
#
# ROLE + SKILL ELIGIBILITY (g-115-5664, g-306-440). "Eligible" includes the
# goal's ROLE and its SKILL, and the scorer knows neither.
# Rationale (WHY role-first, why `undetermined` is a WORD not an rc, why the
# flag ORDER is load-bearing, why the banner branch keeps the selector
# role-blind): core/config/rationale/worker-role-gate.md
#
# For each candidate IN RANK ORDER, ask the contract before claiming.
# --role COMES FIRST (the skill arg is argparse REMAINDER, so a TRAILING
# --role is swallowed as skill text and never read). Omit --role when unset.
Bash: py -3 core/scripts/worker_execute.py goal-eligible --role <the goal's executable_by_role field> <the goal's skill field, verbatim>
# THE GOAL-LEVEL executable_by_role IS CONSULTED FIRST and is decisive where
# present. READ THE STDOUT WORD, not just rc — there are THREE:
#   reducer-only (rc 1) -> SKIP THIS GOAL, take the NEXT candidate in the same
#     pass. Say the stderr reason out loud; a silent skip is the half of this
#     that would rot. Do NOT burn a select cycle per refusal, and do NOT file
#     anything: the goal is not broken, it is the reducer's, and it stays
#     visible to the reducer where it belongs.
#   eligible (rc 0) -> a real judgment was made. Proceed to CLAIM.
#   undetermined (rc 0) -> THE BRIDGE DECLINED TO JUDGE (skill-less goal, or a
#     skill the table does not map). The zero is FAIL-OPEN, NOT a pass, and the
#     call is YOURS (g-115-6523, g-306-440).
#
# The check is a scoped CALL into the shared component (guard-2676). The refusal
# list is NOT duplicated here and must not be: worker_execute derives it from
# LIFECYCLE_DISPOSITIONS, so a stage whose disposition changes moves its skills
# with it. `reducer-only-skills` prints the current set if you want to see it.
#
# A GREEN ANSWER IS NOT A PROOF. On `undetermined` — and whenever a named skill
# looks like loop-phase encoding over YOUR OWN unmerged experience — read the
# goal's
# verification outcomes and description BEFORE claiming, with THIS command
# (never a hand parser over the store file — the guard refuses it):
Bash: bash core/scripts/aspirations-query.sh --goal-field id <goal-id> --full
# Work that ENCODES to tree/reasoning-bank/guardrails, RESOLVES a hypothesis,
# drains a capture lane, consumes worker refs, pushes main, or writes the
# agent-wide working-memory.yaml is REDUCER-ONLY: release it, take the next
# candidate. SKILL_LIFECYCLE_STAGE is the remedy for a NAMED skill only — a
# skill-less goal has no key to add. The line: loop-phase encoding (forbidden)
# vs goal-directed artifact creation from content supplied in the goal
# (`/tree` is pinned for that reason).
IF no goal: PARK AWAITING SUPPLY — the same resumable park as Phase 0.5 rc=1,
  NOT a close (g-353-73). Exhaustion is TRANSIENT on a multi-Body fleet
  (measured: core/config/rationale/worker-park.md). A worker with no work is
  WAITING, not finished.
    Bash: py -3 core/scripts/body-manifest.py park-expired --sid "$MIND_SID" --agent "$MIND_AGENT"
  rc=0 (parked past the cap) -> GENUINE close. Record it, write the body-closing
  sentinel so the stop-hook (Phase-2B producer) marks this Body
  closed-pending-merge for the reducer to merge at generalize-down, then STOP on
  a Bash echo:
    Bash: python3 core/scripts/stop-reason-record.py --path worker-park-expired \
            --reason "parked <N>h (no reducer / no supply); cap reached" --agent "$MIND_AGENT"
    Bash: touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"
  rc=1 (not parked, or parked under the cap) -> run THE PARK SEQUENCE of Phase
  0.5 with reason "SELECT returned no eligible goal; parked awaiting supply",
  board tags `supply-gap,body-parked` (first park only), and the same 3600s
  wakeup as the LAST call. The Body re-enters hourly: reducer poll -> SELECT ->
  a claim resumes it (Phase 2), no goal re-parks it (here).

  THE CLOSE CONDITION IS EXHAUSTIVE — THERE IS EXACTLY ONE, and it is the only
  place in this file that writes the sentinel: an EXPIRED park, reached from
  either trigger (reducer gone, Phase 0.5; supply gone, here). A user stop is the
  other path and is not yours to initiate. Anything else is an INVENTED stop
  condition (guard-3479), and parking must never acquire a soft edge for
  anything else.
  ** CONTEXT PRESSURE IS NEITHER A CLOSE NOR A PARK CONDITION. ** Not "context is
  filling up", "the session has run long", "I have done N units", "the next goal
  will not fit". Autocompact makes a long session survivable and this loop runs
  indefinitely: nearly-out-of-context is a reason to enter the next unit
  (stop-hook-compliance.md rules 3-4). A persuasive board post explaining why
  this Body closed itself is the SIGNATURE of the defect (measured: worker-park.md).
  IF A SPECIFIC GOAL WILL NOT FIT, RELEASE THE CLAIM UNSTARTED and keep looping —
  `aspirations-release.sh <goal-id> --source <world|agent>`, then VERIFY by
  re-reading that the record shows status=pending / claimed_by=None (the release
  echo is not proof). Never close or park the Body for it.
  A WRONG CLOSE IS NOT RECOVERABLE: the sentinel stages this Body's WM, Phase -0
  then refuses every unit on this SID, and only a user `/start` of a NEW session
  reopens work. A wrong park costs one hourly poll.
  (The reducer generates work, not the worker. Do NOT file here: SELECT finding
   nothing is the park edge, not a moment to manufacture work — see "May a
   worker file a goal?" below.)

# Phase 2 — CLAIM (claimed_by = the agent name; <source> = the queue SELECT printed)
Bash: aspirations-claim.sh <goal-id> <agent> --source <source>
IF claim conflict or goal_id_collision: abort this goal, loop to SELECT.
SUCCESS = the full goal record with `executed_by_sid` == YOUR `$MIND_SID`
  (`executed_by` is the agent name every Body shares — it never means another
  Body holds the goal). A refusal is a JSON `error`. Never execute a goal this
  pass did not claim: measured 2026-08-29, one Body re-selected past its own
  successful claim and a sibling executed an unclaimed goal.
IF this Body's manifest reads `parked` (either trigger): the claim IS the resume.
  BEFORE any execution:
    Bash: py -3 core/scripts/body-manifest.py resume --sid "$MIND_SID" --agent "$MIND_AGENT"
    Bash: python3 core/scripts/stop-reason-record.py --clear --agent "$MIND_AGENT"
  The `--clear` is the half that is easy to drop, and dropping it disables a
  detector: nothing else ever removes that file (a park→resume never passes
  through /start), so a resumed Body working for days under EXPECTED_IDLE
  suppresses the alert for a LATER genuine death. Idempotent, never raises.

# Phase 2.9 — READ THE RECORD YOU WERE JUST HANDED (g-115-6695). The claim
# response IS the full goal record. The loop's only OTHER prompt to touch
# these fields is the one that WRITES them (3.9), so skipping it puts the read
# after work it would prevent. Workers are MOST claims.
READ EVERY narrative field, WHOLE: `outcome_note`, `outcome_notes` (plural,
  guard-3512), `progress_note`, `description`, `release_negatives` when present
  (typed: why the last Body let go; `kind: not-due` → check the cadence before
  spending a pass; g-115-8163). Enumerate from the RECORD, not from this list —
  it goes stale silently (guard-2283). They are APPEND-ORDERED — print
  len() first and read to it; the corrective block sits at the END, which is what
  a head read drops (guard-2043). An empty outcome_note is NOT an untouched goal
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
# The reducer's learning handlers need the EXECUTING session's experience, which
# the reducer never had. This step is the hand-off: the worker RECORDS the
# observation; the reducer RUNS the handlers over it at generalize-down.
# Rationale (WHY four capture lanes, measured): core/config/rationale/worker-capture-lanes.md
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
# matter most:
#   "sq-013" — this observation is WORK someone must own (a defect, a follow-up,
#              a capability gap, a dependency): the Case-B relay of the filing
#              ruling below. The reducer's Worker Spark Replay runs the sq-013
#              work-discovery handler over sq-013 relays and FILES the goal
#              (dedup first). Shape the observation as a filing, not a musing:
#              what is wrong / needed, where (path:line, script, store), the
#              evidence you measured, and a one-line suggested title.
#   null / another sq — a lesson (how to work, a gotcha, a pattern): the
#              learning handlers encode it; no goal is filed from it.
# A finding that is BOTH a lesson and work gets TWO entries with the two
# triggers; the reducer routes each. One entry cannot carry both.
# goal_id is REQUIRED: body-merge unions array slots by CONTENT HASH, so two
# identical observations would collapse into one and the second goal's learning
# would vanish. The write routes to the Body WM via BODY_WM_PATH, never agent-wide.

# Phase 3.6 — EXPERIENCE CAPTURE (g-306-199). Sibling of 3.5: a spark is a
# reusable LESSON; this is the execution NARRATIVE the reducer encodes an
# experience .md from.
#
# UNLIKE 3.5, THIS IS NOT CONDITIONAL: write an exp_capture entry for EVERY
# executed goal, routine ones included. Capturing only interesting units would
# bias the archive toward drama and lose the baseline it is measured against.
#
# You MAY also write the experience .md for THIS goal: experience-add.sh takes
# a worker write SCOPED to a goal this Body holds, rc=3 otherwise (g-306-418;
# partition in digest 4.25). Tree/rb/guardrail/journal stay reducer-only. OFF
# the claim-holding box it returns no_claim: relay, never retry.
Bash: echo '{"goal_id":"<goal-id>","category":"<goal.category>","execution_summary":"<2-3 sentences: what was done and what it produced>","outcome_class":"<deep|routine>","key_decisions":["<decision + why, one per entry>"],"surprise_level":<0-10>,"verbatim_anchors":["<exact error codes / paths / hashes / commit shas — the strings a future reader would grep for>"]}' | bash core/scripts/wm-append.sh exp_capture
# verbatim_anchors matters most: exact strings die with the session that saw
# them. goal_id is REQUIRED for the same content-hash reason as 3.5.

# Phase 3.65 — HYPOTHESIS-EVIDENCE CAPTURE (g-306-200). Third capture lane, and
# the one with the narrowest trigger.
#
# CONDITIONAL, like 3.5 and unlike 3.6. Write an entry ONLY when execution
# surfaced evidence bearing on a hypothesis that ALREADY EXISTS in
# world/pipeline.jsonl — a prediction this unit confirmed, contradicted, or
# sharpened. Most units surface none; an empty lane is the correct output of a
# unit that touched no hypothesis, and inventing one to fill the slot is worse
# than silence because it manufactures evidence the reducer will act on.
#
# This is EVIDENCE INPUT to the reducer's /review-hypotheses protocol, keyed to a
# hypothesis_id. THE WORKER DOES NOT RESOLVE, AND THAT IS THE WHOLE DESIGN:
# supplying evidence and resolving are different acts, and only the first is
# yours (a worker resolving from its own unmerged state is the Nth-reducer defect).
#
# hypothesis_id MUST name a real pipeline.jsonl record — check before writing.
# An id that matches nothing survives the merge and reads as a broken protocol.
Bash: echo '{"goal_id":"<goal-id>","hypothesis_id":"<YYYY-MM-DD_slug from world/pipeline.jsonl>","evidence_summary":"<what execution actually showed, in enough detail for the reducer to resolve from without this session>","surprise_level":<0-10>,"confirms_or_contradicts":"<confirms|contradicts|partial>","suggested_resolution":"<your read, explicitly NON-binding — the reducer runs the full protocol>"}' | bash core/scripts/wm-append.sh hyp_capture
# goal_id is REQUIRED for the same content-hash reason as 3.5/3.6 (two units on
# the SAME hypothesis_id would otherwise collapse into one entry).
# suggested_resolution is a READ, not a verdict: if it is ever applied as-is, this
# lane has become a second resolver.

# Phase 3.66 — ENCODING CAPTURE (g-306-202). Fourth and last capture lane, and
# the hardest to tell apart from 3.5 — so lead with the discriminator rather than
# the rationale. The axis is learning-routing.md's, not a stylistic one:
#   3.5  spark_capture     = a LESSON about HOW TO WORK    -> reducer routes to rb / guardrail
#   3.66 encoding_capture  = a FACT about THE WORLD        -> reducer routes to a tree node
# "The producer's count is not the consumer's enumeration" is a spark. "InboxWatch
# counts one bucket, so agentInboxCount under-reports by 41" is an encoding. The
# same work unit routinely yields one, both, or neither.
#
# CONDITIONAL, like 3.5 and 3.65 — but do NOT expect it to be usually empty.
# (Its measured yield, and when this lane should be RETIRED instead of defended:
# core/config/rationale/worker-capture-lanes.md.)
#
# Do NOT write the tree node here, and do not reach for /tree: tree encoding is
# reducer-only-by-design. /tree's worker-eligible pin (SKILL_ELIGIBLE_DESPITE_ENCODING)
# covers goal-directed artifact creation from content supplied IN THE GOAL, never
# encoding your own session's findings.
FOR EACH tree-worthy domain fact this unit established (usually 0):
    Bash: echo '{"goal_id":"<goal-id>","category":"<goal.category>","fact":"<what is now known to be TRUE about the world, stated so a reader who was not here can act on it>","evidence":"<the measurement that establishes it — command, output, count, or path>","suggested_node":"<tree path if you know one, else null — NON-binding, the reducer decides placement>","supersedes":"<node/claim this corrects, or null>"}' | bash core/scripts/wm-append.sh encoding_capture
# goal_id is REQUIRED for the same content-hash reason as 3.5/3.6/3.65.
# `evidence` makes this worth more than an assertion: the reducer cannot
# re-measure what it never observed. `supersedes` earns this lane its keep: a
# fact that CORRECTS an encoded belief is the highest-value thing a worker can
# hand up.
# ADDING A LANE? Register it in ARRAY_SLOTS in BOTH core/scripts/wm.py AND
# mind_api/src/endpoints/wm_write.py (the daemon copy is the live one). An
# unregistered slot is silently NULLED, not refused.
# Rationale (WHY): core/config/rationale/worker-capture-lanes.md

# ---- `load_bearing` — the ONE optional field all four capture lanes share ----
# (g-306-293.) Add `"load_bearing": true` to ANY capture entry in 3.5/3.6/3.65/3.66
# when it SUPERSEDES or CONTRADICTS an existing encoded conclusion, or unblocks a
# queued decision. Omit it otherwise — the default is false and most entries are.
#
# It buys two things: a PRIORITY MERGE (capture_fast_lane.py copies flagged
# entries into the reducer WM every iteration, from ACTIVE Bodies too) and an
# EVICTION EXEMPTION (at cap, wm append FIFO-drops the oldest entry, and flagged
# entries are popped last). Unflagged captures are routinely destroyed at cap.
#
# DO NOT FLAG EVERYTHING. A cap a writer can defeat is not a cap: blanket-
# flagging restores FIFO among the flagged and kills the triage signal. No
# density target exists — a capped lane pins at 80% (the unflagged floor)
# whatever writers do (g-115-10021). A mis-flag costs priority, nothing else.
# Flag honestly even at saturation (a known, accepted cost: g-306-361).
# Rationale (WHY, measured): core/config/rationale/worker-capture-lanes.md

# Phase 3.7 — CARRIER CHECK (g-306-263). The Phase 4 hand-off carries the
# worker's WM and its goal record and NOTHING ELSE: a framework edit made on a
# worker box reaches the reducer via no channel unless a carrier exists.
# Rationale (WHY, measured): core/config/rationale/worker-carrier-and-closure-evidence.md
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
Bash: py -3 core/scripts/worker_execute.py check-capture-carrier
# rc 1 undelivered / 3 unchecked: act on what it prints, never hold the goal (g-115-9852).
# Rationale (WHY a store content read, not an mtime): core/config/rationale/capture-carrier-delivery-check.md

# Phase 3.8 — CARRIER PUSH (g-306-264). Phase 3.7 asks whether this unit's output
# can reach the reducer; this is the step that MAKES it reach for the two classes
# git carries. Run it whenever this unit touched core/**, .claude/** or CLAUDE.md,
# or made any local commit you want the reducer to see. Harmless otherwise — it
# pushes the same HEAD the previous unit pushed.
#
# COMMIT FIRST — BUT CONSULT THE LOCK FIRST (g-115-8957). Your commit and this
# call's fetch+integrate BOTH move HEAD, voiding a co-resident Body's running
# full-suite as Phase -0.3 does (flags differ on PUSH, never MERGE); iteration-push
# guarded its own push, never the commit it orders.
Bash: bash core/scripts/tree-lock.sh check --project-root "$(git rev-parse --show-toplevel)"
# rc=1 (a PEER holds) is the ONLY defer: leave it uncommitted, note it in Phase
# 3.9, carry it next cycle. Every other rc proceeds — including your OWN lock.
# Rationale (WHY + rc table): core/config/rationale/suite-run-voided-by-loop-merge.md
# The ref carries HEAD, so uncommitted work is NOT carried — this carrier's one
# residual failure mode, and why local-git-commit keeps its own row in
# OUTPUT_CLASS_CARRIERS rather than being folded into framework-file-edit.
Bash: bash core/scripts/iteration-push.sh --push-worker-ref
# Pushes HEAD to refs/workers/<agent>/<sid>, then STOPS — it never touches the
# shared branch (a per-sid ref has ONE writer, so Phase -0.3's --no-push
# rationale does not reach it). Fail-soft like every other iteration-push call —
# never branch on the rc, and never let a failed push stop the cycle.
# A worker does NOT run the consumer (worker-ref-consume.sh): merging another
# Body's framework edits into the shared tree is a reducer act.

# Phase 3.9 — CLOSURE EVIDENCE (g-115-5158). A scoped CALL to the SAME producer
# the reducer uses — never a worker-local write (no-transcription contract,
# guard-2676 / g-306-212).
#
# WHY IT EXISTS: closure evidence is the `outcome_note` field. THIS phase is the
# rich-narrative producer: Phase 4a calls do_verify with a ONE-LINE --summary and
# passes --no-supersede on the worker path, so 4a only backfills when this phase
# did not run (g-115-6633).
# Rationale (WHY, measured): core/config/rationale/worker-carrier-and-closure-evidence.md
#
# RUNS AFTER 3.7/3.8 ON PURPOSE. Phase 3.7's STRANDED branch also writes
# outcome_note, and this helper is write-if-absent/never-clobber — so placing
# this before 3.7 would silently prevent a stranding from ever being recorded.
#
# THE NARRATIVE OPENS WITH THE EVIDENCE TABLE (g-375-05): one row per
# verification outcome, and a blank line ends a row. Write what you MEASURED:
#   OUTCOME <n>: MET — <measured value>. Source: <command + output | path | sha | the two timestamps>
#   OUTCOME <n>: NOT MET — <what is missing>; deferred to <goal-id>
# Phase 4a's close refuses a MET row whose path or store key does not exist, or
# whose interval ("~5s") cites fewer than two timestamps.
# Spec: core/config/conventions/goal-schemas.md § Closure Evidence Table.
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
# 4a. JUDGE, THEN WRITE — two calls, one step (g-306-417).
#     FIRST the LLM judgement, for the ONE unit you just executed. INVOKE the
#     skill; never transcribe its steps (guard-1867 — inlining a sub-skill skips
#     the side effects you did not know it had). scope=own-unit runs the per-goal
#     sections for THIS goal alone and skips the cross-Body residue (streaks, and
#     the SAMPLED review of these self-graded closures), which stay reducer-side.
#     A pass means the criteria were met on THIS Body — NEVER that the code
#     landed on main (guard-4638), and an outside-world reading it rests on is a
#     timestamped observation, not a settled fact (guard-3034). Its verdict is
#     what you pass as --status below; under own-unit `aspiration_complete` is
#     REPORTED only, since closing the parent stays reducer-side.
Skill(aspirations-verify) with: goal, result, scope="own-unit"
#     THEN the mechanical status write, through the SHARED close writer, as its
#     own separate call (guard-470): do_verify is the ONLY writer of that
#     transition and of all that hangs off it (guard-2523).
#     Rationale (WHY two calls): core/config/rationale/worker-verify-own-unit.md
Bash: bash core/scripts/iteration-close.sh --phase verify --goal <goal-id> \
        --status <completed|blocked|skipped> --source <world|agent> \
        --outcome <deep|routine> --summary "<one line: what this unit did>"
#     LONG CALL (g-375-02): up to 15 min when its domain-suite gate fires (it
#     says so first). Give it your Bash tool's longest timeout; if the harness
#     moves it to the background, wait as the result says; if it kills it,
#     re-run ONCE with run_in_background and wait. Never write the status.
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
#     THE PREFIX IS NOT THE STRUCTURE, and reading it as such is why this lane
#     is starved: that prose is SKIPPED by the only sweep that clears this class.
#     So ALSO write a machine-evaluable predicate into verification.preconditions
#     — the ONLY field precondition-defer-recheck reads (predicate.py owns the
#     types; after_time for an elapsed window) — and make it evaluable on ANY
#     box, never only yours (guard-4306). If the gate is not machine-checkable,
#     SAY SO in the defer text: then no sweep owns it and a reader must.
#     Rationale: core/config/rationale/worker-defer-predicate.md
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
#     (`_completed_goal_ids` -> `merged_goal_ids` -> worker_retrospective.py);
#     without it the consolidate Step -0.9 retrospective has nothing to run over.
#     Same row shape as aspirations-state-update Step 3 (goal-selector reads
#     these keys — do not rename); omit work_class when the goal record has none.
#     Only for status=completed. Routes to the Body WM (BODY_WM_PATH), never
#     agent-wide.
Bash: echo '{"goal_id":"<goal-id>","aspiration_id":"<aspiration-id>","recurring":<true|false>[,"work_class":"<class>"]}' | bash core/scripts/wm-append.sh goals_completed_this_session
#
# 4c. Do NOT run spark / state-update / learning-gate / productivity-check. The
#     worker's divergent WM + the now-CLOSED goal record are the hand-off; the
#     reducer merges the WM at generalize-down and runs the encode/reflect/
#     consolidate phases over the merged result.
#
# team-state in_flight: 4a's --if-goal clear is the ONLY clear you perform. Do
# NOT add an unconditional clear (g-306-132-d): in_flight is AGENT-keyed with no
# sid, so a worker and its reducer share one row and an unconditional clear would
# blank a live reducer's row. (The stop-hook's worker_close_in_flight_clear.py
# clears after a genuine close ONLY when the row's goal carries THIS Body's
# claimed_by_sid; a hand-rolled clear here would defeat that ownership test.)
# Do NOT write the body-closing sentinel here — finishing ONE work-unit is NOT a
# genuine close (g-306-70). The sentinel is written ONLY by Phase 1's
# expired-park close. A worker that ends abruptly leaves no sentinel;
# cleanup-stale-bindings then stages its WM, so no divergence is lost.
# Rationale (WHY): core/config/rationale/worker-carrier-and-closure-evidence.md

# Phase 5 — CONTINUE (the loop edge). No driver exists: the loop re-invokes
# ITSELF. The terminal tool call of a completed work unit is Skill(worker-loop),
# which re-enters at Phase -0 (re-verifying worker identity — guard-517/guard-463
# class: role-gated re-entry) and runs the Phase 0.5 reducer-liveness poll before
# any new claim. NEVER Skill(aspirations) — that is the reducer's full-loop
# re-entry. Every CLOSE path (an EXPIRED park — the one sentinel writer — or a
# user stop) ends the turn with a Bash call after its sentinel work —
# self-continuation never overrides a close edge. A PARK (Phase 0.5 rc=1, or
# Phase 1 no eligible goal) is not a close and takes the third shape: it ends on
# `ScheduleWakeup(<park-resume prompt>, 3600, noop=false, reason="park re-poll")` and nothing after it.
# Rationale (WHY): core/config/rationale/worker-deadman-net.md
```

## What a worker MUST NOT do

- Run any reducer-only phase (the gate above returns `skip` for all of them).
- Write the agent-wide working memory (`session/working-memory.yaml`) — a worker
  writes ONLY `sessions/<unitKey>/working-memory.yaml`.
- Set/clear `running-session-id` or claim the reducer role.
- Run consolidation — that is the reducer's job.
- Generate new aspirations. Goals are NOT a flat prohibition — see the ruling below.

## May a worker file a goal? (RULED 2026-08-06, g-306-250)

This is the ruling; do not re-derive it (its history:
core/config/rationale/worker-loop-contract.md).

**The rule's purpose is to stop a worker inventing an AGENDA the reducer never
approved.** Every case below is decided against that purpose, not against the
sentence.

| Case | Example | Ruling |
|---|---|---|
| **A. Preserves sanctioned scope** — a successor carrying the unfinished remainder of the goal you are closing | g-335-901 | **FILE.** Filing nothing DROPS work already discovered under work the reducer already approved. That is not a new agenda; it is the same one, unfinished. |
| **B. New scope, observable by any Body** — a framework defect, a mislabelled field, a stale predicate | the agent-queue claim defect (g-306-238) | **DO NOT FILE. Relay** via `wm-append.sh spark_capture` **with `sq_trigger: "sq-013"`** and a filing-shaped observation (Phase 3.5), and post to the findings board if it is time-sensitive. The reducer's Worker Spark Replay runs the sq-013 handler over sq-013 relays and files the goal at its next iteration (2026-08-16); a relay WITHOUT that trigger reaches only the lesson handlers and never becomes work. The relay can cost the WORK, not just time (below). |
| **C. New scope, MACHINE-LOCAL** — only observable from this box: its store, filesystem, process table, installed binaries, local git state | the `.history` wiring fix (g-115-644); `unzip` absent on this box (g-335-869) | **FILE.** No reducer and no partner can EVER observe a worker box's local state, so the relay is the ONLY channel and its loss is unrecoverable — nobody can rediscover what only this box can see. |

A stalled replay costs the WORK, not just time (measured:
core/config/rationale/worker-loop-contract.md). B/C splits on RECOVERABILITY,
not delay: any Body can see a B finding again; a dropped machine-local one is
not late, it is gone.

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

**A worker has its own deadman net (g-306-239).** Without it, a worker turn that
ends on trailing TEXT is dead permanently — the Stop hook cannot reliably catch
a text-only turn-end (rb-629/guard-454) — and **a dead LOOP inside a live
PROCESS** is invisible to every process-liveness check. The net has been
witnessed resurrecting a worker; its delivery latency is owned by g-115-6629.
It does NOT cover an auth-loss stall: ScheduleWakeup cannot fire a turn when the
CLI has no valid login.

Do NOT reach for the reducer's `<<autonomous-loop-dynamic>>` sentinel — it is
both forbidden and inert here. It resolves to the AUTONOMOUS loop instructions
(guard-517/guard-463 forbid a worker entering those), and a worker box is IDLE
by design, so a resurrected turn would refuse at Phase -1.5 rather than resume.
The worker arms a NATURAL-LANGUAGE prompt instead, sanctioned by
`.claude/rules/schedule-wakeup-correctness.md`.

The directive text is NOT written out here on purpose: guard-2676 requires a
scoped CALL to `core/scripts/deadman-directive.sh`, so the delay, the opt-out
flag, and the closure-check-then-arm ordering live in exactly one place. That
component serves the WORKER only (its `--role reducer` branch is retired).
History, the witnessed resurrection, and the auth-loss retraction:
core/config/rationale/worker-deadman-net.md

| Just finished | Terminal tool call |
|---|---|
| A work unit (Phase 4 done, no close condition) | **The deadman PAIR** — run `bash core/scripts/deadman-directive.sh --role worker` and emit exactly the two batched calls it prints: `ScheduleWakeup(<natural-language resurrection prompt>, 600, noop=false, reason="deadman resurrection net")` THEN `Skill(worker-loop)` as the LAST call. `Skill(worker-loop)` is still the primary re-entry (NEVER `Skill(aspirations)` — reducer-only, guard-517/guard-463; and never a bare Bash echo — no driver exists to re-invoke you). The ScheduleWakeup is a NET behind it, not a substitute. |
| A **PARK** — Phase 0.5 rc=1 (reducer gone, g-306-291) or Phase 1 no eligible goal (supply gone, g-353-73), park not expired | **`ScheduleWakeup(<park-resume prompt>, 3600, noop=false, reason="park re-poll")` ALONE, as the last call, with NO `Skill(worker-loop)` after it.** No Skill: re-entering now would re-run the poll that just said "no reducer" and spin. No 600s net either — **the platform keeps ONE pending wakeup (replace-slot), so the park poll IS the net**. A park turn that forgets to arm is indistinguishable from a close. The stop-hook worker-net stands down on the parked manifest (`gate=worker-net-body-parked`), so this turn-end is ALLOWed rather than BLOCKed into a sentinel ceremony that would durably close the Body. |
| A close path — an EXPIRED park (the Phase 1 sentinel just touched) or a user stop | Bash echo stating the close reason. **Do NOT arm the net here** — the turn genuinely ends; stop-hook Phase 2B consumes the sentinel and stages the WM. A net armed by the PREVIOUS work unit is still pending and will fire ~600s later; that firing is benign because THREE layers read the DURABLE closure record, `sessions/<SID>/body-manifest.yaml` `body_state` in the CLOSED SET (`closed-pending-merge` / `merged` / `closed-stale`): the resurrection prompt declines to resume, Phase -0's closure gate refuses a work unit, and the stop-hook worker-net stands down (`gate=worker-net-body-closed`). The `body-closing` SENTINEL cannot serve this purpose — it is CONSUMED at every genuine close. |
| Consulted for the phase split only (no work unit ran) | The `worker_execute.py` Bash call whose output answered the question. |
