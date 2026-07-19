# Rationale: aspirations-precheck Gate Narratives

Referenced from `.claude/skills/aspirations-precheck/SKILL.md` — Phases 0-pre,
0-pre2.5, 0-pre3, 0-pre4, 0.5b.1b, 0.5b.4, 0.5b.5, 0.5b.6, 0.5b.7, 0.5b.8,
0.5b.9, 0.5b.10, 0.5c, and 0.5h. Contains canonical incidents, design rationale,
test coverage references, and implementation cross-references extracted to
reduce the hot-path reload size of aspirations-precheck/SKILL.md.

**DO NOT MODIFY core/scripts/precheck-eval.py or runner-dead-check.sh** —
they are the SSOT for their respective checks.

## Why the consumption-aware canary stamp in force_tree_maintain (Phase 0-pre)

`stale-sentinel-canary.py` uses a CONSUMPTION_AWARE key for the
`force_tree_maintain` slot — mirroring Phase 0-pre3's `fresh_eyes_last_dispatch`
stamp. Without the stamp, the canary reverts to bare presence-count and
FALSE-fires for deep-close-heavy agents: the drift-gate re-arms
`force_tree_maintain` at `iteration-close` (do_state_update) and the canary
samples it SET at the same close (do_productivity_check), BEFORE this consumer
clears it next iteration — so the sentinel reads "set" at sample time even
though it is consumed every iteration (charlie/echo accumulated stuck-counts
while the sentinel was null between iterations). The consumption-aware canary
counts toward "stuck" ONLY while this dispatch timestamp stays FROZEN, so a
genuinely-bypassed consumer still fires while a keeping-up one does not.

Stamp on ANY handling (both the encoding-drift lightweight path and the heavy
`/tree maintain` path — the stamp comes after the IF/ELSE, before the clear)
and stamp FIRST: an interrupt then leaves stamp-done + sentinel-still-set, which
the canary reads as "advanced → reset" (the safe direction).

Origin: g-115-1649.

## Why the evolution-finalize gate exists (Phase 0-pre2.5)

Measured 2026-07-14 across `world/self-evolution.jsonl` (6 agents, 65 MATERIAL
Self edits): 30 were git-sweep backfill (notification correctly not expected),
11 emailed the user, 2 were user-directive — and **22 EXPIRED UNNOTIFIED**. Where
notification was owed it fired 11/33 = **33%**. On 2026-04-22 the user explicitly
traded "ask first" for "notify after, revert if wrong" (guard-380). The
notify-after half was silently not executing, so the autonomy the agents were
exercising had never been paid for.

Mechanism: `evolution-record.py` writes an `awaiting_completion` stub on every
`self.md` / `program.md` edit. `evolution-complete.sh` records the WHY and fires
the guard-380 email. **Nothing ever PROMPTED that call.** 24h later
`evolution-stub-expiry.py` transitions the stub to `expired`. Every sibling
obligation in the rb-428 family already had a forcing consumer (tree-debt 0-pre,
experience-archival 0-pre2, fresh-eyes-code 0-pre3, metric-encoding 0-pre4);
self-evolution finalization was the only one that did not — so it simply did not
happen, and nothing noticed.

**Why the producer runs AFTER `evolution-stub-expiry.py`, not before**
(`iteration-close.sh`): expiry is the honest FALLBACK, not the bug — its docstring
explicitly refuses to fabricate `reasoning='[AUTO-FILLED]'` and records the honest
terminal state instead. Ordering the pending-check after it guarantees the sentinel
can never name a stub that `evolution-complete.sh` would then refuse, which would
wedge the gate in a re-fire loop.

**Why scoped to `self` + `program` only.** Those two carry the guard-380
user-notification promise and are low-volume. `script-evolution` measured 152
pending / 1992 expired vs 23 final (a **99% expiry rate**) — a firehose. Widening
the gate to it without first fixing that would fire every iteration forever and
train the agent to ignore the sentinel, destroying the gate for the streams that
matter.

**NEVER FABRICATE.** If the rationale for a stub is genuinely unrecoverable, leave
it and let expiry record `expired`. An honest "we never recorded this" is worth more
than an invented one — inventing rationales for identity changes is precisely the
failure the expiry sweep was written to avoid.

Cross-references: rb-428 (sentinel-lifecycle pattern), guard-380
(autonomous-self-evolution notify-after), guard-1076 + rb-3429 (the
reflection-vs-SSOT registration defect the full suite caught while landing this),
`core/scripts/evolution-stub-pending-check.sh` (producer),
`/verify-learning` § `evolution-finalize-gate-wiring` (3-leg assertion).
Origin: g-115-2180.

