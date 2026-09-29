"""commons-primer.sh / .py: when an agent's commons primer is drawn and shown ().

`birth` (/boot Phase -2, every boot) draws once per (agent, environment), and only
at the agent's first entry to that environment. It never aborts boot. `show`
(/prime Phase 2) prints that environment's primer until 10 of its world's goals
have completed since it was drawn. The goal reader is monkeypatched and the
producer is a fake script in a tmp world, so nothing here reads a store, spends a
draw or touches the network. Each rule is pinned with its boundary: 9 goals is new
and 10 is not; 9 goals still show the primer and 10 retire it. The
cross-environment case runs with two environment ids: the primer for A is kept, B
gets its own, and a second boot in B draws nothing.
"""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from _bash_helpers import BASH

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "commons-primer.py"

# The fake producer records its argv and writes the primer where it is told, as
# the real one does, so a second boot finds it.
FAKE_PRODUCER = r'''#!/usr/bin/env bash
echo "$@" >> "$(dirname "$0")/args.txt"
out=""; prev=""
for a in "$@"; do [ "$prev" = "--primer-out" ] && out="$a"; prev="$a"; done
mkdir -p "$(dirname "$out")"
printf 'drawn for %s\n' "$out" > "$out"
echo "[commons-primer] listed=3 validated=2 matched=2 drawn=2 primer=written:$out"
'''


@pytest.fixture
def cp():
    spec = importlib.util.spec_from_file_location("commons_primer_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(mod, monkeypatch, rows):
    """Stand in for aspirations-query.sh: the union of both queues, as it returns them."""
    monkeypatch.setattr(mod, "_read_list", lambda cmd, agent: rows)


def _unexpected(*args, **kwargs):
    raise AssertionError("read the goal stores when nothing needed measuring")


def make_world(path):
    (path / "scripts").mkdir(parents=True)
    (path / "scripts" / "commons-retrieve.sh").write_text(FAKE_PRODUCER, encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path / "world")


@pytest.fixture
def agent_dir(tmp_path):
    d = tmp_path / "agent"
    d.mkdir()
    return d


def calls(world):
    p = world / "scripts" / "args.txt"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_birth_draws_at_first_entry_and_passes_the_producer_line_through(cp, monkeypatch, world, agent_dir):
    _rows(cp, monkeypatch, [])
    lines = cp.birth("newbie", agent_dir, world, "env-a")
    primer = agent_dir / "commons-primer" / "env-a.md"
    assert lines == [f"[commons-primer] listed=3 validated=2 matched=2 drawn=2 primer=written:{primer}"], lines
    args = calls(world)[0].split()
    assert args[0] == "--primer", args
    assert str(primer) in args and str(agent_dir / "self.md") in args, args
    assert str(world / "program.md") in args, "not ranked with this environment's program"


@pytest.mark.parametrize("n_world", [10, 249])
def test_birth_never_draws_for_a_veteran_of_this_environment(cp, monkeypatch, world, agent_dir, n_world):
    _rows(cp, monkeypatch, [{"source": "world"}] * n_world)
    lines = cp.birth("veteran", agent_dir, world, "env-a")
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] not drawn: veteran is not new to env-a"), lines
    assert not calls(world), "a paid draw fired for a veteran of this environment"


def test_birth_at_nine_goals_in_this_world_still_draws(cp, monkeypatch, world, agent_dir):
    """The boundary control for the case above: 9 is new, 10 is not."""
    _rows(cp, monkeypatch, [{"source": "world"}] * 9)
    cp.birth("newbie", agent_dir, world, "env-a")
    assert len(calls(world)) == 1


def test_a_trained_agent_entering_a_second_environment_is_primed_there_once_and_keeps_the_first(
        cp, monkeypatch, tmp_path, agent_dir):
    world_a, world_b = make_world(tmp_path / "world-a"), make_world(tmp_path / "world-b")
    _rows(cp, monkeypatch, [])
    cp.birth("alpha", agent_dir, world_a, "env-a")
    primer_a = agent_dir / "commons-primer" / "env-a.md"
    kept = primer_a.read_bytes()
    # Trained in A. B's world holds none of its goals; the 13 agent-queue goals
    # travel with its agent dir and must not count as a stay in B.
    _rows(cp, monkeypatch, [{"source": "agent"}] * 13)
    cp.birth("alpha", agent_dir, world_b, "env-b")
    primer_b = agent_dir / "commons-primer" / "env-b.md"
    assert primer_b.is_file() and len(calls(world_b)) == 1, calls(world_b)
    assert f"--primer-out {primer_b}" in calls(world_b)[0], calls(world_b)
    assert str(world_b / "program.md") in calls(world_b)[0], "not ranked for environment B"
    # A second boot in B draws nothing and measures nothing.
    monkeypatch.setattr(cp, "_read_list", _unexpected)
    lines = cp.birth("alpha", agent_dir, world_b, "env-b")
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] present:"), lines
    assert len(calls(world_b)) == 1
    # The primer for A is kept, byte for byte, and A was drawn for once.
    assert primer_a.read_bytes() == kept and len(calls(world_a)) == 1


