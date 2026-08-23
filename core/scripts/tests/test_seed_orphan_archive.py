"""test_seed_orphan_archive.py — orphans are archived before deletion.

g-115-4471. seed-transplant's orphan sweep (mirror semantics: destination =
manifest AND source) removed destination files with a bare `path.unlink()` —
no enumeration, no archive, no verification, no receipt.

Why that is the archive-before-delete.md case and not a nice-to-have: an
orphan is a file the DESTINATION has and the SOURCE does not. It is untracked
at the destination by construction, so git holds no copy and the deletion is
unrecoverable. The sibling `do_backup()` does not cover it — that archives the
manifest include-set, i.e. precisely the files that get OVERWRITTEN, never the
ones that get DELETED. The two sets are disjoint by definition.

These tests pin the ENUMERATE -> ARCHIVE -> VERIFY -> DELETE -> RECEIPT
sequence, its fail-CLOSED behaviour, and the property that makes the archive
an archive rather than a staging area: it survives the NEXT sweep.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine_orphan_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)


MANIFEST = {"include": [{"path": "core/keep.py", "type": "file"}]}

ORPHAN_REL = "core/gone.py"
ORPHAN_BODY = "IRREPLACEABLE = 'untracked at dest; git has no copy'\n"


def _mk_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    return src


def _mk_dest(tmp_path: Path) -> Path:
    """Destination carrying one manifested file plus one ORPHAN."""
    dest = tmp_path / "dest"
    (dest / "core").mkdir(parents=True)
    (dest / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    (dest / ORPHAN_REL).write_bytes(ORPHAN_BODY.encode("utf-8"))
    return dest


def _graveyards(dest: Path):
    return sorted(p for p in dest.iterdir()
                  if p.is_dir() and p.name.startswith(".seed-backup-orphans-"))


def test_orphan_is_recoverable_after_removal(tmp_path):
    """THE goal requirement: the orphan is gone from dest and recoverable."""
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    res = _engine.do_remove_orphans(dest, MANIFEST, src)

    # Deleted from the destination.
    assert ORPHAN_REL in res["removed"]
    assert not (dest / ORPHAN_REL).exists()

    # ...and recoverable, byte-for-byte, from the graveyard.
    graves = _graveyards(dest)
    assert len(graves) == 1, f"expected exactly one graveyard, got {graves}"
    recovered = graves[0] / ORPHAN_REL
    assert recovered.exists(), "orphan was deleted without an archived copy"
    assert recovered.read_text(encoding="utf-8") == ORPHAN_BODY

    # Restoring it puts the original back exactly.
    (dest / ORPHAN_REL).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(recovered, dest / ORPHAN_REL)
    assert (dest / ORPHAN_REL).read_text(encoding="utf-8") == ORPHAN_BODY


def test_archive_is_verified_and_reported(tmp_path):
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    res = _engine.do_remove_orphans(dest, MANIFEST, src)
    arch = res["archive"]

    assert arch["archived"] is True
    assert arch["verified"] is True
    assert arch["failures"] == []
    assert arch["count"] == 1
    assert arch["bytes"] == len(ORPHAN_BODY.encode("utf-8"))
    assert Path(arch["path"]).is_dir()


def test_receipt_carries_checksums_and_restore_instructions(tmp_path):
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    _engine.do_remove_orphans(dest, MANIFEST, src)
    receipt_path = _graveyards(dest)[0] / "RECEIPT.json"
    assert receipt_path.exists(), "no receipt written alongside the archive"

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["enumerated"] == 1
    assert receipt["archived_verified"] == 1
    entry = receipt["entries"][0]
    assert entry["path"] == ORPHAN_REL
    assert entry["bytes"] == len(ORPHAN_BODY.encode("utf-8"))
    assert entry["sha256"] == _engine._sha256(ORPHAN_BODY.encode("utf-8"))
    # A receipt nobody can restore from is not a receipt.
    assert receipt["restore"]["how"]
    assert receipt["restore"]["do_not"]


def test_fails_closed_when_archiving_fails(monkeypatch, tmp_path):
    """If the archive cannot be written, delete NOTHING.

    Leaving an orphan in place is recoverable; deleting it without a verified
    copy is not. A partial sweep behind an incomplete archive is the exact
    failure this sequence exists to prevent.
    """
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    def _boom(*a, **kw):
        raise OSError("simulated archive failure")

    monkeypatch.setattr(_engine.shutil, "copy2", _boom)

    res = _engine.do_remove_orphans(dest, MANIFEST, src)

    assert res["removed"] == [], "deleted despite a failed archive"
    assert (dest / ORPHAN_REL).exists(), "orphan destroyed with no archive"
    assert res["archive"]["verified"] is False
    assert res["archive"]["failures"], "failure was not reported"


def test_verify_mismatch_blocks_deletion(monkeypatch, tmp_path):
    """A corrupted copy must not count as archived."""
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    real_copy2 = _engine.shutil.copy2

    def _corrupting_copy2(s, d, *a, **kw):
        real_copy2(s, d, *a, **kw)
        Path(d).write_text("CORRUPTED", encoding="utf-8")

    monkeypatch.setattr(_engine.shutil, "copy2", _corrupting_copy2)

    res = _engine.do_remove_orphans(dest, MANIFEST, src)

    assert res["removed"] == []
    assert (dest / ORPHAN_REL).exists()
    assert res["archive"]["verified"] is False
    stages = {f["stage"] for f in res["archive"]["failures"]}
    assert "verify-mismatch" in stages


def test_graveyard_survives_a_subsequent_sweep(tmp_path):
    """Outside the blast radius: the archive is not itself swept next run.

    The graveyard name starts with ".seed-backup-" so _is_preserved_at_dest
    protects it. Without that, round 2 would archive-and-delete round 1's
    archive, and the property would decay to a one-run staging area.
    """
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    _engine.do_remove_orphans(dest, MANIFEST, src)
    first = _graveyards(dest)[0]
    archived_copy = first / ORPHAN_REL
    assert archived_copy.exists()

    # A second orphan appears; sweep again.
    (dest / "core" / "gone2.py").write_text("SECOND = 2\n", encoding="utf-8")
    res2 = _engine.do_remove_orphans(dest, MANIFEST, src)

    assert "core/gone2.py" in res2["removed"]
    # Round 1's archive is untouched and was never re-reported as an orphan.
    assert archived_copy.exists(), "the archive was swept by the next run"
    assert archived_copy.read_text(encoding="utf-8") == ORPHAN_BODY
    assert not any(r.startswith(".seed-backup-orphans-") for r in res2["removed"])

    # ...and round 1's RECEIPT survives too. This assertion is the point of the
    # test, and its ABSENCE is how the original version passed against a real
    # bug: at second-resolution timestamps both sweeps landed in ONE graveyard
    # and sweep 2's RECEIPT.json silently overwrote sweep 1's. The archived FILE
    # kept existing — so a file-only assertion stayed green — while the row
    # naming it vanished, leaving an orphan copy nobody could attribute. That is
    # archive-before-delete.md's "an archive nobody can find or restore from is
    # not an archive", reached by a test that looked like it covered this.
    graves = _graveyards(dest)
    assert len(graves) == 2, (
        f"each sweep needs its OWN graveyard, got {[g.name for g in graves]} — "
        "a shared dir means one sweep's RECEIPT.json overwrote the other's"
    )
    receipts = [json.loads((g / "RECEIPT.json").read_text(encoding="utf-8"))
                for g in graves]
    listed = {e["path"] for r in receipts for e in r["entries"]}
    assert ORPHAN_REL in listed, "sweep 1's receipt row was lost"
    assert "core/gone2.py" in listed, "sweep 2's receipt row was lost"


def test_dry_run_neither_archives_nor_deletes(tmp_path):
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    res = _engine.do_remove_orphans(dest, MANIFEST, src, dry_run=True)

    assert ORPHAN_REL in res["removed"]          # reported...
    assert (dest / ORPHAN_REL).exists()          # ...but not deleted
    assert res["archive"]["archived"] is False
    assert _graveyards(dest) == []


def test_manifested_file_is_never_treated_as_an_orphan(tmp_path):
    """Positive control: the sweep still leaves in-manifest files alone."""
    src, dest = _mk_source(tmp_path), _mk_dest(tmp_path)

    res = _engine.do_remove_orphans(dest, MANIFEST, src)

    assert "core/keep.py" not in res["removed"]
    assert (dest / "core" / "keep.py").exists()
