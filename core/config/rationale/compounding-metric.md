# Compounding-Knowledge Metric -- Design (g-303-18, Phase 1)

**Author:** zeta | **Date:** 2026-06-24 (Phase 1 design) | **Status:** SHIPPED -- Phase 2 implemented in g-303-35 (`core/scripts/compounding-events.py` + iteration-close Step 8.79 emit wiring), dormant behind the default-OFF `compounding_metric.enabled` flag.
**Referenced from:** `core/scripts/compounding-events.py` module docstring (cites Sections 4-9 of this doc). Promoted from `agents/zeta/temp/` (gitignored) to this committed path via `/drain-temp` 2026-06-27 so the shipped code's design reference resolves in every clone.
**Origin:** Alpha session-60 magic-wand #4.

---

## 1. Problem

Framework size is monotonic. Verified 2026-05-12: 994 tree nodes, 712 reasoning-bank entries, 286 guardrails; counts have only grown since. The enforcement gradient is additive-only (four layers push "encode more"; only an advisory whisper pushes "retire" -- see `learning-philosophy.md` rule 5). **Without a compounding signal, growth looks identical to improvement.** We cannot currently answer: "Of the 994 nodes / 712 RB / 286 guardrails, which ones actually *paid off* by shaping later work, and which are dead weight inflating retrieval and carrying maintenance cost?"

The needed signal: **which encoded knowledge gets retrieved AND is load-bearing in a subsequent goal.** Retrieval alone is not the signal (an entry can be retrieved every time and never change a decision). Load-bearing is the signal.

## 2. Core definitions

- **Compounding event**: a single instance of a stored knowledge entry (tree node, RB entry, or guardrail) being *retrieved* during the execution of some goal G, AND being *load-bearing* in G.
- **Load-bearing**: the retrieved entry shaped a decision in G that produced an EXTERNAL ARTIFACT -- a git commit, an encoding write (tree/RB/guardrail/experience), or a blocker resolution. "Shaped a decision" is stricter than "was referenced": the entry's content must have changed what G did, not merely appeared in G's retrieval manifest.
- **The distinction from existing utilization (critical -- this metric is NOT a rename of an existing one):**

  | Signal | Question it answers | Where it lives today |
  |--------|---------------------|----------------------|
  | tree-node `retrieval_count` | Was it retrieved? | tree node front matter |
  | RB/guardrail `utilization.times_helpful` / `times_noise` | Was it *referenced in execution* (helpful) vs ignored (noise)? | RB/guardrail JSONL; written by `utilization-feedback.py` (Phase 4.26) + inferred by the learning-gate |
  | **compounding_events (NEW)** | Was it **load-bearing** -- did it shape a decision that produced an external artifact? | proposed below |

  An entry can have high `times_helpful` and ZERO compounding events: retrieved often, referenced, but never decision-shaping in a goal that produced an artifact. That gap is exactly the signal we lack. Compounding is the strict superset-condition on top of "helpful."

## 3. Design constraints (binding)

From the goal:
- **C1 -- No reflexive self-citation.** A retrieval event must PRECEDE the use it is credited for, and the consuming goal must NOT be the entry's own `source_goal`. An entry cannot compound by being written.
- **C2 -- Conservative heuristics.** "Cheap heuristics will be wrong." Bias toward `was_load_bearing=false` on ambiguity; a false-negative (missed credit) is recoverable, a false-positive (inflated credit) corrupts the retirement signals downstream.

From Phase 2.27 cross-cutting retrieval (load-bearing -- do not drop):
- **C3 -- Value-density, not raw count (guard-841).** Any counter used as a retirement / weakness / staleness signal MUST be value-density oriented, not raw-count. The headline metric is therefore a RATE / distinct-goal count (Section 9), never `len(compounding_events)`.
- **C4 -- Never auto-prune on zero (guard-809).** The D2 tree-archival consumer MUST NOT prune or merge a leaf because its compounding count is 0 (or because article_count is 0). Zero-compounding is an ADVISORY ranking signal for review, never an automatic deletion trigger (Section 10).

## 4. Schema: the compounding event

One event object:

```json
{
  "entry_id": "rb-2320 | guard-868 | <tree-node-key>",
  "entry_kind": "reasoning_bank | guardrail | tree_node",
  "retrieved_by_goal_id": "g-303-18",
  "retrieved_at": "2026-06-24T13:00:38",
  "was_load_bearing": true,
  "confidence": "high | medium",
  "artifact_produced": "commit | encoding_write | blocker_resolution",
  "artifact_ref": "rb-2320 | <commit-sha> | <node-key> | <blocker-id>",
  "evidence": "free text -- the specific decision the entry shaped",
  "source_goal_of_entry": "g-115-1635",
  "experiment_version": "compounding-v1"
}
```

`source_goal_of_entry` is carried so the C1 self-citation guard (`retrieved_by_goal_id != source_goal_of_entry`) can be enforced at read time without a second lookup.

## 5. Storage decision: sidecar JSONL vs embedded array (RECOMMENDATION)

The goal proposed embedding a `compounding_events` array on each tree node / RB / guardrail record. I recommend a **sidecar append-only store** instead, presented with the trade-off so review can decide.

| | Embedded array (goal's proposal) | **Sidecar `world/compounding-events.jsonl` (recommended)** |
|---|---|---|
| Write cost per event | RMW of the large RB/guardrail JSONL or rewrite of a tree-node `.md` | single append line |
| Lock contention | High -- 6 agents share `reasoning-bank.jsonl` / `guardrails.jsonl`; per-event RMW serializes them | None -- append-only, no read-before-write |
| File bloat | RB/guardrail records and tree front matter grow unbounded | event volume isolated from hot records |
| Aggregation | Already co-located | Read-time fold keyed by `entry_id` (cheap; done by the velocity report + the D1/D2 consumers on demand) |
| Pruning old events | Must rewrite the host record | Archive/rotate the JSONL like `changelog.jsonl` |

**Recommendation: sidecar.** Rationale (first-principles + `communication-clarity.md` rule 4, elegance is subtraction): the embedded array couples a high-frequency append to the lowest-frequency, highest-contention, shared hot files in the system. The sidecar is a strict reduction in moving parts on the write path and aligns with the existing `changelog.jsonl` / `gate-firings.jsonl` append-only telemetry pattern. The only thing the embedded array buys -- co-located reads -- is recovered by a `entry_id -> events` fold that the consumers already need to compute density anyway (Section 9). If review prefers the embedded form for locality, the schema in Section 4 is identical either way; only the write site changes.

Sidecar lifecycle: append-only at `world/compounding-events.jsonl` (shared, world-level -- a node retrieved by any agent compounds for the whole world). Rotated/archived by size like the changelog. Accessed exclusively via a script (`compounding-events.py` add/aggregate), never edited by the LLM directly -- same rule as all JSONL stores.

## 6. Instrumentation pipeline (where events are emitted)

Two candidate emission points. I recommend **deriving at iteration-close, reusing the existing retrieval manifest**, rather than the goal's proposed new `retrieval-events.jsonl` + a fresh Step-8 walker.

**Goal's proposal:** `retrieve.sh` writes a per-returned-entry line to `agent/session/retrieval-events.jsonl`; `aspirations-state-update` Step 8 walks the prior-turn log and writes `compounding_events`.

**Recommended refinement -- piggyback on retrieval-session.json + utilization-feedback (already wired):**
- `retrieve.sh --goal` ALREADY writes `agents/<agent>/session/retrieval-session.json` (the retrieval manifest: which tree nodes / RB / guardrails were returned, with `supplementary_detail` entry ids).
- Phase 4.26 `utilization-feedback.py` ALREADY attests, per entry, the `helpful` (referenced-in-execution / ACTIVE-deliberation) vs `noise` classification, and the learning-gate infers it when Phase 4.26 is skipped.
- So the load-bearing JOIN can be computed at **iteration-close (learning-gate / state-update)**, where the goal's OUTCOME and its produced ARTIFACTS are already known, with NO new per-retrieval logging:

  ```
  compounding_event(entry) is emitted IFF
      entry in retrieval-session.json ACTIVE/helpful set   # was referenced/decision-relevant
      AND goal produced an external artifact this iteration  # commit OR encoding write OR blocker resolution
      AND retrieved_at < artifact_write_time                 # C1 temporal order
      AND goal_id != entry.source_goal                       # C1 self-citation guard
      AND load-bearing confidence >= medium                  # C2 conservative (Section 7)
  ```

**Why the refinement:** it reuses two already-wired signals (manifest + helpful-attestation) and one already-computed fact (did this iteration produce an artifact -- the learning-gate already knows encoding writes; `git diff --stat` already runs in Phase 4-post). It avoids (a) a new per-retrieval JSONL written on EVERY `retrieve.sh` call across all agents, (b) session-end archival of that log, and (c) a second walker that re-derives what utilization-feedback already attested. Fewer moving parts, same signal. If review finds the manifest's `helpful` set too coarse to imply "load-bearing," fall back to the goal's explicit `retrieval-events.jsonl` -- but start with the reuse path and measure.

## 7. Load-bearing attribution heuristic (tiered, conservative -- C2)

`was_load_bearing` is set by a tiered rule; default is FALSE.

- **HIGH confidence (was_load_bearing=true):** the `entry_id` is explicitly named in one of: the goal's `decision_log`, the experience-trace `enabled_by` array (Phase 4.27 already records causal enablers), or the commit message / encoding write the goal produced. Explicit citation is the gold signal.
- **MEDIUM confidence (was_load_bearing=true):** the entry was in the ACTIVE deliberation set (`helpful` in the manifest, NOT merely returned) AND the goal produced an external artifact AND there is substantive token-overlap between the entry's content/lesson and the produced artifact's content (above a threshold to be calibrated in Phase 2). Temporal order C1 must hold.
- **Otherwise (was_load_bearing=false):** retrieved-but-not-active, no artifact produced, overlap below threshold, ambiguous, or any C1 violation. Recorded as a NON-load-bearing retrieval event (still useful: it is the denominator for the density rate in Section 9).

Calibration plan (Phase 2): start strict (HIGH only). Add MEDIUM once HIGH-only volume is measured and the token-overlap threshold can be set against real data. Per C2, prefer under-crediting.

## 8. Anti-inflation rules (consolidated)

1. **Self-citation excluded (C1):** `retrieved_by_goal_id != source_goal_of_entry`. An entry never compounds by being authored.
2. **Temporal order (C1):** `retrieved_at` must precede the artifact-write the credit rests on. Retrieval-after-write is reflexive and excluded.
3. **Active, not merely returned:** only entries in the ACTIVE/helpful deliberation set qualify -- a retrieval that returned 15 nodes does not compound 15 entries; it compounds the ones that were actually used.
4. **Distinct-goal de-duplication (the core anti-inflation, C3):** the headline value is DISTINCT consuming goals, not event count. One chatty goal citing an entry five times is ONE compounding goal, not five. (Section 9.)
5. **No meta-pattern double-count:** an entry credited via a hypothesis-resolution path (reflect-on-outcome CONFIRMED/CORRECTED) is not also credited here for the same goal -- mirrors guard-575's existing rule for pattern-signature outcome recording.

## 9. Headline metric: compounding density (value-density per guard-841 / C3)

The raw `len(compounding_events)` is explicitly NOT the metric (guard-841). The reported signals are densities:

- **Per-entry load-bearing rate** = `distinct_goals_load_bearing / distinct_goals_retrieved_in`. "When this entry is retrieved, how often does it actually carry weight?" Range 0..1. Low rate + high retrieval = reference-y / noise candidate.
- **Per-entry compounding reach** = `count(distinct retrieved_by_goal_id where was_load_bearing)`. How many distinct downstream goals it has actually shaped. (Distinct-goal, per rule 4 -- not event count.)
- **Per-category compounding density** = `sum(load-bearing distinct-goal events in category) / count(entries encoded in category)`. The framework-velocity headline: "is category X's knowledge compounding, or just accumulating?" A category with 80 entries and near-zero density is accumulating dead weight.

These three are read-time folds over `compounding-events.jsonl`. None is a raw count of events.

## 10. Downstream consumers (ADVISORY only -- C4)

- **D1 -- guardrail retirement:** the scar-tissue / retirement review PREFERS (ranks higher for review) guardrails with a low compounding reach over a long window. It surfaces them for judgment; it NEVER auto-retires. Combine with existing `utilization.times_active`.
- **D2 -- tree archival:** archival review PREFERS nodes with zero compounding reach as review candidates -- but **NEVER prunes/merges a leaf because compounding (or article_count) is 0 (guard-809).** Zero-compounding is a "look here first" ranking input to the EXISTING archival gates, not a new deletion path. This is the single most important safety constraint in this design: a brand-new correct node has zero compounding until it is first retrieved; auto-pruning on zero would delete fresh knowledge.
- **Velocity reports:** cite per-category compounding density (Section 9) as the real "is the framework improving, not just growing" metric -- the direct answer to the Section 1 problem.

All three are PREFERENCE/RANKING signals feeding existing human/agent-gated review, never autonomous mutation.

## 11. Relationship to existing machinery (reuse, do not duplicate)

| Existing | Reused how |
|----------|------------|
| `retrieval-session.json` (retrieve.sh --goal) | source of the per-goal retrieval manifest + active set -- the JOIN's left side |
| `utilization-feedback.py` / learning-gate inference (Phase 4.26 / 9.5) | the `helpful`/ACTIVE attestation -- "was referenced," the precondition for load-bearing |
| Phase 4.27 causal-enabler scan (`enabled_by` on experience) | HIGH-confidence explicit citations -- already records "entry X provided foundation for goal Y" |
| Phase 4-post `git diff --stat` + learning-gate encoding-write tracking | the "did this iteration produce an external artifact" fact |
| `changelog.jsonl` / `gate-firings.jsonl` append pattern | the sidecar store's lifecycle template |

The compounding metric is a thin DERIVED layer over signals the loop already computes -- it adds the "load-bearing" predicate (active AND artifact-produced AND temporal-order AND distinct-goal) on top of "helpful," and folds the result into density. It is NOT a parallel retrieval-tracking system.

## 12. Open questions for review (Phase 1 -> Phase 2 gate)

1. **Storage:** sidecar (recommended, Section 5) vs embedded array (goal's proposal). Decision needed before Phase 2.
2. **Emission point:** derive-at-iteration-close reusing the manifest (recommended, Section 6) vs new `retrieval-events.jsonl`. Decision needed.
3. **MEDIUM-confidence token-overlap threshold:** start HIGH-only? Defer MEDIUM until HIGH volume is measured? (Section 7.)
4. **Tree-node load-bearing:** tree nodes lack a direct `utilization.times_helpful`; their active-set membership comes from the learning-gate's `inferred_helpful`. Is that strong enough for MEDIUM, or HIGH-only (explicit `enabled_by` / decision_log citation) for tree nodes?
5. **Backfill:** start fresh from Phase-2 ship date (recommended -- no retroactive attribution; cleaner), or attempt a one-time backfill from existing experience `enabled_by` arrays?
6. **Cross-world:** compounding is world-level (recommended). Confirm no per-agent partitioning is wanted.

## 13. Phase 2 implementation plan (separate goal, filed after this review)

1. `core/scripts/compounding-events.py` -- add (append one validated event) + aggregate (read-time density folds, Section 9). JSONL-store discipline: script-only access.
2. Wire emission into `iteration-close.sh` learning-gate / `aspirations-state-update` Step 8: compute the JOIN (Section 6) from `retrieval-session.json` + the iteration's artifact signal, append events behind a feature flag (`compounding_metric.enabled`, default OFF -- dormant until validated, matching the gate-d / anticipatory-reflection ship discipline).
3. Tests: `test_compounding_events.py` -- C1 self-citation exclusion, temporal-order rejection, distinct-goal de-dup (rule 4), density math, value-density (no raw-count leakage, guard-841), and a guard-809 regression asserting the D2 consumer NEVER emits a prune action on zero.
4. Downstream consumers (D1/D2/velocity) wired as ADVISORY ranking inputs only -- separate follow-up goals, gated on the metric having accumulated enough data to be meaningful (calibration window, like the health-ledger 30-day/50-record gate).

**Anti-inflation and guard-809/guard-841 compliance are the acceptance criteria for Phase 2, not afterthoughts.**
