"""_paired_diff — canonical bidirectional set-difference helper for paired-data audits.

Origin: rb-707 (verify-real cluster Candidate 2, bravo session-61 g-275-03).

Failure mode this helper prevents
=================================

Audits comparing two stores expected to be 1-to-1 frequently use net delta
as the "in sync" signal:

    if len(records) - len(files) == 0:
        return "in sync"   # WRONG

This is the rb-707 anti-pattern: net delta of zero (or near-zero) can hide
arbitrarily large bilateral leakage. The canonical incident found 54
actual mismatches behind `net=-2` — 27× mask factor. The system requires
both directions of set difference, separately.

Usage
=====

    from audit_helpers._paired_diff import paired_diff

    md_stems = {p.stem for p in (agent / "experience").glob("*.md")}
    jsonl_ids = {rec["id"] for rec in read_jsonl(agent / "experience.jsonl")}

    result = paired_diff(md_stems, jsonl_ids,
                         a_label="experience.md", b_label="experience.jsonl")
    if not result["in_sync"]:
        print(f"orphan_md (.md without jsonl): {result['a_minus_b_count']}")
        print(f"missing_md (jsonl without .md): {result['b_minus_a_count']}")

Result schema
=============

    {
      "a_minus_b":         [<sorted elements in A not in B>],
      "b_minus_a":         [<sorted elements in B not in A>],
      "a_minus_b_count":   int,
      "b_minus_a_count":   int,
      "total_mismatches":  int,
      "in_sync":           bool,
      "a_label":           str,
      "b_label":           str,
    }

`in_sync` is True ONLY when BOTH counts are zero. Net delta of zero with
nonzero (a_minus_b_count + b_minus_a_count) returns False — the rb-707
trap is exactly this case, and `in_sync` will correctly flag it.

Cross-references
================

- core/config/conventions/paired-data-audits.md (the rule this helper enforces)
- world/reasoning-bank.jsonl rb-707 (origin incident)
- bravo/reports/verify-real-cluster-catalog-2026-05-07.md Candidate 2
- core/scripts/experience-reconcile.py (pre-existing example using this pattern)
"""

from __future__ import annotations

from typing import Iterable, Hashable


def paired_diff(
    set_a: Iterable[Hashable],
    set_b: Iterable[Hashable],
    a_label: str = "A",
    b_label: str = "B",
) -> dict:
    """Compute bidirectional set difference for paired-data audit.

    Args:
        set_a: First store's identifying elements (e.g., file stems).
        set_b: Second store's identifying elements (e.g., jsonl record ids).
        a_label: Human-readable name for set A (used in result keys).
        b_label: Human-readable name for set B (used in result keys).

    Returns:
        Dict with bidirectional difference data. See module docstring for schema.
    """
    a_set = set(set_a)
    b_set = set(set_b)

    a_minus_b = sorted(a_set - b_set)
    b_minus_a = sorted(b_set - a_set)

    a_minus_b_count = len(a_minus_b)
    b_minus_a_count = len(b_minus_a)

    return {
        "a_minus_b": a_minus_b,
        "b_minus_a": b_minus_a,
        "a_minus_b_count": a_minus_b_count,
        "b_minus_a_count": b_minus_a_count,
        "total_mismatches": a_minus_b_count + b_minus_a_count,
        "in_sync": a_minus_b_count == 0 and b_minus_a_count == 0,
        "a_label": a_label,
        "b_label": b_label,
    }


def _self_test() -> int:
    """Smoke tests. Run via: py -3 core/scripts/audit_helpers/_paired_diff.py"""
    failures = []

    # Test 1: Identical sets — in sync
    r = paired_diff({"a", "b", "c"}, {"a", "b", "c"})
    if not r["in_sync"]:
        failures.append(f"T1: identical sets should be in_sync, got {r}")
    if r["total_mismatches"] != 0:
        failures.append(f"T1: identical sets total_mismatches != 0: {r}")

    # Test 2: Bilateral mismatch with net=0 — the rb-707 trap
    r = paired_diff({"a", "b", "c"}, {"a", "x", "y"})
    if r["in_sync"]:
        failures.append(f"T2: bilateral mismatch falsely in_sync: {r}")
    if r["a_minus_b_count"] != 2 or r["b_minus_a_count"] != 2:
        failures.append(f"T2: expected 2/2 mismatches, got {r}")
    if sorted(r["a_minus_b"]) != ["b", "c"]:
        failures.append(f"T2: a_minus_b wrong: {r}")
    if sorted(r["b_minus_a"]) != ["x", "y"]:
        failures.append(f"T2: b_minus_a wrong: {r}")

    # Test 3: A subset of B — one-way mismatch
    r = paired_diff({"a", "b"}, {"a", "b", "c", "d"})
    if r["in_sync"]:
        failures.append(f"T3: A⊂B falsely in_sync: {r}")
    if r["a_minus_b_count"] != 0 or r["b_minus_a_count"] != 2:
        failures.append(f"T3: expected 0/2, got {r}")

    # Test 4: B subset of A — inverse one-way
    r = paired_diff({"a", "b", "c", "d"}, {"a", "b"})
    if r["in_sync"]:
        failures.append(f"T4: B⊂A falsely in_sync: {r}")
    if r["a_minus_b_count"] != 2 or r["b_minus_a_count"] != 0:
        failures.append(f"T4: expected 2/0, got {r}")

    # Test 5: Empty inputs
    r = paired_diff(set(), set())
    if not r["in_sync"]:
        failures.append(f"T5: empty sets should be in_sync, got {r}")

    # Test 6: Labels propagate
    r = paired_diff({"a"}, {"b"}, a_label="experience.md", b_label="experience.jsonl")
    if r["a_label"] != "experience.md" or r["b_label"] != "experience.jsonl":
        failures.append(f"T6: labels did not propagate: {r}")

    # Test 7: Iterables (not just sets) — list input
    r = paired_diff(["a", "a", "b"], ["a", "c"])  # duplicates de-duped
    if r["a_minus_b_count"] != 1 or r["b_minus_a_count"] != 1:
        failures.append(f"T7: list input with dups: {r}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print(f"OK: {7} smoke tests pass")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
