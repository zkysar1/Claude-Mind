"""test_routing_offroster_target.py -- g-115-3482.

An `intended_agent` value OUTSIDE the live vocabulary made a goal DOUBLY dead:

  * UNSELECTABLE -- goal-selector's collect_candidates dropped it, while
    collect_blocked never references intended_agent, so it was absent from
    BOTH outputs. Invisible in both directions, permanently.
  * UNCLAIMABLE -- the CLI takeover guard (aspirations.py) and the daemon
    claim path both refused it as a cross-lane claim.

Two live instances, measured 2026-07-28: g-115-913 and g-115-918, both filed
by the cycle-detector on 2026-05-18 with `intended_agent: "any"` -- a sentinel
that obviously MEANS "anyone may take this" and that the vocabulary
(`active_agents | {"either"}`) does not contain. They sat invisible for 71
days. The retired-agent case ("delta", removed from team-state agent_status)
is the same defect from a different cause.

The fix teaches the shared predicate `routes_away_from()` that an off-roster
value names nobody who can honor the routing, so the goal falls through and
becomes visible -- exactly as "either" behaves, and exactly what "any" meant.

DISCRIMINATION (guard-1220): `test_unresolvable_roster_keeps_status_quo` is
the case that makes this suite non-vacuous. A naive "always fall through" fix
passes every other test here and FAILS that one -- an unreadable team-state
must never make every routed goal visible fleet-wide (rb-1028
never-on-absent-evidence).
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("AYOAI_AGENT")
os.environ.setdefault("AYOAI_AGENT", "alpha")

gs = importlib.import_module("goal-selector")
import aspirations  # noqa: E402

if _SAVED_AGENT is None:
    os.environ.pop("AYOAI_AGENT", None)
else:
    os.environ["AYOAI_AGENT"] = _SAVED_AGENT


ROSTER = {"alpha", "bravo", "echo", "foxtrot", "zeta", "either"}


def _pin_roster(monkeypatch, vocabulary=ROSTER):
    """Pin the intended_agent vocabulary. routes_away_from() resolves it by
    global name lookup inside the aspirations module, so patching there also
    governs goal-selector's imported reference."""
    monkeypatch.setattr(aspirations, "_valid_intended_agents",
                        lambda: set(vocabulary))


# ---------------------------------------------------------------- unit tests

def test_unset_is_not_routed_away(monkeypatch):
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from(None, "alpha") is False
    assert aspirations.routes_away_from("", "alpha") is False


def test_either_sentinel_is_not_routed_away(monkeypatch):
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("either", "alpha") is False


def test_own_name_is_not_routed_away(monkeypatch):
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("alpha", "alpha") is False


def test_real_peer_is_routed_away(monkeypatch):
    """No-regression: a genuine peer on the roster still routes away."""
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("zeta", "alpha") is True


def test_retired_agent_is_not_routed_away(monkeypatch):
    """THE FIX, cause 1: 'delta' was retired out of agent_status."""
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("delta", "alpha") is False


def test_unrecognized_sentinel_is_not_routed_away(monkeypatch):
    """THE FIX, cause 2: the cycle-detector's literal "any" (g-115-913/918)."""
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("any", "alpha") is False


def test_whitespace_is_stripped(monkeypatch):
    _pin_roster(monkeypatch)
    assert aspirations.routes_away_from("  zeta  ", "alpha") is True
    assert aspirations.routes_away_from("  either  ", "alpha") is False


def test_unresolvable_roster_keeps_status_quo(monkeypatch):
    """DISCRIMINATION (guard-1220 / rb-1028).

    An unreadable or empty team-state leaves the vocabulary at {"either"}
    alone. The roster check must then be SKIPPED so the historical
    name-mismatch behavior stands -- otherwise every routed goal in the fleet
    becomes visible to every agent on a transient team-state read failure,
    which is a fail-OPEN at fleet scale.

    A naive "off-roster always falls through" fix passes every other test in
    this file and fails exactly here.
    """
    _pin_roster(monkeypatch, vocabulary={"either"})
    assert aspirations.routes_away_from("zeta", "alpha") is True


# ----------------------------------------------------------- e2e via selector

def _goal(gid, intended=None, claimed_by=None):
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if intended is not None:
        g["intended_agent"] = intended
    if claimed_by is not None:
        g["claimed_by"] = claimed_by
    return g


def _iso(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _collect(monkeypatch, goals, vocabulary=ROSTER):
    """Collect world candidates with orthogonal filters neutralized.

    Every roster agent is pinned FRESH (0.1h) so the idle-reallocation path
    (g-115-1766) cannot be what surfaces a goal -- any goal that appears here
    appears because of the off-roster fall-through under test, not because its
    target looked dormant.
    """
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    doc = {"agent_status": {n: {"last_active": _iso(0.1)}
                            for n in vocabulary if n != "either"}}
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: doc)
    _pin_roster(monkeypatch, vocabulary)
    asps = [{"id": "asp-test", "status": "active", "goals": goals}]
    return {c["goal"]["id"] for c in gs.collect_candidates(
        asps, source="world", reallocation_hours=8)}


