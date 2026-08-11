"""test_goal_selector_idle_reallocation.py --  gap #4.

Proves the intended_agent idle-reallocation: a goal routed (intended_agent) to
an agent that has gone idle beyond reallocation_hours AND is unclaimed falls
through the intended_agent filter so a running capable agent can pick up
otherwise-stranded work. Mirrors the reallocatable+reallocation_hours mechanism
but keyed on intended-agent idleness rather than the explicit reallocatable flag.

Root incident (2026-07-08, alpha MIND-only box): 15 framework goals routed to a
5.75-day-idle agent (zeta) vanished from BOTH the selectable AND the blocked
selector outputs (the intended_agent filter is a select-time drop, not a block),
so the running agent falsely concluded all-blocked while executable framework
work sat invisible. The idle-reallocation surfaces them to any running agent.

Fixture mirrors test_goal_selector_capability_filter.py: pin MIND_AGENT=alpha
around import; monkeypatch _load_team_state_cached to control agent liveness and
_get_runner_capabilities to neutralize the (orthogonal) capability filter.
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

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _goal(gid, intended=None, claimed_by=None):
    """Minimal pending, agent-eligible goal; optionally intended_agent-routed."""
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if intended is not None:
        g["intended_agent"] = intended
    if claimed_by is not None:
        g["claimed_by"] = claimed_by
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _iso(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _pin_team_state(monkeypatch, statuses):
    """statuses: {agent_name: hours_since_last_active}."""
    doc = {"agent_status": {n: {"last_active": _iso(h)} for n, h in statuses.items()}}
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: doc)


def _pin_runner_identity(monkeypatch):
    """Pin the runner identity PER-TEST (). The import-time env
    setdefault above does NOT pin on agent boxes: bash-agent-inject pre-sets
    MIND_AGENT into every Bash call, so on bravo's box the module imported
    with AGENT_NAME="bravo" — flipping self-routed vs other-routed semantics
    (g-self intended="alpha" became other-routed and hidden; g-active-routed
    intended="bravo" became self-routed and surfaced) — the cc-05 2F. The
    intended_agent filter reads the module global, so patch it."""
    monkeypatch.setattr(gs, "AGENT_NAME", "alpha")


def _collect(monkeypatch, goals, reallocation_hours=8):
    """Collect world candidates with the capability filter neutralized.

    Pins _liveness_confirms_dormant -> True (g-115-2315): these tests exercise
    the REALLOCATION mechanics for a genuinely-dormant target, so the liveness
    cross-check is simulated as confirming dormancy. The cross-check itself is
    tested separately below with the real function + a pinned fresh-signal."""
    _pin_runner_identity(monkeypatch)
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    monkeypatch.setattr(gs, "_liveness_confirms_dormant", lambda *a: True)
    return {c["goal"]["id"] for c in gs.collect_candidates(
        _asps(goals), source="world", reallocation_hours=reallocation_hours)}


_UNSET = object()


def _collect_real_liveness(monkeypatch, goals, fresh_iso_or_exc,
                           reallocation_hours=8, auth_iso=_UNSET,
                           auth_prov="authoritative"):
    """Collect with the REAL _liveness_confirms_dormant, BOTH authoritative
    fetches pinned, and the memo cache cleared.

    Two probes must be pinned, not one (g-306-132-e). ``fetch_fresh_signal`` is
    the shard OBJECT's write time (body activity); ``fetch_authoritative_last_active``
    is the ``last_active`` VALUE inside the shard (mind liveness). Pinning only
    the first leaves the second reaching the LIVE store, which makes the test
    non-hermetic and — because the running agent's own last_active is fresh —
    silently flips a dormant scenario to alive. Caught exactly that way when the
    second probe was added.

    BOTH SPELLINGS of the second probe are pinned (g-306-138): production now
    calls ``fetch_authoritative_last_active_with_provenance`` (which returns a
    ``(iso, provenance)`` pair), while the bare-value ``fetch_authoritative_last_active``
    remains for provenance-blind callers. Pinning only the name production
    happens to call today re-opens the exact non-hermeticity above the moment
    that choice changes, so this helper pins both and the test stays hermetic
    under either wiring.

    ``auth_iso`` defaults to mirroring ``fresh_iso_or_exc`` so every pre-existing
    call site keeps its original meaning (both signals agree). Pass it explicitly
    to exercise the split case: object fresh, mind stale. ``auth_prov`` defaults
    to "authoritative" so pre-existing call sites keep asserting the trusted-value
    behavior; pass "local-mirror" to exercise the fail-open-ladder degradation.
    """
    import liveness_check as lc
    _pin_runner_identity(monkeypatch)
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    monkeypatch.setattr(gs, "_LIVENESS_DORMANT_CACHE", {})
    if auth_iso is _UNSET:
        auth_iso = fresh_iso_or_exc
    if isinstance(fresh_iso_or_exc, Exception):
        def _boom(*a, **k):
            raise fresh_iso_or_exc
        monkeypatch.setattr(lc, "fetch_fresh_signal", _boom)
    else:
        monkeypatch.setattr(lc, "fetch_fresh_signal",
                            lambda *a, **k: fresh_iso_or_exc)
    if isinstance(auth_iso, Exception):
        def _boom_auth(*a, **k):
            raise auth_iso
        monkeypatch.setattr(lc, "fetch_authoritative_last_active", _boom_auth)
        monkeypatch.setattr(lc, "fetch_authoritative_last_active_with_provenance",
                            _boom_auth)
    else:
        monkeypatch.setattr(lc, "fetch_authoritative_last_active",
                            lambda *a, **k: auth_iso)
        monkeypatch.setattr(lc, "fetch_authoritative_last_active_with_provenance",
                            lambda *a, **k: (auth_iso, auth_prov))
    return {c["goal"]["id"] for c in gs.collect_candidates(
        _asps(goals), source="world", reallocation_hours=reallocation_hours)}


def test_idle_target_unclaimed_reallocates(monkeypatch):
    """Goal routed to an idle (200h) agent, unclaimed -> COLLECTED (the fix)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-stranded", intended="zeta")])
    assert "g-stranded" in ids, "idle-routed unclaimed goal must reallocate/surface"


