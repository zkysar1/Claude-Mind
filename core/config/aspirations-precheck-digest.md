# Aspirations-Precheck Digest — deferrable phase bodies

Loaded by `core/scripts/load-precheck-digest.sh`. Holds the PROSE of the
deferrable-tier precheck phases: measured markers, parse-shape warnings,
incident traces. Extracted by g-115-6583.

**The chain skeleton did NOT move.** Every `## Phase` header, every budget
`meter check`, and every `IF decision == "drop" ... continue to Phase Y`
line stays in the SKILL.md, because those are control flow — moving them
breaks the drop-branch chain that `test_precheck_phase_chain.py` pins (and
did break it, in this goal's first attempt). Nothing always-run is here.

---


## Phase 0.5b.5


Rationale (WHY pending-questions sentinel sweep): `core/config/rationale/precheck-gates.md`

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check pending-questions-sweep)
Bash: bash core/scripts/pending-questions-sweep.sh sweep --apply
# Reads world+agent aspiration queues to build the completed/superseded
# goal-id set, evaluates the heuristic chain, and (when --apply) atomically
# marks verdict=auto_resolve entries as status=resolved with timestamp.
# Fail-open at every layer: missing files, parse errors, write failures
# all yield empty results without aborting the sweep.
```


## Phase 0.5b.6


Rationale (WHY parent-supersession sweep): `core/config/rationale/precheck-gates.md` (g-268-10, rb-842)

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check parent-supersession-sweep)
Bash: bash core/scripts/parent-supersession-sweep.sh --max-age-hours 24 --min-siblings 2 --apply
# Iterates world + agent queues. For each Apply:-parent with reference
# timestamp + ≥2 superseding Design/Apply siblings (sprint-scope only),
# marks parent status=completed with outcome_note "superseded by sibling
# decomposition". Single-writer, idempotent, fail-quiet — same pattern as
# defer-recheck.sh / pending-questions-sweep.sh.
# Metrics log: <WORLD_PATH>/parent-supersession-sweep-metrics.jsonl
```


## Phase 0.5b.7


Rationale (WHY unblock-parent-status sweep): `core/config/rationale/precheck-gates.md` (g-250-73, rb-908)

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check unblock-parent-status-sweep)
Bash: bash core/scripts/unblock-parent-status-sweep.sh --apply
# (engine: core/scripts/unblock-parent-status-sweep.py behind the wrapper)
# Iterates world + agent queues. For each pending "Unblock:" with a
# parseable parent goal-id whose parent.status is terminal, marks the
# Unblock status=skipped with outcome_note
# "parent resolved without action needed (parent_id=<X>, parent.status=<Y>)".
# Single-writer, idempotent (outcome_note prefix check), fail-quiet —
# same rb-428 pattern as defer-recheck.sh / pending-questions-sweep.sh /
# parent-supersession-sweep.sh.
# Metrics log: <WORLD_PATH>/unblock-parent-status-sweep-metrics.jsonl
```


## Phase 0.5b.8


Rationale (WHY routing-audit target-status sweep): `core/config/rationale/precheck-gates.md` (rb-1478)

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check routing-audit-target-status-sweep)
Bash: bash core/scripts/routing-audit-target-status-sweep.sh --apply
# Iterates world + agent queues. For each pending/in-progress routing-audit goal
# (discovered_by=post-decompose-routing-audit OR origin_signal/title routing-*)
# with a parseable TARGET id whose target.status is terminal, marks the audit
# goal status=skipped with outcome_note "routing-audit target resolved without
# action needed (target_id=<X>, target.status=<Y>)". Single-writer, idempotent
# (outcome_note prefix check), fail-quiet — same rb-428 pattern as
# unblock-parent-status-sweep.sh / parent-supersession-sweep.sh.
# Metrics log: <WORLD_PATH>/routing-audit-target-status-sweep-metrics.jsonl
```


## Phase 0.5b.9


Rationale (WHY credential-defer conservative guard): `core/config/rationale/precheck-gates.md`

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check credential-defer-recheck)
Bash: bash core/scripts/credential-defer-recheck.sh --apply
# Scans world + agent queues. For each pending/in-progress goal with
# defer_reason starting "human_blocked:" that is older than max_age_hours (2h):
#   1. Extract env-var key from defer text using conservative pattern set
#      (explicit env-read.sh has KEY > credential KEY > env var/key KEY > fallback)
#   2. If no key extractable → skipped_no_key (human-only defer, never cleared)
#   3. Run: bash core/scripts/env-read.sh has <KEY> → exit 0 means present
#   4. If key still absent → skipped_probe_fail (defer stays)
#   5. If key now present → clears defer_reason via aspirations.py update-goal
#      (calls Python directly, not via bash, for Windows reliability)
# Age gate (2h default): prevents thrash on freshly-set defers.
# Metrics log: <WORLD_PATH>/credential-defer-recheck-metrics.jsonl
# JSON output: {"scanned":N, "eligible":N, "skipped_no_key":N,
#               "skipped_probe_fail":N, "cleared":N, "would_clear":[...],
#               "details":[...]}
```


## Phase 0.5b.10


Rationale (WHY defer-drift detective): `core/config/rationale/precheck-gates.md` (2026-06-12 asp-304 incident)

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check defer-drift-check)
Bash: bash core/scripts/defer-drift-check.sh --output json
Parse drift_count + drifted[].
IF drift_count == 0:
    continue silently to Phase 0.5b.11   # the clean, common case
ELSE:
    Output: "▸ ⚠ DEFER-DRIFT: {drift_count} goal(s) with a PAST deferred_until + structured-defer marker (deferred_readiness pollution risk)"
    FOR EACH d in drifted[:5]:
        Output: "    {d.goal_id} ({d.source}): deferred_until={d.deferred_until} {d.hours_past}h past | {d.defer_prefix} | pc={d.precondition_status}"
    # File ONE deduplicated Investigate so the drift gets re-gated by judgment.
    # Dedup: skip if an open Investigate with origin_signal
    # "investigate:defer-drift-audit" already exists (a single open re-gate pass
    # covers all current drift — mirrors the rb-428 sweep family's idempotency
    # posture). Uses --goal-field origin_signal (EXACT match on the stable dedup
    # key), NOT --title-contains: the title is prose while the machine key lives
    # in origin_signal, so a title-substring search would be VACUOUS and fail
    # open into a duplicate (g-115-2196 — the exact bug class this call site had
    # with the old nonexistent --status/--contains flags).
    # The key MUST carry the "investigate:" prefix: origin-signal-gate
    # ALLOWED_PREFIXES has no bare "defer-drift-audit" form, so an unprefixed
    # key gets Layer-D auto-derive REWRITTEN to investigate:<title-slug> at
    # filing time and the exact-match dedup here goes vacuous — the sweep then
    # re-files a duplicate every iteration the drift persists (observed
    # 2026-07-17, g-115-2475; second instance of the g-115-2196 vacuous-dedup
    # class, this time on the VALUE not the flag).
    Bash: existing=$(bash core/scripts/aspirations-query.sh --goal-status pending,in-progress --goal-field origin_signal "investigate:defer-drift-audit")
    IF existing is empty:
        Compose an Investigate listing each drifted goal + its precondition_status
        (prose -> re-gate deferred_until to the correct future date from the
        defer_reason; ready -> the gate is merely stale, clear the defer;
        still_unmet -> re-gate). File via aspirations-add-goal.sh into asp-115
        (participants: [agent], category framework-architecture, priority MEDIUM,
        origin_signal "investigate:defer-drift-audit").
```


