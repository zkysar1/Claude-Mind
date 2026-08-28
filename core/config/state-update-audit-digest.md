# State-Update Scripted Audit Pass (Steps 8.8-8.10) — Digest

Extracted from `.claude/skills/aspirations-state-update/SKILL.md` (g-115-7044).
That SKILL.md is on the always-loaded hot path and had grown to 72,080 B, past
the 65,536 B injection budget; this section was 19,891 B of it — 28% of the file
for a block the LLM needs only when it actually reaches Steps 8.8-8.10.

The content below is UNCHANGED from the SKILL.md. It is the authoritative spec
for the scripted audit pass; the SKILL.md now points here rather than carrying
it inline. Do NOT re-inline it — the reload cost is the whole point of the
extraction (same rationale as `aspirations-loop-digest.md` and
`iteration-close-digest.md`).

---

### Scripted Audit Pass (Steps 8.8-8.10)

`state-update-audit.sh run-all` runs velocity (8.8), backpressure (8.85),
temporal credit (8.9), and relative advantage (8.10) in one Python pass —
pure arithmetic, no LLM judgment. This is the ONLY path. The previous
toggle + LLM-fallback pseudocode was removed 2026-04-20 — the script is
the single source of truth. Any bug in `state-update-audit.py` MUST be
fixed in the script, not patched around by reintroducing a shadow LLM
path here.

Steps 8.5 (Actionable Findings Gate), 8.75 (Execution Reflection), 8.76
(Skill Quality), and 8.11 (Execution Feedback) stay on the LLM path —
those require judgment that `run-all` does not cover.

```
IF outcome_class != "routine":
    # LLM gathers four inputs from this iteration's state. These MUST be
    # passed — omitting any flag silently produces learning_value=0
    # (state-update-audit.compute_learning_value uses argparse defaults).
    # The script owns the arithmetic; the LLM owns the inputs.
    tree_updated    = true iff Step 8 wrote an insight node to the tree
    artifacts_count = count(reasoning_bank + guardrails + pattern_sigs created this iteration)
    encoding_score  = from Step 2.7 encoding gate if it fired, else 0.0
    findings_count  = count(Step 8.5 Actionable Findings gated this iteration)

    Bash: bash core/scripts/state-update-audit.sh run-all \
        --goal {goal.id} \
        --outcome-class {outcome_class} \
        --category {goal.category} \
        --experience-id {experience_id} \
        [--tree-updated if tree_updated] \
        --artifacts-count {artifacts_count} \
        --encoding-score {encoding_score} \
        --findings-count {findings_count} \
        [--exploration if goal.execution_mode == "exploration"]
    # Reads the JSON. Flags to watch for (each prefixed by subcommand):
    #   velocity:impk_snapshot_failed  → meta-impk.sh unreachable; investigate stderr
    #   backpressure:rollbacks_applied → one or more meta-strategy fields
    #                                    auto-reverted — note in output
    #   backpressure:check_failed      → meta-backpressure.sh unreachable; investigate stderr
```

