---
name: aspirations-all-blocked
description: "Handles the all-goals-blocked state inside the aspirations loop (phases B0-B7): scans the coordination board, generates constraint-aware aspirations, runs the idle playbook, triggers evolution, kicks off research, performs reflection, and applies exponential backoff. Use whenever the goal selector returns zero executable goals AND selection_reason starts with \"all_blocked\" — the orchestrator invokes this automatically. Internal-only sub-skill of /aspirations; not user-invocable."
user-invocable: false
parent-skill: aspirations
conventions: [aspirations, coordination, goal-schemas, goal-selection]
minimum_mode: autonomous
execution_history:
  total_invocations: 0
  outcome_tracking:
    successful: 0
    unsuccessful: 0
    success_rate: 0.0
  last_invocation: null
revision_id: "skill-bootstrap-aspirations-all-blocked-15dff4"
previous_revision_id: null
---

# /aspirations-all-blocked — Deep While Blocked

**Invocation contract.** Caller (aspirations orchestrator) has verified that
`/aspirations-select` returned `goal is None AND selection_reason starts with "all_blocked"` —
goals exist but all are blocked on external dependencies.

**Principle.** "No Terminal State" — generate new work that avoids blocked resources.
Only sleep as absolute LAST RESORT after all generation attempts fail.

**Quiescence exception** (Change 2). When the queue is structurally user-gated
(every blocked goal has a typed `blocker_ref` with observable `external_id`
and future `expires_at`, hysteresis passes, and wall-clock budget is intact),
the quiescence gate at B6.5 approves a longer sleep in place of the B7
backoff ladder. This is not giving up — it's recognizing that honest silence
is a valid output when all generation signals are genuinely exhausted. The
gate is script-gated, not LLM-discretionary. See `core/config/modes/autonomous.md`
→ "Core Design Principle: No Terminal State" for the full doctrine.

**Control-flow contract.** This skill terminates via one of two paths:
- **LOOP_CONTINUE** (new executable goals were created) — re-enters the orchestrator loop
  via `Skill('aspirations') with args='loop'`. The orchestrator's selection pass will pick
  up the newly-created goals.
- **RETURN** (all generation exhausted) — yields to the harness via
  `interruptible-sleep.sh run_in_background=true`. The harness re-enters the loop on
  the sleep's exit notification. `blocked_sleep_until` is set in WM so Phase -0.5e's
  `idle-tick.sh` short-circuit can activate on re-entry.

## Inputs

- `selection_context`: Output from `/aspirations-select` containing `blocked_goals`,
  `by_reason`, `blocked_count` (passed by the orchestrator)
- `evolutions_this_session` (int) — current evolution-count state for cap tracking
- `max_evolutions_per_session` (int) — from config
- `goals_completed_this_session` (int) — for logging/tracking
- `session_signals` (dict) — mutable state for `consecutive_blocked_sleeps` backoff
  counter. The sub-skill increments this on B7.2 entry.

## Outputs

Control-flow via LOOP_CONTINUE or RETURN (see contract above). No structured return value.

## Step 0: Load Conventions

`Bash: load-conventions.sh aspirations coordination goal-schemas goal-selection`

Read only the paths returned (files not yet in context). If output is empty, proceed.

## Pre-body: Entry Log

```
Output: "▸ ALL BLOCKED — goals waiting on external dependencies"
Output: blocked goal summaries from selection_context
blocked_idle_attempts = []
```

## Step B0: Board Scan for Cross-Agent Actionable Items

See coordination convention for full scan protocol.

> **Pre-silence rule (`.claude/rules/check-team-state-before-silent.md`, `guard-321`).**
> If any branch in B0–B7 — board scan, aspiration generation rationale, backoff
> escalation reasoning, take-back narrative, or proactive notification body — is
> about to conclude that the partner agent is silent / absent / crashed /
> unresponsive / inactive / stalled, run the canonical probe FIRST:
> `bash core/scripts/team-state-read.sh --field agent_status.<partner>.last_active --json`.
> If the timestamp is within 6h of now, the partner is NOT silent. Drop the
> branch. Audit on 2026-04-19 found no committed silent-branch in this skill;
> this banner is a forward defense for the next author.

```
Bash: board-read.sh --channel coordination --type escalation --since 12h --json
FOR EACH escalation_msg NOT from this agent:
    Extract goal_id from msg.tags
    IF no existing investigation goal for this goal_id:
        Create investigation goal: "Investigate: why {goal_title} keeps failing (escalated by {msg.author})"
        blocked_idle_attempts.append("board-scan: created investigation for escalated {goal_id}")
Bash: board-read.sh --channel coordination --type review-request --since 12h --json
deep_review_count = 0
FOR EACH review_msg NOT from this agent:
    Extract goal_id from msg.tags
    IF goal has review_requested but NOT review_completed:
        IF deep_review_count >= 3:
            # Cap at 3 deep reviews per B0 scan to bound context cost
            BREAK

        # ── Deep Code Review Protocol (5-phase hypothesis-driven review) ──
        # See coordination convention "Deep Review Protocol" for rationale.

        # R1 Context: Load full review context
        Bash: experience-read.sh --goal {goal_id}
        # Read the content .md file referenced in the experience trace
        Read the experience trace's content file (the article or diff output)
        Load goal description and verification outcomes from the goal record

        # R2 Architectural Assessment (3 questions):
        # Q1: Do ALL verification outcomes match the claimed result?
        #     Compare each verification check against the experience trace.
        #     Flag any mismatch between claimed outcome and actual evidence.
        # Q2: What downstream goals depend on the changed artifact?
        #     Bash: goal-selector.sh blocked
        #     Scan blocked goals for any whose blocked_by or description
        #     references the same artifact/file/module touched by this change.
        # Q3: Does this change invalidate existing tree knowledge or active hypotheses?
        #     Bash: tree-find-node.sh {artifact_or_topic}
        #     Bash: pipeline-read.sh --stage active
        #     Check if any tree nodes reference the changed artifact with
        #     facts that are now stale, or if active hypotheses assumed the
        #     pre-change state.

        # R3 Hypothesis Formation:
        # Form a testable prediction about the change's downstream impact.
        # Example: "Change to X will cause Y in the next N executions"
        # Apply calibration gate (same ceiling as spark Step 0.5):
        #   a. Read recent accuracy — BOTH stages are REQUIRED:
        #        Bash: pipeline-read.sh --stage resolved
        #        Bash: pipeline-read.sh --stage archived
        #      `--stage resolved` ALONE is a SURVIVORSHIP FILTER — it was 9.4% of
        #      the store when measured 2026-08-04, and scoring only it moves the
        #      cap a FULL BAND (62.4% -> cap 0.80 vs the union's 55.5% -> cap
        #      0.65). Do NOT re-duplicate the rationale here: aspirations-spark
        #      Step 0.5(a) is the single source of truth for this gate, and this
        #      site drifted from it precisely because the two were maintained as
        #      independent copies. Read it there; keep this block a pointer.
        #      (g-115-4866.)
        #      Count CONFIRMED vs CORRECTED over the UNION of both stages, for
        #      code_review category (or overall if <3)
        #      Compute recent_accuracy = confirmed / total
        #      Log BOTH arms (resolved n / archived n), never just the total —
        #      a silent regression to resolved-only logs identically otherwise
        #      (guard-2529 / guard-2273 / guard-2129).
        #   b. Apply confidence ceiling:
        #      - If recent_accuracy < 0.40: cap at 0.55
        #      - If recent_accuracy >= 0.40 and < 0.60: cap at 0.65
        #      - If recent_accuracy >= 0.60 and < 0.80: cap at 0.80
        #      - If recent_accuracy >= 0.80: no cap
        # Add to pipeline. pipeline-add.sh reads a JSON record on STDIN — NOT
        # --title/--confidence flags (the flag form sends an empty body and the
        # wrapper STILL exits 0; see rb "pipeline-add.sh contract"). Required
        # fields: id (YYYY-MM-DD_slug), title, stage, type, category, horizon,
        # confidence, formed_date, position (the prediction). 'tags' is not a
        # field — use category. Confirm success: stdout has id+stage and no
        # 'error' key (exit code is 0 even on validation_failed/invalid_body).
        #   Bash: cat <<'JSON' | pipeline-add.sh
        #   {"id":"{today}_{slug}","title":"Review: {prediction_summary}",
        #    "stage":"discovered","type":"calibration","category":"code-review",
        #    "horizon":"short","confidence":{calibrated_confidence},
        #    "formed_date":"{today}",
        #    "position":"{falsifiable prediction: change to X causes Y in N executions}",
        #    "rationale":"{goal_id} review by {this_agent}"}
        #   JSON

        # R4 Post to Board:
        # Share review hypothesis on findings channel so both agents learn.
        #   Bash: echo "Review of {goal_id}: {hypothesis_summary}. Assessment: {architectural_notes}" \
        #         | board-post.sh --channel findings --type finding \
        #           --tags "code_review,{goal_id}"
        #   (message text goes on STDIN — there is NO --message flag; guard-1531)

        # R5 Issue Handling (preserved from original protocol):
        IF issues_found during R2 assessment:
            Create investigation goal: "Investigate: review issue in {goal_id} — {issue_summary}"

        Bash: aspirations-update-goal.sh --source world <goal-id> review_completed <today>
        deep_review_count += 1
        blocked_idle_attempts.append("board-scan: deep-reviewed {goal_id}")
```