## Phase 0.5b.11


Rationale (WHY reason-less-blocked sweep): a `status=blocked` goal with an EMPTY
Blocker Reference Schema — `blocker_ref` None, `blocked_by` [], `defer_reason`
None — escapes EVERY existing guard. `gates/blocker_ref.py` validates a
blocker_ref's structure only when it is paired with a defer_reason;
`blocker-create-gate.py` fires at CREATE time (these goals flip to blocked LATER
via a direct status update); `blocker-recheck.py` only re-probes goals that HAVE
a blocker. So when a peer-dependency blocker completes, nothing auto-unblocks the
dependent — it strands invisibly (canonical: g-115-2198-b / g-115-2200 stranded
~2 days until felt-sense RAW-read the queue; surfaced by g-115-2591).

SELF-CONTAINED --apply (rb-428 family — NOT the defer-drift detective pattern of
0.5b.10). The SCRIPT files ONE deduplicated reconcile Investigate itself (dedup
by the SAME active read that finds the blocked goals — guard-487 fail-closed,
guard-383 fatal-on-read-error so it can never file blindly) when reason-less
goals exist and no open audit does. The LLM's ONLY job here is to surface the
WARN — do NOT compose or file the Investigate (the `--apply` flag already did,
bash-side). This is deliberate: the exact failure g-115-2595 fixes is
LLM-discretionary steps drifting (guard-616/rb-616), so the filing must be
bash-enforced, not left to LLM memory.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check reason-less-blocked-check)
Bash: bash core/scripts/reason-less-blocked-check.sh --apply --output json
Parse reason_less_count + reason_less[] + investigate_filed + open_audit_goal_id.
IF reason_less_count == 0:
    continue silently to Phase 0.5b.12   # the clean, common case
ELSE:
    Output: "▸ ⚠ REASON-LESS-BLOCKED: {reason_less_count} status=blocked goal(s) with an EMPTY Blocker Reference Schema (no blocker_ref / blocked_by / defer_reason) — invisible to selection, blocker-recheck, AND quiescence"
    FOR EACH e in reason_less[:5]:
        Output: "    {e.goal_id} ({e.source}) [{e.aspiration_id}] intended={e.intended_agent}: {e.title}"
    IF investigate_filed:
        Output: "    → filed reconcile Investigate {investigate_filed} (asp-115) — reconstruct each real blocker into blocked_by/blocker_ref, OR unblock to pending if the premise is gone (route lane-owned goals to their owner, do NOT appropriate)"
    ELIF open_audit_goal_id:
        Output: "    → open reconcile Investigate {open_audit_goal_id} already covers these (dedup — no duplicate filed)"
    # No LLM filing here — the --apply flag already filed-or-deduped bash-side
    # (drift-proof, the whole point of g-115-2595). Continue to Phase 0.5b.12.
```


## Phase 0.5b.12


The exact COMPLEMENT of 0.5b.11 above. That sweep finds blocked goals carrying NO
block signal; this one finds blocked goals whose signals are all PRESENT and all
SATISFIED — a goal is in exactly one of the two populations, never both. Together
they close the `blocked_by`/`blocker_ref` half of the block surface, which the
whole `defer_reason` sweep family (0.5b.3/0.5b.4/0.5b.9/0.5b.10) cannot see.
Canonical cost: g-335-144 sat blocked 7 days after its dependency completed,
found by hand via felt-sense; measured again at first run — g-350-36 had sat
blocked 6.7 days while its only block signal completed ~1.5h after the block was
set.

DETECTIVE ONLY — no `--apply`, unlike 0.5b.11 (deliberate, not an omission).
Three reasons: the population is tiny (2 eligible fleet-wide at first run) so
automation buys little while a wrong auto-unblock is expensive; most hits are
lane-owned by another agent, and unblocking their goal appropriates their queue;
and a passed `expires_at` means the block record FAIL-OPENED per the TTL, which
is NOT proof the premise cleared. Escalate to `--apply` only if the population
grows. Read `resolution_basis` before acting: `referent_terminal` is strong
evidence, `ttl_expired` only means the record aged out.

ROUTING IS NOW MECHANICAL (g-115-3414) — do NOT hand-post the lane routing.
`--post-routing` makes the SCRIPT drop the coordination-board breadcrumb for each
non-suppressed hit, and scan the board first so a hit another agent already
routed inside `--cooldown-hours` (default 24) is skipped. This is the same
shared+durable board-breadcrumb cooldown the siblings 0.5b.1b
(inbox-alert-age-check, g-115-1533) and 0.5b.2b (handoff-aging-check,
g-115-1531) use — not a new mechanism.

Why it was needed: this phase is detective-only, so the routing decision — and
therefore the dedup burden — sat with the LLM. Every agent runs precheck every
iteration and a hit stays surfaced until its LANE OWNER acts, so the same
unchanged goals were routed once per agent per round. Measured: 7 posts from
3 agents over ~29h on goals that never changed; g-250-03-c alone accumulated 35
board mentions. Every post was individually CORRECT under the lane rule — the
rule is right, the missing piece was the cooldown.

`--post-routing` does NOT weaken the detective-only posture. It posts a
breadcrumb and mutates no goal; that is why the flag is not called `--apply`,
which everywhere else in this sweep family means "mutate goals".

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check blocked-signal-resolution-check)
Bash: bash core/scripts/blocked-signal-resolution-check.sh --post-routing --output json
Parse all_resolved[] + disagreement[] + dangling_ref[] + undecidable[],
plus routing_suppressed_count / routing_eligible_count / routing_posted[] /
routing_post_failed[] / routing_cooldown_degraded.
IF routing_cooldown_degraded is true:
    Output: "▸ ⚠ ROUTING COOLDOWN DEGRADED — the board read failed, so the sweep fails OPEN and may re-route a hit a partner already routed. The suppression count below is not a measurement."
    # Fail-open is deliberate: a cooldown that failed CLOSED would silence
    # routing on a transient board fault, turning a plumbing error into
    # invisible blocked work — the exact failure this phase exists to surface.
IF all four lists are empty:
    continue silently to Phase 0.5b.13   # the clean, common case
ELSE:
    # SURFACING IS UNCONDITIONAL — the cooldown gates the outbound BOARD POST
    # only, never these lines. The stdout line is how the RUNNING agent learns
    # the state, and suppressing it would hide the finding from the one agent
    # positioned to act on it.
    FOR EACH e in all_resolved[:5]:
        Output: "▸ ⚠ UNBLOCK-ELIGIBLE: {e.goal_id} ({e.source}, intended={e.intended_agent}) blocked {e.days_blocked}d — every block signal resolved [{e.resolution_basis}]: {e.blocker_ref_why}"
    FOR EACH e in disagreement[:3]:
        Output: "▸ SIGNAL DISAGREEMENT: {e.goal_id} blocked {e.days_blocked}d — blocked_by resolved={e.blocked_by_resolved} but blocker_ref resolved={e.blocker_ref_resolved}. Do NOT unblock; reconcile the stale half instead."
    FOR EACH e in dangling_ref[:3]:
        Output: "▸ DANGLING BLOCK REF: {e.goal_id} blocked {e.days_blocked}d — {e.blocker_ref_why}. Can never auto-clear; repoint or remove the reference."
    IF routing_suppressed_count > 0:
        Output: "▸ routing: {routing_suppressed_count} hit(s) already routed by an agent within {routing_cooldown_hours}h — breadcrumb suppressed, not re-posted"
    IF routing_post_failed is non-empty:
        Output: "▸ ⚠ routing breadcrumb FAILED for {routing_post_failed} — those hits will re-route next iteration (no cooldown record was written)"
    # Route by lane, do NOT appropriate (guard-1007 family): a hit whose
    # intended_agent is another agent is now routed by the SCRIPT's breadcrumb —
    # do not also hand-post it, that is the duplication this fix removed. Only
    # an `either`/self-routed hit may be unblocked by this agent, and only after
    # re-probing a `ttl_expired` basis.
```


