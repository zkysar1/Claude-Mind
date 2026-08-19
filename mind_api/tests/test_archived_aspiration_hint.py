"""Regression pins for `_archived_aspiration_hint` ().

The add-goal 404 used to collapse two different situations into one message:
an aspiration that was ARCHIVED out of the live store, and an id that never
existed. guard-1555 says those must be distinguishable — a lookup miss that
cannot tell them apart reads like a typo rather than a lifecycle event, which
is precisely how the analyze-npc-behavior skill filed every auto-generated
improvement goal into an archived asp-226 for the life of the skill.

The load-bearing invariant is that the hint is ONE-DIRECTIONAL. A hit upgrades
the message; a miss must return exactly "" and never grow into a "this id does
not exist anywhere" claim, because the local aspirations-archive.jsonl is
S3-backed and never pulled (g-115-3541), so the mirror can be stale. Test 4 is
the one that pins that direction, and it is the one worth keeping if any of
these are ever pruned.
"""

import json

import pytest

from mind_api.src.endpoints import aspirations_write


def _write_archive(base, records):
    p = base / "aspirations-archive.jsonl"
    p.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return p


def test_archived_id_yields_hint_naming_the_status(tmp_path):
    """A HIT is trustworthy: name the archival AND the status."""
    _write_archive(tmp_path, [{"id": "asp-226", "status": "completed"}])
    hint = aspirations_write._archived_aspiration_hint(tmp_path, "asp-226")
    assert "ARCHIVED" in hint
    assert "completed" in hint
    # It must say WHERE add-goal looks, or the reader still has to guess.
    assert "LIVE" in hint


def test_id_absent_from_archive_yields_empty_string(tmp_path):
    """A live aspiration is not in the archive — add nothing."""
    _write_archive(tmp_path, [{"id": "asp-226", "status": "completed"}])
    assert aspirations_write._archived_aspiration_hint(tmp_path, "asp-250") == ""


def test_id_in_neither_store_yields_empty_string(tmp_path):
    _write_archive(tmp_path, [{"id": "asp-226", "status": "completed"}])
    assert aspirations_write._archived_aspiration_hint(tmp_path, "asp-99999") == ""


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda p: None, id="archive-file-missing"),
        pytest.param(
            lambda p: (p / "aspirations-archive.jsonl").write_text(
                "{not json\n", encoding="utf-8"
            ),
            id="archive-malformed",
        ),
        pytest.param(
            lambda p: (p / "aspirations-archive.jsonl").write_text(
                "", encoding="utf-8"
            ),
            id="archive-empty",
        ),
    ],
)
def test_miss_is_silent_never_a_nonexistence_claim(tmp_path, setup):
    """THE one-directional invariant.

    Every non-hit path must return the empty string exactly. A stale or
    unreadable mirror must never license wording that asserts the id is absent
    everywhere — the miss carries no such evidence (g-115-3541).
    """
    setup(tmp_path)
    assert aspirations_write._archived_aspiration_hint(tmp_path, "asp-226") == ""


def test_hint_concatenates_onto_the_404_detail(tmp_path):
    """The call site appends the hint to the existing detail string, so the
    hint must read as a continuation rather than a standalone sentence."""
    _write_archive(tmp_path, [{"id": "asp-226", "status": "completed"}])
    detail = "Aspiration asp-226 not found in world"
    combined = detail + aspirations_write._archived_aspiration_hint(
        tmp_path, "asp-226"
    )
    assert combined.startswith(detail)
    assert len(combined) > len(detail)
