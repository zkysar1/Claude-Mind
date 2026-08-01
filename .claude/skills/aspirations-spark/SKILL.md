---
name: aspirations-spark
description: "Runs the Spark Check (Phase 6) and Immediate Learning (Phase 6.5) of the aspirations loop: adaptive spark questions, all sq-XXX handlers (including sq-012 self-purpose update), aspiration-level spark, and immediate capture of reasoning-bank entries, guardrails, and forge awareness. Use whenever a goal completes — this is the recursive self-improvement mechanism that captures learning in-flight instead of deferring to /reflect. Internal sub-skill of /aspirations."
user-invocable: false
parent-skill: aspirations
triggers:
  - "run_spark_check()"
  - "run_aspiration_spark()"
conventions: [aspirations, spark-questions, reasoning-guardrails, experience]
minimum_mode: autonomous
revision_id: "skill-bootstrap-aspirations-spark-e160b8"
previous_revision_id: null
---

# Spark Check (Micro-Evolution) and Immediate Learning

Invoked after every goal completion as Phase 6 (spark check) and Phase 6.5 (immediate learning) of the aspirations loop. The spark check is the recursive self-improvement mechanism. Phase 6.5 captures reasoning bank entries, guardrails, and forge awareness immediately during execution rather than waiting for /reflect.

## Abbreviation Policy

