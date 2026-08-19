"""Consumer-side pins for the utilization reader seam ().

test_utilization_store.py pins the SEAM. This file pins the CONSUMERS that were
converted to use it — a different failure, and one that is invisible without
these tests.

WHY THESE CANNOT BE DEFERRED UNTIL THE WRITER LANDS. Right now no sidecar exists
on any box, so `utilization_of` falls through to the embedded field and a
converted consumer is byte-identical to an unconverted one. That is the whole
point of the reader-before-writer ordering — and it means a call site silently
reverted to `rec.get("utilization")` would pass every existing test, ship, and
only misbehave on the day the writer flips, when the embedded field becomes a
frozen pre-split snapshot. Every test below therefore SUPPLIES a sidecar that
DISAGREES with the embedded field, which is the only way to observe which one a
consumer actually read.

The disagreement is deliberately in the direction that costs the most:

  * `is_dead_entry` is the retirement decision, and retirement is a WRITE. An
    entry whose counters have moved but whose embedded snapshot still reads
    never-helpful would be retired while live. That is knowledge loss, not a
    misreported number, which is why it gets both directions tested.
  * `_never_helpful` and the scar-tissue slate feed the same judgment one step
    earlier.
  * `_sort_by_utility` decides what retrieval surfaces at all — a stale read
    silently reorders every supplementary result.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _curation_predicate import is_dead_entry  # noqa: E402
from _utilization_store import load_all_counters, utilization_of  # noqa: E402

TODAY = date(2026, 8, 17)
OLD = "2025-01-01"  # comfortably past any min_age_days used below


def _rec(rec_id, **util):
    """An active, aged record whose EMBEDDED counters are `util`."""
    return {"id": rec_id, "status": "active", "created": OLD,
            "utilization": dict(util)}


# ---------------------------------------------------------------- predicate --
# The highest-consequence join in the split: this decides what gets retired.

def test_sidecar_helpful_saves_an_entry_the_embedded_field_would_retire():
    """The data-loss direction. Embedded says never-helpful (retire); the live
    sidecar says it HAS been helpful, so it must be kept."""
    rec = _rec("rb-1", retrieval_count=500, times_helpful=0,
               times_cited=0, times_inferred_helpful=0)
    assert is_dead_entry(rec, 100, 30, TODAY) is True, (
        "precondition: with no counters this entry retires on its embedded field")

    counters = {"rb-1": {"retrieval_count": 500, "times_helpful": 3,
                         "times_cited": 0, "times_inferred_helpful": 0}}
    assert is_dead_entry(rec, 100, 30, TODAY, counters) is False, (
        "a live sidecar attesting helpfulness must override the frozen embedded "
        "snapshot — retiring here destroys an entry that is demonstrably in use")


def test_sidecar_zero_retires_an_entry_the_embedded_field_would_keep():
    """The opposite direction, so the test cannot pass by ignoring `counters`
    and always keeping. Embedded claims helpful; the sidecar is the truth."""
    rec = _rec("rb-2", retrieval_count=500, times_helpful=9,
               times_cited=0, times_inferred_helpful=0)
    assert is_dead_entry(rec, 100, 30, TODAY) is False

    counters = {"rb-2": {"retrieval_count": 500, "times_helpful": 0,
                         "times_cited": 0, "times_inferred_helpful": 0}}
    assert is_dead_entry(rec, 100, 30, TODAY, counters) is True


def test_counters_absent_is_byte_identical_to_pre_seam():
    """The reader-before-writer guarantee: today's behaviour is unchanged."""
    for util, expected in (
        (dict(retrieval_count=500, times_helpful=0, times_cited=0,
              times_inferred_helpful=0), True),
        (dict(retrieval_count=500, times_helpful=1, times_cited=0,
              times_inferred_helpful=0), False),
        (dict(retrieval_count=1, times_helpful=0, times_cited=0,
              times_inferred_helpful=0), False),
    ):
        rec = _rec("rb-3", **util)
        assert is_dead_entry(rec, 100, 30, TODAY) is expected
        # An EMPTY counters map must behave as absent, not as "all zeros" — an
        # empty sidecar is the normal state during cutover for an id nobody has
        # touched yet, and treating it as zeros would mass-retire the corpus.
        assert is_dead_entry(rec, 100, 30, TODAY, {}) is expected


