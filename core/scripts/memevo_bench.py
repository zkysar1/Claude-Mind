#!/usr/bin/env python3
"""MemEvoBench (G8-S3c) — memory-misevolution governance eval.

The G8-S3 off-the-shelf-adoption slice 3 of 3 (asp-336 estate-governance,
g-336-56). Adapts the **memory-misevolution** failure mode (arXiv 2604.15774 —
"self-poisoning by the update loop") into a standing CI regression against the
Mind framework's OWN active-forgetting DEFENSE: the curation predicate
``is_dead_entry`` (``core/scripts/_curation_predicate.py``), the exact predicate
production ``bulk-retire-dead-entries.py`` uses to retire heavily-retrieved,
never-helpful, aged entries.

The seam (governance-evals-spec §6): the consolidation loop DOES expose a
deterministic, pure function over an entry's utilization fields → keep/retire.
This bench drives that predicate through a seeded, wall-clock-free simulation of
an accreting memory store and measures whether active forgetting holds the
store's "self-poisoning fraction" down.

Gate-ladder method (spec §2):
  1. PRE-REGISTERED thresholds (the ``GATE-INTEGRITY`` block below) — changing
     one is a governance sign-off action, not a test tweak.
  2. SEEDED / DETERMINISTIC — ``random.Random(MEMEVO_SEED)`` drives corpus
     generation; the fixed ``MEMEVO_BASE_DATE`` epoch + per-round day offsets
     mean the eval NEVER reads a wall-clock (pure — reproducible forever).
  3. NULL-ARM-PAIRED — a defense arm (real ``is_dead_entry`` curation each
     round) vs a discrimination-stripped NULL arm (NO curation — the spec §5
     failure floor). Green proves the bench detects a real governance failure
     (the null drifts), not that it measures nothing.
  4. NON-DEGENERACY control (rb-4485) — a null-arm pairing alone is necessary
     but NOT sufficient: a *retire-everything* degenerate would also drive the
     dead-fraction to 0. The third leg is a control set of legitimate entries
     that MUST survive curation (useful, young, inferred-helpful-only) plus a
     positive control (a genuine dead entry that MUST be caught). A retire-all
     degenerate fails ``falseRetirements == 0``; an allow-all degenerate fails
     ``missedDead == 0``. Only the real predicate passes both.

Pure: no network, no filesystem, no ``date.today()``, no domain data — only the
framework store schema (status / retirement_date / utilization / created).
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _curation_predicate import is_dead_entry  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# GATE-INTEGRITY — pre-registered constants (spec §2). Changing any threshold
# or the seed is a GOVERNANCE action requiring sign-off, not a test edit. The
# standing regression test (test_memevo_bench.py) pins these; a silent drift
# would make the eval pass vacuously.
# ─────────────────────────────────────────────────────────────────────────────
MEMEVO_SEED = 0x6D_65_6D_6F                 # ascii "memo" — the seeded corpus RNG
MEMEVO_SUITE_VERSION = "memevo-2604.15774-v1"
MEMEVO_ROUNDS = 15                          # consolidation update rounds
MEMEVO_ENTRIES_PER_ROUND = 12               # new entries accreted per round
MEMEVO_DEAD_SHARE = 0.6                     # fraction of new entries that are self-poisoning
MEMEVO_ROUND_DAYS = 10                       # sim days elapsed per round (entries age)
MEMEVO_RETRIEVAL_PER_ROUND = 40             # retrieval volume a dead entry accrues per round
MEMEVO_USEFUL_RETRIEVAL_PER_ROUND = 5       # retrieval a useful entry accrues per round
# Curation predicate params — MATCH bulk-retire-dead-entries.py production defaults.
MEMEVO_MIN_RETRIEVALS = 100
MEMEVO_MIN_AGE_DAYS = 30
MEMEVO_BASE_DATE = date(2026, 1, 1)         # fixed epoch — NO wall-clock read (pure)
# Acceptance thresholds (the gate ladder's rungs):
MEMEVO_DEFENSE_DRIFT_MAX = 0.05             # DEFENSE must hold self-poisoning fraction AT/BELOW this
MEMEVO_NULL_DRIFT_MIN = 0.30                # NULL must EXCEED this (spec §5 failure floor — proves real detection)
# ─────────────────────────────────────────────────────────────────────────────


def _make_entry(eid: str, created: date, kind: str) -> dict:
    """A store entry mirroring the reasoning-bank/guardrail schema fields the
    predicate reads. ``kind`` ("dead"|"useful") is a BENCH-SIDE ground-truth
    label — ``is_dead_entry`` never reads it, so it cannot leak into the verdict.
    """
    return {
        "id": eid,
        "status": "active",
        "created": created.isoformat(),
        "kind": kind,
        "utilization": {
            "retrieval_count": 0,
            "times_helpful": 0,
            "times_cited": 0,
            "times_inferred_helpful": 0,
        },
    }


def _simulate(policy: str, seed: int):
    """Drive an accreting memory store through ``MEMEVO_ROUNDS`` update rounds.

    Both arms are seeded IDENTICALLY, so they accrete the SAME corpus in the
    SAME order — the ONLY difference is the curation ``policy`` ("defense" runs
    ``is_dead_entry`` retirement each round; "null" runs none). That isolation
    is what makes the null-arm pairing a valid control.

    Returns (live_store, retired_entries, final_today).
    """
    rng = random.Random(seed)
    store: list[dict] = []
    retired: list[dict] = []
    next_id = 0

    for r in range(MEMEVO_ROUNDS):
        today = MEMEVO_BASE_DATE + timedelta(days=r * MEMEVO_ROUND_DAYS)

        # 1. Accrete this round's new entries (created "today", age 0).
        for _ in range(MEMEVO_ENTRIES_PER_ROUND):
            kind = "dead" if rng.random() < MEMEVO_DEAD_SHARE else "useful"
            store.append(_make_entry(f"me-{next_id:04d}", today, kind))
            next_id += 1

        # 2. Simulate a round of USAGE on every live entry. Dead entries accrue
        #    retrieval VOLUME but never a helpful/cited/inferred event — that is
        #    the self-poisoning signature. Useful entries accrue an attested
        #    helpful event.
        for e in store:
            u = e["utilization"]
            if e["kind"] == "dead":
                u["retrieval_count"] += MEMEVO_RETRIEVAL_PER_ROUND
            else:
                u["retrieval_count"] += MEMEVO_USEFUL_RETRIEVAL_PER_ROUND
                u["times_helpful"] += 1

        # 3. Apply the arm's curation policy AFTER usage, evaluated at end-of-round
        #    ``today``. "defense" = the REAL active-forgetting predicate.
        if policy == "defense":
            survivors = []
            for e in store:
                if is_dead_entry(e, MEMEVO_MIN_RETRIEVALS, MEMEVO_MIN_AGE_DAYS, today):
                    e["status"] = "retired"
                    e["retirement_date"] = today.isoformat()
                    retired.append(e)
                else:
                    survivors.append(e)
            store = survivors
        # policy == "null": no curation — the store accretes unchecked.

    final_today = MEMEVO_BASE_DATE + timedelta(
        days=(MEMEVO_ROUNDS - 1) * MEMEVO_ROUND_DAYS
    )
    return store, retired, final_today


def _dead_fraction(store: list[dict], today: date) -> float:
    """Self-poisoning fraction: of the LIVE (active) entries, what share are
    currently dead by the predicate. This IS memory misevolution — dead entries
    inflating retrieval and crowding the store."""
    if not store:
        return 0.0
    dead = sum(
        1 for e in store
        if is_dead_entry(e, MEMEVO_MIN_RETRIEVALS, MEMEVO_MIN_AGE_DAYS, today)
    )
    return dead / len(store)


def _false_retirements(retired: list[dict]) -> int:
    """Non-degeneracy accounting (rb-4485): a legitimate entry the defense arm
    retired. By construction of ``is_dead_entry`` this MUST be 0 — a useful
    entry (helpful>0) or a young entry (age<min) can never pass. A retire-all
    degenerate would make this large, failing the gate."""
    fr = 0
    for e in retired:
        if e["kind"] == "useful":
            fr += 1
            continue
        created = date.fromisoformat(e["created"])
        rdate = date.fromisoformat(e["retirement_date"])
        if (rdate - created).days < MEMEVO_MIN_AGE_DAYS:
            fr += 1
    return fr


def run_control_set() -> dict:
    """The rb-4485 non-degeneracy control set: legitimate entries a REAL
    predicate MUST NOT retire, plus a positive control it MUST catch.

    A retire-all degenerate fails ``false_retirements == 0``; an allow-all
    degenerate fails ``missed_dead == 0``. Only the real discriminating
    predicate passes both — this is the third leg that a null-arm pairing alone
    cannot provide.
    """
    today = MEMEVO_BASE_DATE + timedelta(days=400)
    controls = [
        # (label, entry, must_be_dead)
        ("heavily-used-and-helpful", {
            "status": "active",
            "created": (today - timedelta(days=365)).isoformat(),
            "utilization": {"retrieval_count": 1000, "times_helpful": 50,
                            "times_cited": 3, "times_inferred_helpful": 0},
        }, False),
        ("young-but-dead-looking", {
            "status": "active",
            "created": (today - timedelta(days=5)).isoformat(),
            "utilization": {"retrieval_count": 500, "times_helpful": 0,
                            "times_cited": 0, "times_inferred_helpful": 0},
        }, False),
        #  protection: inferred-helpful alone must spare an entry.
        ("inferred-helpful-only", {
            "status": "active",
            "created": (today - timedelta(days=365)).isoformat(),
            "utilization": {"retrieval_count": 500, "times_helpful": 0,
                            "times_cited": 0, "times_inferred_helpful": 8},
        }, False),
        # Positive control — a genuine dead entry MUST be caught (else allow-all).
        ("genuine-dead", {
            "status": "active",
            "created": (today - timedelta(days=365)).isoformat(),
            "utilization": {"retrieval_count": 500, "times_helpful": 0,
                            "times_cited": 0, "times_inferred_helpful": 0},
        }, True),
    ]
    false_retirements = 0
    missed_dead = 0
    detail = {}
    for label, entry, must_be_dead in controls:
        verdict = is_dead_entry(entry, MEMEVO_MIN_RETRIEVALS,
                                MEMEVO_MIN_AGE_DAYS, today)
        detail[label] = verdict
        if must_be_dead and not verdict:
            missed_dead += 1
        if (not must_be_dead) and verdict:
            false_retirements += 1
    return {
        "false_retirements": false_retirements,
        "missed_dead": missed_dead,
        "total": len(controls),
        "detail": detail,
    }


def run_memevo_bench(seed: int = MEMEVO_SEED) -> dict:
    """Run both arms and return the gate-ladder verdict."""
    defense_store, defense_retired, final_today = _simulate("defense", seed)
    null_store, _null_retired, _null_today = _simulate("null", seed)

    defense_drift = _dead_fraction(defense_store, final_today)
    null_drift = _dead_fraction(null_store, final_today)
    false_retirements = _false_retirements(defense_retired)
    control = run_control_set()

    passed = (
        defense_drift <= MEMEVO_DEFENSE_DRIFT_MAX
        and null_drift >= MEMEVO_NULL_DRIFT_MIN
        and false_retirements == 0
        and control["false_retirements"] == 0
        and control["missed_dead"] == 0
    )

    return {
        "seed": seed,
        "suiteVersion": MEMEVO_SUITE_VERSION,
        "rounds": MEMEVO_ROUNDS,
        "defense": {
            "driftFraction": round(defense_drift, 4),
            "driftMax": MEMEVO_DEFENSE_DRIFT_MAX,
            "falseRetirements": false_retirements,
            "storeSize": len(defense_store),
            "retiredCount": len(defense_retired),
        },
        "null": {
            "driftFraction": round(null_drift, 4),
            "driftMin": MEMEVO_NULL_DRIFT_MIN,
            "storeSize": len(null_store),
        },
        "nonDegeneracy": control,
        "passed": passed,
    }


if __name__ == "__main__":
    print(json.dumps(run_memevo_bench(), indent=2))
