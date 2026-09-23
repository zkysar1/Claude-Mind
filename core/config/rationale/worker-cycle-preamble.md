# Rationale: Worker cycle preamble — why the phases before SELECT exist

Referenced from `.claude/skills/worker-loop/SKILL.md` Phases -0, -0.5, -0.4,
-0.3, -0.25 and -0.2: the preamble a worker Body runs at the top of EVERY cycle,
before it polls its reducer and selects. Each section below is that phase's own
WHY commentary, moved here verbatim on 2026-09-23 (g-115-8214) so the skill fits
under the 65,536 B injection ceiling. The rules, boundary values and branches
stayed at their call sites; what moved is the provenance: dates, boxes,
measurements and the incident narratives behind each phase.

Most of these phases are instances of one class. A protection wired only into a
reducer path (iteration-close, /prime) never reaches the second orchestrator,
and each fix is a scoped CALL into the shared component (guard-2676), never a
transcription of its steps.

## Phase -0 — why the closure gate tests the CLOSED SET, never 'not active'

TEST THE CLOSED SET, NEVER 'not active'. The old predicate here was
"anything other than active", correct while every non-active state was
terminal — so introducing `parked` would have made this gate refuse every
unit of a Body that is deliberately alive, which is byte-for-byte the durable
close g-306-291 exists to remove. Same latent trap sat in the resurrection
prompt (fixed, pinned) and in the stop-hook worker-net (fixed, 5th valve).
Any state this gate does not recognise resolves toward RUNNING, because a
wrong close is the unrecoverable direction and a wrong continue is not.

## Phase -0.5 — why the light prime exists, and why its recency half is unconditional

Phase -0.5 — LIGHT PRIME (g-306-211). TWO TIERS, and the split is the point:
the IDENTITY half runs once per worker session, the RECENCY half runs on EVERY
re-entry. The worker path never primed at all: /start's W-steps jump
fork->Skill(worker-loop), and /prime runs only for reducer/reader/assistant. The
worker is NOT context-blind — the execute protocol retrieves per-goal at 4
points — but per-goal CATEGORY retrieval structurally misses cross-cutting
MECHANISM-indexed guardrails (the measured two-query dilution class,
retrieval-triggers.md "Why TWO queries"), and Self's Decision Authority never
loads at all. This phase closes exactly that gap and nothing more.

WHY THE RECENCY HALF IS UNCONDITIONAL (g-306-298, landing the 2026-08-13 USER
DIRECTIVE addendum on g-306-211, quoted): "scope now includes PER-UNIT refresh,
not only once-per-session prime ... workers must not run multi-hour sessions on
entry-time rails." This phase used to sit ENTIRELY behind the sentinel, so every
unit after the first ran on rails loaded at session entry. Measured on cc-07
2026-08-16: a light-prime-done sentinel dated 2026-08-15 17:59 with the session
still executing units at 12:xx the next day — ~18.5h on entry-time rails,
against a directive whose stated rationale is measured unit gaps of 15-92 min.

THE COST IS WHY IT CAN BE UNCONDITIONAL RATHER THAN CADENCED. Measured on cc-02
(zeta, uname -r 6.8.0-137-generic): reasoning-bank-read.sh --recent 0.059s,
guardrails-read.sh --summary 0.093s — ~0.15s per unit, against a Phase -0.3 that
runs a real `git fetch` + `git merge`. That is "cost-proportional to the pull",
which is the bound the directive itself sets. Note guardrails-read.sh has NO
--since/delta flag (verified before naming it, guard-359: the arg loop accepts
--id / --category / --active / --summary only), so the "guardrail-index delta"
the directive names is served by re-reading the whole-corpus one-line index —
which is exactly what --summary already is.

The skill's `measured unit gaps of 15-92 min` phrase is ALSO a test anchor:
`test_stranded_claim_sweep.py::test_worker_cycle_gap_figure_still_matches_its_source`
derives a carrier-freshness window from it, which is why that phrase stayed
inline when the rest of this measurement moved here.

## Phase -0.4 — why the liveness tick exists (g-306-227)

