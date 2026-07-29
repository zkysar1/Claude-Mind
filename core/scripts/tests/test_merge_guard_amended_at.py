"""guard-1703: the guardrail content tiebreak must not revert an amendment.

WHAT WAS BROKEN (g-115-3662 item 2). ``_merge_guard_record`` resolved divergent
content fields with ``_canon(va) >= _canon(vb)`` — canonical-JSON BYTE ORDER,
which has no relation to which text is newer. Canonical JSON closes a string
with ``"`` (0x22), so an appended clause is compared character-for-character
against that quote: an addition starting with a SPACE (0x20) loses to the text
it extends, while one starting with a comma (0x2C) or hyphen (0x2D) wins. Which
box's edit survives a cross-box merge therefore turns on the first character of
the addition — something no author picks deliberately. Same class as guard-371
(space-vs-"T" byte ordering).

TWO MEASURED CORRECTIONS to how guard-1703 originally stated the defect, both
encoded as tests here so they cannot silently rot back into the prose:

  * ``rule`` is NOT exposed and never was. ``_guard_identity`` keys on
    (created, FULL rule), so records whose rule text differs are DIFFERENT
    identities that never reach the record merge — a divergent rule SPLITS into
    two records rather than losing one. ``test_divergent_rule_splits_rather_than_merging``
    pins that, because a future "simplification" of the identity key to a rule
    PREFIX would silently arm exactly the data loss this file exists to prevent.

  * "prefer the longer text" is NOT a valid fix, which is why the repair is a
    timestamp and not a length rule. Append and truncate produce the same
    strict-prefix relation in opposite directions, so a superset rule fixes the
    append case and BREAKS legitimate truncation.
    ``test_truncation_is_honored_when_it_is_the_newer_edit`` is the guard against
    someone "simplifying" the stamp away into a length comparison.

The fix is an explicit ordering key: ``amended_at``, stamped by the daemon's
set-field writer (StoreSpec.amend_stamp_field) and read here. Per rb-5493 a
merge-tier ordering field without a writer is a reader with no writer, so
``test_writer_stamps_amended_at_on_every_field_write`` asserts the writer half
against the real endpoint rather than trusting the reader alone.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))

import coordination_merge as cm  # noqa: E402


def _blob(records):
    return ("\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n").encode()


def _guard(**over):
    rec = {
        "id": "guard-900",
        "created": "2026-01-01T00:00:00",
        "rule": "Always probe before concluding",
        "category": "framework-architecture",
        "trigger_condition": "when about to conclude",
        "source": "g-test",
        "status": "active",
        "when_to_use": {},
        "utilization": {},
        "valid_from": "2026-01-01",
        "valid_to": None,
        "amended_at": None,
    }
    rec.update(over)
    return rec


def _merge(a, b):
    out = cm.merge_guardrails(_blob([a]), _blob([b]))
    return [json.loads(x) for x in out.decode().splitlines() if x.strip()]


def _merge_one(a, b):
    recs = _merge(a, b)
    assert len(recs) == 1, f"expected a single merged record, got {len(recs)}"
    return recs[0]


# --- the defect itself ------------------------------------------------------

# The space-first case is the one the ORIGINAL tiebreak got wrong; the
# comma-first case is one it happened to get right. Both are pinned, because a
# fix that only repairs the failing shape would leave the outcome still
# dependent on punctuation — the actual defect is that punctuation decides at all.
@pytest.mark.parametrize("suffix,label", [
    (" and also when a partner looks silent", "space-first (0x20 — LOST before the fix)"),
    (", and also when a partner looks silent", "comma-first (0x2C — won by luck before the fix)"),
    ("; see the negation protocol", "semicolon-first (0x3B)"),
])
def test_amendment_survives_regardless_of_leading_punctuation(suffix, label):
    base = "when about to conclude"
    old = _guard(trigger_condition=base, amended_at="2026-07-01T10:00:00")
    new = _guard(trigger_condition=base + suffix, amended_at="2026-07-20T10:00:00")

    assert _merge_one(old, new)["trigger_condition"] == base + suffix, label
    # Commutative: merge order must not decide the winner.
    assert _merge_one(new, old)["trigger_condition"] == base + suffix, label


def test_truncation_is_honored_when_it_is_the_newer_edit():
    """The reason the fix is a timestamp and not 'prefer longer'.

    A deliberate shortening is a strict PREFIX of the old text — structurally
    identical to an append, just with the arrow reversed. A length/superset rule
    would resolve this one backwards while appearing to fix the case above."""
    long_old = _guard(trigger_condition="when X happens and also Y",
                      amended_at="2026-07-01T10:00:00")
    short_new = _guard(trigger_condition="when X happens",
                       amended_at="2026-07-20T10:00:00")
    assert _merge_one(long_old, short_new)["trigger_condition"] == "when X happens"
    assert _merge_one(short_new, long_old)["trigger_condition"] == "when X happens"


@pytest.mark.parametrize("field", ["trigger_condition", "action_hint", "source"])
def test_every_exposed_content_field_follows_recency(field):
    """The measured exposure surface — NOT `rule` (see the split test below)."""
    old = _guard(**{field: "alpha", "amended_at": "2026-07-01T10:00:00"})
    new = _guard(**{field: "alpha beta", "amended_at": "2026-07-20T10:00:00"})
    assert _merge_one(old, new)[field] == "alpha beta"


# --- the two corrections to guard-1703's original statement -----------------

def test_divergent_rule_splits_rather_than_merging():
    """`rule` is immune because identity keys on the FULL rule text.

    This is a REGRESSION GUARD on the identity function, not on the merge: if
    _guard_identity is ever narrowed to a rule PREFIX, these two become one
    identity, the amendment tier starts deciding rule text, and the retired-twin
    collapse documented in the merge-identity tree node arms itself."""
    a = _guard(rule="Always probe before concluding")
    b = _guard(rule="Always probe before concluding, especially on negative claims")
    assert len(_merge(a, b)) == 2


# --- placement: recency must not outrank the semantic field rules -----------

def test_newer_amendment_does_not_resurrect_a_retired_guardrail():
    """`status` keeps retired-dominates ABOVE the recency tier.

    A retirement on box A must survive a newer, unrelated content edit on box B —
    otherwise the amendment tier becomes a silent un-retire, which is strictly
    worse than the bug it fixes."""
    retired_old = _guard(status="retired", amended_at="2026-07-01T10:00:00")
    active_new = _guard(status="active", trigger_condition="edited later",
                        amended_at="2026-07-20T10:00:00")
    merged = _merge_one(retired_old, active_new)
    assert merged["status"] == "retired"
    # ...while the newer CONTENT still wins, which is the whole point of the tier.
    assert merged["trigger_condition"] == "edited later"


def test_newer_amendment_does_not_regress_a_monotonic_counter():
    """`times_triggered` keeps MAX above the recency tier."""
    high_old = _guard(times_triggered=2066, amended_at="2026-07-01T10:00:00")
    low_new = _guard(times_triggered=3, amended_at="2026-07-20T10:00:00")
    assert _merge_one(high_old, low_new)["times_triggered"] == 2066


def test_utilization_counters_still_merge_by_max_not_recency():
    old = _guard(utilization={"times_helpful": 9}, amended_at="2026-07-01T10:00:00")
    new = _guard(utilization={"times_helpful": 1}, amended_at="2026-07-20T10:00:00")
    assert _merge_one(old, new)["utilization"]["times_helpful"] == 9


# --- graceful degradation on the un-migrated corpus -------------------------

def test_stamped_record_beats_unstamped_legacy_copy():
    """_ts_key(None) == "" sorts oldest, so the amended copy wins over an
    untouched legacy one. This is the entire migration story: 0 of the live
    records carried amended_at when the field was introduced."""
    legacy = _guard(trigger_condition="original")          # amended_at None
    amended = _guard(trigger_condition="original, extended",
                     amended_at="2026-07-20T10:00:00")
    assert _merge_one(legacy, amended)["trigger_condition"] == "original, extended"
    assert _merge_one(amended, legacy)["trigger_condition"] == "original, extended"


def test_two_unstamped_records_keep_pre_fix_behavior_exactly():
    """No stamps => the tier is inert and the generic tiebreak decides, byte-for-
    byte as before. The fix must not perturb records nothing has amended."""
    a = _guard(trigger_condition="aaa")
    b = _guard(trigger_condition="bbb")
    expected = "aaa" if cm._canon("aaa") >= cm._canon("bbb") else "bbb"
    assert _merge_one(a, b)["trigger_condition"] == expected


def test_merge_is_commutative_across_the_whole_record():
    a = _guard(trigger_condition="one", status="active", times_triggered=5,
               amended_at="2026-07-01T10:00:00")
    b = _guard(trigger_condition="one, two", status="retired", times_triggered=9,
               amended_at="2026-07-20T10:00:00")
    assert _merge_one(a, b) == _merge_one(b, a)


# --- the per-field redesign (g-115-3690) ------------------------------------
# THE CASE THIS FILE WAS MISSING. Every test above amends ONE field, and a
# record-level stamp is indistinguishable from a per-field stamp on a one-field
# edit — which is exactly why the suite went green on a real regression. The
# discriminating case needs TWO fields amended on TWO boxes.

def test_concurrent_amendments_to_different_fields_both_survive():
    """Box A amends trigger_condition; box B amends action_hint and never saw A.

    Under the RECORD-level stamp shipped by g-115-3662, B (the later WRITE) won
    EVERY content field, so A's trigger_condition amendment was deterministically
    discarded. Per-field stamps keep each box's own edit because each stamp is
    keyed by the field that mutation actually wrote (guard-1153)."""
    a = _guard(trigger_condition="when Z happens",
               amended_fields={"trigger_condition": "2026-07-28T10:00:00"})
    b = _guard(action_hint="do Y",
               amended_fields={"action_hint": "2026-07-28T11:00:00"})

    for first, second, label in ((a, b, "a<-b"), (b, a, "b<-a")):
        m = _merge_one(first, second)
        assert m["trigger_condition"] == "when Z happens", (
            f"{label}: box A's amendment was discarded by box B's later write — "
            f"the record-level regression is back")
        assert m["action_hint"] == "do Y", f"{label}: box B's amendment lost"


def test_same_field_concurrent_amendment_still_resolves_by_recency():
    """Per-field must not weaken the property the tier exists for: when BOTH
    boxes amend the SAME field, the later stamp still wins."""
    a = _guard(trigger_condition="when A happens and also B",
               amended_fields={"trigger_condition": "2026-07-28T10:00:00"})
    b = _guard(trigger_condition="when A happens, plus C",
               amended_fields={"trigger_condition": "2026-07-28T12:00:00"})
    for first, second in ((a, b), (b, a)):
        assert _merge_one(first, second)["trigger_condition"] == "when A happens, plus C"


def test_stamp_map_unions_rather_than_one_side_replacing_the_other():
    """The stamp map needs its OWN merge rule (guard-1153: no field in a record
    merge gets the opaque default). Left to the generic byte-order tiebreak, one
    box's whole map would replace the other's — losing the ordering evidence this
    tier reads, one level down. Disjoint keys must union; shared keys take MAX."""
    a = _guard(amended_fields={"trigger_condition": "2026-07-28T10:00:00",
                               "source": "2026-07-28T09:00:00"})
    b = _guard(amended_fields={"action_hint": "2026-07-28T11:00:00",
                               "source": "2026-07-28T13:00:00"})
    for first, second in ((a, b), (b, a)):
        m = _merge_one(first, second)["amended_fields"]
        assert set(m) == {"trigger_condition", "action_hint", "source"}, (
            f"stamp map did not union: {sorted(m)}")
        assert m["source"] == "2026-07-28T13:00:00", "shared key did not take MAX"


def test_legacy_scalar_is_read_as_a_per_field_floor():
    """Migration (g-115-3690 step 5): records written between d30d21bd and this
    fix carry the scalar and no map. Reading the scalar as a floor for every
    field keeps them ordering exactly as they did, instead of silently dropping
    to unstamped and regressing them to the byte-order tiebreak."""
    scalar_only = _guard(trigger_condition="when Z happens",
                         amended_at="2026-07-28T10:00:00")
    unstamped = _guard()
    for first, second in ((scalar_only, unstamped), (unstamped, scalar_only)):
        assert _merge_one(first, second)["trigger_condition"] == "when Z happens"


def test_per_field_stamp_outranks_the_legacy_scalar_on_that_field():
    """A record carrying BOTH shapes: the per-field key is the truth for the
    field it names; the scalar only fills in for fields the map omits."""
    newer_map = _guard(trigger_condition="from the map",
                       amended_at="2026-07-01T00:00:00",
                       amended_fields={"trigger_condition": "2026-07-28T20:00:00"})
    older = _guard(trigger_condition="from the scalar",
                   amended_at="2026-07-28T10:00:00")
    for first, second in ((newer_map, older), (older, newer_map)):
        assert _merge_one(first, second)["trigger_condition"] == "from the map"


def test_legacy_scalar_merges_by_max_not_byte_order():
    """_canon(None) is "null"; _canon("2026-…") opens with '"' (0x22 < 0x6E), so
    the generic tiebreak would let a NULL beat a real timestamp and erase the
    migration floor. The scalar needs an explicit MAX rule."""
    stamped = _guard(amended_at="2026-07-28T10:00:00")
    null_stamp = _guard(amended_at=None)
    for first, second in ((stamped, null_stamp), (null_stamp, stamped)):
        assert _merge_one(first, second)["amended_at"] == "2026-07-28T10:00:00"


def test_newer_amendment_still_does_not_resurrect_a_retired_guardrail_per_field():
    """The placement invariant survives the redesign: the per-field tier sits
    BELOW status/utilization/valid_to/monotonic, so a newer amendment to an
    UNRELATED field cannot un-retire a record."""
    retired = _guard(status="retired")
    live_amended = _guard(action_hint="do Y",
                          amended_fields={"action_hint": "2026-07-28T23:00:00"})
    for first, second in ((retired, live_amended), (live_amended, retired)):
        assert _merge_one(first, second)["status"] == "retired"


# --- the writer half (rb-5493: a reader with no writer is half a fix) -------

def test_amended_at_is_allowlisted_so_the_stamp_cannot_self_reject():
    """set_field stamps BEFORE it validates, so an unallowlisted stamp would make
    every guardrail field write fail — and the refusal would name a field the
    caller never passed (the misdirection documented in
    test_merge_stamped_fields_allowlist.py)."""
    sys.path.insert(0, str(REPO))
    from mind_api.src.store_registry import GUARD_KNOWN_FIELDS, STORE_REGISTRY
    spec = STORE_REGISTRY["guardrails"]
    assert spec.amend_stamp_field == "amended_fields"
    assert spec.amend_stamp_field in GUARD_KNOWN_FIELDS
    # The RETIRED scalar must stay allowlisted too: records written between
    # d30d21bd and g-115-3690 still carry it, and dropping it from the allowlist
    # would make every one of them self-reject on its next write.
    assert "amended_at" in GUARD_KNOWN_FIELDS


def test_stamp_is_declared_exactly_where_a_reader_exists():
    """set-field is a GENERIC 4-store endpoint. The stamp is spec-driven so each
    store opts in independently; a hardcoded store check or a blanket stamp would
    have changed all four.

    The invariant is the BICONDITIONAL, derived from source — a store declares
    ``amend_stamp_field`` IF AND ONLY IF its merge handler reads the stamp:
      * declared without a reader = a writer with no reader (rb-5493), the stamp
        is inert and the merge still resolves divergent text by byte order;
      * a reader without the declaration = a reader with no writer, equally inert,
        because nothing in production ever sets the field.

    Originally this pinned the literal set {guardrails}, which was a SNAPSHOT of
    the biconditional at a moment when guardrails held the only reader. g-115-3688
    added readers to _merge_rb_record and _merge_sig_record, and the snapshot form
    then failed on a CORRECT change while the property it meant to protect was
    still satisfied. Deriving both sides keeps rb-5493 enforced as the reader set
    grows, instead of re-pinning a set that must be hand-edited every time.
    """
    sys.path.insert(0, str(REPO))
    from mind_api.src.store_registry import STORE_REGISTRY

    # store -> the record-merge handler wired to it in coordination_merge.py.
    # Explicit because record_merge_fn is passed as a function VALUE, which static
    # analysis cannot follow (same reason test_merge_stamped_fields_allowlist.py
    # keeps an explicit scan set).
    STORE_HANDLER = {
        "guardrails": "_merge_guard_record",
        "reasoning-bank": "_merge_rb_record",
        "pattern-signatures": "_merge_sig_record",
        "spark-questions": "_merge_spark_record",
    }
    merge_tree = ast.parse(
        (REPO / "core" / "scripts" / "coordination_merge.py").read_text(encoding="utf-8"))
    readers = {
        node.name
        for node in ast.walk(merge_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        and sub.func.id == "_field_stamp"
    }
    assert readers, (
        "No handler calls _field_stamp in coordination_merge.py — the reader-side "
        "collector under-scanned, which would make the biconditional below "
        "vacuously demand that NO store declares the stamp.")

    for store, handler in sorted(STORE_HANDLER.items()):
        if store not in STORE_REGISTRY:
            continue
        declared = STORE_REGISTRY[store].amend_stamp_field is not None
        has_reader = handler in readers
        assert declared == has_reader, (
            f"{store}: StoreSpec.amend_stamp_field is "
            f"{'SET' if declared else 'None'} but {handler}() "
            f"{'DOES' if has_reader else 'does NOT'} read the per-field stamp. "
            f"Writer and reader must land together — either half alone is inert "
            f"(rb-5493). Fix whichever side is missing, or drop both.")


def test_writer_stamps_amended_at_on_every_field_write(tmp_path, monkeypatch):
    """Exercise the REAL set_field handler, not a reimplementation of it.

    Without this the reader could pass forever against hand-written fixtures
    while nothing in production ever set the field — the exact
    reader-with-no-writer shape rb-5493 names. Asserting only on spec wiring
    (`spec.amend_stamp_field == "amended_fields"`) is NOT sufficient: it proves
    the field is CONFIGURED, not that the handler reads it.

    Asserts the stamp is keyed BY THE WRITTEN FIELD (g-115-3690), which is the
    whole correctness property: a record-level stamp would make the newer write
    win every content field at merge time. Only-the-written-key is also asserted
    negatively — an unrelated field must NOT appear in the map, since that is
    exactly what would re-create the record-level semantics one layer down.

    The record deliberately starts WITHOUT the stamp field, because that is the
    state of live guardrails during migration.

    Auth and persistence are stubbed; the stamp line is what is under test. The
    absence of a raised validation error is itself an assertion: set_field stamps
    BEFORE it validates, so an unallowlisted stamp would fail here."""
    sys.path.insert(0, str(REPO))
    store_ep = pytest.importorskip("mind_api.src.endpoints.store")

    rec = _guard()
    rec.pop("amended_at", None)                 # legacy record: no stamp at all
    rec.pop("amended_fields", None)
    store_path = tmp_path / "guardrails.jsonl"
    store_path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    class _Paths:
        world = tmp_path
        meta = tmp_path
        agent = tmp_path

    class _Ctx:
        query = {"store": "guardrails", "id": rec["id"],
                 "field": "trigger_condition",
                 "value": "when about to conclude, extended"}
        headers = {"x-ayoai-agent": "alpha"}
        paths = _Paths()

    captured = {}
    monkeypatch.setattr(store_ep, "_require_agent_header", lambda ctx: None)
    monkeypatch.setattr(
        store_ep, "_commit",
        lambda ctx, spec, path, items, msg: captured.__setitem__("items", items))

    store_ep.set_field(_Ctx())

    written = captured["items"][0]
    stamps = written["amended_fields"]
    assert isinstance(stamps, dict), "set_field did not stamp amended_fields"
    assert stamps.get("trigger_condition"), "stamp not keyed by the written field"
    # ONLY the written field is stamped. A map that also dated an untouched field
    # would re-create the record-level semantics this redesign removes.
    assert set(stamps) == {"trigger_condition"}, (
        f"stamp leaked to fields this write did not touch: {sorted(stamps)}")
    assert written["trigger_condition"] == "when about to conclude, extended"
    # Stamp format must match `created` byte-for-byte in shape, since the merge
    # compares them as normalized strings.
    assert len(stamps["trigger_condition"]) == len("2026-01-01T00:00:00")
    assert stamps["trigger_condition"][10] == "T"


def test_writer_does_not_clobber_an_explicit_stamp_backfill(tmp_path, monkeypatch):
    """Setting the stamp field itself stays authoritative.

    A backfill or a correction writing amended_fields directly must not be
    silently overwritten with now() — otherwise the field could never be
    repaired."""
    sys.path.insert(0, str(REPO))
    store_ep = pytest.importorskip("mind_api.src.endpoints.store")

    rec = _guard()
    (tmp_path / "guardrails.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8")

    class _Paths:
        world = tmp_path
        meta = tmp_path
        agent = tmp_path

    class _Ctx:
        query = {"store": "guardrails", "id": rec["id"],
                 "field": "amended_fields",
                 "value": '{"trigger_condition": "2026-01-05T00:00:00"}'}
        headers = {"x-ayoai-agent": "alpha"}
        paths = _Paths()

    captured = {}
    monkeypatch.setattr(store_ep, "_require_agent_header", lambda ctx: None)
    monkeypatch.setattr(
        store_ep, "_commit",
        lambda ctx, spec, path, items, msg: captured.__setitem__("items", items))

    store_ep.set_field(_Ctx())
    assert captured["items"][0]["amended_fields"] == {
        "trigger_condition": "2026-01-05T00:00:00"}