def test_birth_does_not_draw_when_the_stay_cannot_be_measured(cp, monkeypatch, world, agent_dir):
    def boom(cmd, agent):
        raise RuntimeError("aspirations-query.sh rc=1")
    monkeypatch.setattr(cp, "_read_list", boom)
    lines = cp.birth("who", agent_dir, world, "env-a")
    assert len(lines) == 1 and "could not measure whether who is new to env-a" in lines[0], lines
    assert not calls(world)


def test_rows_without_a_source_field_are_not_read_as_a_new_agent(cp, monkeypatch, world, agent_dir):
    """A renamed field must not turn a veteran's 249 goals into zero and pay for a draw."""
    _rows(cp, monkeypatch, [{"goal_id": "g-1-1"}] * 249)
    lines = cp.birth("veteran", agent_dir, world, "env-a")
    assert len(lines) == 1 and "carry no `source` field" in lines[0], lines
    assert not calls(world)


@pytest.mark.parametrize("env_id", [None, "", ".."])
def test_birth_without_an_environment_id_prints_one_line_and_draws_nothing(cp, monkeypatch, world, agent_dir, env_id):
    monkeypatch.setattr(cp, "_read_list", _unexpected)
    lines = cp.birth("newbie", agent_dir, world, env_id)
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] not drawn: ENVIRONMENT_ID is not set"), lines
    assert not calls(world) and not (agent_dir / "commons-primer").exists()


def test_birth_with_no_producer_prints_one_loud_line(cp, monkeypatch, tmp_path, agent_dir):
    _rows(cp, monkeypatch, [])
    lines = cp.birth("newbie", agent_dir, tmp_path / "world-without-the-script", "env-a")
    assert lines == ["[commons-primer] SKIPPED: no producer at $WORLD_DIR/scripts/commons-retrieve.sh. "
                     "This agent starts without a commons primer; boot is not affected."], lines


PRIMER = ('---\nkind: commons-birth-primer\nagent: "newbie"\nenvironment_id: "env-b"\n'
          'created_at: "2026-09-28T17:00:00"\nshow_for_first_goals: 10\n---\n'
          '# Commons primer: lessons other agents learned\n\n## 1. sig-good\n- Lesson: lesson sig-good\n')


def _done(world_after, world_before=0, agent_after=0):
    return ([{"source": "world", "completed_at": "2026-09-28T18:00:00"}] * world_after
            + [{"source": "world", "completed_at": "2026-09-27T09:00:00"}] * world_before
            + [{"source": "agent", "completed_at": "2026-09-28T18:00:00"}] * agent_after)


@pytest.fixture
def primed(agent_dir):
    (agent_dir / "commons-primer").mkdir()
    (agent_dir / "commons-primer" / "env-b.md").write_text(PRIMER, encoding="utf-8")
    return agent_dir


def test_show_prints_this_environments_primer_during_its_first_goals(cp, monkeypatch, primed):
    # Goals completed BEFORE the primer was drawn, and agent-queue goals, do not count.
    _rows(cp, monkeypatch, _done(9, world_before=50, agent_after=30))
    lines = cp.show("newbie", primed, "env-b")
    assert lines[0].startswith("[commons-primer] shown for env-b: 9 of the first 10 goals here"), lines
    assert "lesson sig-good" in lines[1] and "kind: commons-birth-primer" not in lines[1], lines


def test_show_retires_the_primer_after_ten_goals_in_this_environment(cp, monkeypatch, primed):
    _rows(cp, monkeypatch, _done(10))
    assert cp.show("newbie", primed, "env-b") == [
        "[commons-primer] not shown: retired after 10 goals in env-b "
        "(completed since 2026-09-28T17:00:00: 10)."]


def test_show_never_prints_another_environments_primer(cp, monkeypatch, primed):
    monkeypatch.setattr(cp, "_read_list", _unexpected)
    lines = cp.show("newbie", primed, "env-a")
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] not shown: no primer for env-a at"), lines


def test_show_fails_toward_showing_when_goals_cannot_be_counted(cp, monkeypatch, primed):
    def boom(cmd, agent):
        raise RuntimeError("aspirations-query.sh rc=1")
    monkeypatch.setattr(cp, "_read_list", boom)
    lines = cp.show("newbie", primed, "env-b")
    assert "shown anyway" in lines[0] and "lesson sig-good" in lines[1], lines


def test_show_without_an_environment_id_prints_one_line(cp, monkeypatch, primed):
    monkeypatch.setattr(cp, "_read_list", _unexpected)
    lines = cp.show("newbie", primed, None)
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] not shown: ENVIRONMENT_ID is not set"), lines


def test_the_wrapper_exits_zero_with_one_line_for_an_agent_without_a_primer():
    env = {**os.environ, "STORAGE_BACKEND": "local", "MIND_AGENT": "_commons_primer_gate_fixture_"}
    env.pop("MIND_SID", None)
    r = subprocess.run([BASH, (SCRIPTS / "commons-primer.sh").as_posix(), "show"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and lines[0].startswith("[commons-primer] not shown:"), r.stdout
