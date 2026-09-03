---
name: aspirations-precheck
description: "Runs pre-selection checks at the start of every aspirations-loop iteration: completion runners, aspiration health, guardrail checks, blocker resolution re-probe, zombie-aspiration scan (Phase 0.5.0a), and recurring-goal surfacing. Use whenever the aspirations loop starts a new iteration and needs to tidy state before /aspirations-select runs. Internal sub-skill — invoked only from inside the orchestrator, never by the user."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, infrastructure, goal-schemas]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-precheck-f82982"
previous_revision_id: null
---

# /aspirations-precheck — Pre-Selection Checks

Runs all checks that must happen BEFORE goal selection each iteration.
Ensures completion runners fire, aspiration health is maintained,
guardrails are checked, blockers are resolved, and recurring goals are tracked.

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter.

**Step 0-open: RUN THE ENTRY BATTERY FIRST (g-115-6468)** — one call that IS the
executor for Step 0a's meter start, the entry checks, and every **always-run** and
**medium** lane, then prints a per-stage rc table, FINDINGS ONLY, selection
candidates, and a `NEXT ACTION REQUIRED:` imperative. Lane counts are DERIVED —
never re-type one here.

```
Bash: bash core/scripts/iteration-open.sh --apply
```

It SUBSUMES Step 0a and both the always-run AND medium phase bodies below — do
NOT also run them, or the `--apply` lanes escalate twice. Dispose what it prints,
then resume at the first `deferrable` sweep. Run the standalone fallback
(tier table) on `wrapper_failed`, on a BLIND stage, and on a KILLED run — which
reports NEITHER, so judge by rc AND bytes, not output: empty stdout at rc≠0 is
BLIND, not clean, and stderr's last `->` crumb names the stage in flight
(g-115-7844). `--dry-run` lists lanes and wiring status; an undispatched one
prints `not-yet-wired`, not nothing. The **deferrable** tier is NOT yet
wired (strangler step 3).

⚠ That sentence does not MAKE anything run — its twin said "medium" while that
tier sat dark 94-208h on two boxes (g-115-7847). Tiers run by being STAGES in
iteration-open.sh.

Rationale, composition contract, worker-vs-reducer meter split: the
`core/scripts/iteration-open.py` docstring; dark-window trace and
execution-record design: `core/scripts/precheck-medium-battery.py`.

**Step 0a: Budget Meter Start (Magic Wand 2 — g-115-509)** — *fallback only when
Step 0-open did not run; it calls this itself.* Initialize the
precheck cost meter. The meter returns "drop" for deferrable sweeps ONLY when
the zone is `tight` (`zone_drop_rules.tight`). Always-run sweeps (Phase 0-pre,
0-pre2, 0-pre3) NEVER drop. The former wall-clock budget-overrun drop path was
REMOVED (g-115-1489 — `elapsed` measured inter-tool-call LLM latency, not script
cost, so it dropped EVERY deferrable sweep every iteration and starved the
fresh-eyes/felt-sense/health-regression cadence rituals). elapsed-ms is still
logged for telemetry. Decisions log to `agents/<agent>/session/precheck-drops.jsonl`.

```
Bash: bash core/scripts/aspirations-precheck-budget-meter.sh start
```

Before any sweep that the table below marks as `medium` or `deferrable`, call
`meter check <sweep-name>` and skip the phase when the response is `drop`.
Always-run sweeps skip the check (they never drop). At the bottom of this
SKILL.md (after Phase 1) call `meter end` to write the summary record.

The **Invocation** column is the exact command (g-115-6207 — 4 of the names
below did NOT resolve to a runnable command by name alone, and every one of
those failures prints to stderr and exits non-zero, which a batched
`2>/dev/null` loop renders as an empty-stdout all-clear). Sweep NAME ≠ script
name for the two reclaim rows; subcommands are REQUIRED where shown; the
sentinel/cadence/always-run battery rows are dispatched by their battery, not
standalone scripts. The always-run rows also name a standalone fallback — those
scripts DO exist independently and are what a blind battery falls back to; the
column half is not a duplicate. Phase bodies below remain the authority on flags (--apply forms);
this column mirrors them. rc convention: rc=1 is AMBIGUOUS. precheck-eval
run-all exits 1 on ACTIONABLE FINDINGS; l1-skew-check --cadence exits 1 with
ZERO output as a documented silent noop. Judge by OUTPUT, not rc alone.
Every invocation below was run 2026-08-14 with rc recorded (report forms).

**Deferrable phase BODIES live in a digest** (g-115-6583). The table above is the
index and carries each lane's exact Invocation, so the common path needs no load.
Load the digest when a deferrable lane needs its full body — measured markers,
parse-shape warnings, incident traces:

```
Bash: bash core/scripts/load-precheck-digest.sh → IF path returned: Read it
```

Every `## Phase` header, `meter check`, and `IF decision == "drop"` line stays
INLINE below — those are control flow, not prose.

**always-run** and **medium** rows are DISPATCHED by `iteration-open.sh`; their
Invocation is the FALLBACK for a blind stage. Only **deferrable** rows are yours.

