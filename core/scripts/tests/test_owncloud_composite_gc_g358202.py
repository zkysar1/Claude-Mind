"""Unit tests for `plan_gc` in core/scripts/_owncloud_composite.py, the pure orphan-GC planner ( U2e).

Pure-Python: no backend, no daemon, no network. Heads and segment objects come from the real writer
(`split`, `plan_write`), so the names under test are the names it emits, not hand-made look-alikes.

WHAT IS PINNED. The planner decides what a later pass may DELETE from a versioned store, so each pin is
a way it could delete something it must not, paired with a control that does delete, so a test can fail:
  1. a referenced object is never planned, however old; an unreferenced one past the grace is
  2. the clock: first sighting starts it, the object's own age cannot shorten it, a young object cannot
     ride an old ledger entry, and the boundary is inclusive
  3. the ledger drops referenced and vanished names, so a re-referenced name starts over
  4. a name that is not a segment object name is reported and never deleted; every name the writer
     emits is recognised
  5. a pass that cannot trust its inputs deletes nothing and leaves the ledger as it found it
  6. the cap and the oldest-first order; non-finite inputs; duplicates; no input is mutated

File basename starts with ``test_`` so domain-leak-check.sh skips it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _owncloud_composite as c  # noqa: E402

NOW = 2_000_000_000.0
DAY = 86400.0
GRACE = 14 * DAY
OLD = NOW - 400 * DAY
HEX = "0123456789abcdef" * 2


def _dump(rec) -> str:
    return json.dumps(rec, ensure_ascii=True)


def _legacy(spec, overrides=None) -> bytes:
    """Legacy bytes: spec maps an aspiration number to its goal numbers; overrides maps
    (aspiration, goal number) to fields merged into that goal. Goals are in plain string order."""
    overrides = overrides or {}
    recs = []
    for asp, numbers in spec.items():
        goals = []
        for n in numbers:
            g = {"id": "g-%d-%d" % (asp, n), "title": "goal %d of %d" % (n, asp), "status": "pending"}
            g.update(overrides.get((asp, n), {}))
            goals.append(g)
        goals.sort(key=lambda g: g["id"])
        recs.append({"id": "asp-%d" % asp, "title": "aspiration %d" % asp, "status": "active", "goals": goals})
    return "".join(_dump(r) + "\n" for r in recs).encode("ascii")


def _sizes(raw: bytes) -> dict:
    s = c.split(raw)
    return {c.segment_object_name(k, m["md5"]): len(s.segments[k]) for k, m in s.manifest.items()}


def _listing(sizes, modified=OLD):
    return [(name, modified, size) for name, size in sorted(sizes.items())]


class World:
    """Two writer commits: head1 over state 1, then head2 over state 2, which changed one goal and so
    superseded exactly one segment object. `orphan` is that object's name: head2 no longer lists it."""

    def __init__(self):
        spec = {1: range(1, 601), 2: range(1, 11)}  # asp-1 fills segments 0, 1 and 2; asp-2 has one
        raw1 = _legacy(spec)
        raw2 = _legacy(spec, {(1, 5): {"status": "completed"}})
        self.head1 = c.split(raw1).head
        self.head2 = c.plan_write(self.head1, raw2).head
        self.sizes1, self.sizes2 = _sizes(raw1), _sizes(raw2)
        gone = set(self.sizes1) - set(self.sizes2)
        assert len(gone) == 1 and len(self.sizes2) == 4
        self.orphan = gone.pop()
        self.everything = {**self.sizes1, **self.sizes2}


@pytest.fixture(scope="module")
def w():
    return World()


def _plan(w, *, listing=None, head=None, ledger=None, now=NOW, grace_s=GRACE, **kw):
    if listing is None:
        listing = _listing(w.everything)
    return c.plan_gc(listing, w.head2 if head is None else head, {} if ledger is None else ledger,
                     now, grace_s=grace_s, **kw)


def _aged(w):
    """The ledger under which the orphan has been unreferenced for longer than the grace."""
    return {w.orphan: OLD}


def test_the_fixture_has_exactly_one_orphan_and_the_control_plan_deletes_it(w):
    plan = _plan(w, ledger=_aged(w))
    assert plan.delete == [w.orphan] and plan.refused == []
    assert plan.counts["listed"] == len(w.everything) == 5 and plan.counts["referenced"] == 4


def test_the_default_grace_covers_the_longest_noncurrent_window():
    assert c.GC_GRACE_S >= 14 * DAY


