""" outcome 4: the SIBLING scan-then-write sweeps must refuse a stale
candidate too, and the shared policy must be exercised through each of them.

WHY THIS FILE EXISTS RATHER THAN MORE CASES IN THE UNBLOCK SUITE. Wiring the
guard into routing-audit-target-status-sweep and parent-supersession-sweep left
all six pre-existing suites green (94 passed) — and that proved nothing. A
mutation that made the shared policy refuse UNCONDITIONALLY also left both
sibling suites green, which means neither suite reaches its own apply path at
all. A guard that is wired but unexercised is indistinguishable from a guard
that is absent (guard-3893: a helper the script ACCEPTS is not one it WIRES).
Every test below was verified RED by mutation before being committed.

THE TWO PROPERTIES WORTH NAMING:

  * `test_*_refuses_and_writes_nothing` are the mutation-killers. They assert on
    the ABSENCE of a write, not on a return value, because the return value is
    the easy half — the damage is the write.
  * `test_*_seam_resolves_its_own_module_stubs` pins the guard-2385 property the
    whole extraction hangs on: the shared reader takes its collaborators as
    arguments precisely so each caller's module attributes stay patchable. If a
    later refactor lets `_sweep_write_guard` import them itself, these two tests
    go red instead of the stubs silently going quiet.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _sweep_write_guard as swg  # noqa: E402

OPEN_GOAL = {"id": "g-1-1", "status": "pending", "outcome_note": "REAL WORK"}


def _load(stem, alias):
    """Hyphenated filenames block a plain import."""
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def routing():
    return _load("routing-audit-target-status-sweep", "routing_audit_swg")


@pytest.fixture()
def supersession():
    return _load("parent-supersession-sweep", "parent_supersession_swg")


def _forbid_writes(mod, who):
    def explode(*a, **k):
        raise AssertionError(f"{who} issued a WRITE after refusing")
    mod._py = explode


def _assert_seam_is_patchable(mod, monkeypatch):
    """guard-2385: BOTH collaborators must resolve from `mod` at CALL time.

    THE DISCRIMINATOR IS `True`, AND THE FIRST VERSION OF THIS HELPER GOT IT
    WRONG — TWICE, in two different ways. Both are recorded because the second
    is the subtler and cost a full-suite run to surface.

    (1) Patching `_is_owncloud_backend` to False and asserting
    PROV_AUTHORITATIVE passes whether or not the patch is consulted, because the
    real function ALSO returns False on a local-backend test box — positive
    control and negative control produced identical output (guard-2421). The
    mutation that made `_sweep_write_guard` import the collaborator itself —
    exactly the regression this test exists to catch — survived it.

    (2) Patching to True and relying on `OwnCloudBackend.from_env()` to FAIL is
    environment-dependent, not deterministic. It held when run by hand and went
    RED inside the full suite, where the backend constructs successfully: the
    reader then reached the REAL store, did not find the fixture goal, and
    returned PROV_NONE. That version also made a live store read from a unit
    test, which is its own defect (guard-955). Never assert on a failure path
    you have not forced.

    So the backend is made deterministically unavailable here. The own-cloud
    branch is then guaranteed to degrade to PROV_LOCAL_MIRROR, with no network
    and no ambient dependency. If a refactor ever lets the shared module resolve
    its own `_is_owncloud_backend`, the real one returns False on a local box,
    the LOCAL branch is taken instead, and this goes red.
    """
    seen = {"read": 0}

    def _read(source):
        seen["read"] += 1
        return [({"goals": [OPEN_GOAL]}, "world")]

    import owncloud_backend

    def _unavailable(*a, **k):
        raise RuntimeError("test: backend deliberately unavailable")

    monkeypatch.setattr(owncloud_backend.OwnCloudBackend, "from_env",
                        staticmethod(_unavailable))
    monkeypatch.setattr(mod, "_is_owncloud_backend", lambda: True)
    monkeypatch.setattr(mod, "_read_aspirations", _read)
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")

    assert prov == swg.PROV_LOCAL_MIRROR, (
        "the own-cloud branch was not taken — this module's "
        "_is_owncloud_backend stub was not consulted (guard-2385 regression)")
    assert goal == OPEN_GOAL and seen["read"] == 1, (
        "this module's _read_aspirations stub was not consulted")


# ---------------------------------------------------------------------------
# The shared policy, directly. Pure function — no I/O, no fixtures.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["completed", "skipped", "expired", "archived"])
def test_a_terminal_status_refuses(status):
    goal = dict(OPEN_GOAL, status=status)
    reason = swg.stale_candidate_reason(goal, swg.PROV_AUTHORITATIVE)
    assert reason is not None and status in reason


@pytest.mark.parametrize("field", list(swg.COMPLETION_PROVENANCE_FIELDS))
def test_completion_provenance_refuses_even_while_status_looks_open(field):
    """The measured victim's shape: status re-openable, close already landed."""
    goal = dict(OPEN_GOAL, **{field: "bravo"})
    reason = swg.stale_candidate_reason(goal, swg.PROV_AUTHORITATIVE)
    assert reason is not None and field in reason


