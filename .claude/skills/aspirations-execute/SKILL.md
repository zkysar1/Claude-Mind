---
name: aspirations-execute
description: "Phase 4 of the aspirations loop: executes a selected goal end-to-end with precondition checks, LLM-driven intelligent retrieval, memory deliberation, subagent delegation, primary execution, fail-fast cascade, experience archival, context-utilization feedback, domain post-execution steps (commit/push per world/conventions), knowledge reconciliation, and batch execution. Fires only when called from inside the /aspirations orchestrator — after /aspirations-select has chosen a goal and /decompose has broken it into primitives. Never invoke directly from reader or assistant mode."
user-invocable: false
parent-skill: aspirations
triggers:
  - "Phase 4"
conventions: [aspirations, pipeline, experience, tree-retrieval, retrieval-escalation, goal-schemas, infrastructure, reasoning-guardrails, agent-spawning]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-execute-0cf4df"
previous_revision_id: null
---

# Phase 4: Goal Execution

Invoked as Phase 4 of the aspirations loop after goal selection (Phase 2) and decomposition (Phase 3). Covers the full execution pipeline: precondition checking, intelligent LLM-driven retrieval, memory deliberation, agent delegation, primary execution, fail-fast cascade, experience archival, context utilization feedback, domain post-execution steps, knowledge reconciliation, and batch execution.

## Inputs (from orchestrator)

- `goal`: Selected goal object from Phase 2
- `aspiration_id`: Parent aspiration ID
- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls
- `batch_mode`: Boolean (from Phase 2)
- `outcome_class`: Set by Phase 4-post after execution

---

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

## Execution Autonomy Rule

The agent makes ALL decisions autonomously during goal execution — never
stops to ask "should I push?" or "what next?". Phase 4.2 domain steps handle
push/deploy; the loop handles selection.

For significant judgment calls (architecture choices, deploy strategy,
trade-offs), pick the safer/simpler option when unsure, then log one
`pq-NNN` entry in `agents/<agent>/session/pending-questions.yaml` framing it as
"I decided {X} because {Y} — override if you disagree" with
`default_action: "Already executed: {what}"` + `status: pending`. User
reviews retroactively via `/respond` or session recap. Continue immediately.

---

## Cognitive Primitives (Always Available)

During ANY phase — goal execution, error handling, reflection, spark checks —
the agent can create goals from things it notices. Five types: **Unblock**
(CREATE_BLOCKER only — see its digest), **Investigate**, **Idea**,
**Maintain** (inline framework fix, `status: completed` on creation),
**Cross-Agent Insight** (posted to findings board).

Full schemas, the Maintain inline-fix rationale, and the Cross-Agent Insight
tag spec live in `core/config/cognitive-primitives-digest.md`. Load on-demand:

```
Bash: load-cognitive-primitives.sh → IF path returned: Read it
# Then file the primitive per the digest schema.
# See guard-148 (Maintain) and core/config/conventions/board.md (Cross-Agent Insight).
```

A single event can spawn all five primitives simultaneously — they are NOT
mutually exclusive. Dedup before filing: check pending/in-progress goals for
similar titles first.

---

## Phase 4 Setup: Cross-Agent Env Routing (g-115-978 Option 3)

When the selector pulled this goal from a sibling agent's queue,
`aspirations-select` Phase 2.95 split `source='cross-agent:<sib>'` into
`effective_source='agent'` + `cross_agent_owner='<sib>'` and wrote the latter
into `iteration-checkpoint.json` (validated by `loop-state-save.py` SCHEMA).
This setup fires BEFORE the precondition re-check so every subprocess call
in Phase 4 — including pre-claim update-goal and depth-estimate — routes
through the owner's identity. Without it, calls write to THIS agent's queue
and the cross-pulled goal never lands back in the sibling's `aspirations.jsonl`.

```
# Read cross_agent_owner from the iteration checkpoint. Empty/absent means
# this is a normal (non-cross-agent) execution; ENV_PREFIX stays empty and
# every downstream call behaves exactly as before.
#
# Goes through `loop-state-save.sh read`, NOT a hardcoded agents/<a>/session/
# path (g-306-136). The checkpoint is body-keyed: CLAIM and EXECUTE are both
# WORKER_PHASES, so in a worker body the claim wrote it under
# sessions/<unitKey>/ and a literal agent-wide read here would silently miss
# it — the `2>/dev/null || echo ""` fallback would render that as "not a
# cross-agent execution" and quietly write the goal back to the WRONG queue.
# The wrapper resolves through _checkpoint_path(), so reader and writer cannot
# diverge.
Bash: cross_agent_owner=$(bash core/scripts/loop-state-save.sh read 2>/dev/null | py -3 -c "import json,sys; d=json.load(sys.stdin); print((d or {}).get('cross_agent_owner','') or '')" 2>/dev/null || echo "")

IF cross_agent_owner is non-empty:
    # ENV_PREFIX applies ONLY to subprocess calls whose AGENT_DIR resolves
    # via MIND_AGENT. Affected (write the owner's state):
    #   - aspirations-update-goal.sh --source agent <goal-id> <field> <value>
    #   - aspirations-complete-by.sh <goal-id> (when --source agent)
    #   - aspirations-release.sh <goal-id> (when --source agent)
    #   - iteration-close.sh --phase * (operations against the owner's queues)
    #   - recurring-close.sh <goal-id> (wraps iteration-close internally)
    # NOT affected (use the calling agent's identity):
    #   - team-state-in-flight.sh --agent <SELF>   (NEVER swap — this is the
    #     world-level liveness record; partner observers need to see ALPHA
    #     claimed BRAVO's goal, not BRAVO claimed it)
    #   - board-post.sh (board entries are authored by the calling agent)
    #   - heartbeat-tick.sh (ticks THIS runner's heartbeat)
    # MOVED SIDES (g-306-249) — aspirations-claim.sh was listed here as NOT
    # affected, on the reasoning "--source world is world-scoped, no swap". That
    # held only while the claim was world-only. It now honors `&source=agent`
    # and resolves `ctx.paths.agent` from the X-Mind-Agent header, so a
    # CROSS-AGENT goal (effective_source='agent') claimed without the prefix
    # resolves THIS agent's queue, not the owner's — the goal is not there, and
    # the claim 404s. Prefix it like the others when --source agent:
    #   - aspirations-claim.sh <goal-id> --source agent   (world claims: no swap)
    # Pattern: prefix the affected subprocess invocations with
    #   MIND_AGENT={cross_agent_owner} <command>
    # rather than the global env-prefix the PreToolUse[Bash] hook applies.
    # Explicit per-call prefix wins (`bash-agent-inject.sh` detects and
    # preserves user-supplied MIND_AGENT=*).
    ENV_PREFIX="MIND_AGENT=${cross_agent_owner}"
ELSE:
    ENV_PREFIX=""   # normal execution; no swap
```

`ENV_PREFIX` is the variable name every downstream Bash invocation in this
SKILL.md references when calling an affected script. A non-cross-agent
iteration sets it to empty string and the calls behave exactly as before.

**Enforced helper (option A, g-115-1847 — bravo decision msg-20260709-021804-bravo-118).**
For any NEW cross-agent write site, and for ad-hoc cross-agent writes outside
this SKILL.md, prefer the enforced wrapper over hand-rolling the prefix:
```
bash core/scripts/cross-agent-write.sh "{cross_agent_owner}" <write-script.sh> [args...]
```
The owner is passed as a DATA argument, so the `MIND_AGENT=<owner>` prefix is
applied by code and cannot be forgotten (the fragility option A closes). It is
the SSOT for the affected/exempt classification the comment above documents:
the identity/liveness-exempt scripts (`aspirations-claim.sh`, `board-post.sh`,
`team-state-in-flight.sh`, `team-state-clear-in-flight.sh`, `heartbeat-tick.sh`)
are REFUSED (exit 2) so an exempt call can't be swapped to the owner; a
`cross_agent_owner` of `""`/`"-"` is a pure passthrough. The existing
`${ENV_PREFIX}` call sites below already apply the prefix correctly and remain
as-is — the helper is the canonical path for callers that would otherwise
re-derive the prefix by hand. Regression coverage (mechanism + helper):
`core/scripts/tests/test_cross_agent_write.py`.

## Phase 4-lw: Lightweight Goal Mode — Trivial-Goal Classifier (g-305-15; design g-305-02)

Runs ONCE at Phase-4 entry (after Setup, before the Cost-Ordered Preamble).
Predicts whether this goal is TRIVIAL so the Phase 3.9-4.5 ceremony can be
skipped. `trivial_mode` is carried in-context for the rest of Phase 4 (exactly
like `effort_level`), NOT re-read per phase.

```
Bash: tg_json = py -3 core/scripts/trivial-goal-classify.py {goal.id} --source {source} --output json
Parse tg_json.verdict → trivial_mode = (verdict == "trivial")
# Master flag (aspirations.yaml lightweight_mode.enabled) defaults OFF (g-306-08):
# verdict is "full" for EVERY goal until the flag is validated + flipped on, so
# trivial_mode stays False and this spec is byte-identical to pre-change behavior.
# Fail-to-full: any classifier error also yields "full" — never blocks execution.
IF trivial_mode:
    Output: "▸ Lightweight mode: TRIVIAL {tg_json.reasons} — skipping the SKIP-marked phases below (Step 5e Gate D STILL runs)"
    Bash: loop-state-save.sh update --set "phase_progress.trivial_mode=true"   # compaction survival; auto-clears at LOOP_CONTINUE. On resume re-read this OR re-run the classifier (idempotent); absent either, default full (SAFE).
    Bash: echo '{"entry_type":"observation","goal_id":"{goal.id}","content":"lightweight-mode trivial; skipping 3.9-4.5"}' | bash core/scripts/execution-diary.sh append
ELSE:
    trivial_mode = false   # full ceremony runs (default)
```

