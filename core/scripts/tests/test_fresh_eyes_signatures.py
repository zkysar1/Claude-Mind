"""One commit must carry one fresh-eyes content signature on every box.

git checks a commit out with CRLF line endings on a Windows box
(core.autocrlf=true) and with LF elsewhere. Signatures over raw bytes therefore
disagreed across boxes, and every cross-platform last_fresh_eyes_run record read
as an amend (fresh-eyes finding, 2026-09-28). _fresh_eyes_signatures.file_sig
reads CRLF as LF, and every writer and reader of a signature uses it: three
hand-mirrored copies of the hash had to change in lockstep before.
"""
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