## Step B1: Extract Constraint Context

```
blocked_skills = set()
blocked_resources = set()
FOR EACH bg in selection_context.blocked_goals:
    IF bg.reason == "infrastructure":
        blocked_skills.add(extract_skill_from(bg.detail))
        blocked_resources.add(extract_reason_from(bg.detail))
    ELIF bg.reason == "dependency" OR bg.reason == "explicit_status":
        blocked_resources.add(bg.detail)
Bash: wm-read.sh known_blockers --json
FOR EACH blocker in known_blockers:
    FOR EACH skill in blocker.affected_skills:
        blocked_skills.add(skill)
constraint_context = {
    blocked_resources: list(blocked_resources),
    avoid_skills: list(blocked_skills),
    trigger: "all_blocked",
    blocked_count: selection_context.blocked_count,
    by_reason: selection_context.by_reason
}
```

## Step B2: Constraint-Aware Aspiration Generation

**Empty is the designed outcome.** This step asks the generator whether
anything VERIFIABLE is missing from the portfolio — not to "create work
because the queue is empty". Every candidate it files is refused by the
daemon's aspiration-supply gate (`core/scripts/gates/aspiration_supply.py`,
g-357-82/83/87) unless it carries `supply_evidence` with ≥2 existing referents
and a dated `needle_by`, names an operator outcome as the gap (never a blocker,
never the empty queue itself), overlaps no existing aspiration by ≥40%, and the
daily cap (2/agent) is not reached. On a mature portfolio expect empty most of
the time; that routes to B6.5/B7, the quiet window, cheaper than one
manufactured aspiration (measured 2026-09-02: eleven idle-path aspirations in
six days on one deployment). Never pass `--override-supply` from this path.

**Do not regenerate against an unchanged portfolio.** Generation is the most
expensive call on the idle path (it pages the full skill and the archive). If
B2 returned empty and no goal has completed since, the answer is the same —
skip it and let the backoff run.

```
Bash: wm-read.sh loop_state --json → current
completed_now = len(current.goals_completed_this_session or [])
IF current.b2_empty_at_completed == completed_now AND current.b2_empty_at_completed is not null:
    blocked_idle_attempts.append("create-aspiration: skipped (empty last time, portfolio unchanged)")
    Output: "▸ Skipping aspiration generation — last result was empty and no goal has completed since"
ELSE:
    Output: "▸ Attempting constraint-aware aspiration generation..."
    invoke /create-aspiration from-self --plan with: constraint_context
    if new_aspirations_generated:
        blocked_idle_attempts.append("create-aspiration: SUCCESS")
        Output: "▸ Generated new aspirations avoiding blocked resources"
        LOOP_CONTINUE
    blocked_idle_attempts.append("create-aspiration: no viable aspirations found")
    # Remember the portfolio state this empty result was computed against.
    # Surgical per-key update (see B7 for why wholesale replacement is unsafe).
    Bash: wm-read.sh loop_state --json → merged
    merged.b2_empty_at_completed = completed_now
    echo '<merged as JSON>' | Bash: wm-set.sh loop_state
```

## Step B2.5: Signal-Gated Goal Generation (replaces "idle playbook")

**Principle.** Queue thinness is information, not a prompt to generate. Every
agent-authored goal must cite a concrete external signal. If no signal fires,
this step creates at most ONE honest `idle_fallback` goal per session, then
lets B7's exponential backoff sleep longer.

**Gate behavior** (soft mode, the current default):
`core/scripts/origin-signal-gate.py` runs inside `cmd_add_goal`. Invalid
`origin_signal` values BLOCK (catches typos); missing values WARN-pass
(pre-migration callers keep working). Set `origin_signal` on everything
B2.5 creates so the same path works under strict mode too. Do NOT reintroduce
a hardcoded title playbook — that class of make-work is exactly what the
gate + this rewrite were built to eliminate.

**Session cap on `idle_fallback`.** At most ONE per session. Read
`wm-read.sh loop_state` → `idle_fallback_created` (int). If ≥ 1, skip the
fallback branch and let B7 sleep longer.