**Authoritative skip table** (consistent with `core/config/execute-protocol-digest.md`;
both MUST stay in sync — supp-guard L1013: divergent dual specs silently regress).
When `trivial_mode` is true, SKIP the **SKIP**-marked phases; **KEEP** phases ALWAYS
run. The Phase 4-post escape hatch re-enables the SKIP phases if execution falsifies
the prediction (diff / surprise / failure).

| Phase | trivial_mode |
|-------|--------------|
| Phase 4 Setup (env routing) | KEEP |
| Phase 4 Preamble — Cost-Ordered Preconditions | KEEP |
| Phase 4-pre Target-State Probe | SKIP |
| Phase 3.9 Pre-Execution Domain | SKIP (already conditional) |
| Phase 3.95 Depth Estimate | SKIP |
| Phase 3.97 Inbound Signal Sweep | SKIP |
| Phase 4 Execute — Intelligent Retrieval Steps 1-5d | SKIP (largest single saving) |
| **Phase 4 Execute — Step 5e Gate D** | **KEEP — UNTOUCHED (GATE-INTEGRITY; never gated by trivial_mode)** |
| Phase 4 Execute — primary action | KEEP |
| Phase 4.04 Decision-Rule Counter | SKIP |
| Phase 4.05 Mid-Exec Drift + Chunked-Encoding | SKIP |
| Phase 4-post Outcome Classification (+ escape hatch) | KEEP |
| Phase 4-chain Episode Chain | SKIP |
| Phase 4.0 SKIP Fast-Path | KEEP (conditional — infra-unavailable only) |
| CREATE_BLOCKER | KEEP (conditional — failure only) |
| Phase 4.1 Guardrails + Error Response | KEEP — SAFETY, never skip |
| Phase 4.2 Domain Post-Execution | SKIP (already conditional) |
| Phase 4.25 Experience Archival | SKIP (trivial⇒routine⇒already skipped) |
| Phase 4.26 Utilization Feedback | SKIP (backstop applies --all-unknown) |
| Phase 4.27 Causal Enabler Scan | SKIP |
| Phase 4.28 Skill Co-Invocation | SKIP |
| Phase 4.5 Knowledge Reconciliation | SKIP (no_diff ⇒ nothing to reconcile) |
| Phase 4.6 Board Findings | SKIP (already conditional) |
| Phase 4.7 Full-Suite Recommender | SKIP (already file-change-gated) |

17 SKIP / 8 KEEP (2 conditional). The KEEP set is the irreducible spine: route
writes (Setup, Preconditions), keep the experiment seam intact (Step 5e), do the
work (primary), classify + escape-hatch (4-post), never skip safety (4.1) or the
conditional failure handlers (4.0, CREATE_BLOCKER). Asymmetric risk is the design
constraint: a false-negative costs only overhead; a false-positive skips learning
and is recoverable only via the escape hatch — so the classifier fails to full.

## Phase 4 Preamble: Cost-Ordered Precondition Checking

Before expensive data retrieval (SSH, large files, APIs), check local/cheap
preconditions first: timestamps, git log, file existence, metadata.
See: guard-009

### Pre-Claim Structured Precondition Re-Check

Catches the selector→claim race. The selector already filtered goals with
failing structured preconditions at COLLECT time, but state may have flipped
between selection and now (another agent completed a dependency, a pipeline
produced new output, an upstream process restarted, etc.). Re-evaluate the
goal's structured preconditions cheaply here — before any SSH / API / large
retrieval costs are incurred.

```
Bash: predicate-eval.sh --goal {goal.id} --types structured
parsed = JSON output from predicate-eval.sh
exit_code = $?

IF exit_code == 1:
    # At least one structured precondition failed.
    failed_ids = [r.predicate_id or r.type for r in parsed.results if not r.passed]
    Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} \
        defer_reason "precondition_unmet:{','.join(failed_ids)}"
    Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} \
        defer_reason_set_at "$(date +%Y-%m-%dT%H:%M:%S)"
    # --source is load-bearing here, not cosmetic symmetry with the two calls above:
    # aspirations-release.sh defaults SOURCE_VAL="world" (aspirations-release.sh:54), so a
    # sourceless release of an AGENT-queue goal releases against the WORLD queue and leaves the
    # agent-side claim held. The goal then reads as owned and every selector skips it until a
    # stranded-claim sweep frees it. Pinned by /verify-learning MAC14.
    Bash: ${ENV_PREFIX} aspirations-release.sh {goal.id} --source {source}   # release the claim
    Journal: "pre-claim precondition unmet for {goal.id}: {failed_ids}"
    GOTO Phase 7 (select next goal)
# Exit 0 = all passed (vacuous empty-list included). Exit 2 = id not found, warn and proceed.
```

**Distinct from CREATE_BLOCKER**: preconditions are *declared expected
dependencies*, blockers are *unexpected infrastructure failures*. A failed
precondition re-check defers the goal via `defer_reason` (auto-clears when
the predicate flips). It does NOT create a blocker, does NOT cascade-block
same-skill goals, does NOT notify the user.

### Phase 4-pre: Target-State Probe (advisory, gated) — lightweight: SKIP if trivial_mode

Before spending retrieval + skill-invocation tokens, cheaply grep the
target file(s) named in the goal description to check whether the
identifiers-to-implement are already present. No-op unless the goal's
`title + description` yields both a file path and an identifier.

Origin: rb-382 / g-115-141 (fix implemented by g-115-137 predated goal
filing; grep would have caught the already-done state at execution-time).
Sibling to the filing-time check in `goal-duplication-gate.py` — same
extractor (`core/scripts/_target_state.py`), different chokepoint.

```
Bash: bash core/scripts/target-state-probe.sh --goal-id {goal.id} --output json
parsed = JSON output
exit_code = $?   # always 0 unless the probe crashed or got no goal data

# Probe is ADVISORY. Phase 5 verification remains the ground truth —
# this only short-circuits retrieval when evidence says work is done.
# The probe writes NOTHING to disk: its stdout JSON is the single
# source of truth. The orchestrator journals the summary for audit.
IF parsed.probe.verdict == "already_present":
    Journal: "target-state-probe: " + parsed.summary
    # Downstream skill invocation MAY branch into verify-only semantics
    # based on the summary. Never auto-skip — Phase 5 verification
    # still runs and closes the goal if and only if state actually
    # matches the verification criteria.
IF parsed.probe.verdict in ("partially_present", "absent", "unknown"):
    # Proceed with normal execution. No journal entry — noise.
    pass
```

Guard: the probe is fail-open. A crash, a missing `aspirations-read.sh`,
unreadable target files, or an extraction yielding zero identifiers all
produce `verdict="unknown"` and a no-op — never a false block on real work.

## Phase 3.9: Pre-Execution Domain Steps — lightweight: trivial_mode runs ONLY the repo-sync step

```
Bash: load-conventions.sh pre-execution → IF path returned: Read it
# Procedural convention — gate on file EXISTENCE, not load status.
Bash: test -f "$WORLD_DIR/conventions/pre-execution.md"
IF exists AND NOT trivial_mode: follow each step; any step returning SKIP → mark goal skipped, GOTO Phase 7.
IF exists AND trivial_mode: run ONLY the repo-sync step (pre-execution.md
  "Pull Latest") when the goal touches a shared git checkout; skip the rest.
  # A trivial edit against a stale checkout is still a wrong edit
  # (user directive 2026-07-19: religious pull-before / push-after on
  # every shared checkout — read-only goals included).
```

## Phase 3.95: Depth Estimate — lightweight: SKIP if trivial_mode

Pre-commit to an expected depth tier so post-hoc rationalization can't drift the
classification. `reflect-on-outcome` logs mismatches to `meta/depth-calibration.jsonl`
so the next estimate can be better than the last.

```
estimated_depth =
  routine  — recurring AND verification is simple presence check (file/count/status)
  standard — not recurring AND category in last-50 histogram AND ≤2 checks
  deep     — unfamiliar category OR >2 checks OR cross-process OR research/design/investigate

estimated_seconds = midpoint of tier range (routine 30-120s, standard 120-600s, deep 600-3600s);
                    narrow if prior pattern-signature evidence exists.

Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} estimated_depth {tier}
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} {goal.id} estimated_seconds {sec}
```

## Phase 3.96: Anticipatory Reflection (Devil's Advocate) — lightweight: SKIP if trivial_mode

Before executing a DEEP goal, predict the 1–4 most likely ways it will fail and record
them — priming mitigation during execution AND producing an anticipation-accuracy signal
that calibrates over time (the sibling of Phase 3.95 depth-calibration). Design + schema:
`world/conventions/anticipated-failures.md` (g-306-22 / g-306-07). Engine (Phase B):
`core/scripts/anticipated-failures.py` + add/read/update wrappers. DORMANT by default —
Gate 2 flag off = zero behavior change (bravo g-306-30 ship condition).

