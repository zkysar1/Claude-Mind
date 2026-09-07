"""Authored-line scoping for the Q4 provenance sampler (class 5, ).

DELIBERATELY A SEPARATE FILE from test_q4_provenance_sample.py. That file was
being appended to concurrently by another live Body of this agent (unit 3,
commit b60517e99b on refs/workers/alpha/94c0ad1f...), which at the time of
writing was NOT an ancestor of this tree. Two Bodies appending to one file tail
is the one shape git cannot auto-merge cleanly, so the collision is avoided by
construction rather than by hoping (guard-2807: a goal claim does not reserve
the artifact).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from q4_provenance_sample import (  # noqa: E402
    added_line_numbers, scope_text_to_lines, run, sample_clusters)


# ---------------------------------------------------------------- diff parsing

DIFF = """diff --git a/f.py b/f.py
index 1111111..2222222 100644
--- a/f.py
+++ b/f.py
@@ -10,0 +11,2 @@ def thing():
+added eleven
+added twelve
@@ -40,2 +42,1 @@ def other():
-removed one
-removed two
+added forty two
"""


def test_added_line_numbers_returns_NEW_side_numbers_of_added_lines_only():
    assert added_line_numbers(DIFF) == {11, 12, 42}


def test_added_line_numbers_ignores_removals_and_advances_over_context():
    d = ("@@ -1,4 +1,4 @@\n"
         " context one\n"
         "-gone\n"
         "+brand new\n"
         " context two\n")
    # line 1 context, line 2 is the '+' (the '-' consumes no NEW number)
    assert added_line_numbers(d) == {2}


def test_added_line_numbers_is_empty_on_junk_rather_than_raising():
    assert added_line_numbers("") == set()
    assert added_line_numbers("not a diff at all\n+dangling") == set()


def test_added_line_numbers_tolerates_no_newline_marker():
    d = "@@ -1 +1,2 @@\n+one\n+two\n\\ No newline at end of file\n"
    assert added_line_numbers(d) == {1, 2}


# ------------------------------------------------------------------- scoping

def test_scope_blanks_unauthored_lines_and_PRESERVES_numbering():
    text = "a\nb\nc\nd"
    out = scope_text_to_lines(text, {2, 4})
    assert out.splitlines() == ["", "b", "", "d"]
    # numbering preserved is the whole point: findings carry start_line/end_line
    assert len(out.splitlines()) == len(text.splitlines())


def test_scope_with_empty_allowed_set_yields_no_content():
    assert scope_text_to_lines("a\nb", set()) == ""


# ------------------------------------------------- end-to-end discrimination

# Two entity-bearing, digit-carrying claims far apart, so they form SEPARATE
# clusters. The uncited one stands in for "a line an earlier commit wrote".
# A fact line needs an ENTITY *and* an assertion verb (is_entity_bearing and
# is_assertion in ground_truth_citation) -- prose with numbers alone forms no
# cluster at all. The first draft of this fixture had exactly that shape and
# every scoping test below passed while proving nothing; the positive control
# is what caught it (guard-2421, and the same trap a prior unit on this goal
# hit and discarded a draft over).
FIXTURE = (
    "The OLD claim: LogCollector was reported at 4210 sessions in 2024.\n"
    "\n"
    "\n"
    "\n"
    "The NEW claim: Router was reported at 99 percent uptime in 2026.\n"
)
OLD_LINE, NEW_LINE = 1, 5


def _findings(tmp_path, authored=None):
    art = tmp_path / "artifact.md"
    art.write_text(FIXTURE, encoding="utf-8")
    ranges = None if authored is None else {str(art): authored}
    return run("g-115-9059", [str(art)], n=10,
               session_id="no-such-session", authored_ranges=ranges)


def test_POSITIVE_CONTROL_the_fixture_actually_forms_two_clusters(tmp_path):
    """Non-vacuity: if the fixture formed no clusters the scoping tests below
    would pass while proving nothing (the exact trap a prior unit on this goal
    hit and discarded a draft over)."""
    _s, total = sample_clusters(FIXTURE, "g-115-9059", "artifact.md", 10)
    assert total == 2, f"fixture must form 2 clusters, formed {total}"


def test_UNSCOPED_the_old_line_IS_graded_this_is_the_defect(tmp_path):
    res = _findings(tmp_path, authored=None)
    lines = {f["start_line"] for f in res["findings"]}
    assert OLD_LINE in lines, (
        "unscoped, the sampler must still grade the line an earlier commit "
        "wrote -- that is the defect class 5 describes")
    assert res["authored_scoped"] == []


def test_SCOPED_to_the_new_line_the_old_line_is_NO_LONGER_graded(tmp_path):
    res = _findings(tmp_path, authored={NEW_LINE})
    lines = {f["start_line"] for f in res["findings"]}
    assert OLD_LINE not in lines, "scoping must drop the unauthored line"
    assert res["authored_scoped"] == [
        {"artifact": str(tmp_path / "artifact.md"), "authored_lines": 1}]


def test_SCOPED_still_grades_the_AUTHORED_line_not_a_blanket_suppression(tmp_path):
    """The half that makes the previous test mean something. If scoping merely
    silenced everything it would also pass that test."""
    res = _findings(tmp_path, authored={NEW_LINE})
    lines = {f["start_line"] for f in res["findings"]}
    assert NEW_LINE in lines, (
        "the authored line must STILL be graded -- otherwise the scope is "
        "alarm suppression, not scoping (guard-1901)")


def test_DEFAULT_PATH_IS_BYTE_IDENTICAL_when_no_ranges_are_supplied(tmp_path):
    """Opt-in contract: absent authored_ranges, nothing changes."""
    a = _findings(tmp_path, authored=None)
    b = _findings(tmp_path, authored=None)
    assert a == b
    assert a["authored_scoped"] == []
    assert a["clusters_total"] == 2


def test_SCOPED_EMPTY_says_scoping_did_it_and_is_not_reported_as_a_pass(tmp_path):
    res = _findings(tmp_path, authored={3})       # a blank line: no claims
    assert res["verdict"] == "skipped"
    assert res["verdict"] != "pass"
    assert "AUTHORED lines" in (res["skip_reason"] or "")
    assert "NOT a pass" in (res["skip_reason"] or "")