```
# ── Step 8.86: Self/Program/Skill/Rule Evolution Backpressure ───────
# Per world/conventions/self-program-evolution.md
# Runs AFTER 8.10 (meta-strategy backpressure) and BEFORE 8.77.
#
# Re-samples the metric vector for every active evolution monitor
# (monitor_kind ∈ {self_evolution, program_evolution, skill_evolution,
#  rule_evolution}). Fires auto-rollback when consecutive_below_baseline
# crosses the per-kind regression_window. Fires graduation when
# consecutive_above_baseline crosses graduation_window.
#
# Skips entirely when outcome_class == "routine" — routine goals do not
# perturb the metric vector enough to be informative, and sampling on
# every routine iteration would flood the script. The §14.5 windows
# (6-15 iterations) are sized for deep/exploration iterations only.

IF outcome_class == "routine":
    SKIP silent

Bash: bash core/scripts/meta-backpressure.sh evolution-check
    # Returns JSON with:
    #   rollback_actions[]    → each: {revision_id, monitor_kind, file_path,
    #                                  worst_signal, worst_drop, rolled_back,
    #                                  rollback_error?}
    #   graduated[]           → each: {revision_id, monitor_kind, file_path,
    #                                  samples_collected}
    #   active_monitors_count → total post-check (rolled_back + graduated removed)
    #
    # If rollback_actions is non-empty: surface in iteration close output —
    # e.g., "ROLLBACK: skill_evolution {rev_id} reverted — worst signal
    # {worst_signal} dropped {worst_drop}". The rollback record is already
    # appended to the corresponding <kind>-evolution.jsonl by the engine.
    #
    # If subprocess fails (timeout, ImportError): log "WARN: 8.86 evolution-
    # check failed: {stderr}" and continue. NEVER block iteration close on
    # backpressure infra failure — the monitors will resume on the next
    # successful iteration.
```