```
# Gate 1: deep goals only. estimated_depth was set by Phase 3.95.
IF goal.estimated_depth != "deep": SKIP   # no anticipation for routine/standard

# Gate 2: feature flag — the single off-ramp. Read core/config/aspirations.yaml →
# anticipatory_reflection.enabled. Treat a MISSING key (or any read error) as false —
# fail-safe to dormant (bravo g-306-30 ship condition: a missing key must read false,
# never error). Env override GATE_3_96_ENABLED wins when set truthy.
IF anticipatory_reflection.enabled is not true (default false when key absent): SKIP   # dormant — the default on ship

# Ground the prediction in how this goal CLASS has failed before.
Bash: retrieve.sh --category "{goal.category} failure modes" --depth shallow
  → pattern_signatures[] + reasoning_bank[] describing prior failures in this category

# LLM step: name 1–4 of the MOST-LIKELY failure modes for THIS goal, grounded in:
#   - the goal's verification.checks (what would make each check fail)
#   - retrieved prior failures in this category (pattern_signatures / rb)
#   - the goal's blast radius (core-loop edits → regression in adjacent code, etc.)
# Each mode MUST be specific and falsifiable. "it might not work" is NOT a failure mode;
# "testSymmetry regresses because the zero-clamp is too aggressive" IS.
# Build a 1..4-element list; each element: {id:"af-N", mode, why,
#   signal (the observable that CONFIRMS this failure), mitigation (optional)}.
Bash: echo '<entry-json: goal_id, aspiration_id, category, estimated_depth:"deep", anticipated:[1..4 modes]>' | bash core/scripts/anticipated-failures-add.sh
Output: "Anticipated {N} failure mode(s) for {goal.id}: {short mode labels}"
```

## Phase 3.97: Inbound Signal Sweep (G6 / R10) — lightweight: SKIP if trivial_mode

Between goal selection and execution, inbound signals may have arrived —
board posts from partner agents or partner team-state shifts. Without an
explicit sweep, execution proceeds on stale assumptions.

