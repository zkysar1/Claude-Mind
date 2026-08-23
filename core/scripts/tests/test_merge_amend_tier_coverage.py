"""Every id-keyed store's record-merge handler must carry an amendment-ordering
key AT ITS OWN MERGE GRANULARITY — or be explicitly EXEMPT with a measured reason.

WHY THIS IS A STATIC CHECK AND NOT A RUNTIME TEST (g-115-3688, second scope item).
The defect only manifests when two boxes hold DIVERGENT copies of one record, which
no single-box test produces. The full suite passed green for the entire time the
reasoning-bank defect was live: merging content='base text' against 'base text and
more' silently returned 'base text', reverting an amendment to the store's PRIMARY
payload field. Test coverage is structurally incapable of catching this class; only
a static per-handler assertion is.

WHY GRANULARITY AND NOT MERE TIER PRESENCE (the g-115-3690 correction). The first
version of this rule — "assert each handler applies an explicit ordering tier before
the generic content tiebreak" — CANNOT catch the defect g-115-3690 fixed. That defect
was a record-level ``amended_at`` scalar inside a field-by-field merge: the right tier
at the WRONG GRANULARITY, which a presence check passes on. A record-level key applied
per-field silently means "the newer WRITE wins EVERY content field", so a concurrent
amendment to a DIFFERENT field of the same record is deterministically discarded.

So the assertion is a MATCH, not a presence:

  * a FIELD-WISE handler (``for k, vb in b.items(): ... out[k] = ...``) must consult a
    PER-FIELD stamp — a ``_field_stamp(<rec>, <loop key var>)`` call inside that loop.
  * a WHOLE-RECORD-WINS handler (``return a if aa > ab else b``) may use a
    record-level key; per-field would be meaningless there.

A check that would have passed on the live defect measures nothing — the same
"validate the detector" discipline the goal demanded, applied to the check itself.
``test_granularity_check_rejects_a_record_level_scalar`` below pins it directly by
running the checker against a synthetic record-level handler and asserting it FAILS.

WHY THE WRITER/READER LINKAGE ASSERTION EXISTS (Refinement B). The amend-stamp
mechanism is spelled by TWO INDEPENDENT STRING LITERALS on opposite sides of a
boundary that forbids sharing one:

    reader (Layer 1): core/scripts/coordination_merge.py  _AMEND_STAMP_FIELD
    writer (Layer 2): mind_api/src/store_registry.py      StoreSpec.amend_stamp_field

Nothing else asserts they are EQUAL. Rename one side and the mechanism dies silently
— the writer stamps a key the merge never reads, the merge reads a key nothing writes
(the reader-with-no-writer shape rb-5493 names) — while every existing test still
passes, because each side pins its own literal independently. The duplication is
STRUCTURALLY FORCED, not sloppiness: a pre-commit gate enforces Layer-1 -> Layer-2
references == 0, and coordination_merge.py references mind_api only in comments. So
the fix must be an assertion that SPANS the boundary, never a refactor that collapses
it. A test may span it (this file's sibling
``test_merge_stamped_fields_allowlist.py`` already imports Layer 2 the same way);
the two PRODUCTION modules still share nothing.

Scan sets are EXPLICIT rather than derived from an AST call graph, because the
per-store record merge reaches ``_merge_id_keyed_jsonl`` through a function-VALUED
parameter (``record_merge_fn=``) that static analysis cannot follow. An explicit list
is auditable — and ``test_scan_set_covers_every_id_keyed_store`` re-derives the id-keyed
call sites from the source so the list cannot silently UNDER-scan, which is the
narrower-than-it-claims shape this whole check exists to catch.
"""
import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MERGE_PY = REPO / "core" / "scripts" / "coordination_merge.py"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from mind_api.src.store_registry import STORE_REGISTRY  # noqa: E402

