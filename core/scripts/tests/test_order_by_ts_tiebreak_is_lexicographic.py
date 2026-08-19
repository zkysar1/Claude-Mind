"""test_order_by_ts_tiebreak_is_lexicographic.py — .

WHAT THIS PINS. `_order_by_ts` breaks an equal-timestamp tie with
`_canon(a) >= _canon(b)` — a STRING comparison decided at the first divergent
character. Its docstring said "larger canonical JSON wins" for its whole life,
and five sibling comments in the same module repeat the size wording.

WHY A TEST AND NOT JUST A DOC FIX. The wrong reading is not cosmetic; it is
wrong in the direction that costs cycles, and it has now misled three separate
pieces of work:

  1. g-115-5294 — its own record says an amendment "currently wins by being
     longer". Measured: a 63-char document beat a 101-char one. Two versions of
     that goal's coupling test set up the OPPOSITE of the adverse case they
     claimed and passed VACUOUSLY (governed-store-write-classes.md L384-392).
  2. g-115-5411 — an agent built a 4-phrase replacement whose canon was 1953
     against an incumbent's 1942, expecting to win on size. It lost, in both arg
     orders, because index 1 of a list diverged on 'c' > 'a'. A full cycle.
  3. The module ALREADY knew: `_GUARD_MONOTONIC_FIELDS` documents the same
     property correctly, with the worked example `"9" > "10"` regressing a
     counter. Correct prose sitting twelve hundred lines from wrong prose did
     not stop either of the two above.

So the durable fix is executable, not editorial. Prose can drift back; these
assertions cannot.

WHAT IS NOT PINNED: the nine remaining `content-larger` shorthand comments
elsewhere in the module. They are the same loose wording in lower-traffic
places, left rather than swept because each edit needs its own read and the
sweep is not this goal's scope. If one of them misleads someone next, that is
the evidence to finish the sweep.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordination_merge import _canon, _order_by_ts  # noqa: E402


TS = "last_updated"


def _pair(va, vb):
    """Two records with an IDENTICAL timestamp, so only the tiebreak decides."""
    return ({TS: "2026-08-08", "v": va}, {TS: "2026-08-08", "v": vb})


class TestTheTiebreakIsLexicographicNotSize:

    def test_a_shorter_value_beats_a_longer_one_when_it_sorts_higher(self):
        """The headline claim, as a direct counterexample to "larger wins".

        "z" is one character; "aaaa...." is forty. Under a size rule the long
        one wins; under the real rule "z" wins on the first character."""
        short, long_ = _pair("z", "a" * 40)
        win, lose = _order_by_ts(short, long_, TS)
        assert win is short, (
            "a 1-char value lost to a 40-char one — the tiebreak is behaving "
            "like a SIZE comparison, which is the misreading this file exists "
            "to refute")
        assert lose is long_
        assert len(_canon(win)) < len(_canon(lose)), (
            "the fixture stopped being a counterexample: the winner is no "
            "longer the shorter side, so this test proves nothing")

    def test_the_decision_is_made_at_the_first_divergent_character(self):
        """'s exact shape: a LIST whose element 1 decides it, while
        the overall canonical form is longer on the losing side."""
        incumbent, replacement = _pair(
            ["cognitive load", "cost add next environment", "primitives amortization"],
            ["cognitive load", "adapter environment cost", "primitives amortization",
             "one more phrase to make this side strictly longer"],
        )
        assert len(_canon(replacement)) > len(_canon(incumbent)), \
            "fixture broken: the replacement must be the LONGER side"
        win, _ = _order_by_ts(incumbent, replacement, TS)
        assert win is incumbent, (
            "'c' > 'a' at list index 1 must decide this regardless of length — "
            "if the replacement won, the tiebreak changed semantics")

    def test_nine_loses_to_ten_the_counter_regression_shape(self):
        """The module's own worked example, executed rather than described:
        string "9" sorts after string "10", so a bare content tiebreak on a
        counter REGRESSES it. This is why _GUARD_MONOTONIC_FIELDS exists."""
        nine, ten = _pair("9", "10")
        win, _ = _order_by_ts(nine, ten, TS)
        assert win is nine, (
            'string "9" must beat string "10" — if not, _canon is no longer a '
            "plain string compare and _GUARD_MONOTONIC_FIELDS' rationale is stale")

    def test_the_tiebreak_is_symmetric_under_arg_order(self):
        """Commutativity is the property the tiebreak exists FOR — an arbitrary
        winner is acceptable only because both machines pick the SAME one.
        Checked in both directions on every fixture above."""
        for a, b in (_pair("z", "a" * 40),
                     _pair("9", "10"),
                     _pair(["b"], ["a", "a", "a"])):
            fwd, _ = _order_by_ts(a, b, TS)
            rev, _ = _order_by_ts(b, a, TS)
            assert _canon(fwd) == _canon(rev), (
                f"arg order changed the winner for {a['v']!r} vs {b['v']!r} — "
                "two boxes would disagree and the fenced PUT would ping-pong "
                "(guard-907)")

    def test_a_real_timestamp_difference_still_dominates_the_tiebreak(self):
        """Positive control (rb-4133 / guard-1220): without this, every
        assertion above would pass just as well against a function that ONLY
        ever tiebreaks and never reads the timestamp at all."""
        older = {TS: "2026-08-07", "v": "z"}
        newer = {TS: "2026-08-08", "v": "a"}
        win, _ = _order_by_ts(older, newer, TS)
        assert win is newer, (
            "the newer timestamp must win outright — if the tiebreak reached "
            "this pair, recency has stopped being the primary rule")


class TestTheDocstringMatchesTheCode:
    """The prose is what misled three pieces of work, so pin the prose too.

    Deliberately asserts on _order_by_ts's OWN docstring only. A module-wide
    ban on the word "larger" would fail on the corrective sentences that quote
    the wrong claim in order to refute it, and on _GUARD_MONOTONIC_FIELDS,
    which uses "lexically-larger" correctly."""

    def test_the_docstring_says_lexicographic(self):
        doc = _order_by_ts.__doc__ or ""
        assert "LEXICOGRAPHICALLY-GREATER" in doc.upper() or "LEXICOGRAPH" in doc.upper(), (
            "_order_by_ts's docstring no longer states the tiebreak is "
            "lexicographic — that omission has cost two agents a cycle each")

    def test_the_docstring_does_not_claim_larger_wins_unqualified(self):
        doc = _order_by_ts.__doc__ or ""
        for line in doc.splitlines():
            low = line.lower()
            if "larger canonical json wins" in low and "not" not in low:
                raise AssertionError(
                    "the docstring asserts 'larger canonical JSON wins' without "
                    f"refuting it: {line.strip()!r}. It compares STRINGS.")