WHY IT EXISTS: heartbeat-tick.sh writes BOTH the same-box
sessions/<SID>/body-heartbeat AND the syncable session/body-heartbeat-<SID>.json
carrier — the only signals that let the reducer's stranded-claim sweep tell
"worker alive, mid-unit" from "claim abandoned". This loop never called it, so a
cross-box worker had NO liveness signal of any kind, and any claim it held past
stranded-claim-sweep's 120-minute foreign-SID grace was popped mid-execution.
Measured 2026-08-05 on cc-07 (both files absent 17 min into an active unit);
retro-cause of the g-315-518 pop on 2026-08-04, which was closed against the
writer. g-306-208 HAD already fixed the writer's ordering so the body write
precedes the agent-state=IDLE refusal — correct, and inert, because nothing
called it. Its own tests stayed green through the whole defect (guard-1943:
pinning the writer says nothing about the wiring).

## Phase -0.3 — why a worker pulls the framework (g-306-233)

WHY IT EXISTS: a WORKER never pulled, at all, ever. iteration-push.sh is the
only thing in the framework that does a `git fetch` + `git merge --no-edit`,
and when this phase was written its ONLY caller was iteration-close.sh's
do_productivity_check — which this loop deliberately skips. So a reducer stays
current every iteration while a worker runs whatever
code it had when someone last pulled BY HAND. Measured 2026-08-06: cc-07 was
112 commits behind, then 30 four hours later, then 51 — while cc-04 never
exceeded 2. Every framework fix shipped during a worker's life never reached
it, INCLUDING the two liveness fixes written that same day to protect workers.

CORRECTED g-115-3262 (2026-08-10): it is no longer the only caller, and the
line number this comment used to cite had already drifted — hence the function
name above instead. Five call sites now: this phase, worker-loop's
--push-worker-ref, aspirations-graceful-stop, aspirations-execute Phase 4
entry, and do_productivity_check. The reasoning above is unchanged: the
execute-phase call was added BECAUSE close-only left the REDUCER's own tree a
median of 7 commits behind mid-iteration (n=333, .git/iteration-push.log),
which is the same defect this phase fixes for a worker.

Why no commit step precedes the pull, in full:

No commit step is needed first, though a worker tree is normally dirty with its
own store appends: iteration-push.sh self-heals a dirty tree in-run by
COMMITTING agents/<self>/* churn pathspec-limited (g-115-2249) and retrying the
merge once. Its fetch is independently throttled (FETCH_INTERVAL_MIN, stateless
via FETCH_HEAD mtime), so calling it every cycle costs nothing on most cycles.

The escalation branch, with the measurements behind its two shapes:

BRANCH ON ITS STDOUT for the ESCALATION directives. TWO shapes reach here and
retrying can NEVER clear either, so "resume on local code" becomes PERMANENT
staleness: a repeating content CONFLICT (g-306-315; cc-08 ran 85 commits
behind retrying one conflict every cycle while the blocked merge concealed
the peer fix g-306-308) and a repeating integrate DEFER on a dirty shared
file (g-115-6934; cc-08 39 behind while origin already carried the fix).
Fail-soft retry is right for transient shapes and wrong for these. Detection
is bash-owned (guard-399) in iteration-push.sh's shape-aware defer-streak
file, printing ONCE per streak; this branch owns the RESPONSE, same for both.

## Phase -0.25 — why a worker pulls the product repos (g-306-370)

WHY IT EXISTS: the capability shipped 2026-08-20 and was wired ONLY into that
convention — reachable from this loop solely by a five-link prose chain
(Phase 3 -> load-execute-protocol.sh -> digest Phase 3.9 -> load-conventions
-> the pull step), executed by a READER rather than a runner. This loop's own
-0.5/-0.4/-0.3/-0.2 phases are literal Bash calls and run every unit; that
chain did not. MEASURED 2026-08-26: a live worker session reached this point
with 23 of 61 product checkouts behind origin, one by 13 commits — a repo the
same session had ALREADY read source from. Earlier the same defect left a
checkout 3 commits/3 days stale, returned pre-fix source to a grep, and nearly
shipped a redundant change into an auto-deploying repo. Same shape as
g-306-233 (a worker never pulled the FRAMEWORK), one surface over.

## Phase -0.2 — why the watchdog tick exists, and what it does not cover (g-306-240)

THERE WAS BRIEFLY A SECOND COPY OF THIS PHASE, at -0.35, and how it got here is
worth one paragraph because the mechanism is invisible by construction. Two
agents implemented g-306-240 concurrently on different boxes: one wrote the
worker-side probe FILTER, the other wrote the peer-side WorkerStallProbe. In
core/scripts/agent-watchdog.py the two edits landed in the same region, so git
raised a CONFLICT and it was resolved deliberately. In THIS file they landed in different
regions, so git auto-merged both cleanly — producing two phases that ran the
same command twice and disagreed about what it covered (the -0.35 copy claimed
"6 OF 10 PROBES" including BackgroundJobProbe, which the filter excludes). The
conflict was the LUCKY case: it announced itself. The clean auto-merge is the
dangerous one. When resolving a duplicate-implementation collision, grep the
whole tree for the other file(s) the same pair of commits touched — the ones
that merged WITHOUT complaint are where the damage hides.

WHY IT EXISTS: agent-watchdog.py --tick had exactly ONE caller in the tree,
iteration-close.sh:2554, and this loop deliberately skips iteration-close. So
NO watchdog probe had EVER run on a worker box — not DaemonHealthProbe, not
MirrorWedgeProbe, none. Same structural fact as g-306-233 (workers never
pulled) and g-306-235; fourth instance of reducer protections not reaching the
second orchestrator.

WHAT IT DOES NOT COVER — do not let this line be mistaken for stall detection:
  - The five reducer-shaped probes are FILTERED OUT, not merely inert. A worker
    is agent-state=IDLE + agent-mode=autonomous BY DESIGN and writes no
    runner-heartbeat and no running-session-id, so classify_stalled returns
    None at its first guard and HeartbeatProbe false-fires `heartbeat_missing`
    (both measured on cc-08). Enabling them would install five probes that
    cannot fire, which reads as coverage.
  - THIS WORKER'S OWN STALL. The tick runs at the top of a cycle — between work
    units — when the loop is alive and progressing by construction. A tick
    inside the loop dies with the loop, so it cannot observe the auth-loss /
    process-death class (the cc-08 2026-08-06 incident, ~92 min, found by a
    human sweep). That needs an out-of-process or peer-side observer reading
    the syncable session/body-heartbeat-<SID>.json carrier.
    THAT OBSERVER NOW EXISTS: WorkerStallProbe, which runs on the REDUCER and
    is deliberately excluded from WORKER_SAFE_PROBES (a worker running it would
    be watching itself with a detector whose whole premise is out-of-process
    observation). It reads this Body's carrier from the store of record and
    alerts when it goes stale WHILE this Body still holds a claim. Which makes
    the Phase -0.4 heartbeat tick above not merely a liveness courtesy: it is
    the signal that detector consumes, and skipping it makes this Body
    invisible to the only thing watching it.
  - Nor can a diary-staleness threshold be added here to fix that: on a worker
    the execution-diary records ONE entry per GOAL at claim time, so diary
    staleness and unit duration are the SAME quantity. Measured consecutive
    gaps on cc-08 were 34/56/92/28/15 min, where the 92 was a real stall and
    the rest were healthy work — no threshold separates them.

The skill keeps one prose mention of `agent-watchdog.py --tick` beside the call
on purpose: `test_agent_watchdog_worker_role.py`'s negative control mutates the
real file (deletes the `Bash:` invocation, keeps the prose) and needs that
mention to prove its predicate tells an invocation from commentary.

## Phase -0.15 — the measurement as the skill stated it

The skill's Phase -0.15 comment carried this measurement inline; the full
account is in `worker-gate-firings-flush.md`:

stayed stranded in the machine-local spool and invisible fleet-wide (measured
522 records / 17h on cc-09; 194 / 6h on cc-08). Fourth instance of the
workers-never-inherit class (g-306-233 pull, -227 heartbeat, -370 product pull).

## Cross-references

- `.claude/skills/worker-loop/SKILL.md` — the phases these sections explain
- guard-2676 — the no-transcription contract every phase here follows
- guard-3448 — a gate is only as broad as its entry points (the recency half's ordering)
- guard-1943 — pinning the writer says nothing about the wiring (Phase -0.4)
- `worker-stop-standdown.md` — Phase -0-stop, the step before this preamble
- `worker-gate-firings-flush.md` — Phase -0.15, the step after it
- `suite-run-voided-by-loop-merge.md` — why Phase -0.3's merge voids a running suite
