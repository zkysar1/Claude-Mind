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


def test_bare_suffix_form_still_parses():
    """Backward-compat only. The corpus scan found ZERO bare-suffix ids, so this
    pins that the fix ADDED the hyphenated spelling rather than swapping which
    single spelling is understood."""
    assert _evict._capacity(_asp("asp-335", ["g-335-1", "g-335-12a"])) == 13