Mandatory writes for this obligation: see `core/config/obligation-schema.yaml`
→ `obligations.spark`. Spark has no mandatory writes — it's discretionary
by design — but abbreviation is explicitly permitted when
`outcome_class == routine`. When skipping, log one line in the NARRATIVE
daily journal (`agents/<agent>/journal/YYYY/MM/YYYY-MM-DD.md` — plain text;
NEVER `journal.jsonl`, a JSON-per-line index that one raw text line corrupts —
2026-07-16 line-309 incident, every subsequent daemon append 500'd):
`OBLIGATION ABBREVIATED: spark — {condition}`. The learning-gate audit
(Phase 9.5d) verifies the claimed condition was true at iteration time.

---

## Handoff Goal Protocol (Item 3)

When Phase 6.5 or any sq-XXX handler creates a goal that another agent should
pick up (e.g., a planner agent filing "Apply: ..." work for an implementer
agent), set the handoff fields so Item 3's scoring routes the goal correctly:

```json
{
  "title": "Apply: {what the target agent must do}",
  "participants": ["agent"],
  "handoff_to": "<target-agent-name>",
  "handoff_from": "{current MIND_AGENT}",
  "handoff_created_at": "{ISO timestamp now}",
  ...
}
```

The target agent must be reachable via `participants` — `[agent]` (shown
above, visible to all agents), `[agent, user]`, or explicit `[<name>]` all
qualify. `handoff_to` is the *routing preference*; `participants` is the
*visibility gate*. Handoff fields are additive — goals without them keep
baseline scoring. See `core/config/conventions/goal-schemas.md`
§ Cross-Agent Handoff Fields.

Without these fields, the goal lands in the shared world queue with baseline
priority and may rot. With them, the target agent's boot surfaces it
(`▸ N pending handoff(s) for you`) and the selector's handoff_bonus scoring
(default +0.30) prioritizes it above unrelated work.

## Inputs

- `source`: Queue origin (`"world"` or `"agent"`) — pass `--source {source}` to all `aspirations-*.sh` calls

**Step 0: Load Conventions** — `Bash: load-conventions.sh` with each name from the `conventions:` front matter. Read only the paths returned (files not yet in context). If output is empty, all conventions already loaded — proceed to next step.

**Step 0.5: Record this spark firing for sentinel dedup (g-115-1203)** — both
fire paths for this skill funnel through here: (1) the in-turn FAST path, where
the LLM fires Skill(aspirations-spark) directly from recurring-close.sh's
stdout outcome-aware imperative, and (2) the pending_phase_6_spark sentinel,
consumed by aspirations/SKILL.md Phase -0.5c.2 on next-iteration entry. Without
a shared record, the fast path double-fires (fire #1 in-turn + fire #2 from the
still-set sentinel). Recording the firing here lets Phase -0.5c.2's `check` skip
the redundant re-fire. One-shot, fail-open — a dedup-record error must never
block the spark.

```
goal_id = the just-completed goal this spark is evaluating (explicit on the
          sentinel path via Phase -0.5c.2's goal_id arg; the current loop goal
          on the fast path).
IF goal_id is in context:
    # Pipe the wm slot THROUGH the dedup helper in ONE bash call: wm-read emits
    # the current map, spark-fire-dedup.py stamps goal_id and emits the new map,
    # wm-set replaces the slot. The helper is pure stdin->stdout (it must NOT
    # spawn `bash wm-*.sh` itself — that hangs, rb-225/rb-247).
    Bash: bash core/scripts/wm-read.sh spark_fired_session --json | py -3 core/scripts/spark-fire-dedup.py record <goal_id> | bash core/scripts/wm-set.sh spark_fired_session
# No goal_id (rare — e.g. a manual non-goal-scoped invocation): skip the record.
```

## Phase 6.5: Immediate Learning (reasoning bank + guardrails + pattern outcomes)

If this goal's outcome produced a clear, reusable reasoning insight or
a safety lesson, capture it NOW — don't wait for /reflect.
This is for lessons learned during EXECUTION, not hypothesis resolution
(which /reflect handles separately).

SKIP: goal outcome was routine/expected with no new insight.
Exception: the Operational Gotcha Auto-Detection block always runs (it uses
structural keyword signals, not agent judgment about novelty).

PLACEMENT CHECK (before creating ANY guardrail or rb entry below): if the
prescriptive rule's `applies_to` is `domain` — i.e., it names a brand, a
product, a specific external service, or domain-specific infrastructure —
the rule belongs in a `world/conventions/*.md` file or as a `domain`-scoped
guardrail entry, NOT in a core `.claude/rules/*.md` file. Core rules and
core conventions must remain domain-agnostic per
`.claude/rules/domain-free-examples.md`. Pick `applies_to` honestly: if in
doubt between framework and domain, pick domain.

```
    IF goal outcome revealed a reusable reasoning pattern (heuristic, procedure,
       diagnostic, or causal insight) that would help with FUTURE similar goals:

        # Duplicate/contradiction check before creating reasoning bank entry
        existing_rb = Bash: reasoning-bank-read.sh --category {goal.category}
        IF proposed entry semantically overlaps with an existing entry:
            Strengthen existing: Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful
            Log: "Phase 6.5: Strengthened existing {entry.id} instead of creating duplicate"
            SKIP creation
        IF proposed entry contradicts an existing entry:
            Retire old: Bash: reasoning-bank-update-field.sh {entry.id} status retired
            Proceed to create new entry (supersedes old)

        Create reasoning bank entry via reasoning-bank-add.sh:
          # `id` and `created` auto-set by the script inside the file lock.
          # Omit both — capture the assigned id from stdout's full-record JSON.
          title: concise name for the insight
          type: success | failure   # success if from a working approach; failure if from debugging/fixing
          category: goal's category
          # Differentiated extraction prompt by type (g-306-23, ReasoningBank §3:
          # success and failure carry different reusable signal, so prompt them apart):
          #   type==success → EXTRACT-VALIDATED-STRATEGIES: name the reusable
          #     strategy/heuristic that WORKED, the preconditions under which it
          #     applies, and WHY it succeeded — framed as a pattern to REPEAT.
          #     ("What is the validated, transferable approach here?")
          #   type==failure → EXTRACT-COUNTERFACTUAL-PITFALLS: name the specific
          #     pitfall to AVOID, what went wrong, and the counterfactual — the
          #     corrective action that SHOULD have been taken instead.
          #     ("What is the avoidable pitfall, and what should have happened?")
          content: per the type-matched prompt above — a validated strategy to repeat
                   (success) OR a counterfactual pitfall + its correction (failure)
          applies_to: <any|framework|domain|specific>  # REQUIRED. any=cross-cutting methodology; framework=this framework's skills/scripts/gates; domain=this agent's deployment domain (the specific services, products, workflows the agent is deployed into); specific=single-incident
          when_to_use: when this insight applies
          source_goal: goal.id
          source_reflection_id: "ref-{goal.id}-{timestamp}"  # MR-Search: enables reflection quality tracking
          poignancy: <1-10>   # g-306-26 producer (BRD Gap 1a / Generative Agents 2304.03442):
                              # LLM-rated importance at write so the field populates (else the
                              # g-306-08 retrieve blend stays a permanent no-op on an all-null corpus).
                              # 1-3 routine/expected · 4-6 useful · 7-8 pivotal/surprising · 9-10 mission-altering.
                              # Rate how durable + impactful this lesson is for FUTURE retrieval.
        Log in journal: "Immediate learning: created {rb-id from stdout} from {goal.id}"

    IF goal outcome revealed a safety hazard, a mistake to avoid, or a
       precondition that MUST be checked in future similar work:

        # Duplicate/contradiction check before creating guardrail
        existing_guards = Bash: guardrails-read.sh --category {goal.category}
        IF proposed guardrail semantically overlaps with an existing guardrail:
            Strengthen existing: Bash: guardrails-increment.sh {guard.id} utilization.times_active
            Log: "Phase 6.5: Strengthened existing {guard.id} instead of creating duplicate"
            SKIP creation
        IF proposed guardrail contradicts an existing guardrail:
            Retire old: Bash: guardrails-update-field.sh {guard.id} status retired
            Proceed to create new guardrail (supersedes old)

        Create guardrail via guardrails-add.sh:
          # `id` and `created` auto-set by the script — omit both; capture
          # assigned id from stdout's full-record JSON.
          rule: what to check or avoid
          category: goal's category
          trigger_condition: when this guardrail applies
          source: goal.id
          source_reflection_id: "ref-{goal.id}-{timestamp}"  # MR-Search: enables reflection quality tracking
        Log in journal: "Immediate guardrail: created {guard-id from stdout} from {goal.id}"

    # ── Operational Gotcha Auto-Detection (MANDATORY) ──────────────────
    # Structural trigger: if execution involved debugging/fixing an error,
    # the resolution pattern MUST be encoded. Not optional agent judgment.
    # Uses keyword scan on execution context (same pattern as Step 8.5).
    #
    # Signal detection (scan goal outcome summary + execution trace):
    #   error_then_fix: (error|exception|traceback|failed|refused|permission denied|not found)
    #                   AND (fixed by|resolved by|workaround|solution|the fix|root cause|turned out)
    #   explicit_gotcha: (must use|always use|never use|don't forget|gotcha|caveat|pitfall|footgun)
    #   environment_issue: (environment|env var|export|path|config|permission|port|firewall)
    #                      AND (issue|problem|wrong|missing|incorrect|unexpected)
    #
    IF any gotcha signal detected in execution context:
        # Determine store: prescriptive ("always/never/must") → guardrail; diagnostic → reasoning bank
        IF lesson matches prescriptive pattern (always|never|must|do not):
            existing_guards = Bash: guardrails-read.sh --category {goal.category}
            IF semantic overlap with existing:
                Bash: guardrails-increment.sh {guard.id} utilization.times_active
                Log: "OPS GOTCHA: Strengthened existing {guard.id}"
            ELIF no semantic overlap:
                Create guardrail via guardrails-add.sh:
                  # `id` and `created` auto-set — omit both; capture from stdout.
                  rule: the prescriptive lesson
                  category: goal's category
                  trigger_condition: when this gotcha applies
                  source: goal.id
                  tags: ["ops-gotcha"]
                Log: "OPS GOTCHA (guardrail): {rule} from {goal.id}"
        ELSE:
            existing_rb = Bash: reasoning-bank-read.sh --category {goal.category}
            IF no semantic overlap with existing:
                Create reasoning bank entry via reasoning-bank-add.sh:
                  # `id` and `created` auto-set — omit both; capture from stdout.
                  title: "Gotcha: {concise description}"
                  type: failure
                  category: goal's category
                  content: what happened, why, and how it was fixed
                  applies_to: <any|framework|domain|specific>  # REQUIRED. ops gotchas about external services / domain infra → domain; framework-internal gotchas → framework; cross-cutting → any
                  when_to_use: {conditions: ["{error pattern or symptom}"], category: "{goal.category}"}
                  source_goal: goal.id
                  tags: ["ops-gotcha"]
                  poignancy: <1-10>   # g-306-26 producer — see the 1-10 rubric in the
                                      # reusable-reasoning-pattern block above. Ops gotchas
                                      # are typically 5-8 (a reusable pitfall worth retrieving).
                Log: "OPS GOTCHA (reasoning bank): {title} from {goal.id}"
            ELIF semantic overlap found:
                Bash: reasoning-bank-increment.sh {entry.id} utilization.times_helpful
                Log: "OPS GOTCHA: Strengthened existing {entry.id}"

    # Forge awareness: detect recurring manual procedures that should be skills
    IF goal execution required a manual multi-step procedure that was repeated
       across goals, OR that would clearly benefit FUTURE goals as a discoverable
       skill (entry point) rather than inline code:
        Bash: meta-read.sh skill-gaps.yaml
        IF gap already exists for this procedure:
            Increment times_encountered, append to encounter_log
        ELSE:
            Register new gap: id: gap-{next}, status: registered,
              times_encountered: 1, procedure_name, estimated_value,
              type: <utility|analytical>
            # `type` is REQUIRED at registration (g-115-3131). Omitting it was the
            # root cause of 22 of 24 gaps being typeless, which silently handed the
            # forge gate's type-default authority over the whole corpus. Classify
            # against core/config/skill-gaps.yaml gap_types:
            #   utility    = mechanizes an ALREADY-DERIVED procedure — deterministic
            #                steps, known inputs->outputs (API calls, data formatting,
            #                retrieval/orchestration workflows). Gates at CALIBRATE.
            #   analytical = the OUTPUT depends on domain-mature judgment (pattern
            #                recognition, evaluation, deriving semantics). Gates at
            #                EXPLOIT — the higher bar, so choose it deliberately.
            # When genuinely unsure, write `utility` and say why in the description:
            # that IS the default, so recording it explicitly costs nothing and keeps
            # the gate's input meaningful rather than absent.
        # meta-set.sh is a DOTPATH setter — `meta-set.sh <file> <dotpath> <value>`.
        # It does NOT take a whole-file YAML rewrite (that exits 1 with a bare
        # usage line). `[N]` and `.N` are equivalent (meta-yaml.py:110) and a JSON
        # array stores as a real YAML list (parse_value, g-115-1263) — guard-661.
        #
        # The two branches above need DIFFERENT call shapes (g-115-3462). Measured:
        #   EXISTING gap (index i already present) — per-field dotpaths, one call each:
        #     meta-set.sh skill-gaps.yaml "gaps[<i>].times_encountered" <n+1> --reason "<goal-id>"
        #     meta-set.sh skill-gaps.yaml "gaps[<i>].encounter_log" '<full JSON array>'
        #   NEW gap (the ELSE branch) — ONE call writing the WHOLE element at
        #   index == current length, which set_field appends:
        #     meta-set.sh skill-gaps.yaml "gaps[<len>]" '{"id":"gap-NNN","status":"registered","times_encountered":1,"type":"utility",...}'
        #
        # Do NOT reach for a per-field dotpath on a NEW gap: `gaps[<len>].id`
        # raises navigate_error ("list index N out of range"), because _navigate
        # bounds-checks INTERMEDIATE segments but not the final key. That dead end
        # is what pushes a caller toward a whole-array read-modify-write, which is
        # the operation that caused the g-115-3433 corruption. The whole-element
        # append needs no RMW at all. (For CONCURRENT writers prefer
        # _fileops.locked_modify_yaml — meta-set.sh RMW across two daemon calls is
        # a TOCTOU race; guard-661.)
        #
        # A malformed payload is now REFUSED rather than silently stored: writing a
        # non-JSON string over a list/dict-valued dotpath exits non-zero with
        # `type_destruction` instead of replacing the whole key with one scalar
        # (g-115-3462). If you see that error, the payload did not parse as JSON —
        # the usual cause is a stray line of stdout captured into it.
        Bash: meta-set.sh skill-gaps.yaml "gaps[<i>].<field>" <value>   # existing gap
        Bash: meta-set.sh skill-gaps.yaml "gaps[<len>]" '<full gap JSON object>'  # new gap
        # WRITE-INTEGRITY READ-BACK (g-115-3177) — check rc AND re-read. This step
        # used to be one unchecked line, so a failed write was SILENT: the agent
        # journalled "gap registered" and moved on while nothing landed. That is
        # not hypothetical — on 2026-07-26 this exact write returned write_conflict
        # 5/5 for zeta (g-335-275) because the daemon's meta-YAML path fenced
        # against a stale local mirror with no refresh and no retry. Scope is
        # per-object AND per-box (rb-2639/rb-3280), NOT fleet-wide — a peer's
        # cross-box write stales only the OBSERVER's mirror — but where it lands
        # it is PERMANENT: the 412 repeats forever against a remote that never
        # changes, so no gap can be registered or incremented on that box,
        # times_encountered never reaches forge_threshold, and no skill is forged
        # from a detected capability gap. The daemon path is fixed (locked_rmw + force_fresh), but
        # the protocol must not depend on that: assert the write, never assume it.
        # Same rule as the forge-goal read-back below (own-cloud can echo success
        # on a write that did not land).
        IF meta-set.sh exit code != 0:
            Log the FULL stderr — do NOT narrate "gap registered".
            Re-read skill-gaps.yaml; if the gap is genuinely absent, file
            "Investigate: skill-gaps.yaml write failed — <error code>" (HIGH,
            participants [agent]) so this box's forge lane cannot go dark
            unnoticed again, then continue (never block the close on it).
        ELSE:
            Bash: meta-read.sh skill-gaps.yaml → confirm the gap id is present
            with a non-null `type`. If it is NOT, treat exactly as the rc!=0
            branch: an rc=0 that did not land is the worse failure, not the
            better one.

        # Check forge criteria immediately
        # GUARD: skip gaps in a TERMINAL status (Phase 9.2 checks the same set).
        # Terminal set = {forged, dismissed, satisfied-by-extension}:
        #   forged                 — a skill was created (+ a forged-skills.yaml entry)
        #   dismissed              — explicitly declined by /forge-skill
        #   satisfied-by-extension — capability shipped by EXTENDING an existing
        #                            script/skill instead of forging a new one (a
        #                            legitimate outcome, and the honest label when
        #                            nothing was actually forged)
        # Test the SET, never `== "forged"` alone: any non-excluded terminal status
        # makes its gap re-qualify as forge-ready forever, so every pass re-files the
        # forge goal and the duplication gate blocks it on the completed original via
        # origin_signal_completed — the same dead-end investigation, every time.
        # Observed 2026-07-27 on gap-026 (satisfied-by-extension). Keep this set in
        # sync with aspirations-evolve/SKILL.md Step 9; those two are the only readers
        # of gap status and must agree. (guard-821 reinforces: a terminal status with a
        # resolution note IS the mechanism that stops re-qualification.)
        IF gap.status in ("forged", "dismissed", "satisfied-by-extension"): skip forge criteria check
        # Registry cross-check (g-326-09 incident; mirrors evolve Step 9): before trusting
        # gap.status, grep world/forged-skills.yaml for `gap_ref: {gap.id}`. If a forged skill
        # already references this gap, the local skill-gaps.yaml status is STALE (observed 11
        # days stale for gap-006 despite a daemon-routed read) — SKIP the gap.
        # forged-skills.yaml is the authoritative cross-agent registry; it is low-write-frequency
        # and far less divergence-prone than skill-gaps.yaml (guard-1163 family).
        Bash: grep -q "gap_ref: {gap.id}" "$WORLD_DIR/forged-skills.yaml" && SKIP this gap (already forged by another agent)

        Read core/config/skill-gaps.yaml → forge_threshold (default: 2)
        Read agents/<agent>/developmental-stage.yaml → current stage
        # Curriculum contract gate (g-115-1801): the stricter gate /forge-skill enforces at its
        # Step 1. Dev-stage >= EXPLOIT (competence axis) can pass while the curriculum contract
        # (capability-unlock axis) still blocks forging — gate on BOTH so we never queue a forge
        # goal that /forge-skill will ABORT. Exit 0 = permitted, exit 1 = blocked by curriculum stage.
        Bash: curriculum-contract-check.sh --action allow_forge_skill
        IF gap.times_encountered >= forge_threshold
           AND gap.estimated_value >= "medium"
           AND developmental stage >= EXPLOIT (developing+)
           AND curriculum-contract-check exit code == 0:
            # Live-store dedup (g-115-2284 — replaces compact-search; the in-context compact is
            # doubly stale: context-read dedup + local-mirror render):
            Bash: aspirations-query.sh --goal-field origin_signal "idea:forge-ready-{gap.id}"
            Bash: aspirations-query.sh --title-contains "Forge skill: {gap.procedure_name}"
            #   (second probe catches legacy datestamped origin_signal variants)
            IF either probe returns a pending/in-progress goal: SKIP — duplicate exists.
            IF either probe ERRORS (non-zero exit or unparseable output): WARN loudly + SKIP —
                suppression gates fail CLOSED (guard-487); a missed filing re-detects on the
                next encounter, a cross-box duplicate does not self-heal.
            IF both probes returned clean-empty ([]):
                Route to target aspiration (current → matching category → /create-aspiration)
                Build goal: title "Forge skill: {gap.procedure_name}",
                  skill "/forge-skill", args "skill {gap.id}", priority "MEDIUM",
                  origin_signal EXACTLY "idea:forge-ready-{gap.id}" — canonical form, NO
                  datestamp suffix (a datestamped variant defeats the duplication-gate's
                  Strategy-1 exact match; g-115-2284 incident g-115-2279-vs-g-307-54)
                Add via aspirations-add-goal.sh --source {source} {asp.id} (goal JSON on stdin —
                  the canonical gated single-goal writer; replaces aspirations-update.sh here so
                  the goal-duplication-gate baseline fires at this site too, matching Step 9)
                Post-filing read-back: re-run the origin_signal probe; only log "forge goal
                  filed" when the goal reads back (own-cloud can silently swallow the write while
                  echoing success — insight msg-20260714-213836-echo-3288). IF read-back empty:
                  WARN + retry the add once, then file-or-fail loudly.
                Log in journal: "Forge-ready gap detected during execution: {gap.id}"
                Log: echo '{"date":"...","event":"forge-ready","details":"Gap {gap.id} detected in Phase 6.5 from {goal.id}","trigger_reason":"immediate-learning-forge"}' | bash core/scripts/evolution-log-append.sh

    # -- Pattern-Outcome Recording (wire retrieved-and-applied signatures, g-115-1442) --
    # Closes the loop the utilization-feedback path deliberately skips
    # (pattern_signatures have NO utilization increment path --
    # utilization-feedback.py:187 "pattern_signatures don't have utilization
    # increment paths") and that reflect-on-outcome covers ONLY for
    # hypothesis-linked patterns (its CONFIRMED/CORRECTED calls fire from ABC
    # chains). Without this step a pattern RETRIEVED and APPLIED during ordinary
    # goal execution -- never tied to a resolving hypothesis -- accrues
    # retrieval_count while outcome_stats.total stays 0 forever, so calibration
    # measures tracking-presence, not pattern value (g-115-1441 finding: 14/19
    # active patterns unwired; this is the g-115-1442 fix). The recording API
    # (pattern-signatures-record-outcome.sh) already exists; this is the missing
    # AUTOMATIC TRIGGER for the non-hypothesis path. Record VALUE, not presence
    # (rb-1554): a retrieved pattern you did NOT apply records NOTHING.
    #
    # SKIP entirely IF trivial_mode OR outcome_class == routine (no deliberation
    # worth judging). Otherwise:
    Bash: cat agents/<agent>/session/retrieval-session.json   # may be absent
    IF file absent OR retrieval_performed == false: SKIP this block
    retrieved_sigs = [e["id"] for e in (session.supplementary_detail or [])
                      if e.get("type") == "pattern_signature"]
    IF retrieved_sigs is empty: SKIP this block   # common case: no Step-4 patterns
    FOR EACH sig_id in retrieved_sigs that you APPLIED to shape this execution
        (the Step 4 Memory Deliberation ACTIVE set -- NOT merely retrieved):
        # guard-575: a meta-pattern (one that predicts a prediction-error class,
        # e.g. sig-003) is recorded via hypothesis resolution in
        # reflect-on-outcome, NOT here -- skip those to avoid double-counting.
        IF sig_id names a meta-pattern: continue
        Judge against the ACTUAL outcome of this goal:
          - the signature's expected_outcome / lesson HELD here -> CONFIRMED
          - reality diverged / the lesson was wrong here        -> CORRECTED
        Bash: pattern-signatures-record-outcome.sh {sig_id} {CONFIRMED|CORRECTED}
        Log: "Pattern outcome: {sig_id} {verdict} from {goal.id}"
    # A pattern retrieved but NOT applied records nothing -- not-applicable is
    # not CORRECTED, and recording it would inflate outcome_stats.total with
    # noise (the exact failure mode g-115-1441 warned against).
```

---

## Spark Check (Micro-Evolution)

Run after EVERY goal completion. This is the recursive self-improvement mechanism.

### Program-Alignment Response (boost_generative_sparks consumer)

The `aspirations-select` Program-alignment probe (see `aspirations-select/SKILL.md`
§ Program-alignment probe, lines 156-185) reads `world/program.md` every
`check_interval_goals` iterations, asks the LLM whether the top-ranked goal
materially serves The Program, and increments `program_misalignment_streak`
in working memory on misalignment. When the streak reaches 3, the probe sets
`boost_generative_sparks = true`. This phase is the consumer for that flag —
without it, the probe's terminal action is a dead signal and the framework-vs-
domain self-correction mechanism never fires.

```
# CRITICAL: --json mode is load-bearing. The writer in aspirations-select
# stores a JSON bool (`echo 'true' | wm-set.sh ...`). Non-JSON wm-read.sh
# would serialize that as Python's `True` (capital T) while --json emits
# lowercase `true`. Comparing to the string "true" ONLY works in JSON mode.
# wm-read.sh prints `null` + exit 0 on missing slot — no fallback needed;
# `"null" != "true"` is the intended pass-through when the probe hasn't fired.
Bash: boost = wm-read.sh boost_generative_sparks --json
IF boost.strip() == "true":
    Log: "▸ Program-alignment misalignment streak triggered — forcing aspiration_generation spark with product-domain bias"

    # Clear the flag FIRST (atomic, idempotent). Even if subsequent steps
    # error, the flag must not re-fire on the next iteration — otherwise a
    # single misalignment escalation loops forever.
    # CRITICAL: clear with `echo 'false'` (JSON bool) to match the writer's
    # type. `echo '"false"'` would store a JSON string, breaking the bool
    # round-trip that the --json read above depends on.
    Bash: echo 'false' | wm-set.sh boost_generative_sparks

    # Fire sq-007 (aspiration_generation) unconditionally — bypasses the
    # routine-spark filter's escalation gate. The bias toward product-domain is
    # communicated through the in-turn LLM question prompt below; the
    # Extended layer (E1 in the aspiration-management plan) will formalize
    # the bias via aspiration-generation-strategy.yaml domain_class_targets.

    Ask sq-007: "The Program-alignment probe detected a misalignment streak
      of 3 — my recent goals have drifted from The Program's primary
      product focus (read world/program.md for the current domain) toward
      framework-meta work. What new PRODUCT-DOMAIN aspiration would
      materially serve The Program right now? Consider domain-specific
      quality improvements, end-user-facing pipeline work, integration
      work, or competitive-landscape response — whatever The Program
      identifies as its primary value surface. Do NOT propose framework-
      meta, agent-health, or cognitive-core aspirations — those are what
      triggered the streak."
    Bash: spark-questions-increment.sh sq-007 times_asked
    IF sq-007 produces a concrete aspiration candidate:
        Bash: spark-questions-increment.sh sq-007 sparks_generated
        Invoke /create-aspiration from-self --plan with the candidate
        description and explicit product-domain framing. The LLM passes the
        product-domain preference through the candidate text; Phase B of
        create-aspiration evaluates it normally.

    # Log the causal chain so future reflection can attribute outcomes to
    # the alignment intervention. Posted as a finding so correlations
    # between probe firing and spark-generated aspirations are discoverable
    # cross-agent. (journal-add.sh takes stdin JSON with a journal_file key
    # — it is not a free-form telemetry sink; board-post fits this one-line
    # event better.)
    Bash: echo "Misalignment streak consumed: sq-007 fired with product-domain bias; outcome={candidate_created|no_candidate}" | board-post.sh --channel findings --type finding --tags "program-alignment,spark,sq-007"
```

### Goal-Level Spark

### Routine Spark Mode

When `outcome_class == "routine_spark"`, evaluate creative + hypothesis questions.
This keeps the hypothesis pipeline alive AND surfaces non-obvious insights from
routine work. The expanded set is still limited (6 categories, self-selecting)
so cost is bounded. Principle: we are here to learn — never skip.

```
IF outcome_class == "routine_spark":
    Bash: spark-questions-read.sh --active
    all_active_questions = result  # save — result will be overwritten by later reads
    creative_routine_questions = [q for q in all_active_questions if q.category in (
        "hypothesis_generation",       # sq-009 — testable predictions
        "forward_prediction",          # sq-011 — what would break/change
        "experiential_hypothesis",     # sq-c09 — player perspective
        "first_principles",            # sq-016 — inherited assumptions
        "transfer",                    # sq-003 — cross-domain transfer
        "surprise",                    # sq-004 — did the outcome surprise us
        "self_evolution"               # sq-012 — does this outcome change my core purpose?
    )]
    Log: "▸ Routine spark: evaluating {len(creative_routine_questions)} creative+hypothesis questions"
    For each question in creative_routine_questions:
        Ask the question about the just-completed goal
        Bash: spark-questions-increment.sh <question.id> times_asked
        If spark generated:
            Bash: spark-questions-increment.sh <question.id> sparks_generated
            Execute the spark action (hypothesis creation via sq-009 handler,
            or first-principles via sq-016 handler, or transfer insight log)
    If any spark fires → log via:
      echo '{"event":"routine_spark","details":"Goal {id} routine-sparked: {description}","date":"<today>"}' | bash core/scripts/evolution-log-append.sh

    # ── Phase R2: Signal-escalated work discovery ──
    # If any creative spark fired, the recurring goal's output was interesting enough
    # to warrant checking whether it suggests new aspirations or actionable work.
    # Also check for strategic scan signals in working memory — these enrich the
    # spark evaluation even for seemingly routine outcomes.
    Bash: wm-read.sh strategic_scan_signals --json
    has_scan_signals = (result is not null and result != "null" and len(result) > 0)

    IF any_spark_fired OR has_scan_signals:
        # Escalate to generative sparks that were skipped by the routine filter
        generative_questions = [q for q in all_active_questions if q.category in (
            "aspiration_generation",   # sq-007 — "Does this outcome justify a NEW ASPIRATION?"
            "work_discovery"           # sq-013 — "Did this reveal actionable work?"
        )]
        Log: "▸ Routine spark ESCALATION: evaluating {len(generative_questions)} generative questions"
        For each question in generative_questions:
            Ask the question about the just-completed goal
            (include scan signal context from working memory if available)
            Bash: spark-questions-increment.sh <question.id> times_asked
            If spark generated:
                Bash: spark-questions-increment.sh <question.id> sparks_generated
                Execute the spark action (sq-007 handler creates aspiration,
                sq-013 handler creates goals or aspiration)

    RETURN  # Skip full spark evaluation and Phase 6.5
```

### Adaptive Spark Questions
Read active spark questions via script instead of using hardcoded spark questions.
1. `bash core/scripts/spark-questions-read.sh --active` → get active questions as JSON
2. Ask each active question about the just-completed goal
3. If a spark is generated: `bash core/scripts/spark-questions-increment.sh <id> sparks_generated`
4. Always: `bash core/scripts/spark-questions-increment.sh <id> times_asked` (script auto-recomputes yield_rate)

Every `evolution_rules.review_interval_sessions` sessions:
- Retire questions with yield_rate < retire_threshold AND times_asked >= min_asks_before_retire
- Promote highest-priority candidate to replace retired question
- Log the change via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

```
Bash: spark-questions-read.sh --active
# ALL active spark questions evaluated for deep outcomes.
# No question count gating — full treatment regardless of outcome tier.
Log: "▸ Spark: evaluating ALL {len(result)} questions (outcome: {outcome_class})"
For each question in result:
    Ask the question about the just-completed goal
    Bash: spark-questions-increment.sh <question.id> times_asked
    If spark generated:
        Bash: spark-questions-increment.sh <question.id> sparks_generated
        Execute the spark action (add source, create article, log gap, etc.)
    # yield_rate is auto-recomputed by the increment script — no manual update needed

If any spark fires → log via:
  echo '{"event":"spark","details":"Goal {id} sparked: {description of change}","date":"<today>"}' | bash core/scripts/evolution-log-append.sh
```

#### Hypothesis Generation via sq-009

When sq-009 (or sq-c09 experiential variant) fires, it creates a hypothesis goal:
0. Load domain context for informed hypothesis formation:
   ```
   Bash: retrieve.sh --category {goal.category} --depth shallow
   Bash: pipeline-read.sh --stage active
   Bash: pipeline-read.sh --stage discovered
   ```
   Check retrieved active/discovered hypotheses for semantic overlap with the proposed prediction.
   IF a hypothesis already covers this prediction → SKIP creation, log: "sq-009: Duplicate of {existing_id}, skipped"
0.1. Category steering (BEFORE forming the prediction):
     Review the categories of existing active+discovered hypotheses from Step 0.
     Count hypotheses per category.
     IF 3+ existing hypotheses share the same category (e.g., "code", "infrastructure"):
         Log: "sq-009: Category '{saturated_category}' saturated ({count} hypotheses) — steering toward under-represented categories"
         Prefer forming predictions in under-represented categories, especially:
           user-experience, system-behavior, domain-quality, engagement
         over already-saturated categories like: code, infrastructure, pipeline
         Reformulate: what USER-FACING or EXPERIENTIAL consequence follows from this work?
0.5. Calibration gate (BEFORE assigning confidence):
     a. Read recent accuracy: `Bash: pipeline-read.sh --stage resolved`
        - Count CONFIRMED vs CORRECTED in this category (or overall if <3 in category)
        - If total == 0: SKIP gate (no track record yet), proceed to Step 0.7
        - Compute recent_accuracy = confirmed / total
     b. Apply confidence ceiling:
        - If recent_accuracy < 0.40: cap at 0.55
        - If recent_accuracy >= 0.40 and < 0.60: cap at 0.65
        - If recent_accuracy >= 0.60 and < 0.80: cap at 0.80
        - If recent_accuracy >= 0.80: no cap
        - Log: "Calibration gate: {N} resolved, {accuracy}% accurate → cap {cap}"
     c. The agent MAY assign confidence below the cap freely.
        The cap only prevents overconfidence, not underconfidence.
0.7. Adversarial pre-mortem (required when proposed confidence > 0.65):
     Before finalizing confidence, articulate:
     a. "The strongest reason this prediction could be WRONG is: ___"
     b. "The code/system might actually handle this because: ___"
     c. If (b) identifies a plausible mechanism the code already handles it,
        reduce confidence by 0.15 (the "well-engineered codebase" prior).
     d. Scope-quantifier decomposition (g-115-2576; runs at ANY proposed
        confidence, not just > 0.65 — the 2026-07-18 replay found the failure
        band at 0.5-0.68): if the claim contains a scope quantifier — "single
        cause", "all N", "every", "systemic", "complete", "fleet-wide",
        "none", "pure" — decompose it per-conjunct / per-member (rb-2572) and
        price confidence off the WEAKEST conjunct, or NARROW the claim to the
        members actually evidenced. 5 of 10 replayed CORRECTED hypotheses
        shared exactly this shape: single-cause→multi-mechanism, systemic→
        isolated, all-N→1-of-N, fleet-wide→one-box-over. A quantified claim
        is a conjunction; its confidence is bounded by its weakest member.
     e. Discriminating-power check (rb-4133; g-001-51, 2026-07-19) — the
        mirror of (d): where (d) guards OVER-claiming breadth (CORRECTED-prone),
        this guards a criterion with ZERO discriminating power (CONFIRMED-prone,
        and therefore useless). If the prediction claims an intervention CHANGED
        something, ask "could this criterion have come out the OTHER way?" The
        criterion MUST then be a rate, mix, or before/after comparison — NEVER an
        existence test, because the thing being tested is a DIFFERENCE and an
        existence test has no difference in it. Cheap tell: if the criterion is
        satisfiable WITHOUT the intervention existing (baseline behavior already
        produces it), it measures the baseline, not the intervention — replace
        it. Second tell: skipping a rate/mix measurement in favor of a binary
        one on "sharpness" grounds trades power for comfort — the mix
        measurement is the one that can embarrass you, which is exactly why it
        is the informative one. Unambiguity is not discriminating power.
     f. Record the pre-mortem in the experience archive (Step 2.5 content).
     SKIP this step only if the prediction is about external systems
     (AWS behavior, third-party APIs) rather than project code quality —
     and even then, clause (d) still applies to the claim's own quantifiers,
     and clause (e) still applies to any claim that an intervention changed
     something.
1. Create pipeline record: `echo '<record-json>' | bash core/scripts/pipeline-add.sh` (stage defaults to discovered)
2. Add goal to aspiration: `echo '<goal-json>' | bash core/scripts/aspirations-add-goal.sh --source {source} <asp-id>`
   — the canonical GATED single-goal writer. Do NOT use the read-modify-write
   `aspirations-update.sh` whole-aspiration form here: it bypasses the
   origin-signal and goal-duplication gates, exactly as the forge block above
   documents for its own migration off it (g-115-2284). Two write paths in one
   skill, one gated and one not, means the gates fire or not depending on which
   branch the author happened to copy. (g-115-3177 spark, 2026-07-26.)
   Goal fields:
   - `participants: [agent]`
   - `skill: "/review-hypotheses --hypothesis {hypothesis_id}"`
   - `hypothesis_id` linking to the pipeline file
   - `horizon` — select using decision tree below
   - `resolves_no_earlier_than`, `resolves_by` from default windows for chosen horizon
   - `priority: MEDIUM` (default, agent can adjust)
   - `origin_signal: "idea:sq-009-<slug>"` — the origin-signal gate's allowlist
     has NO `hypothesis:` prefix, so the natural choice is REFUSED at write
     time. Spark-generated goals use `idea:`. (Observed g-115-3297.)

   **Horizon selection** (pick the FIRST that matches):
   - **long** — prediction about a trend, scaling limit, or outcome that needs weeks+ to observe
     *Example: "Storage rotation threshold will need adjustment as data volume grows"*
   - **short** — prediction about what will happen after a future change (next commit, deploy, refactor)
     *Example: "Refactoring auth caching will require service health-check interval changes"*
     Also use short when predicting: user's next likely focus area, whether a pattern holds
     across future aspirations, or consequences of a known TODO/tech-debt item
   - **session** — prediction verifiable NOW by reading current state
     *Example: "The service uses two-phase scheduling"*

   **Bias toward short/long**: If the prediction is about current state, it's probably already
   captured by a goal outcome — don't duplicate it as a session hypothesis. Prefer forming
   predictions about what WILL change or what WOULD happen IF something changes.

2.4. Move to active: `bash core/scripts/pipeline-move.sh <hypothesis_id> active`
     (Without this, the record stays in `discovered` forever — /review-hypotheses reads active-only.
     Mirrors /decompose Step 3.)

2.5. Archive hypothesis formation context:
        experience_id = "exp-{hypothesis_id}"
        Write agents/<agent>/experience/{experience_id}.md with:
            - Full context manifest content (what was actually read, not just paths)
            - Evidence consulted and reasoning chain
            - Why this confidence level was chosen
            - What would change the prediction
        echo '<experience-json>' | bash core/scripts/experience-add.sh
        Experience JSON:
            id: "{experience_id}"
            type: "hypothesis_formation"
            # `created` is SCRIPT-OWNED (experience.md schema) — stamped at add
            # time; do NOT supply it on stdin. A supplied value is silently
            # overridden, so a timestamp written here is never the one stored.
            category: "{hypothesis category}"
            summary: "Hypothesis: {claim} (confidence: {N})"
            goal_id: "{goal.id}"   # CANONICAL join key (experience.md schema). The recurring-close 4.25 canary and experience-read --goal match on THIS field — omitting it made template-written entries invisible to both (g-115-2511: writers drifted to source_goal by analogy with the rb/guardrail stores, false-firing force_experience_archival on deep closes)
            hypothesis_id: "{hypothesis_id}"
            tree_nodes_related: [nodes from context manifest]
            verbatim_anchors: [{key: "{kebab-slug}", content: "{key evidence excerpt that informed the prediction}"}, ...]
              # OBJECTS, NOT STRINGS. The validator REFUSES a list of bare excerpts:
              # {"error":"validation_failed","detail":"Each verbatim_anchor must have
              # 'key' and 'content' fields"} — and experience-add.sh exits 1, so the
              # mandated record is simply absent. This ONE line was the last site in
              # the repo still documenting the bare-excerpt shape; every sibling
              # (aspirations-execute:1016, respond:990, reflect-on-outcome:425,
              # encode-session:299) and core/config/conventions/experience.md:36
              # ("list of {key, content} objects, NOT plain strings") were already
              # correct. It produced the SAME validation_failed twice in two days on
              # two agents — g-335-09 (alpha, 2026-07-29, the incident behind
              # guard-1870) and g-115-817 (echo, 2026-07-30) — because the reader
              # follows the skill in front of them, not the convention file. Pair
              # with guard-1870: read the record back BEFORE clearing
              # force_experience_archival, or the failure is silent and the gate
              # that exists to catch it is already spent.
            content_path: "agents/<agent>/experience/{experience_id}.md"
        Set experience_ref on pipeline record:
            bash core/scripts/pipeline-update-field.sh {hypothesis_id} experience_ref "{experience_id}"
3. Move pipeline file from `discovered/` to `active/` (it's immediately actionable)
4. Log spark via `echo '<json>' | bash core/scripts/evolution-log-append.sh`

#### Self-Evolution Spark Handler

**sq-012**: "Does this outcome change how I think about my core purpose? Should my Self evolve?"

When sq-012 fires after goal completion:
1. Read `agents/<agent>/self.md` — current Self content
2. Assess: does the goal outcome suggest a refinement, expansion, or course correction?
2.5. CONTRACT CHECK (before acting on Self):
   Bash: `curriculum-contract-check.sh --action allow_self_edits`
   IF exit code 1 (not permitted):
       Log: "sq-012: Self edit blocked by curriculum stage {stage_name from JSON output}"
       Skip to step 4 — increment sparks_generated but DO NOT edit Self or write pending question
3. IF YES — classify signal strength and change size (enforced by guard-380):
   # Both `last_updated` and `last_update_trigger` MUST be set in the SAME
   # Edit so the front-matter audit trail stays accurate. Setting only the
   # trigger leaves last_updated stale and the change becomes invisible to
   # any reader checking "when was Self last touched." Mirror sites that
   # MUST stay in sync with this pattern: respond/SKILL.md (user-correction),
   # felt-sense-checkin/SKILL.md (Material lane), encode-session/SKILL.md
   # (Lane 7). After Phase 7b collapse, this site no longer has a manual
   # forged-notification invocation — evolution-complete.py (Phase 5) handles
   # decisions-board posting AND user email for material self edits automatically.
   a. STRONG signal + COSMETIC change (wording, typo, formatting only):
      Edit `agents/<agent>/self.md` — update body AND front matter:
        last_updated: <today (YYYY-MM-DD)>
        last_update_trigger: self_evolution
      # The Phase 2 hooks (evolution-prepare -> evolution-record) captured the
      # Edit as a self-evolution.jsonl stub with status=awaiting_completion.
      # Finalize via the canonical primitive (cosmetic edits auto-skip email):
      Bash: bash core/scripts/evolution-complete.sh \
          --revision-id <stub-rev-from-self-evolution.jsonl> \
          --reasoning "<>=80-char rationale citing sq-012 cosmetic signal>" \
          --signal-source sq-012 \
          --signal-evidence '[{"type":"spark_question","id":"sq-012","outcome":"cosmetic"}]'
      Log: "SELF EVOLUTION (cosmetic, audited via self-evolution stream): {summary}"
   b. STRONG signal + MATERIAL change (new/removed drive, principle, role,
      agent-provisionable action, or multi-paragraph rewrite — when in doubt,
      treat as material):
      Edit `agents/<agent>/self.md` — update body AND front matter:
        last_updated: <today (YYYY-MM-DD)>
        last_update_trigger: self_evolution
      # The Phase 2 hooks (evolution-prepare -> evolution-record) captured the
      # Edit as a self-evolution.jsonl stub with status=awaiting_completion.
      # Finalize via the canonical primitive (Phase 5 auto-posts decisions board
      # AND auto-emails user for material self edits — no manual forged-skill
      # invocation needed here):
      Bash: bash core/scripts/evolution-complete.sh \
          --revision-id <stub-rev-from-self-evolution.jsonl> \
          --reasoning "<>=80-char rationale citing sq-012 signal + goal outcome>" \
          --signal-source sq-012 \
          --signal-evidence '[{"type":"spark_question","id":"sq-012","outcome":"confirmed"}]'
      Log: "SELF EVOLUTION (material, audited via self-evolution stream): {summary}"
   c. WEAK / uncertain signal: DO NOT edit self.md on sq-012 alone.
      Record the tentative signal for /reflect-on-self or /fresh-eyes-review
      to cross-reference against other signals before acting.
      Log: "sq-012: weak signal — deferred to cross-signal review"
   (No pending-question pre-approval path — guard-380 replaced it with
   post-notification on 2026-04-22. Pre-approval is no longer written here.)
4. Increment `sparks_generated` on the spark question

#### Data Acquisition Spark Handler

**sq-c05**: "Does my knowledge tree reference external data sources, systems, files, APIs, or environments that I haven't directly accessed? What would I learn from obtaining that data?"

When sq-c05 fires after goal completion:
1. Bash: world-cat.sh knowledge/tree/_tree.yaml  # scan node summaries for data source references
2. Read entity_index — look for external system references (SSH endpoints, file paths, APIs, databases)
3. Identify accessible but unaccessed data sources
4. IF found:
   invoke /create-aspiration from-self (Phase B will pick up the data acquisition opportunity)
5. Increment `sparks_generated` on the spark question

#### Memory Curation Spark Handlers

**sq-014**: "Did completing this goal make any of our existing STRATEGIES, GUARDRAILS, or PATTERN SIGNATURES obsolete or irrelevant?"

When sq-014 fires after goal completion:
1. Identify the completed goal's category/domain
2. Scan that category for strategies, guardrails, and pattern signatures
3. For each item: "Does this goal's outcome make this artifact obsolete or irrelevant?"
4. If YES to any: invoke `/reflect --curate-memory` scoped to that category
5. Increment `sparks_generated` on the spark question

**sq-c04**: "Is there knowledge in our memory tree that CONTRADICTS what we just learned, or that we now know is STALE?"

When sq-c04 fires after goal completion:
1. Load tree nodes for the completed goal's category using `tree-read.sh --leaves-under {category_key}`
2. For each leaf node with articles: check if key insights conflict with the goal's outcome
3. If contradiction found: flag article for re-research:
      echo '"<article_key> contradicts goal outcome: <summary>"' | wm-append.sh knowledge_debt
4. If a belief is affected: weaken it via existing belief weakening logic (Step 7.6 or equivalent)
5. Increment `sparks_generated` on the spark question

#### Work Discovery Spark Handler

**sq-013**: "Did executing this goal reveal actionable work — a requirement, dependency, follow-up, fix, capability gap, or opportunity — that isn't already tracked?"

When sq-013 fires after goal completion:
1. Classify the discovery: `requirement` | `dependency` | `follow-up` | `fix` | `capability_gap` | `opportunity`
2. Determine target aspiration:
   a. Default: current aspiration (if the work fits its scope/motivation)
   b. If out of scope: scan active aspirations (`aspirations-read.sh --summary`)
      for one whose motivation covers this work → use that aspiration
   c. If no existing aspiration fits: invoke `/create-aspiration` with the discovery
      context (title, description, category) — skip to step 9 (log + increment)
3. Read target aspiration: `bash core/scripts/aspirations-read.sh --id <target-asp-id>`
4. Compute next goal ID: find max `g-NNN-NN` sequence in target's goals, increment by 1
5. Build goal object:
   - `id`: computed next goal ID
   - `title`: concise description of the discovered work
   - `description`: what was discovered and why it matters
   - `status`: `pending`
   - `skill`: appropriate skill for the work
   - `priority`: `dependency`/`requirement`/`fix` → `HIGH`, `follow-up`/`capability_gap` → `MEDIUM`, `opportunity` → `LOW`
   - `verification`: `outcomes` + `checks` + `preconditions`
   - `discovered_by`: the completed goal ID that triggered this spark
   - `discovery_type`: the classification from step 1
   # `origin_signal` is gate-required — origin-signal-gate.py rejects
   # agent-sourced goals without one. The mapping below codifies the
   # discovery_type → origin_signal correspondence so future passes pick
   # the right value without re-deriving it; the table is the single
   # source of truth for sq-013-filed goals.
   - `origin_signal`: derive from `discovery_type` (use the prefix form with
     a short slug — typically `g-NNN-NN-<short-tag>` or the discovered work's
     identifier):
       `requirement` | `dependency` | `fix`  → `"maintain:<tag>"` or `"unblock:<tag>"`
       `follow-up`   | `capability_gap`      → `"investigate:<tag>"` or `"idea:<tag>"`
       `opportunity`                         → `"idea:<tag>"`
5.5. **Quality gate for project+ aspirations** (scope-aware goal addition):
   IF target aspiration's scope is "project" or "initiative"
   AND discovery_type NOT in ("fix", "dependency"):  # cognitive primitives exempt
     - `description` MUST include: what was discovered, why it matters, and brief tree consultation
       (`Bash: tree-find-node.sh --text "{goal.title}" --leaf-only --top 1` — enrich with existing knowledge)
     - `verification.outcomes` MUST include meaningful success criteria (not just "task completed")
     - For `capability_gap` or `opportunity` discoveries: consider whether a companion
       test/verification goal should also be created (same pattern as Step 4c in create-aspiration)
6. Add new goal to the target aspiration's `goals` array
7. Pipe the updated aspiration JSON to: `bash core/scripts/aspirations-update.sh --source {source} <target-asp-id>`
8. If discovery type is `dependency`: add new goal ID to `blocked_by` on dependent goals
9. Log spark event: `echo '{"event":"spark","details":"sq-013: Goal <completed-id> discovered <type>: <title> → <target-asp-id>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
10. Increment `sparks_generated` on the spark question

#### Integration Path Coverage Spark Handler

**sq-019**: "Does the test coverage verify the INTEGRATION PATH (trigger -> handler -> side effect), or only the extracted function in isolation?"

When sq-019 fires after goal completion:
1. Did this goal produce or modify code (Edit/Write to source files)? If no → SKIP (not applicable)
2. Trace the integration path from the change point:
   - What triggers the changed code? (API call, event bus message, scheduler, user action)
   - What side effects does it produce? (state change, message publish, file write)
   - Is there a test that exercises trigger → changed code → side effect?
3. IF no integration path test exists:
   Create investigation goal (via Cognitive Primitives):
   - Title: `"Investigate: integration path coverage for {changed module}"`
   - Priority: MEDIUM, category: from goal's category
   - Verification outcome: "Integration path traced with test gap documented or closed"
   - `discovered_by`: the completed goal ID
4. ELIF integration path test exists but is incomplete:
   Create idea goal: `"Idea: extend integration test for {module} to cover {gap}"`
5. ELSE: integration path is covered — no spark generated, SKIP to step 7
6. Log spark event: `echo '{"event":"spark","details":"sq-019: Goal <completed-id> integration path check for <module>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
7. Increment `sparks_generated` on the spark question ONLY if step 3 or 4 created a goal

#### Aspiration Generation Spark Handler

**sq-007**: "Does this outcome justify a NEW ASPIRATION (multi-goal initiative) — not just a single follow-up Idea/Investigate goal?"

When sq-007 fires after goal completion:
1. Assess: does the goal's outcome suggest an entirely new direction that doesn't fit within any existing aspiration?
2. If YES: invoke `/create-aspiration from-self` — the skill reads Self, scans for purpose gaps, and generates aligned aspirations
3. Log spark event: `echo '{"event":"spark","details":"sq-007: Goal <completed-id> suggested new aspiration direction: <brief description>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`
4. Increment `sparks_generated` on the spark question

#### sq-015: Meta-Improvement Spark

**Handler for sq-015** — "Did this outcome suggest a better improvement PROCEDURE?"

When sq-015 fires after goal completion:
1. Bash: meta-cat.sh improvement-instructions.md
2. Compare: did the approach used in this goal deviate from the documented procedure?
   - Deviated AND succeeded: procedure may be outdated → note for evolve phase
   - Deviated AND failed: procedure may be correct → reinforcing signal
   - Followed AND succeeded: procedure validated → reinforcing signal
   - Followed AND failed: procedure may need revision → note for evolve phase
3. IF meta-insight found:
   - Append to meta/meta-log.jsonl via meta-log-append.sh:
     {"date":"<today>","event":"meta_spark","goal_id":"<goal.id>",
      "insight":"<what the meta-insight is>","procedure_match":"<deviated|followed>",
      "outcome":"<succeeded|failed>"}
   - Log: "META SPARK: {insight} from {goal.id}"
4. Bash: spark-questions-increment.sh sq-015 sparks_generated

#### sq-018: Verify-Learning Maintenance Spark

**Handler for sq-018** — "Did this work suggest a NEW test/check/assertion to add to /verify-learning?"

This catches regressions in framework-relevant code (core/scripts, core/config,
mind_api/src, .claude/skills, .claude/rules, .claude/settings.json) by proposing
an explicit assertion in /verify-learning Step 3 BEFORE the next regression hits.

When sq-018 fires after goal completion:
1. SCOPE FILTER — was THIS goal framework-touching?

   NEVER scope on a reflog-relative range (guard-2001). `HEAD@{1}..HEAD` and
   `HEAD~1..HEAD` answer "what moved since the previous reflog position", which
   is not this goal under EITHER of the two conditions that dominate the loop:
   after iteration-push.sh's `merge --no-edit` the range spans the merge and
   reports a PARTNER's framework files (a FALSE POSITIVE — it invites a
   check-goal for code this goal never touched, and severity scales with how far
   behind the branch was); before this goal's own commit exists it reports the
   PREVIOUS goal's files. Four independent reproductions across two agents; the
   two framings were filed separately as g-115-3265 and g-115-3806 and are one
   defect (g-115-3539).

   Bash:
     GID="<goal.id>"
     # (a) The goal's OWN commits. iteration-commit.sh stamps every subject as
     # `type(goal-id): title`, so git history IS the per-goal commit ledger —
     # the same resolution post-state-update-gate.sh uses (L151-159), so no
     # second resolver is introduced. `diff-tree` on a merge emits nothing by
     # design, so a merge can never leak a partner's files in.
     # RECURRING GOALS match MORE THAN ONE commit here — a recurring goal-id is
     # reused every cycle, so the 48h window spans several of its runs (measured:
     # g-001-01 matched 3 commits / 17 files). That is deliberate and bounded:
     # it unions the SAME goal's lineage, so the residual over-fire is
     # same-subject and small. Same trade post-state-update-gate.sh documents at
     # L144-149 — a review producer biases to over-fire, never to silently drop.
     # CORRECTED 2026-07-30 (g-115-1538, alpha): this comment said "never a
     # partner's", which is FALSE for a SHARED recurring goal. A recurring
     # goal-id belongs to the queue, not to an agent, so whichever agent closes
     # a cycle stamps ITS commits with that id — measured on g-115-1538, where
     # 5 matched commits spanned alpha's run AND echo's earlier same-day run,
     # putting `agents/echo/*` in scope. The BOUND still holds (every file came
     # from that one goal's own closes, not from unrelated partner work) and the
     # framework filter returned empty, so no false check was proposed. But do
     # not read the union as single-agent: on a shared recurring goal it is
     # single-GOAL, multi-agent.
     SHAS=$(git log --fixed-strings --grep "(${GID}):" --format=%H -n 50 --since=48.hours 2>/dev/null)
     if [ -n "$SHAS" ]; then
       SCOPE=$(printf '%s\n' "$SHAS" | while IFS= read -r s; do [ -n "$s" ] && git diff-tree --no-commit-id --name-only -r "$s" 2>/dev/null; done)
     else
       # (b) No commit yet — LEGITIMATE, not an error: guard-1320 fires this
       # spark inline after verify, BEFORE state-update runs iteration-commit.
       # The working tree vs HEAD is then this goal's scope.
       SCOPE=$( { git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } )
     fi
     printf '%s\n' "$SCOPE" | sed '/^$/d' | sort -u \
       | grep -E '^(core/(scripts|config)|mind_api/src|\.claude/(skills|rules)|\.claude/settings\.json)' | head -20

   IF empty: SKIP — do NOT increment sparks_generated. Pure non-framework
   goals (domain-specific code, application logic, product features) have
   no /verify-learning surface.

   KNOWN GAP, stated here because this is where a reader checks coverage:
   world/ and meta/ are NOT covered, and no git command can cover them — they
   are external gitignored paths (.gitignore `/world/`), so
   `git ls-files 'world/conventions/*'` returns 0 on every box, for every agent.
   This regex used to LIST `world/conventions`, which is WORSE than omitting it:
   a present-but-unmatchable arm passes the coverage review that a missing arm
   fails, which is how it survived three reviews of this same filter.
   Do NOT reach for world/changelog.jsonl as the replacement — MEASURED
   2026-07-30 (alpha, cc-04) and FALSIFIED: that store holds ZERO rows whose
   file starts with `conventions/` in its entire 35,840-row history, because
   changelog rows are appended by _fileops.py on SCRIPT-mediated store writes
   and convention files are edited with the Edit tool. (The query is sound —
   positive control `changelog-read.sh --since 48h` returns 14,432 rows — so
   this is a coverage fact, not a probe artifact.) Note `knowledge/` IS covered
   there, since tree writes go through tree scripts. Full trace: rb-5942.

2. For each changed framework file, identify a check that catches the same regression:
   - New script invariant   → grep-based assertion ("file X must contain Y")
   - New file expected      → existence check (test -f / test -d)
   - New behavior           → command_check + expected stdout/stderr
   - New convention rule    → assertion that the documented rule holds in code
   - New hook / integration → run-once verification that wiring is live

2.1. ROUTE BEFORE YOU FILE — two questions that end the spark with NOTHING, which is
   a first-class outcome and was undocumented until 2026-07-30. Ask both:

   Q1. IS THIS INVARIANT ALREADY PINNED BY A SUITE-COLLECTED TEST? If the same goal
       (or any prior one) added a pytest/shell test that fails when this regression
       returns, a grep-based verify-learning check DUPLICATES a test that already
       runs in the full suite — with strictly less power, since a grep cannot express
       a negative assertion or a mutation-sensitivity pin.
   Q2. WHICH SUITE OWNS THE FILE THIS INVARIANT LIVES IN, and is /verify-learning
       actually that suite? /verify-learning is FRAMEWORK-scoped. An invariant about
       a DOMAIN script (`world/scripts/**`) belongs in the domain's own suite per
       `.claude/rules/domain-free-examples.md` — filing it here adds to a sealed lane
       AND puts the check where it does not belong.

   If Q1 is yes, or Q2 routes elsewhere: SKIP steps 2.5 and 3 entirely, and do NOT
   run steps 4-5 as written. Log ONE accurate line instead — "sq-018: no spark —
   <invariant> is pinned by <test>" or "... routed to <suite>, not /verify-learning" —
   and do NOT increment sparks_generated, mirroring step 1's scope-filter SKIP. Do NOT
   file, and do NOT append to a drain goal.
   (Step 4's template asserts "proposed verify-learning check for <file>", which is
   FALSE on a decline, and step 5 would credit sq-018 with a spark it did not produce,
   inflating the yield_rate that the retire/promote review reads. Caught on this step's
   FIRST live firing — g-115-3936's own close, where the route landed on pytest — so
   following "continue to steps 4-5" verbatim would have written a false evolution-log
   line and a phantom spark. A decline is a first-class outcome; it must not be
   book-kept as a spark.)

   WHY THIS EXISTS: four consecutive sq-018 evaluations DECLINED to file — bravo
   g-115-3934 (07-29), foxtrot g-115-4062 (07-30), bravo g-115-4084 (07-30), alpha
   g-115-4104 (07-30) — every one of them for a reason on this list, and every one
   re-derived from scratch because the handler encoded only two outcomes: file a
   singleton, or file a drain. Declining was correct all four times and cost four
   agents the same deliberation. This step is upstream of the lane-depth counter,
   which is the point: 2.5 can only choose HOW to file, so it can never reduce
   arrivals. Cutting them at the source is the half no drain round can reach.

2.5. LANE-DEPTH GATE (g-115-3468, 2026-07-28) — make this producer self-limiting.
   sq-018 fires on EVERY goal close and step 3 files a NEW Maintain goal each time, with
   no awareness of how many already wait. The consumer is a single starved MEDIUM goal that
   loses every scoring contest, so the lane grows MONOTONICALLY: 11 pending on 2026-07-27,
   18 later that day, 28 on 2026-07-28. Worse, it is SELF-SEALING — every goal of this shape
   inherits the same boilerplate, so the goal-duplication gate's structural_overlap blocks
   each NEW arrival against its siblings. Both alpha and zeta hit that block and correctly
   declined to override. So the lane could only grow via override and could not drain via
   singletons. That is the additive-only ratchet `.claude/rules/learning-philosophy.md`
   rule 5 names, and it is a PRODUCER defect, not 28 independent goal failures.

   FOUR MEASURED DEFECTS IN THIS GATE, all fixed below (g-115-3936, six agents,
   2026-07-29..30). Read them before changing the probe — each one made the gate
   report confidently on something it could not see:

   (a) THE COUNTER WAS NEAR-DISJOINT FROM THE POPULATION IT MODELS. It counted
       `origin_signal ^= maintain:sq-018`; what actually seals the lane is any open
       goal the duplication gate blocks against — i.e. anything citing
       verify-learning. Measured 16% visibility (6/38), then 20% (9/45), then
       (bravo, cc-05, 2026-07-30) narrow=10 vs content-derived=26 with an
       INTERSECTION OF ONLY 6 and a union of 30. So it was not undercounting a
       nested subset; it was counting a different set. guard-1802 one layer up: a
       self-limiting predicate narrower than the gate that REFUSES its output.
   (b) THE DRAIN PROBE COULD NOT SEE A DRAIN THAT WORKED. It filtered to open
       statuses, so "drain filed, executed, completed" and "nobody ever touched
       this lane" both returned empty — and the ELSE branch below reads that as
       "NOTHING is draining it." Success removed the evidence of success, so the
       better the drain cadence worked the more confidently the gate re-prescribed
       it. Seven drain rounds were filed this way (rb-5977).
   (c) THE origin_signal COLLIDED WITH ITSELF BY CONSTRUCTION. It templated on
       LANE_DEPTH, which is not monotonic — it drops on each drain and re-climbs.
       So the gate regenerated an identical signal every time the lane re-reached a
       depth it had already drained at, and `origin_signal_completed` refused it
       every time. The gate could never file a drain at any depth it had drained at
       before. Combined with (a): the counter picked a branch, and that branch could
       not write.
   (d) THE THRESHOLD WAS TUNED AGAINST THE NARROW COUNT. 8 was chosen when the
       counter read 6. On the wide count it is meaningless. Note the direction:
       widening makes this gate MORE willing to consolidate, not less — which is
       correct, because the lane is genuinely sealed, but it means the DECLINE
       branch below (not the threshold) is now the load-bearing control.

   Measure the lane, then route. ONE read supplies all four signals — do not add a
   second call:
   Bash: bash core/scripts/aspirations-read.sh --source world --id asp-115 2>/dev/null | py -3 -c "
   import sys,json,datetime
   raw=sys.stdin.read(); i=raw.find('{')
   d=json.loads(raw[i:])
   goals=d.get('goals') or []
   open_=[g for g in goals if g.get('status') in ('pending','in-progress')]
   # (a) WIDE counter: the population the duplication gate actually blocks against.
   #     Text-based, matching the refusing gate, NOT an origin_signal proxy.
   def cites(g):
       return 'verify-learning' in ((g.get('title') or '')+' '+(g.get('description') or '')).lower()
   lane=[g for g in open_ if cites(g)]
   narrow=[g for g in open_ if (g.get('origin_signal') or '').startswith('maintain:sq-018')]
   isdrain=lambda g:(g.get('origin_signal') or '').startswith('maintain:drain-verify-learning-check-lane')
   drain=[g for g in open_ if isdrain(g)]
   # (b) A drain that COMPLETED recently means the lane is being serviced. An
   #     open-only probe cannot distinguish that from 'never drained'.
   cut=(datetime.datetime.now()-datetime.timedelta(hours=48)).isoformat()
   recent=[g for g in goals if isdrain(g) and g.get('status')=='completed'
           and str(g.get('completed_at') or '') >= cut]
   print('LANE_DEPTH=%d' % len(lane))
   print('LANE_NARROW=%d' % len(narrow))
   print('DRAIN_GOAL=%s' % (drain[0]['id'] if drain else ''))
   print('DRAIN_RECENT=%s' % ','.join(g['id'] for g in recent))
   "

   Report BOTH counts in the log line. LANE_NARROW is kept only so a reader can see
   how far the old proxy diverged; it must never drive a branch again.

   IF LANE_DEPTH < 15:
       Proceed to step 3 — file a normal singleton. A shallow lane drains fine
       one-at-a-time and singletons keep per-check provenance sharpest.
       (15, not 8: 8 was calibrated against the narrow counter's ~6. Against the
       wide population — observed 26, 30, 38, 45 — anything below ~15 is a lane
       that singletons can still clear.)
   ELIF DRAIN_GOAL is non-empty:
       # APPEND to the open drain goal instead of filing a 29th singleton. This is
       # exactly what alpha and zeta each did by hand when the duplication gate
       # blocked them; step 2.5 makes it the default rather than a lucky judgment call.
       Read the drain goal's current description, then append a dated block naming
       the proposed check, its assertion, and the source goal id:
       Bash: bash core/scripts/aspirations-update-goal.sh --source world <DRAIN_GOAL> description "<existing description>

       -- ADDENDUM (<agent>, <today>, sq-018 spark on <goal.id>) --
       CHECK TO ADD: <one-line assertion>
       WHY: <what regression it catches>"
       Log: "sq-018: lane depth <LANE_DEPTH> (narrow <LANE_NARROW>) >= 15 — appended to open drain goal <DRAIN_GOAL> instead of filing singleton #<LANE_DEPTH+1>"
       SKIP step 3 (no new goal), then continue to steps 4-5 normally.
   ELIF DRAIN_RECENT is non-empty:
       # (b) No drain is OPEN, but one COMPLETED within 48h — so the lane IS being
       # serviced and filing an (N+1)th round is the treadmill, not the fix. The old
       # open-only probe could not reach this branch at all: a drain that ran and
       # closed looked exactly like a lane nobody had touched, so success erased its
       # own evidence and the gate re-prescribed the drain it had just been given.
       # ORDER IS LOAD-BEARING: this sits BELOW the append branch on purpose. When a
       # drain is open, appending keeps the check; declining would discard it. Only
       # when there is nowhere to put it does declining become the right answer.
       Record the check where its subject already has an owning suite (the ROUTING
       question in step 2.1), else carry it in your own goal's close notes.
       Log: "sq-018: lane depth <LANE_DEPTH> (narrow <LANE_NARROW>), no open drain, but <DRAIN_RECENT> completed within 48h — declining to file an (N+1)th drain round"
       SKIP step 3, then continue to steps 4-5 normally.
   ELSE:
       # Lane is deep, no drain is open, and none completed in 48h. File the DRAIN
       # goal (not another singleton), carrying this check as its first item.
       # (c) origin_signal MUST NOT encode LANE_DEPTH. It did until 2026-07-30, and
       # because LANE_DEPTH falls on each drain and re-climbs, the gate regenerated
       # an identical signal at every repeat depth — which origin_signal_completed
       # then refused against the earlier round, forever. Use a DATE, which is
       # monotonic, so round N is never mistaken for round N-1:
       #   maintain:drain-verify-learning-check-lane-<YYYYMMDD>
       File a drain goal titled
       "Drain the stuck verify-learning check lane — <LANE_DEPTH> open goals cite it, batch them in one pass"
       with priority HIGH (a blocked lane outranks any single check it contains),
       participants ["agent"], category framework-hygiene, and a description that
       enumerates the pending sibling ids + this check. Then continue to steps 4-5.
       State in the description that LANE_DEPTH is the WIDE count and name the
       narrow count beside it — a successor who sees only one number cannot tell
       which population the round was scoped against, which is how rounds 1-6 each
       drained a subset and reported the lane clear.

   The threshold is 15 on the WIDE count: below it, independent singletons are
   individually claimable and carry the sharpest provenance; at or above it, the
   duplication gate has effectively sealed the lane and consolidation is the ONLY
   path that shrinks it. But treat the threshold as the weaker control — on the wide
   population the gate will nearly always be above it, so what actually prevents a
   treadmill is the DRAIN_RECENT decline branch plus step 2's routing question.

3. FILE the check as a Maintain-style goal under asp-115 (framework hygiene). The goal
   exists for SCOPE-DISCIPLINE -- adding the check inline during THIS goal's spark would
   be scope creep (implementation-discipline.md) -- NOT because the agent lacks authority.
   `.claude/skills/verify-learning/SKILL.md` is an agent-editable framework file
   (`.claude/skills/**`); its executor applies the check by DIRECT EDIT, routing
   `participants: [agent]`, with care (mirror an existing Step-2 sibling check, validate
   before/after). Do NOT route to the user -- user-gating a verify-learning patch is the
   g-115-792 anti-pattern (`.claude/rules/capability-before-user.md`; rb-1993).

   # origin_signal MUST come from the canonical list enforced by
   # core/scripts/origin-signal-gate.py. Bare "sq-018" is rejected — use the
   # "maintain:" prefix form with a short tag. The tag MUST be UNIQUE per
   # goal (e.g. "maintain:sq-018-<what-the-check-covers>") — the
   # goal-duplication gate's origin_signal strategy exact-matches, so a
   # fixed literal here makes every later sq-018 goal false-block against
   # every earlier one (observed 2026-07-11: marker-check goal blocked
   # against the unrelated g-115-1993 evolution-capture checks).
   # aspirations-add-goal.sh reads JSON from STDIN (BODY="$(cat)" at line 103);
   # positional JSON args are silently discarded.
   echo '{"title":"Maintain: add verify-learning check for <file>",
      "description":"What changed during <goal.id>, why a check is needed, suggested check form",
      "status":"pending","priority":"MEDIUM","category":"framework-hygiene",
      "participants":["agent"],
      "discovered_by":"<goal.id>",
      "discovery_type":"capability_gap",
      "origin_signal":"maintain:sq-018-<distinct-tag-for-this-check>"}' \
     | bash core/scripts/aspirations-add-goal.sh --source world asp-115

