""": the two id-suffix parsers inside aspirations-evict-completed.py
disagreed, and the audit half was the one that was wrong.

`_GOAL_SEQ_RE` (used by `_conservation_violation`, the guard on the EVICT path)
matches the HYPHENATED decompose suffix `g-NNN-262-a`. `_capacity()` (used by
`_audit_violations`, the READ-ONLY audit path) parsed the tail with
`rstrip("a-z")`, which turns `262-a` into `262-` — not a digit string — so the
goal was dropped from BOTH `max_seq` and `suffixed` and never counted toward the
ceiling at all. `_audit_violations` meanwhile counts `in_list` by a plain
`startswith(prefix)`, which DOES include it.

Measured on live world state 2026-07-31 (bravo, cc-05 / Linux 6.8.0-136-generic):
asp-335 held 622 unique goals, `g-335-1..618` complete with zero gaps plus the
four decompose legs `g-335-262-a..d`. `_conservation_violation` returned None
(capacity 622, clean) while `_audit_violations` reported
`in_list=622 capacity=618 excess=4 true_evicted_max=-4`. Opposite verdicts, same
aspiration, same data (rb-301 filter-predicate divergence).

That matters because the audit's remediation is destructive: it directs the
operator to `--repair-census --apply`, which proportionally SHRINKS a census to
satisfy the undercounted ceiling — mutating a CORRECT census to fit a phantom.
`_legacy_census_loose` cannot suppress it either, since that suppressor requires
`in_list < capacity` and here in_list EXCEEDS capacity.

The `true_evicted_max: -4` in that report is the tell worth remembering: a
negative allocation headroom is not a small error, it is arithmetically
impossible, and it was printed next to the confident violation line.

Corpus check the same day: 30 hyphenated-suffix goal ids live across the world
queue, ZERO bare-suffix. The spelling `_capacity()` accepted was the one nothing
mints; the spelling it rejected was the only one in use.

Also pins guard-1161 on this file's regex (widths must be fully open-ended).
`_GOAL_SEQ_RE` was `^g-(\\d{3})-(\\d{2,4})(-[a-z])?$`, which left asp-115 — at
g-115-4270 and climbing — one mint away from `g-115-10000` falling out of its own
conservation guard, and excluded legacy low-end ids like `g-1-5` outright.

Pure unit test: builds aspiration dicts in memory. No S3, no daemon, no world I/O.
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
PROJECT_ROOT = SCRIPTS.parents[1]
for p in (str(SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(mod_name, file_name):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_evict = _load("aspirations_evict_completed_g4270", "aspirations-evict-completed.py")


def _goal(gid, status="completed"):
    return {"id": gid, "status": status, "title": f"t-{gid}", "recurring": False,
            "created_at": "2026-01-01T00:00:00"}


def _asp(asp_id, gids, census=None):
    a = {"id": asp_id, "title": f"a-{asp_id}", "status": "active",
         "goals": [_goal(g) for g in gids]}
    if census is not None:
        a["archived_census"] = census
    return a


# The live asp-335 shape, scaled down: a contiguous numeric run plus the four
# hyphenated decompose legs of one parent.
_LIVE_SHAPE = ["g-335-1", "g-335-2", "g-335-262",
               "g-335-262-a", "g-335-262-b", "g-335-262-c", "g-335-262-d"]


# ---------------------------------------------------------------- the regression

def test_capacity_counts_hyphenated_suffix_ids():
    """The bug, minimally. max_seq 262 + 4 suffixed == 266."""
    assert _evict._capacity(_asp("asp-335", _LIVE_SHAPE)) == 266


def test_hyphenated_suffix_goal_is_not_silently_dropped():
    """Not just undercounted — the old parser skipped the id ENTIRELY, so it
    raised neither max_seq nor suffixed. Isolate that: a suffixed id whose
    numeric part is the highest in the aspiration must still set the ceiling."""
    # -a is the only id carrying seq 900.
    cap = _evict._capacity(_asp("asp-335", ["g-335-1", "g-335-900-a"]))
    assert cap == 901, "suffixed id must contribute BOTH its seq and its +1"


def test_audit_reports_no_violation_on_the_live_shape():
    """The end-to-end false positive that sent an operator at --repair-census."""
    assert _evict._audit_violations([_asp("asp-335", _LIVE_SHAPE)]) == []


# ------------------------------------------------------- the divergence itself

def test_evict_guard_and_audit_agree_on_the_live_shape():
    """rb-301: the load-bearing pin. Either path may be conservative, but the two
    must never return OPPOSITE verdicts on one aspiration — that is what makes a
    false positive look authoritative enough to act on."""
    asp = _asp("asp-335", _LIVE_SHAPE)
    guard_says_violation = _evict._conservation_violation(asp) is not None
    audit_says_violation = bool(_evict._audit_violations([asp]))
    assert guard_says_violation == audit_says_violation is False


def test_the_two_parsers_agree_across_every_id_spelling_in_the_corpus():
    """Both capacity computations must read the same grammar. Drives the same
    ids through each and compares the derived ceiling directly."""
    for gids in (["g-335-1", "g-335-2"],
                 ["g-335-1", "g-335-2-a"],
                 ["g-335-10", "g-335-10-a", "g-335-10-b"],
                 _LIVE_SHAPE):
        asp = _asp("asp-335", gids)
        # _conservation_violation's ceiling, recomputed from its own regex.
        max_seq = suffixed = 0
        for g in asp["goals"]:
            m = _evict._GOAL_SEQ_RE.match(g["id"])
            assert m is not None, f"regex must match live id {g['id']}"
            max_seq = max(max_seq, int(m.group(2)))
            if m.group(3):
                suffixed += 1
        assert _evict._capacity(asp) == max_seq + suffixed, gids


# -------------------------------------------------------- negative control

def test_a_genuine_violation_is_still_detected():
    """The fix raises the ceiling, so prove it did not simply blind the guard.
    Two live goals + a census claiming 5 evictions cannot fit a 2-id space."""
    asp = _asp("asp-401", ["g-401-1", "g-401-2"],
               census={"by_status": {"completed": 5}})
    v = _evict._audit_violations([asp])
    assert len(v) == 1, "a real phantom double-count must still be reported"
    assert v[0]["excess"] == 5, v


def test_clean_aspiration_never_reports_negative_headroom():
    """`true_evicted_max: -4` was the arithmetic tell. A negative value means the
    ceiling was computed below the count it is supposed to bound, which no real
    allocation can produce — so it can only ever indicate a parser defect."""
    for v in _evict._audit_violations([_asp("asp-335", _LIVE_SHAPE)]):
        assert v["true_evicted_max"] >= 0, v


# ------------------------------------------------------------ guard-1161 widths

def test_goal_seq_regex_widths_are_open_ended():
    """Bounded widths silently drop ids at BOTH ends (guard-1161)."""
    for gid, seq in (("g-115-10000", "10000"),   # past the old \\d{2,4} ceiling
                     ("g-115-4270", "4270"),     # today's high-water mark
                     ("g-1-5", "5"),             # legacy non-zero-padded, old \\d{3}
                     ("g-335-262-a", "262")):
        m = _evict._GOAL_SEQ_RE.match(gid)
        assert m is not None, f"{gid} must match"
        assert m.group(2) == seq, gid


def test_capacity_survives_a_five_digit_sequence():
    """The width fix has to reach the ceiling arithmetic, not just the regex."""
    assert _evict._capacity(_asp("asp-115", ["g-115-9999", "g-115-10000"])) == 10000


def test_foreign_ids_stay_outside_the_sequence_space():
    """Cross-world ids (9 live in the corpus) must contribute to neither side —
    widening the digit classes must not start swallowing them."""
    assert _evict._GOAL_SEQ_RE.match("g-xw-20260728T184008-01") is None
    asp = _asp("asp-335", ["g-335-1", "g-xw-20260728T184008-01"])
    assert _evict._capacity(asp) == 1


# ------------------------------------------ : one grammar, one parser
#
#  (above) fixed the SYMPTOM and left the divergence standing: it
# taught `_capacity`'s `rstrip("a-z")` heuristic to also strip a trailing hyphen,
# so the two readings agreed on every id the corpus happened to contain. Measured
# 2026-08-10 (alpha, cc-07 / Linux 6.8.0-137-generic) over all 7,213 live goal +
# evicted ids in 19 aspirations: ZERO divergences, and still three synthetic
# ones — `12a`, `12ab`, `12-ab`, where the heuristic counted and the regex did
# not. Agreement held by luck of population, not by construction.
#
# It was also never only TWO readings. The naive `startswith(prefix)` in
# `_audit_violations` (and its twin in `_repair`) is the OTHER half of the
# apples-to-oranges the incident named: `in_list=622` counted the four
# `-a..d` legs, `capacity=618` did not, and the excess was the
# difference between two id sets rather than a fact about the census.
#
# All four now read `_parse_seq_id`. The tests below pin the property that makes
# the class unconstructable rather than merely absent: an id contributes to BOTH
# sides of the pigeonhole inequality or to NEITHER.

_ALL_SPELLINGS = [
    "g-500-12",       # plain            — 7,163 of 7,213 live ids
    "g-500-12-a",     # hyphenated letter —    50 of 7,213 live ids
    "g-500-12a",      # bare letter      — 0 live, and unmintable (below)
    "g-500-12ab",     # bare multi       — 0 live, unmintable
    "g-500-12-ab",    # hyphenated multi — 0 live, unmintable
    "g-500-12-a-b",   # two suffixes     — 0 live, unmintable
    "g-500-12-1",     # numeric tail     — 0 live, unmintable
    "g-xw-20260728T184008-01",  # cross-world id — outside the sequence space
    "g-501-12",       # another aspiration's sequence space
]


def test_every_spelling_contributes_to_both_sides_or_neither():
    """The load-bearing invariant. For EVERY id spelling, the left side of the
    pigeonhole inequality (`in_list`) and the right side (`capacity`) must make
    the same decision about whether that id exists. A spelling counted on one
    side only is exactly what manufactured `excess=4` on a clean asp-335."""
    for gid in _ALL_SPELLINGS:
        base = _asp("asp-500", ["g-500-1"])
        with_id = _asp("asp-500", ["g-500-1", gid])
        counted_left = (_evict._in_list_sequence_goals(with_id)
                        - _evict._in_list_sequence_goals(base)) > 0
        counted_right = _evict._capacity(with_id) != _evict._capacity(base)
        assert counted_left == counted_right, (
            f"{gid}: in_list counted={counted_left} but capacity counted="
            f"{counted_right} — the two sides read different grammars")


def test_no_spelling_can_manufacture_a_violation():
    """The consequence of the invariant above, stated as the outcome that
    matters: adding ANY single id to a clean aspiration must not make the audit
    report a violation, because a lone extra goal cannot exceed its own ceiling.
    The old code could — a prefixed id the parser rejected raised in_list
    without raising capacity."""
    for gid in _ALL_SPELLINGS:
        asp = _asp("asp-500", ["g-500-1", "g-500-2", gid])
        assert _evict._audit_violations([asp]) == [], gid
        assert _evict._conservation_violation(asp) is None, gid


def test_bare_suffix_form_is_unmintable_so_symmetry_is_the_right_pin():
    """Replaces the  backward-compat pin that asserted `_capacity`
    still understood the BARE spelling (`g-335-12a` -> 13).

    That pin was dropped deliberately, not incidentally. The bare spelling is
    not merely unused — it is UNMINTABLE: the goal-id validator on the write
    path rejects it, so no sanctioned write can produce one. Teaching ONE of two
    counters to accept an id the other cannot is what the divergence WAS.
    Symmetry is the property worth pinning; agreeing on an impossible id is not.
    """
    from aspirations import GOAL_ID_RE   # the write-path validator, verbatim

    assert GOAL_ID_RE.match("g-335-262-a"), "hyphenated suffix must be mintable"
    for unmintable in ("g-335-12a", "g-335-12ab", "g-335-12-ab", "g-335-12-a-b"):
        assert not GOAL_ID_RE.match(unmintable), unmintable
        # ...and both counters ignore it, so no phantom excess appears.
        asp = _asp("asp-335", ["g-335-10", unmintable])
        assert _evict._in_list_sequence_goals(asp) == 1
        assert _evict._capacity(asp) == 10
        assert _evict._audit_violations([asp]) == []


def test_evict_reader_accepts_everything_the_write_validator_can_mint():
    """Cross-file grammar parity, the drift this whole goal is about seen one
    layer out. The evict-side reader must be a SUPERSET of the write-side
    validator: any id that can be minted must be visible to the conservation
    guard, or a real allocation sits outside the ceiling that is supposed to
    bound it. (The reverse is not required — the reader is deliberately wider,
    since the validator's bounded widths exclude legacy ids it must still read.)
    """
    from aspirations import GOAL_ID_RE

    for gid in ("g-115-01", "g-115-999", "g-115-5740", "g-335-262-a", "g-001-42"):
        assert GOAL_ID_RE.match(gid), f"fixture {gid} must be mintable"
        assert _evict._parse_seq_id(gid, None) is not None, (
            f"{gid} is mintable but invisible to the evict-side parser")


# A census that VIOLATES without tripping `_legacy_census_loose`: that
# suppressor requires BOTH no recorded evicted_ids AND in_list < capacity, so
# recording one evicted id defeats it and the violation is reported for real.
_VIOLATING_GIDS = ["g-500-10", "g-500-11-a", "g-500-12a",
                   "g-xw-20260728T184008-01"]
_VIOLATING_CENSUS = {"by_status": {"completed": 40},
                     "evicted_ids": {"completed": ["g-500-9"]}}


def test_repair_and_audit_count_in_list_the_same_way():
    """`_audit_violations` decides WHETHER to repair; `_repair` decides BY HOW
    MUCH. Both carried their own `startswith(prefix)` count. A divergence
    between them repairs to a target the post-repair guard then rejects, so the
    whole write aborts — a silent no-op dressed as a fix."""
    audit = _evict._audit_violations(
        [_asp("asp-500", _VIOLATING_GIDS, census=_VIOLATING_CENSUS)])
    assert len(audit) == 1, "fixture must violate, else this proves nothing"
    # The two out-of-grammar ids (bare-suffix, cross-world) must be excluded
    # from in_list, exactly as they are from capacity.
    assert audit[0]["in_list"] == 2, audit

    repair = _evict._make_census_repair("2026-01-01T00:00:00")
    # Raises if ANY violation survives, so returning at all is the parity proof.
    repaired = repair([_asp("asp-500", _VIOLATING_GIDS,
                            census=_VIOLATING_CENSUS)])
    assert _evict._audit_violations(repaired) == []


def test_repair_skips_what_the_audit_suppresses():
    """The audit and the repair must agree on WHICH aspirations violate, not
    only on how in_list is counted. `_repair` lacked `_legacy_census_loose`, so
    an aspiration the audit classified as a known-loose FALSE POSITIVE was
    shrunk anyway — collateral damage from one genuine violation elsewhere,
    since main() gates the whole repair pass on the audit being non-empty."""
    loose = _asp("asp-500", ["g-500-10", "g-500-11-a"],
                 census={"by_status": {"completed": 40}})   # no evicted_ids
    assert _evict._legacy_census_loose(
        loose, _evict._in_list_sequence_goals(loose), _evict._capacity(loose))
    assert _evict._audit_violations([loose]) == [], "audit must suppress this"

    before = dict(loose["archived_census"]["by_status"])
    # Repair a batch containing BOTH the suppressed aspiration and a genuine
    # violation — the shape main() actually produces.
    out = _evict._make_census_repair("2026-01-01T00:00:00")([
        _asp("asp-500", ["g-500-10", "g-500-11-a"],
             census={"by_status": {"completed": 40}}),
        _asp("asp-501", _VIOLATING_GIDS[:2] + ["g-501-12a"],
             census={"by_status": {"completed": 40},
                     "evicted_ids": {"completed": ["g-501-9"]}}),
    ])
    assert out[0]["archived_census"]["by_status"] == before, (
        "the audit-suppressed aspiration must be left untouched")


def test_two_parsers_are_now_one():
    """Structural: the second parser is GONE, not merely aligned. A future edit
    that reintroduces a local id parse in this file should fail here rather than
    wait for the next opposite-verdict incident.

    Walks the AST rather than the source text — the prose in this file's own
    docstrings names both deleted constructs, so a text scan reports them
    present forever and the pin never fails (it would be a permanent red, which
    is the same uselessness as a permanent green)."""
    import ast

    tree = ast.parse((SCRIPTS / "aspirations-evict-completed.py")
                     .read_text(encoding="utf-8"))
    seq_re_reads, rstrips, prefix_startswith = 0, 0, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_GOAL_SEQ_RE":
            if isinstance(node.ctx, ast.Load):
                seq_re_reads += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "rstrip":
                rstrips += 1
            if (node.func.attr == "startswith" and len(node.args) == 1
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "prefix"):
                prefix_startswith += 1
    assert rstrips == 0, "the rstrip id heuristic is back — that was parser #2"
    assert prefix_startswith == 0, (
        "a naive startswith(prefix) id count is back — that was the other side "
        "of the apples-to-oranges")
    assert seq_re_reads == 1, (
        f"_GOAL_SEQ_RE is read {seq_re_reads}x; it must be read ONLY inside "
        "_parse_seq_id — a second read is a second reading of the grammar")
