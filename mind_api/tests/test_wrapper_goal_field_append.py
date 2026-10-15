"""End-to-end test for goal-field-append.sh against a real daemon ().

goal-field-append.py used to refuse any composed value that began with '{' or '[',
on the premise that aspirations-update-goal.sh JSON-decodes such a value and stores
an object. That is decided inside the wrapper's own encoder and the daemon, so it is
tested there: the real chain (goal-field-append.sh -> aspirations-query.sh and
aspirations-update-goal.sh -> daemon) runs against a seeded goal, and the field is
read back from the fixture's aspirations.jsonl.

Strategy as in test_wrapper_aspirations_update_goal.py: running_daemon spawns a
daemon in a tmp project_root and RT_DIR points the wrappers at it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mind_api.tests.test_wrapper_aspirations_update_goal import _read_goal, _run, _seed_goal

REPO_ROOT = Path(__file__).resolve().parents[2]
APPEND_WRAPPER = REPO_ROOT / "core" / "scripts" / "goal-field-append.sh"
MARKER = "m-9103"


def _append(goal_id: str, text: str, *, project_root: Path):
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    # Not a worker Body: a worker's note is signed with a line this test would have to expect.
    env.pop("MIND_SID", None)
    proc = subprocess.run(
        [shutil.which("bash") or "bash", APPEND_WRAPPER.as_posix(),
         goal_id, "outcome_note", MARKER, text],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_the_update_wrapper_does_store_a_json_valid_value_as_an_object(running_daemon):
    # Positive control: the hazard the old refusal named is real for a value that IS valid
    # JSON. Without it, the test below passing would not say the sentinel line is what kept
    # the composed value out of it.
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port)
    rc, _, err = _run([goal_id, "outcome_note", '{"a": 1}'], project_root=project_root)
    assert rc == 0, f"update wrapper exit {rc}: stderr={err}"
    assert _read_goal(project_root, goal_id)["outcome_note"] == {"a": 1}


@pytest.mark.parametrize("pre, text", [
    ("[closure-evidence] SUPERSEDES a prior-occurrence note", "a later correction"),
    ('{"closure": 1} then prose', "a later correction"),
    ("", "[closure-evidence] a first note"),
    ("", '{"looks": "like json"}'),
])
def test_a_bracket_led_note_is_appended_and_lands_as_text(running_daemon, pre, text):
    project_root, port = running_daemon
    goal_id = _seed_goal(project_root, port, extras={"outcome_note": pre} if pre else None)
    assert (_read_goal(project_root, goal_id).get("outcome_note") or "") == pre

    rc, out, err = _append(goal_id, text, project_root=project_root)
    assert rc == 0, f"goal-field-append.sh exit {rc}: stderr={err}"
    assert json.loads(out)["changed"] is True

    expected = (pre + "\n\n" if pre else "") + text + f"\n[appended:{MARKER}]"
    stored = _read_goal(project_root, goal_id)["outcome_note"]
    assert isinstance(stored, str), f"stored as {type(stored).__name__}, not text"
    assert stored == expected

    # The same marker again changes nothing.
    rc, out, err = _append(goal_id, text, project_root=project_root)
    assert rc == 0, f"re-run exit {rc}: stderr={err}"
    assert json.loads(out)["changed"] is False
    assert _read_goal(project_root, goal_id)["outcome_note"] == expected
