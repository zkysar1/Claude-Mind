# Iteration Close Digest — LLM Residue

Companion to `core/scripts/iteration-close.sh`. That script handles the
mechanical bookkeeping (status updates, streaks, journal index, WM appends,
team-state, diary, board posts, utilization feedback). This digest covers
what ONLY the LLM can do — the judgment calls that required full SKILL.md
context in the old pipeline.

Read this once per session. Reference it at each of the three orchestrator phases.

---

## § VERIFY (Phase 5 — after `iteration-close.sh --phase verify`)

**Always:**

1. **Q1 EVIDENCE** — Name one concrete artifact that would exist only if the
   goal succeeded. Verify it (Read, Grep, command output). If absent: goal is
   NOT verified; status stays `blocked` or `in-progress`.

1.5. **Q1.5 GENERATED CHECKLIST** (TICKing All the Boxes, 2410.03608 — BRD
   Gap 15) — when Q1 passed, generate a 5–10 item yes/no checklist from the
   goal's title/desc/action and evaluate each against the artifact. A failure of
   an item the goal's OWN `verification.outcomes`/`checks` declared = a real miss
   → fail Q1 (conservative gate). A failure of an item the goal's criteria did
   NOT cover → append to `meta/missing-verification-criteria.jsonl` via
   `bash core/scripts/missing-criteria-log.sh` (goal-template-improvement signal,
   not a current-goal failure). Skipped when `outcome_class == routine` or
   `zone == tight`. Full protocol: aspirations-verify SKILL.md § Q1.5.

2. **Q2 NEGATIVE CHECK** — Name the most likely failure mode. Check for it.
   If the goal made a negative claim (e.g., "field X is missing"): run
   `zero-count-gate.py` / `audit-schema-gate.py` / `exhaustive-search-gate.py`
   as applicable. The gates are mechanical; the *claim extraction* is yours.

3. **Q3 INTEGRATION SCOPE** — Unit-only or integration? If integration, spot-check
   at least one call site beyond the immediate change.

4. **Output summary** (one sentence): factual result for handoff + journal.
   Fed to `iteration-close.sh --summary "..."`.

5. **Gate D primary-outcome env (only when `gate-d-check.sh` returns "on";
   GATE-INTEGRITY — omni-blessed text, do not modify):** prefix the
   iteration-close/recurring-close call with `GATE_D_VERIFY_FIRST_PASS=<true|false>
   GATE_D_VERIFY_ESCALATION_DEPTH=<0-3> GATE_D_RETRY_COUNT=<n>`. first_pass=true
   ONLY if Q1-Q3 accepted on the FIRST attempt with no re-execution AND the final
   status is `completed`. **`blocked`/`skipped` goals are first_pass=false, always**
   (amendment 6 — compute from what happened; never default to true). Arm-blind:
   never name or infer the experiment arm.

**On failure:** write the failure summary (one sentence, specific) and pass
it via `--summary` so the diary captures it.

---

## § STATE-UPDATE (Phase 8 — after `iteration-close.sh --phase state-update`)

**Quality flags at invocation time** (deep outcomes only — restores
`improvement-velocity.yaml` signal that was dead-zero across 206/206 goals
through week 17; rb-428 twin, g-115-228). When invoking
`iteration-close.sh --phase state-update`, populate from this iteration's
in-turn tool history:

- `--tree-updated` (boolean flag, no value) — present iff this iteration
  Edit'd a tree node `.md` under `world/knowledge/tree/` OR `_tree.yaml`.
- `--artifacts-count <n>` — count of artifact-creating script calls in
  this iteration (`reasoning-bank-add.sh`, `guardrails-add.sh`,
  `experience-add.sh`, `pattern-signatures-add.sh`, `tree-add.sh` for new
  nodes). Saturates at 5 in `compute_learning_value` — passing a higher
  number is harmless but no extra credit.
