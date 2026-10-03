"""Tests for the retention of a forgotten learned item ()."""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

import pytest

import knowledge_retention as kr

NOW = datetime.datetime(2026, 10, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _record(item_id: str = "reefs", content: bytes = b"Reefs are warm.\n", **restore) -> dict:
    return kr.build_record("node", item_id, content, restore=restore or {"parent": None}, now=NOW)


def test_a_record_carries_the_text_its_digest_and_a_thirty_day_window() -> None:
    record = _record(content="Reefs are warm, café.\n".encode("utf-8"))

    assert record["undo_until"] == "2026-11-01T12:00:00+00:00"
    assert record["forgotten_at"] == "2026-10-02T12:00:00+00:00"
    assert kr.RETENTION_DAYS == 30
    assert kr.retained_content(record) == "Reefs are warm, café.\n".encode("utf-8")
    assert kr.is_live(record)


def test_the_file_name_is_a_digest_so_an_id_is_never_a_path(tmp_path: Path) -> None:
    hostile = "../../escape"

    path = kr.record_path(tmp_path, "node", hostile)

    assert path.parent == tmp_path
    assert hostile not in path.name and path.name.startswith("node-") and path.suffix == ".json"
    assert kr.record_name("node", "reefs") != kr.record_name("guardrail", "reefs"), "kind is part of the name"
    assert kr.record_name("node", "reefs") == kr.record_name("node", "reefs"), "and it is deterministic"


def test_a_written_record_reads_back_equal_and_a_date_in_restore_is_stored_as_text(tmp_path: Path) -> None:
    record = _record(index_entry={"last_updated": datetime.date(2026, 1, 1)}, parent="acme")

    path = kr.write_record(tmp_path / "retention", record)

    assert kr.read_record(path) == record
    assert record["restore"]["index_entry"]["last_updated"] == "2026-01-01"


def test_a_record_whose_text_fails_its_digest_reads_as_nothing(tmp_path: Path) -> None:
    path = kr.write_record(tmp_path, _record())
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["content_b64"] = "QW5vdGhlciBwYWdlLg=="
    path.write_text(json.dumps(tampered), encoding="utf-8")

    assert kr.read_record(path) is None
    assert list(kr.list_records(tmp_path)) == []


def test_a_record_of_another_schema_or_a_malformed_one_reads_as_nothing(tmp_path: Path) -> None:
    other = tmp_path / "node-0000000000000000.json"
    other.write_text(json.dumps({**_record(), "schema": 99}), encoding="utf-8")
    broken = tmp_path / "node-1111111111111111.json"
    broken.write_text("{not json", encoding="utf-8")

    assert kr.read_record(other) is None
    assert kr.read_record(broken) is None
    assert kr.read_record(tmp_path / "missing.json") is None


def test_a_record_that_names_no_item_or_has_unusable_restore_data_reads_as_nothing(tmp_path: Path) -> None:
    """The digest covers the retained bytes only, so each of these passes it: the shape check is
    what refuses them, and one such file must not stop the records beside it from being read."""
    good = kr.write_record(tmp_path, _record("reefs"))
    for name, damage in (("no-item", lambda r: r.pop("item_id")),
                         ("empty-item", lambda r: r.update(item_id="")),
                         ("no-kind", lambda r: r.pop("kind")),
                         ("restore-not-a-mapping", lambda r: r.update(restore=["parent"]))):
        record = _record("tides")
        damage(record)
        (tmp_path / f"node-{name}.json").write_text(json.dumps(record), encoding="utf-8")
        assert kr.read_record(tmp_path / f"node-{name}.json") is None, name

    assert [r["item_id"] for _path, r in kr.list_records(tmp_path)] == ["reefs"]
    assert kr.read_record(good) is not None


def test_a_write_that_does_not_read_back_is_an_error_not_a_record(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(kr, "read_record", lambda _path: None)

    with pytest.raises(ValueError, match="did not read back"):
        kr.write_record(tmp_path, _record())


def test_the_window_is_inclusive_and_an_unreadable_bound_is_outside_it() -> None:
    record = _record()
    until = datetime.datetime(2026, 11, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

    assert kr.in_window(record, NOW)
    assert kr.in_window(record, until)
    assert not kr.in_window(record, until + datetime.timedelta(seconds=1))
    assert not kr.in_window({}, NOW)
    assert not kr.in_window({"undo_until": "soon"}, NOW)
    # A bound with no zone is read as UTC, so a hand-edited record cannot raise.
    assert kr.in_window({"undo_until": "2026-11-01T12:00:00"}, NOW)


def test_listing_skips_what_is_not_a_record_and_a_missing_directory_lists_nothing(tmp_path: Path) -> None:
    kr.write_record(tmp_path, _record("reefs"))
    kr.write_record(tmp_path, _record("tides"))
    (tmp_path / "notes.txt").write_text("not a record", encoding="utf-8")
    (tmp_path / "node-2222222222222222.json").write_text("{", encoding="utf-8")

    listed = [record["item_id"] for _path, record in kr.list_records(tmp_path)]

    assert sorted(listed) == ["reefs", "tides"]
    assert list(kr.list_records(tmp_path / "nowhere")) == []


def test_marking_a_record_undone_drops_the_text_in_place_and_keeps_who_and_when(tmp_path: Path) -> None:
    record = _record(content=b"The text that must not outlive the undo.\n")
    path = kr.write_record(tmp_path, record)

    kr.mark_undone(path, record, NOW + datetime.timedelta(days=2))

    marker = kr.read_record(path)
    assert marker == {"schema": kr.SCHEMA, "kind": "node", "item_id": "reefs",
                      "forgotten_at": "2026-10-02T12:00:00+00:00",
                      "undone_at": "2026-10-04T12:00:00+00:00"}
    assert not kr.is_live(marker)
    assert b"must not outlive" not in path.read_bytes()
    assert len(list(tmp_path.iterdir())) == 1, "replaced in place: no second copy is left behind"


# --- strictly_after: the stamp that orders a forget against an undo -----------

CLOCK = datetime.datetime(2026, 10, 3, 12, 0, 0, 250000, tzinfo=datetime.timezone.utc)
CLOCK_SECOND = datetime.datetime(2026, 10, 3, 12, 0, 0, tzinfo=datetime.timezone.utc)
ONE_SECOND = datetime.timedelta(seconds=1)


def test_a_stamp_with_no_prior_is_the_clock_in_whole_utc_seconds() -> None:
    offset = datetime.timezone(datetime.timedelta(hours=2))

    assert kr.strictly_after(CLOCK) == CLOCK_SECOND
    assert kr.strictly_after(CLOCK.astimezone(offset)) == CLOCK_SECOND
    assert kr.strictly_after(CLOCK.astimezone(offset)).utcoffset() == datetime.timedelta(0)


@pytest.mark.parametrize("prior, expected", [
    ("2026-10-03T12:00:00+00:00", "2026-10-03T12:00:01+00:00"),  # the same second: a tie would hide
    ("2026-10-03T12:00:00.900000+00:00", "2026-10-03T12:00:01+00:00"),  # a fraction still ahead
    ("2026-10-03T13:00:00+00:00", "2026-10-03T13:00:01+00:00"),  # a clock that runs behind
    ("2026-10-03T14:00:00+02:00", "2026-10-03T12:00:01+00:00"),  # another offset, the same instant
    ("2026-10-03T12:00:00", "2026-10-03T12:00:01+00:00"),  # a naive stamp reads as UTC
])
def test_a_stamp_is_moved_one_second_past_a_prior_it_does_not_clear(prior: str, expected: str) -> None:
    assert kr.strictly_after(CLOCK, prior).isoformat() == expected


@pytest.mark.parametrize("prior", ["2026-10-03T11:59:59+00:00", "2025-01-01T00:00:00+00:00"])
def test_a_prior_behind_the_clock_changes_nothing(prior: str) -> None:
    assert kr.strictly_after(CLOCK, prior) == CLOCK_SECOND


@pytest.mark.parametrize("prior", [
    None, "", "not a time", "2026-13-45T00:00:00", 12345, b"x",
    # A stamp nothing can follow, or one a UTC conversion overflows: a corrupt record must not
    # stop a forget or an undo.
    "9999-12-31T23:59:59+00:00", "9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00",
])
def test_a_prior_that_is_missing_or_does_not_parse_is_ignored(prior) -> None:
    assert kr.strictly_after(CLOCK, prior) == CLOCK_SECOND


def test_the_latest_of_several_priors_decides_and_repeated_calls_climb_a_second_each() -> None:
    priors = ("2026-10-03T00:00:00+00:00", "2026-10-03T12:00:02+00:00", None, "2026-10-03T12:00:01+00:00")

    assert kr.strictly_after(CLOCK, *priors) == CLOCK_SECOND + 3 * ONE_SECOND
    stamps, last = [], None
    for _ in range(3):
        last = kr.strictly_after(CLOCK, last.isoformat() if last else None)
        stamps.append(last)
    assert stamps == [CLOCK_SECOND, CLOCK_SECOND + ONE_SECOND, CLOCK_SECOND + 2 * ONE_SECOND]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no tzset on this platform")
def test_a_naive_prior_is_read_as_utc_whatever_the_local_zone_is() -> None:
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        assert kr.strictly_after(CLOCK, "2026-10-03T12:00:00").isoformat() == "2026-10-03T12:00:01+00:00"
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()