def test_a_local_mirror_read_refuses():
    """THE ONE THAT MATTERS MOST — an unverifiable read is not permission.

    On own-cloud a local re-read returns the SAME stale bytes the scan saw, so a
    guard that merely 're-checks before writing' would have passed in the real
    incident. This pins the policy to the store of record.
    """
    reason = swg.stale_candidate_reason(OPEN_GOAL, swg.PROV_LOCAL_MIRROR)
    assert reason is not None and "unverifiable" in reason


def test_goal_absent_from_the_store_refuses():
    assert swg.stale_candidate_reason(None, swg.PROV_NONE) is not None


@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_a_genuinely_open_goal_passes(status):
    assert swg.stale_candidate_reason(dict(OPEN_GOAL, status=status),
                                      swg.PROV_AUTHORITATIVE) is None


def test_a_caller_may_narrow_the_open_set_but_the_default_is_the_shared_one():
    assert swg.DEFAULT_OPEN_STATUSES == ("pending", "in-progress")
    assert swg.stale_candidate_reason(dict(OPEN_GOAL, status="in-progress"),
                                      swg.PROV_AUTHORITATIVE,
                                      open_statuses=("pending",)) is not None


# ---------------------------------------------------------------------------
# routing-audit-target-status-sweep — writes outcome_note then status=skipped
# ---------------------------------------------------------------------------

def test_routing_audit_refuses_and_writes_nothing(routing, tmp_path):
    routing._reread_goal_authoritative = lambda s, g: (
        dict(OPEN_GOAL, status="completed", completed_by="bravo"),
        swg.PROV_AUTHORITATIVE)
    _forbid_writes(routing, "routing-audit _mark_skipped")
    metrics = tmp_path / "m.jsonl"
    assert routing._mark_skipped("world", "g-1-1", "(x)",
                                 metrics_path=metrics) is False
    rows = [json.loads(l) for l in metrics.read_text().splitlines() if l.strip()]
    assert [r["type"] for r in rows] == ["routing_audit_refused_stale_candidate"]
    assert rows[0]["goal_id"] == "g-1-1" and rows[0]["reason"]


def test_routing_audit_writes_when_the_goal_is_genuinely_open(routing, tmp_path):
    """The guard must not become a permanent refusal — the sweep still works."""
    routing._reread_goal_authoritative = lambda s, g: (OPEN_GOAL,
                                                       swg.PROV_AUTHORITATIVE)
    calls = []
    routing._py = lambda args, input_text=None: (calls.append(args), (0, "", ""))[1]
    assert routing._mark_skipped("world", "g-1-1", "(x)",
                                 metrics_path=tmp_path / "m.jsonl") is True
    assert len(calls) == 2  # outcome_note, then status


def test_routing_audit_refusal_never_crashes_without_metrics(routing):
    routing._reread_goal_authoritative = lambda s, g: (None, swg.PROV_NONE)
    _forbid_writes(routing, "routing-audit _mark_skipped")
    assert routing._mark_skipped("world", "g-1-1", "(x)") is False