- `--encoding-score <0.0–1.0>` — pre-curator estimate of insight richness
  (Step 3 curator gate gives the authoritative score AFTER this call, but
  the velocity calc fires inside the bash call). Use 0.3 baseline; 0.7+
  when the iteration produced distinct findings with concrete values
  (numbers, file paths, thresholds, error codes); 0.0 for ritual outcomes
  with no encoding work.
- `--findings-count <n>` — distinct findings/observations from execution
  (1 finding ≈ "I noticed X" with one concrete claim). Saturates at 4.

Absent flags fall back to argparse defaults (`tree_updated=false`,
`artifacts_count=0`, `encoding_score=0.0`, `findings_count=0`) which produces
`learning_value=0.0` — i.e., legacy callers that don't pass quality flags
keep the pre-fix behavior, no regression.

**Routine outcomes:** nothing below — including the quality flags. The
script's outcome-class dispatch short-circuits `run-all` on routine, so the
flags are never read.

**Deep outcomes only** — perform in order:

1. **Precision extraction (Step 8a)** — scan the execution trace for exact
   values: numbers, file paths with line numbers, error codes, thresholds,
   commit hashes, config values. Build a precision manifest: items of shape
   `{label, value, unit, context}`. When in doubt, INCLUDE.

2. **Key Insights composition (Step 8c)** — 1–3 sentences of qualitative
   insight for the target tree node's "Key Insights" section. Find the node
   via `tree-find-node.sh --text "<goal.category>" --leaf-only --top 1`. This
   is the core LLM-irreducible step.

3. **Curator gate (Step c.5)** — score the insight 0–1 on three dimensions:
   - **Coverage**: does it add info not already in the node? (vague/reinforcing → 0.2; concrete new info → 0.8+)
   - **Specificity**: can I state a concrete fact/threshold/procedure? (exact values → 0.8+; vague feelings → 0.2)
   - **Actionability**: what specific action does it tell me to take? ("be careful" → 0.1; "check X before Y" → 0.8)

   For investigation goals (titles starting Investigate/Research/Audit/Analyze/Diagnose):
   `score = 0.50·coverage + 0.30·specificity + 0.20·actionability`.
   Otherwise: `score = 0.40·coverage + 0.35·specificity + 0.25·actionability`.

   If `score < 0.45`: demote to curator_overflow (WM slot) via `wm-set.sh`. DO NOT write to tree.
   If `score >= 0.45`: write Verified Values + Key Insights to the node via Edit. The T21 PostToolUse hook auto-bumps `last_updated` on the node — no explicit `tree-update.sh --set` call required (guard-531; see encoding-protocol-digest.md §6).

4. **Decision rules (Step 8e)** — bash-enforced via
   `decision-rules-append.sh` (see `phase-bash-enforcement-digest.md` §
   PHASE 8e for the full LLM residue). If a clear IF-THEN emerged,
   pipe `{"if":"...","then":"..."}` to the wrapper; if no rule, pipe
   empty stdin (the wrapper emits `reason=no_rule_passed` for
   aggregate drift detection). Format, dedup (>=70% token overlap),
   and section insertion are all handled by the wrapper.

5. **Consistency scan (Step 8f)** — if the insight changes a factual claim
   already in the node (count, threshold, formula, status), search the rest
   of the node for stale references. Use Edit `replace_all` for unambiguous
   strings; case-by-case for ambiguous ones.

6. **Execution feedback (Step 8.11)** — rate this goal's spec on 3 dimensions (0–1):
   clarity, scope_accuracy, verification_quality. Append to `meta/step-attribution.yaml`
   or equivalent if a score is particularly high/low. Low scores signal goal-spec
   work for the next iteration.

