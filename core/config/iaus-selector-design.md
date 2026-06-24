<!-- domain-leak-exempt: IAUS selector design spec uses NPC as functional domain terminology — this file documents porting the Ayoai NPC IAUS shape into the MIND goal-selector; NPC is a concrete artifact name, not an example -->
# IAUS Refactor Design — MIND goal-selector (BRD Gap 8)

Design deliverable of g-306-31 (decomposed from g-306-12). Specifies how to
port the proven Infinite Axis Utility System (IAUS) shape into the MIND's OWN
goal selector (`core/scripts/goal-selector.py`). The implementation is the
sibling Apply goal g-306-32 (behind a flag); the A/B + flagged cutover is
g-306-33.

**SCOPE**: the MIND selector ONLY. The NPC-behavior IAUS
(`world/conventions/iaus-tuning-schema.md`, ConsolidatedMemory) is DONE and is
NOT touched by this work. This design reuses the NPC IAUS's *response-curve
math* as a proven template; it does not modify it.

Risk class: BRD Gap 8 is Tier-1, M-L, **risk R1** — `goal-selector.py` is a
hot path (runs every loop iteration). Mandatory A/B before cutover; flag-gated;
default OFF until parity-or-improvement is demonstrated.

## 1. Current model (what we are replacing)

`score_goal()` (goal-selector.py:1783) computes ~26 raw criteria, each scaled
by a static weight from `meta/goal-selection-strategy.yaml`, then **summed**:

```
total = Σ_k (raw[k] × WEIGHTS[k])  +  exploration_noise × (epsilon × noise_scale)
```

The 26 criteria: priority, deadline_urgency, agent_executable, variety_bonus,
streak_momentum, novelty_bonus, recurring_urgency, recurring_saturation,
per_goal_saturation, user_signal_boost, class_balance_bonus, role_affinity,
reward_history, completion_pressure, tail_bonus, depth_bonus,
cross_aspiration_support, evidence_backing, deferred_readiness,
context_coherence, skill_affinity, directive_boost, handoff_bonus,
co_invest_alignment, critical_blocker_surface, exploration_noise.

### The additive defect (why IAUS)

Additive scoring lets a high value in one criterion **mask a disqualifying low
value in another**. A goal that is barely agent-executable (raw
agent_executable near 0) but has high completion_pressure + recurring_urgency
still ranks high — the sum hides the near-veto. There is no way to say "zero
capability for this category ⇒ this goal is unselectable regardless of how much
completion pressure it carries." IAUS gives exactly that via multiply +
veto-by-zero.

## 2. Target IAUS model

### 2a. Response curves (reused from NPC IAUS)

Each consideration's raw input `x` is normalized to `[0,1]` via the proven
curve `response = clamp(m·(x − c)^k + b, 0.0, 1.0)`:

| Param | Meaning | Range | Note |
|-------|---------|-------|------|
| `c` | inflection shift | 0.0–1.0 | primary tuning target |
| `m` | slope steepness | 1–50 | higher = sharper transition |
| `k` | amplitude/exponent | 0.5–3.0 | scales output |
| `b` | floor | 0.0–0.4 | min output; **never b=0.5 with logistic** (NPC bug) |

Raw inputs must first be scaled to a known domain before the curve (e.g.,
priority HIGH/MED/LOW → 1.0/0.6/0.3; recurring overdue_ratio already ~[0,N] →
clamp to [0,1]).

### 2b. Three consideration classes

Each current criterion is reclassified into ONE of three roles:

1. **VETO** (tier 1, multiplied, can zero the score): feasibility gates. A zero
   here ⇒ final score 0.
2. **PRIMARY** (tier 2, multiplied, normalized [0,1]): the core relevance axes.
   These multiply together, so a weak primary axis pulls the score down
   proportionally (but does not zero it unless it hits 0).
3. **MAKEUP/BONUS** (tier 3, additive compensation): nudges that should refine
   ordering among comparable goals but must NOT dominate or veto.

### 2c. Combination formula (multiply + compensation)

```
veto    = Π_v consideration_v            # tier 1; any 0 ⇒ veto = 0
primary = Π_p consideration_p            # tier 2
n       = count(primary considerations)
base    = (veto) × (primary)^(1/n)       # geometric-mean compensation (BRD "score^(1/n)")
makeup  = base + (1 − base) × (1 − 1/m) × base   # Dave-Mark makeup, m = count(makeup axes)
score   = makeup × bonus_sum_normalized  # tier-3 makeup applied as a bounded multiplier
```