```
IF "create-aspiration: no viable aspirations found" in blocked_idle_attempts:
    Output: "▸ Scanning for concrete signals (no hardcoded playbook) ..."

    signals = []

    # S1. Pending-questions the user hasn't answered in >24h
    IF file exists agents/<agent>/session/pending-questions.yaml:
        Read it. For each entry with status="pending" AND asked_at older than 24h:
            signals.append({
                "kind": "pending_question",
                "id": entry["id"],
                "title": "Follow up on " + entry["id"] + ": " + entry["question"][:60],
                "origin_signal": "pending_question:" + entry["id"],
                "category": "coordination",
            })

    # S2. Coordination-board posts tagged me that I haven't addressed
    # board-read.sh only supports --channel/--since/--author/--tag/--type/--last/--json
    # — no --unread-by, so filter author/audience in the LLM reading step.
    Bash: core/scripts/board-read.sh --channel coordination --since 72h --json
    For each post where (tags includes MIND_AGENT OR audience == MIND_AGENT)
                     AND author != MIND_AGENT:
        signals.append({
            "kind": "board_post",
            "id": post["id"],
            "title": "Respond to " + post["id"] + " from " + post["author"],
            "origin_signal": "board_post:" + post["id"],
            "category": "coordination",
        })

    # S3. Findings-board posts with actionable tag that nobody has claimed
    Bash: core/scripts/board-read.sh --channel findings --since 7d --json
    For each post tagged "actionable" with no linked goal_id:
        signals.append({
            "kind": "finding",
            "id": post["id"],
            "title": "Act on finding " + post["id"] + ": " + post["title"][:60],
            "origin_signal": "board_post:" + post["id"],
            "category": post.get("category", "investigation"),
        })

    # S4. Resolved hypotheses with no reflection/encoding yet
    # pipeline.py exposes --unreflected (resolved AND reflected=false). Use it as
    # the proxy for "resolved, outcome not yet folded back into learning".
    Bash: core/scripts/pipeline-read.sh --unreflected
    For each record where resolved_at within 14d:
        signals.append({
            "kind": "unreflected_hypothesis",
            "id": record["id"],
            "title": "Reflect + encode outcome of " + record["id"],
            "origin_signal": "resolved_hypothesis:" + record["id"],
            "category": "knowledge",
        })

    # Filter: drop signals blocked by resource
    viable = [s for s in signals if s["category"] not in constraint_context.get("blocked_resources", [])]

    IF viable:
        # Signals are appended in priority order (S1 user-facing > S2 partner-facing >
        # S3 findings > S4 learning debt). Taking viable[0] honors that order.
        chosen = viable[0]
        goal_json = {
            title: chosen["title"],
            status: "pending", priority: "MEDIUM",
            participants: ["agent"], category: chosen["category"],
            origin_signal: chosen["origin_signal"],
            description: "Signal-driven. Source: " + chosen["origin_signal"] + ". "
                       + "Address the specific signal above; do not expand scope.",
        }
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source agent asp-001
        blocked_idle_attempts.append("signal-gated: created '" + chosen["title"] + "'")
        Output: "▸ Signal-gated goal: {chosen['title']} (origin={chosen['origin_signal']})"
        LOOP_CONTINUE

    # No signals fired. Decide between single idle_fallback or truly-idle exit.
    Bash: wm-read.sh loop_state → current
    idle_fallback_count = (current.idle_fallback_created or 0)

    IF idle_fallback_count >= 1:
        blocked_idle_attempts.append("signal-gated: no signal, fallback cap reached")
        Output: "▸ No live signal AND idle_fallback cap reached — no goal created"
        # Fall through to B3 (evolution) and ultimately B7 (longer sleep).
    ELSE:
        # Issue ONE honest idle_fallback goal per session. Title names the emptiness
        # so the user can see it in completion reports. Description cites the scan.
        goal_json = {
            title: "Review: no concrete signal in board/questions/pipeline — confirm direction",
            status: "pending", priority: "LOW",
            participants: ["agent", "user"], category: "coordination",
            origin_signal: "idle_fallback",
            description: "Scanned pending-questions (>24h), coordination+findings boards "
                       + "(72h/7d), and unreflected resolved hypotheses (14d) — no actionable "
                       + "signal found. This goal exists so the user sees the agent went idle "
                       + "honestly instead of fabricating category-shaped work.",
        }
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source agent asp-001
        merged = current
        merged.idle_fallback_created = idle_fallback_count + 1
        echo '<merged as JSON>' | bash core/scripts/wm-set.sh loop_state
        blocked_idle_attempts.append("signal-gated: idle_fallback issued (session cap = 1)")
        Output: "▸ idle_fallback goal created — user review requested"

    # ONLY reached when viable is empty (the IF viable branch LOOP_CONTINUEs above).
    # Notify the user so the "no signal" state surfaces outside completion reports.
    # /notify-user rate-limits within a session, so this fires at most once.
    Notify the user about going idle with no concrete signal.
    (Check world/forged-skills.yaml for a skill whose triggers match
    "notify the user" and invoke it with subject="Agent idle — no signal" and
    a short body summarizing the empty scan. If no matching skill is registered,
    fall back to a participants=[agent,user] goal via aspirations-add-goal.sh.
    Never block on notification failure.)
```

## Step B3: Evolution Gap Analysis (Even Outside Normal Triggers)

Evolution accounting (`loop_state.evolutions`, `loop_state.last_evolution_at`) is
BASH-OWNED as of g-115-1561: the `/aspirations-evolve` invocation below runs
`loop-state-bump-counters.py --evolution-fired` internally (the single writer). B3
therefore does NOT mutate or persist those fields in-context — a bare LOOP_CONTINUE
is correct, and the orchestrator's Phase -0.5 restores the bash values next iteration.
(B2.5 above and B7 below still read-merge-write `loop_state` for the genuinely
LLM-owned `idle_fallback_created` / `consecutive_blocked_sleeps` fields, which have
no bash writer.)

```
IF evolutions_this_session < max_evolutions_per_session:
    Output: "▸ Attempting idle evolution gap analysis..."
    invoke /aspirations-evolve with: ["idle_blocked"]
    # g-115-1561: evolutions / last_evolution_at are BASH-OWNED — aspirations-evolve
    # ran loop-state-bump-counters.py --evolution-fired internally (single writer).
    # Do NOT increment in-context here (would double-count vs the bash write). The
    # `evolutions_this_session < max` cap check above reads the bash-restored value.
    # Check if evolution created new executable goals
    Bash: goal-selector.sh
    IF parsed_output is a JSON array with length > 0:
        blocked_idle_attempts.append("evolve: SUCCESS — new executable goals")
        Output: "▸ Evolution created new executable goals"

        # ── LOOP_CONTINUE ──
        # g-115-1561: evolutions / last_evolution_at are BASH-OWNED now
        # (aspirations-evolve --evolution-fired persisted them to WM above). B3
        # has no other loop_state mutation to carry, so a bare LOOP_CONTINUE is
        # correct — the orchestrator's Phase -0.5 restores the bash values next
        # iteration. (Removed the read-merge-write overlay whose stale in-context
        # copies of evolutions/last_evolution clobbered the bash write.)
        Skill('aspirations') with args='loop'
    blocked_idle_attempts.append("evolve: completed but no new executable goals")
ELSE:
    blocked_idle_attempts.append("evolve: skipped (session cap reached)")
```

## Step B4: Exploratory Research

```
Output: "▸ Attempting exploratory research..."
invoke /research-topic (explore broadly based on Self's purpose, avoiding blocked domains)
blocked_idle_attempts.append("research: completed")
```

## Step B5: Full-Cycle Reflection

```
Output: "▸ Running full-cycle reflection..."
invoke /reflect --full-cycle
blocked_idle_attempts.append("reflect: completed")
```

## Step B6: Re-Check for New Executable Goals

B4 and B5 may have produced new work via spark/findings.

```
Bash: goal-selector.sh
IF parsed_output is a JSON array with length > 0:
    Output: "▸ Research/reflection produced new executable goals"
    LOOP_CONTINUE
```