7. **Fresh-eyes dispatch (Step 8.78 — deep outcomes only)** — `iteration-close.sh
   do_state_update` runs `post-state-update-gate.sh deep` and, when it fires,
   writes the full gate JSON (plus a `set_at` stamp) to WM slot
   `fresh_eyes_dispatch_pending` AND prints
   a `[iteration-close] DISPATCH: /fresh-eyes-code required` line to stderr.
   When you see that line in this iteration's output (or when
   `wm-read.sh fresh_eyes_dispatch_pending` returns non-null), invoke
   `/fresh-eyes-code` with the file list from the JSON. After review completes,
   stamp the dispatch timestamp then clear the signal:
   `printf '"%s"' "$(date +%Y-%m-%dT%H:%M:%S)" | wm-set.sh fresh_eyes_last_dispatch`
   then `echo 'null' | wm-set.sh fresh_eyes_dispatch_pending`. The stamp is
   load-bearing (g-115-1553): `stale-sentinel-canary.py` keys on
   `fresh_eyes_last_dispatch` ADVANCING to tell "consumer kept up" from
   "consumer bypassed" — without it the canary false-fires because this
   sentinel is re-armed on every substantive deep close. Stamp on ANY clear,
   dispatch or justified-no-dispatch.
   Dispatch is LLM-only because Skill invocations cannot run from bash.
   Gate owner: guard-343 (threshold spec); caller-wiring: g-248-17 (rb-428
   pattern — same drift class as experience-archival). The bash enforces the
   threshold; the Skill dispatch stays LLM-only — keep this seam.

8. **Outcome-observation hook (deep outcomes only — bash-only, no LLM action)** —
   `iteration-close.sh do_state_update` invokes
   `$WORLD_DIR/scripts/outcome-metrics-collect.sh` at the end of every deep close,
   gated on the convention file `$WORLD_DIR/conventions/outcome-observation.md`
   existing (fail-open on fresh agents). Parallel to Step 8.12 (cold-path inside
   aspirations-state-update SKILL.md) — the hot-path block ensures
   outcome-metrics.yaml advances regardless of whether the state-update phase
   was invoked via the SKILL.md sub-skill or directly via iteration-close.sh
   (which is the common path: /aspirations loop, recurring-close.sh, hand-rolled
   Phase 4 short-circuits, etc.). No LLM residue — listed here only so digest
   readers see the full hook set. Per g-115-747 (Apply) / g-115-742
   (Investigate); 20+ days stale incident traced to hot-path bypass.

### Slow-filesystem backgrounding (deep outcomes) - do NOT wait, do NOT `timeout`

On deep outcomes, `iteration-close.sh --phase state-update` runs
`iteration-commit.sh` (git add/commit, push if configured) in PROJECT_ROOT. On
slow-filesystem deployments (a cloud-synced or network-mounted working tree, or
a large repo) that commit can take long enough that the Bash tool BACKGROUNDS the whole
state-update call. When it does:

1. **Do NOT wait for it.** Proceed IMMEDIATELY to the learning-gate phase (then
   productivity-check). The script writes the loop-state-bump
   (`loop-state-bump-counters.py` -> `goals_completed`/`productive_goals` in WM)
   EARLY, before the slow git tail - so the counters are already durable by the
   time you reach learning-gate; the git commit finishes async and is confirmed
   by the background-task completion notification. An idle turn-ending wait here
   is exactly what lets the Stop hook fire mid-close and ORPHAN learning-gate +
   productivity-check (the close tail never runs; the loop can stall).

2. **NEVER wrap the state-update call in `timeout`.** A `timeout` firing
   mid-commit kills the git op half-done - `goals_completed` unbumped, files
   uncommitted - which then has to be reconciled the next iteration. (Observed
   2026-06-07: `timeout 25 bash iteration-close.sh --phase state-update` killed
   the commit mid-way; re-run without `timeout` fixed it.)

Validated 3x on 2026-06-07: launch state-update,
proceed inline through learning-gate + productivity-check, let the commit land
async - zero orphaning, zero stop-hook re-entry. Same applies to
`recurring-close.sh` deep closes (same commit path).

---

## § LEARNING-GATE (Phase 12 — after `iteration-close.sh --phase learning-gate`)

**All outcomes:**

1. **Meta-learning signal** — ONE question: "Did the way I learned from this
   goal suggest a better procedure?" If yes: append to `meta/meta-log.jsonl`
   via `meta-log-append.sh` with event=meta_signal.

