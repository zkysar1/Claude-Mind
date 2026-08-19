"""decision-rules-append must stamp the node it wrote ().

THE DEFECT. tree-edit-since.py decides whether a modified tree node counts as THIS
session's encoding by matching `^\\s*session:\\s*(\\S+)\\s*$` against the node's front
matter. It fail-opens ONLY for a node carrying no session stamp at all, so a node
already stamped by a DIFFERENT session -- most nodes in a multi-agent fleet, where
agents append to each other's nodes constantly -- was permanently unattributable to any
later appender. decision-rules-append.py wrote the node with a direct Python
write_text() and touched no front matter, so it could never satisfy the check that
decides whether its own write counted.

The symptom was silent and in the under-crediting direction: iteration-close printed
"--tree-updated passed but no tree-file change detected ... IGNORING flag" on an
iteration that genuinely encoded, learning_value did not credit the work, and the
tree-encoding-drift-gate incremented as though nothing had been written.

WHY THE FIX IS AT THE WRITER AND NOT THE CHECKER. The consumer's docstring is right
that an unread node must not be credited; fail-opening it would credit genuinely
foreign edits. This is a producer/consumer contract where the consumer was always
correct, so the producer is what had to learn to satisfy it.

WHAT MAKES THESE PINS NON-VACUOUS. test_foreign_stamp_is_rejected_without_the_fix
asserts the PRE-FIX state FAILS the same predicate the post-fix state passes, using the
REAL attributable_to_session loaded from tree-edit-since.py rather than a reimplemented
copy of its regex. A pin that cannot fail against the defect it names is not a pin, and
a pin that re-implements the predicate it is checking proves only that the copy agrees
with itself.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
WRITER = SCRIPTS / "decision-rules-append.py"

FOREIGN_SID = "03fda40a-b902-4975-b266-df887f43a4fd"
OUR_SID = "caeb1579-54b2-4fdc-b99f-fd23b4ebbba2"

NODE = """---
topic: "Test Node"
last_updated: '2026-08-01'
last_update_trigger:
  type: "goal_execution"
  source: "g-000-00 earlier"
  session: {sid}
---

# Test Node

Body text.
"""


def _load_consumer():
    """Load the REAL predicate from tree-edit-since.py.

    Imported by path because the filename is hyphenated and therefore not a legal
    module name. Deliberately NOT a local copy of the regex: the whole point is to
    check agreement with the code that actually gates attribution.
    """
    spec = importlib.util.spec_from_file_location(
        "_tree_edit_since", SCRIPTS / "tree-edit-since.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.attributable_to_session


def _write_node(root: Path, sid: str) -> Path:
    node = root / "knowledge" / "tree" / "system" / "testnode" / "node.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text(NODE.format(sid=sid), encoding="utf-8")
    return node


def _run_writer(node: Path, sid: str, rule: str):
    env = dict(os.environ)
    env.update({"MIND_SID": sid, "MIND_AGENT": "alpha",
                "STORAGE_BACKEND": "local"})  # guard-955: never own-cloud in a test
    return subprocess.run(
        [sys.executable, str(WRITER), "--goal", "g-115-5831",
         "--node-path", str(node)],
        input=rule, capture_output=True, text=True, timeout=60, env=env,
    )


RULE = '{"if":"a probe returns zero","then":"positive-control it before believing it"}'


def test_append_stamps_the_node_with_the_writing_session():
    """The node starts stamped by a FOREIGN session -- the common fleet case."""
    with tempfile.TemporaryDirectory() as td:
        node = _write_node(Path(td), FOREIGN_SID)
        r = _run_writer(node, OUR_SID, RULE)
        assert r.returncode == 0, r.stderr
        assert "appended=1" in r.stdout, r.stdout
        text = node.read_text(encoding="utf-8")
        assert OUR_SID in text, "writing session was not stamped onto the node"
        assert FOREIGN_SID not in text, "foreign session stamp survived the write"


def test_consumer_accepts_the_node_after_the_append():
    """The assertion that actually matters: the REAL predicate now credits it."""
    attributable = _load_consumer()
    with tempfile.TemporaryDirectory() as td:
        node = _write_node(Path(td), FOREIGN_SID)
        assert _run_writer(node, OUR_SID, RULE).returncode == 0
        assert attributable(node, OUR_SID) is True


def test_foreign_stamp_is_rejected_without_the_fix():
    """THE DISCRIMINATOR. The pre-fix state must FAIL the same predicate the
    post-fix state passes, or the pins above prove nothing."""
    attributable = _load_consumer()
    with tempfile.TemporaryDirectory() as td:
        node = _write_node(Path(td), FOREIGN_SID)   # written, never stamped
        assert attributable(node, OUR_SID) is False


def test_rule_content_still_lands():
    """The stamp is a courtesy to a downstream consumer; appending is the job.
    Guards against a stamping change that silently breaks the primary write."""
    with tempfile.TemporaryDirectory() as td:
        node = _write_node(Path(td), FOREIGN_SID)
        assert _run_writer(node, OUR_SID, RULE).returncode == 0
        body = node.read_text(encoding="utf-8")
        assert "## Decision Rules" in body
        assert "positive-control it before believing it" in body
        assert "source: g-115-5831" in body


def test_non_tree_path_is_a_silent_no_op():
    """A node outside knowledge/tree/ has no virtual path, so there is nothing the
    consumer would look at. It must still receive its rule, and must not error."""
    with tempfile.TemporaryDirectory() as td:
        node = Path(td) / "loose.md"
        node.write_text(NODE.format(sid=FOREIGN_SID), encoding="utf-8")
        r = _run_writer(node, OUR_SID, RULE)
        assert r.returncode == 0, r.stderr
        assert "appended=1" in r.stdout
        assert "positive-control it before believing it" in node.read_text(encoding="utf-8")
