"""POST /v1/wm/{set,append,clear,prune,init,reset,clear-identity}, GET /v1/wm/ages.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): endpoints wired, the
     set/append/clear/prune/init/ages flows work end-to-end incl. the
     agent-header gate, structured-dict refusal, and knowledge_debt validation.
  2. Byte-compat (direct handler vs the REAL CLI wm.py): working-memory.yaml is
     byte-identical for a TOP-LEVEL key set (top-level writes skip slot_meta
     timestamping, so the file is fully deterministic). Both sides read the
     real core/config/memory-pipeline.yaml so _default_wm_data matches.

The CLI is redirected with MIND_AGENT_DIR (unit-test override) so it writes to
a temp agent dir, never the real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
WM_PY = REPO_ROOT / "core" / "scripts" / "wm.py"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(port, path, query, body=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _get(port, path, query=None, *, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_wm(agent_dir: Path) -> dict:
    p = agent_dir / "session" / "working-memory.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_set_slot_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "active_strategy"},
                         '"breadth-first"')
    assert status == 200, body
    wm = _read_wm(agent_dir)
    assert wm["slots"]["active_strategy"] == "breadth-first"
    assert wm["slot_meta"]["active_strategy"]["updated_at"]


def test_set_top_level_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "last_goal_category"},
                         "framework")
    assert status == 200, body
    assert _read_wm(agent_dir)["last_goal_category"] == "framework"


def test_set_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "active_strategy"}, '"x"', agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 without X-Mind-Agent")


def test_set_structured_dict_rejected(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/set", {"slot": "loop_state"}, '"a bare string"')
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "structured_dict_required"
    else:
        raise AssertionError("expected 400 for non-dict loop_state write")


def test_set_loop_state_dict_ok(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/set", {"slot": "loop_state"},
                         json.dumps({"signals": {"quiescence": False}}))
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["loop_state"]["signals"]["quiescence"] is False


def test_append_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/append", {"slot": "known_blockers"},
                         json.dumps({"id": "blk-1", "reason": "waiting"}))
    assert status == 200, body
    arr = _read_wm(agent_dir)["slots"]["known_blockers"]
    assert arr[-1]["id"] == "blk-1"
    assert "_item_ts" in arr[-1]


def test_append_knowledge_debt_invalid_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/wm/append", {"slot": "knowledge_debt"},
              json.dumps({"node_key": "no-such-node"}))
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "validation_failed"
    else:
        raise AssertionError("expected 400 for unresolvable knowledge_debt node_key")


def test_append_heals_int_in_goals_completed_list_slot(running_daemon):
    """2026-08-16 worker-loop Phase 4b outage: the TOP-LEVEL
    goals_completed_this_session (a LIST of hand-off rows) had been collapsed
    to an int on 3 of 3 forked Bodies checked, and every append was refused
    `not_a_list` forever — body-merge then dropped the Body's contribution
    silently. An int there carries no rows, so it is always corruption: the
    endpoint heals it to [] IN THE SAME REQUEST, appends, and says so."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    wm_path = agent_dir / "session" / "working-memory.yaml"
    data = _read_wm(agent_dir)
    data["goals_completed_this_session"] = 0          # the collided counter shape
    wm_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    status, body = _post(port, "/v1/wm/append", {"slot": "goals_completed_this_session"},
                         json.dumps({"goal_id": "g-999-01", "aspiration_id": "asp-999",
                                     "recurring": False}))
    assert status == 200, body
    out = json.loads(body)
    assert out["ok"] is True and out["healed_from"] == "int:0", out
    assert "warning" in out and "counter" in out["warning"], out
    rows = _read_wm(agent_dir)["goals_completed_this_session"]
    assert isinstance(rows, list) and rows[-1]["goal_id"] == "g-999-01", rows
    # A second append is a plain append: no heal reported, both rows kept.
    status, body = _post(port, "/v1/wm/append", {"slot": "goals_completed_this_session"},
                         json.dumps({"goal_id": "g-999-02"}))
    assert status == 200 and "healed_from" not in json.loads(body), body
    assert [r["goal_id"] for r in _read_wm(agent_dir)["goals_completed_this_session"]] == \
        ["g-999-01", "g-999-02"]


