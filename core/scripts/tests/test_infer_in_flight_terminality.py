""": _infer_in_flight_goal_id must not return a TERMINAL goal id.

THE DEFECT. retrieve.py's inference read in_flight.goal_id from the agent's
team-state row and returned it after a NON-EMPTY-STRING CHECK ALONE. in_flight
is stamped inside aspirations-claim.sh, which ONLY world-source goals invoke
(guard-2835), so a run of agent-queue goals leaves the row naming a finished
goal for an unbounded period. mind_api/src/endpoints/retrieve.py:322 uses the
inference as the fallback (`effective_goal = goal or _r._infer_in_flight_goal_id()`)
whenever the caller omits --goal, and writes that value into
retrieval-session.json as goal_id — so every broad category-only retrieve in
that window mis-stamps the manifest.

MEASURED CONSEQUENCES, two consumers, one announced and one silent:
  * ANNOUNCED (g-115-5887 origin, bravo/cc-05): in_flight still named a goal
    closed 80 minutes earlier; utilization-feedback refused with
    {status: goal_mismatch, session_goal: <stale-id>}. 46 module_health_updates
    were recovered only by hand re-stamping.
  * SILENT (alpha/cc-04, 2026-08-13): iteration-close.sh reads the same manifest,
    probes the foreign goal_id into current_file_goal, NEVER prints it,
    overwrites the manifest with a no-retrieval stub, and reports
    performed=false — textually identical to a genuine no-consult, and an
    accusation against the agent. It then climbs pre_apply_consult_miss_streak
    and fires a forced-consult sentinel. Live instance: g-115-5216 carried 2
    goal-tied SUBJECT+MECHANISM consult rows in world/retrieval-trace.jsonl and
    still closed performed=false at streak=2.

ONE DEFINITION, ONE FIX SITE. The daemon does `import retrieve as _r`
(mind_api/src/endpoints/retrieve.py:68) rather than mirroring the function, so
guard-2323's "port the mind_api twin in the same change" is satisfied by
construction — verified by grep: the only `def _infer_in_flight_goal_id` in the
tree is core/scripts/retrieve.py. guard-984 still binds for VERIFICATION: the
daemon holds the imported module in memory until a commit recycles it, so these
tests (fresh pytest import from disk) are evidence about DISK, never about the
running daemon.

FAIL-OPEN IS PART OF THE CONTRACT and is asserted here, not merely intended: a
read error, a missing store, or an unparsable line must never SUPPRESS
inference, because losing attribution is the lesser harm and this runs on the
retrieval hot path.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]  # core/scripts
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import retrieve  # noqa: E402


def _write_world(tmp_path, aspirations):
    """Write a minimal world/aspirations.jsonl and return the world dir."""
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as fh:
        for asp in aspirations:
            fh.write(json.dumps(asp) + "\n")
    return world


def _asp(asp_id, goals, census=None):
    rec = {"id": asp_id, "title": f"aspiration {asp_id}", "goals": goals}
    if census is not None:
        rec["archived_census"] = census
    return rec


# --------------------------------------------------------------- the predicate


@pytest.mark.parametrize("status", ["completed", "skipped", "expired",
                                    "decomposed", "superseded"])
def test_terminal_statuses_detected(tmp_path, monkeypatch, status):
    """Every member of the _goal_census SSOT reads as terminal.

    Parametrized over the set rather than spot-checking "completed": the
    original defect was that NO status was consulted, so a fix that only
    handled the obvious one would leave four silent holes.
    """
    world = _write_world(tmp_path, [
        _asp("asp-115", [{"id": "g-115-1", "status": status}]),
    ])
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-1") is True


@pytest.mark.parametrize("status", ["pending", "in-progress", "blocked"])
def test_live_statuses_not_terminal(tmp_path, monkeypatch, status):
    """POSITIVE CONTROL. A predicate that returned True unconditionally would
    pass every test above; these pin that it discriminates."""
    world = _write_world(tmp_path, [
        _asp("asp-115", [{"id": "g-115-1", "status": status}]),
    ])
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-1") is False


def test_id_mentioned_only_as_prose_reference_is_not_a_hit(tmp_path, monkeypatch):
    """The substring prefilter must not decide the answer.

    The scan skips lines not containing the id, but a line CAN contain it purely
    as a reference in another goal's description. The real goal lives in a later
    line, so returning from the first textual match would report the WRONG
    aspiration's answer — here, terminal for a goal that is pending.
    """
    world = _write_world(tmp_path, [
        _asp("asp-001", [{"id": "g-001-9", "status": "completed",
                          "description": "supersedes g-115-1 entirely"}]),
        _asp("asp-115", [{"id": "g-115-1", "status": "pending"}]),
    ])
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-1") is False


def test_evicted_goal_is_terminal_via_census(tmp_path, monkeypatch):
    """An EVICTED goal is terminal by definition and absent from `goals`.

    aspirations-evict-completed.py removes aged terminal goals from the live
    list, and an aged in_flight naming one is exactly the case this check
    exists for — so a `goals`-only scan would fail open on its worst instance.
    """
    world = _write_world(tmp_path, [
        _asp("asp-115", [{"id": "g-115-2", "status": "pending"}],
             census={"evicted_ids": {"completed": ["g-115-1"]}}),
    ])
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-1") is True


def test_unknown_goal_and_missing_store_fail_open(tmp_path, monkeypatch):
    """Absence is never terminality, and a missing store never suppresses."""
    world = _write_world(tmp_path, [
        _asp("asp-115", [{"id": "g-115-1", "status": "completed"}]),
    ])
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-999") is False

    monkeypatch.setattr(retrieve, "WORLD_DIR", tmp_path / "nonexistent")
    assert retrieve._goal_id_is_terminal("g-115-1") is False

    monkeypatch.setattr(retrieve, "WORLD_DIR", None)
    assert retrieve._goal_id_is_terminal("g-115-1") is False


def test_unparsable_line_fails_open(tmp_path, monkeypatch):
    """A corrupt store must not suppress inference on the retrieval hot path."""
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "aspirations.jsonl").write_text(
        "g-115-1 {this is not json\n", encoding="utf-8")
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    assert retrieve._goal_id_is_terminal("g-115-1") is False


# ------------------------------------------------- the inference (outcome 2)


class _IdentityBackend:
    def ensure_local(self, path):
        return path


def _bind_inference(monkeypatch, world, row):
    """Point the inference at a tmp world with `row` as the agent's team-state."""
    import _team_state

    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    monkeypatch.setattr(retrieve, "get_backend", lambda: _IdentityBackend())
    monkeypatch.setattr(_team_state, "read_agent_row",
                        lambda *a, **k: row, raising=False)