Per `.claude/rules/retrieve-before-deciding.md` decision point 6 ("acting on
an inbound signal"). Note: the access-email skill handles email-class inbound
signals via its own G6/R10 retrieval (Inbound Signal Retrieval section); this
sweep covers the board + team-state surface that the executor sees.

```
# Cheap probes — signal-presence checks only. The 30m lookback covers
# typical claim→execute latency (goal-selector pick + Phase 4 retrieval).
# This is fixed-window for simplicity; board-read uses duration filters
# (`--since 30m`), not ISO timestamps.
inbound_signals = []

# 1. Coordination board posts since approximate claim time
Bash: posts=$(bash core/scripts/board-read.sh --channel coordination --since 30m --json 2>/dev/null || echo "")
IF posts is non-empty AND posts contains entries authored by the partner agent
   (NOT self — filter on `author != MIND_AGENT`):
    inbound_signals.append({"type": "board_post", "data": posts})

# 2. Partner agent team-state shift (claimed a goal that overlaps mine)
Bash: partner_in_flight=$(bash core/scripts/team-state-read.sh --field "agent_status" --json 2>/dev/null || echo "{}")
IF partner_in_flight contains an entry for any agent != self AND that
   entry's in_flight.goal_id matches this goal's category OR blocked_by chain:
    inbound_signals.append({"type": "partner_overlap", "data": partner_in_flight})

# If any signals fired, retrieve context BEFORE acting on them
IF inbound_signals is non-empty:
    # Build a query from the inbound signal content
    signal_summary = concatenate first 200 chars of each signal's body/text
    Bash: bash core/scripts/retrieve.sh --category "{goal.category} {signal_summary[:200]}" --depth shallow --read-only

    Use the returned JSON to decide:
      - If a signal indicates the goal should defer (e.g., partner claimed a
        prerequisite): defer with reason and exit to Phase 7
      - If a board post warns about an in-flight conflict: re-check the claim
        and consider release-and-reclaim

    Diary breadcrumb:
      echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"Inbound signal sweep: <count> signals, retrieved context"}' | bash core/scripts/execution-diary.sh append

# Fail-open: any of the probe scripts erroring is no-op. The sweep must not
# block execution. Phase 4 proceeds with whatever inbound_signals collected.
```

## Phase 4: Execute (with intelligent retrieval)

```
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} <goal-id> status in-progress
Bash: ${ENV_PREFIX} aspirations-update-goal.sh --source {source} <goal-id> started <today>

# ── Origin integrate at execute start (g-115-3262) ──────────────────
# Origin used to be integrated ONLY at iteration close, so a long iteration
# read and edited git-tracked code against a tree frozen since the previous
# close — staleness scaled with goal duration, which is backwards. Measured on
# cc-03 across 338 integrates (.git/iteration-push.log, 2026-08-01→08-10):
# 88.0% of inter-integrate gaps exceeded the 10-min fetch interval (median
# 31.1m, p90 56.0m), and the integrate then found the tree a median of 7
# commits behind (p90 15, max 145; 29.0% were >=10 behind). That staleness is
# exactly what guard-1759 / guard-1385 / rb-4641 / rb-4716 warn about
# downstream: a local read that misses origin yields a false "absent".
# --no-push is fetch+integrate ONLY, stopping before the push decision, so this
# ADDS an integrate point and leaves the merge-never-rebase posture untouched.
# A merge that meets a dirty tree or true conflict aborts cleanly and logs —
# it never overwrites, so this is safe ahead of execution.
# Cheap by construction: the stateless FETCH_HEAD-mtime throttle
# (FETCH_INTERVAL_MIN, default 10m) skips the fetch on most iterations, and a
# real fetch measured ~650ms on this repo. Same call shape the worker loop
# already uses at its Phase -0.3.
# EXPECT SILENCE. On a throttled or already-current iteration this prints
# nothing and changes nothing — that is the step working, not a dead step to
# drop (guard-1084). No Output: narration line by design (guard-874).
Bash: bash core/scripts/iteration-push.sh --no-push

# ── Unblock-intake probe (g-115-1017, rb-1111) ──────────────────────
# Fast intake-time probe: if this is an Unblock goal whose cited bug was
# fixed by an independent commit between filing and pickup, surface that
# signal BEFORE the heavy retrieval + execution pipeline. Canonical
# incident: g-115-985 (filed against loop-state-save.py:82, commit
# a49e4805 fix landed 12h before pickup; verify-and-close was the right
# path, not redundant deep-fix). Probe is ADVISORY — never blocks
# execution. Title-gated (skips non-Unblock goals), age-gated (skips
# fresh Unblocks), config-gated (unblock_intake_probe.enabled in
# core/config/aspirations.yaml).
Bash: ${ENV_PREFIX} bash core/scripts/unblock-intake-probe.sh --goal-id <goal.id> --source {source}
Parse JSON output from stdout:
  status = <probable-fix-landed | bug-still-present | inconclusive | skipped>
  recommendation = <verify-and-close | execute-normally>
  IF status == "probable-fix-landed":
      Output: "▸ UNBLOCK INTAKE PROBE: probable-fix-landed ({recommendation}; signals: {signals[:2]})"
      # The cited bug appears resolved already. Execution proceeds, but
      # verify-and-close is the expected path — DO NOT apply a redundant
      # fix. If verify confirms the resolution, mark complete with
      # outcome_note "verified-and-closed (intake-probe: probable-fix-landed)".
  ELIF status == "bug-still-present":
      Output: "▸ UNBLOCK INTAKE PROBE: bug-still-present ({signals[:2]})"
      # Proceed normally with deep-fix execution.
  ELIF status == "inconclusive":
      Output: "▸ UNBLOCK INTAKE PROBE: inconclusive ({skip_reason or 'no signal'}); proceed normally"
  # status == "skipped" → silent (gate-skipped; not signal-bearing)
# ── End Unblock-intake probe ────────────────────────────────────────

# ── Forged-Skill Surface (g-115-3811, sig-48) ───────────────────────
# UNCONDITIONAL — no trivial_mode skip, no config gate, no enable flag.
# guard-1516 states the defect verbatim: "Nothing in Phase 4 surfaces the
# forged registry automatically - retrieve.py does not index it and
# aspirations-execute never reads it - so this check is entirely manual."
# .claude/rules/forged-skill-resolution.md rule 2 says "never reason about
# whether a skill 'should' exist - check the registry", and the registry was
# never read here, so the rule had nothing to act on. Measured twice ~5 weeks
# apart by different agents, same double-miss shape (g-335-45 missed gap-019 +
# gap-017; g-335-448 missed gap-011 + gap-019) - a correct, retrievable rule
# that nothing READS at the moment of the action. That is sig-48 (7/7
# CONFIRMED), whose prescription is to build a reader at the action point, and
# whose strongest recorded evidence (g-335-409) is that the working shape is an
# UNCONDITIONAL step - because then nothing has to remember.
#
# Do NOT add a skip condition here. A condition is one more thing that can be
# wrong, and re-introduces the "something must decide to check" failure this
# step exists to remove. Cost is one small YAML read + 42 front-matter reads
# (~57ms) and ~2,800 characters of output.
#
# It prints the WHOLE registry, every time - there is no per-goal matcher and
# no goal argument. g-115-4446 scored five candidate matchers on a 30-goal
# hand-labelled sample (24 ground-truth pairs) on BOTH precision and recall;
# the shipped one had recall 0.00 (all 4 of its fires were false positives) and
# precision never exceeded 0.12 on ANY candidate, so a threshold could only
# move along a bad frontier. Reproduced independently on a second box
# (g-115-4475). An unconditional index has recall 1.00 by construction and
# nothing left to drift. Always exits 0 - it must never block execution.
Bash: ${ENV_PREFIX} py -3 core/scripts/forged-skill-surface.py
# Output is the full menu - 42 rows of "/skill-name — one line".
# SCAN IT for a skill that already does what this goal is about to do. Per
# forged-skill-resolution.md rule 1, invoke that skill (or its companion
# script) INSTEAD of hand-rolling the procedure inline. Most goals have no
# entry that applies, and that is the expected case - the index claims only
# EXISTENCE, never relevance, so passing over all 42 needs no justification.
IF a listed skill covers this goal's procedure:
    Output: "▸ FORGED-SKILL SURFACE: /<skill> already does this - invoking instead of hand-rolling"
# ── End Forged-Skill Surface ────────────────────────────────────────

# ── Encode-Stable-Facts Gate (G17) ─────────────────────────────────
# Before resource-access steps (SSH, AWS CLI, describe-*, list-*, find),
# enforce the three-probe threshold from .claude/rules/encode-stable-facts.md.
# When the goal's primary_action or description references a discoverable
# external resource (shared filesystem path, service endpoint, account
# ID, remote storage location), check whether a locator already exists BEFORE
# issuing discovery probes. The gate is called inline each time the
# agent is about to issue the Nth discovery command for a single resource.
#
# Contract: --resource-id <canonical id> --probe-count <int> [--override "<text>"]
# Exit 0 = pass (below threshold, locator found, or override). Exit 1 = block.
# When blocked: STOP probing, encode the discovered value as a locator in
# world/conventions/ before continuing. See core/config/conventions/resource-locators.md.
#
# Invocation (before each discovery probe for a given resource):
#   Bash: bash core/scripts/encode-stable-facts-gate.sh \
#            --resource-id "<resource identifier>" \
#            --probe-count <N>
#   IF exit 1: encode the value discovered so far as a locator, then
#              read the locator instead of probing further.
#
# Fail-open: gate errors (missing world dir, script crash) exit 0 —
# never blocks execution. The gate is advisory-loud, not hard-block.
# ── End Encode-Stable-Facts Gate ───────────────────────────────────

# ── Intelligent Retrieval Protocol ──────────────────────────────────
# LIGHTWEIGHT MODE (g-305-15): IF trivial_mode, SKIP this entire block (the
# digest's Retrieval Steps 1-5d) — `no_retrieval_call` guarantees retrieval adds
# nothing. Step 5e below STILL runs. IF NOT trivial_mode, proceed:
# Full pseudocode lives in the execute-protocol digest. Load on-demand:
Bash: load-execute-protocol.sh → IF path returned: Read it
# The digest covers Steps 1-5c (tree summary → primary nodes → supplementary
# → memory deliberation → codebase/web escalation → deliberation-on-hypothesis
# → retrieval influence articulation). Follow the digest's Retrieval Protocol
# section inline; the SKILL.md no longer duplicates it.
# Key side effects to remember:
#   - retrieve.sh --goal auto-writes retrieval-session.json (utilization tracking)
#   - iteration-close.sh do_state_update runs the utilization repair before the
#     Phase 4.26 gate, so feedback lands even if Phase 4.26 skips (g-115-3123)
#   - Step 4b (strategy-apply.sh) closes the meta-strategy → execution loop
#   - Step 5b.1 persists deliberation onto the linked hypothesis record when present
# ── End Intelligent Retrieval ───────────────────────────────────────

# ════ Step 5e ALWAYS RUNS — NEVER gated by trivial_mode (lightweight mode, ═══
# g-305-15 / brief §6). The lightweight retrieval-skip is scoped to Steps 1-5d
# ONLY. GATE-INTEGRITY: the experiment seam stays byte-identical on BOTH the
# trivial and full paths; the classifier never reads/infers/branches on Gate D
# state, and no skip guard wraps this block.
# ── Step 5e: Gate D commons-pattern injection (DORMANT — DEFAULT OFF) ──────
# Gate D experiment seam (methodology §4.2, RATIFIED 2026-06-10; R1-R9 binding).
# A/B test: do agents complete goals better when cross-world commons patterns
# are injected at execution time? Arm A = control (no-op); arm B = top-K=5
# commons-pattern injection. SINGLE-BLIND: the executing context reads the
# patterns under a NEUTRAL heading with NO arm/experiment/commons labels (label
# leakage would bias effort). DEFAULT OFF — gate-d-check.sh returns "off" until
# omni flips GATE_D_ENABLED (omni-only; GATE-INTEGRITY 9.5 — agents MUST NOT set
# it, and MUST NOT modify the assignment/flag logic after omni blesses the diff).
# SEAM-1: arm A is a COMPLETE no-op — no injection, no context-budget reservation.
Bash: gate_d_status = bash core/scripts/gate-d-check.sh
IF gate_d_status == "on":
    # Assign + retrieve in one call. goal-text caps description at 500 chars.
    # _gate_d.py reads GATE_D_CORPUS_PATH from env; empty/missing corpus → no_patterns.
    Bash: gate_d_json = bash core/scripts/gate-d-inject.sh \
            --goal-id "{goal.id}" \
            --goal-text "{goal.title}. {goal.description[:500]}" \
            --category "{goal.category}"
    Parse gate_d_json → arm, status, assignment_hash, patterns, patterns_injected,
          pattern_signatures, injection_tokens, retrieval_precision, corpus_size, corpus_source

    # SINGLE-BLIND INJECTION — arm B + status "injected" ONLY. Emit each pattern
    # under a NEUTRAL heading. NO "arm", "Gate D", "experiment", "commons", or
    # "B-arm" token may appear in what the executing context reads — the patterns
    # must be indistinguishable from ordinary supplementary retrieval.
    IF arm == "B" AND status == "injected":
        FOR index, p IN enumerate(patterns, start=1):
            Output (into execution context, before goal.skill runs):
              "-- SUPPLEMENTARY REFERENCE PATTERN ({index}/{patterns_injected}) --"
              "Context:  {p.context}"
              "Approach: {p.approach}"
              "Lesson:   {p.lesson}"
    # arm == "A" (control) OR status in (no_patterns | error): COMPLETE no-op —
    # nothing injected; execution proceeds identically to a Gate-D-off run.

    # ASSIGNMENT telemetry (methodology §4.6) — written BEFORE execution, ONE line,
    # strictly append-only, per-agent. R1: the ayoai-side agent env var is
    # MIND_AGENT (the methodology's $MIND_AGENT is the zds-side name); resolve via
    # $MIND_AGENT. world resolves from $GATE_D_WORLD (settings.json). excluded=true
    # (E7) when status==error. R5: the OUTCOME record (iteration-close do_verify)
    # joins on (agent, goal_id), so goal_id MUST match exactly.
    estimated_depth = "deep" if goal is substantive else "routine"   # advisory heuristic
    assignment_record = {
        "record_type": "assignment", "goal_id": goal.id,
        "aspiration_id": goal.aspiration_id, "agent": "$MIND_AGENT",
        "world": "$GATE_D_WORLD", "arm": arm, "assignment_hash": assignment_hash,
        "injection_status": status, "patterns_injected": patterns_injected,
        "pattern_signatures": pattern_signatures, "injection_tokens": injection_tokens,
        "retrieval_precision": retrieval_precision, "goal_category": goal.category,
        "estimated_depth": estimated_depth, "excluded": (status == "error"),
        "corpus_source": corpus_source, "corpus_size": corpus_size,
        "experiment_version": "gate-d-v1", "timestamp": "$(date +%Y-%m-%dT%H:%M:%S)"
    }
    Bash: append the one-line assignment_record JSON to
          agents/$MIND_AGENT/session/gate-d-telemetry.jsonl (append-only; the
          session/ dir already exists, so no L1 new-top-level-entry concern).
    # Diary breadcrumb (single-blind: NO status/arm — "status=injected" would
    # unblind arm B to a post-compaction reader of this diary [omni bless
    # amendment 2026-06-11]; the marker exists only so a recovered session knows
    # Step 5e already ran for this goal and MUST NOT re-run it — a re-run would
    # append a duplicate ASSIGNMENT record):
    Bash: echo '{"entry_type":"observation","goal_id":"{goal.id}","content":"step-5e context preparation complete"}' | bash core/scripts/execution-diary.sh append
# IF gate_d_status == "off" (the DEFAULT): skip Step 5e entirely — zero overhead,
# no telemetry, execution identical to pre-Gate-D behavior.
# ── End Step 5e ───────────────────────────────────────────────────────────

# Team-Based Research Delegation (optional — tool, not rule)
#
# The host MAY dispatch read-only team agents via TeamCreate + Agent
# (team_name={team}, run_in_background=true) to pre-fetch research. Full
# protocol + context-injection: core/config/conventions/agent-spawning.md.
#
# Team agents do READ-ONLY research. They MUST NOT invoke skills, write/edit
# files, call state-mutating scripts, or make git commits. Use
# build-agent-context.sh to inject primed context at spawn time; register via
# pending-agents.sh BEFORE dispatch (crash-safe staleness timeout).
#
# Curriculum gate: curriculum-contract-check.sh --action allow_multi_goal_parallelism
#   exit 0 → dispatch allowed; exit 1 → sync research instead.
# Prompt MUST specify: 10-minute hard limit (stop new work at 8m), read-only
# constraint, structured-findings report format.

# Misroute guard: catch skill-creation goals that arrived with skill: null
IF goal.skill is null AND goal.title matches (forge|create.*skill|make.*skill|skill.*creation):
    goal.skill = "/forge-skill"; goal.args = "list"

# Capture start time so Phase 4.05 can detect mid-execution drift
phase_4_started_at = "$(date +%s)"

# Pre-apply consult gate (g-115-826, rb-987 / g-115-796 incident;
# WIDENED by g-115-2201 on 2026-07-14):
# When this goal's title or description references a framework-file path
# (core/, .claude/, world/conventions/, core/config/, SKILL.md, CLAUDE.md),
# emit an advisory-loud directive BEFORE the first Edit to surface
# guardrails/reasoning-bank entries that may contradict the spec.
# Triggers on ANY such goal — OWN-AUTHORED included. handoff_from is NOT a
# trigger; it is an ESCALATOR that makes the banner louder on an inherited
# spec (extra rb-987 hazard: a test suite pinning the spec pins its violation
# too). Silent when retrieval is ALREADY recorded for the goal — a gate that
# fires when satisfied is one the agent learns to ignore. Fail-open on
# env/path errors. The gate exits 0 unconditionally — it shifts the
# consult-before-edit discipline from after-the-fact learning-gate audit to
# before-the-fact directive but does not block the loop.
# SSOT for the predicate is the docstring in core/scripts/pre-apply-consult-gate.py.
# This comment kept the PRE-widening predicate for 17 days after the code moved,
# and a reader believed it over the gate: g-115-4358 was filed HIGH to widen an
# already-widened gate. Re-sync here whenever that docstring changes.
Bash: bash core/scripts/pre-apply-consult-gate.sh <goal.id>

# Execute primary goal inline (host does ALL writing)
result = invoke goal.skill with goal.args

# ── Inner Refinement (Self-Refine, g-306-10 / BRD Gap 4) ───────────────────
# OPTIONAL same-LLM generate -> critique -> regenerate loop on the artifact
# `result` just produced. Gated on goal.inner_refinement; ABSENT or null = OFF,
# so goals without the block behave EXACTLY as before (no extra passes, no cost).
# The pass count is clamped to INNER_REFINEMENT_MAX_ITERS_CAP (=5, defined in
# core/scripts/aspirations.py) -- this clamp is the EXECUTION-SIDE termination
# guarantee: it holds even if the goal reached the queue via the daemon write
# path, whose _validate_goal does NOT range-check max_iters (guard-547 split).
# Schema + worked example: core/config/conventions/goal-schemas.md "Inner Refinement".
IF goal.inner_refinement is not null:
    ir = goal.inner_refinement
    cap = INNER_REFINEMENT_MAX_ITERS_CAP        # = 5; mirrors the aspirations.py constant
    max_passes = min(int(ir.max_iters), cap)    # defensive clamp -- never trust stored max_iters past cap
    satisficed_when = ir.satisficed_when        # non-empty stop predicate (CLI-validated)
    outcomes = (goal.verification.outcomes if goal.verification else []) or []
    Output: "▸ INNER-REFINEMENT: {max_passes}-pass cap, stop-when=\"{satisficed_when}\""
    FOR pass_n in 1..max_passes:
        # CRITIQUE: the SAME LLM critiques `result` against the goal's
        # verification.outcomes (+ checks where present). No external grader --
        # self-feedback per Self-Refine 2303.17651.
        critique = LLM critique of `result`:
            "List each verification outcome this artifact does NOT yet satisfy,
             with the specific gap. If all outcomes are satisfied AND
             satisficed_when ({satisficed_when}) is met, reply exactly SATISFICED."
        IF critique == "SATISFICED":
            Output: "▸ INNER-REFINEMENT: satisficed at pass {pass_n}/{max_passes} -- stopping"
            BREAK
        # REGENERATE: revise `result` to close ONLY the named gaps (no scope
        # creep -- implementation-discipline). Carry the improved draft forward.
        result = LLM regenerate `result` addressing each gap named in `critique`
        Output: "▸ INNER-REFINEMENT: pass {pass_n}/{max_passes} regenerated"
    # Loop exits on SATISFICED or after max_passes (the clamp) -- ALWAYS terminates.
    # If the primary action already persisted the artifact at first-draft time,
    # re-persist the final refined `result` so Phase 5 verify reads the improved
    # version, not the pre-refinement draft.
# ── End Inner Refinement ───────────────────────────────────────────────────
```

## Phase 4.04: Decision-Rule Application Counter (E8) — lightweight: SKIP if trivial_mode

Rationale (WHY rule counters must be encoded and why idempotent-per-call): `core/config/rationale/aspirations-execute.md`

```
# Self-report: which Decision Rules in the retrieval manifest informed
# this execution? "Informed" means the agent reasoned about the rule
# BEFORE deciding the next step — not "the rule was visible in
# retrieval output." The encoded counter reflects ACTIONS taken on
# rules, not READS of them.

cited_rules_by_node = {}  # {node.file: ["IF X THEN Y", ...], ...}
For each tree node in the retrieval manifest:
    For each Decision Rule the agent CITED as informing execution:
        cited_rules_by_node.setdefault(node.file, []).append(rule_text)

IF cited_rules_by_node is non-empty:
    For each node_file, rule_list in cited_rules_by_node.items():
        Bash: echo '{"rules": <rule_list as JSON array>}' \
              | bash core/scripts/decision-rules-increment.sh \
                  --node-path <node_file>
        # Fail-open: a non-zero exit means no rule matched (the agent's
        # self-report misquoted the rule body). Log to execution-diary
        # but never block Phase 4.05.

# Skip entirely IF the agent did not cite any rule. Empty self-report is
# legitimate — many goals are mechanical and run without rule citation.
```

## Phase 4.05: Mid-Execution Drift Check (G4 / R11) — lightweight: SKIP if trivial_mode

Rationale (WHY drift-check fires for long executions and why the snapshot ages out): `core/config/rationale/aspirations-execute.md`

```
phase_4_duration_sec = $(($(date +%s) - phase_4_started_at))
result_size_chars = len(result.text if result.text else "")

IF phase_4_duration_sec > 1800 OR result_size_chars > 4000:
    # Re-retrieve at the same depth as Phase 4's initial snapshot
    # Use goal.category as the query — the retrieval that informed Phase 4
    Bash: retrieve.sh --category "{goal.category}" --depth shallow --read-only

    From the returned JSON, compare to Phase 4's snapshot:
      - New reasoning_bank entries added since phase_4_started_at? (check `created` field)
      - New guardrails added that constrain remaining cognitive phases?
      - Tree nodes touched since phase_4_started_at? (check `last_updated`)

    IF any drift detected:
      Log: "MID-EXECUTION DRIFT: {phase_4_duration_sec}s elapsed, {N} new entries"
      Pass the drift summary into Phase 4.1's guardrail consultation context
      so guardrail evaluation uses the fresh set, not the stale snapshot.

      Diary breadcrumb:
        echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"Mid-exec drift: <N> new RB/G entries detected"}' | bash core/scripts/execution-diary.sh append

    ELSE:
      # No drift — proceed with Phase 4's snapshot for downstream phases
      pass

ELSE:
    # Execution was short enough that drift is unlikely. Skip.
    pass
```

Fail-open: if retrieve.sh errors, log and proceed with Phase 4's snapshot.
The drift check must not block post-execution phases.

### Phase 4.05 Chunked-Encoding Producer (E13)

Rationale (WHY long results are chunked instead of bundled, and the producer/consumer split): `core/config/rationale/aspirations-execute.md`

```
IF phase_4_duration_sec > 1800 OR result_size_chars > 4000:
    # Continue past the drift check above. Add CHUNKING below.

    # Segment result.text by natural boundaries:
    #   - ### Markdown headings
    #   - distinct tool-output blocks (e.g., "▸" prefixes)
    #   - paragraph breaks where topic shifts
    # Cap at 5 chunks (above that, the encoding-pass cost > value).
    # Skip if result.text has no natural splits (single coherent finding).

    chunks = split_result_by_boundaries(result.text, max_chunks=5)
    IF len(chunks) <= 1: SKIP rest of chunked-encoding producer
                          (one bundled payload is fine)

    For each chunk_idx, chunk_text in enumerate(chunks):
        # Score this chunk independently per Section A of the digest
        scores = {
          "novelty":            <agent self-rates 0-1>,
          "outcome_impact":     <agent self-rates 0-1>,
          "surprise":           <agent self-rates 0-1>,
          "goal_relevance":     <agent self-rates 0-1>,
          "repetition_strength": <agent self-rates 0-1>
        }
        # Classify content_type by chunk inspection
        content_type = "finding" | "decision" | "code-change" | "observation"
        # Identify target node (or null) by topic
        target_article = <node_key from tree-find-node, or null>

        Bash: echo '{
          "source_goal": "<goal.id>",
          "chunk_idx": <idx>,
          "chunk_total": <len(chunks)>,
          "chunk_text": "<chunk_text[:2000]>",
          "content_type": "<type>",
          "scores": <scores>,
          "target_article": <key or null>,
          "replay_priority": "<replay-priority>"
        }' | bash core/scripts/wm-append.sh sensory_buffer

    Bash: echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"E13 chunked encoding: <N> chunks emitted from <result_size> chars"}' | bash core/scripts/execution-diary.sh append
```

Fail-open: chunk-append errors log to execution-diary but never block
Phase 4-post. Phase 8 consumes whatever chunks landed.

## Phase 4-post: Outcome Classification

Binary: routine (recurring + no findings) or deep (everything else). Gates
post-execution cognitive phases; does NOT affect execution itself.

```
outcome_class = "deep"   # default
IF goal.recurring AND goal_succeeded AND result produced no actionable items
   AND no new information:
    outcome_class = "routine"

# SEMANTIC OVERRIDE — knowledge-debt closure (rb-245):
# A routine goal that clears a declared knowledge_debt entry OR freshly
# touches the tree node pointed at by goal.closes_knowledge_debt becomes
# "deep". Without this override, debt-closing goals silently skip encoding.
IF outcome_class == "routine" AND goal.closes_knowledge_debt is non-empty:
    Check wm-read.sh knowledge_debt + tree node last_updated == today.
    IF debt_cleared OR node_touched_today: outcome_class = "deep"

# SAFETY: Non-recurring, failed, recurring-with-findings, or debt-closing
# goals ALWAYS remain "deep". Bias toward full treatment on any uncertainty.

# ESCAPE HATCH (lightweight mode — g-305-15 / brief §7): the trivial classifier
# was a PREDICTION. If execution falsified it, re-enable the full post-execution
# ceremony so no learning is lost — converting a dangerous false-positive into a
# recoverable one. Runs HERE, before the Phase 4.25/4.26/4.27/4.5 SKIP guards
# read trivial_mode. (No-op when trivial_mode is already false — the common path.)
IF trivial_mode:
    Bash: git -C {repo} diff --stat   # the repo(s) this goal could have touched
    IF the goal produced a non-empty diff, OR a surprise fired, OR the goal failed:
        trivial_mode = false
        outcome_class = "deep"   # a falsified trivial prediction is deep by definition
        Output: "▸ Lightweight mode: ESCAPE HATCH fired (diff/surprise/failure) — reverting to FULL; the SKIP phases (4.25/4.27/4.5) will run"
        Bash: loop-state-save.sh update --set "phase_progress.trivial_mode=false"
```

## Phase 4-chain: Episode Chain Protocol (MR-Search) — lightweight: SKIP if trivial_mode

After Phase 4-post outcome classification, before proceeding to Phase 4.0/4.1,
check if this goal should be retried with accumulated reflection context.
Inspired by MR-Search (arXiv 2603.11327): chaining N attempts with structured
self-reflection between each episode enables the agent to learn *through*
failure within the same problem context.

Full pseudocode (chain_trigger determination, context-zone-overridden
max_episodes, structured mini-reflection schema, goal-state rewiring for
re-execution, and breadcrumb emission) lives in its own digest. Load
on-demand when a deep-outcome failure is the classification:

```
IF outcome_class == "deep" AND NOT goal_succeeded AND result NOT in (INFRASTRUCTURE_UNAVAILABLE, RESOURCE_BLOCKED):
    Bash: load-episode-chain-protocol.sh → IF path returned: Read it
    Follow the digest's chain_trigger + max_episodes + reflection steps inline.
ELSE:
    # No chaining — clear any stale episode_chain WM slot if it matches this goal.id
    Bash: wm-read.sh episode_chain --json
    IF exists AND episode_chain.goal_id == goal.id: echo 'null' | Bash: wm-set.sh episode_chain
```

GUARD: Never chain infrastructure failures — Phase 4.0 owns the blocker protocol.

## Phase 4.0: Structured SKIP Fast-Path (with Recovery Attempt)

Skills that SKIP at preflight return INFRASTRUCTURE_UNAVAILABLE or
RESOURCE_BLOCKED. `skip-fastpath-eval.sh` maps `goal.skill` to its
component via `agents/<agent>/infra-health.yaml`, runs ONE recovery probe via
`infra-health check`, and returns the decision.

```
Bash: bash core/scripts/skip-fastpath-eval.sh \
         --goal-skill {goal.skill} --skip-result {result} \
         [--retry-attempted]
Read JSON result:
  next_action = "RETRY"          → re-execute skill → return to Phase 4.0
  next_action = "PROVISION"      → invoke probe_data.provision_skill →
                                   branch on same_skill_recovery:
                                     same-skill + success → result = provision_result → Phase 5
                                     cross-skill + success → retry_attempted = true → re-execute
                                     fail → follow CREATE_BLOCKER protocol
  next_action = "CREATE_BLOCKER" → follow CREATE_BLOCKER protocol below
  next_action = "NO_COMPONENT"   → no skill_mapping entry; CREATE_BLOCKER
```

The script enforces the verify-before-assuming multi-signal requirement
internally (`signal_count` and `flags` fields). When it returns
CREATE_BLOCKER with `signal_count < 2`, add an alternative probe
(different tool / endpoint) before proceeding.

Execution-diary breadcrumbs (`decision`, `observation`, `failure` entry
types) are written at each branch — they surface in postcompact-restore
for debugging.

Execution-diary breadcrumbs (`decision`, `observation`, `failure` entry types)
are written at each branch — they surface in postcompact-restore for debugging.

## CREATE_BLOCKER Protocol

Single source of truth for blocker creation. Invoked by Phase 4.0 (fast-path
SKIP), Phase 4.1e (unfixable infrastructure failure), and Phase 0.5b
(pre-selection sweep).

Orchestrated by `create-blocker.sh`, which runs (in order) wm-read dedup →
blocker-create-gate → conclusion-record → capability-gate →
aspirations-add-goal (unblocking goal) → wm-set(known_blockers). The LLM
still handles notification (forged-skill call) and the journal entry —
those actions are emitted in the script's `next_steps_for_llm` array.

```
Bash: bash core/scripts/create-blocker.sh \
         --failure-skill <skill> --failure-reason "<reason>" \
         --goal-id <id> --aspiration-id <asp-id> \
         --evidence '[...]' --probe-command "<exact>" \
         [--infra-health-check '{...}'] \
         [--schema-probe-evidence '{...}'] \
         --diagnostic-context '{...}' \
         --intended-participants agent
Read JSON: act on flags; for each entry in next_steps_for_llm, perform
the LLM-level action (notify user / journal entry).
```

Why two gates are non-negotiable: `blocker-create-gate.py` rejects four
structural failure modes — synthetic probe, single-signal negation,
statistical-without-schema-probe, infrastructure-without-health-check.
`capability-gate.py` matches `failure_reason` against agent-provisionable
capabilities to prevent unjustified `participants:[user]` routing.
Skipping either has historically produced false-positive blockers that put
the agent to sleep on non-problems.

Cross-references: `.claude/rules/capability-before-user.md`,
`.claude/rules/verify-before-assuming.md`,
`.claude/rules/probe-with-canonical-code-path.md`, `rb-226/246/258/245`.

## Phase 4.1: Post-Execution Guardrail Consultation + Error Response

After goal execution, consult learned guardrails and reasoning bank for
checks relevant to this goal's outcome. This is how the agent applies
lessons from experience — the specific checks are learned behaviors stored
in world/ (guardrails, reasoning bank), not hardcoded here.

For infrastructure goals, this enables learned behaviors to fire even when
the goal appeared to succeed. A "successful" goal can mask real infrastructure
errors that only guardrails know to check for.

Phase 4.1 does NOT fire guardrail checks on local/tooling errors: script
validation rejections, file not found in world/ or agent dir, build/compile errors during
code editing, or git failures.

Full pseudocode (guardrail-check for infrastructure + testing contexts,
error-alert cascade detection, severity classification, inline-fix attempt,
fallback to CREATE_BLOCKER) lives in `core/config/execute-protocol-digest.md`
under "Phase 4.1: Post-Execution Guardrails + Error Response". Load
on-demand; the summary below captures the control flow.

```
goal_succeeded = (result achieved verification.outcomes AND no errors/timeouts)
involved_infrastructure = (goal.skill in agents/<agent>/infra-health.yaml skill_mapping
                           OR goal.category in category_mapping)
involved_testing = (goal.category contains "test" OR goal.title contains "test"/"verify")

# Consultation: run guardrail-check.sh for each applicable context (infrastructure
# and/or testing). Execute every matched guardrail's action_hint. If output
# reveals issues (non-empty error alerts, health-check failures, testing-rule
# violations): guardrail_found_issues = true.

# Error-response protocol fires when: guardrail_found_issues OR (failed AND infrastructure).
IF guardrail_found_issues OR (NOT goal_succeeded AND involved_infrastructure):
    # 4.1a SEEK ERROR ALERTS: sleep 45 to catch async delivery (failure-only, skip
    #      if guardrails already confirmed). Read via infra-health.yaml error_check.
    # 4.1b CASCADE DETECTION: sort alerts by timestamp; earliest = root cause;
    #      build cascade_report {root_cause, cascade_effects, agent_observed_symptom, chain_summary}.
    # 4.1c SEVERITY: alerts present → "confirmed_infrastructure";
    #      structured failure markers → "explicit_failure"; else "soft_failure" (no block).
    # 4.1d TRY FIX INLINE: search tree/reasoning-bank/experience; ONE attempt, no loops.
    #      Success → log + optional Investigate/Idea goals (via cognitive-primitives digest).
    # 4.1e COULDN'T FIX → load-create-blocker-protocol.sh and invoke the protocol
    #      with diagnostic_context = {error_alerts count, cascade_chain, attempted_fix}.

    # Exit paths (do NOT merge — different semantics):
    #   NOT goal_succeeded: revert to status pending; continue (skip Phases 4.25-9).
    #   goal_succeeded + guardrail issue: fall through to Phase 4.25+ (goal completes).

# SAFETY: Guardrail findings override routine classification.
# Phase 4-post classified before guardrails ran — if guardrails found
# real issues, this IS new information regardless of skill result.
IF guardrail_found_issues:
    outcome_class = "deep"  # guardrail issues → override to deep

# ── Anticipation consumer (g-306-22 Phase C / design §4) — closes the Phase 3.96 loop ──
# REUSES the error signal computed above (guardrail consultation + infra error alerts +
# test failures) — NO new error-collection. No-op when no entry exists (Gate 2 flag was
# off, or the goal was not deep), so it is dormant exactly when Phase 3.96 is.
Bash: af_entry=$(bash core/scripts/anticipated-failures-read.sh {goal.id})   # record JSON, or "null"
IF af_entry != "null" AND af_entry.outcome is null:
    errors_observed = the actual error signatures from THIS execution's error-response
                      path above (empty list when the goal ran clean)
    FOR each anticipated mode in af_entry.anticipated:
        HIT  if its `signal` matches an observed error (substring / LLM-judgment, NOT ==)
        MISS otherwise
    surprises     = observed errors matching NO anticipated mode
    clean_success = (errors_observed is empty)
    # anticipation_score is computed by Phase D (reflect-on-self §5), NOT here — emit 0.0.
    Bash: echo '<outcome-json: executed_at, errors_observed[...], hits[af-N...], misses[af-N...], surprises[...], anticipation_score:0.0, clean_success>' | bash core/scripts/anticipated-failures-update.sh {goal.id}
    Output: "Anticipation {goal.id}: {H} hit / {M} miss / {S} surprise"
```

## Phase 4.2: Post-Execution Domain Steps — lightweight: SKIP if trivial_mode (already conditional)

```
# Load domain convention into context if not yet loaded (dedup).
Bash: paths=$(bash core/scripts/load-conventions.sh post-execution 2>/dev/null)
IF paths is non-empty:
    Read the file at the returned path

# Follow domain post-execution steps if convention exists.
# CRITICAL: Gate on file existence, NOT on load status. The convention is procedural —
# it must run every goal, not just the first time it's loaded into context.
Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/post-execution.md" && echo "exists"
IF exists:
    Follow each Step in the convention, evaluating conditions against current goal context
    Collect results (external_changes, behavioral_observations)
    Pass collected results to Phase 4.5 for knowledge reconciliation
ELSE:
    # No domain post-execution convention exists (fresh agent). Nothing to do.
    external_changes = null
    behavioral_observations = null
```

## Phase 4.25: Archive Goal Execution Trace — lightweight: SKIP if trivial_mode (trivial⇒routine⇒already skipped)

SKIP if outcome_class == "routine". Otherwise:

**NOTE (rb-428 compliance gate)**: If you skip this step, `experience-staleness-check.sh`
will set `force_experience_archival` in working memory. The next iteration's precheck
Phase 0-pre2 (`aspirations-precheck/SKILL.md`) reads that sentinel and BLOCKS goal
selection until you compose the record retroactively. Drift is self-correcting within
one iteration — but composing it now, while context is fresh, avoids the retro-compose
tax.


```
# Bash-enforced write path (rb-428 / guard-365). The wrapper does the
# .md placement, JSONL append, schema validate, dedup, meta refresh,
# and emits stderr WARN on short trace (<200B) or short summary (<20
# chars) as drift signals. LLM residue: the reasoning trace content.

Write a reasoning-trace .md file to any path <trace-path>. Include tool outputs,
decisions, outcome, verification, surprises, and verbatim anchors (exact
technical values — error codes, limits, timeouts, paths+lines+commits,
latencies, API responses).

echo '<stdin-json>' | bash core/scripts/experience-archive-goal.sh \
  --goal {goal.id} --skill-slug {goal.skill_name_slug} \
  --category {goal.category} --summary "<one-line summary>" \
  --trace-file <trace-path>
# <stdin-json> may include any subset of:
#   verbatim_anchors [{key, content}], tree_nodes_related[],
#   retrieval_audit{manifest_present, nodes_count, active_count, skipped_count,
#     utilization_fired, influence}, enabled_by[] (filled by 4.27 — leave []
#     here), reasoning_chain[], hypothesis_id, temporal_credit.
# The wrapper moves <trace-path> to canonical
# agents/<agent>/experience/exp-{goal.id}-{goal.skill_name_slug}.md.

echo '{"experience_refs":["exp-{goal.id}-{goal.skill_name_slug}"]}' | Bash: wm-set.sh active_context.experience_refs
```

## Phase 4.26: Context Utilization Feedback — lightweight: SKIP if trivial_mode (backstop applies --all-unknown)

```
IF outcome_class != "routine":
    helpful_items = items that met structural helpfulness (referenced in
      execution commands/decisions/output OR matched in Phase 4.1)
    Bash: utilization-feedback.sh --goal {goal.id} --helpful "{comma-separated IDs}"
ELSE:
    Bash: utilization-feedback.sh --goal {goal.id} --all-noise
```

BACKSTOP (hot path): `iteration-close.sh`'s `_repair_utilization_pending` runs
inside `do_state_update` immediately BEFORE `phase-4-26-gate.sh`, applying
`--infer --confidence balanced` (falling back to `--all-unknown` on schema<2)
when this phase left `utilization_pending=true`. `do_learning_gate` calls the
same helper again as a no-op backstop for crash-resume paths.

BACKSTOP (direct-skill path only): the `utilization-gate.sh` PreToolUse[Skill]
hook covers `Skill(aspirations-state-update)` invocations that bypass
iteration-close. It does NOT cover the Bash hot path — a PreToolUse[Skill]
matcher structurally cannot (g-115-3123). (Pre-2026-05-07 both backstops used
`--all-noise`, which silently poisoned times_noise on unattested-but-relevant
nodes — see audit notes in utilization-gate.sh.)

phase-4-26-gate still blocks goal completion when only a backstop ran, forcing
the LLM to attest or pass `--no-retrieval-applicable` — but note that gate is
itself currently inert at its line 108 (g-115-3113, arming deferred).

MR-Search reflection-quality tracking: helpful items with `source_reflection_id`
write positive downstream signal to `meta/reflection-strategy.yaml →
reflection_quality_log`.

## Phase 4.27: Causal Enabler Scan (MR-Search Temporal Credit) — lightweight: SKIP if trivial_mode

```
IF outcome_class != "routine" AND goal_succeeded:
    FOR EACH active item from retrieval_manifest that met structural helpfulness:
        item_source_goal = item.source_goal or item.source
        IF item_source_goal:
            goals_between = count goals completed between then and now
            Bash: experience-update-field.sh exp-{goal.id}-* enabled_by \
                '<append {experience_id: "exp-{item_source_goal}", relationship: "provided_foundation", temporal_distance: goals_between}>'
```

## Phase 4.28: Skill Co-Invocation Logging — lightweight: SKIP if trivial_mode

```
invoked_skills = [goal.skill stripped of "/" and params]
Append any auxiliaries invoked during execution (decompose, tree, research-topic, etc.)
IF len(invoked_skills) >= 2:
    Bash: skill-relations.sh co-invoke --goal {goal.id} --skills {comma_separated}
```

## Phase 4.5: Knowledge Reconciliation Check — lightweight: SKIP if trivial_mode (no_diff ⇒ nothing to reconcile)

After executing a goal, check if the knowledge that informed it needs updating.
This closes the loop: knowledge -> action -> knowledge update.

### Cooperative Stop-Check (runs first)

If `/stop` fired during primary execution, commit/push (Phase 4.2) has already
completed — no uncommitted changes at risk. Reconciliation is defer-safe:
stale nodes get caught by the next session's reconciliation pass or by
`/reflect --curate-memory`. Skip this phase so the iteration reaches
Phase 8-stop faster. `iteration-close.sh --phase verify` and `--phase
state-update` still run afterwards in the orchestrator — they are the
mandatory obligations, they are fast, and Phase -1.4 of the next iteration
then invokes graceful-stop for clean finalization.

INVARIANT (do not reorder): this check must stay ABOVE the Reconciliation
block. The whole point is to skip reconciliation under stop. Do NOT also
skip Phase 4.6 — board post is one shell call and is worth keeping.

```
Bash: `session-signal-exists.sh stop-requested`
stop_pending = (exit 0)
IF stop_pending:
    Bash: echo "COOP_STOP: Phase 4.5 reconciliation skipped — /stop pending; verify + state-update still run via iteration-close"
    echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"coop-stop: Phase 4.5 reconciliation skipped; commit/push already done in 4.2; graceful-stop finalizes on next iteration"}' | bash core/scripts/execution-diary.sh append
    PROCEED directly to Phase 4.6 — skip the Reconciliation block below
```

### Reconciliation (runs when stop NOT pending)

```
# Freshness prioritization: check most-retrieved nodes first
# High-retrieval nodes have more impact if they're wrong
Bash: experience-read.sh --most-retrieved 10
high_retrieval_nodes = extract tree_nodes_related from top experiences
Prioritize these nodes in the reconciliation scan below

IF external_changes:  # Set by Phase 4.2 domain steps (concrete detection, not assumed)
    tree_nodes_used = primary_nodes read during intelligent retrieval (from Phase 4)
    IF tree_nodes_used is non-empty:
        For each node_key in tree_nodes_used:
            Read the node's .md file (brief scan, not deep read)
            Ask: "Does this node still accurately reflect reality after what I just changed?"
            IF node is stale or contradicted:
                IF quick fix (< 3 sentences): update node now, set last_update_trigger:
                    {type: "reconciliation", source: goal.id, session: N}
                ELSE: echo '{"node_key": "<node_key>", "reason": "<reason>", "source_goal": "<goal.id>", "priority": "medium", "created": "<today>"}' | Bash: wm-append.sh knowledge_debt

ELIF goal resolved a hypothesis with outcome CORRECTED:
    # Corrections mean our knowledge was WRONG — high-priority reconciliation
    affected_nodes = nodes from retrieval context matching goal.category
    For each affected node:
        IF outcome contradicts node content → reconcile immediately or log HIGH debt
        IF outcome refines understanding → update confidence, add compressed insight
```

### Probe-Outcome Surprise Detection (E7)

Runs IN ADDITION to the two branches above — they cover different signals.
external_changes covers world-changing actions the agent performed. CORRECTED
covers hypothesis outcomes. Probe-outcome surprise covers a third case:
Phase 4's primary action invoked a canonical probe (any script that reads
external system state — `infra-health.sh`, the agent's domain probes like
`efs-ssh.sh`, `operator-api.sh`, `state-replay`, `aws-exec.sh`, etc.) and the
probe output DIVERGES from documented expected values.

