"""test_bitemporal_writer_path.py --  (BRD Gap 5, sub-goal A of ).

Pins the bi-temporal WRITER path: optional valid_from/valid_to validity-interval
fields across the Mind stores --

  * RB + guardrails dual-mirror validators (mind_api/src/store_registry.py daemon
    + core/scripts/reasoning-bank.py CLI), kept verbatim-in-sync;
  * the team-state belief snapshot (_team_belief.supersede_beliefs);
  * tree-node front-matter defaults (mind_api/src/world/tree_write._apply_defaults,
    whose CLI mirror tree.py apply_defaults is pinned identical by
    test_daemon_cli_mirror_parity.py).

It also pins the close-old-insert-new FALSIFICATION semantics at the record
level (the old record gets valid_to set and a NEW record is inserted with
valid_from -- NOT an in-place mutation) and the additive/backward-compatible
guarantee (records without the fields still validate).

Daemon-safe (no daemon spawn, no daemon_integration marker): the daemon
validators are plain functions, the CLI module loads via importlib (the pattern
from test_rb_entry_type_taxonomy_sync.py / test_applies_to_required.py), and the
tree default is a plain package import.

Cross-references:
  - g-306-35 -- added valid_from/valid_to + _validate_bitemporal dual-mirror
  - test_rb_entry_type_taxonomy_sync.py -- the dual-mirror sync pattern reused
  - test_daemon_cli_mirror_parity.py -- pins tree apply_defaults CLI<->daemon parity
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Daemon side -- plain package imports, no module-load side effects.
from mind_api.src import store_registry as SR  # noqa: E402
from mind_api.src.world import tree_write as TW  # noqa: E402

# CLI side -- core/scripts/reasoning-bank.py is hyphen-named, so load it via
# importlib. Importing the module bootstraps WORLD/path resolution, so stash
# MIND_WORLD/MIND_AGENT first and restore immediately after (guard-588: a
# module-level os.environ mutation must not leak into other tests in the same
# pytest session).
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="bitemporal-writer-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RB_PATH = CORE_SCRIPTS / "reasoning-bank.py"
_spec = importlib.util.spec_from_file_location("reasoning_bank", _RB_PATH)
_rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rb)

if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _team_belief as TB  # noqa: E402


# --- minimal-valid record builders ------------------------------------------

def _rb_rec(**over):
    rec = {"id": "rb-1", "title": "t", "type": "success", "category": "c",
           "content": "x", "applies_to": "any", "status": "active", "tags": []}
    rec.update(over)
    return rec


def _guard_rec(**over):
    rec = {"id": "guard-1", "rule": "r", "category": "c", "trigger_condition": "tc",
           "source": "s", "status": "active", "tags": []}
    rec.update(over)
    return rec


def _rb_both(rec):
    """Run rec through BOTH RB validators (daemon + CLI). Returns (daemon_err,
    cli_err) -- None on accept, the ValueError text on reject -- and asserts the
    two mirrors AGREE (both accept or both reject). Passes dict copies because
    the validators mutate (_normalize_tags)."""
    d = c = None
    try:
        SR.validate_rb_record(None, dict(rec))
    except ValueError as e:
        d = str(e)
    try:
        _rb.validate_rb_record(dict(rec))
    except ValueError as e:
        c = str(e)
    assert (d is None) == (c is None), (
        f"RB validator MIRROR DRIFT on {rec!r}: daemon={d!r} cli={c!r}")
    return d, c


def _guard_both(rec):
    """As _rb_both, for the guardrail validators."""
    d = c = None
    try:
        SR.validate_guard_record(None, dict(rec))
    except ValueError as e:
        d = str(e)
    try:
        _rb.validate_guard_record(dict(rec))
    except ValueError as e:
        c = str(e)
    assert (d is None) == (c is None), (
        f"GUARD validator MIRROR DRIFT on {rec!r}: daemon={d!r} cli={c!r}")
    return d, c


# --- RB validator: accept paths ---------------------------------------------

def test_rb_accepts_absent_bitemporal_backward_compat():
    # A record with NO valid_from/valid_to validates -- additive + backward-compat.
    d, c = _rb_both(_rb_rec())
    assert d is None and c is None


def test_rb_accepts_null_bitemporal():
    d, c = _rb_both(_rb_rec(valid_from=None, valid_to=None))
    assert d is None and c is None


def test_rb_accepts_closed_interval():
    d, c = _rb_both(_rb_rec(valid_from="2026-06-01T00:00:00",
                            valid_to="2026-06-19T00:00:00"))
    assert d is None and c is None


def test_rb_accepts_open_current_interval():
    # valid_to None = the record is currently valid (the common write shape).
    d, c = _rb_both(_rb_rec(valid_from="2026-06-19T00:00:00", valid_to=None))
    assert d is None and c is None


def test_rb_accepts_equal_endpoints():
    # valid_from == valid_to is a degenerate-but-legal zero-width interval.
    d, c = _rb_both(_rb_rec(valid_from="2026-06-19T00:00:00",
                            valid_to="2026-06-19T00:00:00"))
    assert d is None and c is None


# --- RB validator: reject paths ---------------------------------------------

def test_rb_rejects_inverted_interval():
    d, c = _rb_both(_rb_rec(valid_from="2026-06-19T00:00:00",
                            valid_to="2026-06-01T00:00:00"))
    assert d is not None and "valid_from" in d


def test_rb_rejects_nonstr_valid_from_b10():
    # B10: a non-str must raise a CLEAN ValueError, never a TypeError 500.
    d, c = _rb_both(_rb_rec(valid_from=20260619, valid_to=None))
    assert d is not None and "valid_from" in d


def test_rb_rejects_list_valid_to_b10():
    d, c = _rb_both(_rb_rec(valid_from=None, valid_to=["2026-06-19T00:00:00"]))
    assert d is not None and "valid_to" in d


def test_rb_rejects_malformed_datetime():
    d, c = _rb_both(_rb_rec(valid_from="not-a-date", valid_to=None))
    assert d is not None and "valid_from" in d


# --- guardrail validator: accept + the unknown-field-gate interaction --------

def test_guard_accepts_bitemporal_not_rejected_as_unknown_field():
    # CRITICAL: guardrails has the strict unknown-field gate. valid_from/valid_to
    # must be in GUARD_KNOWN_FIELDS (via GUARD_DEFAULT_FIELDS) or this 400s.
    d, c = _guard_both(_guard_rec(valid_from="2026-06-01T00:00:00", valid_to=None))
    assert d is None and c is None


def test_guard_rejects_inverted_interval():
    d, c = _guard_both(_guard_rec(valid_from="2026-06-19T00:00:00",
                                  valid_to="2026-06-01T00:00:00"))
    assert d is not None and "valid_from" in d


def test_guard_backward_compat_absent():
    d, c = _guard_both(_guard_rec())
    assert d is None and c is None


# --- dual-mirror sync invariants (the verbatim-in-sync contract) ------------

def test_rb_default_fields_carry_bitemporal_both_mirrors():
    for name, mod in (("daemon", SR), ("cli", _rb)):
        for f in ("valid_from", "valid_to"):
            assert mod.RB_DEFAULT_FIELDS.get(f, "MISSING") is None, (
                f"{name} RB_DEFAULT_FIELDS missing/!None {f}")


def test_guard_default_and_known_fields_carry_bitemporal_both_mirrors():
    for name, mod in (("daemon", SR), ("cli", _rb)):
        for f in ("valid_from", "valid_to"):
            assert mod.GUARD_DEFAULT_FIELDS.get(f, "MISSING") is None, (
                f"{name} GUARD_DEFAULT_FIELDS missing/!None {f}")
            # The unknown-field gate set is derived from GUARD_DEFAULT_FIELDS,
            # so the field must be allowlisted on both sides.
            assert f in mod.GUARD_KNOWN_FIELDS, (
                f"{name} GUARD_KNOWN_FIELDS missing {f} -- guard writes carrying "
                "it would 400 as an unknown field")


def test_guard_known_fields_carry_encoded_by_both_mirrors():
    """ — the writing-agent stamp, on the same in-sync contract.

    Unlike the bitemporal pair above, `encoded_by` is allowlisted in the
    EXPLICIT set rather than reached through GUARD_DEFAULT_FIELDS: a default
    would backfill a null onto every historical guardrail the next time any
    path rewrote the store, and that goal's contract is that prior rows are
    untouched. So it needs its own mirror assertion — the derived-from-defaults
    one above cannot cover it.

    The daemon writes this field on every guardrail append. A CLI mirror that
    omits it means the two validators disagree about what a valid guardrail is,
    which is the `displaced_from` failure shape: an unallowlisted field does not
    merely go unrecognized, it 400s every subsequent write to the records that
    carry it. No production caller reaches the CLI validator today; this test is
    what keeps the mirror honest if one returns.
    """
    for name, mod in (("daemon", SR), ("cli", _rb)):
        assert "encoded_by" in mod.GUARD_KNOWN_FIELDS, (
            f"{name} GUARD_KNOWN_FIELDS missing encoded_by -- guard writes "
            "carrying the append-time agent stamp would 400 as an unknown field")
        assert "encoded_by" not in mod.GUARD_DEFAULT_FIELDS, (
            f"{name} GUARD_DEFAULT_FIELDS gained encoded_by -- defaults are "
            "applied on rewrite, which would backfill historical records")


def test_validate_bitemporal_helper_present_both_mirrors():
    assert hasattr(SR, "_validate_bitemporal")
    assert hasattr(_rb, "_validate_bitemporal")


def test_validate_bitemporal_verbatim_in_sync():
    """The two _validate_bitemporal copies MUST be textually identical (the
    'validators mirror verbatim' contract, g-306-35; same regression class
    test_rb_entry_type_taxonomy_sync.py pins for RB_VALID_ENTRY_TYPES). A
    one-sided edit -- tweak the daemon helper but forget the CLI, or vice-versa
    -- diverges silently and would let a record one side accepts the other
    reject. This fails loudly the moment the bodies drift."""
    daemon_src = inspect.getsource(SR._validate_bitemporal)
    cli_src = inspect.getsource(_rb._validate_bitemporal)
    assert daemon_src == cli_src, (
        "_validate_bitemporal diverged between the daemon "
        "(mind_api/src/store_registry.py) and the CLI "
        "(core/scripts/reasoning-bank.py). These two copies are hand-kept "
        "verbatim-in-sync; edit BOTH sides when changing the helper.")


# --- falsification: close-old-insert-new (NOT in-place mutate) ---------------

def test_falsification_closes_old_and_inserts_new():
    """The writer-path falsification contract: superseding a record CLOSES the
    old one (sets valid_to) and INSERTS a new record (valid_from = now), leaving
    two distinct records -- never an in-place content mutation."""
    now = "2026-06-19T00:00:00"
    old = _rb_rec(id="rb-1", content="believed X",
                  valid_from="2026-06-01T00:00:00", valid_to=None)
    # 1. old is a valid CURRENT record (open interval).
    assert _rb_both(old) == (None, None)

    # 2. CLOSE old: stamp valid_to=now. A NEW dict -- the original is untouched.
    closed = {**old, "valid_to": now}
    assert _rb_both(closed) == (None, None)          # closed interval still valid
    assert old["valid_to"] is None                   # NOT mutated in place
    assert old["content"] == "believed X"            # content preserved

    # 3. INSERT new: a distinct record (new id) with valid_from=now, open.
    new = _rb_rec(id="rb-2", content="now believe Y", valid_from=now, valid_to=None)
    assert _rb_both(new) == (None, None)

    # Two distinct records survive the falsification: the closed history record
    # and the new current record -- the close-old-insert-new shape the reader
    # path () consumes to answer "what was believed at T".
    assert closed["id"] != new["id"]
    assert closed["valid_to"] == new["valid_from"] == now


# --- beliefs writer stamps the interval -------------------------------------

def test_beliefs_supersede_stamps_bitemporal_interval():
    out = TB.supersede_beliefs([], "partner-a", "observed", 0.5, "2026-06-19T00:00:00")
    assert out[0]["valid_from"] == "2026-06-19T00:00:00"
    assert out[0]["valid_to"] is None


# --- tree front-matter default --------------------------------------------

def test_tree_apply_defaults_seeds_bitemporal_null():
    # Daemon _apply_defaults seeds null valid_from/valid_to (read-safe). The CLI
    # tree.py apply_defaults is pinned identical by test_daemon_cli_mirror_parity.
    out = TW._apply_defaults({"key": "k", "children": []})
    assert out["valid_from"] is None
    assert out["valid_to"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