def test_active_target_stays_routed(monkeypatch):
    """Goal routed to a FRESH (1h) agent -> NOT collected (routing preserved)."""
    _pin_team_state(monkeypatch, {"zeta": 1})
    ids = _collect(monkeypatch, [_goal("g-routed", intended="zeta")])
    assert "g-routed" not in ids, "goal routed to an active agent must stay hidden"


def test_reallocation_disabled_preserves_status_quo(monkeypatch):
    """reallocation_hours=None (disabled) -> idle-routed goal stays hidden."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-stranded", intended="zeta")],
                   reallocation_hours=None)
    assert "g-stranded" not in ids, "reallocation disabled must preserve status-quo hiding"


def test_missing_last_active_not_idle(monkeypatch):
    """Target with no team-state record -> NOT idle -> goal stays routed
    (conservative: never surface on absent liveness evidence)."""
    monkeypatch.setattr(gs, "_load_team_state_cached", lambda: {"agent_status": {}})
    ids = _collect(monkeypatch, [_goal("g-routed", intended="zeta")])
    assert "g-routed" not in ids, "missing liveness evidence must NOT trigger reallocation"


def test_either_and_self_routed_unaffected(monkeypatch):
    """'either' and self-routed goals surface regardless of team-state (the
    intended_agent filter never dropped these; the fix must not change that).

    Identity note: the same box-dependence was found independently twice —
    echo/cc-03 (g-115-2315, fixed via a live gs.AGENT_NAME fixture) and
    cc-05 (g-115-2313, fixed via a per-test AGENT_NAME pin in _collect).
    The pin supersedes the live-fixture form: _collect forces
    AGENT_NAME="alpha" for the duration of the call, so the fixture must
    name the PINNED identity (a live gs.AGENT_NAME read here captures the
    import-time value BEFORE the pin applies and goes stale)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [_goal("g-either", intended="either"),
                                 _goal("g-self", intended="alpha")])
    assert "g-either" in ids and "g-self" in ids, ids


def test_mixed_only_idle_routed_surfaces(monkeypatch):
    """Mixed queue: idle-routed surfaces; active-routed stays hidden;
    'either' always surfaces -- the exact shape of the root incident."""
    _pin_team_state(monkeypatch, {"zeta": 200, "bravo": 1})
    ids = _collect(monkeypatch, [
        _goal("g-idle-routed", intended="zeta"),     # idle target -> surface
        _goal("g-active-routed", intended="bravo"),  # active target -> hidden
        _goal("g-open", intended="either"),          # open -> surface
    ])
    assert ids == {"g-idle-routed", "g-open"}, ids