## Step B6.5: Quiescence Gate

Before the escalating backoff fires, ask the quiescence gate whether the
queue is HONESTLY user-gated. The gate is script-gated (not LLM-discretionary)
— its decision is purely a function of structured blocker_ref metadata on
every blocked goal, hysteresis, wall-clock budget, and recent wake-miss
history. See `core/scripts/quiescence-gate.py` and
`core/config/conventions/goal-schemas.md` → "Blocker Reference Schema".

Exit 0 approves a quiescent sleep of `sleep_seconds_min`-`sleep_seconds_max`
(default 1800-3600s) instead of the B7 backoff ladder. All six conditions from
the script must pass: C1 all-blocked (we assert it via --all-blocked), C2 every
blocked goal has blocker_ref, C3 every blocker_ref expires in the future, C4
hysteresis (same blocker-set hash for N iters), C5 wall-clock budget not
exceeded, C6 wake-miss cooldown not tripped.

```
Bash: MIND_AGENT=<agent> py -3 core/scripts/quiescence-gate.py check --all-blocked
# Parse JSON stdout; branch on exit code:

IF rc == 0 (approved):
    # Gate wrote an active_snapshot to WM and returned sleep_seconds.
    # Skip the B7 backoff ladder entirely. Do NOT increment
    # consecutive_blocked_sleeps — quiescence is a different state.
    sleep_seconds = output.sleep_seconds

    # g-303-11: prolonged-quiescence escalating user-ping. When the gate reports
    # the SAME user-gated blocker set has persisted past the wall-clock threshold
    # (prolonged_quiescence_hours, default 4h) AND every blocked goal is gated by
    # a user-only blocker_ref AND this hash has not been pinged this throttle
    # window, surface ONE focused escalation naming the single highest-leverage
    # blocker. The gate already wrote the per-hash throttle file, so should_notify
    # is true AT MOST once per window — no email flood (the requirement that
    # motivated this goal). Fires on BOTH the drainable and straight-to-sleep
    # sub-paths below. Never block the sleep on notification failure.
    IF output.prolonged_quiescence == true AND output.should_notify == true:
        p = output.prolonged_payload
        Output: "▸ PROLONGED QUIESCENCE: {p.hours_in_quiescence}h on one user-gated blocker set ({p.blocker_count} goals, {p.distinct_blocker_count} distinct blockers) — escalating ONE ping for highest-leverage blocker {p.highest_leverage_blocker_id}."
        Notify the user about prolonged user-gated quiescence.
        (Check world/forged-skills.yaml for a skill whose triggers match
        "notify the user" and invoke it with a short subject and message:
           subject = "Quiescence: {p.blocker_count} goal(s) waiting on you {p.hours_in_quiescence}h — clear {p.highest_leverage_blocker_id}"
           message = names {p.highest_leverage_blocker_id} as the single action that
             unblocks the most goals, lists p.sample_blocked_goal_titles, and notes
             these goals are blocked ONLY on a user-only action (nothing the agent
             can self-provision — the gate already verified every blocker_ref is a
             user-only type).
         If no matching skill is registered, fall back to a participants:[agent,user]
         goal via aspirations-add-goal.sh under asp-001, titled
         "Unblock: {p.blocker_count} user-gated goal(s) idle {p.hours_in_quiescence}h — clear {p.highest_leverage_blocker_id}",
         with origin_signal "quiescence:prolonged-ping". Never block on notification
         failure — proceed to the sleep branches below regardless.)

    # Magic Wand #2 (alpha session-60): set QUIESCENCE_SLEEP=1 so
    # interruptible-sleep.sh demotes informational wake signals
    # (board-activity, goal-claim-released — partner activity) without
    # exiting 2. Blocker-class signals (blocker-cleared, pq-resolved,
    # email-received) still break the sleep early — those are real
    # state changes that unblock work. See interruptible-sleep.sh
    # "Wake-signal classes" header for the contract.
    quiescence_sleep_env = "QUIESCENCE_SLEEP=1"

    # B6.8 (g-303-28): symmetric drainable-debt branch. When the queue is
    # honestly user-gated AND drainable framework-hygiene work exists that does
    # NOT depend on the user-gated blockers, drain ONE unit before sleeping
    # instead of idling 30-60min on it. The gate computes approved_but_drainable
    # ONLY on this approved path (the denied path routes to B6.7).
    IF output.approved_but_drainable == true:
        primary = output.drainable_summary.primary_target
        primary_n = output.drainable_summary.primary_target_count
        Output: "▸ Quiescence approved WITH drainable debt ({primary}={primary_n}) — draining one unit before sleep (B6.8), then sleeping {sleep_seconds}s."
        CONTINUE to Step B6.8   # B6.8 drains one unit, then GOTOs B7.2 with sleep_seconds + quiescence_sleep_env
    ELSE:
        # No drainable evidence (or the daemon read failed → fields false/null):
        # back-compatible behavior — straight to quiescent sleep, no B6.8.
        Output: "▸ Quiescence approved — sleeping {sleep_seconds}s on structured blocker_refs."
        GOTO Step B7.2 with sleep_seconds + quiescence_sleep_env

ELSE IF rc == 1 (denied):
    # Gate evaluated but one or more conditions failed. The JSON stdout
    # enumerates which. Magic Wand #5 (alpha session-60): when quiescence
    # denies, the queue is NOT structurally user-gated, so there's signal
    # to act on. Route to B6.7 (targeted deep work) BEFORE B7 backoff so
    # we convert idle time to learning, not sleep. B6.7 falls through to
    # B7 honestly when no targeted work is available.
    Output: "▸ Quiescence denied: " + ", ".join(r.condition for r in output.reasons)
    CONTINUE to B6.7
```

After wake: the B7.2 background sleep exits 0 on natural completion or 2 on
wake-signal. Either way, the orchestrator's re-entry path MUST invoke the
gate's post-sleep audit if an active_snapshot exists:

```
Bash: MIND_AGENT=<agent> py -3 core/scripts/quiescence-gate.py verify-wake
# rc=0 clean, rc=2 drift detected (emits an Investigate goal via the
# orchestrator's normal post-sleep path).
```

The `verify-wake` call belongs in the orchestrator's post-sleep branch
(aspirations/SKILL.md Phase -0.5e), not here — the B7.2 RETURN yields to
the harness, which re-enters the orchestrator.

## Step B6.7: Targeted Deep Work When Quiescence Denies (Magic Wand #5)

**Magic Wand #5 (alpha session-60 reflection, 2026-05-07; expanded 2026-05-07
per user feedback).** When B6.5 quiescence denies (rc=1), the queue is NOT
structurally user-gated — there's unstructured signal somewhere. B4 (broad
research) and B5 (broad reflection) already ran, but they cast a wide net and
often produced nothing actionable. Before falling to the B7 backoff sleep, run
TARGETED deep work pointed at the freshest concrete signal we can find.
Convert idle time to deep work, not sleep.

User direction (verbatim): "we heavily preferr b4/b5 over b7 backoff! Like,
instead of backing off, the agents know enough about what needs to get done
next, so we should be creating more aspirations." Target 1 (aspiration
synthesis from blocker patterns) was added FIRST in priority to honor this:
when goals are blocked, the blocker_refs themselves carry signal about what
work the agent should be doing. Convert that signal into aspirations BEFORE
falling to backoff.

