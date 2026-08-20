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


def _bind_inference(monkeypatch, world, row, role="reducer"):
    """Point the inference at a tmp world with `row` as the agent's team-state.

    `role` PINS the Body role (g-115-6748), and pinning it is not optional
    hygiene — without it these tests read the role of whatever Body is running
    pytest. `_body_role()` derives from `AGENT_DIR/sessions/$MIND_SID/
    working-memory.yaml`, and a worker Body's own shell exports MIND_SID while
    AGENT_DIR resolves to the real agent dir, so that file EXISTS and the
    inference correctly returns None. Measured: with the role unpinned,
    `test_inference_still_returns_a_live_in_flight_goal` passes on a reducer box
    and FAILS on a worker box, on the same tree — an environment-dependent
    assertion masquerading as a behavioural one.

    Default "reducer" is the pre-g-115-6748 semantics every existing caller was
    written against, so their meaning is preserved exactly.
    """
    import _team_state

    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setattr(retrieve, "WORLD_DIR", world)
    monkeypatch.setattr(retrieve, "get_backend", lambda: _IdentityBackend())
    monkeypatch.setattr(_team_state, "read_agent_row",
                        lambda *a, **k: row, raising=False)

    agent_dir = world.parent / "agent"
    monkeypatch.setattr(retrieve, "AGENT_DIR", agent_dir)
    if role == "unknown":
        # `unknown` is reached by an ABSENT sid, which is the case that fires on
        # every non-Body caller — hence it must fall through to the reducer
        # behaviour, not the worker one.
        monkeypatch.delenv("MIND_SID", raising=False)
        return
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    if role == "worker":
        wm = agent_dir / "sessions" / "sid-under-test" / "working-memory.yaml"
        wm.parent.mkdir(parents=True, exist_ok=True)
        wm.write_text("slots: {}\n", encoding="utf-8")


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


# ------------------------------------------------ Body role ()


@pytest.mark.parametrize("role,expected", [
    ("worker", None),
    ("reducer", "g-001-350"),
    ("unknown", "g-001-350"),
])
def test_worker_body_does_not_inherit_the_reducers_in_flight_goal(
        tmp_path, monkeypatch, role, expected):
    """: the in_flight row is AGENT-keyed with no sid, so on a WORKER
    Body it names the REDUCER's goal — measured 2026-08-19 (alpha worker, cc-07,
    SID d1aec55b: a worker executing g-115-6653 stamped its manifest g-363-20,
    the reducer's goal on cc-04).

    All three roles in ONE parametrize on purpose. The worker row is the fix;
    the reducer row is the positive control that the fix did not become
    "return None always" (which would silently regress g-115-137 and satisfy a
    worker-only test perfectly); and the unknown row pins the FAIL-OPEN
    direction, which is the half most likely to be broken by a later
    simplification. `unknown` fires whenever MIND_SID is unset — i.e. for every
    non-Body caller — so folding it in with `worker` would disable goal-stamping
    fleet-wide while still passing both other rows.
    """
    world = _write_world(tmp_path, [
        _asp("asp-001", [{"id": "g-001-350", "status": "in-progress"}]),
    ])
    _bind_inference(monkeypatch, world,
                    {"in_flight": {"goal_id": "g-001-350",
                                   "claimed_at": "2026-08-19T00:00:00"}},
                    role=role)
    assert retrieve._infer_in_flight_goal_id() == expected


@pytest.mark.parametrize("role,expected", [
    ("worker", "worker"), ("reducer", "reducer"), ("unknown", "unknown"),
])
def test_body_role_is_three_way(tmp_path, monkeypatch, role, expected):
    """The predicate keeps THREE values, not a bool (guard-2913: an unevaluated
    check is not a passed one). A future refactor to `_is_worker() -> bool`
    would erase the distinction between "this is the reducer" and "I could not
    tell", and only the caller's comment would remember which way unknown fell.
    """
    world = _write_world(tmp_path, [_asp("asp-001", [])])
    _bind_inference(monkeypatch, world, {}, role=role)
    assert retrieve._body_role() == expected


def test_body_role_reads_the_swappable_module_global_not_a_resolver(
        tmp_path, monkeypatch):
    """The daemon swaps `_r.AGENT_DIR` per request under its lock, so the role
    MUST come from that global. A version resolving the agent dir itself (e.g.
    via `agent_dir(agent)` or `_paths`) would read the DAEMON's process state
    and mis-classify every request — which is the same defect class as the
    in_flight row this whole fix is about, one level down.

    Asserted by pointing AGENT_DIR at a directory that only the module global
    knows about: if the function consulted any resolver instead, it could not
    find this file and would answer `reducer`.
    """
    world = _write_world(tmp_path, [_asp("asp-001", [])])
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    elsewhere = tmp_path / "somewhere-no-resolver-would-look"
    wm = elsewhere / "sessions" / "sid-under-test" / "working-memory.yaml"
    wm.parent.mkdir(parents=True, exist_ok=True)
    wm.write_text("slots: {}\n", encoding="utf-8")
    monkeypatch.setattr(retrieve, "AGENT_DIR", elsewhere)
    assert retrieve._body_role() == "worker"

    monkeypatch.setattr(retrieve, "AGENT_DIR", None)
    assert retrieve._body_role() == "unknown"


def test_daemon_endpoint_swaps_the_sid_it_was_handed(tmp_path):
    """WIRING tripwire, because the CLI-side unit tests above cannot see it.

    `_body_role()` reads os.environ["MIND_SID"], and in production the ONLY
    caller is the daemon (retrieve.py's CLI main() was deleted at the 2026-05-14
    cutover). The daemon process env holds the DAEMON's startup sid, so without
    a per-request swap the predicate would answer for the wrong Body — exactly
    the defect Decision #58 fixed for MIND_AGENT. Every test above would still
    be green with that swap missing, which is why this asserts the wiring
    textually rather than trusting the unit coverage.
    """
    root = SCRIPTS.parent.parent
    src = (root / "mind_api" / "src" / "endpoints" / "retrieve.py").read_text(
        encoding="utf-8")
    assert "x-mind-sid" in src, (
        "the retrieve endpoint no longer reads the x-mind-sid header — "
        "_body_role() would then classify every request by the DAEMON's sid")
    # Assert the SWAP form specifically, not the bare env name. A mutation that
    # deleted only the swap was NOT caught by `'os.environ["MIND_SID"]' in src`,
    # because the RESTORE block below it contains that same substring — the
    # tripwire read as green against an endpoint that had stopped swapping
    # entirely. Found by running the mutation, not by reading the assertion.
    assert 'os.environ["MIND_SID"] = _req_sid' in src, (
        "the retrieve endpoint no longer swaps MIND_SID per request "
        "(g-115-6748); the Body-role check in _infer_in_flight_goal_id is inert "
        "on the daemon path, which is the only path production uses")
    assert 'os.environ.pop("MIND_SID", None)' in src, (
        "the retrieve endpoint no longer RESTORES MIND_SID; a leaked worker "
        "sid makes the next request — any agent — resolve as a worker and "
        "silently stop inferring its goal")


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