# Disposition of every id-keyed record-merge handler.
#   COVERED — carries an ordering key at its own granularity.
#   EXEMPT  — measured to have NO amendable free-text non-identity field, so no
#             ordering key is needed. The reason is required and must name the
#             MEASUREMENT, not an assumption.
#
# handler -> (store label, "field-wise" | "whole-record", disposition, reason)
HANDLER_DISPOSITION = {
    "_merge_rb_record": (
        "reasoning-bank", "field-wise", "COVERED",
        "g-115-3688. _rb_identity keys on (created, TITLE), so `content` — the "
        "PRIMARY payload — is a mutable non-identity field. Measured pre-fix: "
        "'base text' vs 'base text and more' merged to 'base text'.",
    ),
    "_merge_guard_record": (
        "guardrails", "field-wise", "COVERED",
        "g-115-3662 (tier) + g-115-3690 (per-field correction). _guard_identity "
        "keys on (created, FULL rule), so `rule` is immune — a divergent rule "
        "SPLITS. Exposed fields are trigger_condition / action_hint / source.",
    ),
    "_merge_sig_record": (
        "pattern-signatures", "field-wise", "COVERED",
        "g-115-3688. _sig_identity keys on (created, name), so `name` is immune. "
        "Measured on a live record: `description` and `expected_outcome` are "
        "amendable non-identity free-text and reverted under the byte tiebreak.",
    ),
    "_merge_spark_record": (
        "spark-questions", "whole-record", "EXEMPT",
        "Measured on a live record (meta/spark-questions.jsonl), NOT assumed: the "
        "ONLY long free-text field is `text`, and _spark_identity IS `text`. A "
        "divergent text SPLITS into two records rather than merging, so no "
        "amendable non-identity free-text field exists. Every remaining field is "
        "an id, an explicitly-handled counter, a derived rate, or a short enum. "
        "The handler is also whole-record-wins (`win, lose = (a, b) if _canon(a) "
        ">= _canon(b) ...`), so a per-field key would be meaningless here.",
    ),
    "_merge_tree_node": (
        "_tree.yaml", "field-wise", "COVERED",
        "g-115-5411. BASE is the DEFAULT class of _classify_tree_field (a TOTAL "
        "function), so summary / entities / saturated_topics / maintain_exempt / "
        "origin_goal_id / valid_from / domain_class and every field added later "
        "rode the newer-last_updated LWW base -- and last_updated is DATE-granular "
        "by design (g-001-67; g-115-1683 does not bump it on a field poke), so "
        "same-day edits ALWAYS tied and fell to the lexicographic content "
        "tiebreak. Measured live, not assumed: a saturated_topics narrowing was "
        "re-derived away by every merge cycle, a 1953-char replacement losing to a "
        "1942-char incumbent at list index 1 ('c' > 'a'), in BOTH arg orders. "
        "Unlike the four stores above this one has no StoreSpec -- _tree.yaml is a "
        "YAML tree written by tree_write.py, not an id-keyed JSONL record store -- "
        "so its Layer-2 writer is hand-written in BOTH tree.py cmd_set and "
        "mind_api/src/world/tree_write.py _apply_set (byte-compat mirrors), and "
        "the stamp is SECOND-granular there because a date-granular one would "
        "reproduce the very tie it exists to break.",
    ),
}

# The Layer-1 reader constant. Read from source rather than imported so a rename
# in coordination_merge.py surfaces HERE as well as in the linkage test.
LAYER1_STAMP_CONST = "_AMEND_STAMP_FIELD"


