#!/usr/bin/env python3
"""Tests for the embedded-block hand-roll predicate, gate, and detective.

g-115-6228 — the tool-layer closure of guard-2222 after a third recurrence
across a third agent. Each POSITIVE case below is a real measured incident
shape, not an invented one; the incident id is named in the case.
"""
import json
import os
import subprocess
import sys

import pytest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)

from _embedded_block_predicate import advisory_text, detect  # noqa: E402
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never bare "bash")

PROJECT_ROOT = os.path.dirname(SCRIPTS)
GATE = os.path.join(SCRIPTS, "embedded-block-extraction-gate.sh")
AUDIT = os.path.join(SCRIPTS, "embedded-block-hand-roll-audit.sh")

# --- the three measured incidents --------------------------------------------
FOXTROT = (  # , 2026-08-01
    "start=$(grep -nF 'python3 - <<' efs-session-classify.sh | cut -d: -f1)\n"
    "end=$(awk -v s=\"$start\" 'NR>s && /^PYEOF/{print NR; exit}' f.sh)\n"
    "sed -n \"${start},${end}p\" f.sh\n"
)
BRAVO = (  # , 2026-08-09 -- bounds from an earlier read, no grep here
    "sed -n '377,1494p' world/scripts/usage-liveness-sweep.sh | python3"
)
ECHO = (  # , 2026-08-14
    "START=$(grep -nF 'CSTATS=$(timeout 120 bash' cycle.sh | cut -d: -f1)\n"
    "END=$(awk -v s=\"$START\" 'NR>s && $0==\")\" {print NR; exit}' cycle.sh)\n"
    "sed -n \"${START},${END}p\" cycle.sh\n"
)


@pytest.mark.parametrize("name,cmd", [
    ("foxtrot-g-005-31", FOXTROT),
    ("bravo-g-335-1035", BRAVO),
    ("echo-g-326-220", ECHO),
])
def test_every_measured_incident_fires(name, cmd):
    assert detect(cmd) is not None, f"{name} must be detected"


def test_bravo_is_the_slice_interpret_form():
    """Bravo's bounds came from an EARLIER read, so no line-number source
    appears in the command at all. Requiring both halves in one command -- the
    obvious design -- would miss it entirely."""
    f = detect(BRAVO)
    assert f["form"] == "slice+interpret"
    assert f["line_number_source"] is None


@pytest.mark.parametrize("cmd", [
    'grep -n M h.sh; sed -n "${start},${end}p" h.sh',   # braced
    'grep -n M h.sh; sed -n "$a,$b p" h.sh',            # bare var
    "grep -n M h.sh; sed -n '12,40p' h.sh",             # literal
])
def test_sed_bounds_all_three_shell_forms(cmd):
    """REGRESSION (caught by this module's own positive control on first run).

    The bounds class was originally `[\\$\\{]?[\\w\\}]+`, which accepts `}` but
    not `{` -- so `${start}`, the single most common way a script carries a
    computed bound, never matched. The bug hid because the other cases' slice
    half was being satisfied by the `awk NR>` alternative instead. A unit test
    drawn from the implementation would not have found this; the control did."""
    assert detect(cmd) is not None


@pytest.mark.parametrize("cmd", [
    "sed -n '138,178p' core/scripts/goal-field-census-ratchet.py",  # bare read
    'grep -n "X" f.py',                                             # bare grep
    "sed -i 's/a/b/' f.txt",                                        # no range
    "sed '1,5d' f.txt",                                             # no -n
    "git log --oneline -1",
    "grep -c foo bar.txt | wc -l",
])
def test_ordinary_commands_stay_silent(cmd):
    """A false positive here is not free: it trains the reader to ignore the
    advisory, which is how guard-2222 reached times_noise 9 to begin with."""
    assert detect(cmd) is None


@pytest.mark.parametrize("cmd", [
    'bash core/scripts/extract-embedded-block.sh --file f.sh --open-marker X',
    'bash core/scripts/extract-embedded-block.sh --file f.sh | sed -n "1,5p"',
])
def test_never_advises_against_the_helper_itself(cmd):
    assert detect(cmd) is None


def test_partial_match_skips_rather_than_swallows():
    """guard-2655: a scanner whose OPENER matches but CLOSER does not must SKIP.
    A slice with neither a derive nor an interpreter is an ordinary file read."""
    assert detect("sed -n '1,50p' host.sh") is None
    assert detect("grep -n MARKER host.sh") is None


def test_advisory_names_the_helper_and_the_guardrail():
    text = advisory_text(detect(ECHO))
    assert "extract-embedded-block.sh" in text
    assert "guard-2222" in text
    assert "ADVISORY ONLY" in text


# --- the gate, end to end through the real wrapper ----------------------------
def _run_gate(payload):
    return subprocess.run(
        bash_cmd(GATE), input=json.dumps(payload), capture_output=True,
        text=True, cwd=PROJECT_ROOT, timeout=60)


