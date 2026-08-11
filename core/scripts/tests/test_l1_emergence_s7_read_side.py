"""Tests for S7 read-side awareness in l1-emergence-detector.py ().

The S7 low-write branch used to be guarded by `imbalance > 0`, which silently
DROPPED every zero-pick L1 — precisely the population the signal exists to
judge. Removing that guard alone would have been the opposite error: measured
live 2026-08-02, the `performance` L1 had 0 picks and 78.9 retrievals/node,
the HIGHEST density in the tree, so a write-side-only verdict would have
labelled the best-consulted L1 dying.

These pins cover both failure directions plus the blind-instrument rail:

  1. zero picks + retrieval at/above median  -> stable-reference (healthy)
  2. zero picks + retrieval below median     -> stagnating (genuine)
  3. no L1 has retrieval data at all         -> stagnating + read_side
                                                "unavailable" (guard-1974:
                                                absence of evidence must
                                                never render as the healthy
                                                verdict)
  4. the zero-pick L1 APPEARS in findings    -> the dropped-guard regression
  5. hot L1s keep write-side-only semantics  -> no invented read-side numbers

Loaded by file path because the module name carries hyphens.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DETECTOR = Path(__file__).resolve().parents[1] / "l1-emergence-detector.py"
_spec = importlib.util.spec_from_file_location("l1_emergence_detector", _DETECTOR)
led = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(led)


def _stats(buckets):
    """buckets: {l1: (total_nodes, total_retrieval_count)}"""
    return {
        "by_l1": {
            l1: {"total_nodes": n, "total_retrieval_count": r}
            for l1, (n, r) in buckets.items()
        }
    }


def _picks(counts):
    """counts: {l1: n} -> a pick list shaped like meta/l1-pick-log.jsonl rows."""
    out = []
    for l1, n in counts.items():
        out.extend({"l1": l1} for _ in range(n))
    return out


def _by_l1(verdict):
    return {f["l1"]: f for f in verdict["findings"]}


# --- 1. zero picks + high retrieval -> stable-reference ----------------------

def test_zero_pick_l1_with_at_or_above_median_retrieval_is_stable_reference():
    # `quiet` gets 0 picks but is the densest-consulted L1 in the tree.
    stats = _stats({
        "busy": (100, 1000),    # 10.0 / node
        "quiet": (20, 1600),    # 80.0 / node  <- zero picks, highest density
        "mid": (50, 1500),      # 30.0 / node
    })
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    f = _by_l1(verdict)["quiet"]
    assert f["signal"] == "stable-reference"
    assert f["imbalance"] == 0.0
    assert f["read_side"] == "measured"
    assert f["retrieval_per_node"] == 80.0
    assert f["retrieval_per_node"] >= f["median_retrieval_per_node"]
    assert "NOT a retirement candidate" in f["interpretation"]


# --- 2. zero picks + low retrieval -> stagnating -----------------------------

def test_zero_pick_l1_with_below_median_retrieval_is_stagnating():
    # `dead` gets 0 picks AND is barely consulted -> low on both sides.
    stats = _stats({
        "busy": (100, 5000),    # 50.0 / node
        "dead": (40, 4),        # 0.1 / node   <- zero picks, lowest density
        "mid": (50, 1500),      # 30.0 / node
    })
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    f = _by_l1(verdict)["dead"]
    assert f["signal"] == "stagnating"
    assert f["read_side"] == "measured"
    assert f["retrieval_per_node"] < f["median_retrieval_per_node"]
    assert "retirement or merge" in f["interpretation"]


# --- 3. blind read-side instrument -> never the healthy verdict --------------

def test_no_retrieval_data_anywhere_stays_stagnating_and_names_the_blindness():
    """guard-1974: total absence of evidence must not render as healthy.

    With every retrieval count at zero the median is 0.0 and EVERY L1 would
    trivially satisfy `>= median` — collapsing the whole signal into
    stable-reference and permanently hiding a genuinely dead L1. The verdict
    must instead fall back to the write-side-only answer and SAY that it is
    blind.
    """
    stats = _stats({"busy": (100, 0), "quiet": (20, 0), "mid": (50, 0)})
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    f = _by_l1(verdict)["quiet"]
    assert f["signal"] == "stagnating", "blind instrument must not read healthy"
    assert f["read_side"] == "unavailable"
    assert "UNAVAILABLE" in f["interpretation"]


# --- 3b. the PARTIAL-coverage case: a zero median must not launder a verdict --

def test_zero_retrieval_l1_is_never_stable_reference_when_median_collapses():
    """guard-963 partial-coverage corollary — the all-zero branch is the EASY half.

    The original g-115-3214 rail gated on a GLOBAL `any L1 has retrieval
    data` flag while the comparison it protected was PER-L1. One measured
    peer flips that flag True; if a majority of L1s are unconsulted the
    median is 0.0, and `0.0 >= 0.0` then hands every zero-retrieval L1 the
    `stable-reference` (NOT-a-retirement-candidate) verdict off a zero
    basis — with `read_side: "measured"`, itself a false claim about an L1
    nothing was measured for.

    An ALL-zeros fixture cannot catch this: the global rail fires and the
    test passes. The discriminating fixture is the PARTIAL one.
    """
    stats = _stats({
        "busy": (100, 5000),    # 50.0 / node — the one measured L1
        "quiet": (20, 0),       # 0.0 / node  <- zero picks AND zero retrievals
        "mid": (50, 0),         # 0.0 / node  -> median collapses to 0.0
    })
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    f = _by_l1(verdict)["quiet"]
    assert f["median_retrieval_per_node"] == 0.0, "fixture must collapse the median"
    assert f["signal"] == "stagnating", (
        "a zero-retrieval L1 must never reach the healthy verdict, however "
        "low the median basis falls")
    assert f["read_side"] == "unavailable", (
        "read_side must not claim 'measured' for an L1 with no retrievals")


def test_unmeasured_l1_names_whether_peers_have_data():
    """The two blind cases have DIFFERENT remedies, so they must read apart.

    Whole instrument blind -> suspect retrieval logging. This L1 alone blind
    while peers report -> the L1 really is unconsulted. Same verdict
    (stagnating/unavailable), different next action.
    """
    partial = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}),
        _stats({"busy": (100, 5000), "quiet": (20, 0), "mid": (50, 0)}))
    assert "though peers do" in _by_l1(partial)["quiet"]["interpretation"]

    total = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}),
        _stats({"busy": (100, 0), "quiet": (20, 0), "mid": (50, 0)}))
    assert "whole read-side" in _by_l1(total)["quiet"]["interpretation"]


def test_measured_l1_above_a_zero_median_is_still_stable_reference():
    """The gate is on the L1's OWN evidence, not on the median being positive.

    An L1 that IS consulted, sitting above a median that happens to be zero,
    is a legitimate at-or-above-median call. Pinning it stops a future
    over-correction from gating on `median_density > 0` and silently
    demoting genuinely-consulted branches.
    """
    stats = _stats({
        "busy": (100, 5000),
        "quiet": (20, 10),      # 0.5 / node — low, but NOT zero
        "mid": (50, 0),         # 0.0 / node -> median 0.5? no: median of
                                # [50.0, 0.5, 0.0] is 0.5
    })
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    f = _by_l1(verdict)["quiet"]
    assert f["read_side"] == "measured"
    assert f["signal"] == "stable-reference"


# --- 4. the dropped-guard regression ----------------------------------------

def test_zero_pick_l1_appears_in_findings_at_all():
    """The `imbalance > 0` guard dropped zero-pick L1s entirely.

    This pin fails on the pre-fix code regardless of which signal is chosen,
    because the L1 was simply absent from `findings`.
    """
    stats = _stats({"busy": (100, 1000), "quiet": (20, 1600), "mid": (50, 1500)})
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    assert "quiet" in _by_l1(verdict)
    assert _by_l1(verdict)["quiet"]["imbalance"] == 0.0


# --- 5. hot L1s are unchanged and carry no invented read-side numbers -------

def test_hot_signal_is_write_side_only():
    stats = _stats({"small": (10, 100), "big": (990, 9900)})
    verdict = led.detect_s7_reparent_signal(
        _picks({"small": 150, "big": 50}), stats)
    f = _by_l1(verdict)["small"]
    assert f["signal"] == "hot"
    assert "read_side" not in f
    assert "retrieval_per_node" not in f


# --- status/passthrough contract --------------------------------------------

def test_balanced_tree_emits_no_findings():
    stats = _stats({"a": (100, 1000), "b": (100, 1000)})
    verdict = led.detect_s7_reparent_signal(_picks({"a": 100, "b": 100}), stats)
    assert verdict["findings"] == []
    assert verdict["status"] == "balanced"


def test_sparse_pick_log_short_circuits_before_read_side_work():
    stats = _stats({"a": (100, 1000), "b": (20, 1600)})
    verdict = led.detect_s7_reparent_signal(_picks({"a": 3}), stats)
    assert verdict["status"] == "data_sparse"
    assert verdict["findings"] == []


def test_massless_l1_is_skipped_not_divided_by_zero():
    stats = _stats({"a": (100, 1000), "ghost": (0, 0), "b": (100, 1000)})
    verdict = led.detect_s7_reparent_signal(_picks({"a": 100, "b": 100}), stats)
    assert "ghost" not in _by_l1(verdict)


def test_massless_l1_is_excluded_from_the_median_basis():
    """A 0-node L1 is skipped in findings, so it must not sit in the basis.

    Its meaningless 0.0 density would drag the median down and make
    `stable-reference` easier for every real L1 to reach. Here the two real
    L1s sit at 10.0 and 40.0: the correct median is 25.0, so `quiet` at 10.0
    is BELOW it and stagnating. Include the ghost and the median falls to
    10.0, flipping `quiet` to stable-reference on a phantom.
    """
    stats = _stats({
        "busy": (100, 4000),    # 40.0 / node
        "quiet": (20, 200),     # 10.0 / node  <- zero picks
        "ghost": (0, 0),        # meaningless
    })
    verdict = led.detect_s7_reparent_signal(_picks({"busy": 100}), stats)
    f = _by_l1(verdict)["quiet"]
    assert f["median_retrieval_per_node"] == 25.0
    assert f["signal"] == "stagnating"


# --- renderer surfaces the discriminating evidence --------------------------

def test_markdown_renders_read_side_columns_and_the_stable_reference_note():
    stats = _stats({"busy": (100, 1000), "quiet": (20, 1600), "mid": (50, 1500)})
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    md = led.render_markdown({
        "ts": "2026-08-02T00:00:00",
        "window": 200,
        "total_in_log": 200,
        "s4_new_l1_candidates": [],
        "s6_cross_domain_leaks": [],
        "s7_reparent_signals": verdict,
    })
    assert "Retr/node" in md
    assert "stable-reference" in md
    assert "do NOT retire" in md


def test_markdown_names_the_blind_instrument():
    stats = _stats({"busy": (100, 0), "quiet": (20, 0), "mid": (50, 0)})
    verdict = led.detect_s7_reparent_signal(
        _picks({"busy": 100, "mid": 100}), stats)
    md = led.render_markdown({
        "ts": "2026-08-02T00:00:00",
        "window": 200,
        "total_in_log": 200,
        "s4_new_l1_candidates": [],
        "s6_cross_domain_leaks": [],
        "s7_reparent_signals": verdict,
    })
    assert "UNAVAILABLE" in md
    assert "n/a" in md
