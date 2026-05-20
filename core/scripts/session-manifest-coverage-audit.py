#!/usr/bin/env python3
"""session-manifest-coverage-audit.py — verify pair-consumer entry-type coverage.

Asserts every distinct entry-type in core/config/session-manifest.yaml has a
handler branch in BOTH consumers:
  - core/scripts/session_snapshot.py    main() entry-type dispatch
  - core/scripts/session-manifest-clear.sh   inline-Python entry-type dispatch

Today the contract is enforced by guard-477 (LLM-side) only — a real script
catches drift mechanically.

Output: coverage table per entry-type with pass/fail per consumer.
Exit code: 0 if all pass, 1 if any miss, 2 on usage / parse failure.

Lineage: g-115-416, rb-728 (SSOT/re-derive), guard-477 (pair-consumer contract).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = ROOT / "core" / "config" / "session-manifest.yaml"
SNAPSHOT_PY = ROOT / "core" / "scripts" / "session_snapshot.py"
CLEAR_SH = ROOT / "core" / "scripts" / "session-manifest-clear.sh"

# Per entry-type, the regex that marks the corresponding branch in each
# consumer. A match means the consumer has a code path that recognizes that
# type. Non-match = drift (consumer dropped the branch while manifest still
# has entries of that type).
#
# Patterns chosen against verified live source (2026-05-08):
#   snapshot.py L196-198 — entry_type / is_dir_entry / is_glob_entry trio
#   clear.sh L63/L68/L76 — f.get("glob") / f.get("type") == "dir" / else+p.unlink
PATTERNS: dict[str, tuple[str, str]] = {
    "file": (
        # snapshot.py: file-existence predicate appears only in the non-glob branch
        r"\bp\.is_file\(\s*\)",
        # clear.sh: bare `p.unlink()` (no missing_ok) lives only in the file else branch
        r"^\s*p\.unlink\(\s*\)\s*$",
    ),
    "glob": (
        r"entry\.get\(\s*['\"]glob['\"]\s*\)",
        r"f\.get\(\s*['\"]glob['\"]\s*\)",
    ),
    "dir": (
        r"entry_type\s*==\s*['\"]dir['\"]",
        r"f\.get\(\s*['\"]type['\"]\s*\)\s*==\s*['\"]dir['\"]",
    ),
}


def classify_entry(entry: dict) -> str:
    """Map a manifest entry to a known entry type, or surface an unknown type.

    Returns 'glob' for glob:true, 'dir' for type:'dir', 'file' for
    type:'file' or absent type. Any OTHER `type` value is returned verbatim
    so the caller's PATTERNS lookup fails and the audit reports FAIL — this
    catches manifest entries with unrecognized types that no consumer has
    been wired to handle.
    """
    if entry.get("glob"):
        return "glob"
    t = entry.get("type")
    if t in (None, "file"):
        return "file"
    if t == "dir":
        return "dir"
    return t


def main() -> int:
    for path, label in (
        (MANIFEST_PATH, "manifest"),
        (SNAPSHOT_PY, "snapshot consumer"),
        (CLEAR_SH, "clear consumer"),
    ):
        if not path.exists():
            print(f"FAIL: {label} not found at {path}", file=sys.stderr)
            return 2

    try:
        manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f"FAIL: manifest YAML parse error: {e}", file=sys.stderr)
        return 2

    entries = (manifest or {}).get("files") or []
    if not entries:
        print("FAIL: manifest has no `files:` entries", file=sys.stderr)
        return 2

    present_types: dict[str, int] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        t = classify_entry(e)
        present_types[t] = present_types.get(t, 0) + 1

    snapshot_src = SNAPSHOT_PY.read_text(encoding="utf-8")
    clear_src = CLEAR_SH.read_text(encoding="utf-8")

    rows = []
    overall_pass = True
    for t in sorted(present_types):
        if t not in PATTERNS:
            # Unknown entry type in manifest — no audit pattern defined,
            # so consumers almost certainly don't handle it either.
            rows.append((t, present_types[t], False, False, "FAIL (unknown type)"))
            overall_pass = False
            continue
        snap_re, clear_re = PATTERNS[t]
        snap_hit = bool(re.search(snap_re, snapshot_src, flags=re.MULTILINE))
        clear_hit = bool(re.search(clear_re, clear_src, flags=re.MULTILINE))
        verdict = "PASS" if snap_hit and clear_hit else "FAIL"
        if verdict == "FAIL":
            overall_pass = False
        rows.append((t, present_types[t], snap_hit, clear_hit, verdict))

    print(
        f"Manifest entries: {len(entries)} | "
        f"Distinct entry-types: {len(present_types)} ({', '.join(sorted(present_types))})"
    )
    print()
    header = f"{'entry_type':<12}{'manifest_count':>16}{'snapshot':>12}{'clear':>10}{'verdict':>10}"
    print(header)
    print("-" * len(header))
    for t, count, snap_hit, clear_hit, verdict in rows:
        snap_mark = "OK" if snap_hit else "MISS"
        clear_mark = "OK" if clear_hit else "MISS"
        print(f"{t:<12}{count:>16}{snap_mark:>12}{clear_mark:>10}{verdict:>10}")

    extra_handler_types = set(PATTERNS.keys()) - set(present_types)
    if extra_handler_types:
        print()
        print(
            f"Note: consumers may handle types {sorted(extra_handler_types)} "
            "not currently present in manifest (informational)."
        )

    print()
    if overall_pass:
        print(
            f"PASS: every distinct entry-type ({len(present_types)}) in "
            "session-manifest.yaml has a handler in both consumers."
        )
        return 0
    print(
        "FAIL: at least one entry-type is missing a handler in one or both "
        "consumers — see verdict column. Drift from guard-477."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