## Phase 0.5b.13


Lane B of the reclaim duty (`.claude/rules/reclaim-routed-work.md`). The sweeps
above all test the PREMISE axis — is the blocking condition still true? This
phase tests the population those sweeps keep returning "still true" on, and asks
the question none of them ask: has this been frozen so long that the ROUTING
itself deserves re-derivation?

Why it exists: `audit-deferred-defers.py` shipped with no bash wrapper and NO
call site in any loop phase — built, verified-to-exist by a presence-only
`/verify-learning` check, and never once invoked. A sweep with no call site is
indistinguishable from a sweep that always returns clean. Both were fixed
2026-07-29: the wrapper landed, and its classifier stopped stamping every
structured-prefix defer "genuine" unconditionally (72.5% of the live queue took
that early return, hiding defers frozen 83 and 95 days).

DETECTIVE ONLY — no `--apply` (same reasoning as 0.5b.12): most hits are
lane-owned by another agent, and clearing their defer appropriates their queue.
Read the `stale-structured` evidence as a TRIGGER to re-derive, never as a
verdict that the defer is wrong (rule 3 — age selects what to re-check first,
it never by itself justifies closing anything).

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check reclaim-defer-audit)
Bash: bash core/scripts/audit-deferred-defers.sh --output json
Parse records[]; select those whose evidence contains "stale-structured", plus
all category=="c" (narrative-only) records.
IF both selections are empty:
    continue silently to Phase 0.5b.14   # the clean, common case
ELSE:
    FOR EACH e in stale_structured[:5]:
        Output: "▸ ⚠ STALE DEFER: {e.goal_id} ({e.src}) [{e.asp_id}] frozen {age} — well-formed prefix, but re-derive BOTH axes (premise still true? reason still valid?): {e.title}"
    FOR EACH e in narrative_only[:3]:
        Output: "▸ ⚠ NARRATIVE DEFER: {e.goal_id} ({e.src}) — defer reads as excuse, not structural block: {e.title}"
    # Route by lane exactly as 0.5b.12: another agent's hit gets a coordination
    # board post naming the goal id + evidence; only an `either`/self-routed hit
    # may be re-derived and cleared by this agent, and only on fresh evidence.
```


## Phase 0.5b.14


Lane P of the reclaim duty. The largest SILENT accumulator in the queue: a goal
carrying `participants: [agent, user]` still looks like agent work and never
appears in any blocked tally, so no existing sweep or dashboard surfaces it as
routed-away. Measured 2026-07-28: 29 non-terminal goals carried `user`, the
oldest 73 days, with zero sweep covering the population.

`audit-user-to-agent.py` is the lane-P tool. It had the same orphan shape as
lane B — its only live references were a doc and a presence-only verification
check — AND it was blind to the population it existed to drain: the
`participants == ["user"]` EXACT-match predicate had a live candidate set of
**zero** (one goal in the fleet matched, and that goal was a deliberate park
the audit correctly refuses to touch). Correct routing caused the blindness:
`capability-before-user.md` tells the fleet to file `[agent, user]` whenever
both legs are real, so the creation-time gate working as designed produced
exactly the population the audit-time tool could not see. Widened 2026-07-29
to `"user" in participants`, matching what the creation-time advisory
(`gates/user_leg_scope.py`) always tested.

Two lanes now run, and they ask OPPOSITE questions:

- **PROMOTE** (`participants == ["user"]`) — "should the AGENT be involved?"
  Answered by the capability gate. `--apply` mutates. Safe: it only widens
  participants, never removes one.
- **DROP** (`user` alongside others) — "is the USER still needed?" The gate
  CANNOT answer this: agent capability says nothing about whether the human
  leg is discharged. Decidable only when the leg was declared, via
  `user_leg_scope` joined against the `## Standing User Grants` table. **Reports
  only, never mutates** — removing the human is a one-way door inside the loop,
  and the field is populated on a minority of goals.

Read `undeclared` as the lane's primary finding, not as noise: 20 of 28
`[agent, user]` goals never recorded WHAT the user is for, so no grant can
match them and no sweep can re-derive them. `grants no goal can key to` is the
mirror finding — a grant row whose scope head avoids the `user_leg_scope`
vocabulary carries real permission this audit can never apply. Both are
`.claude/rules/reclaim-routed-work.md` rule 4 (declare invalidations in
machine-findable terms) pointed at the two tables that must converge.

DETECTIVE ONLY here — `--apply` stays a deliberate operator action because it
mutates goals across every agent's queue, not a per-iteration automatic one.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check reclaim-user-participant-audit)
Bash: bash core/scripts/audit-user-to-agent.sh --output json
# JSON, not the text report, because the branches below address named fields:
# promote_lane.reclassified / drop_lane.verdicts / drop_lane.counts /
# grants.unkeyed. Reading fields the output does not name is how pseudocode
# rots into decoration.
# Never treat an empty PROMOTE plan as a clean sweep — on the live queue that
# lane is legitimately empty while the drop lane has 28 goals to report.
FOR EACH v in drop_lane verdicts where verdict == "grant-covered" (max 5):
    Output: "▸ ⚠ DROP `user`: {v.goal_id} — {v.grants} already covers scope '{v.user_leg_scope}': {v.title}"
    Output: "    → drop `user` from participants; close outright if the agent leg is also done"
FOR EACH e in promote plan[:5]:
    Output: "▸ ⚠ RECLAIMABLE [user]-only goal: {e.goal_id} ({e.source}) — capability gate matched '{e.matched_capability}': {e.title}"
IF promote plan non-empty:
    Output: "    → run `bash core/scripts/audit-user-to-agent.sh --apply` to promote"
IF undeclared count > 0:
    Output: "▸ {N} [agent, user] goal(s) never declared a user_leg_scope — they cannot be re-derived until backfilled"
IF grants.unkeyed non-empty:
    Output: "▸ {N} standing grant(s) no goal can key to — reword the scope head to use the user_leg_scope vocabulary"
IF nothing in any of the four buckets:
    continue silently to Phase 0.5b.15
