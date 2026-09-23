"""Whole-segment age-cap for the gate-firings store ().

FIXTURE-BASED ON PURPOSE. The live population has ZERO expired segments and
will until 2026-09-26 (oldest segment 2026-08-17 + retention_days 40), so a
test that waits for real supply would never run. echo's handoff note on
g-358-10 says exactly this: "whoever picks this up next should plan for a
fixture rather than expecting a live expired segment." Every date below is
synthetic and `--today` is pinned, so these assertions do not drift with the
calendar -- a test whose verdict changes with the wall clock is not a test.

THE DIRECTION THAT MATTERS. This module DELETES. The cheap half is proving it
removes what it should; the load-bearing half is proving it does NOT remove
what it should not, and that it refuses entirely when its recovery layer is
absent. Both directions are asserted below.
"""
import datetime as _dt
import os
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# guard-955: pin the backend BEFORE importing anything that resolves one. An
# own-cloud box would otherwise derive an S3 key from the customer prefix --
# NOT from the tmp dir -- and a tmp write would collide on the production key.
os.environ["STORAGE_BACKEND"] = "local"

import importlib.util  # noqa: E402

# The script is a kebab-case CLI (matching gate-firings-flush.py), so it is not
# importable by name; load it by path, the same idiom the sibling tests use.
_SPEC = importlib.util.spec_from_file_location(
    "gate_firings_segments_expire", _SCRIPTS / "gate-firings-segments-expire.py")
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _seg(meta: pathlib.Path, day: str, body: str = '{"gate":"x"}\n') -> pathlib.Path:
    p = meta / f"gate-firings-{day}.jsonl"
    p.write_text(body, encoding="utf-8")
    return p


def _hygiene(tmp: pathlib.Path, days: int = 40) -> pathlib.Path:
    """Minimal store-hygiene.yaml carrying the G5 entry this script keys on,
    plus a decoy entry before it so the path-anchored scan is exercised rather
    than a "first retention_days wins" accident."""
    p = tmp / "store-hygiene.yaml"
    p.write_text(
        "stores:\n"
        "  - path: world/changelog.jsonl\n"
        "    mode: rotate\n"
        "    retention_days: 9999\n"
        f"  - path: {mod._G5_PATH_KEY}\n"
        "    enabled: true\n"
        "    mode: cap\n"
        "    by: age\n"
        f"    retention_days: {days}\n"
        "    ts_field: ts\n",
        encoding="utf-8")
    return p


# --- shape: what IS and IS NOT a segment ----------------------------------

@pytest.mark.parametrize("name,expected", [
    ("gate-firings-2026-08-17.jsonl", _dt.date(2026, 8, 17)),
    ("gate-firings-2026-01-01.jsonl", _dt.date(2026, 1, 1)),
    # The machine-local spool shares the stem and is NOT part of the shared
    # store. Deleting it would drop firings that have not been flushed yet.
    ("gate-firings.spool.jsonl", None),
    # The hyphenated spool form: the loose glob admitted it once already
    # (_gate_log's own comment records that regression), so it is pinned here.
    ("gate-firings-spool.jsonl", None),
    ("gate-firings.jsonl", None),          # legacy file -- G5 already owns it
    ("gate-firings-2026-08-17.jsonl.bak", None),
    ("gate-firings-2026-8-17.jsonl", None),  # unpadded: not the writer's shape
    ("changelog.jsonl", None),
])
def test_only_real_date_segments_are_recognised(name, expected):
    assert mod.segment_date(name) == expected


def test_segment_shape_is_delegated_not_recopied():
    """The matcher must BE _gate_log's, not a lookalike. If a future edit
    inlines a private regex here, the writer and this reader can drift and the
    failure is silent -- the writer keeps producing files this script no longer
    recognises, so the store grows while the report says zero."""
    import _gate_log
    assert mod.segment_date(_gate_log.segment_name(_dt.date(2026, 5, 4))) == \
        _dt.date(2026, 5, 4)


# --- policy: retention_days is read, never assumed ------------------------

def test_retention_days_reads_the_g5_entry_not_the_first_one(tmp_path):
    assert mod.retention_days(_hygiene(tmp_path, days=40)) == 40