4. Log: `echo '{"event":"spark","details":"sq-018: Goal <goal-id> proposed verify-learning check for <file>","date":"<today>"}' | bash core/scripts/evolution-log-append.sh`

5. Bash: spark-questions-increment.sh sq-018 sparks_generated

#### sq-016: First-Principles Spark

**Handler for sq-016** — "Did this goal's approach rest on inherited assumptions rather than verified ground truth?"

When sq-016 fires after goal completion:
1. Identify the goal's execution approach and framing
2. Surface 2-3 assumptions embedded in the approach:
   - What was taken for granted?
   - What conventional wisdom was applied without verification?
   - What "standard approach" was used because it is standard, not because it was derived?
3. For each assumption, classify:
   - **VERIFIED**: agent has direct evidence for this assumption (from tree, experience, or execution)
   - **INHERITED**: assumption came from documentation, convention, or prior framing without independent verification
   - **UNTESTED**: assumption was neither verified nor consciously inherited — it was implicit
4. IF any assumption is INHERITED or UNTESTED:
   a. Check existing reasoning bank for entries about this assumption:
      Bash: reasoning-bank-read.sh --category {goal.category}
   b. IF no existing entry covers this assumption:
      Create reasoning bank entry via reasoning-bank-add.sh:
        # `id` and `created` auto-set — omit both; capture from stdout.
        title: "Assumption: {concise description of the inherited assumption}"
        type: failure
        category: goal's category
        content: "Goal {goal.id} used this assumption without verification: {assumption}. The approach {did/did not} succeed, but the assumption remains unverified. Ground truth check: {what would need to be true for this to be verified}."
        applies_to: any  # surfaced assumptions are methodological — they apply across domains
        when_to_use: "{conditions where this assumption is relevant}"
        source_goal: goal.id
        source_horizon: micro  # g-303-34 / rb-876 attribution gap: first-principles spark RBs are micro-horizon-originated; stamping makes the origin traceable. Passthrough field — reasoning-bank-add.sh forwards the full JSON, apply_defaults preserves extras, and the rb validator has no unknown-field gate, so this persists without a store-contract change.
        tags: ["first-principles", "inherited-assumption"]
      Log: "FIRST PRINCIPLES: Surfaced inherited assumption from {goal.id}: {assumption}"
   c. IF assumption is UNTESTED AND goal succeeded:
      # Most dangerous case — success reinforces unchecked assumptions
      Create a micro-hypothesis in working memory. resolves_when + consumer are
      REQUIRED (g-303-34, zeta audit g-303-14): the dominant micro-hyp failure is
      NON-RESOLUTION + NO-CONSUMER (71% never settle, 0% consumed). A micro-hyp
      that cannot name BOTH a concrete later settling signal AND a downstream
      consumer is noise — do NOT file it. resolves_when = the concrete later
      signal that settles it (keep ASCII — this JSON is piped to wm-append.sh).
      consumer = which decision/goal/encoding will use the resolution.
      echo '{"claim":"Goal {goal.id} succeeded despite untested assumption: {assumption}. This assumption may fail when {condition}.","confidence":0.40,"source_goal":"{goal.id}","source_step":"sq-016","horizon":"session","resolves_when":"next {goal.category} goal that relies on {assumption}: observe whether it fails under {condition}","consumer":"reasoning-bank entry under {goal.category} gating future reliance on {assumption} if it fails"}' | Bash: wm-append.sh micro_hypotheses
      Log: "FIRST PRINCIPLES -> HYPOTHESIS: untested assumption '{assumption}' may fail under {condition}"
   # Only count as spark if at least one assumption was surfaced (step 4b or 4c fired)
   Bash: spark-questions-increment.sh sq-016 sparks_generated

