#!/usr/bin/env python3
""" — the five write forms extract_targets used to miss.

Drives the hook end-to-end via subprocess + stdin payload (the production
invocation shape), not the regex in isolation — a regex-only test measures a
branch production never takes (`hook-authoring-pitfalls`, and the sibling
test_bash_path_hook_out_of_root.py docstring).

FOUR forms are wired here (sed -i, install, dd of=, >|). The fifth —
inline-python open-for-write — is DELIBERATELY NOT wired, and that decision is
pinned below rather than left as an absence, because the goal asked for it to
be decided explicitly rather than dropped by omission. Measured on the
60,507-call corpus: all 1,087 raw matches for it disappear once the hook's own
strip_heredoc_bodies/strip_payload_spans run, because an inline-python body IS
a payload span. Catching it means un-stripping the very spans that kill two
measured false-positive classes, so it stays out.

THE FALSE-POSITIVE PINS ARE THE LOAD-BEARING HALF (guard-4166). Every form
here ADDS deny-power, and a deny-power test suite that only asserts "the new
thing denies" is green under an extractor that denies far too much. So each
new-deny assertion is paired with an approve assertion covering the way that
same form was measured to over-match while it was being built:

  * sed -i EXPRESSION fragments — the goal named this the highest-risk form.
    The target is a positional arg following an expression that routinely
    contains slashes, spaces and quotes. Two successive regex candidates
    leaked fragments as paths ('role-ish', 'timer/On', 'is/carries', bare '/')
    before the tokenizer replaced them.
  * sed -i running PAST its own command on a multi-line input. shlex treats
    '\n' as ordinary whitespace, so a walker without the newline substitution
    captures the NEXT line's tokens as sed "files" — measured, that denied
    /usr/bin/grep on a command whose sed touched only a relative path.

Corpus check at wiring time: flagged 3 -> 3, verdicts {deny: 3} -> {deny: 3}
over 60,517 real Bash calls. The four forms added ZERO new denials, so their
value is prospective coverage of a real bypass, not a retroactive catch.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT_ROOT / "core" / "scripts"
BASH_HOOK = SCRIPTS / "bash-path-resolution-hook.py"
AGENT = os.environ.get("MIND_AGENT") or "zeta"

OUT_OF_ROOT = "/opt/definitely-not-a-configured-root/g1153345.txt"


def _conf_present():
    return (PROJECT_ROOT / "agents" / AGENT / "local-paths.conf").is_file()


def bash_verdict(cmd):
    """deny (stdout payload) | advisory (stderr banner) | approve.

    Three-valued for the same reason the sibling suite keeps it: collapsing
    `advisory` into `approve` would let a silent demotion of the out-of-root
    branch read as a passing test.
    """
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(PROJECT_ROOT)
    env["MIND_AGENT"] = AGENT
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    p = subprocess.run(
        [sys.executable, str(BASH_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )
    if p.stdout.strip():
        return "deny"
    if "[l1-bash-path] ADVISORY" in p.stderr:
        return "advisory"
    return "approve"


@unittest.skipUnless(_conf_present(),
                     f"agents/{AGENT}/local-paths.conf absent — the hook "
                     f"fails open with no governed roots, so every verdict "
                     f"would be approve for a reason unrelated to this change")
class TestNewlyCoveredWriteForms(unittest.TestCase):
    """The four wired forms now reach the out-of-root branch."""

    def test_sed_in_place_short_flag(self):
        self.assertEqual(bash_verdict(f"sed -i 's/a/b/' {OUT_OF_ROOT}"), "deny")

    def test_sed_in_place_long_flag(self):
        self.assertEqual(
            bash_verdict(f"sed --in-place 's/a/b/' {OUT_OF_ROOT}"), "deny")

    def test_sed_in_place_with_suffix(self):
        """`-i.bak` is still in-place — the suffix must not defeat detection."""
        self.assertEqual(bash_verdict(f"sed -i.bak 's/a/b/' {OUT_OF_ROOT}"), "deny")

    def test_install_destination(self):
        self.assertEqual(
            bash_verdict(f"install -m 644 /tmp/src {OUT_OF_ROOT}"), "deny")

    def test_dd_of_destination(self):
        self.assertEqual(
            bash_verdict(f"dd if=/dev/zero of={OUT_OF_ROOT} bs=1 count=1"), "deny")

    def test_clobber_redirect(self):
        self.assertEqual(bash_verdict(f"echo hi >| {OUT_OF_ROOT}"), "deny")


@unittest.skipUnless(_conf_present(), "no governed roots without the conf")
class TestPreExistingFormsStillDeny(unittest.TestCase):
    """Positive controls (guard-4166).

    These cover forms this change did NOT touch. Their job is to stay GREEN
    while the new-form tests above would go red under a revert — a control
    that flips with the fix is not a control, it is a third copy of the same
    assertion.
    """

    def test_mkdir_still_denies(self):
        self.assertEqual(bash_verdict(f"mkdir -p {OUT_OF_ROOT}"), "deny")

    def test_cp_still_denies(self):
        self.assertEqual(bash_verdict(f"cp /tmp/a {OUT_OF_ROOT}"), "deny")

    def test_plain_redirect_still_denies(self):
        self.assertEqual(bash_verdict(f"echo hi > {OUT_OF_ROOT}"), "deny")


@unittest.skipUnless(_conf_present(), "no governed roots without the conf")
class TestFalsePositiveFloor(unittest.TestCase):
    """The half that keeps the added deny-power honest."""

    def test_sed_expression_containing_a_path_is_not_a_target(self):
        """The out-of-root path is inside the sed SCRIPT, not the file list.

        The first bare argument after `sed` is the expression. A naive
        last-arg or whitespace-split extractor reads this path as a write
        target and denies a command that only edits a relative file.
        """
        self.assertEqual(
            bash_verdict(f"sed -i 's|{OUT_OF_ROOT}|replacement|' README.md"),
            "approve")

    def test_sed_expression_with_spaces_and_slashes_leaks_no_fragments(self):
        """Fragments like 'role-ish' / 'is/carries' were captured as paths."""
        self.assertEqual(
            bash_verdict("sed -i 's/carries 121 fields and the only role-ish "
                         "one is x, which is/carries 122 fields/' notes.md"),
            "approve")

    def test_sed_does_not_run_past_a_newline_into_the_next_command(self):
        """The measured /usr/bin/grep false positive.

        shlex flattens '\\n' to whitespace, so without the newline
        substitution the walker consumes the SECOND line's tokens as sed
        "files" and denies a path this sed never touched.
        """
        cmd = f"sed -i 's/a/b/' README.md\necho done {OUT_OF_ROOT}"
        self.assertEqual(bash_verdict(cmd), "approve")

    def test_sed_semicolon_inside_the_expression_is_not_a_separator(self):
        """`s/^const X = 10;$/.../` — the ';' is DATA, inside the quotes."""
        self.assertEqual(
            bash_verdict("sed -i 's/^const X = 10;$/const X = 9;/' script.mjs"),
            "approve")

    def test_relative_sed_target_is_not_denied(self):
        self.assertEqual(bash_verdict("sed -i 's/a/b/' requirements.txt"),
                         "approve")

    def test_inline_python_open_for_write_is_deliberately_not_covered(self):
        """DECISION PIN, not an oversight — see the module docstring.

        An inline-python body is a payload span, which the hook strips as DATA
        before extraction. If a future change makes this deny, that is a
        signal the span-stripping was weakened; re-read the two false-positive
        classes it exists to kill before calling this test stale.
        """
        self.assertEqual(
            bash_verdict(f"python3 -c \"open('{OUT_OF_ROOT}','w').write('x')\""),
            "approve")


if __name__ == "__main__":
    unittest.main()