```


## Phase 0.5b.15


The defer-recheck family all share one assumption: that a defer is cleared by
RE-RUNNING SOMETHING. 0.5b.4 re-probes an agent-provisionable capability with its
canonical script, 0.5b.9 does the same for the credential class, 0.5b.3 re-evaluates
a structured precondition. A `human_blocked` defer has no script to run — what
satisfies it is a HUMAN MESSAGE arriving on a channel — so it falls through every
one of them and is effectively permanent until a person notices by hand.

Measured cost of that gap (2026-07-25, foxtrot): the user granted the exact
authorization at 14:23 in a relayed board directive naming the commit by SHA.
Nothing cleared the defer. ~8h later the approved work was still unshipped and the
goal was ABSENT from goal-selector's entire candidate list — a deferred goal is not
a candidate, so no amount of looping surfaces it. It also manufactured a spurious
Investigate in a second agent's queue, correct about the mechanism and blind to the
fact that the work was already authorized.

Not redundant with 0.5b.9, and this was MEASURED rather than assumed — the
credential sweep is the one phase that also matches on the `human_blocked:`
prefix, so it is the obvious reason to delete this one. Live on 2026-07-31 it
scanned 6 such defers and put **all 6** in `skipped_no_key`, whose own stated
reason is "human-only defer, never cleared". It also scopes to
`("pending","in-progress")` (`credential-defer-recheck.py:241`), so the 2
`blocked`-status defers are outside it entirely. That is 100% of the population
handed off by design: 0.5b.9 clears the credential subset, and this phase is the
only thing that looks at the residue.

DETECTIVE ONLY — no `--apply`, and that is a design decision rather than an
unfinished half. guard-1249: "match the probe to the DEFER'S PREMISE, not to the
resource it names ... never batch-clear several defers naming the same external
resource on a single probe." A keyword join proves a message MENTIONS a goal; it
cannot prove the message GRANTS that goal's specific blocking condition. The live
population shows the hazard is real: of 8 such defers, THREE named one Studio host.

Read `confidence`, never mere presence. The four signals demand DIFFERENT actions,
and the two deterministic ones demand OPPOSITE ones:

| signal | confidence | what it means | action |
|---|---|---|---|
| `pq_answered` | deterministic | the cited pending-question now reads answered/resolved | re-derive — but the tier is per-CITATION, not per-LEG: a defer naming several legs is NOT discharged by one answered pq (g-326-191). Count the legs first |
| `pq_retired` | deterministic | the cited question was WITHDRAWN | the OPPOSITE — the clearing path is dead, so the defer cannot be satisfied as written. Re-premise or re-file; never read as granted |
| `board_directive` | heuristic | a board post newer than the defer names this goal | evidence a human SPOKE about it. Open the post; never act on this alone |
| `pq_missing` | none | the cited `pq-` id exists in no agent's file | nothing arrived — the citation itself is broken. Confirm the block is really filed (guard-1197) |

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check human-blocked-defer-join)
Bash: bash core/scripts/human-blocked-defer-join.sh --output json
Parse verdict + records[] + shared_premise_clusters + errors[].
IF verdict == "unreadable":
    Output: "▸ ⚠ HUMAN-BLOCKED JOIN UNREADABLE: {errors} — this is NOT a clean sweep (rb-245)"
ELIF verdict == "clean":
    continue silently to Phase 0.5b.16   # the common case
ELSE:
    FOR EACH k, v in shared_premise_clusters.items():
        Output: "▸ SHARED PREMISE: {v} defers name '{k}' — guard-1249: probe each premise separately, never batch-clear the cluster"
    FOR EACH r in records where best_confidence == "deterministic" (max 5):
        Output: "▸ ⚠ DEFER SIGNAL ARRIVED: {r.goal_id} ({r.source}, intended={r.intended_agent}) — {signal names}: {r.title}"
    FOR EACH r in records where best_confidence == "heuristic" (max 3):
        Output: "▸ defer mentioned on the board: {r.goal_id} — open the post before concluding anything: {r.title}"
    FOR EACH r in records where best_confidence == "none" (max 3):
        Output: "▸ BROKEN CITATION: {r.goal_id} — its defer cites a pq that exists in no agent's file; nothing arrived. Confirm the block is really filed (guard-1197)"
    # Say what each bucket IS. Rendering a `none` record with the heuristic line
    # would announce a board post that was never found — the sweep asserting
    # evidence it never saw, which is the failure class it exists to catch.
    # Route by lane exactly as 0.5b.12/0.5b.13: a hit whose intended_agent is
    # another agent gets a coordination board post naming the goal id + the
    # evidence. Only an `either`/self-routed hit may be re-derived by this agent,
    # and only after reading the cited pq or post — never on the join alone.
```


## Phase 0.5b.16


Walks the `blocked_by` GRAPH. Every sweep above it inspects a single EDGE, so a
two-goal ring (X blocked_by Y, Y blocked_by X) passes all of them at once —
each edge is individually well formed and nothing looks at the shape they make
together. Filed from ZDS-Mind off a live incident that froze one aspiration at
71.8% and was found only by dumping every blocked goal beside its `blocked_by`
and reading the list by hand.

Why it is NOT folded into 0.5b.12, which already loads exactly these records:
that sweep scans `status=blocked`, and **guard-1690 names that filter as a DEAD
ZONE** — a goal left `pending` or set `skipped` while holding a live
`blocked_by` is invisible to 0.5b.11 and 0.5b.12 both. This sweep scans EVERY
non-terminal goal regardless of status. Measured at first run (2026-08-09,
cc-05): 8,809 goals scanned, 26 carry live edges, of which only a minority are
`status=blocked` — folding it in would have inherited a filter hiding most of
the population.

DETECTIVE ONLY — no `--apply`, and this one is not a "grow the population
later" call like 0.5b.12's. Breaking a cycle means deciding WHICH edge is
wrong, which is a judgment about intent rather than shape: in the founding
incident the goal made to wait opened its description with the words
"PREREQUISITE for", recoverable only by reading the goals. An automatic break
would pick a victim arbitrarily *and look like a normal unblock while doing
it* — the same failure mode as the 48h dependency fail-open this sweep exists
to pre-empt.

READ THE POPULATION, NOT JUST THE VERDICT. `cycles_found: 0` beside
`goals_scanned: 0` is a sweep that scanned nothing, not a clean queue — the
payload always carries `goals_scanned` / `goals_with_edges` / `edges_total` so
the zero is falsifiable (rb-245, guard-1922). `archive_degraded: true` means
the archive read failed, so treat `dangling_edges` as unreliable that run
(guard-1890: a COMPLETED-then-ARCHIVED dependency is otherwise
indistinguishable from one that never existed).

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check dependency-cycle-check)
Bash: bash core/scripts/dependency-cycle-check.sh --output json
Parse cycles[] + dangling_edges[] + goals_scanned + archive_degraded.
IF goals_scanned == 0:
    Output: "▸ ⚠ DEPENDENCY-CYCLE SWEEP SCANNED ZERO GOALS — this is a read failure, not a clean queue"
ELIF cycles[] and dangling_edges[] are both empty:
    continue silently to Phase 0.5b.17   # the clean, common case
ELSE:
    FOR EACH c in cycles[:5]:
        Output: "▸ ⚠ DEPENDENCY CYCLE ({c.length}-goal{' SELF-LOOP' if c.self_loop else ' ring'}): " + " -> ".join(g.goal_id for g in c.goals) + " -> {c.goals[0].goal_id} — every goal in this ring is frozen and invisible to the selector AND to its blocked-work reporting."
        FOR EACH g in c.goals:
            Output: "      {g.goal_id} [{g.status}] {g.aspiration_id}: {g.title}"
    FOR EACH d in dangling_edges[:3]:
        Output: "▸ DANGLING DEPENDENCY: {d.goal_id} -> {d.missing_target} (absent from live AND archived queues) — can never auto-clear; repoint or remove."
    # Resolve by READING the goals, never by breaking the cheapest edge. The
    # direction is usually recoverable from the goals' own descriptions (a
    # goal calling itself a PREREQUISITE cannot depend on its dependent).
    # Route by lane exactly as 0.5b.12: a ring whose goals are intended for
    # another agent gets a coordination board post naming the goal ids and the
    # evidence — do NOT edit another agent's edges (guard-1007 family).