Without this check, drift between a script's hardcoded defaults and the
tree's documented values (the canonical drift class — rb-334 / guard-308 /
rb-389) sits in the execution archive only and never reaches the tree. The
next session re-discovers the same drift.

```
1. Detect probe execution: did the result text contain canonical-probe
   output? Look for command output blocks tagged with rc/exit codes,
   structured JSON responses, or output prefixed with a script name from
   `world/scripts/` or `core/scripts/`. Use judgment — this is LLM-driven
   detection, not regex.

   IF no probe was invoked this Phase 4: SKIP this sub-section entirely.

2. For each probe identified:
   a. What was probed? Short description (e.g., "service health endpoint",
      "remote filesystem listing", "API response shape").
   b. Find candidate documenting tree nodes:
        Bash: bash core/scripts/tree-find-node.sh --text "<probe topic>" --top 3
   c. For each candidate node, read briefly (front matter + Verified Values
      section + Key Insights). Extract documented:
        - Expected ports / URLs / endpoints
        - Documented field names / schema shape
        - Expected output format / sentinel values
        - Documented error patterns

3. Compute surprise per probe — pick the highest:
     port/URL/endpoint mismatch  → surprise 8 (HIGH)
     schema/field-shape divergence → surprise 7 (HIGH)
     missing documented field    → surprise 6 (MEDIUM-HIGH)
     silent empty output          → surprise 5 (MEDIUM — per rb-389,
                                    silent failure is non-zero signal but
                                    requires the two-probe rule before
                                    high-conviction conclusion)
     output matches documented   → surprise 0 (skip — no encoding needed)

   IF max surprise < 6: SKIP — drift not significant enough to encode.

4. File a knowledge_debt entry. node_key is the best-matching candidate
   node from step 2c (NOT null — we have a concrete target):

   echo '{
     "node_key": "<best-matching node key>",
     "reason": "probe-outcome-divergence: <probed thing> — <what diverged>",
     "source_goal": "<goal.id>",
     "priority": "HIGH",
     "created": "<today ISO>",
     "surprise_score": <integer 6-8>,
     "probe_script": "<canonical probe name>",
     "divergence_summary": "<one-line: documented X, observed Y>"
   }' | bash core/scripts/wm-append.sh knowledge_debt

5. Also queue an encoding observation in sensory_buffer so Phase 8's
   encoding pipeline picks it up via the high_surprise replay priority.
   Shape matches `core/config/memory-pipeline.yaml` `encoding_observation`
   template. Surprise score normalized to 0-1 (divide step-3 score by 10):

   echo '{
     "source_goal": "<goal.id>",
     "observation": "Probe drift detected: <divergence_summary>",
     "encoding_score": 0.0,
     "scores": {
       "novelty": 0.7,
       "outcome_impact": 0.6,
       "surprise": <surprise_score/10>,
       "goal_relevance": 0.5,
       "repetition_strength": 0.2
     },
     "target_article": "<best-matching node file>",
     "replay_priority": "high_surprise"
   }' | bash core/scripts/wm-append.sh sensory_buffer

6. Diary breadcrumb:
   echo '{"entry_type":"observation","goal_id":"<goal.id>","content":"E7 probe-outcome surprise: <summary>"}' | bash core/scripts/execution-diary.sh append

Fail-open: tree-find-node errors, wm-append errors, or empty candidate
lists → log to execution-diary, do not block downstream phases. The
debt-and-observation queue is a best-effort signal; missing one drift
detection is recoverable, blocking the loop is not.
```