# ---- 1. referenced objects ----------------------------------------------------------------------


def test_a_referenced_object_is_never_planned_however_old(w):
    ledger = {n: OLD for n in w.sizes2}  # an ancient ledger entry for every object the head names
    plan = _plan(w, listing=_listing(w.sizes2), ledger=ledger)
    assert plan.delete == [] and plan.refused == [] and plan.counts["orphans"] == 0
    assert plan.ledger == {}  # and each one left the ledger
    with_orphan = _plan(w, ledger={**ledger, w.orphan: OLD})  # control: the same, plus one orphan
    assert with_orphan.delete == [w.orphan]


# ---- 2. the clock -------------------------------------------------------------------------------


def test_an_orphan_inside_the_grace_is_kept_and_ledgered(w):
    plan = _plan(w, listing=_listing(w.everything, modified=NOW - 10), ledger=_aged(w))
    assert plan.delete == [] and plan.counts["orphans"] == 1 and plan.counts["aged"] == 0
    assert plan.ledger == {w.orphan: OLD}  # a kept name keeps its first sighting


def test_an_old_object_first_seen_today_waits_a_full_grace(w):
    first = _plan(w)  # the object is 400 days old, but this pass is the first to see it unreferenced
    assert first.delete == [] and first.ledger == {w.orphan: NOW}
    assert _plan(w, ledger=first.ledger, now=NOW + GRACE - 1).delete == []
    assert _plan(w, ledger=first.ledger, now=NOW + GRACE).delete == [w.orphan]  # inclusive boundary


def test_a_young_object_cannot_ride_an_old_ledger_entry(w):
    young = NOW - DAY
    listing = _listing(w.everything, modified=OLD)
    listing = [(n, young if n == w.orphan else m, s) for n, m, s in listing]
    ledger = {w.orphan: NOW - 100 * DAY}
    assert _plan(w, listing=listing, ledger=ledger).delete == []  # the object's own age sets the clock
    assert _plan(w, listing=listing, ledger=ledger, now=NOW + GRACE - DAY - 1).delete == []
    assert _plan(w, listing=listing, ledger=ledger, now=NOW + GRACE - DAY).delete == [w.orphan]


# ---- 3. the ledger ------------------------------------------------------------------------------


def test_the_ledger_drops_referenced_and_vanished_names_and_keeps_orphans(w):
    vanished = "asp-9/0.%s.jsonl" % HEX
    ledger = {w.orphan: NOW - 3 * DAY, next(iter(w.sizes2)): NOW - 2 * DAY, vanished: NOW - DAY}
    plan = _plan(w, ledger=ledger)
    assert plan.ledger == {w.orphan: NOW - 3 * DAY}


def test_a_re_referenced_name_starts_its_clock_over(w):
    seen = _plan(w)  # pass 1: the orphan is first seen unreferenced
    assert seen.ledger == {w.orphan: NOW}
    again = _plan(w, head=w.head1, ledger=seen.ledger, now=NOW + 5 * DAY)  # pass 2: head1 names it again
    assert again.delete == [] and w.orphan not in again.ledger  # (head1 does not name the newer object, so that is tracked)
    later = NOW + 20 * DAY
    third = _plan(w, ledger=again.ledger, now=later)  # pass 3: unreferenced again, so a NEW clock
    assert third.delete == [] and third.ledger == {w.orphan: later}
    assert _plan(w, ledger=third.ledger, now=later + GRACE - 1).delete == []
    assert _plan(w, ledger=third.ledger, now=later + GRACE).delete == [w.orphan]
    # the control that shows the pruning is what resets it: pass 3 on pass 1's ledger deletes at once
    assert _plan(w, ledger=seen.ledger, now=later).delete == [w.orphan]


# ---- 4. names -----------------------------------------------------------------------------------

BAD_NAMES = [
    "README",
    "asp-1/0.%s.json" % HEX,           # wrong extension
    "asp-1/0.%s.jsonl" % ("g" * 32),   # not hex
    "asp-1/0.%s.jsonl" % ("A" * 32),   # upper-case hex: the writer emits lower case
    "asp-1/0.%s.jsonl" % ("0" * 31),   # md5 one character short
    "asp-1/a/0.%s.jsonl" % HEX,        # deeper than <aspiration>/<token>
    "0.%s.jsonl" % HEX,                # no aspiration directory
    "asp-1/y.%s.jsonl" % HEX,          # a token that is neither digits nor x
    "pfx/asp-1/0.%s.jsonl" % HEX,      # a prefix the listing adapter forgot to strip
]


