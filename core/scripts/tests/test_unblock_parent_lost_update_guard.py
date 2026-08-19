""": unblock-parent-status-sweep must not overwrite a goal another
box completed between scan and apply.

THE MEASURED INCIDENT (bravo, cc-05, 2026-08-15). g-115-6326 completed at
14:33:38 with a 6,270-char outcome_note. Three seconds later, from ALPHA's box,
this sweep wrote status=skipped and replaced that note with a 78-char template.
The run reported rc=0, scanned:46 candidates:1 applied:1 — a clean success.

NEGATIVES FIRST. The dangerous direction is a write that proceeds on a stale
decision, so the refusal cases come first and the happy path last. Every test
here was verified RED by mutation before being committed.

THE ONE THAT MATTERS MOST is `test_a_local_mirror_read_refuses`: on own-cloud a
local re-read returns the SAME stale bytes the scan saw, so a guard that merely
"re-checks before writing" would have passed in the real incident and changed
nothing. That test is what pins the fix to the store of record rather than to a
re-read.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_module():
    """Import unblock-parent-status-sweep.py (hyphen in name blocks plain import)."""
    spec = importlib.util.spec_from_file_location(
        "unblock_parent_status_sweep_lug",
        SCRIPT_DIR / "unblock-parent-status-sweep.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return load_module()


OPEN_GOAL = {"id": "g-1-1", "status": "pending", "outcome_note": "REAL WORK"}


def _stub_reread(mod, goal, prov):
    mod._reread_goal_authoritative = lambda source, goal_id: (goal, prov)


def _forbid_writes(mod):
    """Any write attempt is a test failure, not a mocked success."""
    def explode(*a, **k):
        raise AssertionError("_mark_skipped issued a WRITE after refusing")
    mod._py = explode


# ---------------------------------------------------------------------------
# NEGATIVES — the write must be refused
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["completed", "skipped", "expired",
                                    "decomposed", "superseded"])
def test_a_terminal_status_on_reread_refuses(mod, status):
    """The literal race: candidate scanned open, terminal by apply time."""
    _stub_reread(mod, dict(OPEN_GOAL, status=status), mod.PROV_AUTHORITATIVE)
    reason = mod._stale_candidate_reason("world", "g-1-1")
    assert reason is not None
    assert status in reason


@pytest.mark.parametrize("field", ["completed_by", "completed_by_sid",
                                   "outcome_class"])
def test_completion_provenance_refuses_even_while_status_looks_open(mod, field):
    """A close in flight can stamp provenance before status settles.

    This is the damage signature the goal names (`status=skipped AND
    completed_by AND outcome_class`), so provenance must be checked
    INDEPENDENTLY of status rather than as a corroborating detail.
    """
    _stub_reread(mod, dict(OPEN_GOAL, **{field: "x"}), mod.PROV_AUTHORITATIVE)
    reason = mod._stale_candidate_reason("world", "g-1-1")
    assert reason is not None
    assert field in reason


def test_a_local_mirror_read_refuses(mod):
    """UNVERIFIABLE IS NOT PERMISSION — and this is the whole fix.

    In the real incident alpha's mirror never carried bravo's completion, so a
    local re-read would have returned status=pending and the write would have
    proceeded. Treating a mirror read as good enough reproduces the bug with
    extra steps.
    """
    _stub_reread(mod, dict(OPEN_GOAL), mod.PROV_LOCAL_MIRROR)
    reason = mod._stale_candidate_reason("world", "g-1-1")
    assert reason is not None
    assert "unreachable" in reason or "unverifiable" in reason


def test_goal_absent_from_the_store_refuses(mod):
    _stub_reread(mod, None, mod.PROV_NONE)
    assert mod._stale_candidate_reason("world", "g-1-1") is not None


def test_the_race_leaves_outcome_note_byte_identical(mod, tmp_path):
    """End-to-end: selected while open, completed before apply, no write at all.

    Asserts on the ABSENCE of any write call rather than on the note's value —
    reading the note back would pass even if a write had happened and been
    reverted, and would not catch a write to a different field.
    """
    victim = {"id": "g-1-1", "status": "completed",
              "completed_by": "bravo", "completed_by_sid": "0a35f258",
              "outcome_class": "deep", "outcome_note": "SIX THOUSAND CHARS"}
    _stub_reread(mod, victim, mod.PROV_AUTHORITATIVE)
    _forbid_writes(mod)

    metrics = tmp_path / "m.jsonl"
    ok = mod._mark_skipped("world", "g-1-1", "g-1-parent", "completed",
                           metrics_path=metrics, aspiration_id="asp-1")
    assert ok is False
    assert victim["outcome_note"] == "SIX THOUSAND CHARS"
    assert victim["status"] == "completed"


def test_every_refusal_is_counted(mod, tmp_path):
    """A silent no-op is indistinguishable from never having raced.

    Without this row the guard's own effectiveness is unmeasurable, which is how
    a guard gets 'simplified' away later.
    """
    _stub_reread(mod, dict(OPEN_GOAL, status="completed"), mod.PROV_AUTHORITATIVE)
    _forbid_writes(mod)
    metrics = tmp_path / "m.jsonl"

    mod._mark_skipped("world", "g-1-1", "g-1-parent", "completed",
                      metrics_path=metrics, aspiration_id="asp-1")

    rows = [json.loads(l) for l in metrics.read_text().splitlines() if l.strip()]
    assert len(rows) == 1, f"expected exactly one refusal row, got {rows}"
    r = rows[0]
    assert r["type"] == "unblock_parent_refused_stale_candidate"
    assert r["goal_id"] == "g-1-1"
    assert r["parent_id"] == "g-1-parent"
    assert r["aspiration_id"] == "asp-1"
    assert r["reason"], "the refusal reason must be recorded, not just the fact"


def test_a_refusal_never_crashes_when_metrics_are_disabled(mod):
    """metrics_path=None is the documented 'no metrics' contract (fail-open)."""
    _stub_reread(mod, dict(OPEN_GOAL, status="completed"), mod.PROV_AUTHORITATIVE)
    _forbid_writes(mod)
    assert mod._mark_skipped("world", "g-1-1", "p", "completed",
                             metrics_path=None) is False


# ---------------------------------------------------------------------------
# The happy path must survive — a guard that refuses everything is not a fix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["pending", "in-progress"])
def test_a_genuinely_open_goal_still_passes(mod, status):
    _stub_reread(mod, dict(OPEN_GOAL, status=status), mod.PROV_AUTHORITATIVE)
    assert mod._stale_candidate_reason("world", "g-1-1") is None


def test_an_open_goal_still_gets_written(mod, tmp_path):
    """Positive control. Without this, every refusal test above would pass
    against a `_mark_skipped` that unconditionally returns False."""
    _stub_reread(mod, dict(OPEN_GOAL), mod.PROV_AUTHORITATIVE)
    calls = []

    def fake_py(args, input_text=None):
        calls.append(args)
        return (0, "", "")

    mod._py = fake_py
    ok = mod._mark_skipped("world", "g-1-1", "g-1-parent", "completed",
                           metrics_path=tmp_path / "m.jsonl")
    assert ok is True
    assert len(calls) == 2, "expected the note write then the status write"
    assert "outcome_note" in calls[0]
    assert "status" in calls[1]
    assert not (tmp_path / "m.jsonl").exists(), "a pass must emit no refusal row"


# ---------------------------------------------------------------------------
# Provenance plumbing — the local-backend case must not self-refuse
# ---------------------------------------------------------------------------

def test_local_backend_treats_its_own_file_as_authoritative(mod, monkeypatch):
    """On a non-own-cloud box the local file IS the store of record.

    Calling it a mirror would refuse every write on a local deployment — the
    exact degradation `_team_state` warns about in its provenance docstring.
    """
    monkeypatch.setattr(mod, "_is_owncloud_backend", lambda: False)
    monkeypatch.setattr(mod, "_read_aspirations",
                        lambda source: [{"goals": [dict(OPEN_GOAL)]}])
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")
    assert prov == mod.PROV_AUTHORITATIVE
    assert goal["id"] == "g-1-1"


def test_local_backend_absent_goal_is_none_not_mirror(mod, monkeypatch):
    monkeypatch.setattr(mod, "_is_owncloud_backend", lambda: False)
    monkeypatch.setattr(mod, "_read_aspirations", lambda source: [{"goals": []}])
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")
    assert goal is None and prov == mod.PROV_NONE


def test_the_real_reread_handles_read_aspirations_tuple_shape(mod, monkeypatch):
    """`_read_aspirations` yields (aspiration, source) TUPLES, not bare dicts.

    THIS IS THE TEST THE REST OF THIS FILE COULD NOT PROVIDE, and the gap is
    worth naming. Every other test here stubs `_reread_goal_authoritative` — the
    function that was broken — so all 19 passed against a version that raised
    AttributeError on the real shape. The exception was swallowed into "goal not
    found", which the guard correctly treats as a REFUSAL, so the sweep silently
    applied nothing while reporting rc=0. A wedge that looks exactly like the
    guard working. Only the integration suite caught it.

    Pins BOTH shapes because the own-cloud branch parses raw JSONL into bare
    dicts while this branch gets tuples.
    """
    monkeypatch.setattr(mod, "_is_owncloud_backend", lambda: False)

    tuple_shape = [({"id": "asp-1", "goals": [dict(OPEN_GOAL)]}, "world")]
    monkeypatch.setattr(mod, "_read_aspirations", lambda source: tuple_shape)
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")
    assert prov == mod.PROV_AUTHORITATIVE, "tuple shape must resolve, not refuse"
    assert goal["id"] == "g-1-1"

    dict_shape = [{"id": "asp-1", "goals": [dict(OPEN_GOAL)]}]
    monkeypatch.setattr(mod, "_read_aspirations", lambda source: dict_shape)
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")
    assert prov == mod.PROV_AUTHORITATIVE, "bare-dict shape must resolve too"
    assert goal["id"] == "g-1-1"


def test_a_read_error_is_announced_not_swallowed(mod, monkeypatch, capsys):
    """A silent refusal is a sweep that reports success while doing nothing."""
    monkeypatch.setattr(mod, "_is_owncloud_backend", lambda: False)

    def boom(source):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(mod, "_read_aspirations", boom)
    goal, prov = mod._reread_goal_authoritative("world", "g-1-1")
    assert goal is None and prov == mod.PROV_NONE
    assert "RuntimeError" in capsys.readouterr().err


def test_the_predicate_is_imported_not_redefined():
    """guard-2783: one predicate per question.

    A third copy of the backend dispatch would drift the first time a backend
    name is added — in the direction that makes this guard read the mirror while
    believing it read the store.
    """
    src = (SCRIPT_DIR / "unblock-parent-status-sweep.py").read_text(encoding="utf-8")
    assert "def _is_owncloud_backend" not in src, (
        "the backend predicate must be imported from _team_state, not redefined")
    assert "from _team_state import" in src
