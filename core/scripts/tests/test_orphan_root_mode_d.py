"""Test Mode D cruft detection for orphan-root-sweep ().

Mode D residue: PROJECT_ROOT top-level entries whose names encode a
stale-daemon path-resolution failure. Two shapes:
  1. Name contains U+F03A (NTFS-remapped colon)
  2. Drive-letter-segment shape (e.g., `C`, `C:`)

The bash sweeper at `core/scripts/orphan-root-sweep.sh` Scan 4 calls
`_orphan_root_helpers.is_mode_d_cruft` so the predicate is the single
source of truth. These tests pin the predicate against regression.

See world/knowledge/tree/system/system-constraints-loop/external-path-resolution-cruft.md
Mode D + rb-939 + guard-554 for the diagnostic protocol after detection.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _orphan_root_helpers import is_mode_d_cruft  # noqa: E402

NTFS_REMAPPED_COLON = ""


# ─── Shape 1: U+F03A presence ───────────────────────────────────────────


def test_u_f03a_alone_detected():
    assert is_mode_d_cruft(NTFS_REMAPPED_COLON) is True


def test_letter_plus_u_f03a_detected():
    assert is_mode_d_cruft(f"C{NTFS_REMAPPED_COLON}") is True
    assert is_mode_d_cruft(f"D{NTFS_REMAPPED_COLON}") is True
    assert is_mode_d_cruft(f"z{NTFS_REMAPPED_COLON}") is True


def test_u_f03a_anywhere_detected():
    # The canonical incident is letter-prefix but the helper accepts U+F03A
    # anywhere in the name — covers any future shape that contains it.
    assert is_mode_d_cruft(f"prefix{NTFS_REMAPPED_COLON}suffix") is True
    assert is_mode_d_cruft(f"{NTFS_REMAPPED_COLON}leading") is True


# ─── Shape 2: drive-letter-segment shape ────────────────────────────────


def test_single_uppercase_letter_detected():
    assert is_mode_d_cruft("C") is True
    assert is_mode_d_cruft("D") is True
    assert is_mode_d_cruft("Z") is True


def test_single_lowercase_letter_detected():
    assert is_mode_d_cruft("c") is True
    assert is_mode_d_cruft("d") is True


def test_letter_plus_literal_colon_detected():
    # Literal `C:` cannot exist on NTFS (OS rewrites to U+F03A), but a
    # POSIX-flavored Python failure could mirror it under cwd as `C:`.
    assert is_mode_d_cruft("C:") is True
    assert is_mode_d_cruft("c:") is True
    assert is_mode_d_cruft("D:") is True


# ─── Negative cases: must NOT trigger ───────────────────────────────────


def test_empty_string_not_detected():
    assert is_mode_d_cruft("") is False


def test_two_letters_not_detected():
    # Two letters is a common short-name shape (e.g., `os`, `ui`) — never cruft.
    assert is_mode_d_cruft("CC") is False
    assert is_mode_d_cruft("os") is False
    assert is_mode_d_cruft("ui") is False


def test_normal_directory_names_not_detected():
    for name in ("alpha", "bravo", "charlie", "delta", "world", "meta",
                 "core", "scripts", "tests", "knowledge"):
        assert is_mode_d_cruft(name) is False, f"false-positive on {name!r}"


def test_letter_plus_non_colon_not_detected():
    # `C` followed by anything other than `:` or U+F03A — not Mode D.
    for name in ("C@", "C/", "C.", "CD", "C-", "C_"):
        assert is_mode_d_cruft(name) is False, f"false-positive on {name!r}"


def test_digit_or_symbol_not_detected():
    assert is_mode_d_cruft("1") is False
    assert is_mode_d_cruft("$") is False
    assert is_mode_d_cruft(":") is False
    assert is_mode_d_cruft("-") is False


def test_canonical_incident_shape():
    # The bytes `\xef\x80\xba` are the UTF-8 encoding of U+F03A.
    # This test pins the exact shape seen in git status as `C\357\200\272/`.
    canonical = b"C\xef\x80\xba".decode("utf-8")
    assert canonical == f"C{NTFS_REMAPPED_COLON}"
    assert is_mode_d_cruft(canonical) is True