def test_append_other_type_mismatch_still_refused(running_daemon):
    """The heal is scoped to the ONE colliding slot: an int in any other array
    slot, and a non-numeric scalar in this one, still refuse `not_a_list`."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    wm_path = agent_dir / "session" / "working-memory.yaml"
    data = _read_wm(agent_dir)
    data["slots"]["known_blockers"] = 3
    data["goals_completed_this_session"] = "seven"
    wm_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    for slot in ("known_blockers", "goals_completed_this_session"):
        try:
            _post(port, "/v1/wm/append", {"slot": slot}, json.dumps({"id": "x"}))
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert json.loads(e.read())["error"] == "not_a_list", slot
        else:
            raise AssertionError(f"expected not_a_list 400 for {slot}")


def test_append_not_initialized_400(running_daemon):
    project_root, port = running_daemon
    # Use bravo, whose conftest dir has no working-memory.yaml.
    try:
        _post(port, "/v1/wm/append", {"slot": "known_blockers"},
              json.dumps({"id": "x"}), agent="bravo")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "not_initialized"
    else:
        raise AssertionError("expected 400 appending to uninitialized WM")


def test_clear_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    status, body = _post(port, "/v1/wm/clear", {"slot": "active_strategy"})
    assert status == 200, body
    assert _read_wm(agent_dir)["slots"]["active_strategy"] is None


# ---------------------------------------------------------------------------
# POST /v1/wm/drain-goals ()
# ---------------------------------------------------------------------------

def _seed(port, slot, entries):
    status, body = _post(port, "/v1/wm/set", {"slot": slot}, json.dumps(entries))
    assert status == 200, body


def test_drain_goals_removes_only_matching(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [
        {"goal_id": "g-1-01", "note": "a"},
        {"goal_id": "g-1-01", "note": "b"},
        {"goal_id": "g-2-02", "note": "c"},
    ])
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-1-01"]))
    assert status == 200, body
    verdict = json.loads(body)
    assert (verdict["removed"], verdict["kept"]) == (2, 1), verdict
    survivors = _read_wm(agent_dir)["slots"]["exp_capture"]
    assert [e["goal_id"] for e in survivors] == ["g-2-02"], survivors


def test_drain_goals_keeps_entries_it_cannot_classify(running_daemon):
    """An entry the classifier cannot classify must not be destroyed by it."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "encoding_capture", [
        {"goal_id": "g-3-03", "note": "doomed"},
        {"note": "no goal_id at all"},
        "a bare string, not a dict",
    ])
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "encoding_capture"},
                         json.dumps(["g-3-03"]))
    assert status == 200, body
    assert json.loads(body)["removed"] == 1
    survivors = _read_wm(agent_dir)["slots"]["encoding_capture"]
    assert len(survivors) == 2, survivors
    assert "a bare string, not a dict" in survivors


def test_drain_goals_takes_ids_never_a_survivor_list(running_daemon):
    """guard-3881: the API shape is what makes the lost update impossible.

    The retired design read the slot, filtered in the CALLER, and POSTed the
    surviving list to /v1/wm/set — a full-slot overwrite of a stale snapshot,
    so any entry appended in between was destroyed. This endpoint accepts the
    goal-id SET only; a survivor list (dicts) carries no usable id and is
    refused outright, so the old shape cannot be resurrected by accident.
    """
    _, port = running_daemon
    _seed(port, "exp_capture", [{"goal_id": "g-4-04"}])
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps([{"goal_id": "g-9-09", "note": "survivor"}]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "empty_goal_ids" in body, body


def test_drain_goals_survives_an_append_the_caller_never_saw(running_daemon):
    """The filter runs on data read INSIDE the handler, so a late append lives.

    This is the behaviour the endpoint exists for: the caller's view of the slot
    is irrelevant because it never sends one. An entry appended after the caller
    decided — for a goal that is NOT being drained — must still be present after
    the drain.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-5-05", "note": "old"}])
    # The caller decides on ["g-5-05"] here. THEN a peer appends:
    status, body = _post(port, "/v1/wm/append", {"slot": "exp_capture"},
                         json.dumps({"goal_id": "g-6-06", "note": "late"}))
    assert status == 200, body
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-5-05"]))
    assert status == 200, body
    survivors = _read_wm(agent_dir)["slots"]["exp_capture"]
    assert [e["goal_id"] for e in survivors] == ["g-6-06"], survivors


def test_drain_goals_refuses_a_non_capture_slot(running_daemon):
    """Blast radius: the only destructive goal-keyed primitive stays scoped."""
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "conclusions"},
                             json.dumps(["g-7-07"]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "slot_not_drainable" in body, body


def test_drain_goals_refuses_an_empty_id_array(running_daemon):
    """A drain with no ids is a caller defect, and must be loud, not a no-op."""
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps([]))
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body
    assert "empty_goal_ids" in body, body


def test_drain_goals_advances_slot_meta(running_daemon):
    """guard-540: wm-prune ages a lane from slot_meta.updated_at.

    A drain that left the meta untouched would leave the lane aged from its last
    APPEND, so the prune cadence could evict the very survivors it just spared.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-8-08"}, {"goal_id": "g-9-09"}])
    before = _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"]
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-8-08"]))
    assert status == 200, body
    meta = _read_wm(agent_dir)["slot_meta"]["exp_capture"]
    assert meta["update_count"] == before + 1, meta
    assert meta["updated_at"], meta


def test_drain_goals_no_match_leaves_the_slot_alone(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed(port, "exp_capture", [{"goal_id": "g-10-10"}])
    before = _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"]
    status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                         json.dumps(["g-nope-99"]))
    assert status == 200, body
    assert json.loads(body) == {"ok": True, "slot": "exp_capture",
                                "removed": 0, "kept": 1}
    # No write at all, so no meta bump either.
    assert _read_wm(agent_dir)["slot_meta"]["exp_capture"]["update_count"] == before


def test_drain_goals_requires_agent_header(running_daemon):
    _, port = running_daemon
    try:
        status, body = _post(port, "/v1/wm/drain-goals", {"slot": "exp_capture"},
                             json.dumps(["g-1-01"]), agent=None)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    assert status == 400, body


def test_init_roundtrip(running_daemon):
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "bravo"
    status, body = _post(port, "/v1/wm/init", {}, agent="bravo")
    assert status == 200, body
    assert json.loads(body)["slots"] >= 1
    assert (agent_dir / "session" / "working-memory.yaml").exists()


def test_ages_roundtrip(running_daemon):
    _, port = running_daemon
    status, body = _get(port, "/v1/wm/ages")
    assert status == 200, body
    data = json.loads(body)
    assert "active_context" in data


def test_prune_dry_run(running_daemon):
    _, port = running_daemon
    status, body = _post(port, "/v1/wm/prune", {"dry_run": "1"})
    assert status == 200, body
    data = json.loads(body)
    assert data["dry_run"] is True
    assert "report" in data


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, agent: Path, project_root: Path, world: Path):
        self.agent = agent
        self.project_root = project_root
        self.world = world
        self.agent_name = "alpha"

    def wm_path(self, unit_key=None):
        # Mirrors AgentPaths.wm_path ( per-Body routing): no unit_key /
        # no forked body-WM-file collapses to the agent-wide WM. Tests pass no
        # SID header, so the fallback is the only branch exercised.
        return self.agent / "session" / "working-memory.yaml"