```
# ── Step 8.77: User-Notable Event Push Classifier ────────────────────
# Proactive user notification on goal outcomes that cross a "user would
# want to know" threshold. Catches the gap Alpha named 2026-04-23:
# coordination-board updates flow constantly, but user-facing signal
# fires only via fresh-eyes-review (cadence 25) + blocker alerts +
# explicit invocations. No per-goal middle tier — until now.
#
# This step is a CLASSIFIER, not a new transport. Dispatch routes through
# the existing /notify-user skill (which owns self-identification,
# 30m same-subject dedup, email + fallback cascade, and
# wm.notification_log append). This step decides WHEN to fire; /notify-user
# decides HOW to deliver.
#
# Runs AFTER Step 8.76 (skill quality) and BEFORE Step 8.78 (fresh-eyes
# gate) — placed so reflection/quality outputs are in context but the
# more expensive fresh-eyes dispatch is still downstream.

# ── Skip conditions (fail-closed; default is NO push) ──
IF outcome_class == "routine":
    SKIP silent  # routine outcomes already degate via Part A's auto-deep at 5
IF NOT goal_succeeded:
    SKIP silent  # failures route via CREATE_BLOCKER → /notify-user category=blocker
IF goal.category == "blocker" OR goal.title starts with "Blocker:":
    SKIP silent  # double-push avoidance — CREATE_BLOCKER already dispatched
# ── Skip condition #4: user-only mute ──
# User can mute Step 8.77 entirely by writing the slot:
#   bash core/scripts/wm-set.sh suppress_user_push true
# Useful when on-call / focusing — agent keeps working but stops pushing
# user-notable events. Clear with `... wm-set.sh suppress_user_push false`
# (or wm-clear.sh suppress_user_push). Intentionally has no programmatic
# writer — agent must never auto-mute itself. Whitelisted in
# signal-lifecycle-gate.py phantom-reads CATEGORY 2.
Bash: wm-read.sh suppress_user_push
IF signal is not null AND signal == "true":
    Output: "▸ Step 8.77: suppress_user_push set (user-muted) — skipping classifier"
    SKIP

# ── Trigger evaluation (match any — multiple triggers per goal is valid) ──
triggers = []   # list of (trigger_id, category, subject, message)

# ---- Trigger 1: shipped — a new capability landed ----
# Signal: deep outcome AND ship-verb title OR insight_text contains shipping language.
# Keeps false-positive rate low by requiring the deep-outcome precondition
# (only non-routine, verified-successful, material outcomes reach here).
IF outcome_class == "deep":
    title_matches_ship = re.match(r"^(ship|deploy|release|build|create|add)\b", goal.title, re.I)
    insight_has_ship = any(s in (insight_text or "").lower()
                            for s in ["shipped", "deployed", "released to", "landed in"])
    IF title_matches_ship OR insight_has_ship:
        # Agent name is READ, never hardcoded (g-335-1202, 2026-08-13). This
        # line said "Alpha shipped:" literally, so every non-alpha agent that
        # reached Trigger 1 emailed the user a FALSE ATTRIBUTION for its own
        # work — and the user-facing channel is exactly where a wrong author
        # is least recoverable. Caught by bravo at the moment of use.
        subject = f"{AGENT_NAME.capitalize()} shipped: {goal.title[:60]}"
        message = (insight_text or goal.title)[:400]
        triggers.append(("shipped", "info", subject, message))

# ---- Trigger 2: resolved-long-blocker — Unblock goal closed after >= 7d open ----
# Single source of truth for age = goal.created_at (aspirations schema).
# DO NOT add fallback fields (created, created_date, blocked_since) — if
# created_at is missing, the age signal is untrustworthy and the trigger
# MUST skip. Fail-open matches user-stated design rule ("when in doubt,
# don't protect"). 40/50 Unblock goals in the live store lack created_at
# as of 2026-04-23 — guard-first or crash (datetime - None → TypeError).
IF goal.title starts with "Unblock:" AND goal.created_at is not None:
    age_days = (now - parse_iso(goal.created_at)).days
    IF age_days >= 7:
        title_trimmed = goal.title[9:65].strip() if len(goal.title) > 9 else goal.title
        subject = f"Unblocked after {age_days}d: {title_trimmed}"
        message = (insight_text or "")[:400] + f"\n\nBlocker had been open {age_days} days."
        triggers.append(("resolved-long-blocker", "info", subject, message))

# ---- Trigger 3: recovered — infra/session/job recovery ----
# Matches output language from recovery-gate, stale-scanner, crash recovery
# notices. Same keyword set used by Part A's recovery-notice display.
IF goal.title starts with "Recover:" OR any(
    k in (insight_text or "").lower() for k in [
        "crashed runner recovered",
        "stale_scanner killed",
        "stale scanner killed",
        "reaped",
        "infrastructure recovered",
        "recovery-gate fired",
    ]
):
    subject = f"Recovered: {goal.title[:60]}"
    message = (insight_text or goal.title)[:400]
    triggers.append(("recovered", "info", subject, message))

# ---- Future extensions (NOT shipped in this MVP — documented for next pass) ──
# Trigger 4: hypothesis-confirmed — requires /review-hypotheses integration
#            (read outcome + confidence from pipeline record resolved this
#            iteration). Would fire for correct high-conviction resolutions
#            AND for wrong-but-high-confidence (surprise) resolutions.
# Trigger 5: participant-user-goal-created — requires a goal-creation delta
#            (compact snapshot before/after this iteration) OR a WM counter
#            that aspirations-add-goal bumps when --participants includes user.
#            Collapses the original "something to say" flag into the push
#            classifier. MVP omits because the delta signal isn't wired.

# ── Short-circuit if nothing triggered ──
IF triggers is empty:
    SKIP silent

# ── Rate cap: max 3 immediate pushes per rolling hour ──
# SINGLE SOURCE OF TRUTH for THIS AGENT'S per-hour push cadence:
# wm.notification_log (owned by /notify-user; written on successful send
# only). DO NOT introduce a parallel counter — dual stores drift. The
# FLEET-WIDE "was the user already told this?" question is a different
# store — world/notifications-sent.jsonl, written by email-send.sh and read
# by notify-user Step 1.7 — and is not a duplicate of this one: this is a
# per-agent rate cap, that is a cross-agent/cross-world topic dedup.
# Entry shape: {subject, category,
# sent_at: <ISO 8601 string>}. LLM parses sent_at via datetime.fromisoformat
# before comparing.
Bash: wm-read.sh notification_log
recent_pushes = [e for e in (notification_log or [])
                 if datetime.fromisoformat(e.sent_at) >= now - timedelta(seconds=3600)]
push_slots_remaining = max(0, 3 - len(recent_pushes))

# ── Dispatch (preserve trigger order; first N under cap go out immediate) ──
# Over-cap pushes are DROPPED, not queued. Rationale: a user-notable moment
# has decay — a push 6h later stamps the wrong time (rb-464 stale-narrative).
# Suppression surfaces in the iteration Output line only; do NOT write a
# shadow journal entry (orphaned — no index via journal-add.sh).
FOR (trigger_id, category, subject, message) in triggers:
    IF push_slots_remaining <= 0:
        Output: f"▸ Step 8.77: user-push suppressed ({trigger_id}, rate cap) — {subject}"
        CONTINUE
    # Canonical prose form per .claude/rules/forged-skill-resolution.md so a
    # domain-registered notify forge-override can intercept. /notify-user owns
    # 30m same-subject dedup AND the notification_log append — this step
    # decides WHEN, /notify-user decides HOW.
    Notify the user about {trigger_id} with category={category}, subject={subject}, message={message}.
    push_slots_remaining -= 1
    Output: f"▸ Step 8.77: user-push fired ({trigger_id}) — {subject}"
```

