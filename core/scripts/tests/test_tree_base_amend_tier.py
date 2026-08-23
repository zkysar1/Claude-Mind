"""test_tree_base_amend_tier.py — , the BASE-class per-field
amendment tier on `_merge_tree_node`, and the two writers that feed it.

THE DEFECT. `_classify_tree_field` is a TOTAL function whose default is BASE, so
`summary`, `entities`, `saturated_topics`, `maintain_exempt`, `origin_goal_id`,
`valid_from`, `domain_class` — and every per-node field added later — rode the
newer-`last_updated` LWW base. `last_updated` is DATE-granular by design
(g-001-67 is its SSOT; g-115-1683 deliberately does not bump it on a field poke),
so two edits on the SAME DAY can never be ordered by recency and `_order_by_ts`
always falls through to its LEXICOGRAPHIC content tiebreak.

That tiebreak's winner is arbitrary with respect to intent AND is a stable FIXED
POINT: every merge re-derives the same winner, re-pushes it to the authoritative
store, and the next attempt meets it again. Measured on the filing (g-115-5411):
two live write attempts plus an offline simulation over the real bytes in BOTH
arg orders, all losing to the incumbent — a 1953-char replacement beaten by a
1942-char one because the first divergent character sat at list index 1 ('c' >
'a'). So for `_tree.yaml` BASE fields the tie was not a corner case; it was the
NORMAL condition for same-day work, and an edit could never land.

WHY THE FIX IS THE SHARED PER-FIELD TIER AND NOT THE GOAL'S OWN SUGGESTION. The
goal recommended keying BASE LWW on a dedicated node-level sub-day stamp "the way
PROGRESSION already does". That is the guard-1153 trap, and a prior unit of this
goal declined it for that reason. PROGRESSION governs three tightly-coupled
fields one writer bumps together, so a record-level stamp is the same-mutation
timestamp for all of them. BASE is the open-ended DEFAULT class, so a node-level
stamp would be a FOREIGN timestamp for every BASE field except the one just
written — degrading to newer-write-wins-everything and deterministically
discarding concurrent amendments to the others (g-115-3690). The tier here is the
one `_merge_rb_record` / `_merge_guard_record` / `_merge_sig_record` already use,
via the generic `_AMEND_STAMP_FIELD` / `_field_stamp` / `_merge_stamp_map`
helpers — adopting the shared cure rather than adding a fourth local fix.

BACKFILL SAFETY IS PINNED, NOT ASSUMED (`test_unstamped_both_sides_*`): with no
stamps on either side both `_field_stamp` calls return `""`, the sides tie, and
the base-pick value survives — byte-identical to the pre-change merge for every
node in the live tree until writers start stamping.

ANTI-VACUITY. `test_tier_discriminates_rather_than_always_picking_a_side` proves
the tier actually SELECTS; without it, every "newer wins" assertion below would
also pass against a merge that simply always returned `a`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "cm_base_amend", CORE_SCRIPTS / "coordination_merge.py")
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)

_tree_spec = importlib.util.spec_from_file_location(
    "tree_cli_base_amend", CORE_SCRIPTS / "tree.py")
tree_cli = importlib.util.module_from_spec(_tree_spec)
_tree_spec.loader.exec_module(tree_cli)

DAEMON_WRITER = CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "world" / "tree_write.py"

OLD = "2026-08-21T09:00:00"
NEW = "2026-08-21T17:30:00"
# Same DAY on both sides — the whole point. If these differed the NEWER-class
# last_updated merge would order the sides and the tier would never be reached.
DAY = "2026-08-21"


def _node(**kw):
    n = {"last_updated": DAY, "summary": "unset"}
    n.update(kw)
    return n


# --- the regression this goal exists for -----------------------------------

def test_stamped_base_field_beats_unstamped_stale_copy():
    """The measured live case: this box edits, the peer holds a stale unstamped
    copy. Before the tier the winner was decided by byte order and the edit lost
    forever. Both arg orders, because a merge that only works one way is not a
    merge — each box runs it with its own copy first.
    """
    mine = _node(summary="NARROWED", amended_fields={"summary": NEW})
    peer = _node(summary="stale wide value")
    assert cm._merge_tree_node(mine, peer)["summary"] == "NARROWED"
    assert cm._merge_tree_node(peer, mine)["summary"] == "NARROWED"


def test_newer_stamp_wins_between_two_stamped_copies():
    a = _node(summary="older edit", amended_fields={"summary": OLD})
    b = _node(summary="newer edit", amended_fields={"summary": NEW})
    assert cm._merge_tree_node(a, b)["summary"] == "newer edit"
    assert cm._merge_tree_node(b, a)["summary"] == "newer edit"


def test_lexicographically_losing_text_still_wins_when_it_is_newer():
    """The exact shape from the filing: the intended value SORTS LOWER than the
    incumbent, so under the content tiebreak it could never land however many
    times it was retried. 'a' < 'c', and the newer stamp must override that.
    """
    incumbent = _node(summary="cost add next environment")
    replacement = _node(summary="adapter environment cost",
                        amended_fields={"summary": NEW})
    assert cm._canon("adapter environment cost") < cm._canon("cost add next environment"), (
        "fixture no longer reproduces the adverse ordering — pick text that "
        "LOSES the byte tiebreak, or this test passes for the wrong reason")
    assert cm._merge_tree_node(incumbent, replacement)["summary"] == "adapter environment cost"
    assert cm._merge_tree_node(replacement, incumbent)["summary"] == "adapter environment cost"


# --- backfill safety --------------------------------------------------------

def test_unstamped_both_sides_falls_through_to_content_tiebreak_unchanged():
    """Every node in the live tree is unstamped today. The tier must be inert
    for them — not merely 'usually right', but byte-identical to the old result.
    """
    a = _node(summary="A text")
    b = _node(summary="B text")
    expected = a if cm._canon("A text") >= cm._canon("B text") else b
    assert cm._merge_tree_node(a, b)["summary"] == expected["summary"]
    assert cm._merge_tree_node(b, a)["summary"] == expected["summary"]


# --- the tier must not outrank the semantic classes above it ----------------

def test_recency_never_regresses_a_max_counter():
    """MAX runs above the tier. A newer amendment carrying a LOWER counter must
    not drag it down — that would silently discard peer retrievals.
    """
    a = _node(retrieval_count=100)
    b = _node(retrieval_count=3, amended_fields={"retrieval_count": NEW})
    assert cm._merge_tree_node(a, b)["retrieval_count"] == 100
    assert cm._merge_tree_node(b, a)["retrieval_count"] == 100


def test_recency_never_overrides_a_calibration_value():
    """CALIBRATION keys on its own dedicated stamp. A BASE-tier stamp must not
    reach `accuracy`, or an unrelated summary edit could revert a data-derived
    recalibration (the g-115-5856 defect, one class over).
    """
    a = _node(accuracy=0.9, calibration_updated_at="2026-08-21")
    b = _node(accuracy=0.4, calibration_updated_at="2026-08-20",
              amended_fields={"accuracy": NEW})
    assert cm._merge_tree_node(a, b)["accuracy"] == 0.9
    assert cm._merge_tree_node(b, a)["accuracy"] == 0.9


# --- guard-1153 addition (2): the metadata field is ITSELF merged ------------

def test_stamp_map_is_union_merged_not_replaced():
    """Box A amends `summary`; box B amends `entities` and never saw A. If the
    map rode the BASE LWW base, one box's whole map would replace the other's and
    the ordering evidence would be lost one level down — the exact defect this
    tier reads. Union of keys, per-key MAX.
    """
    a = _node(summary="A-sum", entities=["x"], amended_fields={"summary": NEW})
    b = _node(summary="old", entities=["y"], amended_fields={"entities": NEW})
    out = cm._merge_tree_node(a, b)
    assert set(out["amended_fields"]) == {"summary", "entities"}
    # And BOTH amendments survive, which is what the union is FOR.
    assert out["summary"] == "A-sum"
    assert out["entities"] == ["y"]


def test_stamp_map_key_order_is_sorted_so_the_merge_is_byte_commutative():
    """: this map is a nested VALUE and emitters write insertion order,
    so an unsorted union reaches the output BYTES and the two boxes ping-pong
    under guard-907's ETag-fenced PUT even though the CONTENT agrees.
    """
    a = _node(amended_fields={"summary": NEW})
    b = _node(amended_fields={"entities": OLD})
    ab = list(cm._merge_tree_node(a, b)["amended_fields"])
    ba = list(cm._merge_tree_node(b, a)["amended_fields"])
    assert ab == ba == sorted(ab)


def test_one_sided_stamp_map_survives():
    a = _node(summary="A", amended_fields={"summary": NEW})
    b = _node(summary="B")
    assert cm._merge_tree_node(a, b)["amended_fields"] == {"summary": NEW}
    assert cm._merge_tree_node(b, a)["amended_fields"] == {"summary": NEW}


# --- pre-existing behaviour must be undisturbed -----------------------------

def test_loser_only_base_field_is_still_preserved():
    """-a: authored BASE fields are not self-correcting, so a
    loser-only one must not be dropped. The new tier iterates the INTERSECTION
    and must not have disturbed this.
    """
    a = _node(summary="A")
    b = _node(summary="B", origin_goal_id="g-115-5411")
    assert cm._merge_tree_node(a, b)["origin_goal_id"] == "g-115-5411"
    assert cm._merge_tree_node(b, a)["origin_goal_id"] == "g-115-5411"


# --- anti-vacuity -----------------------------------------------------------

def test_tier_discriminates_rather_than_always_picking_a_side():
    """Proves the tier SELECTS. Without this, every assertion above survives a
    merge that always returns `a` (or always `b`) — guard-1793.
    """
    a_wins = cm._merge_tree_node(
        _node(summary="A", amended_fields={"summary": NEW}),
        _node(summary="B", amended_fields={"summary": OLD}))["summary"]
    b_wins = cm._merge_tree_node(
        _node(summary="A", amended_fields={"summary": OLD}),
        _node(summary="B", amended_fields={"summary": NEW}))["summary"]
    assert (a_wins, b_wins) == ("A", "B"), (
        f"degenerate: the tier returned {a_wins!r} and {b_wins!r} for mirrored "
        f"stamps, so it is not reading the stamp at all")


# --- the WRITER half: a tier with no writer is inert (rb-5493) --------------

def test_writer_stamps_a_base_field():
    node = {}
    tree_cli._stamp_amendment(node, "summary")
    assert "summary" in node["amended_fields"]


@pytest.mark.parametrize("field", ["confidence", "retrieval_count", "children",
                                   "last_updated", "accuracy"])
def test_writer_does_not_stamp_non_base_fields(field):
    """Those classes have their own ordering keys above the tier; stamping them
    would write a key the merge never reads and invite a future reader to
    consult it in the wrong tier.
    """
    node = {}
    tree_cli._stamp_amendment(node, field)
    assert node == {}, f"{field} is not BASE-class and must not be stamped"


def test_writer_stamp_is_second_granular():
    """DATE granularity here would reproduce the exact same-day tie the stamp
    exists to break — which is why this deliberately differs from the
    date-granular _stamp_progression / _stamp_calibration next to it.
    """
    node = {}
    tree_cli._stamp_amendment(node, "summary")
    stamp = node["amended_fields"]["summary"]
    assert len(stamp) == 19 and stamp[10] == "T" and stamp.count(":") == 2, stamp


def test_writer_preserves_other_fields_stamps():
    node = {"amended_fields": {"entities": OLD}}
    tree_cli._stamp_amendment(node, "summary")
    assert node["amended_fields"]["entities"] == OLD
    assert "summary" in node["amended_fields"]


def test_writer_sorts_keys_to_match_the_merge_output():
    node = {"amended_fields": {"zzz": OLD}}
    tree_cli._stamp_amendment(node, "aaa")
    assert list(node["amended_fields"]) == ["aaa", "zzz"]


def test_writer_tolerates_a_corrupt_stamp_map():
    """A hand-edited node could carry a non-dict here. Overwrite rather than
    raise — a write path that dies on malformed metadata is worse than one that
    repairs it.
    """
    node = {"amended_fields": "not-a-dict"}
    tree_cli._stamp_amendment(node, "summary")
    assert isinstance(node["amended_fields"], dict)


def test_daemon_writer_mirrors_the_cli_writer():
    """The daemon copy is the LIVE path (wrappers are daemon-only), so a stamp
    added to tree.py alone changes nothing at runtime while reading as correct
    in the diff — g-115-2422, where the CLI dropped its stamp and the daemon kept
    it for 19 days. Compared as SOURCE because tree_write.py uses relative
    imports and cannot be loaded standalone.
    """
    src = DAEMON_WRITER.read_text(encoding="utf-8")
    assert "_NON_BASE_STAMP_FIELDS" in src and "def _stamp_amendment" in src, (
        "the daemon mirror of the BASE amendment stamp is missing — the CLI half "
        "alone is invisible to production")
    assert "_stamp_amendment(node, field)" in src, (
        "the daemon defines _stamp_amendment but never CALLS it from _apply_set: "
        "a writer that is never invoked is the same inert half as no writer "
        "(guard-1943 — pinning the writer says nothing about the wiring)")
    for name in tree_cli._NON_BASE_STAMP_FIELDS:
        assert f'"{name}"' in src, (
            f"{name!r} is in the CLI's _NON_BASE_STAMP_FIELDS but not the "
            f"daemon's — the two copies have drifted, so one writer stamps a "
            f"field the other does not")


def test_non_base_list_covers_every_named_merge_class():
    """The writer list is the COMPLEMENT of the named classes, so it is only
    correct while it covers all of them. Derived from the merge-side SSOT rather
    than re-listed, so adding a field to any class fails HERE instead of silently
    writing a stamp the merge ignores.
    """
    named = (set(cm._TREE_MAX_FIELDS) | set(cm._TREE_NEWER_FIELDS)
             | set(cm._TREE_PROGRESSION_FIELDS) | set(cm._TREE_CALIBRATION_FIELDS)
             | set(cm._TREE_STRUCTURAL_FIELDS))
    missing = sorted(named - set(tree_cli._NON_BASE_STAMP_FIELDS))
    assert not missing, (
        f"non-BASE field(s) {missing} are absent from _NON_BASE_STAMP_FIELDS, so "
        f"the writers would stamp them as if they were BASE")
    # And every field the merge classifies BASE must NOT be excluded.
    for f in tree_cli._NON_BASE_STAMP_FIELDS:
        assert cm._classify_tree_field(f) != "BASE", (
            f"{f!r} is excluded from stamping but the merge classifies it BASE, "
            f"so edits to it can never win a same-day tie")
