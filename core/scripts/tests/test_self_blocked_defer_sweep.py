"""Tests for core/scripts/self-blocked-defer-sweep.py ().

The sweep classifies free-text `defer_reason` prose into bands. Its whole value
is that an EMPTY or SMALL candidate band means "nothing matched" rather than
"the reader is broken", so the tests here pin the CONTROL machinery at least as
hard as the classifier itself.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "_self_blocked_defer_sweep", SCRIPT_DIR / "self-blocked-defer-sweep.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


def test_control_passes_as_shipped(mod):
    ok, bad = mod.run_control()
    assert ok, f"shipped control is RED: {bad}"


def test_control_has_both_a_positive_and_a_negative_half(mod):
    """guard-3845: a keyword classifier over a work queue needs BOTH.

    This predicate's failure mode is UNDER-exclusion — an exogenous row
    surfacing as a candidate — which a positive-only control cannot see.
    """
    wants = {want for _text, want, _why in mod.CONTROL}
    assert "self_blocked_candidate" in wants, "no positive fixture"
    assert wants - {"self_blocked_candidate"}, "no negative fixture"


def test_negative_half_catches_an_always_candidate_classifier(mod):
    """The mutation the negative half exists to catch."""
    real = mod.classify
    try:
        mod.classify = lambda _text: "self_blocked_candidate"
        ok, bad = mod.run_control()
    finally:
        mod.classify = real
    assert not ok, "an always-candidate classifier passed the control"
    assert len(bad) >= 2, f"only {len(bad)} fixture(s) bit; the negative half is thin"


def test_positive_half_catches_a_dropped_clearer_family(mod):
    """Removing a family must make a real corpus row flip into the candidate band."""
    saved = mod.COMPILED
    try:
        mod.COMPILED = tuple((n, r) for n, r in saved if n != "role")
        ok, bad = mod.run_control()
    finally:
        mod.COMPILED = saved
    assert not ok, "dropping the role family did not regress the control"
    assert any(b["got"] == "self_blocked_candidate" for b in bad)


def test_role_is_its_own_band_not_folded_into_neighbours(mod):
    """A reducer-only block is exogenous to a WORKER and not to the fleet.

    It clears when a differently-roled Body picks the goal up, so it is neither
    a self-blocked candidate nor a world-condition wait. Collapsing it either
    way erases that distinction.
    """
    got = mod.classify(
        "precondition_unmet: hypothesis resolution is reducer-only "
        "(worker-loop Phase 3.65); evidence supplied via hyp_capture")
    assert got == "role"


def test_other_structured_prefixes_are_out_of_scope(mod):
    """Only `precondition_unmet:` re-probes, so only it can freeze on itself.

    `human_blocked:` never auto-clears BY DESIGN and must not be swept in.
    """
    assert mod.classify("human_blocked: owner must approve the deploy") == "out_of_scope"
    assert mod.classify("blocked_on_dependency g-115-1") == "out_of_scope"
    assert mod.classify("") == "out_of_scope"
    assert mod.classify(None) == "out_of_scope"


@pytest.mark.parametrize("text", [
    "precondition_unmet: the livelihood metric is unwritten",   # not \\blive\\b
    "precondition_unmet: rehousing the helper is unfinished",   # not \\bhost\\b
    "precondition_unmet: the approver-agnostic path is unbuilt",  # \\bapprov\\w*\\b DOES match
])
def test_tokens_do_not_match_inside_unrelated_words(mod, text):
    """guard-3845: every token \\b-anchored.

    An unanchored fragment excludes rows silently — a wrongly-EXCLUDED row never
    appears in the output at all, so the failure is invisible by construction.
    The third case is a deliberate TRUE positive: `approv` is a prefix-token by
    design (`\\bapprov\\w*\\b` covers approve/approval/approver), so it is listed
    here to pin that the anchoring is word-START, not whole-word.
    """
    got = mod.classify(text)
    if "approver" in text:
        assert got == "exogenous:human"
    else:
        assert got == "self_blocked_candidate", (
            f"{text!r} was excluded as {got} — a token matched inside a word")


def test_sweep_rejects_an_apply_flag(mod):
    """Read-only by construction: argparse must not know `--apply`.

    Pinned BEHAVIOURALLY, not by grepping the source. A source scan for the
    literal "--apply" matches this script's own docstring, which says it has
    none — a predicate that fires on its own documentation is the guard-2421
    shape and it failed exactly that way when first written.
    """
    with pytest.raises(SystemExit) as exc:
        mod.main(["--apply"])
    assert exc.value.code == 2


def test_sweep_imports_no_mutation_helper(mod):
    """No writer is reachable from the module namespace."""
    for banned in ("locked_rmw", "locked_append_jsonl", "_fileops",
                   "write_text", "atomic_write"):
        assert not hasattr(mod, banned), f"read-only sweep exposes {banned!r}"


def test_a_read_that_scanned_no_goals_is_refused_not_reported_as_clean(
        mod, monkeypatch, capsys):
    """An unreadable queue is NOT a queue with no self-blocked defers."""
    monkeypatch.setattr(mod, "_load_population",
                        lambda: ([], {"sources_read": 0, "goals_scanned": 0}))
    rc = mod.main([])
    assert rc == 2
    assert "REFUSING" in capsys.readouterr().err


# ── empty is not unreadable () ──────────────────────────────────────
#
# The sweep used to refuse any queue whose loader returned no DEFERRED goal, so a
# queue that was read in full and simply holds no deferred goal could never pass,
# on any box. These tests drive the REAL loader over a tmp store (patching `_paths`,
# which audit-deferred-defers.py reads at import) rather than hand the sweep a
# coverage the test wrote: the expectation must come from the OTHER component at
# runtime (guard-1220). The unreadable cases are the control that the guard still
# fires; the candidate case is the control that the sweep still finds work in the
# same harness (guard-4166: a fix whose effect is that something stops appearing
# needs a positive control that does NOT flip under the mutant).

def _point_loader_at(monkeypatch, tmp_path, world_text):
    """A tmp world (no agents) for the real loader. `None` writes no queue file."""
    import _paths  # on sys.path: the `mod` fixture's import of the sweep put it there
    world = tmp_path / "world"
    world.mkdir()
    if world_text is not None:
        (world / "aspirations.jsonl").write_text(world_text, encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setattr(_paths, "agents_root", lambda: agents)


def _queue(*goals):
    return json.dumps({"id": "asp-900", "goals": list(goals)}) + "\n"


def test_a_readable_queue_with_no_deferred_goal_is_a_clean_census(
        mod, monkeypatch, tmp_path, capsys):
    """Pre-fix this returned 2 on any queue without a deferred goal."""
    _point_loader_at(monkeypatch, tmp_path, _queue(
        {"id": "g-1", "title": "a", "status": "pending"},
        {"id": "g-2", "title": "b", "status": "completed"}))
    rc = mod.main(["--json"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    d = json.loads(out.out)
    assert d["population"] == 0
    assert d["coverage"] == {"sources_read": 1, "goals_scanned": 2}
    assert d["self_blocked_candidates"] == []


def test_the_human_report_of_a_clean_census_says_what_it_scanned(
        mod, monkeypatch, tmp_path, capsys):
    _point_loader_at(monkeypatch, tmp_path, _queue({"id": "g-1", "status": "pending"}))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "deferred population (SSOT loader) : 0" in out
    assert "scanned (queue files / goals)     : 1 / 1" in out


@pytest.mark.parametrize("world_text,why", [
    (None, "the world dir holds no aspirations.jsonl"),
    ("", "a zero-byte aspirations.jsonl, the shape of a truncated store"),
    ("not json\n{also not\n", "every line unparseable"),
    (json.dumps({"id": "asp-900", "goals": []}) + "\n",
     "a store with aspirations and no goals: the deliberate conservative boundary, "
     "indistinguishable from a truncation that kept the aspiration shells"),
])
def test_an_unreadable_queue_is_still_refused(
        mod, monkeypatch, tmp_path, capsys, world_text, why):
    _point_loader_at(monkeypatch, tmp_path, world_text)
    rc = mod.main(["--json"])
    out = capsys.readouterr()
    assert rc == 2, why
    assert "REFUSING" in out.err and out.out == "", why


def test_the_sweep_still_finds_a_candidate_in_a_readable_queue(
        mod, monkeypatch, tmp_path, capsys):
    """Specificity control: same harness, one deferred goal naming no external clearer.

    The sweep's own positive CONTROL string is the input, so the expectation is the
    classifier's, not this test's.
    """
    own_fix = mod.CONTROL[0][0]
    assert mod.classify(own_fix) == "self_blocked_candidate"
    _point_loader_at(monkeypatch, tmp_path, _queue(
        {"id": "g-1", "title": "t", "status": "pending", "defer_reason": own_fix}))
    rc = mod.main(["--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert d["population"] == 1
    assert [c["goal_id"] for c in d["self_blocked_candidates"]] == ["g-1"]


def test_control_regression_refuses_to_report(mod, monkeypatch, capsys):
    """A broken classifier must not emit a band count at all."""
    monkeypatch.setattr(mod, "classify", lambda _t: "self_blocked_candidate")
    rc = mod.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "CONTROL REGRESSED" in err


# ── band counts are exclusion counts, not a taxonomy () ─────────────

def test_band_attribution_is_order_dependent(mod):
    """The mechanical fact behind the 'not a taxonomy' warning.

    `classify` returns the FIRST matching family, so a defer naming TWO
    clearers is attributed to whichever is listed earlier in CLEARERS — the
    cascade is tuned to EXCLUDE reliably, not to attribute correctly. Pinned
    BEHAVIOURALLY rather than by grepping the docstring for the warning text:
    a predicate that fires on its own documentation is the guard-2421 shape.
    """
    # NOTE the id shape: `\\bg-\\d{3}-\\d{2,4}\\b` needs >=2 trailing digits, so
    # "" does NOT match other-goal and would make this fixture inert.
    # An earlier draft used it and the test passed for the wrong reason — it
    # was asserting locus against a string only locus ever matched.
    both = "precondition_unmet: cc-04 must run g-115-11 before this can proceed"
    names = [n for n, _ in mod.CLEARERS]
    assert names.index("locus") < names.index("other-goal"), (
        "this test encodes the shipped order; if CLEARERS is reordered the "
        "band counts change meaning and the docstring note needs revisiting")
    assert mod.classify(both) == "exogenous:locus", (
        "a row naming BOTH a box and a goal must land in the earlier family — "
        "that is why a band size is a lower bound on itself and nothing more")

    # and prove it is the ORDER doing the work: swap the two families and the
    # SAME text must change bands. Without this half the assertion above is
    # satisfied by any string locus alone would have matched.
    saved = mod.COMPILED
    try:
        by_name = dict(saved)
        mod.COMPILED = tuple(
            [(n, by_name[n]) for n in ("other-goal", "locus")]
            + [(n, r) for n, r in saved if n not in ("other-goal", "locus")])
        assert mod.classify(both) == "exogenous:other-goal", (
            "reordering CLEARERS did not move the row — the fixture does not "
            "actually match both families")
    finally:
        mod.COMPILED = saved


def test_candidate_floor_is_invariant_under_reattribution(mod):
    """Moving rows BETWEEN exogenous families must not change the floor.

    This is the property that makes the band-attribution softness tolerable:
    the one number this script exists to produce is unaffected by it. Measured
    live when the locus pattern was widened (g-363-60): locus 68->72,
    other-goal 33->29, candidates 18->18.
    """
    rows = [
        # matches BOTH locus and other-goal, so reordering moves it
        "precondition_unmet: cc-04 must run g-115-11 before this can proceed",
        # matches other-goal only
        "precondition_unmet: waiting on g-115-11 to land",
        # matches nothing — the candidate, and the row that must not move
        "precondition_unmet: the byte-range read code is not yet written",
    ]
    before = [mod.classify(r) for r in rows]
    saved = mod.COMPILED
    try:
        # reorder so other-goal is consulted before locus
        by_name = dict(saved)
        mod.COMPILED = tuple(
            [(n, by_name[n]) for n in ("other-goal", "locus")]
            + [(n, r) for n, r in saved if n not in ("other-goal", "locus")])
        after = [mod.classify(r) for r in rows]
    finally:
        mod.COMPILED = saved
    assert before != after, "reordering changed nothing — the fixtures are weak"
    n = lambda bands: sum(1 for b in bands if b == "self_blocked_candidate")
    assert n(before) == n(after) == 1, (
        "the candidate floor moved when only the ATTRIBUTION order changed")


@pytest.mark.parametrize("text,why", [
    ("precondition_unmet: no Linux fleet box can host one",
     "one intervening qualifier — the four-row miss this fix addresses"),
    ("precondition_unmet: needs the linux box", "bare two-word form, no regression"),
    ("precondition_unmet: needs the windows box", "bare two-word form, no regression"),
])
def test_box_locus_tolerates_one_qualifier(mod, text, why):
    assert mod.classify(text) == "exogenous:locus", why


def test_population_rows_carry_what_the_classifier_needs(mod):
    """Guards the SSOT loader's PROJECTION, which is easy to forget it has.

    `load_deferred()` returns a 9-key projection, not full goal records. The
    sweep only needs `defer_reason` (+ `title` for display), so the projection
    is fine HERE — but a consumer asking a DIFFERENT question of this same
    population (does this goal set `blocked_by`?) gets a confident zero from
    every row, because the key is absent rather than falsy. Measured g-363-60:
    a first pass reported '0 of 191 use blocked_by' that was purely an artifact
    of the projection. Ask an axis question by joining to the full store.

    Asserts the MINIMUM the classifier needs, so a future widening of the
    loader still passes.
    """
    pop, _coverage = mod._load_population()
    if not pop:
        pytest.skip("no deferred goals on this box — nothing to characterise")
    assert all("defer_reason" in r for r in pop)
    assert all("goal_id" in r for r in pop)