@pytest.fixture(scope="module")
def merge_src():
    return MERGE_PY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def merge_tree(merge_src):
    return ast.parse(merge_src)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _field_loops(fn):
    """Per-key merge loops in a handler: ``for <k>, <v> in <x>.items():``.

    Returns [(loop_node, key_var_name)]. A handler with at least one such loop
    that also assigns ``out[<k>]`` is FIELD-WISE — it decides each field
    independently, which is exactly what makes a record-level ordering key wrong.
    """
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.For):
            continue
        tgt = node.target
        if not (isinstance(tgt, ast.Tuple) and len(tgt.elts) == 2
                and all(isinstance(e, ast.Name) for e in tgt.elts)):
            continue
        it = node.iter
        if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "items"):
            continue
        key_var = tgt.elts[0].id
        assigns_by_key = any(
            isinstance(sub, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Name)
                    and t.slice.id == key_var
                    for t in sub.targets)
            for sub in ast.walk(node)
        )
        if assigns_by_key:
            found.append((node, key_var))
    return found


def _per_field_stamp_calls(loop, key_var):
    """``_field_stamp(<anything>, <key_var>)`` calls inside this per-key loop.

    The SECOND argument being the loop's key variable is the whole assertion: it
    is what distinguishes a per-field ordering key from a record-level one. The
    retired scalar design computed its winner ONCE before the loop and therefore
    produces zero hits here — which is why this check, unlike a tier-PRESENCE
    check, would have failed on the live g-115-3690 defect.
    """
    hits = []
    for sub in ast.walk(loop):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == "_field_stamp"):
            continue
        if len(sub.args) == 2 and isinstance(sub.args[1], ast.Name) \
                and sub.args[1].id == key_var:
            hits.append(sub.lineno)
    return hits


def _granularity_offender(fn, handler, store, declared_granularity):
    """None when the handler's ordering key matches its merge granularity, else a
    message naming BOTH the granularity observed in the code and the key found."""
    loops = _field_loops(fn)
    observed = "field-wise" if loops else "whole-record"

    if observed != declared_granularity:
        return (
            f"{handler}() ({store}): HANDLER_DISPOSITION declares granularity "
            f"{declared_granularity!r} but the AST shows {observed!r}. The declared "
            f"granularity decides which ordering key is correct, so a stale "
            f"declaration silently changes what this check enforces. Re-read the "
            f"handler and update the table."
        )

    if observed == "whole-record":
        return None  # a record-level key is correct here; nothing per-field to assert

    stamped = [ln for loop, kv in loops for ln in _per_field_stamp_calls(loop, kv)]
    if stamped:
        return None

    return (
        f"{handler}() ({store}) merges FIELD BY FIELD but consults no PER-FIELD "
        f"ordering key: no `_field_stamp(<rec>, <loop key var>)` call appears inside "
        f"its per-key loop. Its generic `out[k] = va if _canon(va) >= _canon(vb) else "
        f"vb` branch therefore resolves divergent text by BYTE ORDER, which is "
        f"unrelated to recency — canonical JSON closes a string with '\"' (0x22), so "
        f"an amendment appended after a space (0x20) LOSES to the text it extends "
        f"while one appended after a comma (0x2C) wins. "
        f"FIX (reader, Layer 1 — core/scripts/coordination_merge.py): add the "
        f"{LAYER1_STAMP_CONST} map branch, the _AMEND_LEGACY_FIELD MAX branch, and a "
        f"`_field_stamp(a, k) / _field_stamp(b, k)` tier in the generic else, strictly "
        f"BELOW that handler's semantic tiers (status / counters / monotonic dates) so "
        f"recency can never un-retire a record or regress a counter. "
        f"FIX (writer, Layer 2 — mind_api/src/store_registry.py): add the stamp field "
        f"to this store's *_DEFAULT_FIELDS and set amend_stamp_field on its StoreSpec. "
        f"A record-level scalar is NOT an acceptable substitute here — inside a "
        f"per-field merge it discards concurrent amendments to other fields "
        f"(g-115-3690). If the store genuinely has no amendable non-identity free-text "
        f"field, MEASURE that on a live record and move it to EXEMPT with the evidence."
    )


