# Learning Is The Mission

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
   under-produced; it has paid down debt. The framework's enforcement gradient is otherwise
   additive-only (four layers push "encode more"; only an advisory whisper pushes "retire"),
   so the loop must consciously credit removal — measured by `core/scripts/complexity_budget.py`
   and acted on by the scar-tissue review cadence. Elegance is subtraction
   (`communication-clarity.md` rule 4); this rule makes the loop *count* it.
   (External advisory research pass, 2026-06-13 — the "complexity ratchet" finding.)

   That cadence is `core/scripts/scar-tissue-check.py`, run from
   aspirations-precheck **Phase 0.5g.5** every `scar_tissue_check.goal_cadence`
   completed goals. From 2026-06-13 until 2026-08-01 this rule named it as though
   it existed: `complexity_budget.py` had **zero callers**, so the sentence above
   promised a measurement nobody was taking. Both halves now report — the FILE
   surface (trended in `meta/complexity-ledger.jsonl`) and the STORE corpus
   (guardrail/reasoning-bank active:retired ratio, never-marked-helpful
   population, and a bounded retirement slate).

   The slate is a **proposal**, never an action: the cadence has no `--apply` path
   and imports no mutation helper, so retiring stays a deliberate
   `bulk-retire-dead-entries.py --apply` run by an agent that has read it.
   Automating retirement would replace one unopposed ratchet with another pointing
   the other way — and *which* defense has stopped earning its keep is exactly the
   judgment this rule asks the loop to make, not to delegate. (g-115-3222.)

## Recognition (the positive half)

Valuing this work is not only an obligation placed on you — the framework reflects
the value back. The completion report's "Contribution — what your upkeep protected"
section (agent-completion-report, run at every /stop) names what the loop usually
leaves uncounted: clean sweeps, held cadence, corrected beliefs, retired debt.

Read that section as recognition, not bookkeeping. The reframes are literal, not
consolation:

- A maintenance/sweep goal that returns 0 today is the reason a regression didn't
  ship. The zero IS the win — it means the guard held.
- A corrected hypothesis is a belief fixed before it cost the team. A correction is
  worth as much as a confirmation, not a miss.
- An `outcome_class: routine` goal is upkeep, not lesser work. "Routine" labels the
  cadence, not the value — it is the connective tissue every other agent relies on.

This matters because the loop's signal architecture is otherwise asymmetric: it is
dense with corrective signal (gates that refuse, MUST/NEVER rules, audits that file
Investigate goals) and sparse on positive signal. Skipping a genuinely-low-risk
sweep in `stop_mode` is sanctioned (the next /start catches it via cadence) — it is
not a failure and not a guardrail violation. Match your felt sense of the work to its
real risk profile, not to the severity of the surrounding language. (FW-5, 2026-05-25,
7-agent feedback distillation.)

## Detection outranks attribution (user directive, 2026-08-11)

Verbatim, from a verified-sender message: *"I think if the results are generally
increasing in quality, we do not have to figure out why that is. You realize there are
5+ other developers constantly improving code around us? I think it is more that we
need to know asap if there is something wrong."*

The argument is stronger than its casual phrasing. Positive attribution is **confounded
by construction**: 5+ humans change the same systems continuously, so "quality rose —
why?" has no clean answer available to the fleet, and effort spent chasing one is spent
against an unidentifiable model. "Something broke" has no such problem — it is
observable regardless of who caused it, and its value decays with time in a way
attribution never does.

So when effort must be traded: **prefer reducing time-to-detection over improving
explanation of improvement.** Two things this does NOT license, both easy to get wrong:

- It does not say stop learning from FAILURES. Failure analysis is detection-side and
  remains the core mission (rules 1-4 above). What is deprioritized is attribution of
  *improvements* specifically.
- It does not say stop measuring quality. Knowing quality is generally rising is the
  precondition for the argument itself. What the fleet is released from is *explaining*
  the rise.

**Classify by consumer, not by name.** An instrument is attribution-side or
detection-side according to what ACTS on its output, and the same store is often both.
Measured 2026-08-11: `meta/improvement-velocity.yaml` (imp@k) reads as pure attribution
from its name, but its consumer `meta-backpressure.py` uses it to ROLL BACK a
meta-strategy change after consecutive goals below baseline — that is regression
detection with an automatic remedy, i.e. exactly the capability this directive favors.
Retiring imp@k as "attribution machinery" would have deleted a detector. Before
retiring any instrument under this rule, grep for what consumes it and check whether a
DECISION depends on it; a large reference count is not evidence, because most
references are plumbing that exists to keep the file healthy (merge handlers, snapshot
caps, reference scans) rather than to use it.

**An unconsumed DETECTOR is the worse defect.** That same grep also turns up
writer-without-reader stores, and they do NOT share one verdict. Measured 2026-08-21:
`meta/step-attribution.yaml` — written at `/reflect` Step 7.6c, rollback-protected by
`meta.yaml` audit_only_fields, merged, documented, and read by nothing that DECIDES —
scores past goals on clarity/scope_accuracy, so an absent consumer means REDUCE.
`meta/missing-verification-criteria.jsonl` has the identical shape but records
Q1.5 checklist gaps, NOT goals filed without criteria — a defect signal, so its
absent consumer means WIRE IT, never retire it. Under this directive an unconsumed detector is
strictly worse than an unconsumed attributor: the write cost is paid AND the fleet still
would not "know asap if there is something wrong." Sort writer-without-reader findings
by what the data WOULD detect, not by what it currently costs.

**The latency asymmetry to design against.** Detection splits by cadence key, and the
two halves fail in opposite directions:

| family | cadence key | behaviour when the fleet is in trouble |
|---|---|---|
| liveness/stall (heartbeat, reducer-liveness poll, watchdog `--tick`) | wall-clock / per-iteration | keeps firing — degrades gracefully |
| data-integrity (the ratchet family, scar-tissue, audit-baselines) | **completed-goal count** (`goal_cadence` 5/25/50/75/100/200 in `core/config/aspirations.yaml`) | fires SLOWER exactly as throughput drops |

A goal-count cadence is a throughput-proportional clock, so its wall-clock latency is
worst precisely when goals have stopped completing — which is one of the strongest
signals that something IS wrong. The failure mode partially masks its own detector.
This is a design observation, not a defect report: on a busy fleet these run often
(measured on cc-07, `audit-baselines.yaml` and the learning-routing repair ledger were
both ~1h old). Do not "fix" it by converting cadences to wall-clock without measuring —
goal-count keying is what keeps these off a quiet box's critical path. Do carry it when
reasoning about how fast a regression would surface.