def test_retention_days_raises_when_the_entry_is_absent(tmp_path):
    """Fail-CLOSED. A default here would delete against a window nobody
    declared -- the one direction this script must never take."""
    p = tmp_path / "store-hygiene.yaml"
    p.write_text("stores:\n  - path: world/changelog.jsonl\n    mode: rotate\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="retention_days not found"):
        mod.retention_days(p)


def test_live_config_still_carries_the_entry():
    """SSOT control against the REAL config: if G5 is renamed or its key moves,
    this fails here rather than silently reporting zero expired forever."""
    assert mod.retention_days(mod._repo_root() / mod._DEFAULT_HYGIENE) > 0


# --- selection: the boundary ----------------------------------------------

def test_boundary_day_is_kept_strictly_older_is_expired(tmp_path):
    meta = tmp_path / "meta"; meta.mkdir()
    _seg(meta, "2026-07-01")   # older than cutoff -> expired
    _seg(meta, "2026-08-12")   # exactly ON cutoff  -> KEPT
    _seg(meta, "2026-08-13")   # newer             -> kept
    cutoff = _dt.date(2026, 8, 12)
    assert [p.name for _, p in mod.expired_segments(meta, cutoff)] == \
        ["gate-firings-2026-07-01.jsonl"]


# --- the dry run must not touch anything ----------------------------------

def test_dry_run_reports_but_deletes_nothing(tmp_path, capsys):
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    old = _seg(meta, "2026-07-01")
    new = _seg(meta, "2026-09-20")
    rc = mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
                   "--today", "2026-09-21"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert rep["action"] == "would-delete"
    assert rep["expired"] == ["gate-firings-2026-07-01.jsonl"]
    assert rep["applied"] is False
    assert old.exists() and new.exists(), "dry run must not delete"


def test_zero_expired_reports_the_unfiltered_population_beside_it(tmp_path, capsys):
    """guard-2298: a bare `expired_count: 0` cannot be told apart from a probe
    that found nothing at all. The live store's real reading today is 36
    present / 0 expired, and that pairing is the whole evidence."""
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    _seg(meta, "2026-09-19"); _seg(meta, "2026-09-20")
    mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
              "--today", "2026-09-21"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["expired_count"] == 0 and rep["segments_present"] == 2
    assert rep["oldest_segment"] == "gate-firings-2026-09-19.jsonl"
    assert rep["action"] == "noop"


# --- apply refuses without a recovery layer -------------------------------

def test_apply_without_archive_dir_is_refused_and_deletes_nothing(tmp_path, capsys):
    """archive-before-delete step 2. .history is blacklisted for this store
    (guard-3095) and a LocalBackend delete is final, so the independent
    current-copy archive is mandatory, not preferable."""
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    old = _seg(meta, "2026-07-01")
    rc = mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
                   "--today", "2026-09-21", "--apply"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert rep["action"] == "refused-no-archive-dir"
    assert old.exists(), "a refused apply must leave the store untouched"


# --- apply: archive, verify, delete, receipt ------------------------------

def test_apply_archives_verifies_deletes_and_writes_a_receipt(tmp_path, capsys):
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    arch = tmp_path / "graveyard"
    old_a = _seg(meta, "2026-07-01", '{"gate":"a"}\n')
    old_b = _seg(meta, "2026-07-02", '{"gate":"b"}\n')
    keep = _seg(meta, "2026-09-20", '{"gate":"keep"}\n')

    rc = mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
                   "--today", "2026-09-21", "--apply", "--archive-dir", str(arch)])
    rep = json.loads(capsys.readouterr().out)

    assert rc == 0 and rep["action"] == "deleted" and rep["errors"] == []
    # deleted from the store...
    assert not old_a.exists() and not old_b.exists()
    # ...and the in-window segment is untouched, with its bytes intact
    assert keep.exists() and keep.read_text(encoding="utf-8") == '{"gate":"keep"}\n'
    # ...and recoverable, byte-for-byte, from the archive
    assert (arch / old_a.name).read_text(encoding="utf-8") == '{"gate":"a"}\n'
    assert (arch / old_b.name).read_text(encoding="utf-8") == '{"gate":"b"}\n'

    # RECEIPT.md at the archive TOP LEVEL, under the exact name the one reader
    # (temp-drain-purge.sh) preserves on.
    receipt = arch / "RECEIPT.md"
    assert receipt.exists() and rep["receipt"] == str(receipt)
    body = receipt.read_text(encoding="utf-8")
    for name in (old_a.name, old_b.name):
        assert name in body, "the receipt must enumerate what it destroyed"
    assert "retention_days=40" in body and "2026-08-12" in body
    assert "RESTORE:" in body


def test_archive_verify_failure_aborts_before_any_delete(tmp_path, capsys,
                                                         monkeypatch):
    """The load-bearing failure path. If the archive cannot be verified, the
    run must abort with the store INTACT -- never delete-then-discover."""
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    old_a = _seg(meta, "2026-07-01")
    old_b = _seg(meta, "2026-07-02")

    def boom(src, archive_dir):
        raise OSError("simulated archive verify failure")

    monkeypatch.setattr(mod, "_archive_one", boom)
    rc = mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
                   "--today", "2026-09-21", "--apply",
                   "--archive-dir", str(tmp_path / "gy")])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 1 and rep["action"] == "aborted-archive-failed"
    assert old_a.exists() and old_b.exists(), "abort must leave everything"
    assert rep["deleted"] == []


def test_apply_with_nothing_expired_is_a_noop(tmp_path, capsys):
    import json
    meta = tmp_path / "meta"; meta.mkdir()
    keep = _seg(meta, "2026-09-20")
    rc = mod.main(["--meta-dir", str(meta), "--hygiene", str(_hygiene(tmp_path)),
                   "--today", "2026-09-21", "--apply",
                   "--archive-dir", str(tmp_path / "gy")])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["action"] == "noop" and keep.exists()
    assert not (tmp_path / "gy").exists(), "a noop must not create an archive dir"