```


## Phase 0.5b.17


Surfaces OPEN goals whose backing hypothesis already reached a terminal pipeline
stage. Nothing else closes them: `hypothesis-discovered-overdue-sweep.py` handles
the INVERSE case (records orphaned in `discovered`) and never looks at goals; no
close logic anywhere keys on `stage==resolved`; and `goal-selector.py` reads
`hypothesis_id` for SCORING ONLY — so a goal whose question is already answered
keeps competing for selector attention, and its `priority` keeps working in its
favour. Measured: g-115-3668 sat 5 days after its hypothesis resolved and then
scored **rank 1 of 584**. The precheck Hypothesis Expiration Check does not cover
this — it fires on DATE (`now > resolves_by`), which is both later and wrong:
g-115-1983's `resolves_by` was three weeks out, and `expired` is exempt from
accuracy stats, so the mislabel is silently lossy.

DETECTIVE ONLY — no `--apply`, same as 0.5b.12, and for one reason stronger than
population size: closing on hypothesis stage ALONE would drop real work.
g-115-3668 carried a second obligation ("the other two open register rows should
be re-read in that light") that was satisfied by a DIFFERENT goal — verifying it
meant reading a register, not the hypothesis record. Read
`residual_scope_suspected` as a PROMPT TO READ, never a determination
(guard-2028).

READ `claimed_by` BEFORE ACTING ON ANY HIT. `intended_agent` is the routing
preference; `claimed_by` is who is executing it right now. At first run 27 of 31
hits were `intended_agent: either` (reads as "mine") while claimed by a LIVE
partner — so an intended-only reading points this agent at a partner's entire
working set. The script routes those to `board-post` for you; do not re-derive
the lane by hand from `intended_agent`.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check hypothesis-terminal-goal-check)
Bash: bash core/scripts/hypothesis-terminal-goal-check.sh --output json
Parse hypothesis_terminal[] + hypothesis_dangling[] + scanned + degraded.
IF degraded is true:
    Output: "▸ ⚠ HYPOTHESIS-TERMINAL SWEEP DEGRADED (pipeline={pipeline_read_failed} goals={goal_read_failed}) — the counts below are a FLOOR, not a measurement"
IF scanned == 0:
    Output: "▸ ⚠ HYPOTHESIS-TERMINAL SWEEP SCANNED ZERO GOALS — this is a read failure, not a clean queue"
ELIF both lists are empty:
    continue silently to Phase 0.5b.18   # the clean, common case
ELSE:
    FOR EACH e in hypothesis_terminal WHERE e.action == "review-and-close" [:5]:
        Output: "▸ ⚠ HYPOTHESIS ALREADY {e.outcome}: {e.goal_id} ({e.source}, {e.priority}) still {e.status} {e.days_since_outcome}d after {e.hypothesis_id} reached {e.hypothesis_stage} (reflected={e.reflected}) — {e.title}"
        IF e.residual_scope_suspected: Output: "      residual scope suspected ({e.verification_outcome_count} outcomes, markers {e.residual_markers}) — READ the goal; the hypothesis may settle only part of it"
        IF e.reflected is false: Output: "      NOT reflected — closing may still owe reflection work"
    FOR EACH e in hypothesis_terminal WHERE e.action == "board-post" [:3]:
        Output: "▸ HYPOTHESIS TERMINAL (not my lane — {e.lane}, claimed_by={e.claimed_by}, intended={e.intended_agent}): {e.goal_id} — board post, never close (guard-1007)"
    FOR EACH e in hypothesis_dangling[:3]:
        Output: "▸ DANGLING HYPOTHESIS REF: {e.goal_id} -> {e.hypothesis_id} (absent from every stage) — can never auto-clear; repoint or remove."
    # This sweep is STATELESS (guard-1826): it re-surfaces the same hits every
    # iteration until the underlying goal changes. A hit is evidence the
    # condition HOLDS, never that it is UNREPORTED — before filing any goal or
    # board post about one, query by the goal id first (guard-2177).
```


## Phase 0.5b.18


The only lane that asks WHERE rather than WHEN: which frozen rows name a place,
and whether THIS box is it. Read the BRACKET, never a percentage — the share is
not regex-derivable (proved both directions; script docstring has the evidence).
`candidates` are rows to READ, not work to claim: a hostname can name the
blocker, a probe site, an exclusion, or spare capacity, and only a reader tells
which.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check locus-sweep)
Bash: bash core/scripts/locus-sweep.sh --output json
Parse population + bracket + this_box.counts + this_box.candidates.
IF the command exits 2:
    Output: "▸ ⚠ LOCUS SWEEP CONTROL REGRESSED — the classifier is broken; the census is NOT a clean read (guard-2421)"
ELIF this_box.counts.candidate == 0:
    continue silently to Phase 0.5b.19   # the common case on most boxes
ELSE:
    Output: "▸ LOCUS: {population} deferred, locus-bound between {bracket.floor} and {bracket.ceiling}; {counts.candidate} name a locus THIS box satisfies"
    FOR EACH c in this_box.candidates[:5]:
        Output: "    {c.goal_id} ({c.band}) — {c.why}: {c.title}"
    # Route exactly as 0.5b.12: read the row before acting, and a candidate
    # claimed by another agent gets a coordination post, never a re-route.
```

## Phases 0.5b.19-0.5b.21: Three Detective Lanes (g-115-7871)

Same deferrable shape as 0.5b.10-0.5b.18 — meter-check, run, surface, route by
lane (guard-1007), never mutate another agent's goal. All three are DETECTIVE
ONLY. A finding is a stimulus, not a verdict (guard-4794). An empty or non-zero
result is a FAILED run, not a clean queue (guard-2298).
# Rationale (WHY each exists, the measured invocation mismatches, first-run counts):
#   core/config/rationale/precheck-orphaned-detectors.md

**The invocations below are measured, not inferred.** All three reject the
`--output json` their neighbours use; one has no `--json` flag at all (rb-538 —
verify at the parser whitelist, never by analogy with a sibling).

```
# 0.5b.19 self-blocked-defer-sweep — the PREMISE-axis lane the defer family lacks:
# 0.5b.3-0.5b.15 all re-probe an EXTERNAL condition, none asks whether the defer
# waits on anything external at all (reclaim-routed-work.md's RULE axis).
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check self-blocked-defer-sweep)
Bash: py -3 core/scripts/self-blocked-defer-sweep.py --json      # `--json`, NOT `--output json`
Parse band_counts + self_blocked_candidates[] (that IS the key — NOT `candidates`,
which the sibling lanes use; rows carry goal_id/src/asp_id/status/defer_set_at/
title/defer_reason and have NO intended_agent or why field. Verified against the
emitter 2026-08-26 — the rb-538 parser-whitelist check has an OUTPUT-SHAPE twin,
and naming sibling-lane keys here surfaces zero rows beside a non-zero count).
Only `self_blocked_candidate` is actionable; a large `exogenous:*` band is the
classifier working, not a backlog.
IF it is 0: continue silently to Phase 0.5b.20
ELSE: surface up to 5 as "{goal_id} ({src}, {asp_id}, deferred {defer_set_at}): {title}"
      plus the head of {defer_reason}; read the goal to learn its lane (no
      intended_agent here), and re-run probe-before-defer.md rule 1 before touching
      one (the classifier matched TEXT; that is not a probe).