def test_unknown_names_are_reported_and_never_deleted(w):
    listing = _listing({**w.sizes2, w.orphan: w.everything[w.orphan], **{n: 7 for n in BAD_NAMES}})
    plan = _plan(w, listing=listing, ledger={n: OLD for n in [w.orphan, *BAD_NAMES]})
    assert plan.delete == [w.orphan]  # control: the well-formed orphan in the same listing goes
    assert plan.unknown == sorted(BAD_NAMES) and plan.counts["unknown"] == len(BAD_NAMES)
    assert plan.ledger == {w.orphan: OLD}  # unknown names are not tracked
    assert plan.counts["listed"] == plan.counts["referenced"] + plan.counts["unknown"] + plan.counts["orphans"]


def test_every_name_the_writer_emits_is_recognised():
    def state(value):
        recs = [{"id": "asp.v2_a-b", "goals": [{"id": "g-1", "n": value}, {"id": "g-1-12", "n": 2},
                                               {"id": "g-nonumber", "n": 3}]}]
        return "".join(_dump(r) + "\n" for r in recs).encode("ascii")

    old, new = _sizes(state(1)), _sizes(state(9))
    assert len(old) == 2 and all(c._OBJECT_NAME.match(n) for n in {**old, **new})
    assert any("/x." in n for n in old)  # the non-numeric goal id lands under the x token
    plan = c.plan_gc(_listing({**old, **new}), c.split(state(9)).head, {n: OLD for n in old}, NOW, grace_s=GRACE)
    assert plan.unknown == [] and len(plan.delete) == 1 and plan.delete[0] in old and plan.delete[0] not in new


# ---- 5. refusals --------------------------------------------------------------------------------


def _head_doc(**fields):
    doc = {"composite": c.FORMAT, "span": 250, "joined_md5": "0" * 32, "aspirations": [], "segments": {}}
    doc.update(fields)
    return (json.dumps(doc) + "\n").encode("ascii")


