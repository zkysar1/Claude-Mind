#!/usr/bin/env python3
"""g-115-895 — Migrate daemon wrapper response handling from argv to stdin.

Mechanical sweep across `core/scripts/*.sh` that use the pattern:

    $(rt_python_launcher) -c "
    ...
    _src = sys.argv[1]                              # RAW_ASSIGN variant
    OR
    resp = json.loads(sys.argv[1])                  # LOADS_ARGV variant
    ...
    " "$RESPONSE"

and rewrites it to:

    printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
    ...
    _src = sys.stdin.read()
    OR
    resp = json.load(sys.stdin)
    ...
    "

Background: Windows argv limit is ~32KB. Large daemon responses (e.g.,
asp-265 archive ~57KB) trigger E2BIG at exec time AFTER the daemon-side
write succeeded — wrapper exits non-zero from the print failure, leaving
callers thinking the op failed. Routing the response via stdin removes
the length limit; daemon HTTP POST already has no such constraint.

Canonical fix: g-115-772 (aspirations-complete.sh, 2026-05-13).
This sweep extends the same fix to the 34 remaining wrappers.

Idempotent: skips wrappers that already use the stdin pattern. Safe to
re-run after partial application.

Usage:
    py -3 core/scripts/migrate-argv-to-stdin.py --check     # dry-run
    py -3 core/scripts/migrate-argv-to-stdin.py --apply     # write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE

# Match the `$(rt_python_launcher) -c "<...>" "$RESPONSE"` block.
# Captures: indent before $(rt_python_launcher), the heredoc-style Python
# source, the closing fence + " "$RESPONSE" "  (or "$RESP" with possible
# spaces).
#
# The Python block is bounded by double-quotes that contain newlines —
# we anchor on the opening `$(rt_python_launcher) -c "` and the closing
# `" "$RESPONSE"` (or any "$<var>" final argv).
CALL_RE = re.compile(
    # Group 1: leading whitespace (indent)
    r"^(?P<indent>[ \t]*)"
    # Literal $(rt_python_launcher) -c "
    r"\$\(rt_python_launcher\)\s+-c\s+\"\n"
    # Group 2: the Python source (everything until the closing "<arg>")
    r"(?P<src>.*?)"
    # Closing fence: a line with just " (closing the python source quote)
    # followed by whitespace and "$VAR" (variable holding the daemon
    # response). Accepts:
    #   - "$RESPONSE", "$RESP", "$BODY", etc. (uppercase env-style)
    #   - "$1", "$2" (positional args — used by function-style wrappers
    #     like guardrails-add.sh _print_record)
    r"\"\s+\"(?P<respvar>\$(?:[A-Z_][A-Z0-9_]*|[0-9]+))\"\s*$",
    re.MULTILINE | re.DOTALL,
)


def _convert_python_src(src: str) -> str:
    """Convert the Python source from argv-reading to stdin-reading."""
    out = src
    # LOADS_ARGV variant: json.loads(sys.argv[1])
    out = out.replace("json.loads(sys.argv[1])", "json.load(sys.stdin)")
    # RAW_ASSIGN variant: _src = sys.argv[1]
    out = out.replace("_src = sys.argv[1]", "_src = sys.stdin.read()")
    # Generic last-resort: any other sys.argv[1] reference becomes
    # sys.stdin.read() (covers edge cases).
    out = re.sub(r"\bsys\.argv\[1\]\b", "sys.stdin.read()", out)
    return out


def migrate_file(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Migrate one wrapper. Returns (sites_rewritten, before-snippets).

    Idempotent: if no argv-pattern matches were found AND the file
    contains json.load(sys.stdin), assume already-migrated and return 0.
    If matches exist alongside json.load(sys.stdin), only rewrite the
    matching sites (partial migrations are healed by re-running).
    """
    src = path.read_text(encoding="utf-8")
    matches = list(CALL_RE.finditer(src))
    if not matches:
        return 0, []

    rewrites: list[str] = []
    # Walk matches in reverse so position offsets stay stable as we splice.
    new_src = src
    for m in reversed(matches):
        indent = m.group("indent")
        py_src = m.group("src")
        respvar = m.group("respvar")
        py_new = _convert_python_src(py_src)
        if py_new == py_src and "sys.argv[1]" not in py_src:
            # No argv reference in this block — likely a different
            # wrapper shape (e.g., a Python block that doesn't read the
            # response). Skip without rewrite.
            continue
        replacement = (
            f'{indent}printf \'%s\' "{respvar}" | '
            f'$(rt_python_launcher) -c "\n'
            f'{py_new}'
            f'"'
        )
        start, end = m.start(), m.end()
        rewrites.append(
            f"site at offset {start}: respvar={respvar} "
            f"({len(py_src)} → {len(py_new)} src chars)"
        )
        new_src = new_src[:start] + replacement + new_src[end:]

    sites = len(rewrites)
    if sites > 0 and apply:
        path.write_text(new_src, encoding="utf-8")
    return sites, rewrites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run/check)")
    ap.add_argument("--check", action="store_true",
                    help="explicit dry-run (no writes)")
    ap.add_argument("--files", nargs="*",
                    help="explicit files (default: all *.sh in core/scripts)")
    args = ap.parse_args()
    if args.check and args.apply:
        print("ERROR: --check and --apply are mutually exclusive",
              file=sys.stderr)
        return 2
    apply = args.apply

    # Find target files: every .sh in core/scripts that contains the
    # rt_python_launcher -c pattern.
    if args.files:
        candidates = [Path(f).resolve() for f in args.files]
    else:
        candidates = sorted(SCRIPTS_DIR.glob("*.sh"))
    targets = [
        p for p in candidates
        if p.exists() and "rt_python_launcher" in p.read_text(encoding="utf-8")
    ]
    print(f"Scanning {len(targets)} wrappers in {SCRIPTS_DIR}", file=sys.stderr)

    total_sites = 0
    migrated = []
    skipped = []
    for path in targets:
        sites, _rewrites = migrate_file(path, apply)
        if sites > 0:
            migrated.append((path.name, sites))
            total_sites += sites
        else:
            skipped.append(path.name)

    print(f"\n=== {'APPLIED' if apply else 'DRY-RUN'} ===")
    print(f"Files needing migration: {len(migrated)}")
    print(f"Total sites: {total_sites}")
    print(f"Already-migrated / no-pattern: {len(skipped)}")
    if migrated:
        print("\nMigrated:")
        for name, sites in migrated:
            print(f"  {name}: {sites} site(s)")
    if not apply:
        print("\n(dry-run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
