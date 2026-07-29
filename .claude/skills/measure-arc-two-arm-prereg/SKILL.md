---
name: measure-arc-two-arm-prereg
description: "Runs a preregistered two-arm (ON/OFF) live measurement of an ARC-AGI-3 solver mechanism: registers the design plus PRIMARY/SECONDARY/TERTIARY thresholds and zero-discretion verdict branches BEFORE any run, stages two sequential main.py recordings that are identical except one flag delta, extracts ARC-METRICS, runs the offline trend analyzer over both recordings, then emits the mechanical verdict plus a results md/json commit. Fires whenever a goal asks to measure, ablate, or A/B a solver flag or strategy on ARC (for example 'measure solver-v2 impact', 'two-arm ARC experiment', 'preregister the AEVS trend', 'ON/OFF ablation', 'does state-graph help the score'). MUST use this skill for any ARC ON/OFF strategy-impact claim — never eyeball two runs or pick a verdict after seeing the results (guard-1128). Registering thresholds first is what makes a null or inverted result an honest reportable finding rather than a failure."
forged: true
forged_by: echo
forged_date: "2026-07-18"
forged_from: gap-015
user-invocable: false
minimum_mode: autonomous
type: analytical
parent: aspirations-execute
tools_used: [Bash]
conventions: [arc-agi-3-api]
triggers:
  - measure ARC solver impact
  - two-arm ARC experiment
  - preregister ARC measurement
  - ON/OFF ARC ablation
  - AEVS trend measurement
  - measure solver flag impact
  - A/B a solver strategy on ARC
---

# /measure-arc-two-arm-prereg — Preregistered Two-Arm ARC Measurement

Forged from capability gap `gap-015` (encountered on `g-315-303`, `g-315-380`).
Codifies the two-arm preregistered-measurement methodology proven in the real
`analysis/g315303_aevs_trend_preregistration.md`: measure whether an ARC-AGI-3
solver mechanism (a flag, a store, a strategy) changes behavior by running two
otherwise-identical live recordings that differ by exactly one flag, comparing
them through a single offline pipeline, and reading a **verdict that was decided
before any data existed**.

This skill is the ARC-specific operational runbook. The general experiment-design
convention `world/conventions/strategy-impact-experiment.md` covers a different
vertical (NPC behavior); this skill does NOT duplicate it — it instantiates the
same scientific discipline for the ARC live path.

## Core discipline (non-negotiable)

1. **Register the verdict before the run.** Design, metrics, thresholds, and
   zero-discretion verdict branches are written and timestamped BEFORE any
   measurement run. Choosing or moving a threshold after seeing data is
   `guard-1128` post-hoc verdict selection — forbidden.
2. **One flag delta.** The two arms differ by exactly one flag. Everything else
   (game, episodes, max-actions, seed policy, framework routing) is identical, so
   any divergence is attributable to the mechanism under test.
3. **Attribution control via OFF-arm invariance.** The OFF arm runs unchanged
   code and MUST be byte-identical across runs. If the OFF arm drifts between
   runs, attribution is downgraded (rb-3765/3768) — the divergence can no longer
   be pinned on the mechanism.
4. **Null and inverted results are findings.** A mechanism that does nothing, or
   makes things worse, is a valid, honest, reportable outcome. The prereg makes
   that reportable instead of embarrassing.
5. **Proxy trend is not score.** TERTIARY (score) is reported with NO pass/fail
   threshold — a proxy-metric trend (new-states, sequence diversity) does not
   license a score claim (rb-1500).

## Preconditions

- Curriculum `allow_forge_skill` / analytical-measurement capability unlocked
  (this skill was forged at cur-02).
- The ARC live path is reachable: framework-routed session-open succeeds and the
  target game is locked. If session-open fails, that is an infrastructure
  blocker (probe via the canonical path, file a blocker) — it is NOT a
  measurement null. Do not report a verdict when the live path is down.
- Read `world/conventions/arc-agi-3-api.md` for the live-path invocation
  particulars (game IDs, session-open, per-tick local solver decisions).

## The 7-step runbook

### Step 1 — Preregister (BEFORE any run)

Write or extend a prereg markdown at
`<arc-repo>/analysis/<goal-id>_<mechanism>_preregistration.md`, timestamped
before the first run. It MUST contain:

- **Design.** The two arms — ON (mechanism flag present) and OFF (flag absent) —
  with the EXACT invocation for each (identical except the one flag delta), the
  locked game id, the `episodes × max-actions` budget, and a statement that both
  arms run SEQUENTIALLY through the same framework-routed live path (AyoAI
  session-open, per-tick decisions from the local solver, live ARC API).
- **Metrics.** The per-episode observables the offline pipeline extracts —
  `seq_hash`, `new_states`, `score` — computed by the IDENTICAL pipeline over
  both recordings.
