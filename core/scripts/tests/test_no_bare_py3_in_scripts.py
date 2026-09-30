#!/usr/bin/env python3
"""No new bare `py -3` in command position in core/scripts/*.sh ().

`py` is the Windows Python launcher. A Linux box has it only when someone
provisioned /usr/local/bin/py, which fleet boxes have and WSL and fresh
containers do not. There a bare `py -3` dies rc=127, and because most call
sites sit behind `2>/dev/null`, `|| true` or `|| echo <default>`, the script
carries on with an empty or default value and says nothing. Measured
2026-09-29 on WSL: iteration-commit.sh's commit-refused.json writer never ran,
and 2 of 13 tests in test_iteration_commit_gate_refusal.py went red for that
reason alone.

g-115-11431 converted 155 call sites in 59 scripts. 58 took the
rt_python_launcher idiom:

    source "$SCRIPT_DIR/_python_launcher.sh"
    PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3
    $PYLAUNCH -c '...'

That gives `py -3` on Windows and `python3` elsewhere, and keeps py-3-first on
Windows as guard-1098 requires for hook scripts. stop-hook.sh already had a
top-of-file $PY resolver, so its one stray site uses that instead. This test
keeps the class from coming back.

A RESOLVER'S OWN PROBE STAYS LITERAL. `if py -3 --version; then echo "py -3"`
tests the launcher it reports; rewritten to `$PYLAUNCH --version` it tests
python3 on a Windows box without py, and the resolver then reports a launcher
that does not exist. Those sites are in ALLOWED, not converted.

HOW IT DETECTS: a `py -3` in COMMAND POSITION, meaning at the start of a
logical line (backslash continuations joined) or after ; & | ( ) ` ! { $( exec
then do else if elif while until time, optionally behind VAR=value prefixes
(a value may be quoted and may hold a $(...) with quotes of its own).
Text that merely mentions the launcher, such as `echo "run py -3 x.py"`,
`PY="py -3"` or a comment, is not in command position and passes.

ALLOWED lists the command-position sites that are correct today, per file,
with the reason. Each is a resolver (probe `command -v py`, fall back to
python3), a `py -3 ... || python3 ...` chain, a Windows-only case arm, or not
a call at all. The count is exact both ways: a new site fails, and so does a
removed one, which keeps the list honest as it shrinks.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent

# An assignment VALUE may hold "$(cmd "arg")": quotes nested inside a command
# substitution inside quotes. A plain "[^"]*" stops at the first inner quote,
# which hid iteration-commit.sh's commit-refused.json writer (CR_STAGED="$(git
# -C "$REPO" ...)" ... py -3) from this test. Every alternative below starts
# with a different character, so the match stays linear.
_SUBST = r"""\$\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)"""
_VALUE = (r"""(?:"(?:[^"\\$]|\\.|""" + _SUBST + r"""|\$(?!\())*"|'[^']*'"""
          r"""|[^\s;&|()'"$\\]|\\.|""" + _SUBST + r"""|\$(?!\())*""")
CMD_POS = re.compile(
    r"""(?:^|[;&|()`!{]|\$\(|\b(?:exec|then|do|else|if|elif|while|until|time)\b)\s*"""
    r"""(?:[A-Za-z_][A-Za-z0-9_]*=""" + _VALUE + r"""\s+)*"""
    r"""py\s+-3\b"""
)

ALLOWED = {
    "aspirations-precheck-budget-meter.sh": (1, "now_ms(): py -3 ... || python3 ... chain"),
    "bring-up-doctor.sh": (1, "resolver: command -v py && py -3 -c pass, else python3 (guard-1098's named idiom)"),
    "check-no-bare-bash.sh": (1, "inside a MINGW*|MSYS*|CYGWIN*) case arm"),
    "check-no-ownership-flag.sh": (1, "inside a MINGW*|MSYS*|CYGWIN*) case arm"),
    "check-no-daemon-wrapper-reparse.sh": (1, "a grep -E pattern '(py -3|python3)', not a call"),
    "check-prerequisites.sh": (1, "probe: if py -3 --version, else python3"),
    "loop-exhaustion-fence.sh": (1, "resolver: PYRUN=(py -3) only after command -v py, else python3"),
    "migrate-to-phase-2-6.sh": (1, "py -3 ... \\ || python3 ... chain across a continuation line"),
    "mind-api-start.sh": (1, "resolver: _python_launcher() probes py -3 in its Windows case arm, then echoes the py it tested"),
    "permissions-add.sh": (1, "resolver: command -v py && py -3 --version, else python3"),
    "promotion-git-state.sh": (1, "resolver: exec py -3 if command -v py, else exec python3"),
    "promotion-plan-triage.sh": (1, "resolver: exec py -3 if command -v py, else exec python3"),
    "promotion-preflight.sh": (1, "resolver: exec py -3 if command -v py, else exec python3"),
    "session-summary-write.sh": (1, "py -3 ... \\ || python3 ... chain across a continuation line"),
    "sessionstart-orchestrator.sh": (2, "(py -3 ... || python3 ...) chains"),
    "sid-collision-check.sh": (1, "py -3 ... \\ || python3 ... chain across a continuation line"),
    "stop-hook.sh": (1, "resolver: command -v py && py -3 -c pass, else python3; its $PY serves every site (guard-1098)"),
    "wm-contamination-check.sh": (2, "(py -3 ... || python3 ...) chains"),
}

