"""transcript_archive.py — hermetic unit tests (, 2026-09-03).

Nothing here touches S3, the real home directory, or the live harness trees:
`harnesses()` is monkeypatched onto tmp_path fixtures and every destination is
a LocalDestination under tmp_path. The one thing deliberately NOT stubbed is
the archive->index->restore round trip, because byte-identity is the whole
product and a mocked destination would assert nothing about it.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_transcript_archive.py -q
"""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "transcript_archive", SCRIPT_DIR / "transcript_archive.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TA = load_mod()


@pytest.fixture
def dest(tmp_path):
    return TA.LocalDestination(tmp_path / "archive", None)


def _tree(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return root


@pytest.fixture
def live(tmp_path, monkeypatch):
    """Two fake harnesses: nested (claude-code shape) and flat (zak-code shape)."""
    a = _tree(tmp_path / "h_nested", {
        "proj-one/aaa.jsonl": '{"t":1}\n',
        "proj-one/subagents/bbb.jsonl": '{"t":2}\n',
        "proj-two/ccc.jsonl": '{"t":3}\n',
        "proj-two/notes.txt": "ignored, wrong glob",
    })
    b = _tree(tmp_path / "h_flat", {"ddd.jsonl": '{"t":4}\n'})
    monkeypatch.setattr(TA, "harnesses", lambda: [
        TA.Harness("nested", [tmp_path / "missing-root", a], "*.jsonl"),
        TA.Harness("flat", [b], "*.jsonl"),
    ])
    return a, b


# ── the safety invariant: transcripts must never reach git ──────────────────

def test_local_destination_refuses_root_inside_project_root(tmp_path):
    proj = tmp_path / "repo"
    (proj / "sub").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        TA.LocalDestination(proj / "sub" / "archive", proj)
    assert "INSIDE the repo working tree" in str(e.value)


def test_local_destination_allows_root_outside_project_root(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    d = TA.LocalDestination(tmp_path / "outside", proj)
    assert d.name == "local"


# ── harness discovery ───────────────────────────────────────────────────────

def test_discover_first_existing_root_wins_and_glob_filters(live):
    a, _ = live
    hs = {h.name: h for h in TA.harnesses()}
    assert hs["nested"].root() == a, "a missing first root must fall through"
    rels = sorted(rel for _, rel in hs["nested"].discover())
    assert rels == ["proj-one/aaa.jsonl", "proj-one/subagents/bbb.jsonl",
                    "proj-two/ccc.jsonl"], "notes.txt must not match *.jsonl"


def test_discover_yields_nothing_when_no_root_exists(tmp_path):
    h = TA.Harness("gone", [tmp_path / "nope"], "*.jsonl")
    assert h.root() is None and list(h.discover()) == []


def test_home_prefers_userprofile_over_msys_home(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "win"))
    monkeypatch.setenv("HOME", str(tmp_path / "msys"))
    assert TA._home() == tmp_path / "win"


# ── index integrity ─────────────────────────────────────────────────────────

def test_missing_index_is_empty_not_an_error(dest):
    idx = TA.load_index(dest, "BOX")
    assert idx["entries"] == {} and idx["machine"] == "BOX"


def test_corrupt_index_raises_never_reads_as_empty(dest, tmp_path):
    """An index that reads as empty would re-upload everything and call it success."""
    p = tmp_path / "archive" / TA.index_key("BOX")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        TA.load_index(dest, "BOX")


def test_index_key_shape():
    assert TA.index_key("BOX") == "transcripts/BOX/_index.json"


def test_unchanged_requires_archived_at_size_and_mtime():
    cur = {"size": 10, "mtime": 100.0}
    assert TA._unchanged({"archived_at": "t", "size": 10, "mtime": 100.0}, cur)
    assert not TA._unchanged({"size": 10, "mtime": 100.0}, cur), "never archived"
    assert not TA._unchanged({"archived_at": "t", "size": 11, "mtime": 100.0}, cur)
    assert not TA._unchanged({"archived_at": "t", "size": 10, "mtime": 101.0}, cur)


# ── the round trip: archive -> index -> restore, byte-identical ─────────────

def test_archive_then_restore_is_byte_identical(dest, live, tmp_path, capsys):
    a, _ = live
    rc = TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    assert rc == 0
    r = json.loads(capsys.readouterr().out)
    assert r["failed_count"] == 0 and r["archived_count"] == 4
    assert r["by_harness"] == {"nested": 3, "flat": 1}

    src = a / "proj-one" / "subagents" / "bbb.jsonl"
    out = tmp_path / "restored.jsonl"
    assert TA.do_restore(dest, "BOX", "nested/proj-one/subagents/bbb.jsonl", out) == 0
    assert out.read_bytes() == src.read_bytes()
    assert (hashlib.sha256(out.read_bytes()).hexdigest()
            == hashlib.sha256(src.read_bytes()).hexdigest())


def test_restore_of_unknown_key_refuses(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        TA.do_restore(dest, "BOX", "nested/nope.jsonl", tmp_path / "x")


def test_second_pass_skips_unchanged(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    r = json.loads(capsys.readouterr().out)
    assert r["archived_count"] == 0 and r["unchanged_skipped"] == 4


def test_appended_file_is_re_archived(dest, live, tmp_path, capsys):
    a, _ = live
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    p = a / "proj-two" / "ccc.jsonl"
    p.write_bytes(p.read_bytes() + b'{"t":5}\n')
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    r = json.loads(capsys.readouterr().out)
    assert r["archived_count"] == 1 and r["unchanged_skipped"] == 3
    out = tmp_path / "re.jsonl"
    TA.do_restore(dest, "BOX", "nested/proj-two/ccc.jsonl", out)
    assert out.read_bytes() == p.read_bytes()


def test_dry_run_writes_no_objects(tmp_path, live, capsys):
    d = TA.LocalDestination(tmp_path / "arch-dry", None)
    TA.do_archive(d, "BOX", tmp_path / "stage", None, True, True)
    r = json.loads(capsys.readouterr().out)
    assert r["dry_run"] is True
    assert not (tmp_path / "arch-dry" / "transcripts").exists(), \
        "a dry run must not create the index, the blobs or the receipt"


# ── deletion detection: the archive is a RECORD OF WHAT EXISTED ─────────────

def test_deletion_is_detected_and_the_bytes_survive(dest, live, tmp_path, capsys):
    a, _ = live
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    victim = a / "proj-one" / "aaa.jsonl"
    body = victim.read_bytes()
    victim.unlink()

    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    r = json.loads(capsys.readouterr().out)
    assert r["newly_deleted_detected"] == 1
    assert "nested/proj-one/aaa.jsonl" in r["newly_deleted_sample"]
    assert r["index_total_entries"] == 4, "a deleted entry is retained, not dropped"

    out = tmp_path / "recovered.jsonl"
    assert TA.do_restore(dest, "BOX", "nested/proj-one/aaa.jsonl", out) == 0
    assert out.read_bytes() == body, "the whole point: deleted, still recoverable"


def test_deletion_is_reported_once_not_every_run(dest, live, tmp_path, capsys):
    a, _ = live
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    (a / "proj-one" / "aaa.jsonl").unlink()
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    r = json.loads(capsys.readouterr().out)
    assert r["newly_deleted_detected"] == 0, "NEWLY deleted, not still-absent"


# ── verify + the index listing's truncation tell ────────────────────────────

def test_verify_passes_on_a_healthy_archive(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    assert TA.do_verify(dest, "BOX", 0, True) == 0
    v = json.loads(capsys.readouterr().out)
    assert v["checked"] == 4 and v["ok"] == 4 and v["mismatched"] == 0


def test_verify_catches_a_corrupted_blob(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    blob = tmp_path / "archive" / "transcripts" / "BOX" / "flat" / "ddd.jsonl"
    blob.write_bytes(b"tampered")
    assert TA.do_verify(dest, "BOX", 0, True) != 0
    v = json.loads(capsys.readouterr().out)
    assert v["mismatched"] == 1


def test_index_listing_declares_when_it_truncates(dest, tmp_path, monkeypatch, capsys):
    """A grep over a silently-capped listing returns a clean, wrong absence."""
    idx = {"version": TA.INDEX_VERSION, "machine": "BOX", "updated_at": None,
           "entries": {"a/%04d.jsonl" % i: {"size": 1} for i in range(250)}}
    TA.save_index(dest, "BOX", idx, tmp_path / "stage")
    TA.do_index(dest, "BOX", None, False)
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 250
    assert out["keys_listed"] == 200 and out["keys_truncated"] is True
    assert len(out["keys"]) == 200


def test_index_listing_reports_no_truncation_when_complete(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    TA.do_index(dest, "BOX", None, False)
    out = json.loads(capsys.readouterr().out)
    assert out["keys_truncated"] is False and out["count"] == 4


# ── destination selection ───────────────────────────────────────────────────

def test_resolve_uses_local_when_no_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("TRANSCRIPT_ARCHIVE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("MACHINE_ID", "PINNED")
    monkeypatch.setitem(sys.modules, "storage_backend", None)   # import raises
    d, machine = TA.resolve(tmp_path / "repo")
    assert d.name == "local" and machine == "PINNED"


def test_resolve_refuses_when_own_cloud_selected_but_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    monkeypatch.setitem(sys.modules, "storage_backend", None)
    with pytest.raises(SystemExit):
        TA.resolve(tmp_path / "repo")


def test_receipt_is_written_beside_the_archive(dest, live, tmp_path, capsys):
    TA.do_archive(dest, "BOX", tmp_path / "stage", None, False, True)
    capsys.readouterr()
    rec = list((tmp_path / "archive" / "transcripts" / "BOX" / "receipts").glob("RECEIPT-*.json"))
    assert len(rec) == 1, "archive-before-delete step 6: RECEIPT.* at the top level"
    assert json.loads(rec[0].read_text(encoding="utf-8"))["archived_count"] == 4
