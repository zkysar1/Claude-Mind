""" — a status=decomposed write must re-point, or refuse over, live dependents.

THE DEFECT. `decomposed` is terminal, so the status write ran the terminal cleanup
that strips the parent's id from every blocked_by in the store, and goal-selector.py
counts a decomposed goal as done anyway. A goal waiting on the parent was released the
moment the parent was decomposed, before any child had run. Measured 2026-09-25:
g-376-51 decomposed into g-376-51-a..d with /decompose Step 6.2 skipped, and
g-376-53.blocked_by went [g-376-51, g-376-52] -> [g-376-52].

THE FIX. core/scripts/_decomposed_dependents.py plans the re-point before any
mutation and applies it ahead of the strip. Both writers call it: the daemon's
update_goal (the live path) and the CLI's cmd_update_goal (its twin, guard-547 /
guard-2323). Each writer is pinned END TO END here, because a helper that is
correct and never called is exactly how a twin goes stale.

TEST SHAPE (guard-4166). Existence pins (the dependent keeps waiting, on the right
child) and absence controls (a completed predecessor still releases its dependents,
the guard-4609 path; a dependent-free decomposition still writes). The daemon
control test disables the re-point in-process and shows the same write releasing
the dependent, so the existence pin is known to be able to fail.

Hermetic: tempdir world, in-process DaemonFixture for the daemon path, subprocess
with MIND_WORLD/MIND_META pinned and STORAGE_BACKEND=local forced for the CLI path
(guard-955: an unpinned tmp-world write lands on the production key).

Run: STORAGE_BACKEND=local python3 -m pytest \
    core/scripts/tests/test_decomposed_dependents_repoint.py -v
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
for _p in (str(SCRIPT_DIR), str(CORE_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _decomposed_dependents as DD  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402

ASP_ID = "asp-900"
PARENT = "g-900-01"
EXPECTS = "the ported module and its parity report"


def _goal(gid, status="pending", **extra):
    g = {
        "id": gid,
        "title": f"Goal {gid}",
        "description": f"Fixture goal {gid}",
        "status": status,
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    g.update(extra)
    return g


def _child(suffix, blocked_by=()):
    return _goal(f"{PARENT}-{suffix}", parent_goal=PARENT,
                 origin_signal=f"decomposition:{PARENT}", blocked_by=list(blocked_by))


def _incident_goals():
    """The  shape: a sequential chain of children, two live dependents."""
    return [
        _goal(PARENT, status="in-progress"),
        _child("a"),
        _child("b", [f"{PARENT}-a"]),
        _child("c", [f"{PARENT}-b"]),
        # waits on the parent alone: the pre-fix write leaves it blocked_by []
        _goal("g-900-02", blocked_by=[PARENT]),
        # the incident dependent: a second predecessor plus an output-passing edge
        _goal("g-900-03"),
        _goal("g-900-05", blocked_by=[PARENT, "g-900-03"],
              depends_on=[{"goal_id": PARENT, "expects": EXPECTS}]),
        # already finished: never re-pointed, the strip cleans it as before
        _goal("g-900-04", status="completed", blocked_by=[PARENT]),
    ]


def _items(goals):
    return [{"id": ASP_ID, "goals": copy.deepcopy(goals)}]


def _by_id(items):
    return {g["id"]: g for asp in items for g in asp["goals"]}


# --- the shared module (pure) -------------------------------------------------

def test_plan_names_the_live_dependents_and_the_last_child():
    items = _items(_incident_goals())
    before = copy.deepcopy(items)
    p = DD.plan(items, PARENT)
    assert p["refuse"] is False
    assert p["dependents"] == ["g-900-02", "g-900-05"], (
        "completed g-900-04 and the children themselves are not dependents")
    assert p["sinks"] == [f"{PARENT}-c"], "a sequential chain waits on its last child"
    assert items == before, "plan must not mutate: a refusal has to write nothing"


def test_apply_repoints_blocked_by_and_depends_on():
    items = _items(_incident_goals())
    lines = DD.apply(items, DD.plan(items, PARENT), PARENT)
    g = _by_id(items)
    assert g["g-900-02"]["blocked_by"] == [f"{PARENT}-c"]
    assert g["g-900-05"]["blocked_by"] == ["g-900-03", f"{PARENT}-c"]
    assert g["g-900-05"]["depends_on"] == [{"goal_id": f"{PARENT}-c", "expects": EXPECTS}], (
        "depends_on.goal_id must stay inside blocked_by (goal-schemas.md)")
    assert g["g-900-04"]["blocked_by"] == [PARENT], "a finished goal is left to the strip"
    assert len(lines) == 2
    assert all("g-115-10977" in line for line in lines)
    assert any("g-900-02" in line for line in lines)
    assert any("g-900-05" in line for line in lines)


def test_parallel_children_are_all_waited_on():
    goals = [_goal(PARENT), _child("a"), _child("b"),
             _goal("g-900-02", blocked_by=[PARENT])]
    items = _items(goals)
    DD.apply(items, DD.plan(items, PARENT), PARENT)
    assert _by_id(items)["g-900-02"]["blocked_by"] == [f"{PARENT}-a", f"{PARENT}-b"]


def test_exact_origin_counts_as_a_child_but_free_text_does_not():
    exact = _goal("g-900-07", origin_signal=f"decomposition:{PARENT}")
    free_text = _goal("g-900-08", origin_signal=f"decomposition: surfaced while running {PARENT}")
    goals = [_goal(PARENT), exact, free_text, _goal("g-900-02", blocked_by=[PARENT])]
    p = DD.plan(_items(goals), PARENT)
    assert p["sinks"] == ["g-900-07"]


def test_live_dependents_and_no_children_refuses_naming_them():
    goals = [_goal(PARENT), _goal("g-900-02", blocked_by=[PARENT]),
             _goal("g-900-06", status="blocked", blocked_by=["g-900-99", PARENT])]
    items = _items(goals)
    p = DD.plan(items, PARENT)
    assert p["refuse"] is True
    assert "g-900-02" in p["message"] and "g-900-06" in p["message"]
    assert DD.apply(items, p, PARENT) == []
    assert _by_id(items)["g-900-02"]["blocked_by"] == [PARENT], "a refusal changes nothing"


def test_no_dependents_is_a_noop_with_or_without_children():
    for goals in ([_goal(PARENT)], [_goal(PARENT), _child("a")]):
        items = _items(goals)
        p = DD.plan(items, PARENT)
        assert p["refuse"] is False and p["dependents"] == []
        assert DD.apply(items, p, PARENT) == []


# --- end to end through both writers -----------------------------------------

def _make_world(tmp: Path, goals) -> tuple[Path, Path]:
    world = tmp / "world"
    world.mkdir()
    asp = {
        "id": ASP_ID,
        "title": "decomposed dependents",
        "motivation": "Pin that decomposing a goal keeps its dependents waiting",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-09-01T00:00:00",
        "goals": goals,
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    agent_dir = tmp / "alpha"
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _stored(world: Path) -> dict:
    out = {}
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            for g in json.loads(line).get("goals", []):
                out[g["id"]] = g
    return out


def _daemon_status(port: int, goal_id: str, status: str) -> tuple[int, dict]:
    """POST the same update-goal call aspirations-update-goal.sh makes."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field=status&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(status).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _cli_status(world: Path, goal_id: str, status: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "MIND_WORLD": str(world),
        "MIND_META": str(world.parent / "meta"),
        "MIND_AGENT": "alpha",
        "STORAGE_BACKEND": "local",
    })
    (world.parent / "meta").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "aspirations.py"),
         "--source", "world", "update-goal", goal_id, "status", status],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=120)


