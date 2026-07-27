#!/usr/bin/env python3
"""Schema validator for meta/skill-gaps.yaml ().

Detective layer (defense-in-depth) guarding the two corruption classes that
rotted skill-gaps.yaml to zero gaps on 2026-05-26 and motivated the
meta-yaml.py list-value + dotpath hardening:

  1. gaps-as-string: meta-set.sh storing a JSON array through the (then)
     scalar-only parse_value wrote the whole ``gaps`` field as a STRING.
  2. orphan-key: bracket-index dotpaths (``gaps[N]``) created literal sibling
     keys (``gaps[6]``) instead of indexing the list.

The source fixes in meta-yaml.py (parse_value JSON-detect + navigate
bracket-normalization) PREVENT both at the write path. This validator CATCHES
residue from write paths those fixes do not cover — e.g. a direct hand-edit
introducing a duplicate id, or a value laundered in before the fix landed.

Invariants checked (structural only — does NOT hardcode the set of valid gap
fields, per guard-426):
  - no top-level key contains ``[`` (orphan bracket-key leak -> class 2)
  - top-level ``gaps`` exists and is a LIST (not a string -> class 1)
  - every gaps entry is a mapping carrying a non-empty string ``id``
  - gap ids are unique

Exit 0 = valid; exit 1 = corrupt (per-issue diagnostic on stderr); exit 2 =
could not load (missing file / unparseable YAML / PyYAML absent). Validates
meta/skill-gaps.yaml by default; pass an explicit path to validate any file
(used by the test fixtures).
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def validate(data) -> list[str]:
    """Return a list of human-readable issues. Empty list == valid."""
    issues: list[str] = []
    if not isinstance(data, dict):
        return [f"top-level YAML is {type(data).__name__}, expected a mapping"]

    # Class 2 — orphan bracket-keys. Bracket notation must never survive as a
    # literal top-level key; navigate() now normalizes it to a list index.
    for k in data:
        if isinstance(k, str) and "[" in k:
            issues.append(
                f"orphan key '{k}' — bracket-index dotpath created a literal "
                f"sibling key instead of a list overlay (orphan-key corruption)"
            )

    # Class 1 — gaps-as-string + structural shape.
    gaps = data.get("gaps")
    if gaps is None:
        issues.append("missing top-level 'gaps' key")
    elif isinstance(gaps, str):
        issues.append(
            f"'gaps' is a STRING ({gaps[:50]!r}...) — expected a list "
            f"(gaps-as-string corruption)"
        )
    elif not isinstance(gaps, list):
        issues.append(f"'gaps' is {type(gaps).__name__}, expected a list")
    else:
        ids: list[str] = []
        for i, g in enumerate(gaps):
            if not isinstance(g, dict):
                issues.append(f"gaps[{i}] is {type(g).__name__}, expected a mapping")
                continue
            gid = g.get("id")
            if not gid or not isinstance(gid, str):
                issues.append(f"gaps[{i}] is missing a non-empty string 'id'")
            else:
                ids.append(gid)
        seen: set[str] = set()
        dups: set[str] = set()
        for gid in ids:
            if gid in seen:
                dups.add(gid)
            seen.add(gid)
        for gid in sorted(dups):
            issues.append(f"duplicate gap id '{gid}'")

    return issues


def load_yaml(path: Path):
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: {path} is not parseable YAML: {e}", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        path = Path(argv[1])
    else:
        # Default target: meta/skill-gaps.yaml via the path resolver.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import META_DIR  # type: ignore
        if META_DIR is None:
            print("ERROR: META_DIR unresolved (set MIND_META or bind an agent "
                  "via /start). Pass an explicit path to validate a file.",
                  file=sys.stderr)
            return 2
        path = META_DIR / "skill-gaps.yaml"

    data = load_yaml(path)
    if data is None:
        data = {}
    issues = validate(data)
    if issues:
        print(f"FAIL: skill-gaps schema invalid — {len(issues)} issue(s) in {path}:",
              file=sys.stderr)
        for it in issues:
            print(f"  - {it}", file=sys.stderr)
        return 1
    print(f"PASS: skill-gaps schema valid ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