#### Failure Stepping-Stone Spark Handler (OMNI-EPIC-inspired)

**sq-c08** (candidate): "Did this goal fail in a way that suggests an easier stepping-stone variant?"

Inspired by OMNI-EPIC's failure-informed difficulty adjustment (arXiv 2405.15568):
when a task fails, generate an easier variant rather than retrying or abandoning.
This creates natural curriculum progression without explicit difficulty parameters.

When sq-c08 fires after a FAILED goal:
1. Analyze the failure mode:
   - Was it too ambitious? (scope exceeded current capability level)
   - Was it missing prerequisites? (knowledge gap, infrastructure dependency)
   - Was it unclear? (poorly specified, ambiguous verification criteria)
   - Was it blocked by external factors? (user action needed, service unavailable)

2. If the failure suggests a simpler version would succeed:
   Generate a stepping-stone goal that:
   - Addresses the SAME domain/category as the failed goal
   - Has reduced scope (narrower question, smaller artifact, fewer components)
   - Includes the prerequisite the original was missing
   - Explicitly references the failed goal in description:
     "Stepping stone for {failed.title} — {what makes this version simpler}"

3. Add via aspirations-add-goal.sh to the same aspiration:
   ```
   echo '{"title":"Stepping stone: {simpler variant title}","description":"Easier variant of {failed.id}: {failed.title}. {what makes this simpler}. Original failure mode: {failure_analysis}.","priority":"{same as failed}","category":"{failed.category}","participants":["agent"],"origin_signal":"unblock:{failed.id}"}' | Bash: aspirations-add-goal.sh --source {source} {asp.id}
   ```