# 0.5b.20 phantom-goal-audit — all-null-provenance records, invisible to every
# age-keyed sweep here (they cannot be aged) yet still in the candidate pool.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check phantom-goal-audit)
Bash: py -3 core/scripts/phantom-goal-audit.py audit    # positional REQUIRED; emits JSON, has NO --json
Parse scanned + schema_verified + live_phantoms + already_filed_open_investigate.
IF schema_verified is not true OR scanned == 0: surface UNRELIABLE — the zero is
   not a measurement (rb-245). ELIF live_phantoms == 0: continue silently to 0.5b.21.
ELSE: surface them; file ONE consolidated Investigate unless already_filed_open_investigate.
Report-only here — `--apply` is deliberately withheld (see rationale).

# 0.5b.21 hardcoded-scope-audit — scope literals that should resolve through a
# helper (CLAUDE.md Agent-dir Resolution family).
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check hardcoded-scope-audit)
Bash: source core/scripts/_paths.sh && py -3 core/scripts/hardcoded-scope-audit.py --json \
        | py -3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d.get(k) for k in ('verdict','files_scanned','roots','roots_skipped','tier_counts')}))"
# BOTH halves are load-bearing: without `source` $WORLD_PATH is unset, world/conventions
# is dropped, and it returns SCANNED_PARTIAL — a real number over a corpus missing its
# domain half. Without the projection the body is ~144KB.
IF verdict == "SCANNED_PARTIAL" AND roots_skipped: surface it — counts are a FLOOR.
IF files_scanned == 0: surface READ FAILURE. ELIF tier_counts["active-scope"] == 0:
   continue silently to Phase 0.5c.
ELSE: surface the count; pull rows with `--tier active-scope --json`, route by lane.
```


## Phase 0.5c


Rationale (WHY shape-recurring-trap sweep): `core/config/rationale/precheck-gates.md`

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check recurring-precondition-sweep)
Bash: py core/scripts/recurring-precondition-sweep.py
# Iterates world + agent queues. For each recurring goal past its time
# gate with a failing structured precondition, advances lastAchievedAt
# to now via aspirations.py update-goal. Does NOT increment
# consecutive_routine (the goal was shelved, not closed).
# Fail-open: always exits 0. Output is one line per advance.
```

Companion to cargo-cult auto-extend in core/scripts/cargo-cult-detector.py:
auto-extend fixes the "detector fires too often" symptom; this sweep fixes
one of the root causes for precondition-gated goals.


## Phase 0.5g


Periodic passive observability check. Every 50 completed goals (configured
in `core/config/aspirations.yaml` → `l1_skew_check.goal_cadence`), compute
per-L1 distribution (structural mass, retrieval volume, mature capability
mass) and post a coordination-board `findings` message on a taxonomy-shape
defect: dominance (one L1 >= 90% of a metric's mass), share_creep (dominant
L1 grew >= 3pp since last fire), or empty_l1. Max/min ratios ride along as
evidence but no longer gate the post (g-115-2455 — a tiny-but-healthy
min-denominator L1 made the ratio unsatisfiable by any taxonomy action).

NOT a user-facing ritual — no email, no pending-question. The board post
gives partner agents and /fresh-eyes-tree (S5) cross-session visibility
into when the L1 boundaries look wrong. Quiet on balanced state.

The cadence gate and the check itself are both inside `l1-skew-check.py`
— a single script, one bash call from this phase. Fail-open: any error
prints to stderr and the loop continues. Exit code 1 on noop is silent.

```
# Budget meter — Magic Wand 2 (g-115-509). Skip when zone==tight.
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check l1-skew-cadence)
# Successor is 0.5g.5, NOT Phase 1 (g-115-3830; successor updated g-115-3222
# when 0.5g.5 was inserted). Both pointers here once read "Phase 1", which skips
# the intervening sweeps entirely. Every successor phase carries its OWN budget
# meter, so handing control to one under a tight budget costs nothing — it gates
# itself and continues onward on its own drop. That is the whole point of the
# immediate-successor rule: every phase must get its own budget decision rather
# than inherit a neighbour's. When inserting a phase here, update these pointers
# to the NEW immediate successor — a stale pointer silently skips it forever,
# which is the orphan class g-115-3222 exists to close.
Bash: core/scripts/l1-skew-check.sh --cadence --post-board
IF exit 0 (fire — cadence crossed, check ran):
    # Script printed its JSON verdict to stdout (LLM context). Board post
    # already fired if any_flagged. Continue silently to Phase 0.5g.5.
    continue
IF exit 1 (noop — cadence not crossed):
    continue
IF exit 2 (stats read error):
    # Stderr already noted. Fail-open. Continue.
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from `/tree stats` (one-shot, depth-only) and `/reflect` Step 7
Tree Health Lint (per-node staleness + cross-refs + width). Those check
NODE health; this checks TAXONOMY shape. The output feeds the /fresh-eyes-tree
ritual (S5) which assembles the briefing when the cadence-300-goal ritual
fires — board posts make the L1 skew visible BEFORE that joint review,
so partners (alpha/bravo) have signal to interpret on their own iterations.
See `core/scripts/l1-skew-check.py` and `core/scripts/tree.py
_compute_by_l1_stats`.


## Phase 0.5g.5


Periodic passive observability check, sibling to 0.5g. Every 100 completed goals
(`core/config/aspirations.yaml` → `scar_tissue_check.goal_cadence`) it measures the
framework's complexity surface and posts a `findings` board message when there is
signal. It is the periodic caller `complexity_budget.py` was written for and never
had: that script's docstring says it exists to give "the scar-tissue review cadence
an objective number to move", but measured 2026-08-01 it had ZERO callers, so the
additive ratchet ran both unopposed and unmeasured (g-115-3222).

Reports two DIFFERENT corpora side by side — they are not interchangeable, and the
originating goal conflated them:

- **half A — FILE surface**: gates, rules, skills, scripts, conventions, plus the
  orchestrator and aspirations.yaml line counts, appended to
  `meta/complexity-ledger.jsonl` so the TREND is visible rather than a spot value.
- **half B — STORE corpus**: guardrail + reasoning-bank active:retired ratio, the
  never-marked-helpful population, and a BOUNDED retirement slate.

**Proposal only, structurally.** The script has no `--apply` path and imports no
mutation helper, so it cannot retire anything even if invoked wrongly. The slate is
input to agent judgment; acting on it stays a deliberate
`bulk-retire-dead-entries.py --apply` run by an agent that has read it. Retirement
is reversible (`update-field <id> status active`), but automating it would replace
one unopposed ratchet with another pointing the other way.

Not in the Phase 0.5e cadence battery, by that registry's own documented scope: the
battery is for cadences "whose fire-action is a single LLM SKILL INVOCATION", and
this one is SELF-ACTING (it posts to the board inside the script) with no
`/scar-tissue-review` skill to invoke. Same exclusion `l1-skew` carries.

Quiet on a clean bill of health: a flat surface with an empty slate posts nothing,
because an instrument that posts on every fire trains its readers to skip it. A
`shrinking` surface is likewise not signal — subtraction is the goal, not an alarm
(`learning-philosophy.md` rule 5).

```
# Budget meter — deferrable cadence sweep (sibling to 0.5e/0.5f/0.5g).
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check scar-tissue-cadence)
# Immediate successor is 0.5g.6, which carries its own meter (see the note in 0.5g).
# Repointed from 0.5h when 0.5g.6 was inserted (g-115-6260) — a drop must skip ONE
# phase, never a span, or it silently disables everything in between.
Bash: core/scripts/scar-tissue-check.sh --cadence --post-board
IF exit 0:
    # Either the cadence had not crossed (noop — silent) or the check ran and
    # printed its report to stdout, having already posted to the board if there
    # was signal. Both are healthy. Continue to Phase 0.5h.
    continue