# ── : liveness cross-check on the idle verdict ──────────────────
# A stale LOCAL-mirror last_active alone must not surface another agent's
# routed goals; the authoritative-store fresh signal decides. Root incident
# (2026-07-16, echo/cc-03): foxtrot last_active 27.5h stale locally while its
# shard hit the authoritative store 4 minutes earlier — its alert goal leaked
# to echo. These tests run the REAL _liveness_confirms_dormant with the
# fresh-signal fetch pinned.

def test_stale_mirror_fresh_shard_not_idle(monkeypatch):
    """last_active stale (200h) but authoritative shard push FRESH (4m ago)
    -> verdict alive -> NOT idle -> goal stays routed (the root incident)."""
    _pin_team_state(monkeypatch, {"foxtrot": 200})
    fresh = (datetime.now() - timedelta(minutes=4)).isoformat(timespec="seconds")
    ids = _collect_real_liveness(
        monkeypatch, [_goal("g-leak", intended="foxtrot")], fresh)
    assert "g-leak" not in ids, \
        "fresh authoritative shard must veto the stale-mirror idle verdict"


def test_both_signals_stale_is_idle(monkeypatch):
    """last_active stale AND authoritative shard stale -> dormant is a
    supported conclusion -> goal reallocates (the g-115-1766 fix preserved)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    stale = (datetime.now() - timedelta(hours=200)).isoformat(timespec="seconds")
    ids = _collect_real_liveness(
        monkeypatch, [_goal("g-stranded", intended="zeta")], stale)
    assert "g-stranded" in ids, \
        "both-signals-stale (dormant) must still reallocate stranded goals"


def test_worker_body_shard_write_does_not_make_a_dead_reducer_look_alive(monkeypatch):
    """-e, consumer side. A worker Body writing the shard keeps the
    OBJECT fresh while the reducer's own last_active has aged out. Before the fix
    that read 'alive' and the mind's stranded goals stayed routed to a dead agent
    forever. Now the verdict is 'unknown' -> _liveness_confirms_dormant is False
    -> still NOT idle. Asserting the safe direction, not reallocation: 'unknown'
    must never authorise a take-back (guard-1042)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    body_write = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    mind_stale = (datetime.now() - timedelta(hours=200)).isoformat(timespec="seconds")
    ids = _collect_real_liveness(
        monkeypatch, [_goal("g-routed", intended="zeta")], body_write,
        auth_iso=mind_stale)
    assert "g-routed" not in ids, \
        "a fresh shard object with a stale mind heartbeat is UNKNOWN, not dormant"


def test_fresh_signal_unavailable_not_idle(monkeypatch):
    """Fresh signal unreadable (fetch returns None -> verdict unknown)
    -> NOT idle: never conclude dormant on absent evidence."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect_real_liveness(
        monkeypatch, [_goal("g-routed", intended="zeta")], None)
    assert "g-routed" not in ids, \
        "unknown liveness (no fresh signal) must NOT trigger reallocation"


def test_probe_error_not_idle(monkeypatch):
    """Probe raising entirely -> fail-safe NOT idle (goals stay routed)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect_real_liveness(
        monkeypatch, [_goal("g-routed", intended="zeta")],
        RuntimeError("boto3 exploded"))
    assert "g-routed" not in ids, \
        "a probe error must degrade toward goals-stay-routed, not false-idle"


def test_liveness_probe_memoized_per_agent(monkeypatch):
    """The fresh-signal fetch fires once per stale agent per process even
    across repeated collect calls (world + agent queues share the memo)."""
    import liveness_check as lc
    _pin_team_state(monkeypatch, {"zeta": 200})
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: set())
    monkeypatch.setattr(gs, "_LIVENESS_DORMANT_CACHE", {})
    calls = []
    stale = (datetime.now() - timedelta(hours=200)).isoformat(timespec="seconds")
    monkeypatch.setattr(lc, "fetch_fresh_signal",
                        lambda *a, **k: calls.append(1) or stale)
    for _ in range(3):
        gs.collect_candidates(_asps([_goal("g-x", intended="zeta")]),
                              source="world", reallocation_hours=8)
    assert len(calls) == 1, f"expected 1 memoized probe, saw {len(calls)}"