def _refusal_cases(w):
    empty_head = c.split(b"").head
    missing_one = [t for t in _listing(w.everything) if t[0] != next(iter(w.sizes2))]
    no_segments_field = json.dumps({"composite": c.FORMAT, "aspirations": [], "joined_md5": "0" * 32}).encode("ascii")
    return [
        ("not-a-head", {"head": _legacy({1: [1]})}),
        ("head-unreadable", {"head": w.head2[: len(w.head2) // 2]}),
        ("head-unreadable", {"head": no_segments_field}),
        ("head-unreadable", {"head": _head_doc(segments=[])}),
        ("head-unreadable", {"head": _head_doc(segments={"asp-1/0": {}})}),
        ("head-unreadable", {"head": _head_doc(segments={"asp-1/0": "md5"})}),
        ("head-names-unlisted-segments", {"listing": missing_one}),
        ("head-names-no-segments", {"head": empty_head}),
        ("grace-invalid", {"grace_s": -1}),
        ("grace-invalid", {"grace_s": float("nan")}),
        ("grace-invalid", {"grace_s": None}),
        ("grace-invalid", {"grace_s": "14d"}),
        ("grace-invalid", {"grace_s": True}),
    ]


def test_a_pass_that_cannot_trust_its_inputs_deletes_nothing_and_keeps_the_ledger(w):
    ledger = {w.orphan: OLD, "asp-9/0.%s.jsonl" % HEX: OLD}
    assert _plan(w, ledger=ledger).delete == [w.orphan]  # control: the unmodified inputs delete
    for code, change in _refusal_cases(w):
        plan = _plan(w, ledger=ledger, **change)
        assert plan.refused and plan.refused[0].startswith(code), (code, plan.refused)
        assert plan.delete == [] and plan.unknown == [], code
        assert plan.ledger == ledger and plan.ledger is not ledger, code  # unchanged, and a copy


def test_the_head_as_it_is_stored_blank_padded_plans_like_the_bare_head(w):
    padded = c.pad_head(w.head2)
    assert len(padded) >= c.HEAD_MIN_BYTES > len(w.head2)  # the stored form really is the larger one
    assert _plan(w, head=padded, ledger=_aged(w)) == _plan(w, ledger=_aged(w))
    assert _plan(w, head=padded, ledger=_aged(w)).delete == [w.orphan]


def test_an_empty_head_over_an_empty_listing_is_a_clean_empty_plan():
    plan = c.plan_gc([], c.split(b"").head, {}, NOW, grace_s=GRACE)
    assert plan.refused == [] and plan.delete == [] and plan.counts["listed"] == 0


# ---- 6. cap, order, non-finite inputs, duplicates, purity -----------------------------------------


def _synthetic(i):
    return "asp-1/9.%s.jsonl" % hashlib.md5(b"synthetic-%d" % i).hexdigest()


def test_the_cap_and_the_oldest_first_order(w):
    names = [_synthetic(i) for i in range(5)]  # none is named by head2, so all five are orphans
    listing = _listing(w.sizes2) + [(n, OLD, i + 1) for i, n in enumerate(names)]
    ledger = {n: NOW - (20 + i) * DAY for i, n in enumerate(names)}  # names[4] has waited longest
    oldest_first = [names[4], names[3], names[2], names[1], names[0]]
    full = _plan(w, listing=listing, ledger=ledger)
    assert full.delete == oldest_first and full.counts["orphan_bytes"] == 15
    capped = _plan(w, listing=listing, ledger=ledger, max_delete=3)
    assert capped.delete == oldest_first[:3]
    assert capped.counts["aged"] == 5 and capped.counts["deletable"] == 3 and capped.counts["deletable_bytes"] == 12
    assert set(capped.ledger) == set(names)  # the names left for the next pass stay tracked
    assert _plan(w, listing=listing, ledger=ledger, max_delete=0).delete == []
    assert _plan(w, listing=listing, ledger=ledger, max_delete=-1).delete == []  # never 'all but one'


@pytest.mark.parametrize("modified", [None, float("nan"), float("inf"), float("-inf"), "yesterday", True])
def test_a_modification_time_that_is_not_a_finite_number_counts_as_now(w, modified):
    listing = [(n, modified if n == w.orphan else OLD, s) for n, s in sorted(w.everything.items())]
    for now in (NOW, NOW + GRACE, NOW + 10 * GRACE):  # young on EVERY pass: it never becomes old enough
        plan = _plan(w, listing=listing, ledger=_aged(w), now=now)
        assert plan.delete == [] and plan.counts["undated"] == 1 and plan.counts["orphans"] == 1
    assert _plan(w, ledger=_aged(w)).counts["undated"] == 0  # control: a dated listing reports none


@pytest.mark.parametrize("first", [None, float("nan"), float("inf"), "long ago", True])
def test_a_ledger_value_that_is_not_a_finite_number_restarts_the_clock(w, first):
    plan = _plan(w, ledger={w.orphan: first})
    assert plan.delete == [] and plan.ledger == {w.orphan: NOW}
    assert _plan(w, ledger={w.orphan: OLD}).delete == [w.orphan]  # control: a number is believed


def test_a_duplicate_listing_entry_keeps_the_newest_modification(w):
    base = _listing(w.everything)
    dup = base + [(w.orphan, NOW - 10, 1), (w.orphan, OLD - DAY, 2)]
    plan = _plan(w, listing=dup, ledger=_aged(w))
    assert plan.delete == [] and plan.counts["listed"] == len(w.everything)
    assert plan.counts["orphan_bytes"] == 1  # the newest entry's size, not the first or the last listed
    assert _plan(w, listing=base + [(w.orphan, OLD - DAY, 2)], ledger=_aged(w)).delete == [w.orphan]


def test_no_input_is_mutated_and_the_plan_does_not_depend_on_listing_order(w):
    listing = _listing(w.everything)
    ledger = {w.orphan: OLD}
    kept_listing, kept_ledger = list(listing), dict(ledger)
    one = _plan(w, listing=listing, ledger=ledger)
    two = _plan(w, listing=list(reversed(listing)), ledger=ledger)
    assert listing == kept_listing and ledger == kept_ledger
    assert one == two and one.ledger is not ledger
    assert _plan(w, listing=iter(listing), ledger=ledger) == one  # a one-shot iterable is fine


def test_the_counts_add_up_and_the_bytes_are_the_listed_sizes(w):
    plan = _plan(w, ledger=_aged(w))
    c_ = plan.counts
    assert c_["listed"] == c_["referenced"] + c_["unknown"] + c_["orphans"] == 5
    assert c_["orphan_bytes"] == c_["deletable_bytes"] == w.sizes1[w.orphan]
    assert c_["aged"] == c_["deletable"] == 1
