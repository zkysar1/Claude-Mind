"""test_merge_handlers_commutativity_property.py — generic guard-907 property
test over ALL coordination_merge._HANDLERS (g-115-2350).

g-115-2341 fixed key-order non-commutativity in the 4 id-keyed merge fns and
added targeted regressions — but those pin only the EXISTING handlers. A
FUTURE handler registered in _HANDLERS that skips _commutative_key_order (or
introduces its own order/tiebreak nondeterminism) re-introduces the
fenced-PUT ping-pong class (guard-907) with no test to catch it. This file
closes that: it iterates the LIVE registry, so a newly registered basename is
tested automatically with zero edits here.

Per registered basename, format-matched synthetic divergent inputs exercise:
  (a) same-id records with DISTINCT new keys   (the g-115-2341 shape)
  (b) same-content serialization-ORDER divergence
  (c) multiround settle: merge(m, m) == m
  (d) degenerate inputs: (empty, empty), (doc, empty) vs (empty, doc)
asserting BYTE equality of merge(a, b) and merge(b, a) throughout.

Inputs are shaped by HANDLER FUNCTION (schema-strict handlers get minimal
valid shapes; everything else gets the generic id-keyed/extension shape), with
FIXED timestamps so LWW branches resolve deterministically. An unknown future
handler falls back to the generic shape for its extension — the property
assertions still hold because commutativity is shape-independent.

Runs under pytest AND standalone (`py -3 <file>`) — zero-arg test functions +
__main__ runner, no pytest import (pytest-less-box pattern, g-115-2336 lane).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import coordination_merge as cm  # noqa: E402

# Fixed, unequal timestamps — LWW branches must resolve the same way in both
# arg orders, never off wall-clock.
T_OLD = "2026-07-01T00:00:00"
T_NEW = "2026-07-02T00:00:00"


def _jsonl(*recs: dict) -> bytes:
    return ("".join(json.dumps(r, sort_keys=False) + "\n" for r in recs)).encode()


def _yaml_doc(d: dict, reverse: bool = False) -> bytes:
    # Hand-rolled flat-ish dump so key ORDER is controllable for case (b)
    # without depending on yaml.dump ordering. Values are json-encoded scalars/
    # containers, which yaml.safe_load parses fine (JSON is a YAML subset).
    items = list(d.items())
    if reverse:
        items = items[::-1]
    return ("".join(f"{k}: {json.dumps(v)}\n" for k, v in items)).encode()


def _json_doc(d: dict, reverse: bool = False) -> bytes:
    items = list(d.items())
    if reverse:
        items = items[::-1]
    return (json.dumps(dict(items), indent=2) + "\n").encode()


# ---------------------------------------------------------------------------
# Per-handler-function input builders. Each returns (side_a, side_b, order_a,
# order_b): sides carry shape (a) divergence; orders carry shape (b) — same
# content, different serialization order.
# ---------------------------------------------------------------------------

def _build_generic_jsonl():
    r1a = {"id": "x-001", "created": T_OLD, "note": "shared", "only_a": 1}
    r1b = {"id": "x-001", "created": T_OLD, "note": "shared", "only_b": 2}
    r2 = {"id": "x-002", "created": T_OLD, "unique_to": "a"}
    r3 = {"id": "x-003", "created": T_NEW, "unique_to": "b"}
    side_a = _jsonl(r1a, r2)
    side_b = _jsonl(r1b, r3)
    order_a = _jsonl(r2, r3)
    order_b = _jsonl(r3, r2)
    return side_a, side_b, order_a, order_b


def _build_reasoning_bank():
    r1a = {"id": "rb-901", "title": "t", "created": T_OLD, "status": "active",
           "only_a": 1}
    r1b = {"id": "rb-901", "title": "t", "created": T_OLD, "status": "active",
           "only_b": 2}
    r2 = {"id": "rb-902", "title": "a-side", "created": T_OLD, "status": "active"}
    r3 = {"id": "rb-903", "title": "b-side", "created": T_NEW, "status": "active"}
    return _jsonl(r1a, r2), _jsonl(r1b, r3), _jsonl(r2, r3), _jsonl(r3, r2)


def _build_guardrails():
    r1a = {"id": "guard-901", "rule": "r", "created": T_OLD, "status": "active",
           "only_a": 1}
    r1b = {"id": "guard-901", "rule": "r", "created": T_OLD, "status": "active",
           "only_b": 2}
    r2 = {"id": "guard-902", "rule": "a", "created": T_OLD, "status": "active"}
    r3 = {"id": "guard-903", "rule": "b", "created": T_NEW, "status": "active"}
    return _jsonl(r1a, r2), _jsonl(r1b, r3), _jsonl(r2, r3), _jsonl(r3, r2)


def _build_pattern_signatures():
    r1a = {"id": "sig-901", "pattern": "p", "created": T_OLD, "status": "active",
           "only_a": 1}
    r1b = {"id": "sig-901", "pattern": "p", "created": T_OLD, "status": "active",
           "only_b": 2}
    r2 = {"id": "sig-902", "pattern": "a", "created": T_OLD, "status": "active"}
    r3 = {"id": "sig-903", "pattern": "b", "created": T_NEW, "status": "active"}
    return _jsonl(r1a, r2), _jsonl(r1b, r3), _jsonl(r2, r3), _jsonl(r3, r2)


def _build_aspirations():
    g1a = {"id": "g-901-01", "status": "pending", "title": "g", "only_a": 1}
    g1b = {"id": "g-901-01", "status": "pending", "title": "g", "only_b": 2}
    g2 = {"id": "g-901-02", "status": "pending", "title": "a-side"}
    g3 = {"id": "g-901-03", "status": "pending", "title": "b-side"}
    asp_a = {"id": "asp-901", "status": "active", "title": "t",
             "created": T_OLD, "goals": [g1a, g2]}
    asp_b = {"id": "asp-901", "status": "active", "title": "t",
             "created": T_OLD, "goals": [g1b, g3]}
    asp_solo = {"id": "asp-902", "status": "active", "title": "solo",
                "created": T_NEW, "goals": []}
    side_a = _jsonl(asp_a)
    side_b = _jsonl(asp_b, asp_solo)
    order_a = _jsonl(asp_a, asp_solo)
    order_b = _jsonl(asp_solo, asp_a)
    return side_a, side_b, order_a, order_b


def _build_pipeline():
    r1a = {"id": "2026-07-01_prop-test", "status": "active", "created": T_OLD,
           "only_a": 1}
    r1b = {"id": "2026-07-01_prop-test", "status": "active", "created": T_OLD,
           "only_b": 2}
    r2 = {"id": "2026-07-01_a-side", "status": "active", "created": T_OLD}
    r3 = {"id": "2026-07-02_b-side", "status": "resolved", "created": T_NEW}
    return _jsonl(r1a, r2), _jsonl(r1b, r3), _jsonl(r2, r3), _jsonl(r3, r2)


def _build_spark_questions():
    r1a = {"id": "sq-901", "question": "same text identity", "created": T_OLD,
           "status": "active", "only_a": 1}
    r1b = {"id": "sq-901", "question": "same text identity", "created": T_OLD,
           "status": "active", "only_b": 2}
    r2 = {"id": "sq-902", "question": "a-side text", "created": T_OLD,
          "status": "active"}
    r3 = {"id": "sq-903", "question": "b-side text", "created": T_NEW,
          "status": "active"}
    return _jsonl(r1a, r2), _jsonl(r1b, r3), _jsonl(r2, r3), _jsonl(r3, r2)


def _build_team_state():
    base = {"last_updated": T_OLD,
            "agent_status": {"agent-a": {"last_active": T_OLD}}}
    doc_a = dict(base)
    doc_a["agent_status"] = {"agent-a": {"last_active": T_NEW, "only_a": 1}}
    doc_b = {"last_updated": T_NEW,
             "agent_status": {"agent-b": {"last_active": T_NEW, "only_b": 2}}}
    order_doc = {"last_updated": T_NEW, "agent_status": {"agent-a": {}},
                 "extra": "same-content"}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_module_health():
    doc_a = {"modules": {"mod-shared": {"runs": 3, "only_a": 1},
                         "mod-a": {"runs": 1}}}
    doc_b = {"modules": {"mod-shared": {"runs": 5, "only_b": 2},
                         "mod-b": {"runs": 2}}}
    order_doc = {"modules": {"mod-a": {"runs": 1}}, "extra": "x"}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_forged_skills():
    doc_a = {"skills": {"skill-shared": {"forged_date": T_OLD[:10],
                                         "parent": "p", "only_a": 1},
                        "skill-a": {"forged_date": T_OLD[:10], "parent": "p"}}}
    doc_b = {"skills": {"skill-shared": {"forged_date": T_NEW[:10],
                                         "parent": "p", "only_b": 2},
                        "skill-b": {"forged_date": T_NEW[:10], "parent": "q"}}}
    order_doc = {"skills": {"skill-a": {"forged_date": T_OLD[:10]}},
                 "extra": "x"}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_skill_relations():
    doc_a = {"last_updated": None,
             "forged_relations": [
                 {"source": "s-shared", "target": "t", "type": "compose_with"},
                 {"source": "s-a", "target": "t", "type": "similar_to"}],
             "co_invocation_log": [
                 {"goal_id": "g-a", "skills": ["x", "y"], "date": T_OLD}]}
    doc_b = {"last_updated": T_NEW,
             "forged_relations": [
                 {"source": "s-shared", "target": "t", "type": "compose_with",
                  "confidence": 0.9},
                 {"source": "s-b", "target": "t", "type": "compose_with"}],
             "co_invocation_log": [
                 {"goal_id": "g-b", "skills": ["p", "q"], "date": T_NEW}]}
    order_doc = {"last_updated": None,
                 "forged_relations": [
                     {"source": "s-a", "target": "t", "type": "similar_to"}]}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_outcome_metrics():
    doc_a = {"updated_at": T_OLD, "window": "24h", "only_a": 1}
    doc_b = {"updated_at": T_NEW, "window": "24h", "only_b": 2}
    order_doc = {"updated_at": T_NEW, "window": "24h"}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_tree():
    doc_a = {"last_updated": T_OLD, "total_entities": 3,
             "nodes": {"root/node-a": {"summary": "a", "only_a": 1}},
             "unmapped_categories": ["cat-a"]}
    doc_b = {"last_updated": T_NEW, "total_entities": 5,
             "nodes": {"root/node-b": {"summary": "b", "only_b": 2}},
             "unmapped_categories": ["cat-b"]}
    order_doc = {"last_updated": T_NEW, "total_entities": 1,
                 "nodes": {"root/node-a": {"summary": "a"}}}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


def _build_meta_json():
    doc_a = {"last_updated": T_OLD, "session_count": 5,
             "readiness_gates": {"gate_a": "a"}, "only_a": 1}
    doc_b = {"last_updated": T_NEW, "session_count": 7,
             "readiness_gates": {"gate_b": "b"}, "only_b": 2}
    order_doc = {"last_updated": T_NEW, "session_count": 5}
    return (_json_doc(doc_a), _json_doc(doc_b),
            _json_doc(order_doc), _json_doc(order_doc, reverse=True))


def _build_generic_yaml():
    doc_a = {"last_updated": T_OLD, "only_a": 1}
    doc_b = {"last_updated": T_NEW, "only_b": 2}
    order_doc = {"last_updated": T_NEW, "k": "v"}
    return (_yaml_doc(doc_a), _yaml_doc(doc_b),
            _yaml_doc(order_doc), _yaml_doc(order_doc, reverse=True))


# fn.__name__ -> builder. Unknown future handlers fall back by extension —
# the property assertions are shape-independent, so generic inputs still
# exercise commutativity on whatever fallback path the handler takes.
_BUILDERS_BY_FN = {
    "merge_reasoning_bank": _build_reasoning_bank,
    "merge_guardrails": _build_guardrails,
    "merge_pattern_signatures": _build_pattern_signatures,
    "merge_aspirations": _build_aspirations,
    "merge_pipeline": _build_pipeline,
    "merge_spark_questions": _build_spark_questions,
    "merge_team_state": _build_team_state,
    "merge_team_state_shard": _build_team_state,
    "merge_module_health": _build_module_health,
    "merge_forged_skills": _build_forged_skills,
    "merge_skill_relations": _build_skill_relations,
    "merge_outcome_metrics": _build_outcome_metrics,
    "merge_tree": _build_tree,
    "merge_aspirations_meta": _build_meta_json,
    "merge_pipeline_meta": _build_meta_json,
    "merge_append_only_jsonl": _build_generic_jsonl,
}


def _builder_for(basename: str, fn) -> "callable":
    b = _BUILDERS_BY_FN.get(fn.__name__)
    if b is not None:
        return b
    if basename.endswith(".jsonl"):
        return _build_generic_jsonl
    if basename.endswith(".json"):
        return _build_meta_json
    return _build_generic_yaml


def _handlers_under_test():
    """The LIVE registry + the shard handler (registered by path pattern, not
    basename — see test_coordination_merge.test_shard_dispatch_by_path_pattern)."""
    handlers = dict(cm._HANDLERS)
    shard_fn = getattr(cm, "merge_team_state_shard", None)
    if shard_fn is not None:
        handlers.setdefault("team-state/agents/<shard>.yaml", shard_fn)
    return handlers


def _assert_commutative(fn, a: bytes, b: bytes, basename: str, case: str):
    ab = fn(a, b)
    ba = fn(b, a)
    assert ab == ba, (
        f"{basename} ({fn.__name__}) NOT byte-commutative on {case}: "
        f"merge(a,b) != merge(b,a) — guard-907 violation.\n"
        f" merge(a,b)[:300]={ab[:300]!r}\n merge(b,a)[:300]={ba[:300]!r}")
    return ab


def test_divergent_keys_commutative():
    """(a) same-id records with DISTINCT new keys — the 1 shape."""
    failures = []
    for basename, fn in sorted(_handlers_under_test().items()):
        side_a, side_b, _, _ = _builder_for(basename, fn)()
        try:
            _assert_commutative(fn, side_a, side_b, basename, "divergent-keys")
        except AssertionError as e:
            failures.append(str(e))
    assert not failures, "\n\n".join(failures)


def test_serialization_order_commutative():
    """(b) same content, different serialization ORDER on the two sides."""
    failures = []
    for basename, fn in sorted(_handlers_under_test().items()):
        _, _, order_a, order_b = _builder_for(basename, fn)()
        try:
            _assert_commutative(fn, order_a, order_b, basename, "order-divergence")
        except AssertionError as e:
            failures.append(str(e))
    assert not failures, "\n\n".join(failures)


def test_multiround_settle_idempotent():
    """(c) merge(m, m) == m — a settled doc must not keep mutating (the
    fenced-PUT ping-pong shape is exactly a merge that never settles)."""
    failures = []
    for basename, fn in sorted(_handlers_under_test().items()):
        side_a, side_b, _, _ = _builder_for(basename, fn)()
        try:
            m = _assert_commutative(fn, side_a, side_b, basename, "settle-pre")
            mm = fn(m, m)
            assert mm == m, (
                f"{basename} ({fn.__name__}) NOT idempotent on settled doc: "
                f"merge(m,m) != m — multiround ping-pong hazard.\n"
                f" m[:300]={m[:300]!r}\n mm[:300]={mm[:300]!r}")
        except AssertionError as e:
            failures.append(str(e))
    assert not failures, "\n\n".join(failures)


def test_degenerate_inputs_commutative():
    """(d) empty-side degenerates (rb-2943 class, representable generically):
    (empty, empty) and (doc, empty) vs (empty, doc) must not crash and must
    stay byte-commutative."""
    failures = []
    for basename, fn in sorted(_handlers_under_test().items()):
        side_a, _, _, _ = _builder_for(basename, fn)()
        for case, a, b in (("empty-empty", b"", b""),
                           ("doc-vs-empty", side_a, b"")):
            try:
                _assert_commutative(fn, a, b, basename, case)
            except AssertionError as e:
                failures.append(str(e))
            except Exception as e:  # noqa: BLE001 — a crash on degenerates is a finding
                failures.append(
                    f"{basename} ({fn.__name__}) RAISED on {case}: "
                    f"{type(e).__name__}: {e}")
    assert not failures, "\n\n".join(failures)


def test_registry_nonempty_and_callable():
    """Sanity: the live registry has the known floor of handlers and every
    value is callable — guards against an import-order regression silently
    emptying _HANDLERS (which would make the property tests vacuous)."""
    handlers = _handlers_under_test()
    assert len(handlers) >= 15, f"registry suspiciously small: {len(handlers)}"
    assert all(callable(f) for f in handlers.values())


if __name__ == "__main__":
    for fn in (test_registry_nonempty_and_callable,
               test_divergent_keys_commutative,
               test_serialization_order_commutative,
               test_multiround_settle_idempotent,
               test_degenerate_inputs_commutative):
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll 5 property tests green over "
          f"{len(_handlers_under_test())} registered handler basenames "
          f"({len(set(f.__name__ for f in _handlers_under_test().values()))} distinct fns)")