- **Pre-registered hypotheses & thresholds.**
  - PRIMARY: the main effect and its concrete numeric threshold.
  - SECONDARY: an effect-size ratio, pass iff `ON/OFF ratio ≥ 1.2×`.
  - TERTIARY: score — reported with NO pass/fail threshold (proxy ≠ score,
    rb-1500).
  - Each hypothesis gets a **zero-discretion verdict branch**: the verdict is a
    mechanical function of the threshold, decided here, not after the data lands.
- **Attribution control.** Declare the OFF-arm cross-run byte-invariance check and
  that OFF-arm drift downgrades attribution (rb-3765/3768).
- **Outcome-wording delta.** If the goal's claimed outcome differs from the
  mechanism actually under test, state the gap explicitly, up front.

For a RE-RUN, append a dated ADDENDUM registered BEFORE the re-run, repeating the
exact protocol (same arms / game / budget / analyzer / thresholds) and naming the
new mechanism plus its verdict branches. A smoke test validates only the bridge
and produces NO trend data — never count it as a measurement run.

### Step 2 — Stage the run script

Prepare both invocations. Example ON arm:

```
python main.py --game <locked-game-id> --use-solver-v2 --state-graph \
  --action-value-store --episodes 12 --max-actions 200 --record \
  --tags "<mechanism>,on"
```

OFF arm = identical minus the single tested flag (for example drop
`--state-graph`), with `--tags "<mechanism>,off"`. Confirm the game is locked and
no other games run concurrently. Budget = `episodes × max-actions × 2 arms`,
bounded and already declared in the prereg.

### Step 3 — Run both arms sequentially

Launch the arms in the order declared in the prereg, each `--record`ing to its own
recording file. Background long runs (`run_in_background: true`); the harness
notifies on completion — do NOT poll with `ScheduleWakeup`
(`schedule-wakeup-correctness.md` Anti-pattern A/D). Capture each recording path.

### Step 4 — Extract ARC-METRICS

Run the identical offline extraction over BOTH recordings, producing per-episode
`{seq_hash, new_states, score}`. The SAME code path processes both arms — no
arm-specific processing, or the comparison is contaminated.

### Step 5 — Run the offline trend analyzer

Run the registered analyzer over both recordings' metrics:

```
python analysis/g315303_aevs_trend_analysis.py <on-recording> <off-recording>
```

(Use whatever analyzer the goal's prereg registered; the g315303 analysis is the
proven default.) This produces the ON-vs-OFF comparison the prereg thresholds
consume.

### Step 6 — Evaluate the pre-registered verdicts (zero discretion)

Apply the Step 1 thresholds mechanically:

- PRIMARY: pass iff `observed ≥ registered threshold`.
- SECONDARY: pass iff `ON/OFF ratio ≥ 1.2`.
- TERTIARY (score): report the delta; assign NO pass/fail (rb-1500).
- Attribution check: verify OFF-arm cross-run byte-invariance. If the OFF arm
  drifted, DOWNGRADE attribution and say so explicitly.

The verdict is exactly what the branches say. Do NOT re-pick thresholds after
seeing the data (guard-1128). A NULL or INVERTED result is a valid, honest,
reportable finding.

### Step 7 — Write results + commit

Emit `<goal-id>_<mechanism>_results.md` plus a companion `.json` capturing: the
registered design (link the prereg), observed per-arm metrics, each verdict
branch's mechanical outcome, the attribution-control result, and any
outcome-wording delta. Then `git add` + commit BOTH the prereg and the results so
the measurement is auditable and fleet-visible.

## Input / output contract

**Input (from the parent goal / aspirations-execute Phase 4):**
- `goal-id`, `mechanism-under-test` (the single flag delta), `game-id` (locked),
  `analyzer-path`, and the `episodes` / `max-actions` budget.

**Output (to aspirations-execute Phase 4/5 verify):**
- Committed `results.md` + `.json`; the mechanical PRIMARY / SECONDARY / TERTIARY
  verdict; the attribution verdict. Return the verdict summary as the closing
  signal so Phase 5 can verify against the prereg (not against a post-hoc story).

## Error handling

- **Live path down** (session-open fails): infrastructure blocker, not a null.
  Probe with the canonical path; file a blocker if genuinely unavailable. Never
  emit a measurement verdict on a dead path.
- **OFF-arm byte-drift across runs**: attribution downgrade. Report it; do NOT
  claim mechanism attribution.
- **Analyzer error**: fix the pipeline. Never hand-compute a verdict to route
  around a broken analyzer.
- **Post-hoc threshold temptation**: REFUSE (guard-1128). If the prereg's
  thresholds were wrong, that is a lesson for the NEXT prereg's addendum — not a
  live edit to the current one.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The terminal action is the Bash `git` commit of the results md/json (Step 7),
or a Bash echo returning the mechanical verdict summary to the orchestrator.
Never end with a text summary.