def test_gate_advises_but_never_denies():
    r = _run_gate({"tool_name": "Bash", "tool_input": {"command": BRAVO}})
    assert r.returncode == 0
    hso = json.loads(r.stdout)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", "advisory must NEVER deny"
    # allow+reason alone does not reach the model (); the delivering
    # field is additionalContext, so assert that field specifically.
    assert "extract-embedded-block.sh" in hso["additionalContext"]


def test_gate_silent_on_ordinary_command():
    r = _run_gate({"tool_name": "Bash",
                   "tool_input": {"command": "sed -n '1,25p' README.md"}})
    assert r.returncode == 0
    assert r.stdout.strip() == ""


@pytest.mark.parametrize("payload", [
    {}, {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None},
    {"tool_name": "Bash", "tool_input": {"command": None}},
    {"tool_name": "Edit", "tool_input": {"command": BRAVO}},
])
def test_gate_fails_open_on_every_malformed_shape(payload):
    """A hook that breaks must never break the Bash tool (guard-591)."""
    r = _run_gate(payload)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_gate_wrapper_never_propagates_python_rc():
    """guard-591: the bash wrapper MUST always exit 0, never `exit $RC`."""
    body = open(GATE, encoding="utf-8").read()
    assert "exit $RC" not in body
    assert body.rstrip().endswith("exit 0")


# --- the detective ------------------------------------------------------------
def test_audit_reports_a_positive_control_and_a_count():
    r = subprocess.run(bash_cmd(AUDIT, "--json"), capture_output=True,
                       text=True, cwd=PROJECT_ROOT, timeout=900)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    # A zero from a scanner that is not scanning is indistinguishable from a
    # clean corpus (guard-2298). The control is what makes the count readable.
    assert d["positive_control"] == "PASS"
    assert d["files_scanned"] > 0
    assert d["count"] == len(d["findings"])


def test_audit_reports_broken_rather_than_a_reassuring_zero():
    """The control gate is the load-bearing half: the script must exit non-zero
    and say BROKEN when the predicate cannot fire, never print a clean 0."""
    src = open(os.path.join(SCRIPTS, "embedded-block-hand-roll-audit.py"),
               encoding="utf-8").read()
    assert "BROKEN" in src
    assert "return 2" in src


# --- heredoc bodies: data vs execution (found LIVE, not by inspection) --------
def test_written_heredoc_prose_does_not_fire():
    """REGRESSION, measured in production .

    Minutes after the gate was wired, it fired on `cat > record.md <<'EOF'`
    whose PROSE described the hand-rolled shape -- an experience record about
    this very defect. Docs, guardrail text and commit messages all discuss the
    shape by quoting it, so without heredoc-body stripping the gate accuses
    every document that explains it. guard-2222 already carries times_noise 9;
    an advisory that cries wolf is ignored inside a day, which would retire the
    whole deliverable. This is the gate-side counterpart of the prose filtering
    guard-319 mandates for corpus scanners -- the detective got _prose_filter
    and the gate originally got nothing."""
    cmd = (
        "cat > rec.md <<'MDEOF'\n"
        "The invariant: a line number is obtained via grep -n and the file is\n"
        "then sliced by it. The awk NR> variant and sed -n \"${start},${end}p\"\n"
        "are the commonest computed-bound forms.\n"
        "MDEOF\n"
        "echo done"
    )
    assert detect(cmd) is None


def test_shell_heredoc_that_executes_the_shape_still_fires():
    """The exception that keeps the stripping honest: a body fed to bash IS
    executed, so it is real code and must still be caught."""
    cmd = ("bash <<'EOF'\n"
           "start=$(grep -n MARKER h.sh | cut -d: -f1)\n"
           "sed -n \"${start},${end}p\" h.sh\n"
           "EOF")
    assert detect(cmd) is not None


def test_python_heredoc_body_is_data_not_shell():
    """A `python3 - <<EOF` body is PYTHON; a shell-shaped string inside it is a
    literal, not an execution -- the same distinction the detective draws for
    .py files. Keeping python bodies made the gate fire on its own harness."""
    cmd = ("py -3 - <<'PYEOF'\n"
           "CASES = [\"grep -n M h.sh\", \"sed -n '1,9p' h.sh\"]\n"
           "PYEOF")
    assert detect(cmd) is None


def test_unterminated_written_heredoc_strips_to_end():
    """Direction-of-error is deliberate. guard-2655 says a scanner must SKIP
    rather than SWALLOW, but that governs ACCUSING; here swallowing produces
    FEWER accusations. For an advisory a false negative is a missed reminder,
    while a false positive is noise that retires the gate -- so strip
    generously when the closing delimiter never arrives."""
    cmd = "cat > x.md <<'EOF'\ngrep -n M h.sh then sed -n '1,9p' h.sh"
    assert detect(cmd) is None
