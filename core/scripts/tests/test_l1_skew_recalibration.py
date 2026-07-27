#!/usr/bin/env python3
"""Pin the  l1-skew-check recalibration.

The pre-2455 flag was max/min ratio >= threshold — unsatisfiable on a tree
with a tiny-but-healthy L1 (measured 53.6x with the best available taxonomy
action still leaving 29.5x). The recalibrated flag fires only on defects a
real taxonomy action could fix: dominance (share >= ceiling), share_creep
(dominant L1 grew >= N pp since last cadence fire), empty_l1 (zero-node L1).

Pure unit tests over compute_skew — no subprocess, no daemon, no tree read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from importlib.machinery import SourceFileLoader

_MOD = SourceFileLoader(
    "l1_skew_check",
    str(Path(__file__).resolve().parents[1] / "l1-skew-check.py"),
).load_module()
compute_skew = _MOD.compute_skew


def _bucket(nodes, leaves=None, retrievals=0, exploit=0, master=0):
    return {
        "total_nodes": nodes,
        "leaf_count": leaves if leaves is not None else max(nodes - 1, 0),
        "total_retrieval_count": retrievals,
        "capability_mass": {"EXPLOIT": exploit, "MASTER": master},
    }


def _by_metric(findings):
    return {f["metric"]: f for f in findings}


# The live 2026-07-17 shape: intelligence 1020/1204 (84.7% share, ratio 53.7x
# vs performance=19). Fleet-adjudicated healthy (maturity gradient, split
# assessment already before the user). MUST NOT flag — this is the goal's
# "alarm-noise stream stops on the healthy-but-skewed tree" outcome.
LIVE_SHAPE = {
    "execution": _bucket(48, 31, 2005, exploit=42),
    "intelligence": _bucket(1020, 687, 22906, exploit=600, master=35),
    "performance": _bucket(19, 14, 655, exploit=1),
    "system": _bucket(116, 91, 3754, exploit=84),
}


def test_healthy_but_skewed_tree_not_flagged():
    findings = compute_skew(LIVE_SHAPE, threshold=5.0)
    assert findings, "expected findings for 4 metrics"
    flagged = [f for f in findings if f["flagged"]]
    assert not flagged, (
        "healthy-but-skewed tree must not flag (was the 5x/24h alarm noise): "
        + repr([(f["metric"], f["flag_reason"]) for f in flagged]))
    # Ratio evidence still rides along (back-compat payload).
    bm = _by_metric(findings)
    assert bm["total_nodes"]["ratio"] > 50
    assert bm["total_nodes"]["flag_reason"] is None
    assert abs(bm["total_nodes"]["share"] - 1020 / 1203) < 0.01


def test_dominance_flags_hoover_bucket():
    shape = {
        "execution": _bucket(40),
        "intelligence": _bucket(1300),  # 1300/1359 = 95.7%
        "system": _bucket(19),
    }
    bm = _by_metric(compute_skew(shape, threshold=5.0))
    f = bm["total_nodes"]
    assert f["flagged"] and f["flag_reason"] == "dominance"
    assert f["max_l1"] == "intelligence"


def test_dominance_satisfiable_by_split():
    # The natural action (npc-intelligence -> L1, 560 nodes out of a 1090-node
    # hoover bucket) drops max share below the ceiling: the alarm CLEARS.
    # This is the "a real taxonomy action could satisfy" outcome — the old
    # ratio metric stayed flagged (530/19 = 27.9x) after the same split.
    before = {
        "intelligence": _bucket(1090),  # 1090/1204 = 90.5% -> flags
        "execution": _bucket(48),
        "performance": _bucket(19),
        "system": _bucket(47),
    }
    after = {
        "intelligence": _bucket(530),
        "npc-intelligence": _bucket(560),
        "execution": _bucket(48),
        "performance": _bucket(19),
        "system": _bucket(47),
    }
    assert _by_metric(compute_skew(before, 5.0))["total_nodes"]["flagged"]
    f_after = _by_metric(compute_skew(after, 5.0))["total_nodes"]
    assert not f_after["flagged"], "split must clear the dominance flag"
    assert f_after["ratio"] > 5.0, (
        "sanity: the OLD ratio metric would still have flagged post-split")


def test_share_creep_flags_degradation():
    prev = {"total_nodes": 0.82}
    shape = {
        "intelligence": _bucket(860),  # 860/1000 = 86% -> +4pp vs 82%
        "execution": _bucket(60),
        "performance": _bucket(20),
        "system": _bucket(60),
    }
    f = _by_metric(compute_skew(shape, 5.0, prev_shares=prev))["total_nodes"]
    assert f["flagged"] and f["flag_reason"] == "share_creep"


def test_share_creep_needs_baseline():
    # Same degraded shape, no prior cadence-fire baseline -> no creep flag
    # (and 86% < 90% ceiling -> no dominance either).
    shape = {
        "intelligence": _bucket(860),
        "execution": _bucket(60),
        "performance": _bucket(20),
        "system": _bucket(60),
    }
    f = _by_metric(compute_skew(shape, 5.0, prev_shares=None))["total_nodes"]
    assert not f["flagged"]


def test_share_creep_ignores_minor_jitter_and_small_l1s():
    # +2pp is under the 3pp default: not flagged.
    prev = {"total_nodes": 0.83}
    shape = {
        "intelligence": _bucket(850),  # 85% -> +2pp
        "execution": _bucket(60),
        "performance": _bucket(30),
        "system": _bucket(60),
    }
    f = _by_metric(compute_skew(shape, 5.0, prev_shares=prev))["total_nodes"]
    assert not f["flagged"]
    # Rebalancing among small L1s (max share < 0.5) never creep-flags even
    # on a large pp jump.
    prev2 = {"total_nodes": 0.30}
    shape2 = {
        "a": _bucket(45),  # 45/100 = 45% -> +15pp but below majority floor
        "b": _bucket(30),
        "c": _bucket(25),
    }
    f2 = _by_metric(compute_skew(shape2, 5.0, prev_shares=prev2))["total_nodes"]
    assert not f2["flagged"]


def test_empty_l1_flags_structural_only():
    # Young tree: 6 matured nodes total, all in one L1 — legitimately
    # concentrated, must not flag (metric total below the 10-mass floor).
    shape = {
        "intelligence": _bucket(200, exploit=6),
        "execution": _bucket(0, leaves=0),   # dead L1 -> empty_l1
        "system": _bucket(100, exploit=0),   # young L1: 0 matured nodes
    }
    bm = _by_metric(compute_skew(shape, 5.0))
    assert bm["total_nodes"]["flag_reason"] == "empty_l1"
    # Derived metrics must NOT flag on a zero min (the old infinite-ratio
    # noise: a young L1 legitimately has 0 matured nodes for weeks).
    assert not bm["mature_capability_mass"]["flagged"]
    assert bm["mature_capability_mass"]["ratio_infinite"]


def test_dominance_mass_floor():
    # 100% concentration over a sub-10 total is quantization noise: no flag.
    young = {
        "a": _bucket(120, exploit=6),
        "b": _bucket(90, exploit=0),
    }
    assert not _by_metric(compute_skew(young, 5.0))[
        "mature_capability_mass"]["flagged"]
    # The same 100% concentration above the floor IS extreme and flags.
    grown = {
        "a": _bucket(120, exploit=50),
        "b": _bucket(90, exploit=0),
    }
    f = _by_metric(compute_skew(grown, 5.0))["mature_capability_mass"]
    assert f["flagged"] and f["flag_reason"] == "dominance"


def test_prev_shares_junk_types_never_raise():
    # Fresh-eyes F1 (2026-07-17): non-dict prev_shares reaching the API
    # (`metric in 42` -> TypeError; a string could substring-match then
    # raise on subscript). main() guards the CLI path; the API must too.
    shape = {
        "intelligence": _bucket(860),
        "execution": _bucket(140),
    }
    for junk in (42, "total_nodes-lookalike", [0.8], None,
                 {"total_nodes": "not-a-number"}, {"total_nodes": None}):
        findings = compute_skew(shape, 5.0, prev_shares=junk)
        assert not any(f["flag_reason"] == "share_creep" for f in findings), (
            "junk prev_shares %r must not creep-flag" % (junk,))


def test_creep_zero_pp_requires_positive_delta():
    # Fresh-eyes F2 (2026-07-17): with creep_pp=0 a bare `>=` flagged an
    # UNCHANGED share as "creep". Growth requires a positive delta.
    shape = {
        "intelligence": _bucket(860),  # 86% share
        "execution": _bucket(140),
    }
    prev_equal = {"total_nodes": 860 / 1000}
    f = _by_metric(compute_skew(shape, 5.0, prev_shares=prev_equal,
                                creep_pp=0.0))["total_nodes"]
    assert not f["flagged"], "unchanged share is not creep even at pp=0"
    prev_lower = {"total_nodes": 0.855}
    f2 = _by_metric(compute_skew(shape, 5.0, prev_shares=prev_lower,
                                 creep_pp=0.0))["total_nodes"]
    assert f2["flag_reason"] == "share_creep", (
        "any positive delta flags at pp=0")


def test_single_l1_never_flags():
    shape = {"intelligence": _bucket(500)}
    findings = compute_skew(shape, 5.0)
    assert findings
    assert not any(f["flagged"] for f in findings), (
        "share is trivially 100% with one L1 — skew is meaningless")


def test_orphan_bucket_excluded():
    shape = dict(LIVE_SHAPE)
    shape["_orphan"] = _bucket(2000)  # would dominate if counted
    findings = compute_skew(shape, 5.0)
    assert not any(f["flagged"] for f in findings)
    assert all(f["max_l1"] != "_orphan" for f in findings)


def main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except AssertionError as e:
            failed += 1
            print("FAIL " + name + ": " + str(e))
    print("{}/{} passed".format(len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
