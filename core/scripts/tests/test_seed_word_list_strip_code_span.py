"""test_seed_word_list_strip_code_span.py — regression guard for the 2026-07-27
code-corruption incident (g-115-3503, guard-1640).

Bug shape: `word_list_strip` rules carry `when_in_context: comment`, but
`apply_word_list_strip` substituted over the WHOLE LINE whenever
`_is_comment_line` returned True — and `_is_comment_line` returned True for ANY
line matching `\\s#`, including a line that is mostly CODE with a trailing
comment. So a stripped word appearing in the CODE half was deleted too:

    import boto3  # noqa: E402     ->     import   # noqa: E402

which is a SyntaxError, and it shipped to the public seed repo.

The collision is structural rather than incidental: `# noqa` lives on import
lines by construction, and the stripped words are import-adjacent by
construction, so the two meet on exactly the lines that break loudest.

Fix (`_seed_transforms._split_comment_span`): split the line into
(code, marker, comment) at the first REAL `#` — tracking single/double quote
state and backslash escapes so a `#` inside a string literal is not treated as a
comment marker — and confine the substitution to the comment tail. Suffix-gated
to `_HASH_COMMENT_SUFFIXES` (.py/.sh/.yaml/.yml) since bare-hash-to-EOL is not
the comment syntax everywhere.

This is the WRITE-side mirror of guard-319 / guard-1205, which cover comment/code
separation for read-only SCANNERS. It is strictly more severe: a scanner false
positive is a wrong report, a transformer false positive is corrupted code that
has already been published.

Run: py -3 -m pytest core/scripts/tests/test_seed_word_list_strip_code_span.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _seed_transforms import apply_word_list_strip  # noqa: E402

# Mirrors the live W1 rule in core/config/seed-manifest.yaml.
RULE = {
    "id": "W1-test",
    "type": "word_list_strip",
    "words": ["boto3", "Lambda", "EFS", "DynamoDB"],
    "applies_to": ["core/**/*.py", "core/**/*.sh", "mind_api/**/*.py"],
    "when_in_context": "comment",
}
REL_PY = "core/scripts/tests/sample.py"


# --- the incident itself --------------------------------------------------

def test_import_with_trailing_noqa_keeps_the_import():
    """THE incident: code half must survive a trailing comment on the same line."""
    out = apply_word_list_strip("import boto3  # noqa: E402\n", RULE, REL_PY)
    assert "import boto3" in out, (
        "the stripped word was removed from the CODE span — this is the exact "
        "shape that shipped a SyntaxError to the public seed"
    )


def test_incident_line_still_compiles():
    """Stronger form: the transformed line must remain valid Python."""
    out = apply_word_list_strip("import boto3  # noqa: E402\n", RULE, REL_PY)
    compile(out, "<transformed>", "exec")  # raises SyntaxError on regression


# --- the behavior the rule is FOR must be preserved -----------------------

def test_full_line_comment_is_still_stripped():
    out = apply_word_list_strip("# uses boto3 to talk to the store\n", RULE, REL_PY)
    assert "boto3" not in out, "full-line comments must still be stripped"


def test_indented_full_line_comment_is_still_stripped():
    out = apply_word_list_strip("    # boto3 client here\n", RULE, REL_PY)
    assert "boto3" not in out


def test_code_kept_and_comment_stripped_on_the_same_line():
    """Both halves handled independently — the discriminating case."""
    out = apply_word_list_strip(
        "client = boto3.client('s3')  # boto3 wrapper\n", RULE, REL_PY
    )
    assert "boto3.client" in out, "code span must be untouched"
    assert "# boto3 wrapper" not in out, "comment span must be stripped"


# --- quote-awareness: a `#` inside a string is not a comment marker -------

def test_hash_inside_single_quoted_string_is_not_a_comment_marker():
    out = apply_word_list_strip("sep = '#'  # Lambda note\n", RULE, REL_PY)
    assert "sep = '#'" in out, "the string literal must survive intact"
    assert "Lambda" not in out, "the real trailing comment must still be stripped"


def test_hash_inside_double_quoted_string_is_not_a_comment_marker():
    out = apply_word_list_strip('tag = "#EFS"  # EFS note\n', RULE, REL_PY)
    assert 'tag = "#EFS"' in out, "the string literal must survive intact"
    assert "# EFS note" not in out


def test_line_with_no_comment_is_untouched():
    src = "import boto3\n"
    assert apply_word_list_strip(src, RULE, REL_PY) == src


# --- suffix gating --------------------------------------------------------

@pytest.mark.parametrize("rel", ["core/x.py", "core/x.sh"])
def test_inline_comment_suffixes_split_the_span(rel):
    """For the suffixes that reach the comment branch, both halves are handled."""
    rule = dict(RULE, applies_to=["core/**"])
    out = apply_word_list_strip("keep = boto3  # Lambda\n", rule, rel)
    assert "keep = boto3" in out, f"code span not protected for {rel}"
    assert "Lambda" not in out, f"comment span not stripped for {rel}"


@pytest.mark.parametrize("rel", ["core/x.yaml", "core/x.yml"])
def test_yaml_inline_comments_are_not_reached_at_all(rel):
    """Documents a real asymmetry — pins current behavior, does not endorse it.

    `_split_comment_span` accepts .yaml/.yml via `_HASH_COMMENT_SUFFIXES`, but the
    gate that routes a line INTO the comment branch (`_is_comment_line`, the
    inline-`#` scan) is suffix-gated to .py/.sh only. So a YAML line carrying a
    TRAILING comment never reaches the split and is returned untouched — the
    stripped word survives in the comment.

    This is the SAFE direction of the asymmetry: nothing is substituted, so the
    code span cannot be corrupted (the incident class this file guards). It is
    non-coverage, not damage. Extending the strip to YAML inline comments would
    change what ships in every YAML file in the seed, so it is a deliberate
    decision rather than a side effect — tracked separately, see the module
    docstring reference in g-115-3503.

    A FULL-LINE YAML comment IS still stripped (covered by _COMMENT_PATTERNS);
    only the trailing-comment form is out of reach.
    """
    rule = dict(RULE, applies_to=["core/**"])
    out = apply_word_list_strip("keep: boto3  # Lambda\n", rule, rel)
    assert "keep: boto3" in out, "code span must be protected regardless"
    assert "Lambda" in out, (
        "behavior change: YAML trailing comments now reach the strip. That may be "
        "the desired fix — if so, update this test deliberately and re-check what "
        "it newly strips across every YAML file in the seed."
    )
