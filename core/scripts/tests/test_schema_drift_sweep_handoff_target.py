"""schema-drift-sweep.py handoff-target resolution ().

The sweep files cross-agent handoff goals. It used to build its peer roster by
hand — core team-state `agent_status` keys UNION `_team_state.row_agent_names()`
— and take `peers[0]`. Neither input applies the retirement tombstone, and
`row_agent_names` is unfiltered BY DESIGN, so the roster carried retired agents
AND leaked test fixtures that own live row files. Measured on cc-03 the day this
landed: 13 names for 5 real agents. `peers[0]` happened to be "alpha" only
because nothing sorted ahead of it.

These tests pin the two halves of the fix against the REAL production call path
(`create_fix_goals`), not against a re-typed copy of the derivation expression:

1. the roster comes from `_agents.get_active_agents()` — the tombstone-applying
   SSOT — and NOT from the raw row-name source, even when the raw source offers
   a name that would sort first;
2. `intended_agent` is set explicitly (guard-2980), so the capability-route gate
   cannot silently route a handoff goal back to the agent that filed it.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _agents  # noqa: E402
import _team_state  # noqa: E402

# schema-drift-sweep.py is hyphenated -> load by path
_spec = importlib.util.spec_from_file_location(
    "schema_drift_sweep", SCRIPT_DIR / "schema-drift-sweep.py")
sds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sds)


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def filed(monkeypatch):
    """Capture the goal JSON create_fix_goals pipes to aspirations-add-goal.sh."""
    goals = []

    def fake_run(cmd, input=None, **kw):
        goals.append(json.loads(input))
        return _Proc()

    monkeypatch.setattr(sds.subprocess, "run", fake_run)
    return goals


def _drift():
    """One store with drift, so create_fix_goals emits exactly one goal."""
    return [{
        "store": "aspirations",
        "jsonl_path": "world/aspirations.jsonl",
        "drift_hits": [{"file": "some/skill.md", "line": 12, "field": "stale"}],
        "allowlisted_hits": [],
        "probe": {"status": "drift", "missing": []},
    }]


def test_retired_first_sorting_agent_is_not_the_handoff_target(monkeypatch, filed):
    """The regression this goal was filed for.

    `aaa-retired` sorts ahead of every live agent and IS returned by the raw
    row-name source (a retirement is a tombstone, not a delete, so the row
    survives and keeps being written). It is absent from get_active_agents().
    If this ever reverts to the hand-rolled union, `aaa-retired` wins and this
    test goes red.
    """
    monkeypatch.setenv("MIND_AGENT", "echo")
    monkeypatch.setattr(_agents, "get_active_agents",
                        lambda: ("alpha", "bravo", "echo", "zeta"))
    monkeypatch.setattr(_team_state, "row_agent_names",
                        lambda *a, **k: ["aaa-retired", "alpha", "bravo", "echo", "zeta"])

    assert sds.create_fix_goals(_drift()) == 1
    goal = filed[0]
    assert goal["handoff_to"] == "alpha"
    assert "aaa-retired" not in json.dumps(goal)


def test_self_is_excluded_from_the_peer_list(monkeypatch, filed):
    """Alphabetically-first ACTIVE agent wins — unless it is us."""
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(_agents, "get_active_agents",
                        lambda: ("alpha", "bravo", "echo"))

    assert sds.create_fix_goals(_drift()) == 1
    assert filed[0]["handoff_to"] == "bravo"


def test_no_active_peer_files_unrouted_rather_than_naming_a_target(monkeypatch, filed):
    """The filter's inverse (tree node: drop-without-inverse-pattern).

    With no live peer the goal must go to the shared queue with NO handoff_to,
    rather than falling back to a hardcoded name that may itself be retired —
    which is the very defect this fix removes.
    """
    monkeypatch.setenv("MIND_AGENT", "echo")
    monkeypatch.setattr(_agents, "get_active_agents", lambda: ("echo",))

    assert sds.create_fix_goals(_drift()) == 1
    goal = filed[0]
    assert "handoff_to" not in goal
    assert "handoff_created_at" not in goal
    assert goal["intended_agent"] == "either"


def test_unreadable_roster_fails_open_to_alpha(monkeypatch, filed):
    """Distinct from the empty case: an exception means we do not KNOW the
    roster, not that we know it is empty. Prior behaviour is preserved."""
    monkeypatch.setenv("MIND_AGENT", "echo")

    def boom():
        raise RuntimeError("team-state unreadable")

    monkeypatch.setattr(_agents, "get_active_agents", boom)

    assert sds.create_fix_goals(_drift()) == 1
    assert filed[0]["handoff_to"] == "alpha"


def test_intended_agent_is_always_set_explicitly(monkeypatch, filed):
    """guard-2980 — omission is not 'unrouted'. The capability-route gate sets
    intended_agent when absent, by DOMAIN match, which routes a framework
    defect straight back to the agent that detected it. handoff_to alone does
    not prevent that: it is a selector bonus, not the routing gate."""
    monkeypatch.setenv("MIND_AGENT", "echo")
    monkeypatch.setattr(_agents, "get_active_agents",
                        lambda: ("alpha", "bravo", "echo"))

    assert sds.create_fix_goals(_drift()) == 1
    goal = filed[0]
    assert goal["intended_agent"] == "either"
    assert goal["handoff_from"] == "echo"
    assert goal["participants"] == ["agent"]


def test_roster_exception_on_the_fallback_agents_own_box_files_unrouted(
        monkeypatch, filed, capsys):
    """F-002: the bypass branch the suite above leaves open ().

    `test_unreadable_roster_fails_open_to_alpha` runs as "echo", so the
    fail-open literal and the runner differ and the self-exclusion is never
    exercised on that path. On the alpha box the same exception named alpha as
    its OWN recipient: a handoff goal that hands off to nobody, sitting in the
    filer's queue with a handoff_to that reads as routed.

    The exclusion used to live inside the peer comprehension, which the
    except branch never reaches. It is now a post-assignment guard, so every
    branch meets it.

    POSITIVE CONTROL IS IN THIS TEST BY DESIGN (guard-4166): the expected
    outcome is an ABSENCE, and an absence assertion passes just as happily
    against a create_fix_goals that filed nothing, a broken fixture, or a
    typo'd key. The control below fires the SAME exception on a DIFFERENT box
    and must still produce handoff_to == "alpha" — if it ever stops doing so,
    this test is passing for the wrong reason.
    """
    def boom():
        raise RuntimeError("team-state unreadable")

    monkeypatch.setattr(_agents, "get_active_agents", boom)

    # --- the defect case: runner IS the fail-open literal ---
    monkeypatch.setenv("MIND_AGENT", "alpha")
    assert sds.create_fix_goals(_drift()) == 1
    goal = filed[0]
    assert "handoff_to" not in goal, (
        "the fail-open literal named the running agent as its own recipient")
    assert "handoff_created_at" not in goal
    # unrouted, not unfiled — the goal still reaches the shared queue
    assert goal["intended_agent"] == "either"
    assert goal["handoff_from"] == "alpha"

    # --- F-001: the roster failure must leave a trace (guard-5501: prove the
    # diagnostic can actually fire, rather than trusting that a print exists) ---
    err = capsys.readouterr().err
    assert "WARN" in err and "roster unreadable" in err, err
    assert "team-state unreadable" in err, (
        "the bound exception itself must reach stderr, not just a generic line")

    # --- POSITIVE CONTROL: same exception, different box, still routes ---
    filed.clear()
    monkeypatch.setenv("MIND_AGENT", "echo")
    assert sds.create_fix_goals(_drift()) == 1
    assert filed[0]["handoff_to"] == "alpha", (
        "control flipped: the absence above is not evidence of the guard")
    assert filed[0]["handoff_from"] == "echo"


def test_handoff_from_is_derived_not_a_second_env_read(monkeypatch, filed):
    """One derived value instead of a second os.environ read ().

    handoff_from read MIND_AGENT again rather than reusing self_agent, so the
    recipient guard and the stamped sender could disagree about who is running
    if the two reads ever diverged. Pinned as one value with one source.
    """
    monkeypatch.setenv("MIND_AGENT", "bravo")
    monkeypatch.setattr(_agents, "get_active_agents",
                        lambda: ("alpha", "bravo", "echo"))

    assert sds.create_fix_goals(_drift()) == 1
    goal = filed[0]
    assert goal["handoff_from"] == "bravo"
    # the sender is never also the recipient
    assert goal.get("handoff_to") != goal["handoff_from"]
    assert goal["handoff_to"] == "alpha"
