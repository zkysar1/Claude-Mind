""": the output-class carrier contract in worker_execute.py.

A WORKER Body produces outputs the reducer may never see. The measured
instance: g-115-5147's finished framework fix sat on cc-07 and was 0% present
on cc-04, because a worker's WM and staged artifacts have carriers and its
framework FILE EDITS have none. The pre-existing lifecycle table described
that limit correctly ("the worker stages its divergent WM") and enforced
nothing about it, because it is indexed by lifecycle STAGE, not by output
CLASS -- so the gap was unrepresentable in the table that would have caught it.

These tests pin the third SSOT table: every canonical output class names a
carrier or an explicit no-carrier row, and a no-carrier row must name the goal
that fixes it.

WHY THE POSITIVE CONTROLS MATTER (rb-7135). Asserting `carrier_gaps() == []`
against the live table passes just as well if the function body were
`return []`. Every detector here is therefore tested in BOTH directions: once
that it reports the real table clean, and once -- against a synthetic table
injected over the module globals -- that it reports a gap it is supposed to
catch. A suite that only ever measures the clean fixture is measuring one axis
and reporting coverage on two.

Daemon-safe (no daemon_integration marker -- pure in-process contract checks,
no daemon, no subprocess, no filesystem writes).

Run:
  python -m pytest core/scripts/tests/test_worker_execute_carriers.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


we = _load("worker_execute", "worker_execute.py")


# --------------------- table shape ---------------------

def test_every_canonical_output_class_has_a_carrier_row():
    for name in we.CANONICAL_OUTPUT_CLASSES:
        assert name in we.OUTPUT_CLASS_CARRIERS, f"{name} has no carrier row"


def test_no_carrier_row_outside_the_canonical_set():
    extra = set(we.OUTPUT_CLASS_CARRIERS) - set(we.CANONICAL_OUTPUT_CLASSES)
    assert not extra, f"carrier rows not in CANONICAL_OUTPUT_CLASSES: {sorted(extra)}"


def test_every_carrier_kind_is_a_declared_kind():
    for name, row in we.OUTPUT_CLASS_CARRIERS.items():
        assert row.kind in we.CARRIER_KINDS, f"{name} has unknown kind {row.kind!r}"


def test_the_live_table_is_complete():
    assert we.carrier_gaps() == []


# --------------------- the measured defect ---------------------

def test_framework_file_edit_is_now_carried_by_the_worker_ref():
    """The  defect, CLOSED by . This test used to assert the
    gap; it now asserts the carrier, because the gap is gone. Kept as the same
    test rather than deleted so the file records that this class was once
    stranded -- a silently removed test is how a closed gap stops being
    verifiable that it stayed closed."""
    row = we.OUTPUT_CLASS_CARRIERS["framework-file-edit"]
    assert row.kind == we.GIT_REF
    # The target must name BOTH halves. A carrier declared with only a push and
    # no consumer is the original defect one layer out: the artifact becomes
    # durable and remote and still reaches nobody.
    assert "refs/workers/" in row.target
    assert "--push-worker-ref" in row.target, "target must name the producer"
    assert "worker-ref-consume" in row.target, "target must name the CONSUMER"
    # pending_goal is forbidden on a carried row -- it means "someone owes a fix".
    assert row.pending_goal is None


def test_local_git_commit_shares_the_ref_but_keeps_its_own_row():
    """The two classes share one mechanism and still fail differently: the ref
    carries HEAD, so an edit left UNCOMMITTED is not carried at all. Merging the
    rows would hide that residual failure mode."""
    row = we.OUTPUT_CLASS_CARRIERS["local-git-commit"]
    assert row.kind == we.GIT_REF
    assert row.pending_goal is None
    assert "UNCOMMITTED" in row.why, "the row must state what the carrier still misses"

def test_no_output_class_is_stranded_in_the_live_table():
    """The state  was filed to reach. Asserted on the LIVE table so a
    future row added with kind=no-carrier and no carrier work fails here."""
    assert we.unreachable_output_classes() == []
    assert all(r.kind != we.NO_CARRIER for r in we.OUTPUT_CLASS_CARRIERS.values())

def test_classes_that_do_have_carriers_are_not_reported_unreachable():
    """Direction control: a detector that returns everything is as useless as
    one that returns nothing."""
    joined = " | ".join(we.unreachable_output_classes())
    for name in ("working-memory", "spark-observation", "goal-record",
                 "shared-store-file"):
        assert name not in joined, f"{name} has a carrier and must not be listed"


# --------------------- construction-time validation ---------------------

@pytest.mark.parametrize("kwargs,frag", [
    (dict(kind="telepathy", target="x", why="w"),                     "unknown carrier kind"),
    (dict(kind="wm-slot", target="   ", why="w"),                     "target must be non-empty"),
    (dict(kind="shared-store", target="x", why=""),                   "why must be non-empty"),
    (dict(kind="no-carrier", target="x", why="w"),                    "pending_goal is REQUIRED"),
    (dict(kind="wm-slot", target="x", why="w", pending_goal="g-1-11"), "only for no-carrier"),
    (dict(kind="no-carrier", target="x", why="w", pending_goal="nope"), "must be a goal id"),
])
def test_malformed_rows_are_refused(kwargs, frag):
    with pytest.raises(ValueError) as exc:
        we.CarrierDisposition(**kwargs)
    assert frag in str(exc.value)


@pytest.mark.parametrize("junk", [12345, ["g-306-263"], {"id": "g-306-263"}, 3.5, True])
def test_non_string_pending_goal_raises_ValueError_not_AttributeError(junk):
    """Ordering regression pin.

    The requiredness check uses `(pending_goal or "").strip()`, which raises
    AttributeError on a non-string. The class docstring promises ValueError, so
    with the shape check ordered AFTER the requiredness check a caller catching
    the documented contract would not catch an int. Moving the shape check
    first is what this test pins -- `pytest.raises(ValueError)` fails on
    AttributeError, so reverting the order reds this test specifically.

    Same defect class fresh-eyes found on the sibling LifecycleDisposition
    (F-001); it was reproduced one table over before this pin existed.
    """
    with pytest.raises(ValueError):
        we.CarrierDisposition(kind="no-carrier", target="x", why="w", pending_goal=junk)


@pytest.mark.parametrize("junk", [["no-carrier"], {"k": 1}, {"no-carrier"}, 7, None])
def test_non_string_kind_raises_ValueError_not_TypeError(junk):
    """`kind not in CARRIER_KINDS` is a hash lookup, so an unhashable kind
    raises TypeError -- not the ValueError this class documents. Found by
    fresh-eyes on this goal's own diff, one field over from the pending_goal
    ordering bug (guard-3075): same defect class, same method, twice."""
    with pytest.raises(ValueError):
        we.CarrierDisposition(kind=junk, target="x", why="w")


@pytest.mark.parametrize("junk", ["g-306-264\n", "g-306-264\n\n"])
def test_goal_id_regex_rejects_a_trailing_newline(junk):
    """guard-1283: Python's `$` also matches immediately BEFORE a trailing
    newline, so a `$`-anchored id regex accepted "g-306-264\\n". The table's
    pending_goal is the field most likely to receive a value from a captured
    command substitution, which is exactly where a stray newline comes from."""
    assert not we._GOAL_ID_RE.match(junk)
    with pytest.raises(ValueError):
        we.CarrierDisposition(kind=we.NO_CARRIER, target="x", why="w", pending_goal=junk)


def test_both_legal_shapes_construct():
    """Positive control: validation that refuses everything is not validation."""
    carrier = we.CarrierDisposition(
        kind=we.WM_SLOT, target="some_slot", why="merge_wm carries it")
    assert carrier.pending_goal is None
    stranded = we.CarrierDisposition(
        kind=we.NO_CARRIER, target="somewhere", why="no channel exists",
        pending_goal="g-306-263")
    assert stranded.pending_goal == "g-306-263"


# --------------------- the enforcement half ---------------------

def test_stranded_detection_still_works_when_a_row_IS_stranded(monkeypatch):
    """THE COVERAGE THIS FILE WOULD OTHERWISE HAVE LOST. Every stranded-path
    test above used to run against a live no-carrier row; with the table fully
    carried there is nothing live to exercise them, so flipping the last row
    would have silently deleted all coverage of the gate that REFUSES -- the
    gate could go inert and no test would notice. A synthetic row keeps the
    machinery under test independently of whether the real table happens to
    have a gap today."""
    fake = dict(we.OUTPUT_CLASS_CARRIERS)
    fake["synthetic-stranded"] = we.CarrierDisposition(
        kind=we.NO_CARRIER, target="nowhere", why="fixture", pending_goal="g-000-00")
    monkeypatch.setattr(we, "OUTPUT_CLASS_CARRIERS", fake)

    out = we.stranded_outputs(["working-memory", "synthetic-stranded"])
    assert len(out) == 1
    assert "synthetic-stranded" in out[0]
    assert "g-000-00" in out[0], "must name the goal that FIXES it"
    # Two-way: the carried class must NOT be flagged, or the detector is vacuous.
    assert we.stranded_outputs(["working-memory"]) == []
    assert any("synthetic-stranded" in l for l in we.unreachable_output_classes())

def test_stranded_outputs_passes_a_fully_carried_unit():
    """Direction control -- a gate that always refuses is not a gate. A worker
    producing only carried outputs must close normally."""
    assert we.stranded_outputs(
        ["working-memory", "spark-observation", "goal-record", "shared-store-file"]) == []


def test_stranded_outputs_is_loud_about_an_unknown_class():
    """An unlisted class must NOT read as carried. Returning [] here would
    reproduce the original defect through the very API added to prevent it.

    Asserts the GUIDANCE, not merely the exception type. Mutation testing
    caught this: deleting the deliberate raise leaves the dict lookup in the
    comprehension raising a bare KeyError with the same key in its message, so
    a type-plus-key assertion passes identically against no validation at all.
    The known-classes list and the remedy sentence are what distinguish an
    error that tells the caller what to do from one that just fell over.
    """
    with pytest.raises(KeyError) as exc:
        we.stranded_outputs(["working-memory", "telepathic-broadcast"])
    msg = str(exc.value)
    assert "telepathic-broadcast" in msg
    assert "known:" in msg, "must list the valid classes, not just fail"
    assert "not thereby carried" in msg, "must state the remedy"


def test_stranded_outputs_empty_input_is_vacuously_clean():
    assert we.stranded_outputs([]) == []


def test_stranded_outputs_deduplicates(monkeypatch):
    fake = dict(we.OUTPUT_CLASS_CARRIERS)
    fake["synthetic-stranded"] = we.CarrierDisposition(
        kind=we.NO_CARRIER, target="nowhere", why="fixture", pending_goal="g-000-00")
    monkeypatch.setattr(we, "OUTPUT_CLASS_CARRIERS", fake)
    out = we.stranded_outputs(["synthetic-stranded", "synthetic-stranded"])
    assert len(out) == 1

def _cli(*args):
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "worker_execute.py"), *args],
        capture_output=True, text=True)


def test_cli_exits_0_when_every_named_output_is_carried():
    r = _cli("check-outputs", "working-memory", "goal-record")
    assert r.returncode == 0, r.stderr
    assert "carried" in r.stdout


def test_cli_exits_0_on_a_carried_output():
    """rc 0 = every named output reaches the reducer. Both former no-carrier
    classes are asserted here because they are the ones this goal flipped.

    SCOPE, STATED (guard-1462): the CLI's rc=1 branch is NOT exercised here.
    It fires only when the LIVE table has a no-carrier row, and the table now
    has none; a subprocess cannot see the monkeypatched fixture the function
    tests use. So rc=1 is covered at the FUNCTION layer only
    (test_stranded_detection_still_works_when_a_row_IS_stranded), and a
    dispatch-level break in that one branch would not be caught by this file.
    The honest fix when a real no-carrier row next appears is to re-add a live
    rc=1 case then -- not to add a test-only seam to production code now."""
    r = _cli("check-outputs", "working-memory", "framework-file-edit", "local-git-commit")
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_still_exits_2_on_an_unknown_output_class():
    """rc 2 must stay distinct from rc 0. An unlisted class is exactly how the
    original defect hid, so 'unknown' must never be reassuring."""
    r = _cli("check-outputs", "no-such-output-class")
    assert r.returncode == 2, r.stdout + r.stderr

def test_cli_exits_2_on_an_unknown_output_class():
    """Distinct from rc 1 on purpose: 'you named something I do not know' is a
    different instruction to the caller than 'this one is stranded'."""
    r = _cli("check-outputs", "telepathic-broadcast")
    assert r.returncode == 2
    assert "unknown output class" in r.stderr


def test_cli_carrier_gaps_and_unreachable_agree_with_the_functions():
    """The CLI must not drift from the API it wraps."""
    assert _cli("carrier-gaps").returncode == 0            # live table complete
    assert _cli("unreachable").returncode == 0             # nothing stranded now
    assert _cli("lifecycle-gaps").returncode == 0          # sibling contract intact
    assert _cli("phases").stdout.split() == list(we.WORKER_PHASES)

def test_carrier_gaps_detects_a_missing_row(monkeypatch):
    """Inject a table that is missing a canonical class and assert the gap is
    REPORTED. Without this, `carrier_gaps()` could be `return []` and every
    other test in this file would still pass."""
    trimmed = dict(we.OUTPUT_CLASS_CARRIERS)
    trimmed.pop("working-memory")
    monkeypatch.setattr(we, "OUTPUT_CLASS_CARRIERS", trimmed)
    gaps = we.carrier_gaps()
    assert any("working-memory" in g and "NO carrier row" in g for g in gaps), gaps


def test_carrier_gaps_detects_an_undeclared_row(monkeypatch):
    """The other direction: a row for a class nobody declared canonical, which
    is what a rename leaves behind."""
    extended = dict(we.OUTPUT_CLASS_CARRIERS)
    extended["telepathic-broadcast"] = we.CarrierDisposition(
        kind=we.WM_SLOT, target="nowhere", why="synthetic fixture")
    monkeypatch.setattr(we, "OUTPUT_CLASS_CARRIERS", extended)
    gaps = we.carrier_gaps()
    assert any("telepathic-broadcast" in g and "not in CANONICAL" in g for g in gaps), gaps


def test_import_time_assertion_refuses_an_incomplete_contract(monkeypatch):
    """The gap must FAIL, not merely be reportable. Pins that
    _assert_carrier_contract actually raises on the gaps carrier_gaps finds."""
    trimmed = dict(we.OUTPUT_CLASS_CARRIERS)
    trimmed.pop("goal-record")
    monkeypatch.setattr(we, "OUTPUT_CLASS_CARRIERS", trimmed)
    with pytest.raises(ValueError) as exc:
        we._assert_carrier_contract()
    assert "goal-record" in str(exc.value)
    assert "g-306-263" in str(exc.value)


def test_import_time_assertion_passes_on_the_real_table():
    we._assert_carrier_contract()      # must not raise


# --------------------- the existing contracts still hold ---------------------

def test_carrier_table_did_not_disturb_the_phase_or_lifecycle_contracts():
    """Three SSOT tables now live in this module. A change to one must not
    quietly break another -- cheap to assert, and the reason to assert it here
    is that all three are edited in the same file."""
    assert we.WORKER_PHASES == ("select", "claim", "execute")
    assert we.lifecycle_gaps() == []