```
# ── Step 8.78: Post-State-Update Fresh-Eyes Gate (guard-343 bash-enforced) ─
# Bash decides WHETHER (threshold gate), LLM decides WHAT (Skill dispatch).
# The gate fires only for deep outcomes with material core/ changes. See
# core/scripts/post-state-update-gate.sh for the threshold spec (core_files>=3,
# loc>=100, or new script in core/scripts). If the gate fires, the LLM
# dispatches /fresh-eyes-code on the returned file list. rb-393 + guard-343.

IF outcome_class == "deep":
    Bash: core/scripts/post-state-update-gate.sh deep
    gate_json = parse stdout as JSON
    IF gate_json.fired:
        Output: "▸ Step 8.78: fresh-eyes gate fired ({gate_json.reason})"
        Skill('fresh-eyes-code') with args: space-separated paths from gate_json.files
    # ELSE: silent pass — gate.reason already logged in gate_json for audit
# ELSE: gate is always false for routine — skip silently
```

```
# ── Step 8.11: Execution Feedback (Cross-Agent Goal Quality) ─────
# When executing a goal created by another agent, post structured quality
# feedback to world/board/feedback.jsonl. Creates a backward learning signal
# so the goal creator can improve future goal descriptions.
# See board.md Execution Feedback Schema for field definitions.

IF outcome_class != "routine" AND source == "world":
    # Determine who created the goal. `filed_by_agent` is the field that
    # actually carries the creating AGENT — measured 2026-08-11 (zeta, cc-02)
    # over all 4,196 asp-115 goals: filed_by_agent 95.9% present and holding
    # real agent names (alpha 1148 / bravo 943 / zeta 912 / echo 482 /
    # foxtrot 381 / omni 103 / delta 47), `created_by` present on **0**, and
    # `discovered_by` present on 41.5% but holding a GOAL ID (85.9% match
    # `g-NNN-NN`; 0.5% look like agent names) — it is the goal that DISCOVERED
    # the work (sq-013 schema), never the author. So do NOT read discovered_by
    # as an agent: it names the wrong entity type.
    #
    # NOT a measured defect, and the distinction matters before anyone "fixes"
    # more than this line: the prior list read created_by -> discovered_by ->
    # unknown, which predicts feedback addressed to a goal id on the 41.5%.
    # That prediction was CHECKED and FALSIFIED — of 19 feedback posts in the
    # trailing 30 days, 18 are addressed to an agent and **0** to a goal id, so
    # the LLM path has been compensating for the stale list all along. This
    # edit removes the compensation burden; it does not repair broken output.
    goal_created_by = goal.get("filed_by_agent") or goal.get("created_by") or "unknown"

    IF goal_created_by != AGENT_NAME AND goal_created_by != "unknown":
        # Rate the goal on three dimensions (1-5 each)
        # Based on actual execution experience:
        clarity = <1-5: was the description clear and actionable?>
        scope_accuracy = <1-5: was the effort estimate right? 1=wildly off, 5=spot on>
        verification_quality = <1-5: were checks testable and sufficient?>
        friction = <"low"|"medium"|"high": overall execution friction>
        notes = <optional: what was missing or wrong — only if friction >= medium>

        feedback_json = {
            "goal_id": goal.id,
            "created_by": goal_created_by,
            "executed_by": AGENT_NAME,
            "clarity": clarity,
            "scope_accuracy": scope_accuracy,
            "verification_quality": verification_quality,
            "friction": friction,
            "notes": notes
        }

        echo '<feedback_json>' | Bash: board-post.sh --channel feedback \
            --type execution-feedback --tags "{goal.id},created_by:{goal_created_by}"
        Output: "▸ Execution feedback: clarity={clarity} scope={scope_accuracy} verify={verification_quality} friction={friction}"
```

