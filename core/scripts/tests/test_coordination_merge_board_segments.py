""": a board DATE SEGMENT must inherit its parent channel's handler.

`merge_handler_for`'s board branch keyed on the EXACT basename, so the live
`coordination.jsonl` resolved to `merge_rotated_board_jsonl` and every
`coordination-<YYYY-MM-DD>.jsonl` segment resolved to None.

Why that is a wedge rather than a slow path: a board store is class (b),
fence-only (`core/config/conventions/governed-store-write-classes.md`) — there
is no reconciler below the write, so a stale fence on a handler-less object is
PERMANENT, not retryable (rb-2639: own-cloud CAS `write_conflict` is exactly a
per-object stale-IfMatch deadlock). Coordination alone had SIX concurrent
cross-box writers in the g-358-181 alarm window. So this must be registered
BEFORE any writer mints a segment (guard-6883 / guard-6907).

The load-bearing test here is `test_old_exact_basename_dispatch_would_fail`: it
runs the PRE-FIX predicate against the same input and asserts it returns None.
Without that control this file would pass just as happily against the defect,
which is the shape guard-2421 warns about — a green that never had a chance to
be red.
"""
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402
import _board_paths as bp  # noqa: E402
import board  # noqa: E402

_DAY = datetime.date(2026, 9, 17)


def _registered_board_channels():
    """The canonical board channels, IMPORTED from board.py rather than
    hardcoded here — a hardcoded list silently stops covering a channel added
    later (guard-1715: an enumerator's all-clear is bounded by the population
    IT declares).

    Deriving the population from `_HANDLERS` instead looks more general and is
    WRONG: it sweeps in every append-only .jsonl whose basename merely collides
    with a board-shaped name. Measured while writing this file — `gate-firings`
    is a `meta/` telemetry store, and `gate-firings-<date>.jsonl` is already
    claimed by an EARLIER dated-basename branch in merge_handler_for, so the
    wider population asserted a board rule against a non-board store and failed
    on a file this change never touches.
    """
    return sorted(set(board.DEFAULT_CHANNELS))


def test_there_is_at_least_one_registered_board_channel():
    """Positive control for every parametrised test below: an empty population
    would make them all vacuously green."""
    assert _registered_board_channels(), "board.DEFAULT_CHANNELS is empty"


def test_live_channel_still_resolves_to_the_rotated_handler():
    """Regression floor — the pre-existing behaviour must not move."""
    assert cm.merge_handler_for("world/board/coordination.jsonl") is cm.merge_rotated_board_jsonl


def test_every_registered_channel_segment_inherits_the_parent_handler():
    checked = 0
    for channel in _registered_board_channels():
        seg = bp.segment_name(channel, _DAY)
        live = bp.live_name(channel)
        if cm.merge_handler_for("world/board/" + live) is not cm.merge_rotated_board_jsonl:
            # Not a board channel (some append-only stores live elsewhere).
            continue
        checked += 1
        assert cm.merge_handler_for("world/board/" + seg) is cm.merge_rotated_board_jsonl, (
            "segment %s did not inherit its parent %s" % (seg, live)
        )
    assert checked, "no board channel resolved through the board branch"


def test_old_exact_basename_dispatch_would_fail():
    """THE CONTROL. Re-runs the pre-fix predicate verbatim; it must return the
    defect (None) for a segment while returning the handler for the live file.

    A copy, not a monkeypatch: the point is to pin what the OLD code did, so
    this stays meaningful even if the production branch is refactored again.
    """
    def old_dispatch(path):
        parts = path.split("/")
        if (len(parts) >= 2 and parts[-2] == "board"
                and cm._HANDLERS.get(parts[-1]) is cm.merge_append_only_jsonl
                and ".history" not in parts):
            return cm.merge_rotated_board_jsonl
        return None

    assert old_dispatch("world/board/coordination.jsonl") is cm.merge_rotated_board_jsonl
    assert old_dispatch("world/board/coordination-2026-09-17.jsonl") is None, (
        "the pre-fix dispatch must fail here, or this file's green means nothing"
    )
    # ... and the shipped dispatch must disagree with it on exactly that input.
    assert cm.merge_handler_for("world/board/coordination-2026-09-17.jsonl") is cm.merge_rotated_board_jsonl


def test_merger_and_reader_agree_on_is_segment():
    """One definition, asked from both sides (guard-5940 / guard-2108)."""
    seg = bp.segment_name("coordination", _DAY)
    live = bp.live_name("coordination")
    arch = bp.archive_name("coordination")

    assert bp.is_segment("coordination", seg) is True
    assert bp.is_segment("coordination", live) is False
    assert bp.is_segment("coordination", arch) is False

    assert bp.segment_parent(seg) == "coordination"
    assert bp.segment_parent(live) is None
    assert bp.segment_parent(arch) is None

    # The merger's answer must track the predicate's, not a second regex.
    assert cm.merge_handler_for("world/board/" + seg) is cm.merge_rotated_board_jsonl
    assert cm.merge_handler_for("world/board/" + arch) is None


def test_unregistered_channel_segment_does_not_inherit():
    """Inheritance is parent-registration-gated: a segment of a channel nobody
    registered stays fence-only rather than acquiring a handler by pattern."""
    assert cm.merge_handler_for("world/board/not-a-channel-2026-09-17.jsonl") is None


def test_history_snapshot_of_a_segment_is_still_excluded():
    """`.history` holds IMMUTABLE point-in-time copies; unioning two boxes'
    snapshots corrupts the artifact the store exists to preserve (g-358-119)."""
    p = "world/.history/snapshots/board/coordination-2026-09-17.jsonl"
    assert cm.merge_handler_for(p) is not cm.merge_rotated_board_jsonl


@pytest.mark.parametrize("bad", [
    "coordination-2026-9-17.jsonl",      # not zero-padded
    "coordination-2026-09-17.json",      # wrong extension
    "coordination-archive.jsonl",        # archive, not a date
    "coordination.jsonl",                # the live file
])
def test_near_miss_names_do_not_read_as_segments(bad):
    assert bp.segment_parent(bad) is None
