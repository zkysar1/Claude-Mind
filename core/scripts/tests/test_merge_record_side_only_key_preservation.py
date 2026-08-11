"""test_merge_record_side_only_key_preservation.py — pins that the record-level
coordination_merge handlers PRESERVE a key present on only one side (g-115-5287).

WHY THIS FILE EXISTS, AND WHY THE SIBLING TEST DOES NOT COVER IT.
test_merge_handlers_commutativity_property.py iterates the same live registry
and its inputs already carry only_a/only_b markers — but it asserts BYTE
EQUALITY of merge(a, b) and merge(b, a). A handler that drops a one-sided key
drops it identically in both arg orders, so it is perfectly commutative and
passes. Commutativity and preservation are ORTHOGONAL properties, and until
this file only the first was pinned. That is why the one-sided-key class was
found three separate times by hand (g-115-4163 _merge_aspiration_record,
g-115-5017 _merge_goal, g-115-5287's audit) rather than by the test that
already visits every handler.

SCOPE. This file pins the handlers g-115-5287 audited and found CORRECT, so a
later "simplification" that removes a preservation loop fails here instead of
silently losing fleet data. It deliberately does NOT assert the property
registry-wide: 14 registered handlers drop a one-sided key and they split
three ways, not two —
  (a) preservation loop present            -> correct (pinned here)
  (b) newest-wins-wholesale, OPEN schema   -> genuine exposure
  (c) CLOSED schema, VALIDATING consumer   -> dropping is REQUIRED
merge_improvement_velocity is a measured (c): meta-impk's
validate_velocity_structure RAISES on any unknown top-level key, so preserving
one would produce a file its own writer refuses to read back. A blanket
registry-wide assertion would therefore be WRONG. Triage + the exemption-bearing
registry property are g-115-5295; _merge_strategic_focus is g-115-5294.

Runs under pytest AND standalone (`py -3 <file>`) — zero-arg test functions +
__main__ runner, no pytest import (pytest-less-box pattern, matching the
sibling commutativity file).
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import coordination_merge as cm  # noqa: E402

A_KEY = "zz_only_on_a"
B_KEY = "zz_only_on_b"


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=True, default=str)


def _assert_preserves(fn, rec_a: dict, rec_b: dict, label: str,
                      mark_a: object = "AAA", mark_b: object = "BBB") -> None:
    """Both sides' exclusive keys survive, in BOTH arg orders, and the merge is
    byte-commutative. Checking both orders matters: `out = dict(win)` keeps the
    winner's exclusive key by construction, so asserting only one side would
    pass on a fully-unrepaired handler whenever that side happened to win.

    mark_a/mark_b are overridable because a VALUE-map handler (_merge_stamp_map
    is field-name -> timestamp) would otherwise be fed a non-timestamp marker
    and compared on a branch it never takes in production — the wrong-arg-shape
    probe class (rb-5235). Give each handler markers of its own value type."""
    a = dict(rec_a, **{A_KEY: mark_a})
    b = dict(rec_b, **{B_KEY: mark_b})
    for first, second, order in ((a, b, "merge(a,b)"), (b, a, "merge(b,a)")):
        out = fn(first, second)
        assert out.get(A_KEY) == mark_a, (
            f"{label}: {order} DROPPED the a-only key — one-sided-key loss "
            f"(g-115-5287 class). got={out.get(A_KEY)!r}")
        assert out.get(B_KEY) == mark_b, (
            f"{label}: {order} DROPPED the b-only key — one-sided-key loss "
            f"(g-115-5287 class). got={out.get(B_KEY)!r}")
    ab, ba = fn(a, b), fn(b, a)
    assert _canon(ab) == _canon(ba), (
        f"{label}: NOT commutative once side-only keys are present — guard-907.")
    assert list(ab.keys()) == list(ba.keys()), (
        f"{label}: key ORDER diverged between arg orders — guard-907.")


# --- the two handlers  was filed to audit --------------------------

_PIPE_A = {"id": "2026-01-01_h", "stage": "resolved", "outcome": "CONFIRMED",
           "outcome_date": "2026-01-05T00:00:00", "experience_ref": "exp-h"}
_PIPE_B = {"id": "2026-01-01_h", "stage": "active", "confidence": 0.4}

_SPARK_A = {"id": "sq-001", "type": "question", "text": "T",
            "times_asked": 10, "sparks_generated": 3, "yield_rate": 0.3}
_SPARK_B = {"id": "sq-001", "type": "question", "text": "T",
            "times_asked": 4, "sparks_generated": 9, "status": "active"}


def test_merge_pipeline_record_preserves_side_only_keys():
    _assert_preserves(cm._merge_pipeline_record, _PIPE_A, _PIPE_B,
                      "_merge_pipeline_record")


def test_merge_spark_record_preserves_side_only_keys():
    _assert_preserves(cm._merge_spark_record, _SPARK_A, _SPARK_B,
                      "_merge_spark_record")


def test_pipeline_loser_only_field_survives_stage_rank_loss():
    """The loser here is chosen by STAGE RANK, not content — the branch a
    lower-ranked side takes. `confidence` lives only on the active (losing)
    side and must still survive."""
    out = cm._merge_pipeline_record(_PIPE_A, _PIPE_B)
    assert out.get("confidence") == 0.4, (
        f"loser-only field dropped on the stage-rank branch: {out.get('confidence')!r}")


def test_spark_counters_max_and_yield_rate_recomputed():
    """guard-1153: counters get explicit semantics (MAX), and the DERIVED
    yield_rate is recomputed rather than carried — a blind MAX would overstate."""
    out = cm._merge_spark_record(_SPARK_A, _SPARK_B)
    assert out["times_asked"] == 10 and out["sparks_generated"] == 9
    assert out["yield_rate"] == round(9 / 10, 4), out["yield_rate"]


# --- the out=dict(a) family, verdicted independently ------------------------

_DICT_A_FAMILY = [
    ("_merge_counters", {"x": 1, "y": 5}, {"x": 3, "z": 7}),
    ("_merge_rb_record",
     {"id": "rb-1", "title": "T", "content": "C", "created": "2026-01-01T00:00:00"},
     {"id": "rb-1", "title": "T", "content": "C", "created": "2026-01-01T00:00:00",
      "status": "active"}),
    ("_merge_guard_record",
     {"id": "guard-1", "rule": "R", "created": "2026-01-01T00:00:00"},
     {"id": "guard-1", "rule": "R", "created": "2026-01-01T00:00:00",
      "status": "active"}),
    ("_merge_sig_record",
     {"id": "sig-1", "created": "2026-01-01T00:00:00"},
     {"id": "sig-1", "created": "2026-01-01T00:00:00", "occurrences": 2}),
]


def test_dict_a_family_preserves_side_only_keys():
    """The goal explicitly warned not to assume the dict(win) analysis transfers
    to this structurally different family — so it gets its own verdict."""
    for name, ra, rb in _DICT_A_FAMILY:
        fn = getattr(cm, name, None)
        assert fn is not None, f"{name} vanished from coordination_merge"
        _assert_preserves(fn, ra, rb, name)


# --- nested stamp-map key order ( regression) ----------------------

def test_merge_stamp_map_preserves_and_canonicalizes_order():
    """_merge_stamp_map is field-name -> TIMESTAMP, so its markers are
    timestamps too (see _assert_preserves). Beyond preservation, its output key
    order must be canonical."""
    _assert_preserves(cm._merge_stamp_map,
                      {"k1": "2026-01-01T00:00:00"}, {"k2": "2026-02-01T00:00:00"},
                      "_merge_stamp_map",
                      mark_a="2026-04-01T00:00:00", mark_b="2026-05-01T00:00:00")


def test_nested_stamp_map_does_not_break_file_level_commutativity():
    """THE  REGRESSION, asserted where guard-907 actually binds: BYTES.

    Two boxes each amend a DIFFERENT field of the SAME guardrail — the exact
    disjoint-key case _merge_stamp_map's own docstring gives as the reason to
    union. Before the fix the merged CONTENT was identical but the nested
    amended_fields insertion order differed by arg order, and _dump_jsonl calls
    json.dumps WITHOUT sort_keys, so that order reached the output bytes:
    merge_guardrails(a,b) != merge_guardrails(b,a) — guard-907's ETag-fenced
    PUT ping-pong precondition.

    This is asserted at FILE level on purpose. A record-level canon comparison
    with sort_keys=True passes on the unrepaired code (content was never the
    problem), so a record-level test would have been a false green — which is
    precisely how this survived the registry-wide commutativity property, whose
    divergence case places distinct keys at a record's TOP level and never
    inside a nested map."""
    field = cm._AMEND_STAMP_FIELD
    base = {"id": "guard-001", "rule": "R", "created": "2026-01-01T00:00:00"}
    ra = dict(base, **{field: {"trigger_condition": "2026-02-01T00:00:00"}})
    rb = dict(base, **{field: {"action_hint": "2026-03-01T00:00:00"}})
    la = (json.dumps(ra) + "\n").encode()
    lb = (json.dumps(rb) + "\n").encode()

    ab, ba = cm.merge_guardrails(la, lb), cm.merge_guardrails(lb, la)
    assert ab == ba, (
        "merge_guardrails NOT byte-commutative when two copies carry disjoint "
        "nested amended_fields keys — guard-907 violation.\n"
        f" merge(a,b)={ab!r}\n merge(b,a)={ba!r}")

    # Both stamps must survive the union (the docstring's stated purpose), and
    # the surviving order must be the canonical one.
    merged = json.loads(ab.decode().strip())
    stamps = merged.get(field) or {}
    assert set(stamps) == {"trigger_condition", "action_hint"}, stamps
    assert list(stamps) == sorted(stamps), (
        f"nested stamp-map key order is not canonical: {list(stamps)}")


def test_positive_control_unsorted_stamp_map_breaks_file_commutativity():
    """The RED half of this regression, kept executable instead of living in a
    transcript. Swaps the pre-fix (unsorted) _merge_stamp_map back in and
    requires the file-level bytes to DIVERGE — proving the assertion above
    detects the defect rather than passing for some unrelated reason.

    Patched inside the function with restore in `finally` (never at module
    level — guard-1165), so a failure cannot leave the module altered for the
    rest of the session's suite."""
    def unsorted_stamp_map(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, vb in b.items():
            va = out.get(k)
            out[k] = vb if va is None or cm._ts_key(vb) > cm._ts_key(va) else va
        return out                      # <- pre-fix: no sorted() on the way out

    field = cm._AMEND_STAMP_FIELD
    base = {"id": "guard-001", "rule": "R", "created": "2026-01-01T00:00:00"}
    la = (json.dumps(dict(base, **{field: {"trigger_condition": "2026-02-01T00:00:00"}})) + "\n").encode()
    lb = (json.dumps(dict(base, **{field: {"action_hint": "2026-03-01T00:00:00"}})) + "\n").encode()

    original = cm._merge_stamp_map
    try:
        cm._merge_stamp_map = unsorted_stamp_map
        ab, ba = cm.merge_guardrails(la, lb), cm.merge_guardrails(lb, la)
        assert ab != ba, (
            "POSITIVE CONTROL DID NOT FIRE: the pre-fix unsorted stamp map "
            "produced byte-identical output, so the regression assertion above "
            "proves nothing.")
        # And the content really was identical — that is why it hid so long.
        assert (json.loads(ab.decode().strip()) == json.loads(ba.decode().strip())), (
            "control: records differ in CONTENT too, so this is not the "
            "order-only divergence the regression describes")
    finally:
        cm._merge_stamp_map = original


# --- convergence -------------------------------------------------------------

def test_multiround_convergence_over_permutations():
    """guard-907 beyond pairwise: every merge ORDER of three divergent copies
    must land on ONE value, or two boxes compute different bytes and the
    fenced-PUT loop ping-pongs."""
    for name, fn, recs in (
            ("_merge_pipeline_record", cm._merge_pipeline_record,
             [_PIPE_A, _PIPE_B, dict(_PIPE_A, extra="E")]),
            ("_merge_spark_record", cm._merge_spark_record,
             [_SPARK_A, _SPARK_B, dict(_SPARK_A, extra="E")])):
        results = set()
        for perm in itertools.permutations(recs):
            acc = perm[0]
            for r in perm[1:]:
                acc = fn(acc, r)
            results.add(_canon(acc))
        assert len(results) == 1, (
            f"{name}: {len(results)} distinct results across 6 merge orders — "
            f"non-convergent.")


# --- positive control --------------------------------------------------------

def test_positive_control_unrepaired_shape_fails_the_assertion():
    """PROOF THE ASSERTION HAS TEETH (and the red half of the red/green split).

    Every assertion above passes against live code, so on its own this file is
    indistinguishable from one whose predicate is broken. This reconstructs the
    UNREPAIRED shape — `out = dict(win)` with only enumerated families
    overridden and no preservation loop, exactly what g-115-4163 and g-115-5017
    each had to repair — and requires _assert_preserves to REJECT it.

    Reconstructed locally rather than by mutating production code, so the
    control costs nothing at runtime and cannot leave the module patched."""
    def unrepaired(a: dict, b: dict) -> dict:
        win, lose = (a, b) if _canon(a) >= _canon(b) else (b, a)
        out = dict(win)                      # <- no preservation loop
        for f in ("outcome", "surprise"):    # <- enumerated families only
            if out.get(f) is None and lose.get(f) is not None:
                out[f] = lose[f]
        return out

    try:
        _assert_preserves(unrepaired, _PIPE_A, _PIPE_B, "unrepaired-control")
    except AssertionError as exc:
        assert "one-sided-key loss" in str(exc), (
            f"control failed for the WRONG reason: {exc}")
        return
    raise AssertionError(
        "POSITIVE CONTROL DID NOT FIRE: _assert_preserves accepted a handler "
        "with no preservation loop, so every green above is meaningless.")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