@pytest.mark.parametrize("handler", sorted(HANDLER_DISPOSITION))
def test_ordering_key_matches_merge_granularity(handler, merge_tree):
    """Refinement A: assert granularity MATCH, never mere tier presence."""
    store, granularity, disposition, reason = HANDLER_DISPOSITION[handler]
    fn = _func(merge_tree, handler)
    assert fn is not None, (
        f"{handler}() is in HANDLER_DISPOSITION but no longer exists in "
        f"{MERGE_PY.relative_to(REPO)}. Renaming a handler without updating this "
        f"table silently drops {store} from the scan — the under-scan-reads-as-clean "
        f"shape this check exists to catch."
    )
    assert reason.strip(), f"{handler}: every disposition needs a reason naming the measurement."

    if disposition == "EXEMPT":
        assert granularity == "whole-record" or "SPLIT" in reason.upper(), (
            f"{handler}() ({store}) is EXEMPT but is field-wise and its reason does "
            f"not explain why divergence SPLITS instead of merging. An exemption must "
            f"rest on a measured absence of exposure, not on the fix being unfinished "
            f"— use a tracking goal for the latter, never EXEMPT."
        )
        return

    offender = _granularity_offender(fn, handler, store, granularity)
    assert offender is None, offender


def test_granularity_check_rejects_a_record_level_scalar():
    """VALIDATE THE DETECTOR: a check that passes on the live defect measures nothing.

    Rebuilds the retired g-115-3662 shape — a record-level winner computed ONCE
    before the per-field loop — and asserts this checker REJECTS it. Without this,
    the granularity assertion could silently degrade to a presence check.
    """
    record_level = ast.parse(
        "def _merge_x_record(a, b):\n"
        "    amend_winner = a if _ts_key(a.get('amended_at')) >= _ts_key(b.get('amended_at')) else b\n"
        "    out = dict(a)\n"
        "    for k, vb in b.items():\n"
        "        va = out[k]\n"
        "        out[k] = amend_winner[k]\n"
        "    return out\n"
    )
    fn = _func(record_level, "_merge_x_record")
    offender = _granularity_offender(fn, "_merge_x_record", "synthetic", "field-wise")
    assert offender is not None, (
        "The record-level scalar shape PASSED the granularity check. That is the "
        "exact defect g-115-3690 fixed, so this checker is measuring nothing."
    )
    assert "PER-FIELD" in offender

    per_field = ast.parse(
        "def _merge_y_record(a, b):\n"
        "    out = dict(a)\n"
        "    for k, vb in b.items():\n"
        "        va = out[k]\n"
        "        sa, sb = _field_stamp(a, k), _field_stamp(b, k)\n"
        "        out[k] = va if sa > sb else vb\n"
        "    return out\n"
    )
    fn2 = _func(per_field, "_merge_y_record")
    assert _granularity_offender(fn2, "_merge_y_record", "synthetic", "field-wise") is None, (
        "The per-field shape was REJECTED — the checker is inverted."
    )


