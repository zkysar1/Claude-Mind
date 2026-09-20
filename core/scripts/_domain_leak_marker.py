#!/usr/bin/env python3
"""Single source of truth for the `domain-leak-exempt` marker predicate.

WHY THIS FILE EXISTS (g-115-10246, guard-6989). Every consumer of the marker
used to test for it as a BARE UNANCHORED SUBSTRING anywhere in the file, so a
file that merely DISCUSSED the marker -- in prose, in backticks, in a docstring,
in a comment explaining the wall -- claimed the exemption. `domain-leak-check.sh`
then skipped that file for EVERY blocklist term, with no marker, no rationale
text, and nothing to review. Measured 2026-09-18 by a planted positive control
that refused to go red.

`domain-leak-commit-gate.py` (g-115-10048) fixed its OWN copy first and recorded
the divergence deliberately; this module is the convergence that note asked for.
The predicate now lives here once and every consumer reads it from here.

WHAT COUNTS AS CLAIMING THE EXEMPTION: the token must OPEN A COMMENT. That is
the construct that actually claims it per `.claude/rules/domain-free-examples.md`
section "Marker Placement" -- `#` (shell/python/YAML), `<!--` (markdown/HTML),
`//` (C-style), `--` (SQL/Lua), `*` (block-comment continuation), optionally
indented. A prose mention, a quoted string, or a token inside a sentence does
NOT claim it.

TWO ENGINES, ONE SPEC (rb-1915). Three consumers are bash and four are python,
and bash cannot import python. So the ERE below is the canonical spec and the
python pattern is DERIVED from it by exactly one documented substitution
(`[[:blank:]]` -> `[ \\t]`), because python's `re` has no POSIX character
classes while a POSIX ERE has no `\\t` inside a bracket expression -- writing
`[ \\t]` in an ERE matches space, backslash and the letter `t`, which is a real
bug, not a style choice. `_ere_to_python()` is a total function over the ERE we
actually ship, and `test_domain_leak_marker.py` pins both engines against the
shared FIXTURES corpus below so the two can never drift apart silently.

CONSUMERS (measured 2026-09-20, zeta, cc-02 -- SEVEN, not the six the goal's
progress note inherited; re-derive this list, never trust the count):
  bash   core/scripts/domain-leak-check.sh            marker-honor  -> SUPPRESS
  bash   core/scripts/domain-leak-check.sh            misplacement  -> REPORT
  bash   core/scripts/seed-path-self-reference-scan.sh              -> SUPPRESS
  python core/scripts/marker-placement-gate.py        PreToolUse    -> REFUSE
  python core/scripts/domain-leak-commit-gate.py      commit-msg    -> BLOCK
  python core/scripts/rule-vs-convention-gate.py      PreToolUse    -> SUPPRESS
  python core/scripts/_seed_transforms.py             seed rewrite  -> SUPPRESS

THIS FILE CARRIES NO MARKER, DELIBERATELY. It names the token only inside string
literals and prose, neither of which opens a comment, so under its own predicate
it does not exempt itself -- which is the whole point (guard-6087: a check whose
own text contains the searched token must not be falsified by its own
installation).

CLI (for the bash consumers):
    py -3 core/scripts/_domain_leak_marker.py --print-ere
        prints the canonical POSIX ERE on one line; call it ONCE per script run
        and reuse the value with `grep -qE "$RX"`, never once per file.
    py -3 core/scripts/_domain_leak_marker.py --file PATH
        exit 0 if PATH claims the exemption, 1 if not, 2 on read failure.
"""
from __future__ import annotations

import re
import sys

MARKER_TOKEN = "domain-leak-exempt:"

# Canonical spec. POSIX ERE, usable directly as `grep -qE "$RX"`.
MARKER_ANCHORED_ERE = r"^[[:blank:]]*(#|<!--|//|--|\*)[[:blank:]]*" + MARKER_TOKEN


def _ere_to_python(ere: str) -> str:
    """Translate the canonical ERE to a python `re` pattern.

    Total over the ERE this module ships: the ONLY construct that differs
    between POSIX ERE and python `re` here is the `[[:blank:]]` class, which
    python spells `[ \\t]`. Everything else (anchors, alternation, escaped
    literal `*`) is identical in both engines. Kept as an explicit function
    rather than a second hand-written pattern so there is exactly one place
    where a future ERE change has to be re-checked.
    """
    return ere.replace("[[:blank:]]", "[ \t]")


MARKER_ANCHORED_RX = re.compile(_ere_to_python(MARKER_ANCHORED_ERE))

# Shared fixture corpus. Both engines are pinned against THESE lines by
# core/scripts/tests/test_domain_leak_marker.py, so a divergence is a red test
# rather than a silent behaviour split between bash and python consumers.
# (True, False) = (claims the exemption, does not).
FIXTURES: tuple[tuple[str, bool], ...] = (
    ("# " + MARKER_TOKEN + " functional regex", True),
    ("   # " + MARKER_TOKEN + " indented", True),
    ("\t# " + MARKER_TOKEN + " tab-indented", True),
    ("<!-- " + MARKER_TOKEN + " markdown -->", True),
    ("// " + MARKER_TOKEN + " c-style", True),
    ("-- " + MARKER_TOKEN + " sql/lua style", True),
    (" * " + MARKER_TOKEN + " block-comment continuation", True),
    ("#" + MARKER_TOKEN + " no space after hash", True),
    ('        "         # ' + MARKER_TOKEN + ' <why>",', False),
    ("the " + MARKER_TOKEN + " marker is explained below", False),
    ("See " + MARKER_TOKEN + " for details", False),
    ('print("' + MARKER_TOKEN + '")', False),
    ('MARKER_TOKEN = "' + MARKER_TOKEN + '"', False),
    ("Use the `" + MARKER_TOKEN + "` marker to opt out.", False),
    ("", False),
)


def line_claims_exemption(line: str) -> bool:
    """True when this single line OPENS A COMMENT with the marker token."""
    return MARKER_ANCHORED_RX.match(line) is not None


def claims_exemption(text: str) -> bool:
    """True when any line of `text` claims the exemption."""
    return any(line_claims_exemption(l) for l in text.splitlines())


def file_claims_exemption(path) -> bool:
    """True when the file at `path` claims the exemption. Unreadable -> False."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return claims_exemption(fh.read())
    except OSError:
        return False


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "--print-ere":
        sys.stdout.write(MARKER_ANCHORED_ERE + "\n")
        return 0
    if len(argv) == 2 and argv[0] == "--file":
        try:
            with open(argv[1], "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            sys.stderr.write("cannot read %s: %s\n" % (argv[1], exc))
            return 2
        return 0 if claims_exemption(text) else 1
    sys.stderr.write(__doc__.split("CLI (for the bash consumers):", 1)[-1].strip() + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