Four targets, in priority order:

1. **Aspiration synthesis from blocker patterns (PRIMARY)** —
   `goal-selector.sh blocked` enumerates blocked goals with their blocker_refs.
   Group by `blocker_ref.external_id` (or by `blocker_ref.type` when external_id
   is missing). For any pattern affecting ≥2 goals AND not yet covered by an
   outstanding `Unblock:` aspiration, synthesize one new aspiration via
   `/create-aspiration from-followup`. The new aspiration's seed goal is
   immediately executable next iteration.
2. **Freshest unreflected resolved hypothesis** — `pipeline-read.sh --unreflected`
   returns hypotheses with `reflected: false`. Pick the most recently resolved
   and run `/reflect --on-hypothesis <id>` to extract its lesson into the
   reasoning bank. A resolved hypothesis is already-paid-for evidence whose
   lesson is sitting un-encoded.
3. **Recent finding with no linked goal** — `board-read.sh --channel findings
   --since 7d --json` and pick the freshest finding tagged `actionable` that
   has NO linked goal_id. Convert it to a goal via the create-aspiration
   from-followup fast path.
4. **Freshest pending decompose candidate** — `tree-read.sh --decompose-candidates`
   for a node whose debt is acute. Run `/tree decompose <node>` to harden the
   structural backbone instead of sleeping.

Skip to B7 only if all four targets are empty.

```
IF B6.5 returned rc=1 (quiescence denied):
    # Target 1: aspiration synthesis from blocker patterns (PRIMARY).
    # Direct response to "we should be creating more aspirations"
    # (user feedback 2026-05-07). Blocked goals carry blocker_ref
    # signal — convert it into Unblock aspirations rather than sleep.
    Bash: goal-selector.sh blocked
    Parse JSON. Returns {"blocked_goals": [...], "blocked_count": N, "by_reason": {...}}.
    Iterate parsed.blocked_goals. Group by the external_id CLASS PREFIX —
    the text before the FIRST colon in blocker_ref.external_id. When there is
    no colon, the whole string is its own group (correctly a singleton).

    # NOT the full external_id (near-unique → the >= 2 gate is unreachable,
    # g-115-4923), NOT blocker_ref.type (167/169 `resource` → degenerate), NOT
    # by_reason (strictly coarser). Measure against `goal-selector.sh blocked`,
    # never `aspirations-query.sh --goal-status blocked` (30x smaller population).
    # Rationale (WHY the class prefix, with the measurements):
    # core/config/rationale/blocker-pattern-grouping-key.md

    # Build the set of blocker patterns already covered by an OUTSTANDING
    # Unblock aspiration, so we don't spam duplicates.
    #
    # READ THE ASPIRATION STORE, NOT THE GOAL STORE (g-115-3139). The write
    # below stamps `blocker_pattern:<pattern>` on the ASPIRATION handed to
    # /create-aspiration; no GOAL ever carries that origin_signal, because
    # create-aspiration stamps its seed goal `parent_aspiration:<asp-id>`
    # instead. The previous read scanned GOALS, so covered_patterns was ALWAYS
    # empty and the `NOT IN covered_patterns` suppression below never
    # suppressed anything — a dedup keyed on a value nothing writes
    # (vacuous-dedup class, g-115-2196).
    #
    # Measured 2026-07-29: 0 of 4132 goal records carry a blocker_pattern
    # origin_signal; 1 of 381 aspirations does (asp-279,
    # `blocker_pattern:roblox_studio_session_required`) — confirming the
    # aspiration store is where the writer actually lands. (The goal's own
    # stated evidence was a VACUOUS zero — a bare prefix passed to an
    # exact-match flag. Read guard-1796 before re-verifying this by query.)
    #
    # --active (NOT --archive) preserves the original intent: only OPEN work
    # suppresses re-synthesis. An archived Unblock aspiration whose pattern is
    # STILL blocking should be re-synthesized — the prior attempt did not
    # resolve it. asp-279 is archived and therefore correctly does not suppress.
    #
    # aspirations-read.sh requires a content selector alongside --source
    # (guard-1293) and returns FULL records, so origin_signal is present. Do
    # NOT substitute aspirations-query.sh here: its default projection is six
    # keys and omits origin_signal entirely (guard-1424 / guard-884), which
    # would silently re-introduce the same always-empty bug through a
    # different door.
    covered_patterns = set()
    FOR EACH src IN (world, agent):
        Bash: aspirations-read.sh --active --source {src}
        FOR EACH aspiration WHERE origin_signal starts with "blocker_pattern:":
            covered_patterns.add(origin_signal with the "blocker_pattern:" prefix stripped)

    created_count = 0
    # g-115-4966 landed: self-resolving / routing-artifact classes are a hard
    # exclusion list, core/config/aspirations.yaml → idle_supply.blocker_pattern_exclusions.
    excluded = config.idle_supply.blocker_pattern_exclusions
    FOR EACH pattern WHERE affected_count >= 2 AND pattern NOT IN covered_patterns AND pattern NOT IN excluded:
        Output: "▸ MW#5 Target 1: synthesizing Unblock aspiration for " + pattern
        aspiration_data = {
            title: "Unblock: " + pattern_summary + " (" + affected_count + " goals waiting)",
            priority: "HIGH",
            description: "Blocker pattern: " + pattern + ". Affected goals: " + goal_id_list +
                ". Synthesized by MW#5 Target 1 from blocker_ref signal.",
            origin_signal: "blocker_pattern:" + pattern,
            # blocker_pattern:* is a self-generated origin — the aspiration-supply
            # gate refuses it without supply_evidence. The blocked goal ids ARE the
            # referents; "Unblock:" titles are exempt from the blocker-as-gap check.
            supply_evidence: {
                gap: "No outstanding Unblock covers the " + pattern + " class; " + affected_count + " goals wait on it: " + goal_id_list,
                needle: "Those " + affected_count + " goals become executable once the class is cleared",
                checked: goal_ids_of_affected_goals,
            },
        }
        invoke /create-aspiration from-followup with: aspiration_data
        blocked_idle_attempts.append("MW#5 Target 1: created Unblock aspiration for " + pattern)
        created_count += 1
        IF created_count >= 3:
            BREAK   # cap per iteration to prevent burst-spam

    IF created_count > 0:
        # The new aspiration's seed goal is executable; selector picks it up
        # next iteration. No need to re-check inline — LOOP_CONTINUE here
        # because we DID produce work; B7 backoff is the wrong destination.
        LOOP_CONTINUE
    # else fall through to Target 2

    # Target 2: unreflected hypothesis
    Bash: pipeline-read.sh --unreflected
    Parse JSON; if non-empty, sort by resolved_at DESC, take first.
    IF freshest_unreflected:
        Output: "▸ MW#5 Target 2: reflecting on freshest unreflected hypothesis " + freshest.id
        invoke /reflect --on-hypothesis with: hypothesis_id=freshest.id
        blocked_idle_attempts.append("MW#5 Target 2: reflected on " + freshest.id)
        # Re-check the queue; reflection may have produced new executable goals
        Bash: goal-selector.sh
        IF parsed_output is a JSON array with length > 0:
            LOOP_CONTINUE
        # else fall through to Target 3

    # Target 3: actionable finding without goal
    Bash: board-read.sh --channel findings --since 7d --json
    Filter for posts with "actionable" tag AND no "goal_id" tag AND author != MIND_AGENT.
    IF non-empty:
        chosen = sort by created_at DESC, take first
        Output: "▸ MW#5 Target 3: converting finding " + chosen.id + " to goal"
        Build goal_json with origin_signal="board_post:" + chosen.id, category from finding,
            title="Act on finding " + chosen.id + ": " + chosen.title[:60]
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source agent asp-001
        blocked_idle_attempts.append("MW#5 Target 3: converted finding " + chosen.id)
        LOOP_CONTINUE

    # Target 4: tree decompose candidate
    Bash: tree-read.sh --decompose-candidates
    Parse JSON; sort by debt_score DESC; take first if debt is acute.
    IF acute_decompose_target:
        Output: "▸ MW#5 Target 4: decomposing tree node " + acute.path
        invoke /tree decompose with: node_path=acute.path
        blocked_idle_attempts.append("MW#5 Target 4: decomposed " + acute.path)
        # Re-check; decomposition often produces new sub-goals
        Bash: goal-selector.sh
        IF parsed_output is a JSON array with length > 0:
            LOOP_CONTINUE
        # else fall through to B7

    # All four targets exhausted — fall through to B7 honestly. The agent
    # genuinely has nothing to act on right now.
    Output: "▸ MW#5: No targeted deep work available — falling through to backoff sleep"
    blocked_idle_attempts.append("MW#5: no targeted work, fell through to B7")
```

