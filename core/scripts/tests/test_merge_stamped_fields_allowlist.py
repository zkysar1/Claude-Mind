"""Every field coordination_merge.py STAMPS onto a store's records must be in
that store's strict KNOWN_FIELDS allowlist in mind_api/src/store_registry.py.

WHY THIS IS A STATIC CHECK AND NOT A RUNTIME TEST (g-115-3662, third scope item).
The stamp that motivated this — ``displaced_from``, written by
``_merge_id_keyed_jsonl`` on the collision-reid path — lands ONLY on a record
that survived a cross-box id collision. No single-box test produces one, so the
full suite passed green for the entire time the field was missing from
GUARD_KNOWN_FIELDS. During that window the strict unknown-field gate refused
EVERY update to all 5 re-id'd guardrails (guard-1262, guard-1468, guard-1546,
guard-1570, guard-1697) — they could not be retired, amended, or corrected at
all. Test coverage is structurally incapable of catching this class; only a
static writer-vs-allowlist assertion is.

Two further properties this check is designed around, both from the incident:

  * The refusal names ``displaced_from`` — a field the CALLER never passed — so
    it reads as a caller bug and actively misdirects diagnosis toward the call
    site. ``_offenders`` therefore reports BOTH the writer that stamps the field
    AND the allowlist that lacks it.
  * Nothing pairs the writer with the validator: they sit in different layers
    (core/scripts vs mind_api/src) and neither references the other, so ADDING a
    stamp is a one-file change that silently breaks a different file. This test
    is the only thing that couples them.

Scan sets are EXPLICIT rather than derived from an AST call graph, because the
per-store record merge reaches ``_merge_id_keyed_jsonl`` through a
function-VALUED parameter (``record_merge_fn=``) that static analysis cannot
follow. An explicit list is auditable and cannot silently under-scan; adding a
new id-keyed store means adding one entry here.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MERGE_PY = REPO / "core" / "scripts" / "coordination_merge.py"

from mind_api.src.store_registry import GUARD_KNOWN_FIELDS  # noqa: E402

# store label -> (allowlist, allowlist symbol name, functions that build its
#                 records, {field: minimum number of stamp SITES})
#
# The last element is a coverage floor. Without it the collector can UNDER-scan
# silently: RECORD_VARS and the function list are both hand-maintained, so renaming
# one record variable (rec -> record) or one function drops its stamps from the scan
# while the "found no stamps at all" assertion below still passes on whatever
# survives. Partial under-scan reads as a clean check — the same
# narrower-than-it-claims shape this whole check exists to catch, reproduced inside
# the checker.
#
# It counts SITES, not fields, and that distinction was forced by measurement. The
# first version of this floor pinned the field SET {"id", "displaced_from"} and was
# falsified by mutation: dropping "rec" from RECORD_VARS still finds both fields,
# because merged["displaced_from"] and kept["id"] stamp them from other variables.
# A field set is insensitive to losing a scan variable; a site count is not
# (4 sites -> 2 under that same mutation).
#
# Site counts are deliberately brittle to refactors of the stamping code. A failure
# reading "site count dropped, confirm intentional and update the floor" is the
# desired outcome on any edit to merge-stamping — that surface is exactly where a
# silent change costs 5 un-updatable live records.

STORE_SCAN = {
    "guardrails.jsonl": (
        GUARD_KNOWN_FIELDS,
        "GUARD_KNOWN_FIELDS",
        (
            "merge_guardrails",        # entry point
            "_merge_id_keyed_jsonl",   # shared union/collision-reid machinery
            "_merge_guard_record",     # per-record field merge (passed as record_merge_fn)
        ),
        {"id": 3, "displaced_from": 2},
    ),
}


# Variable names that hold a RECORD in coordination_merge.py's id-keyed path.
# The collector is scoped to these because the same file also writes string
# literal keys into internal BOOKKEEPING dicts — ``g["rec"] = merged`` in
# _merge_id_keyed_jsonl writes the groups accumulator, not a record field. That
# false positive was produced by this test's own first run and is the reason the
# scope is explicit: a check that cries wolf about 'rec' being an unallowlisted
# guardrail field teaches the reader to ignore it, which costs more than the gap
# it was written to catch.
RECORD_VARS = frozenset({"rec", "out", "merged", "kept"})


def _literal_key_stamps(tree, func_names):
    """Collect {field: [where...]} for every ``<record>["literal"] = ...`` inside
    the named module-level functions, where ``<record>`` is a RECORD_VARS name.
    A subscript assignment with a STRING literal key is a stamp — the writer
    deciding a field exists on the record. Variable-key writes (``out[k] = v``,
    the pass-through field merge) carry no literal and are not stamps."""
    stamps = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in func_names:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if not isinstance(tgt, ast.Subscript):
                    continue
                if not (isinstance(tgt.value, ast.Name)
                        and tgt.value.id in RECORD_VARS):
                    continue
                key = tgt.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    stamps.setdefault(key.value, []).append(
                        f"{node.name}() line {sub.lineno}")
    return stamps


def _offenders(allowlist, allowlist_name, stamps):
    """Fields stamped by the writer but absent from the allowlist, rendered so
    the message names BOTH sides — the misdirection the incident turned on."""
    return [
        f"{field!r} is STAMPED by coordination_merge.py "
        f"({'; '.join(where)}) but is NOT in {allowlist_name} "
        f"(mind_api/src/store_registry.py). The strict unknown-field gate will "
        f"refuse EVERY update to any record carrying it — and the refusal names "
        f"{field!r}, a field the caller never passed, so it reads as a caller bug. "
        f"Fix: add {field!r} to {allowlist_name} with a source-writer citation."
        for field, where in sorted(stamps.items())
        if field not in allowlist
    ]


@pytest.fixture(scope="module")
def merge_tree():
    return ast.parse(MERGE_PY.read_text(encoding="utf-8"))


@pytest.mark.parametrize("store", sorted(STORE_SCAN))
def test_allowlist_is_superset_of_merge_stamped_fields(store, merge_tree):
    """Superset, NOT equality: an allowlist legitimately carries fields no merge
    writer stamps (everything the normal mint/update path writes)."""
    allowlist, allowlist_name, funcs, floor = STORE_SCAN[store]
    stamps = _literal_key_stamps(merge_tree, funcs)
    assert stamps, (
        f"scanned {funcs} in coordination_merge.py and found NO literal-key "
        f"stamps at all — the scan set is stale (functions renamed?), so this "
        f"check is measuring nothing")
    thin = {f: (len(stamps.get(f, [])), n) for f, n in floor.items()
            if len(stamps.get(f, [])) < n}
    assert not thin, (
        f"{store}: stamp-site coverage dropped — {thin} (found, expected-min). "
        f"Either a scanned function or a RECORD_VARS name was renamed (silent "
        f"UNDER-scan: the check now measures less than it claims), or stamping was "
        f"deliberately refactored (then update the floor in STORE_SCAN). "
        f"Scanned {funcs}; found "
        f"{ {f: len(w) for f, w in sorted(stamps.items())} }")
    bad = _offenders(allowlist, allowlist_name, stamps)
    assert not bad, f"{store}: " + " | ".join(bad)


def test_the_check_itself_goes_red_when_the_known_gap_is_reintroduced(merge_tree):
    """Validate the detector, not just the code under it.

    Re-creates the exact g-115-3657 gap — ``displaced_from`` absent from the
    guardrail allowlist — against a COPY of the allowlist, and asserts the
    checker reports it. A check that passes both with and without the fix is
    measuring nothing (same discipline as the mutation testing in g-115-3655
    that caught a cell-0-only false fix)."""
    allowlist, allowlist_name, funcs, _floor = STORE_SCAN["guardrails.jsonl"]
    stamps = _literal_key_stamps(merge_tree, funcs)
    assert "displaced_from" in stamps, (
        "the collision-reid path no longer stamps displaced_from — if that is "
        "intentional, retire this regression test along with the writer")

    without = set(allowlist) - {"displaced_from"}
    bad = _offenders(without, allowlist_name, stamps)
    assert any("displaced_from" in msg for msg in bad), (
        "detector FAILED to flag a reintroduced displaced_from gap")
    # And the message must name both sides, since naming only the field is what
    # sent the original diagnosis to the wrong file.
    msg = next(m for m in bad if "displaced_from" in m)
    assert "coordination_merge.py" in msg and allowlist_name in msg
