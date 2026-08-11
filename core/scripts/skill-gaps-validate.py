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
  - every gap's ``status``, WHEN PRESENT, is declared in the ``gap_statuses``
    vocabulary of core/config/skill-gaps.yaml (g-115-3517)

The status check obeys guard-426 rather than breaking it: the vocabulary is
READ from the config SSOT, never copied here. It is what makes that block a
declaration with a consumer instead of schema weight (rb-335 —
writer-without-reader). Before it existed a new status could be coined ad hoc
at resolution time and no layer would notice; `satisfied-by-extension` was, and
the evolve forge filter silently mis-classified its gaps for a day.

A MISSING status is deliberately NOT an issue. Declaring the vocabulary does
not make the field mandatory, and guard-334 warns against schema weight beyond
what a writer actually emits — so this check constrains the values that ARE
written and says nothing about absence.

When the vocabulary cannot be loaded, the status check is SKIPPED and main()
says so on stderr. It never fails the run: this validator's job is corruption
detection, and an unreadable config is not corruption of the file under test.
Silence would be the wrong shape though — a skipped check that prints PASS
reads as "statuses validated" (guard-1760: report what you declined to look
for), hence the explicit NOTE.

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


# The status vocabulary SSOT. Derived from this file's own location (core/scripts
# -> core/config), not from PROJECT_ROOT, so it resolves identically under the
# importlib-by-path test harness and under a bare CLI run.
CONFIG_SKILL_GAPS = Path(__file__).resolve().parent.parent / "config" / "skill-gaps.yaml"


def load_status_vocabulary(path=None):
    """Return the ``gap_statuses`` map from the config SSOT, or None if unreadable.

    None is the caller's signal to SKIP the status check, never to fail — see
    the module docstring. Returns None (not {}) on an absent or empty block so
    "no vocabulary" and "an empty vocabulary that rejects everything" cannot be
    confused; the latter would turn a config typo into a full-corpus FAIL.
    """
    p = Path(path) if path is not None else CONFIG_SKILL_GAPS
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(cfg, dict):
        return None
    vocab = cfg.get("gap_statuses")
    return vocab if isinstance(vocab, dict) and vocab else None


def validate(data, status_vocabulary=None) -> list[str]:
    """Return a list of human-readable issues. Empty list == valid.

    ``status_vocabulary`` is the ``gap_statuses`` map from the config SSOT. When
    it is None the status check is skipped, so a caller wanting it must pass it
    explicitly — main() does. That keeps the structural checks callable without
    touching the filesystem (guard-652: the test harness stays hermetic).
    """
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
            # Status vocabulary (). Absence is fine; an UNDECLARED
            # value is not — that is a status coined ad hoc, which is exactly
            # how `satisfied-by-extension` became invisible to the forge filter.
            if status_vocabulary is not None and "status" in g:
                st = g.get("status")
                if st not in status_vocabulary:
                    issues.append(
                        f"gap '{gid or i}' has undeclared status {st!r} — declare "
                        f"it in gap_statuses of core/config/skill-gaps.yaml (with a "
                        f"writer, per guard-334) or correct the value. Declared: "
                        f"{', '.join(sorted(status_vocabulary))}"
                    )
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
    vocab = load_status_vocabulary()
    if vocab is None:
        # Never fatal — but never silent either, or a PASS below would read as
        # "statuses validated" when nothing looked at them (guard-1760).
        print(f"NOTE: status vocabulary unreadable at {CONFIG_SKILL_GAPS} — "
              f"status check SKIPPED (structural checks still ran)",
              file=sys.stderr)
    issues = validate(data, status_vocabulary=vocab)
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
