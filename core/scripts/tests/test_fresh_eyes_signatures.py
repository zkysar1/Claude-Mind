"""One commit must carry one fresh-eyes content signature on every box.

git checks a commit out with CRLF line endings on a Windows box
(core.autocrlf=true) and with LF elsewhere. Signatures over raw bytes therefore
disagreed across boxes, and every cross-platform last_fresh_eyes_run record read
as an amend (fresh-eyes finding, 2026-09-28). _fresh_eyes_signatures.file_sig
reads CRLF as LF, and every writer and reader of a signature uses it: three
hand-mirrored copies of the hash had to change in lockstep before.

world/ and meta/ paths resolve through _paths to the configured external
directories (g-115-11454): joined onto the repo root they named nothing, so no
world or meta file was ever signed and an amend to one after a review still
read as covered.
"""
import hashlib
import os
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))

import _fresh_eyes_coverage_check  # noqa: E402
import _fresh_eyes_signatures  # noqa: E402


def test_crlf_and_lf_checkouts_of_one_file_sign_the_same(tmp_path):
    (tmp_path / "lf.py").write_bytes(b"a = 1\nb = 2\n")
    (tmp_path / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    sigs = _fresh_eyes_signatures.compute_signatures(["lf.py", "crlf.py"], str(tmp_path))
    assert sigs["lf.py"] == sigs["crlf.py"]


def test_a_content_change_still_changes_the_signature(tmp_path):
    """NEGATIVE CONTROL: reading CRLF as LF must not blind amend detection."""
    (tmp_path / "a.py").write_bytes(b"a = 1\r\n")
    (tmp_path / "b.py").write_bytes(b"a = 2\r\n")
    sigs = _fresh_eyes_signatures.compute_signatures(["a.py", "b.py"], str(tmp_path))
    assert sigs["a.py"] != sigs["b.py"]


def test_an_unreadable_file_has_no_signature(tmp_path):
    """The reader falls back to path-only coverage for a path with no signature."""
    assert _fresh_eyes_signatures.file_sig("missing.py", str(tmp_path)) is None
    assert _fresh_eyes_signatures.compute_signatures(["missing.py"], str(tmp_path)) == {}


def test_writers_and_reader_share_one_signature_function():
    """The reader and the gate's WM writer import the canonical function, so a
    recorded signature is always compared with one computed the same way."""
    assert _fresh_eyes_coverage_check.file_sig is _fresh_eyes_signatures.file_sig
    gate = (CORE_SCRIPTS / "post-state-update-gate.sh").read_text(encoding="utf-8")
    assert "from _fresh_eyes_signatures import file_sig" in gate
    assert "def _file_sig" not in gate


# --- : world/ and meta/ are virtual prefixes ---------------------

def _external_dirs(tmp_path, monkeypatch):
    import _paths
    world, meta, repo = tmp_path / "world-ext", tmp_path / "meta-ext", tmp_path / "repo"
    for d in (world / "scripts", meta, repo):
        d.mkdir(parents=True)
    # file_sig imports _paths at call time, so pin the patched object (guard-1415)
    monkeypatch.setitem(sys.modules, "_paths", _paths)
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setattr(_paths, "META_DIR", meta)
    return world, meta, repo


def test_world_meta_and_repo_paths_each_get_a_signature(tmp_path, monkeypatch):
    world, meta, repo = _external_dirs(tmp_path, monkeypatch)
    (world / "scripts" / "x.py").write_bytes(b"w = 1\r\n")
    (meta / "m.yaml").write_bytes(b"m: 1\n")
    (repo / "r.py").write_bytes(b"r = 1\n")
    sigs = _fresh_eyes_signatures.compute_signatures(
        ["world/scripts/x.py", "meta/m.yaml", "r.py", "world/scripts/missing.py"], str(repo))
    assert set(sigs) == {"world/scripts/x.py", "meta/m.yaml", "r.py"}
    assert sigs["world/scripts/x.py"] == hashlib.sha1(b"w = 1\n").hexdigest()[:12]


def test_an_unconfigured_world_dir_means_no_signature_not_a_crash(tmp_path, monkeypatch):
    import _paths
    monkeypatch.setitem(sys.modules, "_paths", _paths)
    monkeypatch.setattr(_paths, "WORLD_DIR", None)
    assert _fresh_eyes_signatures.file_sig("world/scripts/x.py", str(tmp_path)) is None


def _amended_world_file_verdict(tmp_path, monkeypatch, sign):
    """Review a world file, amend it, and return the coverage verdict."""
    world, _, repo = _external_dirs(tmp_path, monkeypatch)
    p, f = "world/scripts/x.py", world / "scripts" / "x.py"
    f.write_bytes(b"v = 1\n")
    reviewed = sign(p, str(repo))
    record = {"files_set": {p}, "sigs": {p: reviewed} if reviewed else {}, "source": "own"}
    f.write_bytes(b"v = 2\n")
    return _fresh_eyes_coverage_check.evaluate_coverage([p], {p: sign(p, str(repo))}, [record])[3]


def test_an_amend_to_a_world_file_after_a_review_is_detected(tmp_path, monkeypatch):
    """Observed at the consumer (rb-6622): the coverage check stops counting
    the amended file as covered."""
    assert _amended_world_file_verdict(tmp_path, monkeypatch, _fresh_eyes_signatures.file_sig) == "no"


def test_mutation_root_joined_world_path_misses_the_amend(tmp_path, monkeypatch):
    """Teeth (rb-8706): joined onto the repo root, as before , the
    world file is never signed, so the same amend reads as covered. If this
    fails, the test above proves nothing."""
    def root_joined(rel_path, root):
        try:
            with open(os.path.join(root, rel_path), "rb") as fh:
                return hashlib.sha1(fh.read()).hexdigest()[:12]
        except OSError:
            return None
    assert _amended_world_file_verdict(tmp_path, monkeypatch, root_joined) == "yes:self"