def test_routing_audit_seam_resolves_its_own_module_stubs(routing, monkeypatch):
    _assert_seam_is_patchable(routing, monkeypatch)


# ---------------------------------------------------------------------------
# parent-supersession-sweep — writes outcome_note then status=COMPLETED.
# The terminal status differs; the destructive first write does not.
# ---------------------------------------------------------------------------

def test_supersession_refuses_and_writes_nothing(supersession, tmp_path):
    supersession._reread_goal_authoritative = lambda s, g: (
        dict(OPEN_GOAL, status="completed", outcome_class="deep"),
        swg.PROV_AUTHORITATIVE)
    _forbid_writes(supersession, "parent-supersession _mark_superseded")
    metrics = tmp_path / "m.jsonl"
    assert supersession._mark_superseded("world", "g-1-1", ["g-1-2"],
                                         metrics_path=metrics) is False
    rows = [json.loads(l) for l in metrics.read_text().splitlines() if l.strip()]
    assert [r["type"] for r in rows] == ["parent_supersession_refused_stale_candidate"]
    assert rows[0]["sibling_ids"] == ["g-1-2"]


def test_supersession_refuses_on_an_unverifiable_read(supersession, tmp_path):
    supersession._reread_goal_authoritative = lambda s, g: (OPEN_GOAL,
                                                            swg.PROV_LOCAL_MIRROR)
    _forbid_writes(supersession, "parent-supersession _mark_superseded")
    assert supersession._mark_superseded("world", "g-1-1", ["g-1-2"],
                                         metrics_path=tmp_path / "m.jsonl") is False


def test_supersession_writes_when_the_goal_is_genuinely_open(supersession, tmp_path):
    supersession._reread_goal_authoritative = lambda s, g: (OPEN_GOAL,
                                                            swg.PROV_AUTHORITATIVE)
    calls = []
    supersession._py = lambda args, input_text=None: (calls.append(args), (0, "", ""))[1]
    assert supersession._mark_superseded("world", "g-1-1", ["g-1-2"],
                                         metrics_path=tmp_path / "m.jsonl") is True
    assert len(calls) == 3  # outcome_note, status, completed_date


def test_supersession_seam_resolves_its_own_module_stubs(supersession, monkeypatch):
    _assert_seam_is_patchable(supersession, monkeypatch)


# ---------------------------------------------------------------------------
# One predicate, three callers — the reason the policy was extracted at all.
# ---------------------------------------------------------------------------

def test_all_three_sweeps_share_one_policy_object():
    unblock = _load("unblock-parent-status-sweep", "unblock_swg")
    routing = _load("routing-audit-target-status-sweep", "routing_swg2")
    supers = _load("parent-supersession-sweep", "supers_swg2")
    for mod in (unblock, routing, supers):
        assert mod._shared_stale_candidate_reason is swg.stale_candidate_reason
        assert mod._shared_reread_goal_authoritative is swg.reread_goal_authoritative