## Why the fresh_eyes_last_dispatch stamp (Phase 0-pre3)

`stale-sentinel-canary.py` keys on the dispatch timestamp ADVANCING across
samples instead of bare presence-count. This sentinel is unique among the tracked
four in that its writer (`iteration-close.sh` `do_state_update`) re-arms it on
EVERY substantive deep close, so a bare presence-count canary false-fires even
when this consumer keeps up. The canary keys on the dispatch timestamp ADVANCING
across samples instead — hence the mandatory stamp on every handling.

Cross-references: rb-428 (sentinel-lifecycle pattern), guard-343 (post-state-update
review enforcement), g-115-280 (gap discovery — Phase 0-pre/0-pre2 had consumers,
this slot did not), g-115-1553 (consumption-aware canary fix).

Stamp on ANY handling: dispatch OR a justified no-dispatch clear (e.g. the files
turned out to be partner-attributed). Stamp FIRST so an interrupt leaves
stamp-done + sentinel-still-set, which the canary reads as "advanced → reset"
(the safe direction).

## Why the metric-encoding gate exists: canonical incident g-115-707 / rb-917 (Phase 0-pre4)

Canonical incident (g-115-707): an agent closed a deep-outcome goal with
measurable production metrics in `outcome_note` prose. The goal had
`verification: null` — the Verified Values lived in free-form `outcome_note`. No
bash gate inspected the content. Encoding lagged ~50 min until a partner agent's
refresh sweep caught it manually. Filed g-115-707 Investigate → rb-917 +
content-vs-counter-gate decision rule + g-115-724 Apply.

This is a content-gate sibling to the rb-428 counter-gate family (tree-debt,
experience-archival, evolution-finalize, fresh-eyes-code, tree-encoding-drift)
— it catches "LLM did the encoding step on the wrong content" rather than "LLM
skipped the encoding step entirely."

## Why the inbox-alert severity ladder and shared cooldown (Phase 0.5b.1b)

Severity ladder (config: `proactive_escalation.inbox_alert_age_hours`; the two
values are per-severity RE-NOTIFY intervals, so classification maps the
LONGER-aged alert to the MORE-urgent HIGH — g-115-1539):
  - age >= max(`high`, `medium`) (default 12h) → fire HIGH-severity notification
  - age >= min(`high`, `medium`) (default 4h)  → fire MEDIUM-severity notification
  - Cooldown via a SHARED, DURABLE coordination-board scan (g-115-1533):
    before emailing, the sweep scans for a recent `inbox-alert-aged`
    breadcrumb for this goal_id from ANY agent. Re-fire interval matches
    the severity's threshold (HIGH re-notify every 4h, MEDIUM every 12h)
    so urgency cadence tracks severity. A goal aging FURTHER into HIGH
    after a prior MEDIUM fire re-notifies under the HIGH schedule. (The
    original per-agent `wm.proactive_escalation_log` cooldown was the
    email-side twin of the g-115-1531 handoff bug: N agents each emailed
    the user about the same unclaimed alert, and a WM reset re-fired.)

## Phase 0.5b.1b: Test coverage and implementation references

Tests: `core/scripts/tests/test_inbox_alert_age_check.py` (7 cases — no
aged alert noop, aged HIGH fires, cross-agent board-scan cooldown noop,
board post outside window fires, other-goal board post does not suppress,
plus the two candidate-filter skips). g-115-848 provided the first 3;
g-115-1533 swapped the per-agent-WM cooldown case for the cross-agent
board-scan cases (mirroring the g-115-1531 handoff sibling).

See `core/scripts/inbox-alert-age-check.py` `_classify_severity` for the
threshold ladder and `_read_recent_escalations` for the shared board-scan
cooldown discipline.

## Phase 0.5b.2b: Test coverage and implementation references

Tests: `core/scripts/tests/test_handoff_aging_check.py` (5 cases — no-aged
noop, aged fires, cooldown noop, self-routed skip, missing-created_at skip).
See `core/scripts/handoff-aging-check.py` `run()` for the scan + cooldown
logic and `_read_goals` for the fail-open all-queue read.

## Why the stale-defer dependency sweep and design constraints (Phase 0.5b.4)

Motivation (session 55 iter 80): the agent's own g-115-71 sat deferred on
g-115-87 for 3 days after g-115-87 completed — no mechanism re-probed the
dependency chain until a manual inspection cleared it. This sweep mirrors
`blocker-recheck.sh` (Layer C for participants:[user] blockers) and 0.5b.3
(structured preconditions), extending the same re-probe pattern to the
LLM-authored free-form `defer_reason` surface.