IF exit 2 (measurement FAILED — could not run):
    # NOT a clean bill of health. An instrument that fails silently manufactures
    # the confidence it should withhold: "no growth reported" would otherwise be
    # indistinguishable from a crash. Stderr carries the cause. Fail-open —
    # continue to Phase 0.5h; the next cadence retries.
    continue
Bash: echo "aspirations-precheck phase documented"
```

Distinct from 0.5g, which checks knowledge-TREE taxonomy shape. This checks the
FRAMEWORK's own carrying cost — the defense portfolio (gates/rules/guardrails) and
the memory stores. See `core/scripts/scar-tissue-check.py`,
`core/scripts/complexity_budget.py`, and `.claude/rules/learning-philosophy.md`
rule 5.


## Phase 0.5g.6


Surfaces goals that are FINISHED but still held at `in-progress` by a worker Body
whose liveness carrier is DEAD. Nothing else closes them: the reducer that would
verify them is gone, and `goal-selector` deliberately refuses to re-execute a goal
claimed by the same mind from another Body. So the work is paid for and unbanked.

**Why this phase exists even though the population is already printed every
iteration.** `stranded-claim-sweep.py` emits the count on every loop entry and it
reads as HEALTH, because its headline is `scanned=346 / kept=346 / released=0` — a
100%-kept sweep looks *cleaner* than a partial one. Measured 2026-08-15 (zeta,
`hostname` cc-02, `uname -r` 6.8.0-137-generic): **338 completed-not-closed, up
from 305 the previous day**, with that reassuring line printed the whole time. The
triage script had existed since 14:22 that day with **zero call sites** anywhere
in `core/`, `.claude/`, the cadence registry or `aspirations.yaml` — its only
reference was a suggestion string inside the sweep's own stderr, addressed to a
human who happened to be reading. Producer shipped, consumer absent: `rb-7741`,
reproduced in the very reporter built to fix it. This phase is the consumer half.

```
# Budget meter — deferrable cadence sweep (sibling to 0.5e/0.5f/0.5g/0.5g.5).
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check completed-not-closed-cadence)
# (repointed from 0.5h when 0.5g.7 was inserted, 2026-08-16 — a drop must skip
#  ONE phase, never a span; 0.5g.7 is always-run and must not be skipped here.)
Bash: core/scripts/completed-not-closed-triage.sh --cadence --post-board
IF exit 0:
    # Either the cadence had not crossed (noop — one cheap WM read, no sweep
    # subprocess) or the lane ran, printed the oldest N and posted to the board
    # if a backlog existed. Both are healthy. Continue to Phase 0.5g.7.
    continue
IF exit non-zero:
    # Fail-open — this is an observability instrument, not a gate. Stderr carries
    # the cause; the next cadence retries. Continue to Phase 0.5g.7.
    continue
Bash: echo "aspirations-precheck phase documented"
```

**This lane has no `--apply` and must never be given one.** Both obvious remedies
are measured-rejected, so do not re-derive them. RELEASE converts "held for the
reducer" into "available to anyone", and the scorer then ranks finished work FIRST
on its fresh metadata (`g-115-5177`). BLIND-CLOSE by classifying `outcome_note`
was measured on this exact corpus: a 58% false-positive rate flagging not-done
(the tokens match TOPIC words — a note reading "DONE. … 0 failed" flags on
"failed"), and **22 of 423 notes whose head carries a positive verdict word also
say in that same head that they are NOT finished** (`g-115-6138`: "DIAGNOSIS
COMPLETE, FIX NOT DONE"). Closing those buries open work under a false verdict,
and a wrong predicate applied across 338 goals is unrecoverable at that scale.
The lane therefore reports the note's own first line VERBATIM and computes no
verdict — `guard-2852c` ("LENGTH IS NOT VERDICT") applied to the tooling rather
than only to the reader. Same posture as `scar-tissue-check`, for the same reason.


## Phase 0.5h


Periodic self-health regression check + (Phase-3) tiered revert. Spec:
`core/config/conventions/health-ledger.md` §8–§11. Reads the per-agent health
ledger (`agents/<agent>/health/<date>.jsonl`, appended each iteration by
iteration-close.sh) and evaluates the triple-condition gate (negative composite
trend AND composite below floor AND below_baseline) plus a
consecutive-below-baseline counter (one-off bad iterations do not trip). On a
trip it identifies the most-degraded component signal, attributes the regression
to recent in-window file changes (ranked + constitutional-ring-classified), files
an `Investigate:` goal, and — when revert-eligible — routes the top candidate to
a tiered revert.

**LIVE** — `health_regression.mode: full` (`core/config/aspirations.yaml:1921`) and calibrated, since 2026-07-14 under explicit user authorization. Measured 2026-07-30 (bravo, cc-05): `health-regression-check.sh --json` returns `mode:"full", calibrated:true`, and a non-trip reports `reason:"interval not elapsed (N/10)"` — never `reason:"mode=collect-only"`.

This header asserted **DORMANT (launch default `collect-only`)** in the present tense until 2026-07-30: the launch default, never re-read after the mode advanced. Nothing failed, so nothing surfaced it — the rb-5818 expired-reason class, and the SECOND occurrence of it in this file. The sibling in the cadence-battery note (~L1622) was corrected earlier the same day and deliberately QUOTES the old wording in order to retract it; do NOT "fix" that one, and anchor any check on a LIVE claim rather than a bare `grep -q DORMANT` (guard-1685 referent trap — the token survives its own correction).

The mode gate itself is unchanged and still governs: `collect-only` → `tripped:false reason:"mode=collect-only"`; `detect-and-report` (Phase 2) adds Investigate reports but never reverts; `full` (Phase 3) additionally grants tiered revert.
Rationale (WHY the mode gate): `core/config/rationale/precheck-gates.md`

```
# Budget meter — deferrable cadence sweep (sibling to 0.5e/0.5f/0.5g).
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check health-regression-cadence)

# (Phase 3) Verify any pending reverts from prior iterations FIRST — keep or
# undo+dead-end each whose verification window has elapsed. No-op unless
# mode==full + calibrated + a pending entry exists. Cheap; runs every iteration.
Bash: bash core/scripts/health-revert.sh verify --json   # outcomes logged to context

Bash: verdict=$(bash core/scripts/health-regression-check.sh --json)
Parse verdict JSON.

