"""Producer/consumer contract pins for the decision-rules session stamp ().

THE LOAD-BEARING TESTS IMPORT THE REAL CONSUMER. This defect class is
"a consumer's membership predicate the producer was never taught to satisfy",
so a test that re-implements the predicate here would pass while the real
`tree-edit-since.py` kept rejecting the write -- reproducing the exact failure
instead of catching it. Every attribution assertion below therefore calls
`tree_edit_since.attributable_to_session` itself.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dra = _load("decision_rules_append", "decision-rules-append.py")
tes = _load("tree_edit_since", "tree-edit-since.py")

MINE = "7f7f3513-874f-4041-9fed-61fb90962046"
FOREIGN = "03fda40a-b902-4975-b266-df887f43a4fd"


def _node(session=FOREIGN, last_updated="2026-08-10"):
    """A node shaped like the live ones: session NESTED under the trigger."""
    return (
        "---\n"
        'topic: "A Node"\n'
        "last_updated: '%s'\n"
        "last_update_trigger:\n"
        "  type: goal_execution\n"
        '  source: "g-000-00 (someone)"\n'
        "  session: %s\n"
        "---\n"
        "\n## Decision Rules\n\n- IF x THEN y\n" % (last_updated, session)
    ) if session else (
        "---\n"
        'topic: "A Node"\n'
        "last_updated: '%s'\n"
        "---\n"
        "\nbody\n" % last_updated
    )


def _write(tmp_path, body):
    p = tmp_path / "node.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The contract: after stamping, the REAL consumer attributes the node to us.
# ---------------------------------------------------------------------------

def test_a_foreign_stamped_node_is_UNATTRIBUTABLE_before_the_fix(tmp_path):
    """The defect itself. If this ever passes, the premise has changed."""
    p = _write(tmp_path, _node(session=FOREIGN))
    assert tes.attributable_to_session(p, MINE) is False


def test_stamping_makes_the_real_consumer_attribute_it_to_this_session(tmp_path):
    p = _write(tmp_path, dra.stamp_session(_node(session=FOREIGN), MINE))
    assert tes.attributable_to_session(p, MINE) is True


def test_the_stamp_does_not_credit_some_OTHER_session(tmp_path):
    """Guards against a stamp so permissive it would credit anyone."""
    p = _write(tmp_path, dra.stamp_session(_node(session=FOREIGN), MINE))
    assert tes.attributable_to_session(p, "some-other-sid") is False


def test_the_two_session_regexes_have_not_diverged():
    """A silent divergence here re-opens the defect in the quiet direction."""
    assert dra._FM_SESSION_RE.pattern.replace("(\\s*)", "\\s*") == \
        tes._SESSION_RE.pattern.replace("(\\S+)", "\\S+")


# ---------------------------------------------------------------------------
# Scope: front matter only, and no invention where the consumer fail-opens.
# ---------------------------------------------------------------------------

def test_a_node_with_no_session_stamp_is_left_untouched(tmp_path):
    """The consumer fail-opens for these, so the append is already credited;
    adding a key would be a change with no reader."""
    body = _node(session=None)
    assert dra.stamp_session(body, MINE) == body
    assert tes.attributable_to_session(_write(tmp_path, body), MINE) is True


def test_a_session_line_in_the_BODY_is_never_rewritten():
    """A `session:` line can legitimately appear in prose."""
    body = _node(session=FOREIGN) + "\nProse mentioning session: %s here.\n" % FOREIGN
    out = dra.stamp_session(body, MINE)
    assert out.count(FOREIGN) == 1, "the prose occurrence must survive"
    assert MINE in out.split("\n---", 1)[0]


def test_last_updated_is_refreshed():
    out = dra.stamp_session(_node(last_updated="2026-08-10"), MINE, today="2026-08-11")
    assert "last_updated: '2026-08-11'" in out
    assert "2026-08-10" not in out


def test_indentation_of_the_nested_stamp_is_preserved():
    out = dra.stamp_session(_node(session=FOREIGN), MINE)
    assert "  session: %s" % MINE in out, "must stay nested under the trigger"


# ---------------------------------------------------------------------------
# Refusals: never guess at a malformed node.
# ---------------------------------------------------------------------------

def test_an_empty_sid_changes_nothing():
    body = _node()
    assert dra.stamp_session(body, "") == body
    assert dra.stamp_session(body, None) == body


def test_a_node_with_no_front_matter_is_untouched():
    body = "no front matter here\nsession: %s\n" % FOREIGN
    assert dra.stamp_session(body, MINE) == body


def test_unterminated_front_matter_is_untouched():
    body = "---\ntopic: x\n  session: %s\n" % FOREIGN
    assert dra.stamp_session(body, MINE) == body


def test_only_the_first_session_line_is_rewritten():
    """The consumer uses .search (first match), so the writer must match it."""
    body = ("---\n"
            "last_update_trigger:\n"
            "  session: %s\n"
            "prior:\n"
            "  session: %s\n"
            "---\nbody\n" % (FOREIGN, FOREIGN))
    out = dra.stamp_session(body, MINE)
    assert out.count(MINE) == 1
    assert out.count(FOREIGN) == 1