def test_id_missing_from_sidecar_falls_back_to_embedded():
    """A partially-populated sidecar must not blank an id it does not carry."""
    rec = _rec("rb-4", retrieval_count=500, times_helpful=7)
    counters = {"rb-OTHER": {"retrieval_count": 1, "times_helpful": 0}}
    assert is_dead_entry(rec, 100, 30, TODAY, counters) is False


def test_predicate_stays_pure_when_no_counters_are_supplied():
    """`_curation_predicate` documents NO I/O and NO path resolution, and is the
    seam memevo_bench evaluates. The sidecar import is deferred INTO the
    `counters` branch to keep that true; this pins the ordering so a future
    move of that import to module scope fails here rather than silently making
    the governance eval depend on the live filesystem."""
    import subprocess
    scripts = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import _curation_predicate;"
        "print('_paths' in sys.modules or '_utilization_store' in sys.modules)"
        % scripts
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        "importing _curation_predicate must not pull in the path-resolving "
        "seam; got:\n" + out.stdout + out.stderr)


# ------------------------------------------------------------- scar-tissue --

def _scar():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scar-tissue-check.py"
    spec = importlib.util.spec_from_file_location("_scar_tissue_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_never_helpful_reads_the_sidecar():
    scar = _scar()
    rec = _rec("guard-1", times_helpful=0, times_cited=0,
               times_inferred_helpful=0)
    assert scar._never_helpful(rec) is True, "precondition: embedded says never"
    counters = {"guard-1": {"times_helpful": 0, "times_cited": 2,
                            "times_inferred_helpful": 0}}
    assert scar._never_helpful(rec, counters) is False, (
        "a citation recorded in the sidecar is a helpful signal — counting this "
        "entry as never-helpful overstates the retirement population")


def test_subset_pairs_utilisation_reads_the_sidecar():
    """`subset_pairs` reports each member's utilisation so a human can judge
    which of a duplicate pair is stale. Reading the frozen field would show
    both as unused and make the pair unjudgeable."""
    scar = _scar()
    # The reported field is `times_active` (see `_row`), NOT retrieval_count —
    # asserting on the wrong field is how this test first passed vacuously.
    a = {"id": "guard-A", "status": "active", "source": "s", "created": OLD,
         "rule": "abc", "utilization": {"times_active": 0}}
    b = {"id": "guard-B", "status": "active", "source": "s", "created": OLD,
         "rule": "abcdef", "utilization": {"times_active": 0}}
    counters = {"guard-A": {"times_active": 41}}
    out = scar.subset_pairs([a, b], ("source", "created"), "rule", 10, counters)
    pair = out["pairs"][0]
    assert pair["subset_times_active"] == 41, (
        "the sidecar's counters must reach the reported pair; got " + repr(pair))
    assert pair["superset_times_active"] == 0, (
        "an id absent from the sidecar keeps its embedded value")


# ---------------------------------------------------------------- retrieve --

def test_sort_by_utility_reads_the_sidecar():
    """Retrieval ordering is the widest-blast-radius consumer: every
    supplementary result set is ranked here.

    The sidecar arrives as a PARAMETER, matching every other consumer in this
    file (see subset_pairs above). This test previously monkeypatched a
    module-level merged load inside _sort_by_utility — a concurrent
    implementation of the same seam that lost to the parameter form on merge,
    because all three call sites are single-kind and the parameter lets each
    pass its own store's map. The contract asserted is unchanged; only the
    injection point moved off an internal that no longer exists.
    """
    import retrieve as R
    lo = {"id": "rb-lo", "utilization": {"utilization_score": 0.9},
          "created": "2026-01-01"}
    hi = {"id": "rb-hi", "utilization": {"utilization_score": 0.1},
          "created": "2026-01-01"}

    assert [r["id"] for r in R._sort_by_utility([lo, hi])] == ["rb-lo", "rb-hi"]

    # The sidecar inverts the embedded ranking. If _key ever reverts to reading
    # the embedded field, this ordering flips back and the test fails.
    sidecar = {"rb-lo": {"utilization_score": 0.1},
               "rb-hi": {"utilization_score": 0.9}}
    assert [r["id"] for r in R._sort_by_utility([lo, hi], sidecar)] == ["rb-hi", "rb-lo"]


def test_sort_handles_records_of_a_kind_with_no_sidecar():
    """Pattern signatures reach this sort and are not a KIND, so they carry no
    sidecar entry. They must keep their embedded score rather than drop to 0."""
    import retrieve as R
    sig = {"id": "sig-1", "utilization": {"utilization_score": 0.7},
           "created": "2026-01-01"}
    bare = {"id": "rb-x", "created": "2026-01-01"}
    assert [r["id"] for r in R._sort_by_utility([bare, sig])] == ["sig-1", "rb-x"]


# ------------------------------------------------------------- merged load --

def test_load_all_counters_is_empty_while_no_sidecar_exists():
    """Today's state on every box, and the precondition every other consumer
    test relies on."""
    assert load_all_counters() == {}


def test_utilization_of_prefers_sidecar_then_embedded_then_empty():
    rec = {"id": "rb-9", "utilization": {"a": 1}}
    assert utilization_of(rec, {"rb-9": {"a": 2}}) == {"a": 2}
    assert utilization_of(rec, {}) == {"a": 1}
    assert utilization_of({"id": "rb-9"}, {}) == {}


# ----------------------------------------------------- guardrail retirement --
# The destructive engine on the guardrail side, and the counterpart to
# `is_dead_entry` above. Its relevance clock feeds TWO paths that both end in a
# retire — staleness (makes a guard a candidate) and refresh-eligibility
# (removes the cluster's protection) — so a frozen read here does not merely
# misreport, it inverts the engine's own DEFAULT-TO-KEEP invariant on exactly
# the guardrails that are most in use.

TODAY_GR = date(2026, 8, 17)


def _gr():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "guardrail_retire.py"
    spec = importlib.util.spec_from_file_location("_guardrail_retire_seam", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _guard(gid, last_retrieved, times_active=0):
    return {"id": gid, "status": "active", "category": "seam-test",
            "created": "2025-01-01", "rule": "r",
            "utilization": {"last_retrieved": last_retrieved,
                            "times_active": times_active}}


def test_retire_clock_sidecar_saves_a_guard_the_embedded_field_calls_stale():
    """The knowledge-loss direction. Embedded says last retrieved long ago, so
    the guard is a retire candidate; the live sidecar says it was retrieved
    yesterday and must protect it."""
    gr = _gr()
    rec = _guard("guard-s1", "2025-02-01")
    assert gr.staleness_days(rec, TODAY_GR) > 500, (
        "precondition: on its embedded field this guard reads as long-stale")

    counters = {"guard-s1": {"last_retrieved": "2026-08-16", "times_active": 9}}
    assert gr.staleness_days(rec, TODAY_GR, counters) == 1, (
        "a live sidecar retrieval must override the frozen embedded snapshot — "
        "retiring here removes a safety check that is demonstrably in use")


def test_retire_clock_sidecar_stales_a_guard_the_embedded_field_calls_fresh():
    """The opposite direction, so the test cannot pass by ignoring `counters`
    and always reporting fresh."""
    gr = _gr()
    rec = _guard("guard-s2", "2026-08-16")
    assert gr.staleness_days(rec, TODAY_GR) == 1
    counters = {"guard-s2": {"last_retrieved": "2025-02-01"}}
    assert gr.staleness_days(rec, TODAY_GR, counters) > 500


def test_retire_clock_counters_absent_is_byte_identical():
    gr = _gr()
    rec = _guard("guard-s3", "2026-08-10")
    assert gr.staleness_days(rec, TODAY_GR) == 7
    # An EMPTY map must behave as absent, not as "no relevance signal" — during
    # cutover most ids carry no sidecar row yet, and reading {} as "never
    # retrieved" would sweep the whole corpus into retirement candidacy.
    assert gr.staleness_days(rec, TODAY_GR, {}) == 7
    assert gr.effective_relevance(rec, {}) == gr.effective_relevance(rec)


def test_retire_scan_threads_counters_to_refresh_eligibility(tmp_path):
    """`refresh_eligible` is the cluster's protection from retirement. It is a
    SEPARATE read path from staleness, so converting only the staleness half
    would leave live clusters unprotected while every staleness test passed."""
    gr = _gr()
    records = {"guard-s4": _guard("guard-s4", "2025-01-05"),
               "guard-s5": _guard("guard-s5", "2025-01-06")}
    cfg = dict(gr._DEFAULTS)
    cfg["retire_threshold_days"] = 30
    cfg["refresh_lookback_days"] = 60

    # No sidecar: both members are ancient, so nothing protects the cluster.
    out = gr.scan(today=TODAY_GR, cfg=cfg, records=records,
                  repo_root=tmp_path, counters={})
    cand = {c["id"]: c for c in out["candidates"]}
    assert "guard-s4" in cand, "precondition: embedded reads make this stale"
    assert cand["guard-s4"]["refresh_eligible"] is False

    # A sibling fired recently ACCORDING TO THE SIDECAR -> cluster is protected.
    out2 = gr.scan(today=TODAY_GR, cfg=cfg, records=records, repo_root=tmp_path,
                   counters={"guard-s5": {"last_retrieved": "2026-08-15",
                                          "times_active": 4}})
    cand2 = {c["id"]: c for c in out2["candidates"]}
    assert cand2["guard-s4"]["refresh_eligible"] is True, (
        "a sidecar retrieval on ANY cluster member must protect the cluster")


def test_retire_scan_reports_sidecar_times_active(tmp_path):
    """The scan row a human reads when judging a retire verdict."""
    gr = _gr()
    records = {"guard-s6": _guard("guard-s6", "2025-01-05", times_active=0)}
    cfg = dict(gr._DEFAULTS)
    cfg["retire_threshold_days"] = 30
    out = gr.scan(today=TODAY_GR, cfg=cfg, records=records, repo_root=tmp_path,
                  counters={"guard-s6": {"last_retrieved": "2025-01-05",
                                         "times_active": 41}})
    row = next(c for c in out["candidates"] if c["id"] == "guard-s6")
    assert row["times_active"] == 41, (
        "reporting the frozen 0 would show an actively-firing guard as unused, "
        "which is the number a reviewer leans on to approve a retire")


# -------------------------------------------------------- weakness signals --
# Every signal here is a DELTA, which is what makes a frozen read dangerous in a
# way a wrong number is not: `now` and `base` collapse to the same value, every
# delta becomes 0, and the lane goes SILENT rather than wrong. An empty signal
# set is indistinguishable from "nothing is firing".

def _ws():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "weakness-signals.py"
    spec = importlib.util.spec_from_file_location("_weakness_signals_seam", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_weakness_times_active_reads_the_sidecar():
    ws = _ws()
    rec = {"id": "guard-w1", "status": "active",
           "utilization": {"times_active": 3}}
    assert ws._times_active(rec) == 3
    assert ws._times_active(rec, {"guard-w1": {"times_active": 47}}) == 47
    # Absent / empty behaves as pre-seam, and an id the sidecar lacks keeps its
    # embedded value rather than blanking to 0.
    assert ws._times_active(rec, {}) == 3
    assert ws._times_active(rec, {"guard-OTHER": {"times_active": 99}}) == 3


def test_weakness_signal_goes_silent_on_a_frozen_read():
    """The failure this conversion prevents, stated as a test: with the sidecar
    carrying the live count, a guard that has fired 40 times since baseline is a
    signal; reading the frozen embedded count yields delta 0 and NO signal at
    all — a silent lane, not a wrong number."""
    ws = _ws()
    guards = [{"id": "guard-w2", "status": "active", "rule": "r",
               "utilization": {"times_active": 5}}]   # frozen pre-split snapshot
    baseline = {"guard-w2": 5}

    sigs, _ = ws.compute_guardrail_signals(guards, baseline, 1, 2.0, 10)
    assert sigs == [], "precondition: on the frozen field there is no movement"

    sigs2, _ = ws.compute_guardrail_signals(
        guards, baseline, 1, 2.0, 10, {"guard-w2": {"times_active": 45}})
    assert [s["id"] for s in sigs2] == ["guard-w2"]
    assert sigs2[0]["delta"] == 40
    assert sigs2[0]["times_active"] == 45, (
        "the reported count must be the live one a reader will act on")


def test_weakness_baseline_and_current_read_the_same_source():
    """The coupling that breaks quietly if only ONE of the pair is converted.
    `current_guard_map` is written back as the NEXT baseline while
    compute_guardrail_signals measures against the PREVIOUS one, so a sidecar
    `now` compared against an embedded `base` would report the gap between two
    different FIELDS as if it were movement over time."""
    ws = _ws()
    counters = {"guard-w3": {"times_active": 60}}
    rec = {"id": "guard-w3", "status": "active", "rule": "r",
           "utilization": {"times_active": 10}}

    # What the next baseline would record, via the same helper the delta uses.
    baseline_next = ws._times_active(rec, counters)
    assert baseline_next == 60

    # Feeding that baseline straight back in must yield NO movement. If the two
    # sites ever diverge, this returns a phantom 50-delta signal.
    sigs, _ = ws.compute_guardrail_signals(
        [rec], {"guard-w3": baseline_next}, 1, 2.0, 10, counters)
    assert sigs == [], (
        "baseline and current must draw from the same field; a phantom delta "
        "here means the pair diverged")

    # ...and the WIRING half, which the assertion above does NOT cover. That is
    # measured, not assumed: reverting `current_guard_map` in main() to the
    # embedded read left every test above green, because this test supplies its
    # own baseline via _times_active and never exercises main()'s construction
    # of it. Without this assertion the pair could diverge in exactly the way
    # the docstring warns about while the suite stayed green.
    import inspect
    src = inspect.getsource(ws.main)
    assert '_times_active(g, _counters)' in src, (
        "main()'s current_guard_map — which is written back as the NEXT "
        "baseline — must read through the same helper compute_guardrail_signals "
        "uses, or baseline and current drift onto different fields")
    assert '(g.get("utilization") or {}).get("times_active")' not in src, (
        "an embedded read survives in main(); baseline and current would then "
        "compare two different fields as if it were movement over time")


# ----------------------------------------------------- build-agent-context --

def test_build_agent_context_fallback_sorts_route_through_the_seam():
    """SOURCE-SHAPE pin, and deliberately labelled as one — it asserts what the
    code READS, not what it computes.

    `build_context` is a large function that loads the real tree / rb / guardrail
    stores, so a behavioural pin would need a full fixture world for a path that
    only fires when NOTHING matched the requested categories. That is out of
    proportion to a fallback. This pin catches the regression that matters (a
    call site reverted to the embedded field) and is honest that it would not
    catch a semantic change which still routes through the seam."""
    import inspect
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "build-agent-context.py"
    spec = importlib.util.spec_from_file_location("_bac_seam", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "_utilization_of") and hasattr(mod, "_load_all_counters")
    src = inspect.getsource(mod.build_context)
    assert src.count("_utilization_of(") == 2, (
        "both fallback sorts (guardrails and reasoning bank) must join the "
        "sidecar; got " + str(src.count("_utilization_of(")))
    assert '.get("utilization", {}).get("retrieval_count"' not in src, (
        "a fallback sort reverted to the embedded field would silently rank on "
        "a frozen pre-split snapshot")


def test_guardrail_retire_import_stays_pure():
    """The engine depends on stdlib + yaml alone at import time; the sidecar
    import is deferred into the `counters` branch to keep that true. Pins the
    ordering so a future move to module scope fails here."""
    import subprocess
    scripts = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import guardrail_retire;"
        "print('_paths' in sys.modules or '_utilization_store' in sys.modules)"
        % scripts
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", (
        "importing guardrail_retire must not pull in the path-resolving seam; "
        "got:\n" + out.stdout + out.stderr)
