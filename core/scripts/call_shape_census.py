#!/usr/bin/env python3
# domain-leak-exempt: --help examples name real framework flags (--since) for
# traceability to the incidents that motivated this tool. The logic is
# identifier-agnostic.
"""call_shape_census.py — given an IDENTIFIER, enumerate every occurrence in a
repo and classify each as a LIVE call vs a declaration / usage-string /
template / doc / comment / test mention (gap-048).

WHY THIS EXISTS
---------------
Asking "is this hazard LIVE or LATENT?" about a CLI flag, a query parameter, or
an exported symbol is a recurring, already-derived procedure (gap-048,
times_encountered 7). The naive form is one `git grep -c`, and it is wrong in
BOTH directions:

  (a) FALSE POSITIVE — a usage banner (`--since <duration>`), an argparse
      registration, or a doc mention is counted as a caller, so a LATENT hazard
      is reported as LIVE and gets urgent treatment it does not need.
  (b) FALSE NEGATIVE — `git grep -- :!<path>` FATALS with rc=128 ("Unimplemented
      pathspec magic") when an excluded path begins with a pathspec-magic
      character. Under `2>/dev/null` that loud failure becomes a confident EMPTY
      that reads exactly like "nothing consumes this symbol" (guard-1926).

Both are defended structurally here, not by care:

  * stderr is NEVER swallowed. rc=128 raises GitGrepFatal and the census exits
    non-zero with verdict FATAL. A zero can only be produced by rc=1 (git's
    "no match"), never by an error path.
  * a POSITIVE CONTROL runs before any zero is reported: an unfiltered
    `git grep` for the same identifier. That is what separates ABSENT (not in
    the repo at all) from LATENT (present, but no live call) — the exact
    distinction gap-048 was registered to make.

DISTINCT FROM ITS NEIGHBOURS (checked before forging, guard-821 / guard-4961):
  * key-consumer-census.py (gap-029) tabulates WRITERS vs READERS of a field
    key. Symmetric question, different answer shape.
  * uncrossed_seam_census.py (gap-117) asks which producer/consumer seams no
    test crosses.
  * inbound-reference-census.py (g-306-99) counts references TO artifacts and
    answers dangling-ness.
None of them classifies occurrences of one identifier by CALL SHAPE.

CLASSIFICATION IS LINE-LOCAL BY CONSTRUCTION (guard-2340). Every rule below
reads only the hit's own path and its own line text. No rule uses a +/-N-line
window, a nearest-preceding literal, or a same-paragraph heuristic, because a
census that associates structure with a call site by line proximity mis-joins
silently. The cost is an UNCLASSIFIED bucket, which is reported rather than
guessed into a neighbour.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
# `--` is DELIBERATELY ABSENT from the generic list. It is a comment marker in
# SQL/Lua/Haskell, but it is also the prefix of every long CLI flag — the
# primary input to this tool — so a generic `--` rule classifies the line
# `  --since <duration>` (a textbook usage banner) as a comment. Scope it by
# file suffix instead. Caught by this tool's own test suite, 2026-08-30.
COMMENT_PREFIXES = ("#", "//", "*", ";", "<!--")
DASH_COMMENT_SUFFIXES = {".sql", ".lua", ".hs", ".adb", ".ads"}

# A help/usage banner. Matched against the LINE, case-sensitively where the
# token is conventionally capitalised.
USAGE_MARKERS = re.compile(
    r"(Usage:|usage:|Accepted flags|Options:|optional arguments|positional arguments"
    r"|OPTIONS|SYNOPSIS|--help)"
)

# argparse / click / commander style registration of the identifier itself.
DECLARATION_MARKERS = re.compile(
    r"(add_argument|add_option|parser\.add|\.option\(|argparse|getopts|OPTIONS=)"
)


class GitGrepFatal(RuntimeError):
    """git grep failed loudly (rc >= 2). NEVER convert this into a zero."""


def _run_git_grep(identifier: str, repo: Path, excludes: list[str]) -> tuple[list[str], str]:
    """Return (lines, stderr). Raises GitGrepFatal on rc >= 2.

    stderr is captured and RETURNED, never discarded and never redirected to
    /dev/null — that redirect is the whole mechanism behind failure mode (b).
    """
    # `-e` is load-bearing, not stylistic: the primary input to this tool is a
    # CLI FLAG, so the pattern routinely begins with `-`. Without `-e`, git
    # parses `--since` as its OWN option and exits 129 — a loud failure, but
    # one that fires on the tool's main use case. Measured 2026-08-30.
    cmd = ["git", "grep", "-n", "--fixed-strings", "-e", identifier]
    if excludes:
        cmd.append("--")
        cmd.append(".")
        cmd.extend(excludes)
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    # rc 0 = matches, rc 1 = no matches (a LEGITIMATE zero), rc >= 2 = failure.
    if proc.returncode >= 2:
        raise GitGrepFatal(
            f"git grep exited {proc.returncode} for identifier {identifier!r}: "
            f"{proc.stderr.strip()}"
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines, proc.stderr


def _is_test_path(path: str) -> bool:
    p = PurePosix(path)
    base = p.name
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or "/tests/" in f"/{path}"
        or path.startswith("tests/")
    )


class PurePosix:
    """Tiny stand-in so classification never touches the real filesystem — a
    census must classify a PATH STRING from git output, including paths that do
    not exist on this box (dangling-ness is box-dependent, per
    inbound-reference-census.py's load-bearing correction)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.suffix = ("." + self.name.rsplit(".", 1)[-1]) if "." in self.name else ""


def classify(path: str, line: str, identifier: str) -> str:
    """Classify ONE hit from its own path and its own line text only.

    Rules are ordered; the first match wins. Order is load-bearing: a usage
    banner inside a test file is a usage string that happens to live in a test,
    and the caller almost always wants the file-class answer first, so file
    class is decided before line shape.
    """
    p = PurePosix(path)
    if p.suffix in DOC_SUFFIXES:
        return "doc"
    if _is_test_path(path):
        return "test"

    stripped = line.strip()
    if stripped.startswith(COMMENT_PREFIXES):
        return "comment"
    if p.suffix in DASH_COMMENT_SUFFIXES and stripped.startswith("--"):
        # ...unless the line simply STARTS with the dashed identifier, which is
        # a usage banner, not a comment. Fall through to the shape rules.
        if not stripped.startswith(identifier):
            return "comment"
    if DECLARATION_MARKERS.search(line):
        return "declaration"
    if USAGE_MARKERS.search(line):
        return "usage_string"

    # A metavariable immediately after the identifier is the usage-banner
    # fingerprint gap-048 names verbatim: `--since <duration>`.
    tail = line.split(identifier, 1)[1] if identifier in line else ""
    if re.match(r"\s*(<[A-Za-z_][^>]*>|\{[A-Za-z_][^}]*\}|[A-Z][A-Z_]{2,})", tail):
        return "usage_string"

    return "live"


def census(
    identifier: str, repo: Path, excludes: list[str] | None = None
) -> dict:
    excludes = excludes or []
    hits: list[dict] = []
    stderr_seen = ""

    lines, stderr_seen = _run_git_grep(identifier, repo, excludes)
    for raw in lines:
        # git grep -n output: path:lineno:text  (path may itself contain ':'
        # only in pathological repos; split from the LEFT twice is git's own
        # contract for -n output).
        parts = raw.split(":", 2)
        if len(parts) < 3:
            hits.append({"path": raw, "line_no": None, "text": raw, "shape": "unclassified"})
            continue
        path, line_no, text = parts[0], parts[1], parts[2]
        hits.append(
            {
                "path": path,
                "line_no": int(line_no) if line_no.isdigit() else None,
                "text": text.rstrip(),
                "shape": classify(path, text, identifier),
            }
        )

    counts: dict[str, int] = {}
    for h in hits:
        counts[h["shape"]] = counts.get(h["shape"], 0) + 1
    live = counts.get("live", 0)

    # ── POSITIVE CONTROL ─────────────────────────────────────────────────────
    # Required BEFORE reporting any zero. Without it, `live == 0` is three-way
    # ambiguous: latent, absent, or a broken search. The control collapses the
    # third possibility (a fatal already raised above) and separates the first
    # two.
    control_total = None
    if live == 0:
        control_lines, _ = _run_git_grep(identifier, repo, [])
        control_total = len(control_lines)

    if live > 0:
        verdict = "LIVE"
    elif control_total:
        verdict = "LATENT"
    else:
        verdict = "ABSENT"

    return {
        "identifier": identifier,
        "verdict": verdict,
        "total_occurrences": len(hits),
        "live_calls": live,
        "counts_by_shape": counts,
        "positive_control_total": control_total,
        "excludes": excludes,
        "stderr": stderr_seen.strip(),
        "hits": hits,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="call_shape_census.py",
        description=(
            "Census every occurrence of an IDENTIFIER (CLI flag, query "
            "parameter, or exported symbol) and classify each as a LIVE call "
            "vs declaration / usage-string / doc / comment / test mention."
        ),
        epilog="example: call_shape_census.py --since --repo . --json",
    )
    # Same defect one layer up: argparse would parse a leading-dash identifier
    # as an option. `-e/--identifier` is the safe channel; the positional is
    # kept for non-dashed symbols and for `--` separated use.
    ap.add_argument(
        "-e", "--identifier", dest="identifier_flag", default=None,
        help="the identifier, when it begins with a dash (e.g. -e --since)",
    )
    ap.add_argument(
        "identifier", nargs="?", default=None,
        help="the CLI flag, query parameter, or exported symbol",
    )
    ap.add_argument("--repo", default=".", help="repo root to search (default: cwd)")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="a git pathspec to exclude, e.g. ':!core/scripts/tests'. Repeatable.",
    )
    ap.add_argument("--json", action="store_true", help="emit the full JSON record")
    # argparse refuses a dash-leading VALUE in the separated form (`-e --since`
    # is read as two options), and the separated form is what a user naturally
    # types for a tool whose whole purpose is dashed identifiers. Lift the token
    # after -e/--identifier out of argv verbatim before parsing, so all three
    # forms work: `-e --since`, `-e--since`, `--identifier=--since`.
    argv = list(sys.argv[1:] if argv is None else argv)
    lifted = None
    for i, tok in enumerate(argv):
        if tok in ("-e", "--identifier") and i + 1 < len(argv):
            lifted = argv[i + 1]
            del argv[i : i + 2]
            break

    args = ap.parse_args(argv)

    identifier = lifted or args.identifier_flag or args.identifier
    if not identifier:
        ap.error("an identifier is required (positional, or -e for a dashed one)")

    try:
        result = census(identifier, Path(args.repo), args.exclude)
    except GitGrepFatal as exc:
        # Surfaced LOUDLY. This is failure mode (b): the one path that must
        # never be renderable as "nothing consumes this symbol".
        print(json.dumps({"verdict": "FATAL", "error": str(exc)}), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"{result['identifier']}: {result['verdict']}")
        print(
            f"  {result['live_calls']} live call(s) of "
            f"{result['total_occurrences']} occurrence(s)"
        )
        for shape, n in sorted(result["counts_by_shape"].items()):
            print(f"    {shape:<14} {n}")
        if result["positive_control_total"] is not None:
            print(f"  positive control (unfiltered): {result['positive_control_total']} hit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
