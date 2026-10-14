"""Code-artifact scope for the Q4 provenance sampler ().

A code file is sampled on its comments and docstrings only, so a Q4 FAIL on one is
always fixable in a comment. Three things must stay true, and each is pinned with a
control beside it so the scope cannot be satisfied by a blanket suppression
(guard-1901, guard-2421):

- the scope DROPS code lines: a claim-shaped string constant is not graded;
- the scope KEEPS prose: the same claim in a docstring or comment still FAILs, and
  one source token inside that comment clears it without touching a code line;
- everything that is not a recognised code suffix is byte-identical to before.

DELIBERATELY A SEPARATE FILE from test_q4_provenance_sample.py, for the reason
test_q4_authored_scope.py gives: two Bodies appending to one file tail is the one
shape git cannot auto-merge.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import q4_provenance_sample as q4m  # noqa: E402
from q4_code_scope import scope_code  # noqa: E402

GOAL = "g-115-11480"
# One entity (a year) and one assertion (a copula): the smallest line the detector
# reads as a claim. The same sentence is used in every fixture so a difference in
# verdict can only come from WHERE it sits, never from what it says.
CLAIM = "The Router was reported at 99 percent uptime in 2026."
CITED = CLAIM[:-1] + " (g-115-11480)."

PY_SOURCE = f'''\
"""Module docstring: {CLAIM}"""
import os

REASON_TAIL = "{CLAIM}"
SQL = """
{CLAIM}
"""


def check(x):
    """{CLAIM}"""
    assert x == f"Router was reported at {{x}} percent uptime in 2026"
    y = 1  # trailing note: {CLAIM}
    return y
'''

SH_SOURCE = (
    "#!/usr/bin/env bash\n"
    f"# {CLAIM}\n"
    f'echo "{CLAIM}"  # trailing comment after code is dropped\n'
    "cat <<EOF\n"
    f"# heredoc line: {CLAIM}\n"
    f"{CLAIM}\n"
    "EOF\n"
    'grep x <<< "# a here-string is not a heredoc"\n'
    f"# after the heredoc: {CLAIM}\n"
    "cat <<-TABBED\n"
    "\t# tabbed heredoc body\n"
    "\tTABBED\n"
    "# final comment\n"
)

JAVA_SOURCE = (
    f"// {CLAIM}\n"
    'String url = "http://example.com/a"; // trailing comment after code is dropped\n'
    f"/* single-line block: {CLAIM} */ int x = 1;\n"
    "/**\n"
    f" * Javadoc: {CLAIM}\n"
    " */\n"
    "int y = 2;\n"
    "/* opens here\n"
    "   and stays open\n"
    "   until here */ int z = 3;\n"
    'String s = "/* not a comment";\n'
)

LUA_SOURCE = (
    f"-- {CLAIM}\n"
    'local s = "-- not a comment"\n'
    f"--[[ block: {CLAIM}\n"
    "still inside ]] local t = 1\n"
    "--[==[ level two ]] still inside\n"
    "closes here ]==]\n"
    "local u = 2\n"
)


def _kept(source: str, name: str) -> set:
    """1-based numbers of the lines that still carry text after scoping."""
    s = scope_code(name, source)
    assert s is not None, f"{name} must be recognised as code"
    assert len(s.text.split("\n")) == len(source.splitlines()), "line numbering must survive"
    return {i for i, ln in enumerate(s.text.split("\n"), 1) if ln.strip()}


# ------------------------------------------------------------ the four scanners

def test_python_keeps_docstrings_and_comments_and_drops_every_string_literal():
    assert _kept(PY_SOURCE, "m.py") == {1, 11, 13}
    s = scope_code("m.py", PY_SOURCE)
    lines = s.text.split("\n")
    assert lines[3] == "" and lines[5] == "" and lines[11] == ""   # constant, fixture, assert
    assert lines[12].startswith("# trailing note")                  # the comment, not the code
    assert "y = 1" not in lines[12]
    assert (s.kept_lines, s.total_lines) == (3, 14)


def test_python_hash_inside_a_string_is_not_a_comment():
    assert _kept('x = "# not a comment: ' + CLAIM + '"\n', "m.py") == set()


def test_python_source_that_does_not_parse_still_returns_what_it_could_read():
    broken = f"# {CLAIM}\nx = (1,\n"            # unclosed paren: tokenize and ast both fail
    s = scope_code("m.py", broken)
    assert s is not None and s.total_lines == 2
    assert s.text.split("\n")[0].startswith("# ")


def test_shell_keeps_comments_but_not_the_shebang_a_trailing_comment_or_a_heredoc_body():
    assert _kept(SH_SOURCE, "run.sh") == {2, 9, 13}
    assert _kept(SH_SOURCE, "run.bash") == {2, 9, 13}


def test_c_like_keeps_line_and_block_comments_and_not_a_slash_slash_inside_a_string():
    assert _kept(JAVA_SOURCE, "A.java") == {1, 3, 4, 5, 6, 8, 9, 10}
    s = scope_code("A.java", JAVA_SOURCE).text.split("\n")
    assert "int x" not in s[2] and "int z" not in s[9]              # code after a closed block
    assert s[1] == "" and s[10] == ""                               # url string, "/*" in a string
    for name in ("a.js", "a.mjs", "a.cjs", "a.ts", "a.tsx", "a.jsx"):
        assert _kept(JAVA_SOURCE, name) == {1, 3, 4, 5, 6, 8, 9, 10}


def test_lua_keeps_line_and_leveled_block_comments_and_not_a_dash_dash_inside_a_string():
    assert _kept(LUA_SOURCE, "m.lua") == {1, 3, 4, 5, 6}
    assert _kept(LUA_SOURCE, "m.luau") == {1, 3, 4, 5, 6}


# ------------------------------------------------------------ what is NOT code

@pytest.mark.parametrize("name", ["n.md", "n.txt", "d.json", "d.jsonl", "d.yaml", "Dockerfile", "noext"])
def test_a_suffix_outside_the_closed_table_is_left_alone(name):
    assert scope_code(name, "# " + CLAIM + "\n") is None


def test_the_suffix_match_ignores_case():
    assert scope_code("RUN.SH", "# x\n") is not None


def test_a_file_holding_a_line_separator_splitlines_honours_is_left_unscoped():
    """str.splitlines() and the tokenizer would disagree on the line numbers."""
    assert scope_code("m.py", "x = 1" + chr(0x0C) + "\n# y\n") is None


def test_the_module_can_scope_its_own_source():
    """Its docstring once held a literal separator, which made it refuse itself."""
    src = (CORE_SCRIPTS / "q4_code_scope.py").read_text(encoding="utf-8")
    assert scope_code("q4_code_scope.py", src) is not None


# ------------------------------------------------------------ the sampler verdict

def _run(tmp_path, name, text, **kw):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p), q4m.run(GOAL, [str(p)], n=10, session_id="no-such-session", **kw)


def test_the_same_claim_fails_as_a_docstring_and_prose_but_not_as_a_code_constant(tmp_path):
    _p, doc = _run(tmp_path, "doc.py", f'"""{CLAIM}"""\n')
    _p, const = _run(tmp_path, "const.py", f'REASON_TAIL = "{CLAIM}"\n')
    _p, note = _run(tmp_path, "note.md", CLAIM + "\n")
    assert doc["verdict"] == "fail" and [f["kind"] for f in doc["findings"]] == ["missing-citation"]
    assert note["verdict"] == "fail"                    # control: the claim IS detectable
    assert const["verdict"] == "skipped" and const["findings"] == []
    assert const["code_scoped"][0]["kept_lines"] == 0


def test_MUTATION_without_the_scope_the_code_constant_fails_again(tmp_path, monkeypatch):
    """The control for the test above: it proves the fixture would be graded."""
    monkeypatch.setattr(q4m, "scope_code", lambda *a, **k: None)
    _p, const = _run(tmp_path, "const.py", f'REASON_TAIL = "{CLAIM}"\n')
    assert const["verdict"] == "fail" and const["code_scoped"] == []


def test_a_fail_on_a_code_artifact_is_cleared_by_editing_a_comment_alone(tmp_path):
    code_line = "int y = 2;"
    before = f"// {CLAIM}\n{code_line}\n"
    path, res = _run(tmp_path, "A.java", before)
    assert res["verdict"] == "fail" and res["findings"][0]["start_line"] == 1
    after = f"// {CITED}\n{code_line}\n"
    _p, fixed = _run(tmp_path, "A.java", after)
    assert fixed["verdict"] == "pass", fixed
    assert after.splitlines()[1] == before.splitlines()[1] == code_line   # no code line moved


def test_a_code_artifact_reports_what_it_kept_and_a_prose_artifact_reports_nothing(tmp_path):
    _p, code = _run(tmp_path, "A.java", JAVA_SOURCE)
    assert code["code_scoped"] == [{"artifact": str(tmp_path / "A.java"), "language": "c-like",
                                    "kept_lines": 8, "total_lines": 11}]
    _p, prose = _run(tmp_path, "n.md", CLAIM + "\n")
    assert prose["code_scoped"] == []


def test_a_prose_artifact_is_byte_identical_to_the_unscoped_behaviour(tmp_path, monkeypatch):
    _p, a = _run(tmp_path, "n.md", CLAIM + "\n\n\n" + CLAIM + "\n")
    monkeypatch.setattr(q4m, "scope_code", lambda *x, **k: None)
    _p, b = _run(tmp_path, "n.md", CLAIM + "\n\n\n" + CLAIM + "\n")
    assert a == b


def test_an_empty_code_sample_is_skipped_names_the_scope_and_is_never_a_pass(tmp_path):
    _p, res = _run(tmp_path, "const.py", f'X = "{CLAIM}"\n')
    assert res["verdict"] == "skipped"
    reason = res["skip_reason"] or ""
    assert "comments and docstrings only" in reason and "NOT a pass" in reason


def test_the_code_scope_composes_with_the_authored_scope(tmp_path):
    src = f"// {CLAIM}\nint x = 1;\n\n\n// {CLAIM}\n"      # claims at L1 and L5, code at L2
    path = tmp_path / "A.java"
    path.write_text(src, encoding="utf-8")
    res = q4m.run(GOAL, [str(path)], n=10, session_id="no-such-session",
                  authored_ranges={str(path): {2, 5}})
    assert {f["start_line"] for f in res["findings"]} == {5}     # L1 not authored, L2 is code
    assert res["authored_scoped"] and res["code_scoped"]
    only_code = q4m.run(GOAL, [str(path)], n=10, session_id="no-such-session",
                        authored_ranges={str(path): {2}})
    assert only_code["verdict"] == "skipped"
    assert "AUTHORED lines" in only_code["skip_reason"]
    assert "comments and docstrings only" in only_code["skip_reason"]


# ------------------------------------------------------------ the two readers

ENV = {**os.environ, "MIND_SID": "no-such-session", "STORAGE_BACKEND": "local"}


def test_the_cli_summary_says_the_artifact_was_sampled_on_comments_only(tmp_path):
    path = tmp_path / "A.java"
    path.write_text(JAVA_SOURCE.replace(CLAIM, CITED), encoding="utf-8")
    p = subprocess.run([sys.executable, str(CORE_SCRIPTS / "q4-provenance-sample.py"),
                        "--goal", GOAL, "--artifact", str(path)],
                       capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "sampled on comments and docstrings only" in p.stdout
    assert "8 of 11 lines kept" in p.stdout
    as_json = subprocess.run([sys.executable, str(CORE_SCRIPTS / "q4-provenance-sample.py"),
                              "--goal", GOAL, "--artifact", str(path), "--json"],
                             capture_output=True, text=True, encoding="utf-8", env=ENV, timeout=120)
    assert json.loads(as_json.stdout)["code_scoped"][0]["kept_lines"] == 8


_spec = importlib.util.spec_from_file_location("verify_preflight", CORE_SCRIPTS / "verify-preflight.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)


def _real(argv, stdin=None):
    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8",
                       cwd=str(PROJECT_ROOT), env=ENV, timeout=120)
    return p.returncode, p.stdout, p.stderr


def test_the_preflight_q4_line_carries_the_coverage_of_a_code_artifact(tmp_path):
    code = tmp_path / "A.java"
    code.write_text(JAVA_SOURCE.replace(CLAIM, CITED), encoding="utf-8")
    prose = tmp_path / "n.md"
    prose.write_text(CITED + "\n", encoding="utf-8")
    on_code = vp.check_q4(GOAL, [str(code)], None, _real)
    on_prose = vp.check_q4(GOAL, [str(prose)], None, _real)
    assert on_code["state"] == "PASS" and "1 code artifact(s) sampled on comments and docstrings only" in on_code["summary"]
    assert "(8 of 11 lines kept)" in on_code["summary"]
    assert on_prose["state"] == "PASS" and "code artifact" not in on_prose["summary"]