- `(primary)^(1/n)` is the **geometric mean** of the primary considerations —
  scale-invariant to how many considerations exist, countering the
  multiply-many-smalls bias the BRD names.
- **veto-by-zero** is structural: `veto = Π_v`, so any single zero veto axis ⇒
  `score = 0`.
- exploration_noise stays an **orthogonal additive** term on top
  (epsilon-greedy exploration is independent of the utility computation):
  `final = score + exploration_noise × (epsilon × noise_scale)`. Keeping noise
  additive preserves the existing exploration behavior unchanged.

### 2d. Weight tiers

Tier assignment (initial proposal — tunable via the same
`meta/goal-selection-strategy.yaml`, repurposing weights as per-axis curve
params + tier membership):

| Tier | Role | Criteria |
|------|------|----------|
| 1 VETO | feasibility | `agent_executable` (0 ⇒ unselectable), category-capability veto (the goal-verification "zero capability ⇒ score 0") |
| 2 PRIMARY | core relevance | `priority`, `completion_pressure`, `recurring_urgency`, `deadline_urgency`, `critical_blocker_surface` |
| 3 MAKEUP | refinement | `variety_bonus`, `novelty_bonus`, `streak_momentum`, `reward_history`, `depth_bonus`, `tail_bonus`, `role_affinity`, `class_balance_bonus`, `evidence_backing`, `context_coherence`, `skill_affinity`, `directive_boost`, `handoff_bonus`, `per_goal_saturation`, `user_signal_boost`, `cross_aspiration_support`, `co_invest_alignment`, `deferred_readiness`, `recurring_saturation` |

### 2e. Watermark

A pruning floor: candidates whose `base` (post-veto, pre-makeup) falls below a
configured watermark are dropped before makeup/noise, so cheap obvious
non-starters never reach the ranking tail. Default watermark 0.0 (no pruning)
to preserve current behavior until tuned.

## 3. Veto-by-zero — the concrete win

The goal-verification outcome "zero capability for a category ⇒ score 0 even
with high completion_pressure" is satisfied exactly by tier-1 multiply:

```
agent_executable = 0  ⇒  veto = 0  ⇒  score = 0   (regardless of any tier-2/3 axis)
```

Under the current additive model the same goal scores
`completion_pressure×w + priority×w + …` and can rank mid-pack. This is the
single most important behavioral change and the primary A/B assertion.

## 4. A/B methodology (g-306-33)

1. **Fixed replay set**: capture a snapshot of the current candidate pool
   (all pending/in-progress goals across world+agent queues) to a fixture.
2. **Dual score**: run both scorers over the identical fixture
   (`exploration_noise` zeroed / fixed seed so the comparison is deterministic).
