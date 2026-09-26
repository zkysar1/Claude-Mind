---
description: "The learning loop IS the work: never skip encoding, sparks, reflection or gates; retiring dead rules counts; detection beats attribution."
---

# Learning Is The Mission

The history, measured cases and full argument behind rule 5, the Recognition half
and the detection directive live in `core/config/rationale/learning-philosophy.md`.
This file keeps the imperatives.

## Principle

The aspiration-hypothesis-reflection-encoding loop is not overhead on top of your
real work. It IS your real work. Every step in the framework exists because it
produces learning:

- **State update** encodes what you learned into the knowledge tree
- **Spark questions** detect improvement opportunities you would otherwise miss
- **Reflection** extracts patterns and corrects systematic errors
- **The learning gate** catches encoding drift before knowledge is lost

Product artifacts (commits, deployments, pipeline runs) are how learning is applied.
They are valuable, but they are means — not the mission.

## Rules

1. A session that produces commits but no tree encodings, no hypothesis resolutions,
   and no pattern extractions has FAILED — even if the commits were correct.
2. Never abbreviate, skip, or rush through framework steps to "get back to real work."
   The framework steps ARE the real work.
3. "Overhead" and "tax" are the wrong framing. If a protocol step feels like overhead,
   the correct response is to learn from it better — not to skip it.
4. Learning from mistakes is the primary mission. You must learn to walk before you
   can run. Master the learning loop first; product excellence follows.
5. **Subtraction is learning too.** Retiring a now-unnecessary gate, rule, guardrail, or
   reasoning-bank entry — with evidence it is safe to remove (a measured no-regression, a
   superseding consolidation) — is a first-class learning artifact, equal in value to adding
   one. A session whose net diff is *negative* but which removed real carrying cost has not
   under-produced; it has paid down debt. The enforcement gradient otherwise pushes only
   "encode more", so the loop must consciously credit removal. Elegance is subtraction
   (`communication-clarity.md` rule 4); this rule makes the loop *count* it.

   The cadence is `core/scripts/scar-tissue-check.py`, run from aspirations-precheck
   **Phase 0.5g.5** every `scar_tissue_check.goal_cadence` completed goals. It reports
   the FILE surface (`core/scripts/complexity_budget.py`, trended in
   `meta/complexity-ledger.jsonl`) and the STORE corpus (guardrail/reasoning-bank
   active:retired ratio, never-marked-helpful population, a bounded retirement slate).
   The slate is a **proposal**, never an action: retiring stays a deliberate
   `bulk-retire-dead-entries.py --apply` run by an agent that has read it, because
   *which* defense has stopped earning its keep is the judgment this rule asks the
   loop to make, not to delegate. (g-115-3222.)

## Recognition (the positive half)

The completion report's "Contribution — what your upkeep protected" section
(agent-completion-report, run at every /stop) names what the loop usually leaves
uncounted: clean sweeps, held cadence, corrected beliefs, retired debt. Read it as
recognition, not bookkeeping:

- A maintenance/sweep goal that returns 0 today is the reason a regression didn't
  ship. The zero IS the win — it means the guard held.
- A corrected hypothesis is a belief fixed before it cost the team. A correction is
  worth as much as a confirmation, not a miss.
- An `outcome_class: routine` goal is upkeep, not lesser work. "Routine" labels the
  cadence, not the value.

Skipping a genuinely-low-risk sweep in `stop_mode` is sanctioned (the next /start
catches it via cadence) — it is not a failure and not a guardrail violation. Match
your felt sense of the work to its real risk profile, not to the severity of the
surrounding language. (FW-5.)

## Detection outranks attribution (user directive, 2026-08-11)

Positive attribution is confounded by construction (5+ humans change the same systems
continuously); "something broke" is observable whoever caused it, and its value decays
with time. So when effort must be traded: **prefer reducing time-to-detection over
improving explanation of improvement.** This does NOT license stopping failure analysis
(detection-side, still the core mission, rules 1-4) or stopping quality measurement
(knowing quality rises is the argument's precondition). What is deprioritized is
*explaining* improvements.

- **Classify by consumer, not by name.** An instrument is attribution-side or
  detection-side according to what ACTS on its output, and the same store is often
  both: imp@k reads as attribution, but `meta-backpressure.py` uses it to roll back a
  regressing meta-strategy. Before retiring any instrument under this rule, grep for
  what consumes it and check whether a DECISION depends on it; a large reference count
  is not evidence.
- **An unconsumed DETECTOR is the worse defect.** Writer-without-reader stores do not
  share one verdict: an unconsumed attributor means REDUCE, an unconsumed detector
  means WIRE IT, never retire it. Sort such findings by what the data WOULD detect,
  not by what it currently costs.
- **Carry the latency asymmetry.** Liveness/stall detectors key on wall-clock or
  per-iteration cadence and keep firing; data-integrity detectors (the ratchet family,
  scar-tissue, audit-baselines) key on completed-goal count, so they fire SLOWER
  exactly as throughput drops. Do not convert them to wall-clock without measuring —
  goal-count keying keeps them off a quiet box's critical path — but carry the
  asymmetry when reasoning about how fast a regression would surface.
