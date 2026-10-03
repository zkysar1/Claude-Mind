#!/usr/bin/env python3
# domain-leak-exempt: framework store-hygiene infra — calibration constants only, no domain strings.
"""Per-store thresholds for store_dupe_warn's SEMANTIC near-duplicate tier
(g-306-574 unit C, alpha worker 7659f585, 2026-10-02).

CALIBRATION (pre-registered rule, matched-surface sweep — the candidate and
the index rows are both embedded on retrieve.supplementary_text_parts, the
builder's own surface; twin-candidate self-row excluded from every neighbour
set; 300 samples per store, rng seed 42; twin members excluded from the floor
sample because they are true positives, not noise):

  index:    throwaway full-corpus build, 21,201 docs, all-MiniLM-L6-v2,
            fastembed, dim 384 (zc-10, 1043.6 s build)
  twins:    the goal's named set, re-censused: guard-1485 vs guard-1486 is the
            ONLY surviving live pair (LIVE + active). rb-3927 vs rb-4038 and
            rb-4038 vs rb-615 are NOT live records (reader not_found x3 +
            strict-id grep 0) — the missing anchors, recorded, not calibrated on.

  guardrails  twin cosine 0.6227 (both directions; rule-vs-rule direct 0.5546);
              twin ranks 28th / 13th among 7,086 other guardrails (self
              excluded) — BELOW the non-twin nearest-neighbour floor
              p50 0.6788 (p99 0.8832, max 1.0 — the live store already
              carries verbatim duplicates, which is the class this tier does
              catch). C_twin - 0.03 < p99, so the pre-registered rule yields
              the midpoint, flagged THIN-MARGIN: NO threshold separates the
              reworded twin from the unrelated-record floor on this model.
              threshold 0.753  (THIN-MARGIN, twin recall UNVALIDATED)
  reasoning-bank      no surviving live twin -> p99 rarity bound
              threshold 0.844  (UNVALIDATED)
  pattern-signatures  no surviving live twin -> p99 rarity bound
              threshold 0.73   (UNVALIDATED)

  full sweep: core/scripts/../../agents/alpha/sessions/7659f585f5bf4396a44686eb6bf1f148/scratch/g306574c/calibration-v2.json
  (machine-local session scratch — re-run with the same script + a fresh
  index build to reproduce; guard-1511: margins are recorded in that artifact,
  both sides — below C_twin and above p99.)

DO NOT carry these across a rebuilt index or a different model (guard-1511):
cosines are model-specific. If embedding-index-build.py rebuilds with another
model name, or the sweep is re-run on a materially changed corpus, re-sweep
and update both the constants and the docstring — the index meta.json model
field is the source of truth the query side is already checked against by
_embedding_retrieval.
"""
# The model these cosines were measured with. store_dupe_warn's semantic tier
# REFUSES TO SERVE when the box's index was built under any other model —
# a cosine from a different model is not comparable to these numbers
# (guard-1511). The query side checks _embedding_retrieval.index_model()
# against this name on every call.
CALIBRATED_MODEL = "all-MiniLM-L6-v2"

SEMANTIC_THRESHOLDS = {
    "guardrails": 0.753,
    "reasoning-bank": 0.844,
    "pattern-signatures": 0.73,
}