Rationale (WHY HIGH priority and dual-write to both knowledge_debt and sensory_buffer): `core/config/rationale/aspirations-execute.md`

### Phase 4.6: Post Findings to Board — lightweight: SKIP if trivial_mode (already conditional)

After goal execution and knowledge reconciliation, post notable findings:

```
IF goal produced actionable findings OR hypothesis was resolved:
    summary = one-line summary of what was learned or accomplished
    echo "${summary}" | Bash: board-post.sh --channel findings --type finding --tags <goal.category>
```

Skip for routine/maintenance goals that produce no new knowledge.

### Phase 4.7: Full-Suite Test Recommender (g-115-858) — lightweight: SKIP if trivial_mode (already file-change-gated)

Advisory banner that detects code changes from this goal (Mind framework
under `core/scripts/`, `mind_api/src/`, `.claude/skills/`, `.claude/rules/`,
`core/config/`, AND product workspace under `AGENT_WRITE_PATH`) and
recommends the appropriate full-suite test commands BEFORE the orchestrator
hands control to Phase 5 verify.

Rationale (WHY advisory posture not hard-block, and origin incident): `core/config/rationale/aspirations-execute.md`

```
# Skip for routine outcomes — rule scope is deep code closures only.
# The script accepts --outcome-class and short-circuits internally on
# routine, but emit the conditional here so the intent is visible in
# pseudocode and the diary breadcrumb is consistent across goals.
IF outcome_class != "routine":
    Bash: bash core/scripts/full-suite-recommender.sh <goal.id> --outcome-class <outcome_class>
    # Banner names recommended invocations per detected file category.
    # The LLM SHOULD run the recommended commands before Phase 5 verify
    # if "all tests pass" is going to appear in the verify narrative.
    # Skip only if the changes are clearly non-behavioral (pure narrative
    # in a SKILL.md, rule wording without companion script wiring, etc).
ELSE:
    # Quiet skip — the script self-skips on routine; no banner emitted.
    Pass
```