FIX = ('source "$SCRIPT_DIR/_python_launcher.sh"; '
       'PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3; then $PYLAUNCH ...')


def _logical_lines(text: str):
    buf, start = "", None
    for n, line in enumerate(text.splitlines(), 1):
        if start is None:
            start = n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        yield start, buf + line
        buf, start = "", None
    if start is not None:
        yield start, buf


def bare_py3_sites(text: str) -> list[int]:
    """Line numbers of `py -3` in command position (one entry per site)."""
    out: list[int] = []
    for n, line in _logical_lines(text):
        if line.lstrip().startswith("#"):
            continue
        out += [n] * len(CMD_POS.findall(line))
    return out


class DetectorControls(unittest.TestCase):
    def test_positive_control_finds_every_command_position(self):
        for snippet in (
            "py -3 -c 'print(1)'",
            'x="$(py -3 -c "print(1)")"',
            "echo '{}' | py -3 -c 'import json,sys'",
            'GID="$A" PROOT=/x py -3 - <<\'EOF\'',
            'exec py -3 "$SCRIPT" "$@"',
            "if py -3 -c pass; then :; fi",
            "  MINGW*|MSYS*) py -3 x.py ;;",
            "out=$(printf x \\\n  | py -3 -c 'y')",
            "done < <(LOG=x py -3 - 2>/dev/null <<'PYEOF' || true",
            # quotes nested in $(...) inside a quoted prefix value (the two
            # shapes the first version of this test missed):
            '_VALID="$(REG_DIR="$(dirname "${BASH_SOURCE[0]}")" SLOT_TO_CHECK="$SLOT" py -3 -c \'x\')"',
            'CR_STAGED="$(git -C "$REPO" diff | tr \'\\n\' \'\\f\' || true)" \\\n'
            '    CR_TS="$(date +%s)" py -3 - <<\'CRPYEOF\'',
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(len(bare_py3_sites(snippet)), 1, snippet)

    def test_negative_control_ignores_text_and_the_idiom(self):
        for snippet in (
            'echo "run py -3 core/scripts/x.py by hand" >&2',
            "# the old form was py -3 -c '...'",
            'PY="py -3"',
            '$PYLAUNCH -c "print(1)"',
            'PYLAUNCH="$(rt_python_launcher)" || PYLAUNCH=python3',
            "    echo \"py -3\"",
        ):
            with self.subTest(snippet=snippet):
                self.assertEqual(bare_py3_sites(snippet), [], snippet)


class NoBarePy3(unittest.TestCase):
    def setUp(self):
        self.files = sorted(SCRIPTS.glob("*.sh"))

    def test_scan_is_not_vacuous(self):
        self.assertGreater(len(self.files), 300, "core/scripts/*.sh glob found too few files")
        found = sum(bool(bare_py3_sites(f.read_text(encoding="utf-8", errors="replace")))
                    for f in self.files)
        self.assertGreater(found, 0, "detector found nothing at all; the allowlisted sites should match")

    def test_no_bare_py3_outside_the_allowlist(self):
        problems = []
        for f in self.files:
            sites = bare_py3_sites(f.read_text(encoding="utf-8", errors="replace"))
            allowed = ALLOWED.get(f.name, (0, ""))[0]
            if len(sites) != allowed:
                problems.append(f"  {f.name}: {len(sites)} command-position `py -3` "
                                f"(allowed {allowed}) at lines {sites}")
        self.assertFalse(problems,
                         "bare `py -3` changed in core/scripts (g-115-11431):\n"
                         + "\n".join(problems)
                         + f"\nA new site must use the launcher idiom: {FIX}\n"
                         "A removed resolver site: lower or delete its ALLOWED entry.")

    def test_allowlist_names_existing_files(self):
        missing = sorted(n for n in ALLOWED if not (SCRIPTS / n).exists())
        self.assertFalse(missing, f"ALLOWED names files that no longer exist: {missing}")


if __name__ == "__main__":
    unittest.main()