## Step B6.8: Drainable Hygiene When Quiescence Approves (Symmetric to B6.7)

**g-303-28 (asp-303 Story A1; design recovered from git
`899158fd:bravo/reports/a1-symmetric-quiescence-drain-design.md`).** B6.7 fires
only when quiescence DENIES (rc=1). When it APPROVES (rc=0), the queue is
honestly user-gated — but drainable framework-hygiene work that does NOT depend
on the user-gated blockers (tree-decompose candidates, unreflected resolved
hypotheses, actionable findings without a linked goal) may still exist. Without
B6.8 the agent sleeps 30-60min on that work. B6.8 drains exactly ONE unit, then
falls through to the SAME quiescent sleep (B7.2). The single-drain-per-cycle
rule is load-bearing: looping-to-empty would convert "approved sleep" into a
perpetual hygiene busy-loop — itself a new form of idle-path waste.

### Trigger

B6.8 fires when ALL of:
1. B6.5 returned rc=0 (quiescence APPROVED).
2. The gate's JSON `approved_but_drainable == true` (at least one of the three
   drainable-evidence counts is ≥1; the gate computes this only on the approved
   path).
3. B6.8 has not already fired this iteration (single drain per cycle).

`sleep_seconds` and `quiescence_sleep_env` carry in from B6.5 unchanged — the
drain does not alter the approved sleep duration.

### Three-Target Priority (drain the FIRST match, then sleep)

Priority order: **decompose > hypothesis > finding** (highest
long-term leverage first). Each target RE-READS its evidence at fire time, so a
count the gate reported is re-validated against current state — if a partner
drained it in the race window between gate check and B6.8 entry, the cycle falls
through to the next target (staleness self-corrects). Drain ONE target, then
GOTO B7.2.

```
# Single-drain guard: only enter once per iteration.
drained = false

# Target 1: freshest pending decompose candidate (PRIMARY — highest leverage).
# Tree decomposition hardens the structural backbone; a multiplier on every
# future retrieval. Decomposing a node removes it from the candidate set, so
# the candidate count drops by 1 (the design-section-4 verification signal).
Bash: tree-read.sh --decompose-candidates
Parse JSON array. Sort by line_count DESC (debt proxy). Take first.
IF non-empty:
    acute = first candidate
    Output: "▸ B6.8 Target 1: decomposing tree node {acute.key}"
    invoke /tree decompose with: node_path=acute.key
    drained = true; drain_target = "decompose:" + acute.key

# Target 2: freshest unreflected resolved hypothesis.
# Already-paid-for evidence whose lesson sits un-encoded. Cheap (one /reflect),
# high yield (one reasoning-bank entry).
IF NOT drained:
    Bash: pipeline-read.sh --unreflected
    Parse JSON; sort by resolved_at DESC; take first.
    IF freshest_unreflected:
        Output: "▸ B6.8 Target 2: reflecting on {freshest.id}"
        invoke /reflect --on-hypothesis with: hypothesis_id=freshest.id
        drained = true; drain_target = "hypothesis:" + freshest.id

# Target 3: actionable finding without a linked goal.
# Same filter the gate counted with, so what the gate saw is what we drain.
IF NOT drained:
    Bash: board-read.sh --channel findings --since 7d --json
    Filter for posts with "actionable" tag AND no "goal_id" tag AND
    author != MIND_AGENT.
    IF non-empty:
        chosen = sort by timestamp DESC, take first
        Output: "▸ B6.8 Target 3: converting finding {chosen.id} to goal"
        Build goal_json with origin_signal="board_post:" + chosen.id,
            category from finding, title="Act on finding " + chosen.id[:48]
        echo '<goal_json>' | bash core/scripts/aspirations-add-goal.sh --source agent asp-001
        drained = true; drain_target = "finding:" + chosen.id

# Drain complete (or all targets went stale). Either way → quiescent sleep.
IF drained:
    Output: "▸ B6.8 drained one unit ({drain_target}); now sleeping {sleep_seconds}s (B7.2)."
ELSE:
    # Fallback (design section 5): the gate saw drainable evidence but it went
    # stale before B6.8 fired (e.g. a partner drained the only candidate in the
    # race window). Do NOT block the sleep — quiescence approval is still valid.
    Output: "▸ B6.8 entered but all three targets empty post-recheck; falling through to B7.2 sleep"
GOTO Step B7.2 with sleep_seconds + quiescence_sleep_env
```

**Why single-drain, not loop-to-empty**: the quiescence cadence (30-60min) × one
drain per cycle ≈ a few hygiene actions per shift, matched to the rate fresh
evidence is produced. Looping to empty re-creates the busy-loop the quiescence
gate exists to prevent. (Design risk analysis, section 6.)

**Why B6.8 and B6.7 cannot collide**: B6.7 fires on rc=1, B6.8 on rc=0 —
mutually exclusive for the same agent in the same iteration. Cross-agent races
are the standard claim-conflict pattern already handled by `aspirations-claim.sh`
and team-state `in_flight`.

## Step B7: Exponential Backoff Sleep

The `consecutive_blocked_sleeps` increment below must be persisted to WM `loop_state`
BEFORE RETURN. The RETURN yields to the harness, which re-enters the orchestrator,
which reads `loop_state` fresh. Without read-merge-write here, the increment vanishes
and the backoff never escalates past level 0 (stuck at 300s forever). Must NOT clobber
sibling `loop_state` fields (goals_completed, evolutions, etc.) — use read-merge-write.

