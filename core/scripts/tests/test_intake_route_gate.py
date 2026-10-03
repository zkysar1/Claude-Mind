"""test_intake_route_gate.py — B2: the candidate tier's routing gate ().

goal-intake-management.md §3 routes a NEW goal by the head of its
origin_signal, and §8 ships the flag dark. WHAT THIS SUITE PINS:

  1. THE ROUTING TABLE, one test per §3 row: flag off, world-source user
     context, every exempt origin, every candidate origin, maintain filed
     completed, and the unknown head that FAILS OPEN to pending (I1).
  2. ONLY `candidate` CHANGES A GOAL: apply() leaves every other verdict's
     goal exactly as it arrived, and with the flag off it changes nothing for
     any head or status (the flag-off byte-identity the goal asks for).
  3. CONFIG: only a literal `enabled: true` with both origin lists turns the
     gate on; a missing file, block or key reads as off silently (rb-1918), and a
     config that cannot be USED (unreadable, unparseable, not a mapping, or
     enabled with origin lists that are not lists) reads as off WITH a stderr
     warning (I1, guard-424). The repo config's flag is asserted to be a literal
     bool, NOT pinned to a value (g-353-164: B6 flips it in that very file), and
     its lists are the §3 sets.
  4. ONE USER-CONTEXT PREDICATE, shared with the origin-signal gate.
  5. PLACEMENT: both daemon write sites call the routing step AFTER the
     origin-signal gate.
  6. BOTH PRODUCTION DOORS (add-goal, and a whole aspiration through add): a
     fixture daemon files an investigate goal as `candidate` and an
     alert-email goal as `pending` with the flag on, and the investigate goal
     as `pending` with the flag off.

Run: py -3 -m pytest core/scripts/tests/test_intake_route_gate.py -q
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
DAEMON_PATH = (PROJECT_ROOT / "mind_api" / "src" / "endpoints"
               / "aspirations_write.py")
CONFIG_PATH = PROJECT_ROOT / "core" / "config" / "aspirations.yaml"

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402
from gates import intake_route as ir  # noqa: E402
from gates.origin_signal import world_user_context  # noqa: E402

AGENT = "alpha"

# §3, verbatim.
SPEC_CANDIDATE = {"investigate", "idea", "maintain"}
SPEC_EXEMPT = {"unblock", "alert-email", "failing_test", "drift_detected",
               "user_directive", "user_directed", "decomposition",
               "insight_trigger"}

ON = {"enabled": True, "candidate_origins": frozenset(SPEC_CANDIDATE),
      "exempt_origins": frozenset(SPEC_EXEMPT)}


def _route(origin_signal, status=None, *, config=ON, user_context=False):
    goal = {"title": "t", "origin_signal": origin_signal}
    if status is not None:
        goal["status"] = status
    return ir.route_intake(goal, config=config, user_context=user_context)


# ---------------------------------------------------------------------------
# 1. The routing table, row by row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [(None, "pending"),
                                             ("pending", "pending"),
                                             ("completed", "completed")])
def test_flag_off_files_the_filers_status(status, expected):
    assert _route("investigate:x", status, config=ir.OFF) == expected


def test_user_context_files_pending_even_for_a_candidate_origin():
    assert _route("investigate:x", user_context=True) == "pending"


@pytest.mark.parametrize("head", sorted(SPEC_EXEMPT))
def test_exempt_origins_file_pending(head):
    assert _route(f"{head}:x") == "pending"


@pytest.mark.parametrize("signal", ["investigate:x", "idea:forge-ready-y",
                                    "maintain:z"])
def test_candidate_origins_file_candidate(signal):
    assert _route(signal) == "candidate"


@pytest.mark.parametrize("status", [None, "pending"])
def test_maintain_not_completed_files_candidate(status):
    assert _route("maintain:z", status) == "candidate"


def test_maintain_filed_completed_stays_completed():
    assert _route("maintain:z", "completed") == "completed"


@pytest.mark.parametrize("signal", ["spark:x", "investigatex:y", "Idea:x", "",
                                    None])
def test_unknown_heads_fail_open_to_pending(signal):
    assert _route(signal) == "pending"


# ---------------------------------------------------------------------------
# 2. Only "candidate" changes a goal
# ---------------------------------------------------------------------------

def test_apply_writes_candidate():
    goal = {"title": "t", "origin_signal": "idea:x"}
    assert ir.apply(goal, config=ON, source="world", agent_name=AGENT) == "candidate"
    assert goal["status"] == "candidate"


@pytest.mark.parametrize("signal,status", [("unblock:g-1-1", None),
                                           ("maintain:z", "completed"),
                                           ("spark:x", None),
                                           ("alert-email:x", "pending")])
def test_apply_leaves_every_other_verdict_untouched(signal, status):
    goal = {"title": "t", "origin_signal": signal}
    if status is not None:
        goal["status"] = status
    before = copy.deepcopy(goal)
    ir.apply(goal, config=ON, source="world", agent_name=AGENT)
    assert goal == before


def test_apply_with_the_flag_off_changes_nothing_for_any_head_or_status():
    for head in sorted(SPEC_CANDIDATE | SPEC_EXEMPT | {"spark", ""}):
        for status in (None, "pending", "completed", "in-progress"):
            for source, agent in (("world", AGENT), ("world", ""),
                                  ("agent", AGENT)):
                goal = {"title": "t", "origin_signal": f"{head}:x"}
                if status is not None:
                    goal["status"] = status
                before = copy.deepcopy(goal)
                ir.apply(goal, config=ir.OFF, source=source, agent_name=agent)
                assert goal == before, (head, status, source, agent)


def test_apply_files_the_owner_pending_through_the_shared_predicate():
    goal = {"title": "t", "origin_signal": "investigate:x"}
    assert ir.apply(goal, config=ON, source="world", agent_name="") == "pending"
    assert "status" not in goal


# ---------------------------------------------------------------------------
# 3. Config
# ---------------------------------------------------------------------------

def _root_with_config(tmp_path: Path, text: str | None) -> Path:
    cfg = tmp_path / "core" / "config"
    cfg.mkdir(parents=True)
    if text is not None:
        (cfg / "aspirations.yaml").write_text(text, encoding="utf-8")
    return tmp_path


LISTS = ("  candidate_origins: [investigate, idea, maintain]\n"
         "  exempt_origins: [unblock, alert-email]\n")


@pytest.mark.parametrize("text", [
    None,                                               # no file
    "other_block: {a: 1}\n",                            # no block
    "candidate_tier:\n" + LISTS,                        # no enabled key
    "candidate_tier:\n  enabled: false\n" + LISTS,
    "candidate_tier:\n  enabled: 'true'\n" + LISTS,     # a string is not true
    "candidate_tier:\n  enabled: true\n  candidate_origins: investigate\n"
    "  exempt_origins: [unblock]\n",                    # a list that is not a list
    "candidate_tier: [\n",                              # unparseable
    "- candidate_tier\n- enabled\n",                    # a list document ()
    "just a scalar\n",                                  # a scalar document ()
])
def test_config_reads_off_unless_literally_enabled(tmp_path, text):
    assert ir.load_config(_root_with_config(tmp_path, text)) == ir.OFF


def test_config_with_non_utf8_bytes_reads_off_and_says_so(tmp_path, capsys):
    """A 0xff byte raised UnicodeDecodeError out of the loader, and the loader
    runs on every add-goal (g-353-164)."""
    root = _root_with_config(tmp_path, None)
    (root / "core" / "config" / "aspirations.yaml").write_bytes(
        b"candidate_tier:\n  enabled: true\n\xff\xfe\n")
    assert ir.load_config(root) == ir.OFF
    assert "[intake-route] WARN" in capsys.readouterr().err


@pytest.mark.parametrize("text", [
    "candidate_tier: [\n",                              # unparseable
    "- candidate_tier\n- enabled\n",                    # a list document
    "just a scalar\n",                                  # a scalar document
    "candidate_tier:\n  enabled: true\n  candidate_origins: investigate\n"
    "  exempt_origins: [unblock]\n",                    # enabled, origin list not a list
])
def test_a_config_that_cannot_be_used_reads_off_and_says_so(tmp_path, text, capsys):
    """OFF is the right answer (I1), but it must not be BYTE-IDENTICAL to a flag
    that is simply off (guard-424, rb-6115): one line on stderr."""
    assert ir.load_config(_root_with_config(tmp_path, text)) == ir.OFF
    err = capsys.readouterr().err
    assert "[intake-route] WARN" in err and "reads OFF" in err


@pytest.mark.parametrize("text", [
    None,                                               # no file
    "",                                                 # an empty file
    "other_block: {a: 1}\n",                            # no block
    "candidate_tier:\n" + LISTS,                        # no enabled key
    "candidate_tier:\n  enabled: false\n" + LISTS,
])
def test_a_config_that_is_simply_off_stays_silent(tmp_path, text, capsys):
    """The positive control for the warning above: off by configuration is not noise."""
    assert ir.load_config(_root_with_config(tmp_path, text)) == ir.OFF
    assert capsys.readouterr().err == ""


def test_config_on(tmp_path):
    cfg = ir.load_config(_root_with_config(
        tmp_path, "candidate_tier:\n  enabled: true\n" + LISTS))
    assert cfg["enabled"] is True
    assert cfg["candidate_origins"] == {"investigate", "idea", "maintain"}
    assert cfg["exempt_origins"] == {"unblock", "alert-email"}


def test_repo_config_flag_is_a_literal_bool_with_the_spec_lists():
    """The shipped flag VALUE is not pinned here. B6 () flips it in this
    very file, and a test that hard-codes False goes red on the flip and invites
    the "fix" that turns the flag back off (g-353-164). The loader must agree with
    the literal whichever way it ships."""
    import yaml
    block = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["candidate_tier"]
    assert isinstance(block["enabled"], bool)
    assert set(block["candidate_origins"]) == SPEC_CANDIDATE
    assert set(block["exempt_origins"]) == SPEC_EXEMPT
    assert ir.load_config(PROJECT_ROOT)["enabled"] is block["enabled"]


# ---------------------------------------------------------------------------
# 4. One user-context predicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,agent,expected", [
    ("world", "", True), (" World ", None, True), ("world", AGENT, False),
    ("agent", "", False), (None, "", False)])
def test_world_user_context(source, agent, expected):
    assert world_user_context(source, agent) is expected


# ---------------------------------------------------------------------------
# 5. Placement: after the origin-signal gate, at both daemon write sites
# ---------------------------------------------------------------------------

def _span(text: str, start_marker: str) -> str:
    start = text.index(start_marker)
    end = text.find("\ndef ", start + 1)
    return text[start:end if end != -1 else len(text)]


@pytest.mark.parametrize("fn", ["def _run_add_goal_pipeline(", "def add(ctx)"])
def test_both_write_sites_route_after_the_origin_signal_gate(fn):
    body = _span(DAEMON_PATH.read_text(encoding="utf-8"), fn)
    assert "_origin_signal_eval(" in body
    assert "_intake_route_apply(" in body
    assert body.index("_origin_signal_eval(") < body.index("_intake_route_apply(")


# ---------------------------------------------------------------------------
# 6. The production door: a fixture daemon, flag on and off
# ---------------------------------------------------------------------------

def _make_world(tmp: Path) -> Path:
    world = tmp / "world"
    world.mkdir(parents=True, exist_ok=True)
    asp = {"id": "asp-994", "title": "intake routing fixtures",
           "motivation": "g-353-64 B2 regression", "scope": "project",
           "priority": "MEDIUM", "status": "active",
           "created": "2026-10-01T00:00:00", "goals": []}
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _add_goal(port: int, title: str, origin_signal: str) -> int:
    body = {"title": title,
            "description": "seeded by test_intake_route_gate (g-353-64)",
            "priority": "MEDIUM", "participants": ["agent"],
            "origin_signal": origin_signal,
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/aspirations/add-goal?asp_id=asp-994&source=world",
        data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _status_of(world: Path, title: str):
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            for g in json.loads(line).get("goals", []):
                if g.get("title") == title:
                    return g.get("status")
    return None


def _add_aspiration(port: int, goals: list) -> int:
    body = {"title": "intake routing batch fixture",
            "motivation": "g-353-64 B2 regression", "scope": "project",
            "priority": "MEDIUM", "status": "active", "goals": goals}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/aspirations/add?source=world",
        data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", AGENT)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _embedded(title: str, origin_signal: str) -> dict:
    return {"title": title, "description": "embedded by test_intake_route_gate",
            "status": "pending", "priority": "MEDIUM", "blocked_by": [],
            "participants": ["agent"], "origin_signal": origin_signal,
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []}}


def _write_config(df, flag_on: bool) -> None:
    """The repo's own config in the fixture, with only the flag varied. It is
    read per request, so writing it after the daemon starts is enough."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    want = "true" if flag_on else "false"
    # Set the flag explicitly in BOTH directions: the repo ships it either way, and
    # a one-way false->true replace goes red once B6 flips it ().
    text, n = re.subn(r"(candidate_tier:\n  enabled: )(?:true|false)", r"\g<1>" + want,
                      text, count=1)
    assert n == 1
    cfg = df.project_root / "core" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "aspirations.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def local_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955: never the live store