Conservative by design: skips non-pending goals (status filter), requires
ALL cited deps to be completed (partial completion stays deferred), and uses
two distinct regex patterns — structured (`blocked_on_dependency: g-X`) and
proximity (`g-X <verb>` / `<verb> g-X`). Free-form defers that don't match
either pattern are reported but not cleared.

See `.claude/rules/probe-before-defer.md` and the rb-428 bash-consolidation
drift family for the upstream pattern this sweep counters.

The three STRUCTURED_DEFER_PREFIXES (defined in `core/scripts/gates/defer_classifier.py`)
each have their own auto-clear path: `precondition_unmet:` is handled by
Phase 0.5b.3 (precondition-defer-recheck.sh), `blocked_on_dependency:` is
handled here in Phase 0.5b.4 (defer-recheck.sh dependency regex), and
`Circuit breaker:` is filed by the aspirations loop's Phase 5.5
(per `core/config/aspirations-loop-digest.md`) when
`consecutive_goal_failures >= 3`, and cleared on the next successful
attempt. All three bypass the capability-gate's narrative-defer check via
`is_narrative_defer()` so machine-written internal markers never
keyword-collide with forged skills.

## Why the pending-questions sentinel sweep exists (Phase 0.5b.5)

Closes the gap discovered by g-115-485 / g-001-226: pending-questions whose
`source_goal` field names a goal that has since completed/superseded silently
linger forever (canonical incident: a publish-related pending-question lingered
12d after both its origin goal and a follow-up superseder both completed). The
sweep adds a `source_goal-completed` heuristic and a `--apply` mutation flag —
same single-writer, idempotent, fail-quiet pattern as `defer-recheck.sh --apply`,
`blocker-recheck.sh --apply`, and `monitor-stale-check.sh --apply`.

See `core/scripts/pending-questions-sweep.py` `_h_source_goal_completed` for
the heuristic and `_apply_auto_resolve` for the atomic mutation. Future
sentinel-lifecycle gaps that also need same-iteration cleanup belong in this
sweep, not in a new precheck phase.

## Why the parent-supersession sweep: canonical incident g-268-10 / rb-842 (Phase 0.5b.6)

Closes the supersession-blindness gap that produced g-268-10 (rb-842): a
parent "Apply: X" goal carried `defer_reason: blocked_on_design` for hours
while two sibling goals (Design + Apply decomposition) completed the same intent
ABOVE it in the queue. The defer-recheck sweep cleared the defer but the parent
re-emerged at high score because its description no longer pointed at unfinished
work — leading to spurious selection. This sweep catches that incident shape at
parent-supersession time, BEFORE the defer-recheck loop has a chance to
re-promote it.

Heuristic shape: parent goal carries `Apply:` title + has a temporal reference
(`defer_reason_set_at` or `created_at`) + ≥2 sibling goals in the same
aspiration with `Design:`/`Apply:` titles completed AFTER that reference
timestamp. Sprint-scope guard: only aspirations with `≤max_aspiration_goals`
(default 50) qualify — large recurring aspirations like asp-115 (611 goals)
produce false positives at any threshold because parents and unrelated
completions co-exist by design.

Tests at `core/scripts/tests/test_parent_supersession_sweep.py` pin the 8-case
contract (canonical incident + 7 false-positive rejections). See
`core/scripts/parent-supersession-sweep.py` `_find_superseding_siblings`
for the heuristic and `_mark_superseded` for the atomic mutation.

## Why the unblock-parent-status sweep: canonical incident g-250-73 / rb-908 (Phase 0.5b.7)

Closes the Layer-D-auto-Unblock-outlives-parent gap that produced g-250-73
(rb-908). When `capability-gate.py --suggest-unblock` files an auto-Unblock at
defer-write time and the parent goal then lands in a terminal non-execution state
— `skipped` (WRONG LAYER finding), `completed`, `superseded`, or `archived` —
the Unblock survives as actionable work even though its premise has dissolved.
Layer D writes synchronously and never re-probes the parent; this sweep is the
re-probe.