4. Log: `echo '{"date":"<today>","event":"stepping_stone_created","details":"Easier variant of {failed.id} → {new.title}","trigger_reason":"sq-c08 failure stepping-stone"}' | bash core/scripts/evolution-log-append.sh`

5. Bash: spark-questions-increment.sh sq-c08 sparks_generated

**When NOT to create a stepping stone:**
- Failure was due to infrastructure issues (transient — retry is appropriate)
- Failure was due to blocked_by dependency (wait, don't simplify)
- The goal is already a stepping stone (avoid infinite regression)
- The failed goal's title starts with "Stepping stone:" → SKIP

### Aspiration-Level Spark (when entire aspiration completes)
```
Ask these 3 questions:
1. What did we learn from completing this entire aspiration?
   → Write reflection to journal
2. Does this completion unlock a new strategic direction?
   → YES: Create new aspiration via gap analysis
3. Should the system's self-model update?
   → YES: Update meta/meta-knowledge/_index.yaml
4. Did completing this aspiration teach us something about HOW we generate aspirations?
   → IF yes:
       Bash: curriculum-contract-check.sh --action allow_meta_edits
       IF permitted: Read via meta-read.sh and update via meta-set.sh: aspiration-generation-strategy.yaml with learned heuristic.
     Bash: echo '{"date":"<today>","event":"aspiration_meta_learning","aspiration":"<asp-id>","insight":"<insight>"}' | meta-log-append.sh

Replacement aspiration generation is handled by Phase 7 archival in aspirations/SKILL.md
(with --plan for full planning treatment). Do NOT duplicate generation here.
```

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