@pytest.mark.parametrize("flag_on", [True, False])
def test_fixture_daemon_routes_by_origin(local_backend, flag_on):
    with tempfile.TemporaryDirectory() as tmp:
        world = _make_world(Path(tmp))
        with DaemonFixture(world, agent=AGENT) as df:
            _write_config(df, flag_on)
            assert _add_goal(df.port, "intake fixture investigate",
                             "investigate:fixture") == 200
            assert _add_goal(df.port, "intake fixture alert",
                             "alert-email:fixture") == 200
        assert _status_of(world, "intake fixture investigate") == (
            "candidate" if flag_on else "pending")
        assert _status_of(world, "intake fixture alert") == "pending"


@pytest.mark.parametrize("flag_on", [True, False])
def test_fixture_daemon_routes_a_whole_aspiration_by_origin(local_backend, flag_on):
    with tempfile.TemporaryDirectory() as tmp:
        world = _make_world(Path(tmp))
        with DaemonFixture(world, agent=AGENT) as df:
            _write_config(df, flag_on)
            assert _add_aspiration(df.port, [
                _embedded("intake batch investigate", "investigate:fixture"),
                _embedded("intake batch alert", "alert-email:fixture")]) == 200
        assert _status_of(world, "intake batch investigate") == (
            "candidate" if flag_on else "pending")
        assert _status_of(world, "intake batch alert") == "pending"