def test_scan_set_covers_every_id_keyed_store(merge_tree):
    """The explicit table cannot silently under-scan.

    Re-derives every ``record_merge_fn=`` handler passed to ``_merge_id_keyed_jsonl``
    from the source and asserts the table covers each one. Adding a fourth id-keyed
    store means adding a row here — it cannot inherit a clean result by omission.
    """
    referenced = set()
    for node in ast.walk(merge_tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_merge_id_keyed_jsonl"):
            continue
        for kw in node.keywords:
            if kw.arg == "record_merge_fn" and isinstance(kw.value, ast.Name):
                referenced.add(kw.value.id)

    assert referenced, (
        "Found no `record_merge_fn=` handlers passed to _merge_id_keyed_jsonl. The "
        "collector under-scanned — an empty result would make every assertion below "
        "vacuously true."
    )
    missing = sorted(referenced - set(HANDLER_DISPOSITION))
    assert not missing, (
        f"id-keyed handler(s) {missing} are wired into _merge_id_keyed_jsonl in "
        f"{MERGE_PY.relative_to(REPO)} but carry no HANDLER_DISPOSITION row, so this "
        f"check never inspects them. Add a row: COVERED (per-field tier present), or "
        f"EXEMPT with the live-record measurement showing no amendable non-identity "
        f"free-text field."
    )


def _writer_specs():
    return {n: s for n, s in STORE_REGISTRY.items()
            if getattr(s, "amend_stamp_field", None)}


def test_at_least_one_writer_spec_declares_the_stamp_field():
    """Guards the two tests below from passing vacuously on an empty spec set."""
    assert _writer_specs(), (
        "No StoreSpec declares amend_stamp_field, so the allowlist-flow and linkage "
        "assertions below would pass on an empty loop. Either the field was renamed "
        "in mind_api/src/store_registry.py, or STORE_REGISTRY was restructured."
    )


@pytest.mark.parametrize("store", sorted(_writer_specs()))
def test_amend_stamp_field_flows_into_the_allowlist(store):
    """Refinement B(a): ALLOWLIST-FLOW. Load-bearing and previously unguarded.

    ``set_field`` STAMPS before it VALIDATES, so a stamp field missing from the
    store's default fields (which flow into the strict allowlist) makes the store
    self-reject EVERY write — the writer poisons the very record it is updating.
    """
    spec = STORE_REGISTRY[store]
    field = spec.amend_stamp_field
    defaults = getattr(spec, "default_fields", None) or {}
    assert field in defaults, (
        f"StoreSpec {store!r} declares amend_stamp_field={field!r} but {field!r} is "
        f"NOT in its default_fields (mind_api/src/store_registry.py). Default fields "
        f"flow into the strict unknown-field allowlist automatically, and set_field "
        f"stamps BEFORE it validates — so every write to {store!r} would be refused, "
        f"naming a field the caller never passed. Fix: add {field!r} to that store's "
        f"*_DEFAULT_FIELDS."
    )


@pytest.mark.parametrize("store", sorted(_writer_specs()))
def test_writer_and_reader_name_the_same_stamp_field(store, merge_src):
    """Refinement B(b): LINKAGE across the Layer-1 / Layer-2 boundary.

    Rename either side and the mechanism dies silently while every other test stays
    green, because each side pins its own literal independently. Nothing but this
    assertion couples them — and nothing CAN, short of a test: the pre-commit gate
    forbids a Layer-1 -> Layer-2 reference, so no shared constant is available.
    """
    writer = STORE_REGISTRY[store].amend_stamp_field

    reader = None
    for node in ast.walk(ast.parse(merge_src)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == LAYER1_STAMP_CONST \
                and isinstance(node.value, ast.Constant):
            reader = node.value.value
    assert reader is not None, (
        f"{LAYER1_STAMP_CONST} is no longer a module-level string constant in "
        f"{MERGE_PY.relative_to(REPO)}. That constant is the READER half of the "
        f"amend-stamp mechanism; without it the linkage cannot be asserted at all."
    )
    assert reader == writer, (
        f"AMEND-STAMP LINKAGE BROKEN for store {store!r} — the two halves name "
        f"DIFFERENT fields:\n"
        f"  reader (Layer 1) core/scripts/coordination_merge.py {LAYER1_STAMP_CONST} "
        f"= {reader!r}\n"
        f"  writer (Layer 2) mind_api/src/store_registry.py StoreSpec({store!r})."
        f"amend_stamp_field = {writer!r}\n"
        f"The writer stamps a key the merge never reads and the merge reads a key "
        f"nothing writes, so the amendment-ordering tier is inert while every "
        f"per-side test still passes. Fix whichever side was renamed — do NOT "
        f"collapse them into a shared constant: the pre-commit gate enforces "
        f"Layer-1 -> Layer-2 references == 0, so this assertion is the only "
        f"available coupling."
    )