Companion rule: `.claude/rules/run-full-suite-after-deep-code.md` defines
what "full-suite" means per code area (Mind: `python -m pytest
core/scripts/tests -q`; product Java: `./gradlew test --no-daemon`;
product Node: `npm test`).

Cross-reference: `world/conventions/post-execution.md` Step 2.b.1 ALSO
mandates the product-repo full-suite as a pre-push build gate — but
that fires AFTER commit, when Phase 5 already claimed "all tests pass."
This Phase-4.7 advisory fires BEFORE Phase 5, closing the window where
false claims would land in the verify narrative.

## Batched Execution

Batched execution (RARE — only when batch_mode is true).
Default is single-goal. Batch only fires for trivially small second goals.
Sequential batch execution (non-delegated path).
When using agent delegation, parallel dispatch is handled in Phase 2.6.
Each batched goal MUST complete full Phase 5-8 before the next starts.

```
IF batch_mode AND more goals in batch:
    Execute next goal in batch (reuse retrieval context, skip Phase 2)
    Classify outcome_class for batched goal (same Phase 4-post rules)
    MANDATORY per-goal phases (in order, gated by outcome_class):
    - Phase 5: Verify completion (always runs)
    - Phase 6: Spark check (SKIP if routine)
    - Phase 7: Aspiration-level check (always runs)
    - Phase 8: State Update Protocol — full steps with immediate tree encoding if deep,
      Steps 1-4 + abbreviated Step 7 if routine
    Complete ALL phases for this goal before starting the next batched goal.
Bash: echo "aspirations-execute phase documented"
```

## Chaining

- **Called by**: `/aspirations` orchestrator (Phase 4, every iteration)
- **Calls**: `aspirations-update-goal.sh --source`, `aspirations-add-goal.sh --source`, `load-conventions.sh`, `load-tree-summary.sh`, `retrieve.sh`, `tree-update.sh`, `guardrail-check.sh`, `infra-health.sh`, `experience-add.sh`, `wm-set.sh`, `wm-read.sh`, `board-post.sh`, `skill-relations.sh`, `build-agent-context.sh`, `curriculum-contract-check.sh`, `pending-agents.sh`

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
Phase 4.6 ends with tool calls (aspirations-update-goal.sh, experience-add.sh). Never
end with a text "Output:" block; the final Bash or Skill invocation must be last.