Canonical incident: g-250-73 'Unblock: behavior for g-250-69' filed at T+0s,
g-250-69 SKIPPED at T+72s with WRONG LAYER finding ("bumping AspirationalModule
weights would violate TestAspirationalModuleInvariants:461; fix routes through
IntentEngineVerticle.scoreCandidate fallback instead"). Without this sweep,
g-250-73 would have lingered as a pending Unblock until manual inspection
caught it.

Heuristic shape: Unblock-titled goal + parseable parent goal-id (from
`origin_signal "unblock:<g-id>"`, title `"Unblock: <verb> for <g-id>"`, or
`discovered_by` field) + parent.status in {skipped, completed, superseded,
archived}. Title-anchored ("Unblock:" prefix) — does NOT match
`Investigate:`/`Idea:`/`Apply:`/`Recurring:` goals that happen to carry
`origin_signal: "unblock:..."` for unrelated reasons (false-positive shape
observed in g-249-06 / g-250-77).

Tests at `core/scripts/tests/test_unblock_parent_status_sweep.py` pin the
12-case contract (canonical g-250-73 shape, three extraction paths, idempotency,
terminal-state set, title-prefix discipline). See
`core/scripts/unblock-parent-status-sweep.py` `_parse_parent_id` for the
three-source extraction priority and `_mark_skipped` for the atomic mutation.

## Why the routing-audit-target-status sweep: canonical incident rb-1478 (Phase 0.5b.8)

Sibling to Phase 0.5b.7 — same terminal-target auto-close pattern, applied to
the routing-audit goal class instead of the Layer-D Unblock class.
`post-decompose-routing-audit.py` files `Investigate: routing-mismatch <target>`
and `Investigate: routing-either-resolve <target>` goals into asp-115 when a
freshly-stamped goal's `intended_agent` disagrees with the best Self.md
domain-token Jaccard match. The audit goal's primary action is to re-stamp the
TARGET's `intended_agent`. When the target lands in a terminal status
(completed/archived/skipped/superseded), the re-stamp is MOOT and the audit goal
survives as actionable work whose premise dissolved.

Canonical incident (rb-1478 / exp-g-115-1329): routing-either-resolve fired a
re-stamp (either→delta) on g-115-1328 which was ALREADY completed 2026-06-03
(re-stamp moot) AND content-contradicted. This "terminal-target" sub-mode is
distinct from the content-FP-on-a-PENDING-target sub-mode (g-115-1346) and the
metric-bias root (rb-1249 / g-115-1200). The routing-mismatch path runs ~82% FP
(rb-1478), so auto-closing on terminal target retires the dominant moot case; a
genuine systemic capability_route table-gap, if real, re-fires on the next
decompose (the audit runs every decompose) rather than lingering as a stale goal.

Tests at `core/scripts/tests/test_routing_audit_target_status_sweep.py` pin the
15-case contract (both origin_signal forms, title fallback, unparseable generic
shape, class membership incl. Unblock-rejection, idempotency, terminal-state
set). See `core/scripts/routing-audit-target-status-sweep.py` `_parse_target_id`
for the origin-signal-first extraction priority (discovered_by is the constant
discoverer name, NOT a target id, so it is deliberately not a parse source) and
`_mark_skipped` for the atomic mutation.

## Why the credential-defer conservative guard (Phase 0.5b.9)

Auto-clears `human_blocked:` defers whose text names an env-read/credential
probe where the credential is NOW present. The `human_blocked:` class previously
had age-escalation but no auto-clear path — defers accumulated indefinitely once
a credential was provisioned, requiring manual agent intervention. This sweep
closes that gap for the agent-re-provisionable sub-class.

Conservative guard: only clears when (a) an env/credential indicator word
(`credential`, `env-read`, `env-var`, `env-key`) appears in the defer text, AND
(b) a well-formed env-var key (`ALL_CAPS_WITH_UNDERSCORE`, min 4 chars, at least
one underscore) can be extracted from the text, AND (c)
`bash core/scripts/env-read.sh has <KEY>` exits 0 (key now present).

Defers with no extractable key — genuinely human-only blocks such as "user
approve-click", "legal counsel sign-off", GUI/hardware actions — are classified
`skipped_no_key` and NEVER auto-cleared. This mirrors the probe-before-defer.md
/ capability-before-user.md principle: only agent-re-provisionable defers get
the auto-clear; human-gated defers remain frozen until explicitly resolved. See
`credential-defer-recheck.py` docstring constraint block.

Tests at `core/scripts/tests/test_credential_defer_recheck.py` pin the 14-case
contract (all three explicit patterns, indicator-gated fallback, human-only no-op
cases, prefix-stripping, edge cases incl. no-underscore key rejection). See
`core/scripts/credential-defer-recheck.py` `_extract_env_key` for the
three-tier extraction priority and `_probe_env_key` for the env-read.sh
interface.

## Why the defer-drift detective: canonical incident 2026-06-12 asp-304 (Phase 0.5b.10)

Flags goals whose `deferred_until` has gone STALE (PAST) while a
structured-defer marker persists. This is the precise complement of Phase 0.5b.3
(`precondition-defer-recheck.py`), which deliberately SKIPS any goal that has
`deferred_until` set ("the structured time gate is the authoritative scheduler
signal"). Nothing re-probed the time gate ITSELF for drift — so when
`deferred_until` falls into the past while the precondition it represents is
still unmet, goal-selector's `deferred_readiness` criterion reads the expired
gate as "defer just expired, re-evaluate now" and BOOSTS the not-ready goal to
selector-top instead of filtering it.

Canonical incident (2026-06-12, asp-304 Layer-5 cohort): g-304-11 carried
`defer_reason "precondition_unmet: ... completes ~2026-07-11"` but
`deferred_until=2026-05-26` — a date 16 days IN THE PAST relative to its own
`defer_reason_set_at`. The selector surfaced it at score 8.84 despite ~18h of
the required 30 days of telemetry. Four goals were hand-re-gated; this check
makes the drift VISIBLE so it can never linger undetected again. See the
reasoning-bank entry "deferred_until drift from defer_reason prose makes
goal-selector deferred_readiness boost data-immature goals to top".

DETECTIVE, NOT CORRECTIVE. The script never mutates: the correct future date
lives in the `defer_reason` prose, which it cannot parse reliably (and clearing
the defer would wrongly surface a genuinely not-ready goal). It SURFACES drift
for re-gate-by-judgment — exactly the ~30s fix a human/agent applies once the
drift is known. The LLM does the (deduplicated) Investigate filing, not the
script — same detective-script + LLM-acts pattern as precheck-eval flags.

Tests at `core/scripts/tests/test_defer_drift_check.py` pin the contract
(canonical g-304-11 shape, future-gate rejection, terminal-status rejection,
free-form-defer rejection, no-deferred_until rejection, malformed-date tolerance,
min-hours-past suppression, all three structured prefixes). See
`core/scripts/defer-drift-check.py` `_classify_drift` for the eligibility
ladder and `_precondition_status` for the prose/ready/still_unmet annotation.

## Why the shape-recurring-trap sweep (Phase 0.5c)

Closes the "shape-recurring trap" (bravo reasoning-channel musing 2026-04-21):
recurring goals with STRUCTURED preconditions that fail at candidacy time never
reach aspirations-execute, so their `lastAchievedAt` never advances. The
goal-selector's urgency formula then inflates `overdue_ratio` unboundedly; when
the precondition finally unlocks, the goal fires with massive urgency on
trivially-met evidence, closes routine, and feeds cargo-cult.

Distinct from 0.5b.3 (which clears explicit `defer_reason: precondition_unmet:*`).
This sweep targets recurring goals that are NOT deferred — they silently drop out
of COLLECT at the selector's predicate filter (goal-selector.py L680–692),
leaving `lastAchievedAt` frozen.

## Why health-regression detection is DORMANT and its mode gate (Phase 0.5h)

**DORMANT until `health_regression.mode` advances.** In `collect-only` (the
launch default) the detection script returns `tripped:false
reason:"mode=collect-only"` immediately — this phase is a no-op until the mode
is advanced to `detect-and-report` (Phase 2) or `full` (Phase 3). The script
also self-gates on its own interval marker (every `detection.interval` goals).
**Phase 2 = report only.** Reverts (Phase 3) require `mode == full` AND the
calibration AND-gate (30 days AND 50 records) — surfaced as `revert_eligible` in
the verdict and re-checked inside `health-revert.py` (the master safety gate;
even a Ring-3 auto candidate routes to `not-eligible` until both hold). Reverts
are file-granular (one file restored to its pre-regression content via
`git show`), tagged with a `Health-Revert` git trailer, and verified
`revert.verification_iterations` later — kept if the composite improved, else
undone + dead-ended.

## Why the health-regression Investigate is participants:agent (Phase 0.5h)

Detection surfaces an agent-diagnosable condition (attribution + revert are
agent-capable per `.claude/rules/capability-before-user.md`). User involvement
happens at REVERT time and ONLY for Ring-1 candidates (`user-unblock`), where
the user owns the file's intent — never at detection time.

## Cross-references

- `core/scripts/precheck-eval.py` — SSOT for precheck evaluation logic; DO NOT modify via goals in ZDS-Mind
- `core/scripts/runner-dead-check.sh` — SSOT for runner-dead conditions; DO NOT modify via goals in ZDS-Mind
- `.claude/skills/aspirations-precheck/SKILL.md` — pointer source for all sections above
- `core/config/conventions/health-ledger.md` §8–§11 — health-regression subsystem spec
