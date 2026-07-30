#!/usr/bin/env python3
"""Layer C detective for the gradle --tests uppercase-package footgun.

The gate (gradle-tests-gate.py) is Layer A: it refuses the pattern at the Bash
chokepoint, where the mistake is actually made. This script is the observer:
it scans the committed corpus for the same pattern, catching cases the gate
never saw -- text authored before the gate shipped, content written through
Write/Edit rather than Bash, or a command that ran while the hook was
fail-open.

Predicate parity is mandatory: both layers import `bad_test_patterns` from
`_gradle_tests_predicate`. Never inline the check here -- a detective that
disagrees with its gate reports noise.

GUARD-319 COMPLIANCE (this scanner reads .sh/.md corpora with an identifier
regex, so the false-positive class is live): every line passes through the
shared two-layer prose filter before the predicate sees it, plus a third layer
for Python docstrings. Without this the scanner flags its own documentation --
the rule file, this docstring, and the guardrail that records the mechanism all
quote the BAD form deliberately as a counter-example. rb-349 is the canonical
incident for that class.

Usage:
    py -3 core/scripts/gradle-tests-audit.py            # report, exit 0
    py -3 core/scripts/gradle-tests-audit.py --json     # machine-readable
    py -3 core/scripts/gradle-tests-audit.py --exit-on-hits   # exit 1 on hits

`--exit-on-hits` is the form to wire into a recurring goal or pre-commit
check; the bare form only reports, matching aspirations-rejection-audit.py.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
PROJECT_ROOT = SCRIPT_DIR.parent.parent

from _gradle_tests_predicate import bad_test_patterns  # noqa: E402
from _prose_filter import is_prose_line as _is_prose_line  # noqa: E402
from _prose_filter import pyfile_docstring_lines as _pyfile_docstring_lines  # noqa: E402
from _prose_filter import strip_prose_refs as _strip_prose_refs  # noqa: E402

# Corpus roots. Product repos are out of scope -- this audits the framework's
# own instructions to itself (skills, scripts, rules, conventions), which is
# where a wrong form propagates to every future reader.
#
# Test files are excluded wholesale (EXCLUDE_DIR_PARTS below), not allowlisted
# one by one: any test exercising a bad-pattern detector necessarily contains
# bad patterns as fixtures, so allowlisting instances would leave the next test
# author with the same false positive. A fixture is not an instruction.
CORPUS_GLOBS = (
    ("core/scripts", "*.sh"),
    ("core/scripts", "*.py"),
    ("core/config", "*.md"),
    (".claude/skills", "*.md"),
    (".claude/rules", "*.md"),
)

# Directory names that are fixture territory anywhere in the tree.
EXCLUDE_DIR_PARTS = {"tests"}

# The files whose PURPOSE is to document the bad form. They quote it outside
# backticks in places, so the prose filter alone cannot spare them. Keep this
# list minimal -- it is an allowlist, and every entry is a blind spot.
ALLOWLIST = {
    "core/scripts/_gradle_tests_predicate.py",
    "core/scripts/gradle-tests-gate.py",
    "core/scripts/gradle-tests-audit.py",
    ".claude/rules/gradle-tests-pattern.md",
}


def collect_corpus(root: Path) -> list:
    """Return [(relative_path, text)] for every in-scope file that reads."""
    corpus = []
    for subdir, pattern in CORPUS_GLOBS:
        base = root / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(pattern)):
            rel = path.relative_to(root).as_posix()
            if rel in ALLOWLIST:
                continue
            if EXCLUDE_DIR_PARTS.intersection(path.relative_to(root).parts[:-1]):
                continue
            try:
                corpus.append((rel, path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return corpus


def scan(corpus: list) -> list:
    """Return [{path, line, pattern}] for every surviving package-qualified hit."""
    hits = []
    for rel, text in corpus:
        path = Path(rel)
        docstring_lines = (
            _pyfile_docstring_lines(text, path) if rel.endswith(".py") else set()
        )
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Layer 3: Python docstrings are prose regardless of content.
            if lineno in docstring_lines:
                continue
            # Layer 1: whole-line skip (comments in .sh/.md, per guard-319).
            if _is_prose_line(line, path):
                continue
            # Layer 2: strip inline-backtick references before matching.
            effective = _strip_prose_refs(line, path)
            for pattern in bad_test_patterns(effective):
                hits.append({"path": rel, "line": lineno, "pattern": pattern})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--exit-on-hits",
        action="store_true",
        help="exit 1 when hits are found (for recurring goals / pre-commit)",
    )
    parser.add_argument(
        "--root", default=str(PROJECT_ROOT), help="corpus root (default: PROJECT_ROOT)"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    hits = scan(collect_corpus(root))

    if args.json:
        print(json.dumps({"hits": hits, "count": len(hits)}, indent=2))
    elif not hits:
        print("gradle-tests-audit: clean — no package-qualified --tests patterns found.")
    else:
        print(
            "gradle-tests-audit: {} package-qualified --tests pattern(s) — each "
            "matches ZERO tests and passes silently:".format(len(hits))
        )
        for hit in hits:
            print("  {}:{}  --tests '{}'".format(hit["path"], hit["line"], hit["pattern"]))
        print("\nSee .claude/rules/gradle-tests-pattern.md for the working forms.")

    return 1 if (hits and args.exit_on_hits) else 0


if __name__ == "__main__":
    sys.exit(main())
