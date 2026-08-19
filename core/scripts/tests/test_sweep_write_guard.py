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
