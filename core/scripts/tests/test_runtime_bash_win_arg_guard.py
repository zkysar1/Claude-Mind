"""bash_cmd's win32 argv-corruption guard (, guard-5633).

On Windows the MSYS runtime re-processes the raw command line (quote handling
plus glob/brace expansion) before argv reaches the program, and
subprocess.list2cmdline only quotes an argument that contains WHITESPACE. So a
whitespace-free argument carrying quotes or braces is silently altered:

  quotes -> argv TRUNCATION (the argument AND every following one are lost)
  braces -> value MANGLING  ('{a:b}' -> 'a:b'; '{a,b}' expands into two args)

The CORRUPTING / SAFE tables below are not hand-reasoned: every entry was
measured on Windows with an argv-echo script (0 false positives, 0 false
negatives over the 25 shapes). They are kept as data so a future change to the
predicate is checked against observed behaviour rather than against intuition.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _runtime_bash import bash_cmd, _win_arg_corrupts  # noqa: E402

# Measured to be altered in transit on win32.
CORRUPTING = [
    '{"title":"probe","priority":"LOW"}',  # the real-world case: JSON flag value
    '{"a":"b"}', 'a="b"', '"quoted"', 'x"y', '"', 'a"',
    "single'quote", "'sq'",
    '{a:b}', 'brace{x}', '{a,b}', 'a{b}c', '{x}',
]

# Measured to survive unaltered. Whitespace is protective for BOTH classes,
# and the shell-ish punctuation here is deliberately NOT flagged -- an
# over-broad predicate is the failure guard-2860 warns about.
SAFE = [
    'plain', 'no-quotes-here', 'a=b', '--flag', '',
    'has space "q"', "has space 'q'", 'has {a} space', 'has {a,b} space',
    'back\\slash', 'tick`cmd`', 'dollar$VAR', 'semi;colon',
    'pipe|x', 'amp&x', 'paren(x)', 'gt>x', 'star*',
]


@pytest.mark.parametrize("value", CORRUPTING)
def test_predicate_flags_every_measured_corrupting_shape(value):
    assert _win_arg_corrupts(value) is True, (
        f"{value!r} was measured to be corrupted in transit on Windows but the "
        f"predicate does not flag it — a false negative here restores the silent "
        f"corruption this guard exists to prevent."
    )


@pytest.mark.parametrize("value", SAFE)
def test_predicate_leaves_every_measured_safe_shape_alone(value):
    assert _win_arg_corrupts(value) is False, (
        f"{value!r} was measured to survive intact; flagging it would refuse a "
        f"call that works, which is the over-broad-predicate failure mode."
    )


def test_whitespace_is_protective_for_both_classes():
    """The single rule the predicate turns on: list2cmdline quotes on whitespace."""
    assert _win_arg_corrupts('{"a":"b"}') is True
    assert _win_arg_corrupts('{"a":"b"} x') is False
    assert _win_arg_corrupts('{a,b}') is True
    assert _win_arg_corrupts('{a,b} x') is False


@pytest.mark.skipif(sys.platform != "win32", reason="guard is win32-only by design")
def test_refuses_a_corrupting_argument_and_names_the_remedy():
    with pytest.raises(ValueError) as exc:
        bash_cmd("core/scripts/x.sh", "--inject-goal", '{"title":"probe"}', "--reason", "r")
    msg = str(exc.value)
    # The message must carry the fix, not merely the complaint -- that is the
    # whole point of refusing here rather than letting the program misreport.
    assert "input=payload" in msg, "must name the stdin remedy"
    assert "guard-5633" in msg, "must cite the guardrail"
    assert "--inject-goal" not in msg.splitlines()[0], "must name the OFFENDING arg, not the flag before it"
    assert "argument 2" in msg, "must identify which argument by position"


@pytest.mark.skipif(sys.platform != "win32", reason="guard is win32-only by design")
def test_failure_modes_are_described_distinctly():
    """Quotes truncate argv; braces mangle a value. Saying the wrong one misleads."""
    with pytest.raises(ValueError) as q:
        bash_cmd("x.sh", '{"a":"b"}')
    assert "TRUNCATE" in str(q.value)
    with pytest.raises(ValueError) as b:
        bash_cmd("x.sh", "{a:b}")
    assert "MANGLE" in str(b.value)
    assert "TRUNCATE" not in str(b.value)


@pytest.mark.skipif(sys.platform != "win32", reason="guard is win32-only by design")
def test_escape_hatch_allows_a_deliberate_caller_through(monkeypatch):
    monkeypatch.setenv("MIND_BASH_ALLOW_UNSAFE_ARGS", "1")
    argv = bash_cmd("core/scripts/x.sh", '{"a":"b"}')
    assert argv[-1] == '{"a":"b"}'


def test_safe_arguments_still_build_normally():
    argv = bash_cmd("core/scripts/x.sh", "--flag", "plain", "--other", "has space here")
    assert argv[1].endswith("core/scripts/x.sh")
    assert argv[-4:] == ["--flag", "plain", "--other", "has space here"]


def test_guard_does_not_disturb_guard_580_or_581(tmp_path):
    """argv[0] stays the resolved bash and the script keeps as_posix() form."""
    script = tmp_path / "sub" / "x.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    argv = bash_cmd(script, "ok")
    assert argv[0].lower().endswith("bash.exe") or argv[0].endswith("bash")
    assert "\\" not in argv[1], "guard-581: script path must be as_posix()"


def test_non_string_arguments_are_stringified_before_checking():
    argv = bash_cmd("x.sh", 7, 1.5)
    assert argv[-2:] == ["7", "1.5"]