def _assert_repointed(g: dict) -> None:
    assert g[PARENT]["status"] == "decomposed"
    assert g["g-900-02"]["blocked_by"] == [f"{PARENT}-c"], (
        "the dependent must wait on the last child, never on nothing")
    assert g["g-900-05"]["blocked_by"] == ["g-900-03", f"{PARENT}-c"]
    assert g["g-900-05"]["depends_on"] == [{"goal_id": f"{PARENT}-c", "expects": EXPECTS}]
    assert g["g-900-04"]["blocked_by"] == [], "the strip still cleans a finished goal"


def test_daemon_decomposed_write_repoints_live_dependents():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _incident_goals())
        with DaemonFixture(world) as df:
            code, body = _daemon_status(df.port, PARENT, "decomposed")
        assert code == 200, f"update-goal status={code}; body={body!r}"
        warnings = body.get("warnings") or []
        assert any("g-900-02" in w and "g-115-10977" in w for w in warnings), (
            f"the re-point must reach the caller's stderr via warnings[]: {warnings!r}")
        _assert_repointed(_stored(world))


def test_daemon_control_without_the_repoint_the_dependent_is_released():
    """Positive control: the same write with the re-point disabled strips the edge."""
    original = DD.apply
    DD.apply = lambda items, result, parent_id: []
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            world, _ = _make_world(Path(tmpd), _incident_goals())
            with DaemonFixture(world) as df:
                code, body = _daemon_status(df.port, PARENT, "decomposed")
            assert code == 200, f"update-goal status={code}; body={body!r}"
            assert _stored(world)["g-900-02"]["blocked_by"] == [], (
                "without the re-point the harness must see the pre-fix release; "
                "if this fails, the existence pin above proves nothing")
    finally:
        DD.apply = original


def test_daemon_refuses_when_no_child_can_take_the_dependents():
    goals = [_goal(PARENT, status="in-progress"), _goal("g-900-02", blocked_by=[PARENT])]
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), goals)
        with DaemonFixture(world) as df:
            code, body = _daemon_status(df.port, PARENT, "decomposed")
        assert code == 409, f"expected a refusal; status={code}; body={body!r}"
        assert body.get("error") == "decomposed_dependents_unrepointable"
        assert "g-900-02" in json.dumps(body)
        g = _stored(world)
        assert g[PARENT]["status"] == "in-progress", "a refusal writes nothing"
        assert g["g-900-02"]["blocked_by"] == [PARENT]


def test_daemon_completion_still_releases_dependents():
    """guard-4609's path is unchanged: completing a predecessor releases its dependent."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _incident_goals())
        with DaemonFixture(world) as df:
            code, body = _daemon_status(df.port, PARENT, "completed")
        assert code == 200, f"update-goal status={code}; body={body!r}"
        g = _stored(world)
        assert g["g-900-02"]["blocked_by"] == [], "completion is not decomposition"
        assert not any("g-115-10977" in w for w in (body.get("warnings") or []))


def test_cli_decomposed_write_repoints_live_dependents():
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _incident_goals())
        proc = _cli_status(world, PARENT, "decomposed")
        assert proc.returncode == 0, (
            f"CLI update-goal failed rc={proc.returncode}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}")
        assert "re-pointed g-900-02" in proc.stderr, proc.stderr
        _assert_repointed(_stored(world))


def test_cli_refuses_when_no_child_can_take_the_dependents():
    goals = [_goal(PARENT, status="in-progress"), _goal("g-900-02", blocked_by=[PARENT])]
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), goals)
        proc = _cli_status(world, PARENT, "decomposed")
        assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
        assert "decomposed_dependents_unrepointable" in proc.stderr
        assert "g-900-02" in proc.stderr
        g = _stored(world)
        assert g[PARENT]["status"] == "in-progress"
        assert g["g-900-02"]["blocked_by"] == [PARENT]