```
# ── Step 8.12: Outcome-Observation Hook (Tranche C — rb-390) ──────
# Hook slot for domain-supplied outcome observation after state update. The
# process side (goals completed, productive_ratio) can inflate while nothing
# material moves; an outcome-observation convention PULLS evidence from the
# actual systems the work is supposed to affect, producing the process-vs-
# outcome divergence signal downstream consumers (agent-completion-report
# "Outcome Delta" section) report on.
#
# Pattern B hook slot (`outcome-observation`). See
# core/config/conventions/domain-hooks.md. Core names the slot, the world
# convention (if it exists) names what to run. Skipped for routine outcomes
# (the routine early-return above already returned). Fail-open — a missing
# or broken convention does NOT abort state-update.

IF outcome_class != "routine":
    Bash: paths=$(bash core/scripts/load-conventions.sh outcome-observation 2>/dev/null)
    IF paths is non-empty:
        Read the file at the returned path
    # Procedural convention — gate on file EXISTENCE, not load status.
    Bash: source core/scripts/_paths.sh && test -f "$WORLD_DIR/conventions/outcome-observation.md" && echo "exists"
    IF exists:
        # MECHANIZED, not prose (g-115-4879). This line used to read only
        # "Follow each Step in the convention." — and a hook whose invocation
        # is prose does not fire. Measured: core/logs/outcome-observation-runs.jsonl
        # was ABSENT on cc-05 and independently on cc-07, on both boxes while
        # core/logs/ was live and writable, so this slot had never produced a
        # single audit entry on either.
        #
        # THE DEDUP GUARD IS LOAD-BEARING, NOT DEFENSIVE. iteration-close.sh
        # do_state_update also fires this hook (its own comment records that it
        # exists precisely because iteration-close BYPASSES this step), so the
        # two paths are normally mutually exclusive — but an LLM that invokes
        # this sub-skill DIRECTLY and then runs iteration-close would fire both,
        # running the collector twice per close. That duplicate-work regression
        # is exactly why the original remedy for this goal was retracted by its
        # own filer. Keying the skip on the box-local log's LAST entry makes the
        # two paths safe to coexist without either needing to know about the other.
        Bash: OBS_LOG="core/logs/outcome-observation-runs.jsonl"; \
              if [ -f "$OBS_LOG" ] && tail -1 "$OBS_LOG" | grep -q '"goal_id": *"{goal.id}"'; then \
                  echo "[8.12] already audited for {goal.id} this close (iteration-close hot path) — skipping"; \
              else \
                  bash core/scripts/outcome-observation-run.sh "{goal.id}" "{outcome_class}"; \
              fi
        Then follow any REMAINING Steps in the convention that the wrapper does
        not perform. Any step that fails SHOULD be logged and swallowed — never
        abort state-update.
    ELSE:
        # No domain outcome-observation convention exists (fresh agent).
        # Nothing to do — downstream Outcome Delta section will show
        # "no outcome signal configured" and consumers degrade gracefully.
```