2. **Forced-encoding catch (deep outcomes)** — if state-update's curator gate
   rejected the insight AND the sensory_buffer has items with `encoding_score >= 0.40`
   related to this goal: force-write one to the tree node anyway. This is the
   anti-drift safeguard the framework needs to prevent ritual-outcome sessions.

3. **Unreflected hypotheses** — if iteration-close.sh reported `LLM-ACTION: N
   unreflected hypotheses`, invoke `/review-hypotheses --learn`.

4. **Periodic reflection (every 5 goals)** — if `goals_completed_this_session %
   5 == 0`: ask the 5 reflection questions (patterns / surprises / stale nodes
   / conclusion audit / encoding frequency). Append any finding to sensory_buffer.

5. **Full-cycle (every 15 productive goals)** — invoke `/reflect --full-cycle`
   + `/review-hypotheses --learn`.

6. **Experience archival (deep outcomes only — Phase 4.25)** — primary
   enforcement has moved to `experience-archive-goal.sh` called from
   Phase 4.25 of `aspirations-execute/SKILL.md` (see
   `phase-bash-enforcement-digest.md` § PHASE 4.25 for the full LLM
   residue: reasoning-trace content, verbatim anchors, one-line
   summary). This bullet remains as the retroactive safety net:
   if Phase 4.25 was skipped entirely, `experience-staleness-check.sh`
   (default 12h threshold) fires at productivity-check time, and you
   compose the record here instead. The cross-agent fresh-eyes review
   gate (`cross-agent-recent-changes.sh`) depends on this signal to
   attribute recent changes to the reviewee agent — without it the gate
   silently returns empty and reviews close routine on no evidence.
   Drift pattern documented in rb-428; primary fix shipped via
   g-248-16 (staleness canary); wrapper follow-up extends the pattern
   to proactive enforcement.

   **Compliance gate (rb-428 follow-up)**: when staleness is detected,
   `experience-staleness-check.sh` writes the `force_experience_archival` WM
   sentinel. Precheck Phase 0-pre2 reads the sentinel and blocks goal
   selection in the next iteration until the missed record is composed
   retroactively. Drift is self-correcting within one iteration — compose
   the record in-place (here) to avoid the retro-compose tax, not because
   the canary is optional.

7. **Memory-utility curation scan (every 20 goals — advisory, g-115-1468 / Phase 1d)** —
   if `goals_completed_this_session % 20 == 0`: run the retrieval-utility report
   (the earn-the-keep KPI flip — weight "was this later retrieved AND useful?"
   over "how much did we write?") over both stores:
   `export MIND_AGENT=<agent>; source core/scripts/_paths.sh`, then for each of
   `reasoning-bank` and `guardrails`:
   `py -3 core/scripts/retrieval_utility_report.py --store "$WORLD_DIR/<store>.jsonl"`.
   From each report take `zero_hit_high_exposure` (retrieved >=5x, never helpful —
   noise) + `never_retrieved` (dead weight); write the counts + a capped sample
   (first 25 ids each, per store) to the `memory_curation_candidates` WM slot
   (overwrite via `wm-set.sh`) for `/reflect --curate-memory` to act on.
   ADVISORY ONLY — surface, never act: NEVER auto-retire from this scan
   (guard-707) — a low `times_helpful` is usually UNDER-ATTESTATION, not zero
   value; retirement is a `/reflect`-gated decision with its own evidence bar.

---

## After learning-gate

Run `iteration-close.sh --phase productivity-check`. This invokes the
productivity stop gate (`productivity-stop-gate.sh`). If the gate triggers,
`stop-requested` is set and the orchestrator's Phase -1.4 handles graceful
shutdown on next loop entry. Parameters: `core/config/aspirations.yaml` →
`productivity_gate`.

**Then `LOOP_CONTINUE`** — save `loop_state` to WM and re-invoke
`Skill('aspirations') with args='loop'`. Text-only output at this point kills
the session.
