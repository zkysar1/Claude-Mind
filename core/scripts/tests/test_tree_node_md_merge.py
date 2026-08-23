"""Pins for the tree-node markdown section-union merge handler ().

Moves world/knowledge/tree/**/*.md from write-class (b) to (a). The decision and
the rejected alternatives live in
core/config/conventions/governed-store-write-classes.md (g-115-6954).

Every test here is mutation-validated in the goal's record: breaking the handler
must redden these, or they are decoration.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordination_merge as cm  # noqa: E402

H = cm.merge_tree_node_md


# ----------------------------------------------------------------- dispatch --
def test_tree_node_md_dispatches_to_the_section_union_handler():
    assert cm.merge_handler_for("world/knowledge/tree/system/foo.md") is H


def test_dispatch_survives_an_external_world_path():
    # world/ is an EXTERNAL user-supplied path, so the branch must not depend on
    # a literal leading "world/" segment (external-paths.md).
    assert cm.merge_handler_for(
        "/opt/x/World-Data/knowledge/tree/a/b/deep-node.md") is H


def test_does_not_hijack_the_tree_index_yaml():
    # _tree.yaml sits in the same directory and has its OWN handler. A branch
    # matching the directory rather than the .md suffix would steal it.
    h = cm.merge_handler_for("world/knowledge/tree/system/_tree.yaml")
    assert h is not None and h is not H


def test_does_not_claim_unrelated_markdown():
    # The journal .md has its own git merge driver; this handler must not take it.
    assert cm.merge_handler_for(
        "agents/echo/journal/2026/08/2026-08-22.md") is not H


def test_unregistered_path_still_returns_none():
    # The NEGATIVE control the positive dispatch tests need to mean anything.
    # `is not H` above passes whether the result is None or some other handler,
    # so on its own it cannot catch a branch widened into a catch-all. This
    # pins that an unregistered path resolves to NO handler at all.
    for path in ("README.md", "core/scripts/foo.py", "world/some-random-file.md"):
        assert cm.merge_handler_for(path) is None, path


# ------------------------------------------------------------------ merging --
def test_disjoint_sections_from_both_boxes_both_survive():
    a = b"# N\n\n## Alpha\nfrom box A\n"
    b = b"# N\n\n## Beta\nfrom box B\n"
    out = H(a, b)
    assert out is not None
    assert b"Alpha" in out and b"Beta" in out
    assert b"from box A" in out and b"from box B" in out


def test_merge_is_commutative_BYTE_FOR_BYTE():
    # The own-cloud mirror requires BYTE equality, not merely the same content:
    # two boxes see opposite (ours, theirs) by construction, and if they write
    # different bytes for the same logical merge the mirror reads it as
    # divergence and churns forever on a file the merge just "succeeded" on.
    #
    # THE ASSERTION STRENGTH IS THE POINT. This pin previously compared
    # sorted(out.split()) -- a multiset of whitespace-delimited TOKENS, which is
    # blind to ordering and so passed against a handler that was NOT commutative.
    # Measured : merge(a,b) emitted Alpha-then-Beta and merge(b,a)
    # emitted Beta-then-Alpha; the token-multiset form saw no difference. A
    # weaker assertion than the criterion it claims to enforce is indistinguish-
    # able from no assertion.
    a = b"# N\n\n## Alpha\nfrom box A\n"
    b = b"# N\n\n## Beta\nfrom box B\n"
    assert H(a, b) == H(b, a)


def test_same_date_two_section_case_is_commutative_and_loses_nothing():
    # The exact case that killed candidate (a) (whole-file front-matter LWW):
    # equal last_updated on both sides, one distinct section each. LWW picks a
    # whole file and the loser's section is lost; section-union must keep both,
    # and must do so byte-identically in either argument order.
    a = b"---\nlast_updated: 2026-08-22\n---\n\n## Alpha\nA body\n"
    b = b"---\nlast_updated: 2026-08-22\n---\n\n## Beta\nB body\n"
    out = H(a, b)
    assert out is not None
    assert b"A body" in out and b"B body" in out
    assert out == H(b, a)


def test_identical_sides_round_trip():
    a = b"# N\n\n## Alpha\nsame\n"
    out = H(a, a)
    assert out is not None and b"Alpha" in out


# ----------------------------------------------------------------- refusals --
def test_same_heading_divergence_REFUSES():
    # Returning None keeps the backend's safe-freeze. Auto-picking a side here is
    # exactly the silent corruption rb-3683 records.
    a = b"# N\n\n## Alpha\nA says x\n"
    b = b"# N\n\n## Alpha\nB says y\n"
    assert H(a, b) is None


def test_undecodable_side_REFUSES_and_is_never_read_as_empty():
    # A side that will not decode must never be treated as empty, nor lossily
    # decoded - either turns a one-sided edit into a whole-file overwrite.
    #
    # THE PAYLOAD IS LOAD-BEARING. An earlier version of this test used
    # b"\xff\xfe\x00bad", which carries NO '##' heading, so it lands entirely in
    # the preamble and the merge returns None for a reason that has nothing to do
    # with decoding. That pin passed with the decode guard REMOVED - measured,
    # mutation M2 of : 9/9 green against a handler that had been broken.
    # The bytes below decode cleanly under errors="ignore" into a well-formed,
    # DISJOINT section, so a lossy handler would merge them and return bytes.
    # Only a strict-decode refusal returns None here.
    good = b"# N\n\n## Beta\nfrom box B\n"
    lossy_would_merge = b"# N\n\n## Gamma\ncaf\xff\xfe more\n"
    assert H(lossy_would_merge, good) is None
    assert H(good, lossy_would_merge) is None


# ------------------------------------------------------------ known residual --
def test_KNOWN_RESIDUAL_a_deliberate_section_eviction_resurrects():
    """A deleted section comes BACK. This is the accepted cost of section-union.

    Not a bug report and not an aspiration -- a pin on measured behavior, so the
    residual is DEMONSTRATED rather than assumed (g-115-7071 outcome 4) and so a
    future change to it fails loudly here instead of surprising a reader.

    Mechanism: the base is absent, so the handler cannot tell "B deleted this
    section" from "A added this section" -- the two are byte-identical at this
    layer. Union therefore resurrects the evicted section. Every base-free merge
    has this property; it is not specific to the section algorithm.

    Why it is accepted: the alternative failure is worse in both directions.
    Treating an absent section as a deletion would let a box that merely has an
    OLDER copy silently delete a peer's live section, which is the whole-file
    data loss this handler exists to prevent, and the status quo it replaced
    (class (b) fence-only) froze the node permanently on ANY concurrent edit.
    Resurrection is visible and hand-correctable; silent deletion is neither.

    Detection is tracked by the follow-up goal filed from this goal.
    """
    full = b"# N\n\n## Alpha\nkeep\n\n## Doomed\nDELETE ME\n"
    evicted = b"# N\n\n## Alpha\nkeep\n"
    out = H(evicted, full)
    assert out is not None
    assert b"DELETE ME" in out, "residual changed -- re-read this test's docstring"
