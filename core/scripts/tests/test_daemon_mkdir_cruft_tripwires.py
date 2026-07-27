"""Tests for assert_not_cruft tripwire helper ().

Verifies the helper:
  (1) raises CruftPathRefused on POSIX-anchored drive-letter cruft shape
  (2) raises CruftPathRefused on NTFS-rewritten-colon cruft shape
  (3) does NOT raise on legitimate absolute paths (Windows + POSIX)
  (4) does NOT raise on legitimate relative paths
  (5) is wired into all known daemon mkdir sites (regression guard)

The protected sites are listed below; if a new daemon mkdir lands without
a corresponding assert_not_cruft, the LIST_SITES guard test fails.

Origin: 2026-05-21 audit closing the C-dir cruft loophole. The path
absolutization helper (_path_helpers.absolutize) was in place at the resolver
layer, but downstream daemon write helpers received Path objects via function
parameters and had no defense against a caller bypassing absolutize. Tripwires
turn that silent-failure mode into a loud refusal.
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from _path_helpers import assert_not_cruft, CruftPathRefused, looks_like_cruft


# ─── Helper behavior ────────────────────────────────────────────────────


def test_cruft_posix_anchored_drive_letter_raises():
    """Path("/repo/C:/Users/...") is the POSIX-anchored drive-letter cruft
    shape — Path(C:/...) returns RELATIVE on POSIX-flavored Python, and a
    naive `project_root / value` join produces this exact shape."""
    cruft = Path("/home/user/repo/C:/Users/foo")
    with pytest.raises(CruftPathRefused) as exc_info:
        assert_not_cruft(cruft, "test mkdir")
    assert "test mkdir" in str(exc_info.value)
    assert "cruft mirror" in str(exc_info.value).lower()


def test_cruft_ntfs_rewritten_colon_raises():
    """On NTFS, attempting to mkdir a literal 'C:' under a parent rewrites
    the colon to U+F03A (Windows private-use area). This shape MUST also
    fire the tripwire — it's the post-creation residue of the same bug."""
    cruft = Path("C:/repo/C/Users/foo")
    with pytest.raises(CruftPathRefused):
        assert_not_cruft(cruft, "test mkdir")


def test_clean_windows_absolute_passes():
    """Legitimate Windows absolute paths must not self-flag."""
    assert_not_cruft(Path("C:/Users/foo/bar"), "mkdir")  # no raise


def test_clean_posix_absolute_passes():
    """Legitimate POSIX absolute paths must not self-flag."""
    assert_not_cruft(Path("/home/user/project/agents/alpha/scripts"), "mkdir")


def test_clean_relative_passes():
    """Relative paths must not self-flag (the cruft shape has a leading
    abs-path, not relative content)."""
    assert_not_cruft(Path("agents/alpha/journal/2026.md"), "mkdir")


def test_default_operation_label():
    """The operation parameter defaults to 'write' when not specified."""
    with pytest.raises(CruftPathRefused) as exc_info:
        assert_not_cruft(Path("/repo/C:/x"))
    assert "write" in str(exc_info.value)


# ─── Regression guard: list of protected daemon mkdir sites ──────────


PROTECTED_SITES = [
    ("mind_api/src/endpoints/aspirations_write.py", "_atomic_write_jsonl"),
    ("mind_api/src/endpoints/aspirations_write.py", "_append_jsonl"),
    ("mind_api/src/endpoints/aspirations_write.py", "streak-break session dir"),
    ("mind_api/src/endpoints/aspirations_write.py", "meta_update"),
    ("mind_api/src/endpoints/board.py",              "board reads sidecar"),
    ("mind_api/src/endpoints/store.py",              "store._atomic_write_jsonl"),
    # mind_api/src/history.py removed : it no longer mkdirs anything —
    # snapshot() delegates to _fileops.save_history, whose _cruft_tripwire
    # covers the history-snapshot path (see _fileops.py save_history).
    ("mind_api/src/lifecycle.py",                    "runtime_dir"),
    ("mind_api/src/lifecycle.py",                    "_atomic_write_text"),
    ("mind_api/src/world/pipeline_write.py",         "pipeline_write._atomic_write_jsonl"),
    ("mind_api/src/world/pipeline_write.py",         "pipeline_write._append_to_archive"),
    ("mind_api/src/world/pipeline_write.py",         "pipeline_write meta refresh"),
    ("mind_api/src/world/pipeline_write.py",         "pipeline_write meta_update"),
    # Added 2026-06-03 — 7 sites that landed after the 2026-05-21 audit without
    # a tripwire (caught by test_no_unprotected_mkdir_in_daemon).
    ("mind_api/src/endpoints/curriculum.py",         "mkdir (curriculum write_yaml)"),
    ("mind_api/src/endpoints/curriculum.py",         "mkdir (curriculum append_jsonl)"),
    ("mind_api/src/endpoints/experience_write.py",   "mkdir (experience content_dir)"),
    ("mind_api/src/endpoints/wm_write.py",           "mkdir (wm_write)"),
    ("mind_api/src/meta/meta_yaml.py",               "mkdir (meta_yaml append_log)"),
    ("mind_api/src/meta/meta_yaml.py",               "mkdir (meta_yaml log endpoint)"),
    ("mind_api/src/meta/skill_evaluate.py",          "mkdir (skill_evaluate write_yaml)"),
]


def test_all_protected_sites_have_tripwire():
    """Regression guard: each known daemon mkdir site must have an
    assert_not_cruft call paired with it.

    If a new mkdir(parents=True) site lands in mind_api/src/ without a
    paired assert_not_cruft, this test should fail. Adding a new site:
      1. Add the assert_not_cruft(path, "<label>") line above the mkdir.
      2. Add the (file, label) tuple to PROTECTED_SITES above.
    """
    project_root = _SCRIPTS.parent.parent
    for rel_path, label in PROTECTED_SITES:
        full = project_root / rel_path
        content = full.read_text(encoding="utf-8")
        assert label in content, (
            f"Tripwire label {label!r} missing from {rel_path}. "
            f"Either remove the entry from PROTECTED_SITES or add the "
            f"assert_not_cruft call back."
        )


def test_no_unprotected_mkdir_in_daemon():
    """Every mkdir(parents=True, ...) in mind_api/src/ must have an
    assert_not_cruft call on the immediately-preceding non-empty line.

    Catches the case where someone adds a new mkdir site but forgets the
    tripwire. The test reads the source files and walks lines.
    """
    project_root = _SCRIPTS.parent.parent
    daemon_root = project_root / "mind_api" / "src"
    unprotected = []
    for py_file in daemon_root.rglob("*.py"):
        if "/__pycache__/" in str(py_file).replace("\\", "/"):
            continue
        lines = py_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if ".mkdir(parents=True" not in line:
                continue
            # Walk back to first non-empty, non-comment preceding line.
            j = i - 1
            while j >= 0 and (not lines[j].strip() or lines[j].lstrip().startswith("#")):
                j -= 1
            if j < 0 or "assert_not_cruft" not in lines[j]:
                unprotected.append(f"{py_file.relative_to(project_root)}:{i+1}")
    assert not unprotected, (
        f"Found {len(unprotected)} mkdir site(s) without preceding "
        f"assert_not_cruft tripwire:\n  " + "\n  ".join(unprotected)
    )