# ──  / rb-4792: owner-scoped goals never cross-agent reallocated ──
# /drain-temp (and the maintain:temp-drain Maintain goal) operate ONLY on the
# bound agent's own temp dir, so surfacing them to a cross-agent reallocatee via
# the idle-reallocation strands the reallocatee's top-of-queue on unexecutable
# work (bravo, 2026-07-23: /339/61 sat unexecutable for 2+ iterations).
# The _is_owner_scoped_goal exclusion keeps them hidden even when the owner is idle.

def _owner_scoped_goal(gid, intended, *, skill=None, origin=None, title=None):
    g = _goal(gid, intended=intended)
    if skill is not None:
        g["skill"] = skill
    if origin is not None:
        g["origin_signal"] = origin
    if title is not None:
        g["title"] = title
    return g


def test_is_owner_scoped_goal_detection():
    """The helper detects owner-scoped goals three independent ways (skill,
    origin_signal, title). The title signal matches ONLY the exact drain-action
    template (prefix "Maintain: drain " + infix "accumulated temp/ working docs",
    via the shared _drain_title.is_drain_action_title SSOT), NOT any goal that
    merely mentions temp-drain (g-115-2983 — the old both-tokens fallback
    false-positived analysis/Idea goals ABOUT temp-drain, stranding them with a
    dormant owner)."""
    assert gs._is_owner_scoped_goal({"skill": "/drain-temp"}) is True
    assert gs._is_owner_scoped_goal({"origin_signal": "maintain:temp-drain"}) is True
    # the REAL templated drain-action title (precheck-eval.py cmd_temp_pressure)
    assert gs._is_owner_scoped_goal(
        {"title": "Maintain: drain 25 accumulated temp/ working docs to the knowledge tree"}) is True
    # negatives: a normal skill, a title that mentions "drain" but is NOT the
    # drain-action template, and a non-drain origin_signal
    assert gs._is_owner_scoped_goal({"skill": "/reflect"}) is False
    assert gs._is_owner_scoped_goal({"title": "drain the pipeline backlog"}) is False
    assert gs._is_owner_scoped_goal({"origin_signal": "idea:temp-store-audit"}) is False
    assert gs._is_owner_scoped_goal({}) is False
    #  regression guard: a goal ABOUT temp-drain (an Idea/analysis goal,
    # or a "Maintain: add ..." goal) carries "drain"+"temp" but is NOT the
    # drain-action template — the old fallback wrongly marked these owner-scoped
    # and non-reallocatable. They MUST be reallocatable (owner-scoped == False).
    assert gs._is_owner_scoped_goal(
        {"title": "Idea: unify goal-selector owner-scoped-drain title fallback for temp docs"}) is False
    assert gs._is_owner_scoped_goal(
        {"title": "Maintain: add verify-learning check for temp-drain title matcher"}) is False


def test_owner_scoped_temp_drain_not_reallocated(monkeypatch):
    """An owner-scoped temp-drain routed to an idle (200h) agent, unclaimed ->
    NOT collected (the g-115-2945 fix). Without the exclusion the idle-
    reallocation would surface it to this running agent, where it is
    unexecutable (drains the WRONG agent's temp)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [
        _owner_scoped_goal("g-owner", intended="zeta",
                           origin="maintain:temp-drain",
                           title="Maintain: drain 25 accumulated temp/ working docs to the knowledge tree"),
    ])
    assert "g-owner" not in ids, \
        "owner-scoped temp-drain must NOT reallocate cross-agent even to a running agent"


def test_owner_scoped_exclusion_is_narrow(monkeypatch):
    """The exclusion applies ONLY to owner-scoped goals: a normal idle-routed
    goal in the SAME queue still reallocates (the g-115-1766 mechanism intact)."""
    _pin_team_state(monkeypatch, {"zeta": 200})
    ids = _collect(monkeypatch, [
        _owner_scoped_goal("g-owner", intended="zeta", skill="/drain-temp"),
        _goal("g-normal", intended="zeta"),
    ])
    assert ids == {"g-normal"}, ids