class _FakeCtx:
    def __init__(self, agent: Path, project_root: Path, world: Path,
                 query: dict, body: bytes, *, agent_name="alpha"):
        self.paths = _FakePaths(agent, project_root, world)
        self.query = query
        self.body = body
        self.headers = {"x-mind-agent": agent_name}


def _run_wm_cli(world, meta, agent_dir, args, stdin_text):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    env["MIND_AGENT_DIR"] = str(agent_dir)
    world.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(WM_PY), *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI wm.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


@pytest.mark.skipif(yaml is None, reason="PyYAML required")
@pytest.mark.skipif(not WM_PY.exists(), reason="core/scripts/wm.py missing")
def test_byte_compat_set_top_level(tmp_path):
    """Top-level set self-heals to _default_wm_data (deterministic, no
    slot_meta timestamps) then sets the key — fully byte-comparable. Both
    sides read the REAL memory-pipeline.yaml so slot_types match."""
    from mind_api.src.endpoints import wm_write

    cli_agent = tmp_path / "cli-agent"
    dae_agent = tmp_path / "dae-agent"

    _run_wm_cli(tmp_path / "world", tmp_path / "meta", cli_agent,
                ["set", "last_goal_category"], "framework-loop")
    wm_write.set_slot(_FakeCtx(dae_agent, REPO_ROOT, tmp_path / "world",
                               {"slot": "last_goal_category"},
                               b"framework-loop"))

    cli_wm = (cli_agent / "session" / "working-memory.yaml").read_bytes()
    dae_wm = (dae_agent / "session" / "working-memory.yaml").read_bytes()
    assert dae_wm == cli_wm


# ---------------------------------------------------------------------------
# : the WRAPPER layer — wm-append.sh discarded the daemon response
# ---------------------------------------------------------------------------
#
#  fixed the DAEMON: a newly appended entry is never its own eviction
# victim, and test_capture_eviction_newcomer.py pins that behaviour. This file
# covers the layer that fix does not touch. wm-append.sh discarded the entire
# response with `> /dev/null`, so `evicted` — reported by the daemon since
#  — had never once reached a caller. A fix is not shipped when the
# producer emits it; it is shipped when a consumer displays it (guard-742/547
# one layer further out: daemon-vs-wrapper, not CLI-vs-daemon).
#
# Post- an eviction always destroys an OLD entry. WHICH old entry is
# floor-aware since  and this comment named the wrong branch until
# : when flagged entries exceed (cap - _unflagged_floor) the oldest
# FLAGGED one goes, otherwise the oldest UNFLAGGED one goes. The seed below is
# 100% load_bearing, so THIS test exercises the FLAGGED branch — the recoverable
# one, since a flagged entry was mirrored to the Body's carrier at append time
# and still reaches the reducer from there. The unflagged branch is the
# unrecoverable one (the WM slot is that entry's only copy).
#
# known_blockers (array_limits 10) is used rather than a capture lane: the
# behaviour is slot-agnostic, and known_blockers has no per-slot validation.