The escalating backoff above is a function of THIS agent's recent productivity, not
the partner's liveness. Do NOT add a "partner is silent → escalate faster" branch
without first running the team-state probe per the B0 banner above (`guard-321`).
Backoff that ramps "because the partner is silent" is exactly the unverified narrative
the rule prevents.

```
# Dry-idle curve (g-115-2084-c, Layer 3): every B7 arrival IS the dry state
# by construction — the selector returned all_blocked (zero executable) and
# B6.5 did not approve (denied → B6.7 fell through; gate-error → na). The
# single-writer tick applies the Layer-2 streak transition (interlude reset
# per reset_on_executable), persists loop_state.signals.dry_idle under the WM
# lock, and returns the exponential sleep: 120s→7200s cap; flat 2h blocks per
# deployment via meta/config-overrides.yaml
# aspirations.dry_idle_backoff.base_seconds (g-357-90). Fail-open → legacy.
Bash: tick=$(py -3 core/scripts/dry-idle-tick.py --executable-count 0 --quiescence-decision denied)
IF tick.enabled == true AND tick.dry == true:
    sleep_seconds = tick.sleep_seconds
    dry_sleep_env = "DRY_SLEEP=1"   # B7.2 prefix — registers the Tier-A bg job
                                    # (guard-967) WITHOUT quiescence's
                                    # informational-signal demotion: dry sleeps
                                    # wake on board activity (criterion 4).
ELSE:
    # Tick disabled/fail-open — legacy schedule.
    BACKOFF_SCHEDULE = [300, 600, 1200, 1800]  # 5/10/20/30 min
    sleep_index = min(session_signals.consecutive_blocked_sleeps, len(BACKOFF_SCHEDULE) - 1)
    sleep_seconds = BACKOFF_SCHEDULE[sleep_index]
session_signals.consecutive_blocked_sleeps += 1
wake_at = (now + sleep_seconds seconds) as ISO timestamp
echo '"{wake_at}"' | Bash: wm-set.sh blocked_sleep_until

# Persist ONLY the LLM-owned signals subkey we just mutated. Do NOT do
# `merged.signals = session_signals` here — that wholesale replacement
# clobbers bash-owned subkeys written DURING this iteration:
#   - `signals.quiescence` (current_hash + streak written by B6.5 gate)
#   - `signals.goals_since_last_tree_update` (drift counter)
# The B6.5 clobber silently breaks the quiescence gate's C4 hysteresis:
# the gate writes prior_hash + streak this iteration, B7 here would
# overwrite with the LLM's stale Phase -0.5 snapshot, next iteration's
# gate sees stale prior_hash, mismatches, resets streak to 1 —
# hysteresis (>=2) NEVER trips, the gate NEVER approves. Surgical
# per-key update is the only safe pattern.
Bash: wm-read.sh loop_state --json
merged = current_loop_state
merged.signals.consecutive_blocked_sleeps = session_signals.consecutive_blocked_sleeps
echo '<merged as JSON>' | Bash: wm-set.sh loop_state

Output: "▸ All generation attempts exhausted. Sleeping {sleep_seconds}s (backoff level {sleep_index})."
Output: "  Attempts: " + "; ".join(blocked_idle_attempts)
```

## Step B7.1: Proactive User Notification

```
IF config.proactive_escalation.b7_notify:
    Bash: wm-read.sh proactive_escalation_log --json
    last_b7 = find entry where blocker_id == "_all_blocked"
    IF last_b7 is null OR hours_since(last_b7.sent_at) >= config.proactive_escalation.blocker_age_hours:
        # Build summary from known_blockers
        Bash: wm-read.sh known_blockers --json
        blocker_summary = ""
        FOR EACH blocker WHERE resolution is null:
            # LEGACY-SHAPE + CROSS-WRITER TOLERANCE (g-115-3348). Two writers
            # emit different key sets: infra-health.py uses reason/blocker_id,
            # create-blocker.py used failure_reason/id (it now ALSO emits the
            # documented detected_at/blocker_id, but blockers created before
            # that fix are still live in working memory). Reading only the
            # documented names rendered every canonically-created blocker as
            # "• None (blocked ...)" with no age -- in the ALL-BLOCKED
            # last-resort notification, i.e. exactly when the user most needs
            # to know what is stuck.
            age_hours = hours_since(blocker.detected_at or blocker.created_at)
            reason    = blocker.reason or blocker.failure_reason
            blocker_summary += "• {reason} (blocked {age_hours:.0f}h, {len(blocker.affected_goals)} goals)\n"
            blocker_summary += "  Unblock: {blocker.unblocking_goal}\n"
        IF blocker_summary is empty:
            blocker_summary = "All goals blocked on dependencies (no infrastructure blockers active)."
        Notify the user:
            category: blocker
            subject: "All work blocked — agent waiting"
            message: |
                All self-remediation exhausted (board scan, aspiration generation, idle playbook,
                evolution, research, reflection). Entering backoff sleep ({sleep_seconds}s).

                Active blockers:
                {blocker_summary}

                The single highest-value action you can take:
                {most_impactful_blocker_or_dependency_description}
        # Record escalation
        echo '{"blocker_id":"_all_blocked","sent_at":"{now}"}' | Bash: wm-append.sh proactive_escalation_log
```

## Step B7.2: Yield via Background Interruptible-Sleep

CRITICAL contract (do not weaken):
- `interruptible-sleep.sh`, NOT plain `sleep`: its 1s stop-signal check is what lets
  `/stop` respond within seconds instead of waiting up to 30min for the sleep to exit.
- `run_in_background=true`: harness notifies on exit, so autocompacts during the wait
  hit the cheap `idle-tick` path (not the full skill).
- `RETURN`, not `LOOP_CONTINUE`: LOOP_CONTINUE would reinvoke the skill immediately
  and defeat the token-cost reduction. The harness re-enters via the exit notification.
- `blocked_sleep_until` is NOT cleared here. Phase -0.5e is the single owner of
  expire/clear on re-entry.

### B7.2a: Commit first — the idle path's only commit point

`iteration-commit.sh` is wired inside `iteration-close.sh`'s `do_state_update`
(the g-280-03 wiring) — which runs **only when a goal closes**. The all-blocked
path closes no goal, yet B0/B2/B2.5/B3/B4/B5/B6.7/B6.8 all write durable
artifacts: guardrails, reasoning-bank entries, tree nodes, new goals, defer
refreshes, board posts. None of it was ever committed by any mechanism.

MEASURED on downstream prod (omni, 2026-08-08, ZDS g-012-88 — back-ported to
the frontier 2026-08-19): 16 dirty files against 0 unpushed commits, holding a
new guardrail, a substantial tree-node section, a filed digest goal with its
rule-axis reasoning, and two defer refreshes. All real learning, uncommitted,
because no goal had closed since the last iteration-close. **The exposure
window is longest exactly when nothing will sweep it in** — the idle path is
the path that runs during long quiet stretches, and a session that ends or
crashes mid-idle loses the lot.

This placement (inside B7.2, before the sleep call) covers BOTH sleep paths,
because the quiescence-approved branch (B6.5 rc=0 / B6.8 drain) and the B7
backoff branch both funnel through B7.2. One call, both routes — and the
terminal-call contract below is preserved because the commit Bash call runs
BEFORE the sleep Bash call.