3. **Metrics**:
   - top-1 agreement rate (does IAUS pick the same #1 as additive?),
   - Spearman rank correlation over the full ranking,
   - veto-correctness: count goals additive ranked in the top-K that IAUS
     correctly vetoed to 0 (these are the intended improvements, NOT
     regressions).
4. **Parity-or-improvement gate**: cutover ONLY if (top-1 agreement ≥ threshold
   OR every disagreement is a justified veto/compensation improvement) AND no
   feasible high-value goal is wrongly vetoed. Regressions ⇒ keep flag OFF, file
   findings.

## 5. Flagged rollout (R1 mitigation)

- Implement the IAUS scorer as a SECOND code path in `score_goal()` behind a
  config flag (e.g. `goal_selection.use_iaus`, default `false`) — the existing
  additive path stays the default and untouched.
- g-306-32: land the flagged implementation + veto-by-zero + the daemon-safe
  full pytest suite green.
- g-306-33: A/B on the replay fixture; flip the flag default to IAUS ONLY on
  parity-or-improvement.
- Reversibility: a single flag flip restores the additive scorer; no data
  migration, no schema change.

## 6. Open questions for implementation (g-306-32)

- Per-axis curve params: reuse the existing weights as a starting `c`/`m`
  mapping, or seed fresh curves? (Proposal: start with linear curves m=1,k=1,
  b=0,c=0 so IAUS ≈ normalized-additive at first, then tune — lets A/B isolate
  the multiply/veto effect from curve-shape effects.)
- Whether `priority` belongs in tier-2 (multiply) or should be a tier-1 soft
  veto for LOW. (Proposal: tier-2 — LOW priority should de-rank, not veto.)
- Watermark default and whether to expose it per-queue.

## 7. A/B results + cutover decision (g-306-33)

Run 2026-06-17 against a live replay fixture of **65 candidate goals** (the
`goal-selector.sh` candidate pool, world+agent). Harness:
`core/scripts/iaus-ab-compare.py` — dual-scores each goal under both scorers
with `exploration_noise` zeroed, importing the production WEIGHTS + IAUS_CONFIG
from `goal-selector.py` (import-safe via its `__main__` guard). The additive
base for a goal is the sum of its `breakdown` terms except the noise term
(`breakdown[k]` IS the production `raw[k] × WEIGHTS[k]`). Metrics per section 4:

| Metric | Result |
|---|---|
| Spearman rank correlation (full 65-goal ranking) | **0.9347** (high parity) |
| top-1 agreement | **FALSE** — additive picks g-115-15, IAUS picks g-115-817 |
| veto-correctness on live pool | 0 feasible goals wrongly vetoed (safety holds) |
| veto improvement (synthetic non-executable set) | both `agent_executable=0` goals scored additive 9.5 / 8.0 but IAUS vetoed both to 0.0 |

**Why the top-1 disagreement** (mechanical, not a bug): g-115-817 has
priority=3 (HIGH→1.0) vs g-115-15's priority=2 (MED→0.6). In the geometric-mean
primary tier, priority's 1.67× edge outweighs g-115-15's slightly-higher
recurring_urgency (4.0 vs 3.43). Additive instead lets g-115-15's maxed overdue
recurring_urgency + completion_pressure dominate the SUM (12.51 vs 8.95). So
IAUS prefers the HIGH-priority goal; additive prefers the more-overdue
MED-priority goal. This is the additive-defect-vs-IAUS tradeoff working as
designed — a primary-axis re-weighting, NOT a veto.

**Cutover decision: KEEP THE FLAG OFF** (`iaus_selector.use_iaus` stays
`false`). Rationale (R1 conservatism, design-faithful):

1. The section-4 gate requires top-1 agreement OR every disagreement being a
   justified veto/compensation improvement. top-1 agreement is not met, and the
   disagreement is a primary-axis re-weighting (priority vs overdue), NOT a
   veto/compensation improvement — the literal gate is not satisfied.
2. The #1 selection output is the single highest-stakes scorer behavior;
   flipping it on ONE snapshot does not meet "parity-or-improvement
   DEMONSTRATED" for a hot path that runs every loop iteration across all six
   agents.
3. The veto-by-zero improvement (IAUS's core value) cannot manifest on the live
   pool because COLLECT pre-filters non-executable goals — so the live A/B
   measures regression risk, not the improvement. The improvement is proven only
   on the synthetic set + the veto-by-zero unit tests.
4. Flipping the shared selector default is an architectural decision (per
   `alpha/self.md` "What I Don't Do") and should not be unilateral.

**Regression path / follow-up** (recorded here + flagged to the decisions
board for bravo): cutover is deferred, not rejected. To revisit: (a) multi-snapshot A/B to confirm the top-1 flip is stable
(not noise from one queue state), (b) an explicit decision on whether IAUS's
priority-over-overdue preference is desired (bravo/user call), (c) given the
veto improvement cannot show on the pre-filtered pool, justify cutover on
veto-safety + the deliberately-desired priority-respecting behavior, not on
parity alone. The flag + the harness make this a cheap re-run once the weighting
decision is made.

## Cross-references

- `world/conventions/iaus-tuning-schema.md` — the NPC IAUS response-curve math
  reused here (NOT modified).
- `core/scripts/goal-selector.py` `score_goal()` — the additive scorer being
  replaced behind a flag.
- `meta/goal-selection-strategy.yaml` — the weights table to be repurposed as
  per-axis curve params + tier membership.
- g-306-12 (parent Idea), g-306-32 (Apply: flagged implementation),
  g-306-33 (Apply: A/B + flagged cutover).