def test_inference_returns_none_for_terminal_in_flight(tmp_path, monkeypatch):
    """OUTCOME 2, stated verbatim in the goal: given an in_flight row naming a
    completed goal, the inference does not return that id.

    This is the assertion that FAILS against the pre-fix behaviour — the old
    body returned `gid` after a non-empty-string check alone.
    """
    world = _write_world(tmp_path, [
        _asp("asp-001", [{"id": "g-001-350", "status": "completed"}]),
    ])
    _bind_inference(monkeypatch, world,
                    {"in_flight": {"goal_id": "g-001-350",
                                   "claimed_at": "2026-08-11T00:00:00"}})
    assert retrieve._infer_in_flight_goal_id() is None


def test_inference_still_returns_a_live_in_flight_goal(tmp_path, monkeypatch):
    """POSITIVE CONTROL for the fix itself.

    Without this, a change that made the inference return None unconditionally
    would satisfy the test above — and would silently reintroduce the
    unstamped-manifest / times_helpful=0 regression g-115-137 closed.
    """
    world = _write_world(tmp_path, [
        _asp("asp-001", [{"id": "g-001-350", "status": "in-progress"}]),
    ])
    _bind_inference(monkeypatch, world,
                    {"in_flight": {"goal_id": "g-001-350",
                                   "claimed_at": "2026-08-13T00:00:00"}})
    assert retrieve._infer_in_flight_goal_id() == "g-001-350"


def test_inference_unaffected_when_no_in_flight(tmp_path, monkeypatch):
    """The pre-existing None paths are untouched by the new check."""
    world = _write_world(tmp_path, [_asp("asp-001", [])])
    _bind_inference(monkeypatch, world, {})
    assert retrieve._infer_in_flight_goal_id() is None

    _bind_inference(monkeypatch, world, {"in_flight": {"goal_id": ""}})
    assert retrieve._infer_in_flight_goal_id() is None

    _bind_inference(monkeypatch, world, {"in_flight": "not-a-dict"})
    assert retrieve._infer_in_flight_goal_id() is None


def test_single_definition_so_no_daemon_twin_to_port(tmp_path):
    """guard-2323 tripwire.

    The daemon does `import retrieve as _r`, so there is exactly ONE definition
    and a CLI-side fix is automatically live once the daemon recycles. If someone
    later MIRRORS this function into mind_api/src (the shape guard-742 describes),
    this fails — and the terminality check must then be ported in the same change
    or it is inert in production from the moment it lands.
    """
    root = SCRIPTS.parent.parent  # repo root
    hits = []
    for path in (root / "mind_api" / "src").rglob("*.py"):
        if "def _infer_in_flight_goal_id" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(root)))
    assert hits == [], (
        f"_infer_in_flight_goal_id is now DEFINED in mind_api/src ({hits}) — the "
        "daemon no longer merely imports core/scripts/retrieve.py. Port the "
        "g-115-5887 terminality check into that copy (guard-2323/guard-742) or "
        "the daemon serves the unfixed inference."
    )
