""" — comment-vs-logic classification for .sh/.py promotion divergence.

promotion-preflight.py already split parametric CONFIG diffs into value-vs-comment
by PARSING both sides, so comments vanished for free. Code had no parse step, so a
comment-only diff in a shell script was indistinguishable from a logic change and
BLOCKED. Every promotion therefore ended in `PROMOTE_ALLOW_DRIFT=1`, and that
override is all-or-nothing — it suppresses genuine drift too. A gate that always
cries wolf trains the operator to bypass it.

THE ERROR DIRECTIONS ARE NOT SYMMETRIC, and that asymmetry drives these tests.
A false "value" costs one unnecessary review. A false "comment" silently excuses a
real logic change from blocking, which is the failure this gate exists to prevent.
So the negative controls here (logic-only, mixed, shebang, hash-in-string) matter
more than the positive one, and every ambiguous construct is expected to land on
"value", never on "comment".

Written pytest-collectable ON PURPOSE. The sibling
test_promotion_preflight_divergence.py is a main()-style file with no top-level
`def test_`, so pytest collects ZERO tests from it and `run-full-suite.sh` cannot
see it (.claude/rules/run-full-suite-after-deep-code.md § invisible suites). New
coverage should not inherit that blind spot.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
MOD_PATH = SCRIPTS / "promotion-preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("promotion_preflight_mod", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["promotion_preflight_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()
STRUCT = mod.ZONE_PHENO_STRUCT
PARAM = mod.ZONE_PHENO_PARAM
KERNEL = mod.ZONE_KERNEL


def _pair(tmp_path, name, src_text, tgt_text):
    s = tmp_path / f"src_{name}"
    t = tmp_path / f"tgt_{name}"
    s.write_text(src_text, encoding="utf-8")
    t.write_text(tgt_text, encoding="utf-8")
    return s, t


# ------------------------------------------------------------------ positive

def test_shell_comment_only_is_comment(tmp_path):
    """The motivating case: provenance drift in comments, identical logic."""
    src = '#!/usr/bin/env bash\n# set by g-115-1234 (private goal id)\nexport TZ=UTC\n'
    tgt = '#!/usr/bin/env bash\n# set during promotion\nexport TZ=UTC\n'
    s, t = _pair(tmp_path, "a.sh", src, tgt)
    r = mod.classify_divergence(s, t, STRUCT)
    assert r["kind"] == "comment", r
    assert r["reconcile_eligible"] is False


def test_python_comment_only_is_comment(tmp_path):
    src = "# provenance: g-115-1\nX = 1\n"
    tgt = "# provenance: scrubbed\nX = 1\n"
    s, t = _pair(tmp_path, "a.py", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "comment"


def test_trailing_comment_and_whitespace_only_is_comment(tmp_path):
    """Trailing comments, trailing spaces, CRLF and blank lines are all noise."""
    src = "export A=1   # why\r\nexport B=2\r\n"
    tgt = "export A=1\n\nexport B=2 # different note\n"
    s, t = _pair(tmp_path, "b.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "comment"


# ------------------------------------- negative controls (must still BLOCK)

def test_logic_only_still_blocks(tmp_path):
    """The goal's explicit negative control: a one-line logic change."""
    src = "#!/bin/bash\nexport TZ=UTC\n"
    tgt = "#!/bin/bash\nexport TZ=EST\n"
    s, t = _pair(tmp_path, "c.sh", src, tgt)
    r = mod.classify_divergence(s, t, STRUCT)
    assert r["kind"] == "value", r
    assert r["reconcile_eligible"] is True