def test_e2e_offroster_sentinel_is_collected(monkeypatch):
    """The g-115-913/918 shape: intended_agent='any' must now be visible."""
    ids = _collect(monkeypatch, [_goal("g-any", intended="any")])
    assert "g-any" in ids


def test_e2e_retired_agent_is_collected(monkeypatch):
    """The zeta-reported shape: routed to an agent no longer in the roster."""
    ids = _collect(monkeypatch, [_goal("g-delta", intended="delta")])
    assert "g-delta" in ids


def test_e2e_live_peer_still_filtered(monkeypatch):
    """No-regression: a goal routed to a LIVE, non-idle peer stays hidden."""
    ids = _collect(monkeypatch, [_goal("g-peer", intended="zeta")])
    assert "g-peer" not in ids


def test_e2e_unresolvable_roster_does_not_fail_open(monkeypatch):
    """Fleet-scale fail-open guard, end to end through the selector."""
    ids = _collect(monkeypatch, [_goal("g-peer", intended="zeta")],
                   vocabulary={"either"})
    assert "g-peer" not in ids


# --------------------------------------------------- CLI <-> daemon parity

def _logic_body(path, funcname):
    """Structural (comment/format-insensitive) body of a named function."""
    import ast
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == funcname:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)):
                body = body[1:]  # drop docstring
            return [ast.dump(s) for s in body]
    raise AssertionError("%s not found in %s" % (funcname, path))


def test_cli_and_daemon_predicates_stay_in_sync():
    """The claim path is enforced TWICE -- once in core/scripts/aspirations.py
    (CLI takeover guard) and once in the daemon's aspirations_write.py. Under
    daemon-only architecture the DAEMON copy is the one a live
    aspirations-claim.sh actually hits, so a fix applied to only one of them
    leaves the real path broken while the unit tests above stay green.

    The two are duplicated deliberately (each module resolves the roster
    through its own layer -- core via _agents, daemon via
    gates.capability_route), so this asserts logical parity rather than
    forcing a cross-layer import. The daemon module cannot be imported
    directly here (relative imports beyond top-level package), which is why
    this compares source structure instead of behavior.
    """
    repo = CORE_SCRIPTS.parent.parent
    cli = _logic_body(repo / "core/scripts/aspirations.py",
                      "routes_away_from")
    daemon = _logic_body(
        repo / "mind_api/src/endpoints/aspirations_write.py",
        "_routes_away_from")
    assert cli == daemon, (
        "routes_away_from (CLI) and _routes_away_from (daemon) diverged -- "
        "the daemon copy is the one a live claim hits; fix both or the real "
        "claim path stays broken while unit tests pass")


def test_non_string_value_does_not_crash(monkeypatch):
    """Type-tolerance no-regression.

    The predicate this replaced compared with `!=`, so ANY type was safe. This
    runs inside goal-selector's per-goal loop, where an exception crashes
    selection and therefore the autonomous loop. A malformed non-string value
    must fall through to visible (the safe direction), never raise.
    """
    _pin_roster(monkeypatch)
    for weird in ([], ["alpha"], {"a": 1}, 42, 0, object()):
        assert aspirations.routes_away_from(weird, "alpha") is False


def test_roster_resolution_failure_does_not_escape(monkeypatch):
    """A roster-resolution failure must never propagate (fresh-eyes, g-115-3482).

    This predicate runs inside goal-selector's per-goal loop, so an exception
    crashes selection and kills the autonomous loop. The predicate it replaced
    was a pure string comparison and could not raise, so this pins that the
    property was not regressed.

    The path is narrow but real: _agents._resolve_world_team_state calls
    `_agents_root(root).iterdir()` OUTSIDE _from_team_state's try block, so an
    unreadable agents-root raises OSError straight through get_active_agents.
    goal-selector's own _load_team_state_cached docstring states the invariant
    (rb-2429): team-state is advisory, and an unreadable read must not crash
    the selector.

    On failure, fall to the conservative branch — same as an unresolvable
    roster — so a foreign name still routes away rather than becoming visible
    fleet-wide.
    """
    def boom():
        raise OSError("simulated unreadable agents-root")
    monkeypatch.setattr(aspirations, "_valid_intended_agents", boom)
    assert aspirations.routes_away_from("zeta", "alpha") is True
    # the cheap early-outs must still short-circuit before ever resolving
    assert aspirations.routes_away_from(None, "alpha") is False
    assert aspirations.routes_away_from("either", "alpha") is False
    assert aspirations.routes_away_from("alpha", "alpha") is False