| Phase | Sweep name (for `meter check`) | Tier | Invocation (exact) |
|---|---|---|---|
| 0-pre | tree-debt-gate | always-run | dispatched by `bash core/scripts/precheck-sentinel-battery.sh` (Phase 0-pre.0d) → this file's Phase 0-pre section; no standalone script |
| 0-pre2 | experience-archival-gate | always-run | same battery → Phase 0-pre2 section |
| 0-pre2.5 | evolution-finalize-gate | always-run | same battery → Phase 0-pre2.5 section |
| 0-pre3 | fresh-eyes-code-gate | always-run | same battery → Phase 0-pre3 section |
| 0 (Recurring Safety Net) | aspirations-recover-recurring | medium | `bash core/scripts/aspirations-recover-recurring.sh --source world` then `--source agent` |
| 0 (Monitor Stale) | monitor-stale-check | medium | `bash core/scripts/monitor-stale-check.sh --apply` |
| 0.5.0 | precheck-eval | medium | `bash core/scripts/precheck-eval.sh run-all` (subcommand REQUIRED; bare call exits 2) |
| 0.5b.0.5 | blocker-recheck | medium | `bash core/scripts/blocker-recheck.sh --max-age-hours <config.proactive_escalation.blocker_age_hours> --apply` |
| 0.5b.1b | inbox-alert-age-check | always-run | dispatched by `bash core/scripts/precheck-always-run-battery.sh --apply` (Phase 0-pre.0e) → this file's Phase 0.5b.1b section; standalone fallback `bash core/scripts/inbox-alert-age-check.sh --apply` |
| 0.5b.1c | user-blocker-escalation-check | always-run | same battery → Phase 0.5b.1c section; standalone fallback `bash core/scripts/user-blocker-escalation-check.sh --apply` |
| 0.5b.2 | dependency-timeout-check | always-run | same battery → Phase 0.5b.2 section; standalone fallback `bash core/scripts/dependency-timeout-check.sh --apply` |
| 0.5b.1d | directive-mix-check | always-run | dispatched by the ALWAYS-RUN battery (Phase 0-pre.0e); standalone fallback `bash core/scripts/directive-mix-check.sh`. Read-only, no --apply. Surfaces directive-vs-actual close mix; finding is `on_directive_ok=false`. OBSERVABILITY ONLY — never a selection override (scorer sovereignty; rb-5003 surfacing != eliciting) |
| 0.5b.2b | handoff-aging-check | always-run | same battery → Phase 0.5b.2b section; standalone fallback `bash core/scripts/handoff-aging-check.sh --apply` |
| 0.5b.3 | precondition-defer-recheck | medium | `bash core/scripts/precondition-defer-recheck.sh --max-age-hours 2 --apply` |
| 0.5b.4 | defer-recheck | medium | `bash core/scripts/defer-recheck.sh --max-age-hours 2 --apply` |
| 0.5b.5 | pending-questions-sweep | deferrable | `bash core/scripts/pending-questions-sweep.sh sweep --apply` (subcommand REQUIRED; note --apply governs auto_resolve, --apply-cleanup governs needs_transition) |
| 0.5b.6 | parent-supersession-sweep | deferrable | `bash core/scripts/parent-supersession-sweep.sh --max-age-hours 24 --min-siblings 2 --apply` |
| 0.5b.7 | unblock-parent-status-sweep | deferrable | `bash core/scripts/unblock-parent-status-sweep.sh --apply` |
| 0.5b.8 | routing-audit-target-status-sweep | deferrable | `bash core/scripts/routing-audit-target-status-sweep.sh --apply` |
| 0.5b.9 | credential-defer-recheck | deferrable | `bash core/scripts/credential-defer-recheck.sh --apply` |
| 0.5b.10 | defer-drift-check | deferrable | `bash core/scripts/defer-drift-check.sh --output json` |
| 0.5b.11 | reason-less-blocked-check | deferrable | `bash core/scripts/reason-less-blocked-check.sh --apply --output json` |
| 0.5b.12 | blocked-signal-resolution-check | deferrable | `bash core/scripts/blocked-signal-resolution-check.sh --post-routing --output json` |
| 0.5b.13 | reclaim-defer-audit | deferrable | `bash core/scripts/audit-deferred-defers.sh --output json` (script name ≠ sweep name — lane B of reclaim-routed-work.md) |
| 0.5b.14 | reclaim-user-participant-audit | deferrable | `bash core/scripts/audit-user-to-agent.sh --output json` (script name ≠ sweep name — lane P) |
| 0.5b.15 | human-blocked-defer-join | deferrable | `bash core/scripts/human-blocked-defer-join.sh --output json` |
| 0.5b.16 | dependency-cycle-check | deferrable | `bash core/scripts/dependency-cycle-check.sh --output json` |
| 0.5b.17 | hypothesis-terminal-goal-check | deferrable | `bash core/scripts/hypothesis-terminal-goal-check.sh --output json` |
| 0.5b.18 | locus-sweep | deferrable | `bash core/scripts/locus-sweep.sh --output json` (read-only census — the only lane that asks WHERE, not when) |
| 0.5b.19 | self-blocked-defer-sweep | deferrable | `py -3 core/scripts/self-blocked-defer-sweep.py --json` (.py only; `--json`, NOT `--output json`) |
| 0.5b.20 | phantom-goal-audit | deferrable | `py -3 core/scripts/phantom-goal-audit.py audit` (positional REQUIRED; no `--json` flag exists) |
| 0.5b.21 | hardcoded-scope-audit | deferrable | `source core/scripts/_paths.sh && py -3 core/scripts/hardcoded-scope-audit.py --json` (the `source` is load-bearing) |
| 0.5b.22 | closed-against-own-note-check | deferrable | `bash core/scripts/closed-against-own-note-check.sh --min-confidence high` (report-only BY DESIGN — no --apply path exists and none may be added: a wrong auto-close is not cheap. HIGH tier only — medium precision is ~50% because "reopened" fires on ordinary prose. `--json`, NOT `--output json`) |
| 0.5c | recurring-precondition-sweep | deferrable | `py -3 core/scripts/recurring-precondition-sweep.py` (.py ONLY — no .sh wrapper exists) |
| 0.5c.1 | recurring-starvation-check | medium | `bash core/scripts/recurring-starvation-check.sh --apply --max-file 1` |
| 0.5e | fresh-eyes-cadence | deferrable | checked inside `bash core/scripts/precheck-cadence-battery.sh` (Phase 0.5e Cadence Battery) — do NOT meter-check or invoke separately |
| 0.5e.5 | fresh-eyes-program-cadence | deferrable | same cadence battery |
| 0.5e.7 | fresh-eyes-tree-cadence | deferrable | same cadence battery |
| 0.5e.9 | strategic-scan-cadence | deferrable | same cadence battery |
| 0.5f | felt-sense-cadence | deferrable | same cadence battery |
| 0.5g | l1-skew-cadence | deferrable | `bash core/scripts/l1-skew-check.sh --cadence --post-board` |
| 0.5g.5 | scar-tissue-cadence | deferrable | `bash core/scripts/scar-tissue-check.sh --cadence --post-board` |
| 0.5g.6 | completed-not-closed-cadence | deferrable | `bash core/scripts/completed-not-closed-triage.sh --cadence --post-board` |
| 0.5g.7 | completed-not-closed-drain | always-run | dispatched by the ALWAYS-RUN battery (Phase 0-pre.0e; named in full because the nearest preceding rows are the CADENCE battery's) → Phase 0.5g.7, which RE-RUNS `bash core/scripts/completed-not-closed-slate.sh` for the ROWS — the battery reports `slate=N`, a count. Sweep name ≠ script name |
| 0.5g.8 | world-script-crlf-check | always-run | dispatched by the ALWAYS-RUN battery (Phase 0-pre.0e); standalone fallback `bash core/scripts/world-script-crlf-check.sh`. Report-only, no --apply. WHY in the script docstring (g-115-7288) |
| 0.5g.9 | close-phase-skip-check | always-run | dispatched by the ALWAYS-RUN battery (Phase 0-pre.0e); standalone fallback `bash core/scripts/close-phase-skip-check.sh`. Report-only, no --apply. n/a on a worker (state-update is reducer-only). WHY in the script docstring (g-115-8219) |
| 0.5h | health-regression-cadence | deferrable | `bash core/scripts/health-regression-check.sh --json` (then Phase 0.5h verify/revert steps on a trip) |
| 0.5i | curriculum-cadence | deferrable | same cadence battery |
| 0.5j | evolution-cadence | deferrable | same cadence battery |
| 0.5k.1 | check-stderr-json-merge | deferrable | `py -3 core/scripts/check-stderr-json-merge.py` |
| 0.5k.2 | check-uncommitted-edits-log-freshness | deferrable | `py -3 core/scripts/check-uncommitted-edits-log-freshness.py` |
| 0.5k.3 | role-multiplier-coverage-audit | deferrable | `py -3 core/scripts/role-multiplier-coverage-audit.py` |
| 0.5k.4 | verify-rb-type-parity | deferrable | `py -3 core/scripts/verify-rb-type-parity.py` |
| 0.5k.5 | hand-command-audit | deferrable | `py -3 core/scripts/hand-command-audit.py` |
| 0.5k.6 | check-agents-parent-dir-sync | deferrable | `py -3 core/scripts/check-agents-parent-dir-sync.py` |
| 0.5k.7 | fromisoformat-idiom-guard | deferrable | `py -3 core/scripts/fromisoformat-idiom-guard.py` (rc=1 IS a finding) |
| 0.5k.8 | hook-slot-contract-check | deferrable | `py -3 core/scripts/hook-slot-contract-check.py` (rc=1 IS a finding) |
| 0.5k.9 | narrative-clobber-audit | deferrable | `py -3 core/scripts/narrative-clobber-audit.py` |
| 0.5k.10 | guardrail-pair-audit | deferrable | `bash core/scripts/guardrail-pair-audit.sh` (.sh REQUIRED — guard-3864) |
| 0.5k.11 | dropped-field-audit | deferrable | `py -3 core/scripts/dropped-field-audit.py` (~29s) |
| 0.5k.12 | unchecked-write-ratchet | deferrable | `bash core/scripts/unchecked-write-ratchet.sh` (.sh REQUIRED) |
| 0.5k.13 | tree-last-updated-drift-check | deferrable | `py -3 core/scripts/tree-last-updated-drift-check.py` |
| 0.5k.14 | goal-field-census-ratchet | deferrable | `bash core/scripts/goal-field-census-ratchet.sh` (.sh REQUIRED) |
| 0.5k.15 | check-tests-no-live-agent-wm | deferrable | `py -3 core/scripts/check-tests-no-live-agent-wm.py` |
| 0.5k.16 | embedded-python-audit | deferrable | `py -3 core/scripts/embedded-python-audit.py` |
| 0.5k.17 | tree-adjudication-scan | deferrable | `py -3 core/scripts/tree-adjudication-scan.py` |
| 0.5k.18 | displaced-id-audit | deferrable | `py -3 core/scripts/displaced-id-audit.py` (~13s) |
| 0.5k.19 | repo-hygiene-sweep | deferrable | `bash core/scripts/repo-hygiene-sweep.sh` (~77s — needs >=120s bound; g-115-8361) |

Drop semantics — the meter ONLY drops sweeps when:
1. `tier == always-run` → never drop
2. `zone == tight` AND `tier ∈ zone_drop_rules.tight` (default `[deferrable]`)
(The former rule 3 — `tier == deferrable` AND `elapsed_ms > cap_ms` — was REMOVED
in g-115-1489: wall-clock-since-meter-start is dominated by inter-tool-call LLM
latency, not script cost, so it dropped every deferrable sweep every iteration and
starved the cadence rituals. Zone-drop is now the sole drop path; `elapsed_ms` /
`cap_ms` are telemetry-only.)

Fail-open: any meter error returns `run`. The meter is velocity optimization,
not safety gating — never block the loop on a meter bug.

**Cadence-battery note (g-115-2984):** the seven `*-cadence` sweeps for the
skill-invocation cadences — `fresh-eyes-cadence`, `fresh-eyes-program-cadence`,
`fresh-eyes-tree-cadence`, `strategic-scan-cadence`, `felt-sense-cadence`,
`curriculum-cadence`, `evolution-cadence` — are NO LONGER meter-checked
per-phase. (`strategic-scan-cadence` joined 2026-08-02, g-115-4691 — it never
had a per-phase meter check to lose: its consumer phase is ORCHESTRATOR Phase
1.5, and nothing in bash read its stamp at all.) Their CHECKS run
unconditionally inside the ONE **Phase 0.5e Cadence Battery** (cheap + read-only,
so no meter gate on the check); the meter-check for each of these names happens
at DISPATCH time in the battery loop, gating the expensive ritual invocation.
Do NOT `meter check` them separately. `l1-skew-cadence` (0.5g),
`scar-tissue-cadence` (0.5g.5), `completed-not-closed-cadence` (0.5g.6), and
`health-regression-cadence` (0.5h) keep their
own per-phase meter checks — all three are SELF-ACTING or multi-step, so none fits
the battery's "gate -> exit 0 -> invoke one skill" shape.

## Inputs (from orchestrator)

- Aspirations compact data (loaded at loop entry or refreshed here)

## Outputs (to orchestrator)

- Compact aspirations refreshed in context
- Blockers updated in working memory
- Any auto-completed goals logged

## Phase 0-pre.0: Live Partner Snapshot

Read the live cross-agent snapshot once per iteration (cheap — single YAML
read with file lock). Surface partner.in_flight in the iteration header
so the LLM sees it before any reasoning about partner state.
See `core/config/conventions/coordination.md` "in_flight Field".

```
Bash: team-state-read.sh --json
partner = read agent_status.<partner-name>  # bravo if MIND_AGENT=alpha, vice versa
IF partner.in_flight is non-null:
    age_min = (now - partner.in_flight.claimed_at).total_seconds() / 60
    Output: "▸ Partner ({partner-name}): in_flight {partner.in_flight.goal_id} '{partner.in_flight.title[:40]}' phase={partner.in_flight.phase} ({age_min:.0f}m ago)"
ELSE IF partner.last_active:
    age_min = (now - partner.last_active).total_seconds() / 60
    Output: "▸ Partner ({partner-name}): no in_flight | last_active {age_min:.0f}m ago"

# Inbox-alert backlog surface (g-115-849). Read inbox_alert_backlog from the
# SAME team-state JSON already loaded above (no extra read). The domain inbox
# sweep writes it via core/scripts/inbox-backlog-update.py (null when zero);
# surface the aggregate queue depth so the iteration header shows how many
# alert-derived Unblock goals are waiting un-claimed. Complements the
# Phase 0.5b.1b age-escalation (single-alert notification) with a count.
backlog = team_state.get("inbox_alert_backlog")  # absent/None -> treat as null
IF backlog is a non-null dict AND backlog.get("count", 0) > 0:
    Output: "▸ inbox-alert-backlog={backlog.count} oldest={backlog.oldest_age_hours}h goal={backlog.oldest_goal_id}"
# ELSE: silent (null backlog or zero count)

# Stash partner snapshot in iteration context for downstream phases (select drops
# partner.in_flight.goal_id from candidates; execute Phase 4 re-reads for the
# claim-conflict gate — re-read keeps single source of truth even if partner
# transitioned between this read and the claim attempt).
```

## Phase 0-pre.0a: Partner-Belief Contradiction Check (g-306-29)

Theory-of-Mind contradiction-triggered forced reflection — the CONSUME-side
completion of the partner-belief loop (g-306-18 storage → g-306-28
write+consume). Right after the live partner snapshot above (which already
surfaced each partner's `current_focus`), compare that fresh observation against
the domain-belief THIS agent holds about each partner. On N CONSECUTIVE
contradicting observations (default 2 → no false-trigger on the FIRST), the held
belief is REVISED (confidence lowered, or superseded) and the surprise is
recorded. A belief only carries a checkable `domain` when fresh-eyes-review Phase
2.6c wrote it with `--domain`; free-form beliefs are skipped (conservative — the
source of the no-false-trigger guarantee for un-domained beliefs).

Single bash call — daemon read + pure compute (`_belief_contradiction.py`,
unit-tested) + conditional daemon write, all FAIL-OPEN (never blocks the loop).

```
Bash: bash core/scripts/belief-contradiction-check.sh
# Reads team-state agent_status.<agent>.{current_focus,beliefs} + the
# agent-private belief_contradiction_streaks WM slot, runs
# _belief_contradiction.process_all, and on a sustained contradiction lowers/
# supersedes the held belief via team-state-update + records the surprise to the
# evolution log. Prints a one-line summary ("clean" when no domain-belief
# contradiction exists). Tunables: BELIEF_CONTRADICTION_N (default 2),
# BELIEF_CONTRADICTION_MODE (lower|supersede, default lower). Self-limiting: a
# lowered belief decays below threshold and stops re-triggering, so the forced
# reflection fires once per sustained contradiction, not every iteration.
```

## Phase 0-pre.0b: Boredom Signal Surface (observability, no action)

Surfaces the routine-drift counters that Phase 4.1 of the aspirations loop
mutates (Blocks A/B/C of signal mutation — see
`core/config/aspirations-loop-digest.md` §Signal Mutation and
`core/config/rationale/signal-mutation.md`). The auto-deep flips still fire
in Phase 4.1 whether or not this display runs — this phase is pure
observability so the LLM sees its own pattern-matching risk BEFORE selecting
the next goal. Mirrors the bash-emitted one-line pattern of Phase 0-pre.0.

No state mutation. Fail-open: if `wm-read.sh loop_state` returns `null`
(first iteration, fresh session, or counters not yet seeded), skip the
display entirely.

```
Bash: wm-read.sh loop_state
Parse:
    # The on-disk sub-slot is `signals`, NOT `session_signals` — the orchestrator
    # renames it on restore (aspirations/SKILL.md: `session_signals = loop_state.signals`)
    # and every Python writer uses `signals`. Reading the restored NAME here returns
    # absent → every counter defaults to 0 → the `global >= 4` warning below can never
    # fire, silently, on every iteration. Measured 2026-07-29 (zeta): a literal read
    # returned productive_streak=0 while the slot held 43.
    global     = loop_state.signals.routine_streak_global            (int, default 0)
    per_goal   = loop_state.routine_streaks                          (dict, default {})
    total_rt   = loop_state.signals.routine_count_total              (int, default 0)
    # NOT goals_completed_this_session — that key EXISTS but is an INT (a counter),
    # so len() on it raises TypeError. The actual list is counted_goals_this_session.
    # (goals_completed is an int too, so it is not the fix either.) Measured 2026-07-29.
    completed  = loop_state.counted_goals_this_session               (list, default [])

IF loop_state is null OR (global == 0 AND no per_goal value > 0):
    SKIP — clean streak state, no signal to surface

# Build one-line summary:
top3       = sorted per_goal entries by value desc, keep value > 0, take 3
per_goal_s = ", ".join(f"{gid}={n}" for gid,n in top3) or "none"
ratio_s    = f"{total_rt}/{len(completed)} routine" if completed else f"{total_rt} routine"

IF global >= 4:
    # Near-threshold warning. global is capped in {0..4}: Block C flips
    # outcome->deep AT routine_streak_global_ceiling=5 and resets to 0
    # (recurring-loop-state-mutate.py), so the stored value never exceeds 4
    # and global==4 means the NEXT routine close auto-deep-flips.
    # Bravo's complaint (2026-04-23): "When 7 routines stack up, I start
    # pattern-matching rather than reasoning." This surface makes the
    # counter visible BEFORE the flip, not silently after.
    Output: "▸ ⚠ BOREDOM: routine_streak_global={global} (auto-deep at 5) | per-goal: {per_goal_s} | session: {ratio_s} — pattern-matching risk, deepen reasoning on next selection"
ELSE:
    # Informational only — streak exists but below near-threshold.
    Output: "▸ Boredom: routine_streak_global={global} (auto-deep at 5) | per-goal: {per_goal_s} | session: {ratio_s}"
```

## Phase 0-pre.0c: Stash Carryover Probe (g-115-1133)

Cheap per-iteration observability surface in the same cluster as Phase 0-pre.0
(partner snapshot) and 0-pre.0b (boredom). Surfaces non-empty `git stash`
entries, which are invisible to `git status` and therefore an undetectable
cross-session/cross-agent working-tree carryover risk. Origin incident
(g-115-1127): an externally-created stash (92543f81 at 12:15) silently shelved
~10 min of alpha-LLM working-tree work before manual recovery via
`git checkout 92543f81 -- <files>`. Defense-in-depth observability, NOT a gate
— it never blocks, defers, or mutates state; it only warns.

No state mutation. Fail-open: any git error (no repo, detached state) is
swallowed — never blocks the loop. Quiet on the common empty case.

```
# Single local git read (sub-second; reads .git/refs/stash).
Bash: git stash list 2>/dev/null
IF output is non-empty:
    count  = number of stash entries (lines)
    first5 = first 5 entries
    Output: "▸ ⚠ STASH CARRYOVER: {count} non-empty git stash(es) — invisible to `git status`, possible cross-session/cross-agent working-tree carryover. First {min(5,count)}:"
    FOR EACH entry in first5: Output: "    {entry}"
    Output: "    Recover via `git stash show -p stash@{N}` then `git stash pop`, or `git stash drop` once confirmed obsolete (g-115-1127)."
# ELSE: clean — emit nothing (quiet on the common empty case).
```

## Phase 0-pre.0c.5: Branch-Stall / Detached-HEAD Probe (g-115-8281)

Same cluster and same contract as 0-pre.0c above: report-only, quiet on clean,
never a gate. Two git states `git status` calls healthy — (a) **detached HEAD**,
and (b) **branch stall**, the current branch ahead of origin with the OLDEST
unpushed commit past the age threshold. Both are already COMPUTED and then
DISCARDED by iteration-push.sh (`detached HEAD or no branch — skip`, soft_exit 0;
and AGE_MIN, spent only on the push throttle and a depth>=25 alarm), which is how
a detached HEAD ran 25h / 19 commits with the health composite reading 0.9659
(g-115-8145). Rationale, thresholds and the uncovered third shape: the script
header. Controls: `core/scripts/tests/test_branch_stall_probe.py`.

```
Bash: bash core/scripts/branch-stall-probe.sh
IF output is non-empty:
    Surface the ▸ line(s) verbatim and follow the recovery each one names —
    re-attach the branch, or read iteration-push's integrate/defer line.
# ELSE: clean — emit nothing (quiet on the common case).
# No state mutation. Fail-open: any git error is swallowed and the exit is
# ALWAYS 0, so it can never block the loop.
```

## Phase 0-pre.0d: Sentinel Battery (g-115-2303 — ONE call replaces the per-phase wm-reads)

The force-gate/pending sentinel gates below (Phases 0-pre..0-pre6) are
enumerated by ONE script call. The script owns the slot list
(`core/scripts/_sentinel_registry.py` — shared with `stale-sentinel-canary.py`,
single source of truth), so a REGISTERED phase can never silently fall out of the
protocol: post-autocompact, "run the battery" is the only line that must survive
summarization; the output re-derives the full battery. (Origin g-115-2302: a
reconstructed protocol carried 3 of 6 phases and a set sentinel sat unread for
3 iterations.) Adding a new sentinel gate = one registry entry + its consumer
phase section below.

REGISTERED is load-bearing, and the word was added the hard way (g-115-3678).
This paragraph used to promise that a phase "can never" fall out — an unqualified
guarantee the battery cannot give, because its N is its OWN registry, not the set
of consumer phases below. Phase 0-pre2.5 shipped a consumer with no registry
entry, so the battery truthfully reported "all 6 registered sentinels null — no
gates to dispatch" while a 7th sat set; that all-clear then authorised the SKIP
below, past the very gate it had not checked. Measured unseen: 2 stubs / ~9h
(zeta), then 15 MATERIAL self.md stubs / ~19h (bravo) — the latter ~5h from
expiring unnotified, breaking the guard-380 notify-after promise. A present
consumer phase and an absent registry entry each look correct in isolation, and
the count in the all-clear line is the only place the mismatch shows. So: when
adding a gate, the registry entry is not bookkeeping that follows the real work —
it IS what makes the gate exist. And when reading the all-clear, read the COUNT,
not just the word "all".

```
Bash: bash core/scripts/precheck-sentinel-battery.sh
IF output says "all N registered sentinels null — no gates to dispatch":
    SKIP directly past Phases 0-pre..0-pre6 (no per-phase wm-reads needed).
FOR EACH "▸ SENTINEL: <slot> (phase <phase>) payload=<json> → dispatch: <section>" line:
    Handle it via the NAMED phase section below. The payload is on the line —
    do NOT re-run wm-read for that slot. The phase bodies keep ownership of
    action + dispatch-stamp + clear (the battery is READ-ONLY).
IF output reports wrapper_failed or error=...:
    Fall back to the per-phase wm-read preambles below (each phase section
    still documents its slot). The battery must never block the loop.
```

## Phase 0-pre.0e: Always-Run Lane Battery (g-115-6466)

*Fallback only when Step 0-open did not run — `iteration-open.sh --apply`
dispatches this battery itself as its `always-run-battery` stage, so running it
here as well escalates the four `--apply` notification lanes TWICE.* (The two
were built concurrently on two boxes — g-115-6466 on cc-07, g-115-6468 on
cc-08 — and auto-merged without a conflict; this line is the seam.)

Sibling of the sentinel battery above, for the always-run tier: one call that
runs the five STANDALONE always-run lanes (0.5b.1b, 0.5b.1c, 0.5b.2, 0.5b.2b,
0.5g.7) under the meter and prints FINDINGS ONLY.
# Rationale (WHY findings-only, WHY status≠completeness, WHY 0.5g.7 still re-runs): core/config/rationale/always-run-battery.md

```
Bash: bash core/scripts/precheck-always-run-battery.sh --apply
# --apply is REQUIRED, not decorative: the battery defaults to dry-run, so a
# bare call turns all four notification lanes into no-ops and still prints a
# confident "all 5 lanes clean".

IF "all 5 lanes clean" → SKIP the per-lane bodies of 0.5b.1b / 0.5b.1c /
    0.5b.2 / 0.5b.2b; treat 0.5g.7's drain as satisfied-empty this iteration.

IF "NO FINDINGS REACHED — N of 5 lanes blind" → NOT clean. Do not read it as
    clean (guard-4093). Run the named blind lanes' per-phase bodies below.

FOR EACH "▸ FINDING: <lane> (phase <phase>) <detail>" → go to that NAMED phase
    section and do its work. The battery already RAN the lane with --apply, so
    do NOT re-run the four notification lanes; the finding line IS the dispatch.
    0.5g.7 is the exception — it re-runs its slate, because `slate=N` is a count
    and its obligation is a per-item disposition of the rows.

IF wrapper_failed or error=... → fall back to the per-lane invocations in the
    phase sections below. The battery must never block the loop.
```

## Phase 0-pre: Tree-Debt Critical Gate (g-115-81, source-dispatch g-115-721)

Runs BEFORE Phase 0 completion checks. Consumes the `force_tree_maintain` WM
signal set by:
  - iteration-close.sh learning-gate when tree debt exceeds
    `tree_debt_check.debt_threshold * 3` (default 60). Heavy: invoke
    /tree maintain --backlog.
  - tree-encoding-drift-gate.py dual-write (g-115-700) when encoding-drift
    threshold crossed (default 3). Lightweight: drift acknowledged via
    log-and-clear; no /tree maintain invocation. Without source-based
    dispatch, encoding-drift fires /tree maintain --backlog every 3 closes
    regardless of actual tree-debt (witnessed iter-19/21/23 of bravo session,
    backlog 145 stays static, 0-action passes accumulate). g-115-721 fix.

Prevents the LLM-abbreviation drift that grew backlog 85→151 over sessions
48-50 — the obligation is now bash-emitted and mandatory, not an advisory
INFO line that gets skipped under context pressure.

```
signal = Phase 0-pre.0d battery payload for force_tree_maintain
         (fallback only if battery errored: Bash: wm-read.sh force_tree_maintain --json)
IF signal is not null:
    source = signal.get("source", "tree-debt-critical")
    IF source == "encoding-drift":
        Output: "▸ TREE-ENCODING-DRIFT GATE: force_tree_maintain source=encoding-drift threshold={threshold} — drift acknowledged, no /tree maintain invocation (lightweight path, g-115-721)"
        # No /tree maintain. The encoding work was supposed to fire via
        # aspirations-state-update Step 8 force_tree_encoding consumer but
        # the routing path bypassed it. The dual-write (g-115-700) was meant
        # as a backstop but /tree maintain --backlog is the wrong action for
        # encoding-drift (it does decompose/distill backlog drain, not
        # missing-encoding catch-up). Acknowledging the drift via clear-only
        # path lets the agent continue without 0-action heavy maintenance.
        # When tree-debt actually becomes critical (debt > threshold * 3 = 60),
        # iteration-close.sh learning-gate writes the signal with
        # source=tree-debt-critical (or missing), routing to the heavy path.
    ELSE:
        # source=tree-debt-critical or missing (legacy/learning-gate path)
        Output: "▸ TREE-DEBT GATE: force_tree_maintain set (source={source}, count > threshold)"
        invoke /tree maintain --backlog
    # Stamp consumer-dispatch timestamp BEFORE clearing — consumption-aware canary
    # requires this; stamp FIRST so interrupts leave the safe direction.
    # Rationale (WHY consumption-aware stamp): core/config/rationale/precheck-gates.md
    printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | Bash: wm-set.sh force_tree_maintain_last_dispatch
    # Clear signal after handling (one-shot; next iteration's drift gate or
    # learning-gate re-sets if still indicated).
    #
    # Routed through verified-wm-set.sh (write -> read-back -> assert -> retry
    # once, loud on failure) rather than a bare wm-set (g-115-3698). The clear
    # was fire-and-forget: a transient wm-set failure left the sentinel SET and
    # the redirect discarded the error, so the gate re-armed and the ritual
    # re-fired against already-handled state. Observed on pending_phase_6_spark,
    # where `set_at` was unchanged — proving no producer had re-armed it and the
    # clear had simply not landed. Re-running the identical command unsilenced
    # returned rc=0. rc is not sufficient on its own here (the same
    # rc=0-that-did-not-land shape is recorded for other stores), which is why
    # the wrapper owns the READ-BACK. The same rationale applies to every clear
    # in this file; it is stated once, here, rather than six times.
    echo 'null' | Bash: verified-wm-set.sh force_tree_maintain
    # Continue to Phase 0 (compact data may now be stale — Phase 0.5 re-reads it).
```

## Phase 0-pre2: Experience Archival Gate (rb-428)

Runs AFTER tree-debt gate, BEFORE Phase 0 completion checks. Consumes the
`force_experience_archival` WM signal set by `experience-staleness-check.sh`
when `experience.jsonl` is stale beyond the configured threshold (default 12h,
tunable via `EXPERIENCE_STALENESS_HOURS` or `experience_archival_gate.staleness_hours`
in `core/config/aspirations.yaml`). Forces the LLM to compose and submit the
missed experience record before goal selection proceeds. Prevents the 30–76h
drift that g-248-07 surfaced — the bash canary (g-248-16) detects it, this
gate forces action.

Pattern mirrors the tree-debt gate above verbatim — wm-read → if non-null →
action → wm-set 'null'. One-shot retry: if composition fails, the sentinel
persists and the gate fires again next iteration until the LLM succeeds.

```
signal = Phase 0-pre.0d battery payload for force_experience_archival
         (fallback only if battery errored: Bash: wm-read.sh force_experience_archival)
IF signal is not null:
    Output: "▸ EXPERIENCE-ARCHIVAL GATE: force_experience_archival set (last entry {last_entry_id}, {age_hours}h stale)"
    # Compose the missed experience record per Phase 4.25 instructions
    # (execute-protocol-digest.md §4.25). Use the most-recent deep goal's
    # context reconstructible from:
    #   - working memory: wm-read.sh goals_completed_this_session
    #   - prior iteration's journal entry: agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md
    #   - retrieval-session.json (tree_nodes_loaded)
    # If no deep goal is reconstructible (e.g., routine-only streak), write a
    # minimal placeholder record with a short `note` explaining the gap, AND
    # clear the sentinel — do not loop.
    # FORCED-FLIP GUARD (g-xw-20260717T220413-01): a "deep" close whose
    # last_outcome_origin is forced (Block C anti-drift streak flip) carries
    # ROUTINE content — its deep label is streak mechanics, not substance.
    # Treat forced-flip closes like routine for THIS gate: if every deep close
    # since the last experience entry was a forced flip, do NOT compose a full
    # experience record from its routine content (that manufactures a
    # contentless filler entry). Take the placeholder path instead (note:
    # "window contained only forced-flip deep closes — no genuine deep content
    # to archive") and clear the sentinel. Genuineness check: the goal record's
    # last_outcome_origin field ("genuine" vs "forced"), or the close output's
    # outcome_in=routine/outcome_out=deep line.
    <compose experience-add.sh JSON payload>
    # WRITE + CLEAR AS ONE GUARDED STEP (g-115-4177). Do NOT hand-compose
    # `experience-add.sh ... ; wm-set.sh <slot>` — that shape is the defect, and
    # `&&` does not fix it: experience-add.sh can exit **rc=0 while REFUSING the
    # payload** (see aspirations-spark's verbatim_anchors note — a bare-string
    # anchor list is refused, and guardrails-add.sh was measured doing the same
    # over an invalid `applies_to`). Exit status is therefore not proof the
    # record landed, so the read-back is mandatory and the primitive owns it.
    # The primitive runs the write, re-reads it, and clears ONLY if BOTH pass;
    # on any failure it leaves the sentinel SET with a diagnostic naming which
    # check failed, so the obligation re-surfaces next iteration (fail-CLOSED).
    # This also satisfies guard-1870 mechanically — the clear is issued as its
    # own invocation, never sharing a Bash call with the write it gates.
    # Measured six times before this existed; the last three within ~30h by an
    # agent that had documented the failure each time, which is why the
    # corrective is wiring rather than another encoded rule (rb-745, guard-232).
    # `.raw`, NOT `.json` — the extension is the purge marker (temp-store.md:214).
    # This payload is spent script I/O: the knowledge lands in the experience
    # store via the consuming script, and the leftover file carries nothing.
    # A bare `.json` here is EXEMPT from /drain-temp Phase 1.5's purge AND is
    # enumerated by Phase 1 as a drainable working doc, so it is the one suffix
    # that accumulates permanently — guard-1101 / guard-1034 / guard-3260 all
    # prohibit exactly this write. THIS LINE PRESCRIBED IT ANYWAY, which is why
    # the population kept growing while three guardrails said not to: a
    # guardrail cannot outvote the instrument it guards (guard-1984). Measured
    # 2026-08-12 (zeta, cc-02, 6.8.0-137-generic): 2916 of 3095 non-drained
    # files in one agent's temp/ root were scratch `.json`, ~1 month deep, and
    # this gate contributes one on every firing, for every agent.
    Bash: printf '%s' '<payload-json>' > agents/<agent>/temp/exp-payload.raw
    Bash: bash core/scripts/sentinel-clear-guarded.sh \
            --slot force_experience_archival \
            --verify "bash core/scripts/experience-read.sh --goal <goal-id>" \
            --expect "<experience_id>" \
            -- bash -c 'bash core/scripts/experience-add.sh < agents/<agent>/temp/exp-payload.raw'
    # rc=0 cleared · rc=1 the write failed · rc=2 the write did not land (read-back
    # empty or id mismatch) · rc=3 usage/clear error. On 1 or 2 do NOT narrate
    # "experience archived" — read the diagnostic, fix the payload, and let the
    # sentinel re-fire.
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — it blocks goal
selection via the precheck pattern (LLM sees sentinel, acts, clears it) rather
than terminating the loop.

## Phase 0-pre2.5: Evolution-Stub Finalize Gate (g-115-2180, rb-428 family)

Runs AFTER the experience-archival gate, BEFORE the fresh-eyes-code gate.
Consumes the `force_evolution_finalize` WM sentinel set by
`evolution-stub-pending-check.sh` (wired into `iteration-close.sh`'s
productivity-check maintenance tick, beside `evolution-stub-expiry.py`).

**Why this gate exists.** `evolution-record.py` writes an `awaiting_completion`
stub on every `self.md` / `program.md` edit. The LLM is then supposed to call
`evolution-complete.sh`, which records the WHY and — for a MATERIAL `agent_self`
change — fires the **guard-380 user notification**. *Nothing ever prompted that
call.* 24h later `evolution-stub-expiry.py` honestly transitions the stub to
`expired` (it refuses to fabricate a rationale, which is correct — see its
docstring). Measured 2026-07-14: of 65 material Self edits fleet-wide only
**11 (17%)** ever reached the user; **22 EXPIRED unnotified** — the agent's
identity changed and the user was never told. On 2026-04-22 the user explicitly
traded "ask first" for "notify after, revert if wrong"; the notify-after half was
silently not executing, which made the autonomy unearned. This gate is the
missing PROMPT; the expiry sweep remains the honest fallback.

Every sibling obligation in the rb-428 family (tree-debt 0-pre, experience-archival
0-pre2, fresh-eyes-code 0-pre3, metric-encoding 0-pre4) already had a forcing
consumer. Self-evolution finalization was the one that did not.

Pattern mirrors Phase 0-pre2 verbatim — wm-read → if non-null → action → clear.
One-shot with retry: the producer re-fires the sentinel every iteration until the
stub is finalized, so a failed completion is never lost.

```
signal = Phase 0-pre.0d battery payload for force_evolution_finalize
         (fallback only if battery errored: Bash: wm-read.sh force_evolution_finalize --json)
IF signal is not null AND signal.count > 0:
    Output: "▸ EVOLUTION-FINALIZE GATE: {signal.count} awaiting_completion stub(s) ({signal.material_count} MATERIAL) past {signal.threshold_minutes}min — finalizing before goal selection"
    FOR EACH stub in signal.stubs:
        # Reconstruct the WHY from the session's own record — the journal entry,
        # the goal that motivated the edit, the .history snapshot named in the
        # stub, or the diff_excerpt carried on the stub itself.
        #
        # NEVER FABRICATE. If the rationale genuinely cannot be reconstructed
        # (e.g. the edit was made by a prior session whose context is gone), do
        # NOT invent one — leave the stub and let evolution-stub-expiry.py record
        # `expired` honestly at 24h. A fabricated reasoning string is worse than
        # an absent one (verify-before-assuming.md; and it is exactly what the
        # expiry script's docstring refuses to do). Log the skip and move on.
        Bash: bash core/scripts/evolution-complete.sh \
                --revision-id {stub.revision_id} \
                --reasoning "<>=80-char rationale, reconstructed from real evidence>" \
                --signal-source {sq-012 | fresh-eyes-review | user-directive | ...} \
                --signal-evidence '[{"type":"goal","id":"<goal-id>"}, ...]'
        # For change_class=material + file_kind=agent_self, evolution-complete's
        # Phase 5 AUTO-posts the decisions board AND AUTO-emails the user
        # (guard-380). No manual notification call is needed here.
    # Stamp consumer-dispatch timestamp BEFORE clearing — this sentinel is
    # CONSUMPTION-AWARE in the canary (g-115-3678). Its producer re-arms every
    # iteration while any stub is pending, so a bare presence-count would fire
    # on the legitimate never-fabricate path above (a stub left for the 24h
    # expiry). The canary's discriminator is dispatch ADVANCEMENT, so stamp on
    # ANY handling — finalized, OR a justified skip because the rationale was
    # genuinely unreconstructable. Stamp FIRST so an interrupt leaves the safe
    # direction. (Mirrors Phase 0-pre / 0-pre3 / 0-pre4.)
    printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | Bash: wm-set.sh force_evolution_finalize_last_dispatch
    # Clear after the pass. If a completion failed, the producer re-sets the
    # sentinel next iteration — do not hand-retry in a loop here.
    Bash: `echo 'null' | verified-wm-set.sh force_evolution_finalize`
    # Continue to Phase 0-pre3.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as its siblings. Reversibility: raise `--threshold-minutes` in `iteration-close.sh`
to an unreachable value (or remove the one-line call) to stop sentinel writes
without touching this consumer.

**Scope note.** The producer covers the `self` + `program` streams ONLY — the two
carrying the guard-380 promise. It deliberately does NOT cover script/skill/rule:
`script-evolution.jsonl` measured 152 pending / 1992 expired vs 23 final (a **99%
expiry rate**) on 2026-07-14, so widening the gate there would fire every iteration
forever and train the agent to ignore the sentinel. That backlog is a separate
finding with its own goal.

## Phase 0-pre3: Fresh-Eyes-Code Dispatch Gate (g-115-281)

Runs AFTER experience-archival gate, BEFORE Phase 0 completion checks. Consumes
the `fresh_eyes_dispatch_pending` WM signal set by `iteration-close.sh
do_state_update` when `post-state-update-gate.sh` fires (default thresholds:
≥3 core files changed OR ≥150 LOC OR new script under `core/scripts/`).
Forces /fresh-eyes-code dispatch before goal selection so the deferred review
backlog cannot accumulate across iterations the way it did before this gate
existed (33 core files / 605 LOC silently buffered when no consumer was wired).

Pattern mirrors Phase 0-pre and Phase 0-pre2 — wm-read → if non-null → action →
stamp `fresh_eyes_last_dispatch` → **emit the dispatch-observation firing** →
wm-set 'null'. It is no longer verbatim: this phase alone carries the extra
telemetry step (g-115-5323), because this sentinel is the only one whose payload
can MERGE, and `merged_payloads` had no durable store to reach. The signal payload is
the full gate JSON
(`{"fired":true,"core_count":N,"loc_changed":N,"reason":"...","files":[...],"set_at":"..."}`),
so the dispatcher has the file list + reason without re-running the gate.
Rationale (WHY fresh_eyes_last_dispatch stamp): `core/config/rationale/precheck-gates.md`

```
signal = Phase 0-pre.0d battery payload for fresh_eyes_dispatch_pending
         (battery omits fired!=true payloads — a printed line IS actionable;
          fallback only if battery errored: Bash: wm-read.sh fresh_eyes_dispatch_pending --json)
IF signal is not null AND signal.fired == true:
    Output: "▸ FRESH-EYES-CODE GATE: fresh_eyes_dispatch_pending set ({signal.core_count} core files, {signal.loc_changed} LOC, reason={signal.reason}) — invoking /fresh-eyes-code before goal selection"
    invoke /fresh-eyes-code with files = signal.files
    # Stamp consumer-dispatch timestamp BEFORE clearing — canary requires advancing
    # timestamp to detect bypass vs. keeping-up. Stamp FIRST for interrupt safety.
    # Rationale (WHY fresh_eyes_last_dispatch stamp): core/config/rationale/precheck-gates.md
    printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | Bash: wm-set.sh fresh_eyes_last_dispatch
    # (g-115-5323) Durable, cross-agent record of THIS dispatch. Until this line
    # existed, merged_payloads reached NO durable store: it lived only in the WM
    # slot cleared on the next line, and in an iteration-close stderr banner that
    # is invisible whenever that script runs backgrounded (guard-772) — which it
    # routinely does. That is why g-115-4254 accumulated ZERO observations in the
    # 8 days after it was filed while dispatches were demonstrably happening.
    # merged_payloads absent or 1 = unmerged; >=2 = merged (fresh-eyes-sentinel-merge.py).
    # Pass null when the key is absent — absence IS the unmerged reading, and the
    # sample needs it as the baseline, so do not omit the row for lack of a value.
    # EMITTED BEFORE THE CLEAR on purpose: an interrupt after this line costs one
    # duplicate row, which is self-describing and harmless; emitting after the
    # clear would lose the observation outright, which is the defect being fixed.
    Bash: bash core/scripts/gate-log.sh fresh-eyes-dispatch-observation pass \
        --caller "aspirations-precheck/SKILL.md:Phase 0-pre3" \
        --trigger "{signal.reason}" \
        --extra-json '{"merged_payloads": {signal.merged_payloads or null}, "core_count": {signal.core_count}, "loc_changed": {signal.loc_changed}}'
    # Clear signal after dispatch.
    echo 'null' | Bash: verified-wm-set.sh fresh_eyes_dispatch_pending
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as Phase 0-pre and Phase 0-pre2. Reversibility: post-state-update-gate.sh has
its own cooldown logic (compares current file set against last-fire snapshot in
WM); flipping its threshold envs (`CORE_FILE_THRESHOLD`, `LOC_THRESHOLD`) to
unreachable values stops sentinel writes without touching this consumer.

## Phase 0-pre4: Metric-Encoding Dispatch Gate (g-115-724, rb-917)

Runs AFTER fresh-eyes-code dispatch gate, BEFORE Phase 0 completion checks.
Consumes the `force_metric_encoding_pending` WM signal set by
`iteration-close.sh do_state_update` when `post-state-update-metric-gate.sh`
fires on deep-outcome closures. Content-gate sibling to the rb-428 counter-gate
family (tree-debt, experience-archival, fresh-eyes-code, tree-encoding-drift)
— catches "LLM did the encoding step on the wrong content" rather than
"LLM skipped the encoding step entirely."

Rationale (WHY metric-encoding gate): `core/config/rationale/precheck-gates.md` (canonical incident g-115-707)

Pattern mirrors Phase 0-pre/0-pre2/0-pre3 verbatim — wm-read → if non-null →
action → wm-set 'null'. The signal payload includes `candidates`,
`candidate_node_key`, `candidate_node_file`, `distinct_count`, `reason`,
so the LLM has the extracted findings + recommended target node without
re-running the gate or re-scanning prose.

```
signal = Phase 0-pre.0d battery payload for force_metric_encoding_pending
         (battery omits fired!=true payloads;
          fallback only if battery errored: Bash: wm-read.sh force_metric_encoding_pending --json)
IF signal is not null AND signal.fired == true:
    Output: "▸ METRIC-ENCODING GATE: force_metric_encoding_pending set ({signal.distinct_count} distinct findings, target node={signal.candidate_node_key}, reason={signal.reason}) — encoding into tree before goal selection"
    # LLM action: encode the extracted findings as Verified Values into the
    # recommended tree node (candidate_node_file). Use the candidates list
    # (each entry "value :: context") to assemble the Verified Values block.
    # If the recommended node feels wrong for the findings (e.g., category
    # match was weak), pick a better-fit node — the gate's suggestion is
    # advisory, not authoritative. tree-edit-since.py probes mtimes, so any
    # in-iteration tree edit clears the gate's preconditions for next time.
    Edit {signal.candidate_node_file} OR /tree edit {better-fit-key}:
        Add a Verified Values entry naming each candidate
        Include the source goal-id + completion timestamp
        Cross-reference rb-917 / g-115-707 for the pattern lineage
    # Stamp consumer-dispatch timestamp BEFORE clearing — the stale-sentinel-canary
    # is consumption-aware for this sentinel (g-115-1746), keyed on
    # force_metric_encoding_last_dispatch. Advancing it on ANY handling (encode-and-
    # clear here, OR a guard-655 justified no-dispatch clear when the candidate node
    # is partner-attributed/phantom) tells the canary the consumer kept up, so a
    # keeping-up consumer no longer false-fires the "stale sentinel set for N
    # iterations" Investigate. Stamp FIRST for interrupt safety (mirrors Phase
    # 0-pre / Phase 0-pre3).
    printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | Bash: wm-set.sh force_metric_encoding_last_dispatch
    # Clear signal after encoding (one-shot; next iteration's iteration-close
    # re-fires the gate if state-update produces new substantive metrics).
    echo 'null' | Bash: verified-wm-set.sh force_metric_encoding_pending
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as Phase 0-pre/0-pre2/0-pre3. Reversibility: edit `post-state-update-metric-
gate.sh`'s `DISTINCT_COUNT_THRESHOLD` to a high value (e.g. 999) to stop
sentinel writes without touching this consumer.

## Phase 0-pre5: Pipeline-Reconcile Gate (rb-428 family)

Runs AFTER the metric-encoding gate, BEFORE Phase 0 completion checks. Consumes
the `pipeline_reconcile_pending` WM sentinel set by `iteration-close.sh
do_state_update` (Step 8.79b domain-overlay seam) when the just-closed goal was
**pipeline-affecting** — a goal that touched the domain's external lead/outreach/
contact pipeline. Forces a reconcile before goal selection so external pipeline
state (e.g. a remote CRM or external pipeline store) is never left stale after a
relevant goal closes — the freshness guarantee the hook exists to provide.

Domain-agnostic by construction: the sentinel JSON NAMES the skill to invoke
(`signal.skill`, set by the domain gate at
`$WORLD_DIR/scripts/pipeline-reconcile-gate.sh`). This phase invokes whatever
skill the domain named — core hardcodes no domain skill. A fresh world that
never sets the sentinel makes this a one-`wm-read` no-op.

Pattern mirrors Phase 0-pre/0-pre2/0-pre3/0-pre4 verbatim — wm-read -> if non-null
-> act -> wm-set 'null'. One-shot with retry: leave the sentinel set if the
reconcile fails so the next iteration retries.

```
signal = Phase 0-pre.0d battery payload for pipeline_reconcile_pending
         (battery omits fired!=true payloads;
          fallback only if battery errored: Bash: wm-read.sh pipeline_reconcile_pending --json)
IF signal is not null AND signal.fired == true:
    skill = signal.skill            # domain-named consumer; core stays agnostic
    goals = signal.goals            # the just-closed pipeline-affecting goal id(s)
    Output: "▸ PIPELINE-RECONCILE GATE: {goals} was pipeline-affecting ({signal.reason}) — invoking {skill} reconcile before goal selection"
    invoke {skill} with args "reconcile --goals {comma-joined goals}"
    # Clear ONLY after the reconcile ran (one-shot; on failure leave the
    # sentinel so the next iteration retries — same fail-open contract as the
    # sibling gates).
    echo 'null' | Bash: verified-wm-set.sh pipeline_reconcile_pending
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as the sibling sentinel gates. Reversibility: the domain gate's
`PIPELINE_HOOK_ENABLED=0` stops sentinel writes upstream; removing or renaming
`$WORLD_DIR/scripts/pipeline-reconcile-gate.sh` makes the core seam a no-op.
Either disables the feature without touching this consumer.

## Phase 0-pre6: Pre-Apply-Consult Drift Gate (g-115-2201)

Runs AFTER the pipeline-reconcile gate, BEFORE Phase 0 completion checks.
Consumes the `force_pre_apply_consult` WM sentinel set by
`iteration-close.sh do_learning_gate` (via `pre-apply-consult-drift-gate.py`)
when N consecutive framework-touching **deep** closes logged
`retrieval-summary: performed=false` (default N=2, `--threshold`).
`.claude/rules/code-review-protocol.md` step 4 — the pre-apply consultation
(`retrieve.sh` before editing any framework file) — is honor-system and drifted
to a 100% miss rate on framework deep goals (g-115-2194 / g-115-2195 / g-115-2179,
2026-07-14; measured cost ~1h re-deriving a guardrail one `retrieve.sh` would
have surfaced). An advisory cannot fix a 3/3 miss — advisory is what step 4
already is — so this ENFORCES it. Complementary to the per-goal advisory
`pre-apply-consult-gate.py` (g-115-826), which fires BEFORE the edit on any goal
naming a framework file — own-authored included since g-115-2201, which closed the
authorship gap those three misses fell through. This one keys on
`work_class == framework` at CLOSE time, so it still covers framework goals whose
prose never names a file at all.

Pattern mirrors the sibling gates verbatim — wm-read → if non-null → action →
wm-set 'null'. Dormant unless the sentinel is set (only on actual drift), so it
is NOT a tax on every close. Does NOT hard-block the Edit tool (a fail-closed
per-edit gate can wedge the loop, spec WORK item 3) — same non-wedging precheck
pattern as 0-pre..0-pre5.

```
signal = Phase 0-pre.0d battery payload for force_pre_apply_consult
         (fallback only if battery errored: Bash: wm-read.sh force_pre_apply_consult --json)
IF signal is not null:
    Output: "▸ PRE-APPLY-CONSULT DRIFT GATE: force_pre_apply_consult set ({signal.streak} consecutive framework-deep closes with retrieval performed=false, last={signal.goal_id}) — code-review-protocol step 4 skipped {signal.streak}× on framework work."
    # ACT before goal selection — satisfy ONE branch:
    #  (a) DEFAULT: run the pre-apply consult NOW to re-ground in the discipline
    #      and surface guardrails / reasoning-bank entries that constrain
    #      framework edits (this IS the step the drift skipped):
    #        Bash: retrieve.sh --category "code-review-protocol pre-apply consultation framework edit guardrails" --depth shallow
    #      Read the returned reasoning_bank + guardrails. THEN, when THIS
    #      iteration's selected goal touches a framework file (core/, .claude/,
    #      core/config/, world/conventions/), run retrieve.sh again scoped to
    #      that specific fix BEFORE the first Edit (code-review-protocol step 4).
    #  (b) If this iteration's work provably touches NO framework file, log one
    #      line in the journal instead: "PRE-APPLY-CONSULT GATE: not applicable —
    #      no framework edit this iteration" (the ACCEPTANCE "explicitly logging
    #      why it is not applicable" branch).
    # Clear the sentinel after acting (one-shot; iteration-close re-sets it on
    # continued drift — a single framework-deep close that DID consult resets
    # the streak upstream so it never re-fires).
    echo 'null' | Bash: verified-wm-set.sh force_pre_apply_consult
    # Continue to Phase 0.
```

The gate does NOT write `stop-requested` or `stop-loop` — same precheck pattern
as the sibling sentinel gates. Reversibility: raise the drift threshold at the
`pre-apply-consult-drift-gate.py` call site in `iteration-close.sh`
do_learning_gate (`--threshold`, default 2) to an unreachable value to stop
sentinel writes without touching this consumer.

## Phase 0: Automated Completion Checks

Run completion check runners to auto-detect completed goals.

### File Existence Checks
For each goal with `verification.checks` containing `type: "file_check"`:
- If `goal.recurring`: skip
- If file exists at path: mark goal completed, log

### Pipeline Count Checks
For each goal referencing pipeline counts:
- If `goal.recurring`: skip
- `Bash: pipeline-read.sh --counts` — if threshold met: mark completed

### Config State Checks
For each goal referencing config fields:
- If `goal.recurring`: skip
- Read config file, check field value — if matches: mark completed

### Readiness Gate Checks
Check each readiness gate from `aspirations-read.sh --meta`.
`Bash: aspirations-meta-update.sh --source world readiness_gates '<JSON>'`

### Recurring Goal Safety Net
```
Bash: aspirations-recover-recurring.sh --source world
Bash: aspirations-recover-recurring.sh --source agent
```
Bash-enforced (g-001-160, rb-295): the script recovers both (a) `recurring=true
AND status=completed` and (b) shape-recurring corrupted goals (`recurring=false
AND status=completed AND interval_hours AND lastAchievedAt`). Idempotent — safe
to call every iteration. Prints JSON `{recovered: N, goals: [...]}`.

Previously an LLM-executed pseudocode step with archive-sweep as the only
bash backstop; drift across iterations let corrupted goals persist for days
(g-226-22 went 6 days before iter-51 of bravo session 51 caught it manually).
The bash command is now the primary path; the archive-sweep recovery branch
remains as a full-cycle fallback.

### Monitor Stale Check (g-240-37)
```
Bash: monitor-stale-check.sh --apply
```
Bash-enforced: for each pending/in-progress Monitor goal whose title contains
`proc-NNNNNNNNNN`, compares the embedded proc-ID against the current run_dir
reported by `processor-run.sh check-complete`. If the goal's ID is older AND
its age exceeds 48h, the script auto-completes it with `outcome_note:
superseded-by-newer-run`. Fails open: if processor-run.sh returns no run_dir
(fresh install, no completed runs yet), the sweep skips entirely. Prevents
Monitor goals from lingering as permanent pipeline debris after their target
runs complete.

### Hypothesis Expiration Checks
```
For each goal with hypothesis_id AND status pending/in-progress:
    if now > goal.resolves_by:
        mark status = "expired"
        move pipeline file to archived/
```

## Phase 0.5: Aspiration Health Check

```
Bash: load-aspirations-compact.sh → IF path returned: Read it
active_count = count of aspirations with status "active"
if active_count < 2:
    invoke /create-aspiration from-self --plan
    log "Aspiration health: below minimum, created new aspirations"

# Consolidation health snapshot — writes the consolidation_health WM slot
# consumed by /create-aspiration Step 1 (consolidation-gate), /aspirations-
# select Phase 2.55 (near-complete/stalled bias), and /aspirations-evolve
# Step E (gap-analysis refusal when portfolio fragmented). Before this
# write, all three readers fail-open on missing slot — gates were silently
# no-op. Fix: 2026-04-22 signal-lifecycle-gate finding.
# Single-writer rule: this is the ONLY writer for consolidation_health.
# Fail-open: wrapper is pure computation + wm-set; errors log to stderr
# and do not block the loop.
Bash: bash core/scripts/consolidation-health.sh --write >/dev/null || echo "[precheck] WARN: consolidation-health snapshot failed (non-fatal; gates will fail-open this iteration)"
```

## Phase 0.5.0-pre2: Self-Drift Gate (Tranche C.5 — rb-390)

Escalation path for class_balance drift. When work_class distribution stays
below a critical fraction of its configured target, file an Unblock goal
forcing the agent to correct the mix or retune the target. Config:
`aspirations.yaml → self_drift_gate`. Natural-gated (SSOT, rb-395): inactive
until BOTH `class_balance.targets` is non-empty AND
`self_drift_gate.target_aspiration_id` is set. Fail-open: no targets, no
target aspiration, or insufficient iterations all exit 0 with no side
effects. Cooldown-gated so the same drift does not spawn goal after goal.

```
Bash: bash core/scripts/self-drift-gate.sh 2>/dev/null || true
# Reads wm.goals_completed_this_session + aspirations.yaml config.
# When fires: writes one Unblock goal per drifted work_class via
#   aspirations-add-goal.sh --source agent, posts a coordination board
#   status, logs to agents/<agent>/session/self-drift-log.jsonl for cooldown.
# Cheap — just fraction arithmetic. No external calls.
# MUST invoke the .sh wrapper — `bash self-drift-gate.py` parses the
# Python docstring as shell and silently no-ops under `|| true`.
```

## Phase 0.5.0-pre: Signal-Refresh Hook (Tranche C — rb-390)

Hook slot for domain-supplied signal refresh before goal selection. Consumed by
goal-selector.py criterion 7d (`user_signal_boost`) — when a fresh snapshot is
present the scorer picks up inbox replies, pending-question silence, directives;
when absent the scorer contributes zero. Fail-open at every layer — a missing
or broken hook does NOT block iteration entry.

Pattern B hook slot (`signal-refresh`). See
`core/config/conventions/domain-hooks.md`. Core names the slot, the world
convention (if it exists) names what to run.

```
Bash: load-conventions.sh signal-refresh → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/signal-refresh.md" && echo "exists"
IF exists:
    Follow each Step in the convention.
    Any step that fails SHOULD be logged and swallowed — never abort precheck.
ELSE:
    # No domain signal-refresh convention exists (fresh agent). Nothing to do.
    # goal-selector's user_signal_boost dimension will see no snapshot and
    # contribute zero — correct fail-open behavior.
```

## Phase 0.5.0: Scripted Precheck

A single Python invocation performs the entire precheck sweep (zombies,
pipeline depth, hypothesis health, accuracy, consolidation, unproductive
cycles, user-goal reclassification). Read the JSON summary, act on each
flag, then SKIP directly to Phase 0.5a.

This is the ONLY path. The previous toggle + LLM-fallback pseudocode was
removed 2026-04-20 — the script is the single source of truth. Any bug in
`precheck-eval.py` MUST be fixed in the script, not patched around by
reintroducing a shadow LLM path here.

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5.0-scripted
Bash: bash core/scripts/precheck-eval.sh run-all
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5.0-scripted
# Parse the JSON. `flags[]` entries are prefixed with their subcommand
# (e.g. `zombies:needs_complete_review`) because run-all merges sub-reports.
# Action data lives in `results.<subcommand>.*` — the flag signals which
# action block to enter; the payload lives with the subcommand's result.
#
#   zombies:needs_complete_review      → for each entry in results.zombies.zombies[]:
#                                        route by entry.kind (rb-4906 — the asp-352 mis-route):
#                                        · all_terminal (every non-recurring goal already
#                                          terminal; unfinished EMPTY) → invoke
#                                          /aspirations-complete-review (NOT "Phase 7.4"). The
#                                          skill self-discriminates: no-recurring falls through
#                                          7.4's empty-unfinished fast-path to the Phase 7
#                                          fully-complete gate → aspirations-complete.sh
#                                          (all-terminal; a supply_evidence.needle record is
#                                          REFUSED without --needle-satisfied proof, g-357-86);
#                                          has_recurring → Phase 7's functionally-complete
#                                          stamp. Do NOT attempt complete-intent.sh — its
#                                          evidence gate is unsatisfiable with no open goals
#                                          (asp-352).
#                                        · blocked_stale (high completion, only blocked-and-
#                                          stale trailing goals, AND a usable `motivation`)
#                                          → invoke /aspirations-complete-review Phase 7.4
#                                          (intent-satisfaction), reason="zombie-scan" —
#                                          trailing blocked goals need intent-supersession.
#   zombies:needs_retire_or_normal_close → for each entry in results.zombies.zombies[] with
#                                        kind == blocked_stale_no_motivation (g-115-4164):
#                                        the blocked_stale profile on an aspiration whose
#                                        `motivation` yields no usable tokens. Do NOT send it
#                                        to Phase 7.4 — complete-intent.sh validates the
#                                        rationale by quoting that motivation, so with none
#                                        the flag can never be discharged and re-fires every
#                                        pass (measured: 3+ consecutive passes, ZDS asp-008).
#                                        Do NOT backfill a motivation to satisfy the gate that
#                                        validates against it, and do NOT stop detecting these.
#                                        Two paths actually discharge it — choose by judgment
#                                        against the aspiration's TITLE and goal history, since
#                                        there is no motivation text to reason from:
#                                        · intent abandoned / superseded → aspirations-retire.sh
#                                        · intent satisfied, only trailing blocked goals remain
#                                          → close those goals with a reason (skipped), which
#                                          makes the aspiration all-terminal, then
#                                          aspirations-complete.sh (no evidence gate). Next
#                                          pass re-detects it as all_terminal if you stop early.
#   pipeline-depth:thin_pipeline       → invoke /create-aspiration from-self
#   hypothesis-health:stalled_pipeline → /review-hypotheses --resolve
#   accuracy:accuracy_low              → file Investigate goal targeting
#                                        results.accuracy.worst_strategies
#   consolidation:shallow_portfolio    → consolidation pressure — state-update
#                                        scoring handles it (no direct action)
#   consolidation:stalled_aspirations  → surface results.consolidation.stalled[]
#                                        in output for user visibility
#   cycles:cycles_detected             → for each entry in results.cycles.cycles[]:
#                                        file Investigate goal, using entry.reason
#                                        ("repeated_failure" or "zero_learning_velocity")
#                                        to shape the goal description
#   user-goals:reclassifiable_user_goals → for each entry in
#                                        results.user-goals.candidates[]: invoke
#                                        aspirations-update-goal.sh participants '["agent"]'
#   temp-pressure:temp_drain_needed    → file results.temp-pressure.suggested_goal via
#                                        aspirations-add-goal.sh into asp-001 --source
#                                        agent (BOTH queues have an asp-001, ~L2663;
#                                        world's is often RETIRED, g-115-7347). Pass ALL
#                                        fields incl. intended_agent verbatim: it names
#                                        the temp OWNER, and /drain-temp is bound-agent-
#                                        scoped, so dropping either re-routes the goal or
#                                        no-ops the drain (g-115-2979, rb-3876). Dedup:
#                                        suppressed while an open drain goal exists (emits
#                                        temp_drain_pending); +temp_pressure_warn: info.
#   temp-pressure:temp_drain_stalled   → the open drain goal (escalation.goal_id) outlived
#                                        drain_goal_max_age_hours at pressure >= threshold
#                                        (g-115-3774). ACTION: /drain-temp THIS
#                                        iteration — no second goal, no scorer wait.
```

## Phase 0.5a: Pre-Selection Guardrail Check

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5a-guardrails
Bash: matched=$(bash core/scripts/guardrail-check.sh --context any --phase pre-selection --type both 2>/dev/null)
IF matched.matched_count > 0:
    FOR EACH guardrail in matched.matched:
        Bash: <run {guardrail.action_hint}>
        IF output reveals issues:
            → invoke CREATE_BLOCKER(affected_skill, issue_description, ...)
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5a-guardrails
```

## Phase 0.5b: Blocker Resolution Check

```
Bash: bash core/scripts/execution-diary.sh phase-start phase-0.5b-blockers
Bash: wm-read.sh known_blockers --json
IF known_blockers is non-empty:
    FOR EACH blocker WHERE resolution is null:
        # PRIMARY: Did unblocking goal complete?
        IF blocker.unblocking_goal completed: resolve

        # Pre-probe retrieval (G5 / R7): load prior probe-attempt RB before
        # firing the canonical companion script. Without this, repeated
        # blocker re-probes use the same synthetic shape session after session
        # and miss the canonical-probe lesson encoded in rb-246 / guard-147.
        # Per .claude/rules/retrieve-before-deciding.md decision point 4
        # ("re-probing a blocker") and .claude/rules/probe-with-canonical-code-path.md.
        Bash: retrieve.sh --category "blocker probe {blocker.component} canonical companion script" --depth shallow
        From the returned JSON:
          - guardrails[] mentioning canonical-probe, companion-script, synthetic-probe
          - reasoning_bank[] entries describing prior probes of {blocker.component}
            or its sibling components (same skill family)
        Surface to the probe step below:
          - The exact companion_script name(s) the canonical probe should call
          - Failure modes of synthetic probes for this component (rb-246, rb-225)
          - Any prior probe-attempt RB that already established the component
            is fail-open / no-probe (skip the probe, go straight to resolution)
        Fail-open: if retrieve.sh errors, log and proceed to the probe.

        # SECONDARY: Probe infrastructure — but no_probe means "unknown", not "broken"
        Bash: result=$(bash core/scripts/infra-health.sh check {component})
        IF status == "ok": resolve
        ELIF status == "provisionable": attempt provisioning
        ELIF status == "no_probe":
            # No probe exists — can't verify either way.
            # After 3 sessions, clear the blocker and let goals fail-fast if still broken.
            # Re-encountering the failure will re-create the blocker with fresh evidence.
            IF blocker.detected_session + 3 <= current_session:
                resolve with reason "no_probe: cleared after 3-session expiry (fail-open)"
                Log: "BLOCKER EXPIRED (no_probe): {blocker.blocker_id or blocker.id} — letting goals attempt"
        ELSE: log probe failed

    # Phase 0.5b.0.5: Capability Recheck Sweep (see core/scripts/blocker-recheck.sh)
    # For aged blockers routed to [user] or [agent, user], re-run the capability
    # gate against the original failure_reason. If the gate now matches an
    # agent-provisionable capability that was overlooked at creation time,
    # auto-clear the blocker and write an Investigate goal so the retrieval
    # lapse gets learned from instead of buried.
    #
    # Runs BEFORE Phase 0.5b.1 so the user is not notified about a blocker
    # that is actually agent-fixable right now. Dry-run by default — pass
    # --apply to actually clear. Recommended cadence: every aspiration-loop
    # iteration, max-age-hours = config.proactive_escalation.blocker_age_hours.
    # NO known_blockers PRECONDITION (g-115-4328). This call used to be gated on
    # `IF known_blockers is non-empty`, which made the sweep unreachable in exactly
    # the state it exists for: blockers live in TWO stores — the ephemeral per-agent
    # `known_blockers` WM slot AND the durable `blocker_ref` on the goal record — and
    # measured 2026-08-01 all five agents read `known_blockers=null` while six
    # non-terminal goals carried a live `blocker_ref`. Gating on the empty store meant
    # the script never ran, so widening what it can SEE would have shipped inert
    # (guard-1943: a green suite certifies the FUNCTION, never the WIRING).
    # The script now enumerates both populations itself and is cheap + safe when both
    # are empty (it exits reporting total_blockers: 0), so the precondition belongs
    # inside the script, not here. Meter tier `medium` still gates it under pressure.
    Bash: bash core/scripts/blocker-recheck.sh \
            --max-age-hours {config.proactive_escalation.blocker_age_hours} \
            --apply
    # The script's JSON output includes `cleared` count and
    # `investigate_goals_created`. If cleared > 0, those blockers are
    # already resolved in working memory and their unblocking goals are
    # pending; skip to next iteration. Goal-sourced entries are REPORT-ONLY —
    # they are counted and surfaced but never auto-cleared (guard-1978: this
    # sweep decides on a keyword match with no probe behind it, and clearing
    # one would mutate a GOAL, usually another agent's).

    # Phase 0.5b.1: Proactive escalation for aged blockers
    IF config.proactive_escalation.blocker_age_hours:
        Bash: wm-read.sh proactive_escalation_log --json
        FOR EACH blocker WHERE resolution is null:
            # LEGACY-SHAPE TOLERANCE (g-115-3348). The documented schema
            # (handoff-working-memory.md) names these `blocker_id` and
            # `detected_at`, and both writers now emit them. But blockers
            # created BEFORE that fix are live in working memory carrying
            # create-blocker.py's `id`/`created_at` instead. Normalize ONCE
            # here so every read below keys on one name -- previously
            # blocker.detected_at was absent for those, so age_hours was
            # undefined and this entire escalation could never fire, while
            # blocker.blocker_id was None so the cooldown lookup could never
            # match the real id string /notify-user Step 3 writes.
            # Do NOT "fix" this by flipping the reads to id/created_at:
            # infra-health.py streak blockers correctly use blocker_id and
            # would break. Bounded migration shim -- removable once fleet
            # blockers have cycled.
            bid      = blocker.blocker_id or blocker.id
            detected = blocker.detected_at or blocker.created_at
            age_hours = hours_since(detected)
            IF age_hours >= config.proactive_escalation.blocker_age_hours:
                # Re-send cooldown uses re_escalation_hours (default 24), NOT blocker_age_hours
                # (first-fire age only). Before g-115-2400 blocker_age_hours served both roles,
                # so the protocol letter demanded a repeat email every 2h (effective 1h with the
                # meta override) for a standing human-gated blocker — a cadence no session obeyed,
                # producing the silent-skip-judgment pattern instead. Coverage entries appended by
                # /notify-user Step 3 (any outbound notification naming the blocker_id, e.g. a
                # completion-report digest) count as escalations here — newest sent_at wins.
                last_escalation = newest entry in proactive_escalation_log where blocker_id == bid
                IF last_escalation is null OR hours_since(last_escalation.sent_at) >= config.proactive_escalation.re_escalation_hours:
                    Notify the user:
                        category: blocker
                        subject: "Blocker persisting {age_hours:.0f}h: {blocker.reason}"
                        message: |
                            Blocker {bid} has been active for {age_hours:.0f} hours.
                            Type: {blocker.type}
                            Affected goals: {blocker.affected_goals}
                            Unblocking goal: {blocker.unblocking_goal}

                            The one thing that would unblock this:
                            {action_description_based_on_blocker_type}
                    echo '{"blocker_id":"{bid}","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log

    echo '<updated_blockers_json>' | Bash: wm-set.sh known_blockers
Bash: bash core/scripts/execution-diary.sh phase-end phase-0.5b-blockers
```

## Phase 0.5b.1b: Inbox-Alert Age Escalation (g-115-848 — closes g-115-822 finding 2)

Sibling to Phase 0.5b.1 — but the surface scanned is the GOAL QUEUE
(asp-115) instead of the working-memory `known_blockers` slot. When
`world/scripts/alert-sweep.sh` files an Unblock goal for an inbound alert
email it stamps `origin_signal=f"alert-email:{s3_key}"`. If no agent claims
that Unblock within a few hours, the alert silently ages — no upstream
escalation existed before this phase. The bash gate consolidates the
scan + cooldown + notify logic into a single script call (rb-428 pattern).

Rationale (WHY severity ladder and shared cooldown): `core/config/rationale/precheck-gates.md`

Fail-open at every layer: missing config, daemon unreachable, missing
asp-115, a failed board scan (-> empty cooldown set, everything eligible
fires), and per-goal email-send failures all log to stderr and continue.
`inbox-alert-age-check.py` posts the `inbox-alert-aged` board breadcrumb it
reads back as the shared cooldown; the SKILL.md call is the single invoker.

```
# Always-run safety gate (g-115-1526) — NOT meter-gated. This gate escalates
# aged unclaimed alert-derived Unblock goals to the user (external party), so it
# must fire reliably even in the tight zone; the cost is one daemon read +
# cooldown check. Sibling of the always-run handoff-aging-check (Phase 0.5b.2b);
# both notification-age safety gates always run. (Medium tier never dropped it
# anyway — zone_drops=[deferrable] only — but the prior `meter check` call
# emitted a spurious unknown-sweep WARN every iteration; sweep_tier() now
# registers it always-run.)
Bash: bash core/scripts/inbox-alert-age-check.sh --apply
# Iterates asp-115 (the alert-sweep target queue). For each UNCLAIMED Unblock
# goal — status `pending` AND no `claimed_by` (since 2026-08-16, g-115-5929:
# the prior pending/in-progress predicate never read claimed_by, so a goal
# claimed 9h after filing was still escalated as "no agent has claimed it" 7
# days later; a stale claim on a dead session is released by the stranded-claim
# sweep and re-enters this population) — with origin_signal=alert-email:*
# whose age >= threshold:
#   - Scans the coordination board for a recent `inbox-alert-aged` breadcrumb
#     for this goal_id from ANY agent (shared+durable cooldown, g-115-1533).
#   - On miss: routes the escalation through notification_routing_gate.decide
#     (category `blocker`, g-115-5825 user directive) — SUPPRESSED (no user
#     email; the board breadcrumb below IS the fleet-side destination) unless
#     the alert text names a human-only class, in which case it fires the
#     email-send.sh notification as before (subject "Unclaimed alert >Nh:
#     <goal title>", body includes goal_id + classifier subject + age + severity).
#   - Posts an `inbox-alert-aged,<goal_id>,severity:<sev>` board breadcrumb
#     regardless of email/routing outcome (prevents retry storm on transient email
#     failures — the breadcrumb IS the shared cooldown the next sweep reads).
# JSON output includes `applied`, `skipped_cooldown`, and `failed`. Failure
# counts are stderr-noted only — they do NOT block precheck.
```

## Phase 0.5b.1c: User-Blocker Digest Escalation (g-115-3926)

The DELIVERY-CHANNEL sibling of 0.5b.1 / 0.5b.1b / 0.5b.2 / 0.5b.2b. Those four
are each individually correct and **all of them post to the coordination board**,
which is agent-to-agent. That is structurally incapable of discharging a goal
whose blocking condition is a HUMAN action: no agent reading the board can
perform it.

Measured 2026-07-29 — g-326-70 (HIGH, `participants:[agent,user]`, blocking
g-326-63 and g-250-227 under a ship-gate milestone) accumulated 10+ board posts
from two agents in one day while `proactive_escalation_log` stayed EMPTY and the
user was never told. The sibling sweeps each miss it for a correct reason:
0.5b.1b matches an `origin_signal` prefix (this goal's is `unblock:g-326-63`);
0.5b.2 walks `blocked_by` edges (a physical human action has no goal-id to
depend on); 0.5b.2b matches `handoff_to` (unset). The guard-1802 / guard-1890
family: a union of predicates strictly narrower than the population, where every
sweep reports clean forever.

First live run measured **29 user-participant goals, 14 aged past 48h with no
escalation ever sent, oldest 186h (7.75 days).** Re-measured 2026-08-08 under the
fixed cadence: **50 scanned, 36 eligible (14 deliberate, correctly excluded),
ages 8h-463h** — the age filter's removal accounts for 5 of those 36; the other
31 would have qualified under the old 48h threshold too.

Tier is **always-run**, matching its four notification-age siblings — it
notifies an EXTERNAL party, so it must fire even in the tight zone.

```
Bash: bash core/scripts/user-blocker-escalation-check.sh --apply
# Population is IMPORTED from audit-user-to-agent.py's
# `_find_user_participant_goals` (non-terminal + `user` in participants) — never
# re-derived here. A second copy of that predicate is exactly how guard-1802's
# narrow-predicate hole appeared (the old `participants == ["user"]` form had a
# live candidate set of ZERO against 28 real goals).
#
# ONE DIGEST, NOT N EMAILS (reclaim-routed-work.md rule 5). All eligible goals
# produce ONE email, oldest first (measured 2026-08-08: 36). A per-goal send
# would train the recipient to
# filter the sender — a louder version of the silence this phase fixes. The user
# reinforced it from their side (D4): "I want more than one goal per email".
#
# THE TRIGGER IS A FIXED 72h SCHEDULE, NOT AN AGE CROSSING (D2, g-115-4963).
# It fires when `cadence_hours` has elapsed since the LAST DIGEST — goal age
# decides nothing and there is no per-goal cooldown, so anything still waiting
# on the user is on every digest until it is discharged. Re-introducing an age
# predicate rebuilds the unpredictable ping in slower clothing, which is the
# failure the directive names. User's stated value: "Predictable is usually less
# stressful than rare."
#
# AN EMPTY LIST STILL SENDS — the short all-clear (D3): "yes, I do like this, it
# would give me comfort". Silence is the regression, and it is invisible: a
# skipped send and a quiet week produce the same empty inbox.
#
# Category is `blocker` for a populated digest — it is a status report about a
# real block and selects the SendErrorAlert transport. The all-clear sends as
# `info` instead: routing "nothing needs you" through an error alert would
# deliver the comfort email as an alarm.
#
# Goals whose origin_signal marks deliberate user routing (asp-314's park) are
# REPORTED with reason=deliberate_user_routing, never emailed — nagging a
# deliberate choice is the wrong correction, and a silent skip would be
# indistinguishable from a clean sweep.
#
# No schedule marker is recorded when delivery FAILS — marking the clock for an
# email that never sent would start the next window from a send that did not
# happen and suppress the retry for a full cadence.
#
# Fail-open at every layer EXCEPT the schedule gate, which fails CLOSED: this
# phase runs on every loop iteration, so an unreadable clock failing open to
# "send now" would mail the user on every iteration. Always exits 0.
#
# JSON output carries scanned/eligible/applied/all_clear/unknown_age,
# schedule{due,reason,read_ok,hours_since_last_digest,hours_until_next} and
# delivery{attempted,shape,ok,...}. Read `delivery`, not `applied`, to tell
# whether anything was sent — on a sent all-clear `applied` is legitimately 0.
# Cadence: core/config/aspirations.yaml → user_blocker_escalation.cadence_hours (72).
```

## Phase 0.5b.2: Dependency Timeout Escalation

Proactive escalation for dependency-blocked goals approaching
`multi_agent.dependency_timeout_hours`. Bash-enforced by
`dependency-timeout-check.sh` (g-115-3124).

This was the LAST escalation phase still LLM-iterated, and the ONLY one absent
from the phase table above -- no row meant no sweep name, no tier, no meter
accounting, so it was invisible to the very mechanism that decides what
survives context pressure. Measured consequence: the escalation log was empty
fleet-wide while 8+ dependency-blocked goals sat past the 36h threshold (ages
up to 1693h). This phase is the fleet's only automated path from a stuck
dependency to the human, and it had never fired.

Tier is **always-run**, matching its two always-run siblings (0.5b.1b, 0.5b.2b)
-- do NOT `meter check` it. An escalation that fires only when budget permits is
precisely the defect being fixed.

```
Bash: bash core/scripts/dependency-timeout-check.sh --apply

The script owns the DURABLE half, deterministically:
  escalated  -> one coordination-board post per aged dependency. That post IS
                the shared cooldown record. It deliberately REPLACES the
                per-agent WM `proactive_escalation_log`, which both siblings
                abandoned (g-115-1531): a per-agent slot lets all N agents
                escalate the same world goal independently, and does not
                survive a WM reset. Here the action is "email the human", so a
                per-agent cooldown would put N copies in the user's inbox.
  boosted    -> roots flipped to HIGH (agent-resolvable, still pending).

The LLM owns ONLY the transport half:
FOR EACH entry in needs_user_notification:
    Notify the user about the stale dependency.
    (Check world/forged-skills.yaml for a skill whose triggers match
    "notify the user" and invoke it with entry.subject and entry.message.
    If no matching skill is registered, fall back to a participants:
    [agent, user] goal via aspirations-add-goal.sh. Never block on
    notification failure.)

A send failure cannot cause a duplicate-escalation storm: the board cooldown
was already written by the script, so the next sweep stands down regardless.
IF failed is non-empty: surface it. The sweep is fail-open and retries next run.
```

## Phase 0.5b.2b: Handoff Aging Escalation (Item 3; bash-enforced g-115-1524)

Cross-agent handoff goals that sit in the world+agent queues past
`handoff_aging.escalate_hours` (default 72) get a coordination-board
visibility note so the target agent doesn't miss them. Goal-selector already
applies an escalating scoring bonus after `warn_hours`; this phase is the
visibility escalation beyond that.

Bash-enforced (g-115-1524): the previous LLM-iterated pseudocode had NO bash
backstop and silently skipped under abbreviation — a fresh-eyes-review on
2026-06-18 found 6 handoffs aged 78-782h with an EMPTY escalation log.
`handoff-aging-check.{py,sh}` consolidates the scan + cooldown + board-post
into one script call (rb-428 sentinel-gate family), the bash-enforced sibling
of Phase 0.5b.1b's `inbox-alert-age-check`. Runs unconditionally (no
budget-meter gate — same as the original pseudocode; it is a cheap single
daemon read + a safety gate whose whole point is that it ALWAYS runs).

```
Bash: bash core/scripts/handoff-aging-check.sh --apply
# Iterates world + agent queues. For each pending/in-progress goal with
# handoff_to != self AND handoff_created_at age >= handoff_aging.escalate_hours
# AND no cooldown entry within escalate_hours: posts a coordination-board note
# (msg "Handoff aged Nh: <title> [<id>] waiting on <ht>", tags
# handoff-aged,<id>,<handoff_to>) and appends a cooldown entry keyed
# handoff_<id> to wm.proactive_escalation_log. Single-invoker, idempotent
# (cooldown), fail-open at every layer (additive board escalation — a missing
# source just escalates fewer this run; contrast defer-recheck.py's guard-383
# fatal posture, which protects a DESTRUCTIVE defer-clear). JSON output:
# applied / skipped_cooldown / failed.
#
# INBOUND PASS (g-115-5811) — READ THE `inbound` KEY, IT IS REPORT-ONLY.
# The block above is OUTBOUND (work routed AWAY from self) and it self-escalates
# via the board. The `inbound` key is the mirror — pending goals routed TO self
# via intended_agent or handoff_to, which nothing aged before g-115-5811 — and
# it posts NOTHING, because its reader is the agent already running this phase.
# So it is inert unless you actually read it. That is the whole wiring:
#   inbound.reported          -> every aged HIGH (never capped) + the oldest few
#   inbound.aged_count        -> how many are past escalate_hours
#   inbound.suppressed_count  -> how many aged rows the cap did NOT show
#   inbound.age_basis         -> per row: handoff_created_at | created_at | started
# TREAT A REPORTED HIGH ROW AS A SELECTION SIGNAL for this iteration, not as
# background noise: the two incidents that motivated this pass were both HIGH
# goals sitting pending (one a user directive through four cycles, one 111h
# after its block cleared), each found only by an unrelated hand sweep.
# CAVEAT ON THE AGE, so it is not over-read: on most rows `age_basis` is
# `created_at`, meaning "how long this goal has existed", NOT "how long it has
# been routed to you" — measured live, only 2 of 196 inbound rows carry a real
# handoff_created_at. Old-by-created_at is a prompt to look, not proof of
# neglect. `inbound.scanned_pending` / `matched_count` state the population the
# bounded view was drawn from, so a short list is never mistaken for a short queue.
```

Note: the target agent ALSO picks this up via its boot-time pending-handoffs
scan (boot/SKILL.md Step 1.7) and via goal-selector's escalating handoff_bonus.

## Phase 0.5b.3: Structured Precondition Auto-Clear Sweep

Counterpart to the pre-claim re-check in aspirations-execute. Scans goals
deferred due to unmet structured preconditions and auto-clears the deferral
when the predicates now pass. Without this sweep, a goal deferred by the
pre-claim re-check would only re-enter the pool after the 120h
`defer_reason_timeout_hours` fail-open — unacceptable latency when the
dependency is actually satisfied minutes later.

See `core/config/conventions/preconditions.md` for predicate semantics.

Bash-enforced (g-302-02): the previous LLM-iterated loop was replaced by a
single Python pass that calls `predicate.evaluate_all` in-process. This
avoids the vacuous-truth bug at the CLI exit-code level — when a goal's
preconditions list contains ONLY string/free-form entries, the pre-filter
yields zero structured predicates and the script SKIPS rather than
clearing (zero predicates ≠ "all pass"). Mirrors `defer-recheck.sh /
blocker-recheck.sh / monitor-stale-check.sh / pending-questions-sweep.sh`
bash-consolidation pattern (rb-428 family).

```
Bash: bash core/scripts/precondition-defer-recheck.sh --max-age-hours 2 --apply
# Iterates world + agent active queues. For each goal with defer_reason
# starting "precondition_unmet:" and age >= --max-age-hours:
#   - Pre-filters verification.preconditions for dict-structured entries
#     (mirrors goal-selector.py L967-981).
#   - If pre-filter empty: SKIP with reason="no structured preconditions
#     to evaluate — defer is free-form, LLM judgment required". Do NOT clear.
#   - Else: predicate.evaluate_all(struct_pcs, mode="all"). When ALL pass,
#     clear defer_reason + defer_reason_set_at via aspirations.py update-goal.
# Metrics log: <WORLD_PATH>/precondition-defer-recheck-metrics.jsonl
# (run_summary per call; precondition_defer_cleared per actual clear).
# Fail-open at every layer: exits 0 even on partial failures.
```

Cost: one Python pass over active queues per iteration, in-process predicate
evaluation. Cheap predicates (`file_exists_after`, `goal_completed_after`,
`file_check`) are local I/O. `command_succeeds` is subject to its configured
timeout and allowlist; `metric_threshold` invokes its allowlisted script.

## Phase 0.5b.4: Stale-Defer Dependency Sweep (g-115-154)

Counterpart to Phase 0.5b.3 for free-form dependency defers. Scans goals
whose `defer_reason` names one or more dependency goal-ids (`g-NNN-NN`) and
auto-clears the defer when ALL cited deps are `status: completed`.

Rationale (WHY stale-defer dependency sweep): `core/config/rationale/precheck-gates.md`

```
Bash: bash core/scripts/defer-recheck.sh --max-age-hours 2 --apply
# Reads world+agent queues, sweeps eligible goals in one pass.
# `cleared` count in JSON output names the goal-ids whose defer was lifted.
# Fail-open: script exits 0 on all paths; a non-zero exit is a script bug.
```

## Phase 0.5b.5: Pending-Questions Sentinel-Lifecycle Sweep (g-115-486)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.6

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.5) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.6: Parent-Goal Supersession Sweep (g-248-85)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.7

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.6) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.7: Unblock-Parent-Status Sweep (g-250-76, rb-908)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.8

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.7) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.8: Routing-Audit Target-Status Sweep (g-115-1353, rb-1478)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.8.5

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.8) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.8.5: Sweep-Mutation Visibility Surface (g-115-2676)

The three sweeps above (0.5b.6/7/8) mutate a goal's status to a TERMINAL value
but nothing surfaces it — a swept goal leaves BOTH the selector candidate list
AND its blocked list (rb-4149), so the filer never notices. Canonical incident
(2026-07-19): 7 goals silently skipped for days, one a heartbeat-writer fix whose
absence had already produced a live near-miss. This consumer reads the EXISTING
per-sweep metrics logs (no sweep edits — decoupled, single-source-of-truth) and
surfaces apply-mutations newer than a per-agent watermark. ALWAYS-RUN (no budget
meter): a visibility surface must never be dropped, else the invisibility bug
returns. Cheap (3 tail reads + a watermark r/w); quiet on the common empty case.
Fail-open — never blocks the loop.

```
# --announce posts ONE findings-board message per NEW own-applied mutation
# (own-only: with per-agent watermarks, un-filtered every agent would re-announce
# the same mutation → N board posts). The stdout header line surfaces ALL new
# mutations to THIS agent (that IS the cross-agent visibility); the board post is
# the single cross-agent notification, made once by the applier.
Bash: py -3 core/scripts/sweep-mutation-surface.py --announce
IF stdout contains "SWEEP AUTO-CLOSE":
    Surface the line in the iteration header. If a surfaced goal is one YOU filed
    and you did NOT intend it closed, re-open it
    (aspirations-update-goal.sh <id> status pending). Otherwise informational.
# ELSE: quiet — no new sweep mutations since last surface.
```

## Phase 0.5b.9: Credential Defer Auto-Clear (g-115-1709)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.10

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.9) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.10: Defer-Drift Detective Check (g-115-1406, rb defer-drift)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.11

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.10) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.11: Reason-Less-Blocked Sweep (g-115-2595, g-115-2591 lineage)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.12

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.11) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.12: Blocked-Signal Resolution Sweep (g-115-3241)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.13

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.12) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.13: Reclaim — Stale-Defer Audit (lane B)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.14

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.13) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.14: Reclaim — User-Participant Audit (lane P)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.15

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.14) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.15: Human-Blocked Defer Join (lane H) — g-115-3156
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.16

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.15) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.16: Dependency-Cycle Sweep (g-115-3875)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.17

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.16) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.17: Hypothesis-Terminal Goal Sweep (g-115-3355)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.18

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.17) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.18: Locus Sweep (g-115-6684)
IF decision == "drop": SKIP this phase; continue to Phase 0.5b.19

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5b.18) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.19: Self-Blocked Defer Sweep (g-115-7871)
IF decision == "drop": SKIP; continue to Phase 0.5b.20

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phases 0.5b.19-0.5b.21) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.20: Phantom-Goal Audit (g-115-7871)
IF decision == "drop": SKIP; continue to Phase 0.5b.21

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phases 0.5b.19-0.5b.21) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5b.21: Hardcoded-Scope Audit (g-115-7871)
IF decision == "drop": SKIP; continue to Phase 0.5c

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phases 0.5b.19-0.5b.21) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5c: Recurring-Goal Precondition-Filter lastAchievedAt Sweep
IF decision == "drop": SKIP this phase; continue to Phase 0.5c.1

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5c) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5c.1: Recurring-Goal Starvation Detector (g-115-3921)

Every OTHER recurring-cadence detector is CLOSE-TRIGGERED: the streak-break
canary is emitted by `cmd_complete_by`, so a recurring goal that closes LATE
produces a signal while one that simply STOPS closing produces nothing.
`cadence-stale-canary.py` covers the seven skill-invocation cadences and has no
analogue for a goal record. This phase watches the open-loop case.

Runs AFTER Phase 0.5c so any legitimately-shelved goal has already had its
`lastAchievedAt` advanced — but it does not DEPEND on that, because it
re-evaluates the same gates live (0.5c is `deferrable` and drops in a tight
zone). Tier is `medium`, not `deferrable`: a detector that exists because a
5-day blind spot went unnoticed must not be the first thing dropped, and its
cost is one daemon read plus a median.

```
Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check recurring-starvation-check)
IF decision == "drop": SKIP this phase; continue to Phase 0.5e
Bash: bash core/scripts/recurring-starvation-check.sh --apply --max-file 1
# Reports EVERY starved recurring goal (age > 3 x basis) but files at most ONE
# Unblock per run, worst overdue-ratio first, deduped on the exact
# origin_signal `unblock:recurring-starved-<goal-id>`.
#
# Read the SUMMARY LINE, not just the FILED line. The cap bounds what is
# filed, never what is found: first run reported 22 starved and filed 1. A
# count that stays high across iterations is the finding — 8 of those 22 were
# echo's own asp-001 lane, all stopped within a 4-hour window on 2026-07-25
# and unfired for 5 days, which is one common cause rather than 8 independent
# starvations. Filing per-goal for a cluster like that fragments one
# diagnosable finding into N undiagnosable ones.
#
# Two evidence gates, both reported in the summary (guard-138 — a clock-only
# staleness heuristic must be paired with evidence before it acts):
#   basis_suppressed=N — declared interval_hours is frequently aspirational,
#     so the multiple is taken against max(interval, demonstrated p50). Do NOT
#     quote a declared-interval ratio as urgency without checking the basis
#     column: g-115-22 declares 6h against a ~30h demonstrated cadence.
#   shelved=N — a goal whose preconditions/fire_when currently FAIL is parked,
#     not starved. Same evaluator and same gate-gathering as Phase 0.5c.
# Fail-open: always exits 0.
```

## Phase 0.5e: Skill-Invocation Cadence Battery (g-115-2984, fix for g-115-2982)

ONE call runs the cadence gate checks for the SIX skill-invocation cadences —
fresh-eyes-review (0.5e, 25 goals), fresh-eyes-program (0.5e.5, 100),
fresh-eyes-tree (0.5e.7, 200), felt-sense (0.5f, 75), curriculum (0.5i, 24h),
evolution (0.5j) — and reports which FIRE. This REPLACES the six separate
per-phase `<cadence>-cadence-check.sh` gate calls (the old Phases
0.5e/0.5e.5/0.5e.7/0.5f/0.5i/0.5j, now collapsed here). Starvation class it
kills: felt-sense (0.5f) starved 3 days / 581 goals because the LLM abbreviated
its phase under context pressure — the gate was never run, so the ritual was
never invoked (g-115-2982 REFUTED the budget-meter mechanism; the non-fire at
diff 581≫cadence 75 proved the phase was never invoked). Same pattern as
precheck-sentinel-battery (g-115-2303) + orchestrator-entry-battery (g-115-2550):
post-autocompact, "run the cadence battery" is the ONE line that must survive
summarization; the battery's output re-derives the full cadence set from
`core/scripts/_cadence_registry.py` (SSOT) so a cadence can never silently fall
out of the protocol.

Scope: l1-skew (Phase 0.5g) and health-regression (Phase 0.5h) are NOT in the
battery — they keep their own phases below. l1-skew self-acts (posts findings to
the board inside its own script, so it cannot starve via the skill-skip mode);
health-regression has a multi-step verify/verdict/revert flow that does not fit
the uniform "gate → exit 0 → invoke one skill" shape. Both sit outside the
skill-invocation-skip starvation class this fix targets.

**Do NOT re-derive health-regression's exclusion from its mode.** This paragraph
read "health-regression is DORMANT (collect-only)" until 2026-07-30; that premise
expired on 2026-07-14 when `health_regression.mode` advanced to `full` under
explicit user authorization (verified: `health-regression-check.sh --json` returns
`"mode": "full", "calibrated": true`). The exclusion never rested on dormancy —
it rests on the multi-step SHAPE above, which is unchanged and still true. The
trap is that a reader who notices the subsystem is live may read the stale reason
as lapsed and add it to the battery, where its three-call flow does not fit.

The battery is READ-ONLY (the cadence checks only read goal-count vs last-fire)
and does NOT read the budget meter — the checks are cheap, so the meter's
tight-zone `deferrable` drop is applied at DISPATCH time here (gating the
EXPENSIVE skill invocation, not the cheap check).

```
Bash: bash core/scripts/precheck-cadence-battery.sh
IF output matches "[cadence-battery] all <N> cadence gates noop — nothing to fire":
    # <N> is the REGISTERED count printed by the battery, not a literal. It read
    # "all 6" here until 2026-08-02 (g-115-4691 added a 7th) — a hardcoded count
    # in the SKIP condition silently stops matching the moment the registry
    # grows, which is guard-1715 (an enumerator's all-clear is bounded by the
    # population IT declares, not the one the reader remembers). Read the count
    # the battery prints; do not carry one.
    SKIP all cadence dispatch — continue to Phase 0.5g.
IF output reports wrapper_failed OR carries an error= line:
    Fall back to running each cadence check individually (the six
    `*-cadence-check.sh` gates in _cadence_registry). The battery never blocks
    the loop.
FOR EACH "▸ CADENCE FIRE: <name> (phase <phase>) meter=<meter_name> → <dispatch>" line:
    # Meter-gate the EXPENSIVE skill invocation (deferrable tier — drop in tight zone).
    Bash: decision=$(bash core/scripts/aspirations-precheck-budget-meter.sh check <meter_name>)
    IF decision == "drop":
        Output: "▸ cadence <name> fired but meter-dropped (tight zone) — deferring the ritual"
        continue   # do NOT invoke the ritual this iteration
    # Dispatch per <name>:
    fresh-eyes-review  → Invoke /fresh-eyes-review --cadence
    fresh-eyes-program → Invoke /fresh-eyes-program --cadence
    fresh-eyes-tree    → Invoke /fresh-eyes-tree --cadence
    strategic-scan     → Invoke /aspirations-strategic-scan with
                         scan_trigger="time_cadence" (g-115-4691)
                         # Idempotent with ORCHESTRATOR Phase 1.5 via the shared
                         # last_strategic_scan stamp: whichever fires first
                         # stamps it (Phase S5, the single writer per guard-155)
                         # and the other sees a fresh stamp and no-ops. Same
                         # pairing evolution uses with Phase 8.8.
                         # This gate covers the TIME trigger only. Phase 1.5
                         # keeps goal_cadence + recurring_settling, which can
                         # only fire SOONER — so the 4h bound enforced here is
                         # what makes starvation structurally impossible.
    felt-sense         → Invoke /felt-sense-checkin --cadence
    evolution          → Invoke /aspirations-evolve
                         # its MANDATORY final write stamps last_evolution_at_time +
                         # bumps evolutions_this_session (loop-state-bump-counters
                         # --evolution-fired), keeping it idempotent with Phase 8.8
                         # and respecting global.max_evolutions_per_session
    curriculum         → # guard-33 invariant: NEVER call curriculum-promote.sh directly;
                         # promotion routes ONLY through /curriculum-gates.
                         Bash: eval_json=$(bash core/scripts/curriculum-evaluate.sh)
                         # FOUR response shapes, not one (g-115-3607). Only `gates` is
                         # present in every non-error shape; the scalars are branch-local:
                         #   unconfigured  -> configured:false, all_passed, gates
                         #   stage-missing -> error (NO configured key at all)
                         #   TERMINAL      -> configured, current_stage, all_passed:true,
                         #                    terminal_stage:true, gates:[]   <- early
                         #                    return at curriculum.py:392, omits the four
                         #                    scalars below
                         #   full          -> + stage_name, gates_total,
                         #                    gates_passed_count, next_stage
                         # So branch on SHAPE before reading any shape-local field.
                         Parse eval_json: configured, error, all_passed, gates,
                                          current_stage, terminal_stage, stage_name, next_stage
                         # Stamp the cadence slot AFTER evaluate (bare quoted-ISO; stamping
                         # after means a skipped/failed eval re-fires next iteration).
                         Bash: printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | bash core/scripts/wm-set.sh last_curriculum_eval
                         IF eval_json.error is non-null:
                             Output: "▸ CURRICULUM: evaluate returned an error ({error}) — snapshot NOT refreshed"
                             continue
                         IF eval_json.configured == false: continue  # no curriculum for this agent
                         IF eval_json.terminal_stage == true:
                             # all_passed:true with zero gates is the CORRECT steady state
                             # at the end of the curriculum, NOT a pending promotion. Without
                             # this branch the ELSE below fired and rendered
                             # "re-evaluated None — None/None gates pass (not yet promotable)",
                             # asserting the opposite of the truth and sending a reader
                             # chasing a phantom gate failure.
                             Output: "▸ CURRICULUM: {current_stage} is the TERMINAL stage (no graduation gates) — all_passed=true is the correct end state, nothing to promote; snapshot refreshed"
                             continue
                         # Derive the counts from `gates` rather than the gates_total /
                         # gates_passed_count scalars: `gates` is the one field every shape
                         # carries, so the render cannot go None/None if a new shape appears.
                         gates_total        = len(eval_json.gates)
                         gates_passed_count = count of eval_json.gates where passed == true
                         IF eval_json.all_passed == true AND eval_json.next_stage is non-null:
                             Output: "▸ CURRICULUM: all gates pass at {stage_name} — routing to /curriculum-gates for promotion to {next_stage} (guard-33, register+defer)"
                             invoke /curriculum-gates   # non-blocking, deduped
                         ELSE:
                             Output: "▸ CURRICULUM: re-evaluated {stage_name} — {gates_passed_count}/{gates_total} gates pass (not yet promotable); snapshot refreshed"
```

Per-cadence rationale (the ritual is unchanged — only the CHECK moved into the
battery):
- **fresh-eyes-review** (25 goals): portfolio-direction briefing (Self still
  right? right problems?), archived to `agents/<agent>/temp/`. Distinct from
  sq-012 + strategic-scan S3b. See `.claude/skills/fresh-eyes-review/SKILL.md`.
- **fresh-eyes-program** (100 goals): The Program shared-purpose briefing
  (is `world/program.md` still right? have the Selfs drifted?). See
  `.claude/skills/fresh-eyes-program/SKILL.md`.
- **fresh-eyes-tree** (200 goals): L1 taxonomy briefing (are the L1s still the
  right top-level cuts?). See `.claude/skills/fresh-eyes-tree/SKILL.md`.
- **felt-sense** (75 goals): structured 7-lane self-audit; material Self findings
  route through guard-380 post-notification, cosmetic findings journal only. See
  `.claude/skills/felt-sense-checkin/SKILL.md`.
- **curriculum** (24h, g-115-1801): read-only gate re-eval so the stored snapshot
  never goes stale; guard-33 — promotion routes ONLY through /curriculum-gates.
- **evolution** (g-115-2240): precheck-side net so recurring-heavy sessions
  (which bypass Phase 8.8) don't starve evolution; idempotent with 8.8 via
  the shared `last_evolution_at_time` stamp.

## Phase 0.5g: L1 Distribution Skew Check (S1 — Tree Taxonomy Review)
IF decision == "drop": SKIP this phase; continue to Phase 0.5g.5

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5g) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5g.5: Scar-Tissue Check (subtractive gradient — complexity budget)
IF decision == "drop": SKIP this phase; continue to Phase 0.5g.6

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5g.5) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5g.6: Completed-Not-Closed Triage (unbanked finished work)
IF decision == "drop": SKIP this phase; continue to Phase 0.5g.7

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5g.6) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 0.5g.7: Completed-Not-Closed DRAIN (bounded per-iteration obligation)

Phase 0.5g.6 REPORTS the backlog on a cadence; this phase CONSUMES it, a few
goals per iteration, as an obligation the reducer discharges before selecting new
work. It exists because the report had no consumer that the loop was bound to:
measured 2026-08-16 (alpha reducer, `hostname` cc-04), 360 of 361 open alpha
claims were finished work nobody closed (7 worker SIDs, 261 held by dead Bodies,
claim ages to 218h), the cadence lane had fired and posted the whole time, and
the recurring drain goal g-115-6337 (12h) lost in the selector to fresher work.
The population is a guard-4000 class: a KEEP that never consults age grows
without bound. Two things fix it together — worker-loop Phase 4a now closes each
unit at end of unit (so steady-state inflow is ~0), and THIS phase drains what
still lands here (crashed Bodies, pre-fix backlog, Phase 3.7 STRANDED holds).

Tier is **always-run**: it is bounded (default 3 rows, `completed_not_closed_drain
.per_iteration` in `core/config/aspirations.yaml`), costs one ~1s daemon read,
and it is an obligation, not a sweep — the tight zone is exactly when a backlog
would otherwise be deferred forever.

```
Bash: bash core/scripts/completed-not-closed-slate.sh
# Report-only (no --apply exists, and none must be added — see the 0.5g.6 note
# above for the two measured-rejected remedies). Prints the FULL population beside
# the bounded slate (guard-3830: a batch bound is never a scan result), the OLDEST
# eligible rows first, each with the note's OWN first line and any other goal
# ids the note names. Rows younger than `min_claim_age_hours` are held back
# (a live Body closes its own unit at Phase 4a; a fresh open+noted row is a Body
# mid-close, not backlog) and the reducer's own SID is excluded.
#
# POPULATION (widened 2026-08-16, zeta msg-20260816-195023-zeta-5111): OPEN
# (in-progress OR pending), non-recurring, `outcome_note` set, NO `defer_reason`.
# "Yours" = claimed_by you, OR unclaimed with `executed_by` you (a released
# claim keeps the note and the executor stamp, loses the holder). The first cut
# read only in-progress+claimed and reported ~clean while 220 pending goals
# carried a note (guard-1802: predicate narrower than the creating path).
# Each slate row prints `<status>/via-<claim|executed_by>`: a `pending/via-
# executed_by` row is one you executed and released — dispose it the same way;
# complete-by needs no live claim. Deferred rows are deliberately absent: the
# defer/precondition lanes own and re-probe them.

IF slate is EMPTY: continue to Phase 0.5h. (An empty slate with a non-zero
    population is the age gate holding fresh rows back — say so; it is not a
    drained backlog.)

FOR EACH slate row — DISPOSE IT. This is LLM judgment per goal, never a
predicate (guard-2852c: LENGTH IS NOT VERDICT; the note head is necessary,
not sufficient):
    1. READ THE RECORD, authoritatively — not the slate line — through the
       slate's COMPACT reader, never the raw record:
       Bash: bash core/scripts/completed-not-closed-slate.sh --show <goal-id>
       It prints status/holder/verification/description-head and the FIRST 3500
       chars of `outcome_note`; page a long note with `--note-from 3500` (then
       7000 …) only as far as a verdict needs. Do NOT use
       `aspirations-query.sh --goal-field id … --full` here: that is 10k+ chars
       per goal, three per iteration, twice each, and a triage agent following
       exactly that instruction died of autocompact thrash on 2026-08-16.
       Re-check `status`/`claimed_by` on the first line of that read — another
       agent (or the recurring drain goal) may have disposed it since the slate
       printed. If it is no longer OPEN (in-progress/pending), now carries a
       `defer_reason`, or is now claimed by someone else, SKIP it and count it
       as `skipped-moved`.
    2. CROSS-RECORD CLAIMS (guard-3824 / guard-3880 — a note head structurally
       cannot settle a claim about a DIFFERENT record). If the note says a
       residue was "filed as / folded into / routed to / relayed to" something:
       open the named record and confirm the token it says was written is there
       (`completed-not-closed-slate.sh --show <other-id> --note-chars 600`, or
       the board / tree node). If the claim names NO id, that is the tell — a real filing has
       an id: FILE the residue yourself before closing (guard-3880: the reducer
       close is the LAST moment a relayed finding can acquire an owner). If the
       named record lacks the promised content, either write it now (cheap) or
       HOLD.
    3. DISPOSITION — exactly one of:
       CLOSE   — the note states completion AND the verification outcomes are
                 met on the evidence it cites (and step 2 passed):
                 Bash: bash core/scripts/aspirations-complete-by.sh --source <world|agent> <goal-id> \
                         --key-finding "<one line, <=200 chars, from the note's own head>"
                 Bash: bash core/scripts/aspirations-update-goal.sh --source <world|agent> <goal-id> outcome_class <deep|routine>
                 # complete-by is the canonical attribution writer: status
                 # completed (or a recurring cycle bump), completed_by=you,
                 # completed_date/at, claim fields popped, key_finding persisted
                 # ON THE RECORD (2026-08-16), and the team-state
                 # recent_completions row — that append is gated on
                 # --key-finding, so ALWAYS pass it (one team-state writer per
                 # close path; this call IS the close of record here).
       RELEASE — the note says work REMAINS against a future gate (elapsed
                 time, a deploy, a partner's leg) — release the claim AND write
                 the structured defer in the SAME step, never a bare release
                 (g-115-5177: a bare release re-arms re-execution of finished
                 work at rank 1 on fresh metadata):
                 Bash: bash core/scripts/aspirations-release.sh <goal-id> --source <world|agent>
                 Bash: bash core/scripts/aspirations-update-goal.sh --source <world|agent> <goal-id> defer_reason "precondition_unmet: <the gate, verbatim from the note>"
                 (on a `via-executed_by` row there is no claim: release is an
                 idempotent no-op that reports had_claim=false — not an error;
                 the defer write is the operative step)
                 (or `blocked` + blocker_ref if the note names an unfixable
                 blocker; `human_blocked:` only for a genuinely human gate)
       HOLD    — the evidence is genuinely ambiguous (note head is a title
                 echo, no verdict; a stated Phase 3.7 STRANDED hold; a claim
                 you cannot check now). RECORD it — a HOLD that writes nothing
                 is re-served next iteration, and because the slate is
                 oldest-first and bounded, three permanent holds starve every
                 row behind them forever (found by the 2026-08-16 review):
                 Bash: bash core/scripts/completed-not-closed-slate.sh --hold <goal-id> --reason "<one line>"
                 The row is held back for `hold_ttl_hours` (24h) and then
                 RESURFACES carrying its hold_count and last reason (a lease with
                 a release path, guard-3419 — never a permanent exclusion). On
                 the THIRD hold of one goal, file an Investigate rather than
                 hold again; the ledger touches no goal record.
    4. Re-read the record after the write (`--show <goal-id> --note-chars 0`)
       and confirm the status you intended landed on its first line (the echo
       is not proof; own-cloud writes can be lost silently).

PEER LEG — "completions across agents" (2026-08-16). The slate is
holder-scoped by design (the HOLDER's reducer judges its own units), which
leaves one population with no drainer at all: a DORMANT or RETIRED holder's
finished work. stranded-claim-sweep KEEPs noted claims (never a close),
SKIP_STATUSES hides them from every selector, and no peer's slate enumerates
them — so they sit at in-progress until the holder returns, which for a
retired agent is never. The slate prints "other holders' noted-open goals:
<peer>:<n>[<u> unclaimed](oldest <h>h)" whenever any exists — an UNCLAIMED
row is keyed by its `executed_by`, so a dormant executor's released units are
counted under that peer too. FOR EACH such peer whose oldest row is older
than 48h:
    Bash: bash core/scripts/liveness-check.sh --agent <peer> --json
    IF verdict is `dormant` OR `retired` (NEVER on `alive` or `unknown` —
       check-team-state-before-silent.md rule 6; an alive peer drains its own,
       and `unknown` means the signal disagreed, not that the peer is gone):
        Bash: bash core/scripts/completed-not-closed-slate.sh --agent <peer> --min-age-hours 48
        Dispose its rows by the SAME per-row protocol above (--show, cross-record
        check, CLOSE / RELEASE / HOLD), within the SAME per-iteration bound —
        the peer rows share the `per_iteration` budget with your own; never
        exceed it. Say "cross-agent drain of dormant <peer>" in each
        key_finding / defer_reason so the record shows who closed it and why
        it was not the holder. `--hold` writes to YOUR OWN ledger
        (`agents/<you>/session/cnc-drain-holds.jsonl`) on EVERY lane including
        this one, and the slate reads the file it wrote (g-115-6494). A hold is
        a decision YOU made about a row, not a property of the row's holder.
        This paragraph previously said the opposite — "a peer's `--hold` writes
        to THAT peer's ledger ... the hold belongs to the goal's holder" — which
        was never true of the command prescribed four lines up: that `--hold`
        carries no `--agent`, so it always landed in the acting agent's ledger
        while the peer slate read the peer's. The hold therefore suppressed
        nothing on any lane but your own, silently, while still incrementing
        hold_count toward the third-hold Investigate escalation.
        Do NOT pass `--agent` to `--hold` to "match" the read. It no longer
        changes the ledger, and on the `(unattributed)` lane it used to create
        `agents/(unattributed)/session/` — a directory for a bucket key that is
        not an agent (.claude/rules/path-resolution.md L1 cruft). Consequence to
        expect: a hold you take on a peer lane suppresses that row for YOU only;
        another agent draining the same lane holds it independently.
    ELSE: leave them; state the verdict in the conservation line.
    `(unattributed)` rows (no claim AND no `executed_by` — pre-E1 records, or
    a note written outside a claim) have no holder to be dormant: whoever's
    iteration this is drains them, oldest first, within the same bound —
    `completed-not-closed-slate.sh --agent "(unattributed)" --min-age-hours 48`
    (the literal holder key the slate uses; nothing else matches it).

CONSERVATION LINE (guard-990) — one line in the turn, always:
    "▸ cnc-drain: population=<mine_noted> slate=<n> consumed=<n> = closed <a> +
     released <b> + held <c> + skipped-moved <d>; dropped=<dropped>
     recent_hold=<mine_held_back_recent_hold>; peers=<peer:verdict:consumed …>"
    consumed MUST equal closed + released + held + skipped-moved. A run whose
    numbers do not add up has dropped an item; find it before continuing.

Then continue to Phase 0.5h.
```

**Relationship to the recurring drain goal g-115-6337.** That goal is the DEEP
pass (batch 10, full population census + trend, findings post) on a 12h cadence
and stays. This phase is the trickle that runs whether or not the selector ever
picks that goal, so a backlog can no longer accumulate merely because fresher
work outranked it. Both use the same canonical writers, so there is nothing to
reconcile between them; the only shared rule is: never bulk, always by evidence.

## Phase 0.5h: Health-Regression Detection + Revert Sweep (health-ledger subsystem)
IF decision == "drop": SKIP this phase; continue to Phase 1

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Phase 0.5h) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Folded 0.5i + 0.5j: Curriculum and Evolution cadences, now in the Phase 0.5e Cadence Battery (g-115-2984)
Not chain phases — they carry no drop-branch; both CHECKS run inside Phase 0.5e.

▸ Body in `core/config/aspirations-precheck-digest.md` (§ Folded 0.5i + 0.5j) — `bash core/scripts/load-precheck-digest.sh`. g-115-6583.

## Phase 1: Recurring Goal Check

```
check_recurring_goals()
# Ensures recurring goals are properly tracked and due goals are flagged
```

## Phase 2: Budget Meter End (Magic Wand 2 — g-115-509)

Finalize the precheck budget meter — writes a summary record (sweeps_ran,
sweeps_dropped, total_elapsed_ms) to `agents/<agent>/session/precheck-drops.jsonl`
and clears the per-iteration state file. One-shot — do not call without a
preceding `meter start` (Step 0a).

```
Bash: bash core/scripts/aspirations-precheck-budget-meter.sh end
```

## Chaining

- **Called by**: `/aspirations` orchestrator (every iteration, first phase)
- **Calls**: `aspirations-read.sh`, `aspirations-meta-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `wm-read.sh`, `wm-set.sh`, `aspiration-trajectory.sh` (cycle detection), `aspirations-add-goal.sh` (cycle detection, hypothesis pipeline, accuracy gate), `aspirations-query.sh` (user-goal reclassification), `aspirations-update-goal.sh` (user-goal reclassification), `world-cat.sh` (capability-routing convention), `pipeline-read.sh` (hypothesis pipeline + accuracy health), `fresh-eyes-cadence-check.sh` (Phase 0.5e gate), `recurring-precondition-sweep.py` (Phase 0.5c), `recurring-starvation-check.sh` (Phase 0.5c.1), `health-regression-check.sh` (Phase 0.5h detection sweep), `health-revert.sh` (Phase 0.5h verify + tiered revert), `/create-aspiration` (health + pipeline depth), `/fresh-eyes-review --cadence` (Phase 0.5e fire), CREATE_BLOCKER protocol
- **Reads**: Aspirations compact, working memory (blockers), guardrails, trajectory data (cycle detection), pipeline meta (hypothesis counts + accuracy), `core/config/aspirations.yaml` (pipeline_low_water_mark, hypothesis_pipeline_low_water_mark, accuracy_critical_threshold, accuracy_min_sample)

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the last `wm-set.sh`, `aspirations-read.sh`, or
`aspirations-add-goal.sh` call. Never end with a text summary.