# ---------------------------------------------------------------------------
# : a LIVE CLAIM must refuse, and execution HISTORY must not.
#
# The incident: unblock-parent-status-sweep marked  skipped with
# "parent resolved without action needed" 11 seconds before the holding agent's
# own outcome_note landed. The sweep branched on `status`, which read "pending"
# — and the claim path writes claimed_by_sid / started / executed_by_sid while
# leaving status alone, so the single field the sweep consulted is exactly the
# one a live claim does not move.
#
# WHY THE PREDICATE IS `claimed_by_sid` AND NOT `started`/`executed_by_sid`,
# which is what the originating report proposed. Measured on the world store
# 2026-08-24, 2,193 open goals: claimed_by_sid on 4, started on 231 (10.5%),
# executed_by_sid on 205 (9.3%); on terminal goals the latter two persist at
# ~78% because nothing pops them on release. They are execution HISTORY, not
# ownership. Keying the refusal on them would have disarmed all three sweeps
# across ~10% of the open queue, permanently and silently — a far worse defect
# than the one being fixed. That is what the second test below pins.
#
# EXPECTED MUTATION OUTCOMES, stated before running (guard-4166). With
# ACTIVE_CLAIM_FIELDS reverted to ():
#   test_a_live_claim_refuses ................................ RED
#   test_the_unblock_sweep_refuses_a_claimed_goal_and_writes_nothing  RED
#   test_execution_history_alone_does_not_refuse ............. GREEN  (control)
#   test_a_genuinely_open_goal_passes (pre-existing) ......... GREEN  (control)
# An all-red mutation run on a NARROWING fix is a warning sign, not a success.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"claimed_by": "bravo"},   # ordinary live claim
    {},                        # guard-4434: name cleared, sid survives — still a claim
])
def test_a_live_claim_refuses(extra):
    goal = dict(OPEN_GOAL, claimed_by_sid="8d277ad6-597f-4c1a-9e00-000000000001",
                **extra)
    reason = swg.stale_candidate_reason(goal, swg.PROV_AUTHORITATIVE)
    assert reason is not None, (
        "a goal held by a live claim was cleared for a terminal write — this is "
        "the g-115-7410 defect")
    assert "claim in flight" in reason


def test_execution_history_alone_does_not_refuse():
    """THE CONTROL FOR THE NARROWING — must stay GREEN under the mutant.

    started / executed_by / executed_by_sid survive a release, so a goal that
    was worked and handed back carries all three while being genuinely
    unclaimed. If a later author "helpfully" widens ACTIVE_CLAIM_FIELDS to
    include them, this goes red — which is the point: that widening silently
    freezes ~10% of the open queue against every sweep that shares this policy.
    """
    goal = dict(OPEN_GOAL,
                started="2026-08-24T00:32:40",
                executed_by="alpha",
                executed_by_sid="1dc6fc35-c568-4912-8ef3-1cf10b102721")
    assert swg.stale_candidate_reason(goal, swg.PROV_AUTHORITATIVE) is None, (
        "execution history was mistaken for a live claim — this would disarm "
        "all three sweeps across the ~10% of open goals that carry it")


def test_the_unblock_sweep_refuses_a_claimed_goal_and_writes_nothing(tmp_path):
    """The mutation-killer: assert the ABSENCE of the write, not a return value.

    Runs through the sweep the incident happened on, so the wiring is exercised
    and not merely the pure policy (this file's own header: a guard that is
    wired but unexercised is indistinguishable from one that is absent).
    """
    unblock = _load("unblock-parent-status-sweep", "unblock_swg")
    unblock._reread_goal_authoritative = lambda s, g: (
        dict(OPEN_GOAL, claimed_by="alpha",
             claimed_by_sid="1dc6fc35-c568-4912-8ef3-1cf10b102721",
             started="2026-08-24T00:32:40"),
        swg.PROV_AUTHORITATIVE)
    _forbid_writes(unblock, "unblock-parent-status-sweep _mark_skipped")
    metrics = tmp_path / "m.jsonl"
    assert unblock._mark_skipped("world", "g-1-1", "g-306-284", "pending",
                                 metrics_path=metrics) is False


def test_the_sweep_note_does_not_assert_that_nobody_acted():
    """ defect 2: the note claimed knowledge the sweep cannot have.

    The PREFIX is a dedup key (`_is_already_swept`, `_successor_marker_guard`
    and six tests all `startswith` it), so the correction had to be APPENDED.
    This pins both halves: the key survives, and the caveat is present.
    """
    import inspect
    unblock = _load("unblock-parent-status-sweep", "unblock_swg2")
    src = inspect.getsource(unblock._mark_skipped)
    assert 'f"parent resolved without action needed "' in src, (
        "the dedup prefix moved — _is_already_swept and _successor_marker_guard "
        "both startswith it")
    assert "cannot see whether an agent fix produced it" in src, (
        "the note still asserts nobody acted; the sweep reads current state "
        "only and cannot know that")