# Calibration-complete edge (fires ONCE, in ANY mode incl. collect-only). When
# the 30-day/50-record AND-gate is first satisfied, revert authority (Phase 3)
# becomes mathematically eligible. File a one-time goal so the agent proactively
# advances `health_regression.mode` along the rollout rather than silently
# waiting. health-regression-check.py writes a per-agent `.calibrated` marker so
# the edge never re-fires; the dedup query (incl. completed) makes it team-wide
# idempotent — the first agent to calibrate files the single goal.
IF verdict.calibration_just_completed == true:
    # --goal-field origin_signal (EXACT match on the FIXED team-wide key), NOT
    # --title-contains (title is prose; a substring search on the hyphenated key
    # would be VACUOUS and re-file per agent). Includes completed so a finished
    # calibration goal still dedups — "the first agent to calibrate files the
    # single goal" (g-115-2196). Proven: this query matches the live g-001-318.
    Bash: existing=$(bash core/scripts/aspirations-query.sh --goal-status pending,in-progress,completed --goal-field origin_signal "idea:health-ledger-calibration-complete")
    IF existing is empty:
        # Goal fields go in the JSON body via stdin -- NOT as CLI flags.
        # aspirations-add-goal.sh hard-rejects --title/--priority/--participants/
        # --category/--status/--description with exit 2 (script lines 97-105).
        # --source agent: asp-001 here is the AGENT maintenance queue; the world
        # queue ALSO has an asp-001 ("Explore and Learn") — omitting --source
        # mis-files this per-agent health goal there (g-115-2304).
        Bash: cat <<'JSON' | bash core/scripts/aspirations-add-goal.sh asp-001 --source agent
        {
          "title": "health-ledger calibration complete -- advance health_regression.mode when ready",
          "priority": "MEDIUM",
          "participants": ["agent", "user"],
          "category": "framework-architecture",
          "status": "pending",
          "origin_signal": "idea:health-ledger-calibration-complete",
          "description": "The health-ledger calibration AND-gate is now satisfied (<verdict.calibration.days> days / <verdict.calibration.records> records). Revert authority (Phase 3) is mathematically eligible. The rollout advances by editing health_regression.mode in core/config/aspirations.yaml; each step is reversible. (1) collect-only -> detect-and-report is LOW risk (adds Investigate reports, NEVER reverts) -- agent-judgable. (2) detect-and-report -> full GRANTS the agent authority to auto-revert its own Ring-3 framework changes (Ring 1.5/2 route to agent/user Unblocks, Ring 1 to the user) -- this is a deliberate, user-paced authority grant: leave at detect-and-report and let the user advance to full. Spec: core/config/conventions/health-ledger.md section 10. dedup:<verdict.calibration_dedup_key>"
        }
        JSON

IF verdict.tripped != true:
    # collect-only no-op, interval not elapsed, or gate not tripped — all silent.
    continue to Phase 1

# TRIPPED (only reachable in detect-and-report / full mode). Dedup, then file.
# --goal-field origin_signal (EXACT match on the per-signal key), NOT
# --title-contains: the dedup_key (health-regression:<signal>:<date>) lives in
# the DESCRIPTION and the title is prose, so a title-substring search would be
# VACUOUS and fail open into a duplicate Investigate per trip (g-115-2196).
# origin_signal dedups per-signal regardless of date — the intended posture
# ("an open Investigate for this regression already exists").
Bash: existing=$(bash core/scripts/aspirations-query.sh --goal-status pending,in-progress --goal-field origin_signal "investigate:health-regression-<verdict.signal>")
IF existing is non-empty:
    # An open Investigate for this regression already exists — do not double-file.
    continue to Phase 1

Compose the Investigate description from the verdict:
  - degraded signal, window [after → before]
  - composite vs baseline, composite_trend, consecutive count
  - top attribution candidates: each "<score> ring=<ring> <authority> <path> (<commit>)"
  - evolution_change_in_window (if true: "NOTE: a meta-strategy change occurred
    in this window — the dip may be an intended evolution experiment, not a bug")
  - calibration status + revert_eligible (so the reader knows whether Phase-3
    reverts are active yet)
  - the dedup_key (for the next sweep's dedup query)

# Goal fields go in the JSON body via stdin -- NOT as CLI flags (script rejects
# --title/--priority/--participants/--category/--description with exit 2).
# --source agent: per-agent health goal belongs in the AGENT asp-001, not the
# world queue's identically-numbered "Explore and Learn" (g-115-2304).
Bash: cat <<'JSON' | bash core/scripts/aspirations-add-goal.sh asp-001 --source agent
{
  "title": "Investigate: health regression on <verdict.signal>",
  "priority": "MEDIUM",
  "participants": ["agent"],
  "category": "framework-architecture",
  "origin_signal": "investigate:health-regression-<verdict.signal>",
  "description": "<composed description above>"
}
JSON

# (Phase 3) Tiered revert — only acts when verdict.revert_eligible (mode==full
# AND calibrated). The route command re-checks the gate internally, so passing a
# non-eligible verdict is a safe no-op.
IF verdict.revert_eligible == true:
    # OD-7 courtesy: if the top candidate's file was last committed by ANOTHER
    # agent (git log -1 --format=%an <path>), post a coordination-board courtesy
    # note BEFORE routing, so the partner sees the revert. (Mirror goal deferred.)
    Bash: action=$(bash core/scripts/health-revert.sh route --verdict "$verdict" --json)
    Parse action JSON:
      - decision == "auto-revert": the file was reverted + tracked as pending
        (the verify sweep above will keep/undo it later). Note action.revert.commit.
      - decision in ("agent-unblock","user-unblock"): file the action.unblock spec
        via aspirations-add-goal.sh (participants from action.unblock.participants;
        for user-unblock, also notify the user via the forged notification skill
        per .claude/rules/forged-skill-resolution.md).
      - decision in ("not-eligible","skip-ring0"): no-op.

continue to Phase 1
Bash: echo "aspirations-precheck phase documented"
```

## Folded 0.5i + 0.5j: Curriculum and Evolution cadences, now in the Phase 0.5e Cadence Battery (g-115-2984)

The curriculum-cadence (24h, g-115-1801) and evolution-cadence (g-115-2240)
CHECKS now run in the **Phase 0.5e Skill-Invocation Cadence Battery** above,
alongside the four fresh-eyes/felt-sense cadences — ONE un-skippable call for all
six, checked at the 0.5e position (before 0.5g/0.5h; no ordering dependency —
both are idempotent via their own stamps). Their FIRE dispatch lives in that
battery phase's dispatch loop:
- **curriculum** — guard-33 invariant preserved (NEVER call `curriculum-promote.sh`
  directly): read-only `curriculum-evaluate.sh` → stamp `last_curriculum_eval` →
  route to `/curriculum-gates` ONLY when all gates pass (the SOLE guard-33
  promotion chokepoint; register+defer, non-blocking, deduped). Precheck remains
  a caller of that chokepoint alongside consolidation + evolution.
- **evolution** — precheck-side net so recurring-heavy sessions (which bypass
  Phase 8.8) don't starve evolution; `/aspirations-evolve`'s mandatory final write
  stamps `last_evolution_at_time` + bumps `evolutions_this_session`
  (loop-state-bump-counters --evolution-fired), keeping it idempotent with Phase
  8.8 and respecting `global.max_evolutions_per_session`.

This collapse eliminated the six abbreviate-able per-phase gate calls that let
felt-sense starve 3 days / 581 goals (g-115-2982). The rituals themselves are
unchanged; only the CHECK moved into the battery. Distinct from Phase 8.8
(non-recurring close path evolution check) — the battery is the precheck-side net
that survives recurring-heavy sessions.