_KB_LIMIT = 10


def _seed_known_blockers(agent_dir: Path, n: int, *, load_bearing: bool):
    """Write n incumbents straight to disk so cap state is exact, not inferred."""
    p = agent_dir / "session" / "working-memory.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data.setdefault("slots", {})["known_blockers"] = [
        {"id": f"seed-{i}", "reason": "incumbent",
         "load_bearing": load_bearing, "_item_ts": "2020-01-01T00:00:00"}
        for i in range(n)
    ]
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


WM_APPEND_SH = REPO_ROOT / "core" / "scripts" / "wm-append.sh"


@pytest.mark.skipif(not WM_APPEND_SH.exists(), reason="wm-append.sh missing")
def test_wrapper_surfaces_eviction_on_stderr(running_daemon):
    """The THIRD layer, and the one that made the daemon-side fix inert without
    it: wm-append.sh discarded the entire response with `> /dev/null`, so the
    `evicted` field the daemon has reported since g-306-289 never reached a
    single operator. A daemon-only fix would have shipped and changed nothing
    an agent can see (guard-742 class).

    STDOUT must stay empty — the documented wrapper contract is "print nothing
    on success" and callers parse it. The diagnostic goes to STDERR.
    """
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_known_blockers(agent_dir, _KB_LIMIT, load_bearing=True)

    env = dict(os.environ)
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    proc = subprocess.run(
        ["bash", str(WM_APPEND_SH), "known_blockers"],
        input=json.dumps({"id": "wrapper-probe", "reason": "destroyed"}),
        text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip() == "", (
        "the wrapper contract is silent-stdout on success; a diagnostic there "
        f"would break every caller that parses it: {proc.stdout!r}")
    assert "[wm-append]" in proc.stderr, (
        "the eviction must reach the operator, not die in `> /dev/null`: "
        f"{proc.stderr!r}")
    assert "1 older entry evicted" in proc.stderr, proc.stderr

    # : the notice must NAME BOTH victim branches and must not assert
    # the one it cannot observe. The wrapper receives `evicted` as a bare COUNT,
    # so claiming a victim class is a claim beyond what its path saw
    # (guard-2947). The previous wording said the victim is unconditionally "the
    # one that has waited longest for the reducer" — false in the flagged branch
    # THIS test's own 100%-load_bearing seed exercises, and not harmlessly: a
    # peer Body read that line, concluded that relaying through a capture lane
    # would destroy another goal's undelivered observation, and routed around
    # the lane when the append it declined would have evicted a carrier-backed
    # peer.
    assert "floor-aware" in proc.stderr, proc.stderr
    for branch in ("FLAGGED", "UNFLAGGED"):
        assert branch in proc.stderr, (
            f"the notice must name the {branch} branch so the reader can tell a "
            f"recoverable eviction from an unrecoverable one: {proc.stderr!r}")
    # Negative control on the specific false claim, so a revert to the
    # unconditional wording fails here rather than silently re-misinforming.
    assert "The victim is the OLDEST peer" not in proc.stderr, (
        "regressed to the unconditional victim claim g-306-353 removed: "
        f"{proc.stderr!r}")

    # : the newcomer is protected, so it is the entry that SURVIVES
    # and an old peer is the one destroyed. Asserting this here keeps the
    # wrapper test honest about which layer it is exercising.
    arr = _read_wm(agent_dir)["slots"]["known_blockers"]
    assert any(e.get("id") == "wrapper-probe" for e in arr), arr
    assert len(arr) == _KB_LIMIT


@pytest.mark.skipif(not WM_APPEND_SH.exists(), reason="wm-append.sh missing")
def test_wrapper_stays_silent_on_an_ordinary_append(running_daemon):
    """No-false-positive half for the wrapper. Almost every append is ordinary;
    a wrapper that printed on all of them would train callers to ignore it."""
    project_root, port = running_daemon
    agent_dir = project_root / "agents" / "alpha"
    _seed_known_blockers(agent_dir, 2, load_bearing=False)

    env = dict(os.environ)
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    proc = subprocess.run(
        ["bash", str(WM_APPEND_SH), "known_blockers"],
        input=json.dumps({"id": "quiet-probe", "reason": "fits"}),
        text=True, env=env, cwd=str(REPO_ROOT), capture_output=True, timeout=60,
    )

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip() == "", proc.stdout
    assert "evicted to make room" not in proc.stderr, proc.stderr
    assert any(e.get("id") == "quiet-probe"
               for e in _read_wm(agent_dir)["slots"]["known_blockers"])