```
# `--goal-id all-blocked` is a deliberate synthetic label, not a real goal (there
# is none — that IS the gap). It is not format-validated, produces a clean
# `chore(all-blocked): ...` subject, and can never collide with sq-018's
# per-goal ledger grep (`git log --grep "(g-NNN-NN):"`), so nothing that reads
# commit history by goal-id is polluted. Routing through iteration-commit.sh
# rather than a bare `git commit` keeps the namespace filter and the
# over-inclusion attribution audit.
# `--outcome deep` is REQUIRED: the script skips outright on `routine`
# ("skip: outcome=routine (no commit by design)" — verified).
# WHAT IT COSTS ON A CYCLE THAT WROTE NOTHING SUBSTANTIVE, stated honestly
# because the obvious claim is wrong: it does NOT no-op. `world/changelog.jsonl`
# is appended by the loop's OWN script-mediated reads and writes, so a truly
# clean tree is rare here — three consecutive attempts to reach that branch all
# found 1-2 dirty files and committed them (measured on downstream prod). Expect
# a small `chore(all-blocked)` commit carrying a changelog delta on most idle
# cycles. That is bounded and harmless, and it is much cheaper than the failure
# it replaces (stranding a guardrail or a tree-node section through an idle
# stretch). Do not "fix" the noise by gating on cleanliness without
# re-measuring — the gate would rarely fire and would reintroduce the
# stranding risk for the cycles that matter.
# NEVER BLOCK THE SLEEP: on any non-zero rc, log it and continue to the sleep
# below. A failed commit must not strand the loop awake.
Bash: source core/scripts/_paths.sh && bash core/scripts/iteration-commit.sh --goal-id all-blocked \
        --title "Maintain: encode learning produced on the idle path" \
        --outcome deep --repo "$PROJECT_ROOT"
```

```
# Magic Wand #2 (alpha session-60): from B6.5 with quiescence approval prepend
# QUIESCENCE_SLEEP=1 (informational wake signals demoted); from the B7 dry path
# (g-115-2084-c) dry_sleep_env carries DRY_SLEEP=1 — same Tier-A bg-job
# registration (Gate 2.6 ALLOW, guard-967), NO demotion (partner activity may
# create claimable work). Legacy arrivals leave both unset (unregistered).
# HARNESS BRANCH (g-357-89): "the harness re-invokes on completion" is a Claude
# Code fact (run_in_background → task notification). Ask, never assume:
Bash: bash core/scripts/harness-capabilities.sh --get background_job_notify   # true | false (unknown harness → false: a spare net is harmless, a missing one is a dead loop)
IF true:
  Bash: {quiescence_sleep_env or dry_sleep_env:-} bash core/scripts/interruptible-sleep.sh {sleep_seconds} (run_in_background=true)
  RETURN   # yield contract (guard-153): the bg sleep IS the terminal tool call; the harness
           # re-invokes on completion/wake — no ScheduleWakeup arm, no synchronous Skill
           # re-entry (that re-entry is the dry spin this branch exists to stop — g-115-2084).
IF false (no background-job notification — measured on a downstream harness 2026-09-03: five launch attempts, three sleep processes, a 2-minute "quiet window"):
  Bash: {quiescence_sleep_env or dry_sleep_env:-} bash core/scripts/interruptible-sleep.sh {sleep_seconds} &   # ONCE. The tool may time out on this call — the process survives; a repeat launch JOINS the live sleep ("idle-sleep JOINED"), it never spawns another
  ScheduleWakeup(prompt="<<autonomous-loop-dynamic>>", delaySeconds=min({sleep_seconds}+60, 3600))   # TERMINAL call: the wake IS the re-entry here (rb-9668; sanctioned carve-out of schedule-wakeup-correctness.md Anti-pattern C). A remainder past 3600s re-sleeps via Phase -0.5e — which is why B7 MUST have written blocked_sleep_until
  RETURN   # no Skill(aspirations), no further Bash, no prose
```

> **RETURN-PROTOCOL TRAP — read before you write anything after the Bash call (g-115-770, zeta session 74).**
>
> The `interruptible-sleep.sh ... run_in_background=true` Bash call above — or, on
> a no-notify harness, the sized `ScheduleWakeup` after it — **IS the terminal
> tool call** for the RETURN path. **NO prose may follow it** —
> not a "Summary of this iteration", not "the autonomous loop is now in a
> self-recovering blocked state", not a ✶ Insight block, not a "going idle"
> sign-off. The tool call is LAST, period.
>
> **Why this branch is a peak attractor for the loop-killer.** Two forces
> stack here:
> 1. `run_in_background=true` returns control to you **immediately** — the
>    sleep is detached, so this call does NOT *feel* terminal the way a
>    blocking command does. The instinct is to then narrate what just
>    happened. That instinct is exactly the trap from
>    `.claude/rules/return-protocol.md` → "The Trap" (the agent emits a
>    "comply" summary, the turn ends on prose, the loop dies silently).
> 2. You just ran B0–B6.7: board scan, constraint-aware generation,
>    evolution, research, full-cycle reflection, MW#5 synthesis. After heavy
>    diagnostic work the urge to summarize the investigation is strongest —
>    and the all-blocked exit is precisely where it lands as trailing prose.
>
> zeta session 74 did exactly this: it correctly handled all-blocked, then
> ended the turn with "The autonomous loop is now in a correct, honest,
> self-recovering blocked state. Summary of this iteration ...". Caught only
> by the generic Layer-B Stop-hook BLOCK; Layer C was blind to the shape
> until g-115-770 FIX 1 de-blinded `trailing-text-detector.py`
> (`phase_summary`). Do not reproduce it.
>
> **If a summary or insight is genuinely needed, emit it INLINE _before_ the
> B7.2 Bash call** — Insight FIRST, tool LAST (same ordering rule as
> `return-protocol.md` Decision Procedure and the
> `return-protocol-vs-explanatory-style` tree node §"Decision Procedure").

## Return Protocol

This skill's terminal actions (either `LOOP_CONTINUE` via `Skill('aspirations') with args='loop'`
OR `interruptible-sleep.sh` via Bash with `run_in_background=true`, OR — on a harness that cannot
notify on background-job exit — `ScheduleWakeup` sized to the registered sleep, g-357-89) are tool
calls, satisfying the return-protocol requirement. See `.claude/rules/return-protocol.md`.

## Chaining

- **Called by**: `/aspirations` orchestrator when `/aspirations-select` returns `all_blocked`
- **Calls**: `/create-aspiration`, `/aspirations-evolve`, `/research-topic`, `/reflect --full-cycle`,
  notification (resolved via forged skill whose triggers match "notify the user"); many scripts
  (`board-read.sh`, `board-post.sh`, `goal-selector.sh`, `pipeline-add.sh`, `experience-read.sh`,
  `aspirations-add-goal.sh`, etc.)
- **Reads**: known_blockers (WM), proactive_escalation_log (WM), selection_context (input),
  recent board messages, pipeline accuracy
- **Writes**: blocked_sleep_until (WM), proactive_escalation_log (WM), agent-queue goals,
  new aspirations, pipeline hypotheses, board posts