def test_mixed_comment_and_logic_blocks(tmp_path):
    """A logic change must not be laundered by an accompanying comment change."""
    src = "# note one\nexport TZ=UTC\n"
    tgt = "# note two\nexport TZ=EST\n"
    s, t = _pair(tmp_path, "d.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_shebang_change_blocks(tmp_path):
    """`#!` is not a comment — it selects the interpreter."""
    src = "#!/bin/bash\necho hi\n"
    tgt = "#!/usr/bin/env python3\necho hi\n"
    s, t = _pair(tmp_path, "e.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


@pytest.mark.parametrize("suffix,src_line,tgt_line", [
    (".sh", 'PATTERN="^#!/bin/sh"', 'PATTERN="^#!/bin/bash"'),
    (".sh", "MSG='a # b'", "MSG='a # c'"),
    (".py", 'URL = "http://x/#v1"', 'URL = "http://x/#v2"'),
])
def test_hash_inside_quoted_string_is_not_a_comment(tmp_path, suffix, src_line, tgt_line):
    """The case that makes a naive stripper dangerous: text after a quoted `#`
    is CODE. Stripping it would make these files look comment-only and excuse a
    real change from blocking."""
    s, t = _pair(tmp_path, f"f{suffix}", src_line + "\n", tgt_line + "\n")
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_shell_midword_hash_is_literal(tmp_path):
    """POSIX shell: `#` begins a comment only at the start of a word, so
    `echo a#b` is entirely code."""
    s, t = _pair(tmp_path, "g.sh", "echo a#b\n", "echo a#c\n")
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_heredoc_body_is_data_not_comment(tmp_path):
    """A `#` line inside a heredoc is content being emitted, not a comment."""
    src = "cat <<'EOF'\n# banner v1\nEOF\n"
    tgt = "cat <<'EOF'\n# banner v2\nEOF\n"
    s, t = _pair(tmp_path, "h.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_python_docstring_change_blocks(tmp_path):
    """Triple-quoted literals are runtime values (__doc__, embedded SQL), so they
    are preserved as functional. Deliberately over-conservative."""
    src = 'def f():\n    """v1"""\n    return 1\n'
    tgt = 'def f():\n    """v2"""\n    return 1\n'
    s, t = _pair(tmp_path, "i.py", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_hash_inside_python_docstring_is_not_a_comment(tmp_path):
    """A multi-line literal must not be comment-stripped line by line."""
    src = 'SQL = """\nSELECT 1  # v1\n"""\n'
    tgt = 'SQL = """\nSELECT 1  # v2\n"""\n'
    s, t = _pair(tmp_path, "j.py", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


# ------------------------------------------------------------ lane scoping

def test_kernel_code_is_never_excused(tmp_path):
    """KERNEL holds code too (anchor enforcer, transplant mechanism). Its policy
    is always-human-review, so a comment-only KERNEL diff must NOT be excused."""
    src = "# note one\nexport TZ=UTC\n"
    tgt = "# note two\nexport TZ=UTC\n"
    s, t = _pair(tmp_path, "k.sh", src, tgt)
    assert mod.classify_divergence(s, t, KERNEL)["kind"] == "structural"


def test_non_code_structural_still_structural(tmp_path):
    """Pins the pre-existing behavior the sibling suite's case4 asserts: a
    structural NON-code file keeps kind=structural."""
    s, t = _pair(tmp_path, "l.md", "# Title\n\nbody one\n", "# Title\n\nbody two\n")
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "structural"


def test_identical_bytes_short_circuit(tmp_path):
    s, t = _pair(tmp_path, "m.sh", "export A=1\n", "export A=1\n")
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "identical"


# ------------------------------------------------------- enforcement wiring

def test_code_comment_diff_is_excused_from_blocking():
    """End-to-end intent: a code file classified `comment` drops out of the
    blocking set and is NAMED in the excused list, so the report shows what it
    forgave rather than silently passing."""
    cd = {"core/scripts/_platform.sh": {"kind": "comment", "reconcile_eligible": False}}
    filtered, excused = mod.excuse_comment_only_blocks(["core/scripts/_platform.sh"], cd)
    assert filtered == []
    assert excused == ["core/scripts/_platform.sh"]


def test_code_value_diff_is_not_excused():
    cd = {"core/scripts/_platform.sh": {"kind": "value", "reconcile_eligible": True}}
    filtered, excused = mod.excuse_comment_only_blocks(["core/scripts/_platform.sh"], cd)
    assert filtered == ["core/scripts/_platform.sh"]
    assert excused == []


# ------------------------------------------------------------ unit: stripper

@pytest.mark.parametrize("suffix", [".md", ".yaml", ".json", ""])
def test_functional_code_returns_none_for_non_code(suffix):
    """Widening the language set must be an explicit act, not an accident."""
    assert mod._functional_code("# x\n", suffix) is None


def test_functional_code_preserves_indentation():
    """Python indentation is significant — only TRAILING whitespace is normalized."""
    out = mod._functional_code("def f():\n    return 1   \n", ".py")
    assert out == "def f():\n    return 1"


# --------------------------------------------- multi-line quoted string bodies
# Found by the fresh-eyes pass on this file's own first version, which reset
# quote state per line and so comment-stripped the INSIDE of a multi-line shell
# string. guard-165 mandates the `py -3 -c '...'` single-quoted form throughout
# this repo, so the shape is not exotic: 143 of 482 core/scripts/*.sh (30%)
# contain a line that ends inside an open quote.

def test_multiline_quoted_string_body_is_data_not_comment(tmp_path):
    """A `#` line inside a multi-line shell string is CONTENT. Stripping it made
    two differing files compare equal — a false `comment`, the one direction
    this gate must never take (rb-6259)."""
    src = "py -3 -c '\nimport x\n# body v1\nprint(1)\n'\n"
    tgt = "py -3 -c '\nimport x\n# body v2\nprint(1)\n'\n"
    s, t = _pair(tmp_path, "n.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_multiline_string_body_difference_is_detected(tmp_path):
    """The stronger case: the difference is embedded CODE, not a comment."""
    src = "py -3 -c '\nprint(1)\n'\n"
    tgt = "py -3 -c '\nprint(2)\n'\n"
    s, t = _pair(tmp_path, "p.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"


def test_multiline_string_does_not_cost_comment_lane_coverage(tmp_path):
    """The fix must not be a disguised bail: a file CONTAINING a multi-line
    string still gets comment-only drift OUTSIDE that string excused. This is
    what a conservative 'give up on the whole file' fix would have forfeited on
    30% of shell scripts."""
    src = "# provenance: g-115-1234\npy -3 -c '\nimport x\n'\nexport A=1\n"
    tgt = "# provenance: scrubbed\npy -3 -c '\nimport x\n'\nexport A=1\n"
    s, t = _pair(tmp_path, "q.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "comment"


def test_heredoc_introducer_inside_a_string_is_not_a_heredoc(tmp_path):
    """`<<EOF` appearing inside a multi-line string is string content. Treating
    it as a redirection would swallow the rest of the file as heredoc body."""
    src = "py -3 -c '\ns = \"a <<EOF b\"\nprint(1)\n'\n"
    tgt = "py -3 -c '\ns = \"a <<EOF b\"\nprint(2)\n'\n"
    s, t = _pair(tmp_path, "r.sh", src, tgt)
    assert mod.classify_divergence(s, t, STRUCT)["kind"] == "value"
