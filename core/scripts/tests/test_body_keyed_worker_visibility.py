"""test_body_keyed_worker_visibility.py — regression tests for .

A WORKER Body never writes team-state `in_flight`: `team-state-in-flight.sh`
stamps only for the reducer (the g-306-132-d mutual-clobber guard). That closed
the clobber but left worker goals with NO team-state row at all, silently
degrading the two readers that GATE on in_flight:

  * `goal-pickup-coordination-check._partner_in_flight` — the guard-741
    uncommitted-collision probe. With no partner in_flight the gate OPENS and
    uncommitted files in the shared working tree read as unowned.
  * `_cross_agent_attribution_filter` Source 1 — concurrent-work windows.

The fix adds a body-keyed sibling row `agent_status.<agent>.in_flight_bodies.<sid>`
written by the claim path, cleared by the worker close, consumed by both readers.

These tests are hermetic: no daemon, no live team-state, no network. Each pins a
behavior that was WRONG before the fix, so a revert turns them red.

Run: STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_body_keyed_worker_visibility.py -v
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _cross_agent_attribution_filter import filter_paths  # noqa: E402
from _team_state import compose_agent_status  # noqa: E402
import worker_close_in_flight_clear as wc  # noqa: E402


# ── helpers (mirror test_attribution_filter_no_self_inflight.py) ─────────────


def _stage_project(tmp_path, self_agent, partners):
    project_root = tmp_path / "repo"
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    for agent in [self_agent, *partners]:
        adir = project_root / "agents" / agent
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=" + world.as_posix() + "\n", encoding="utf-8"
        )
        (adir / "session").mkdir(parents=True, exist_ok=True)
    return project_root, world


def _write_team_state(world, agent_status):
    import yaml

    (world / "team-state.yaml").write_text(
        yaml.safe_dump({"agent_status": agent_status}), encoding="utf-8"
    )


def _touch_file(repo_root, rel_path, mtime_epoch):
    full = repo_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# test\n", encoding="utf-8")
    os.utime(full, (mtime_epoch, mtime_epoch))


def _iso(ep):
    return datetime.datetime.fromtimestamp(ep).isoformat()


def _body(goal_id, ep, title="t"):
    return {"goal_id": goal_id, "title": title, "claimed_at": _iso(ep), "phase": 4}


# ── 1. compose pass-through ─────────────────────────────────────────────────


def test_compose_preserves_body_keyed_sibling_key():
    """Unknown-key pass-through: compose picks WHOLE ROWS, so a sibling key
    survives. Asserted rather than assumed — the consumers below depend on it."""
    rows = {
        "bravo": {
            "last_active": "2026-08-04T10:00:00",
            "in_flight": None,
            "in_flight_bodies": {"sid-a": _body("g-1", 1700000000)},
        }
    }
    composed = compose_agent_status({}, rows)
    assert "in_flight_bodies" in composed["bravo"]
    assert composed["bravo"]["in_flight_bodies"]["sid-a"]["goal_id"] == "g-1"


def test_compose_body_key_survives_newest_wins_pick():
    """The row wins a tie against a core entry, and carries its sibling key with
    it — the key must not be dropped by the pick."""
    core = {"bravo": {"last_active": "2026-08-04T09:00:00", "in_flight": None}}
    rows = {
        "bravo": {
            "last_active": "2026-08-04T10:00:00",
            "in_flight_bodies": {"sid-a": _body("g-1", 1700000000)},
        }
    }
    composed = compose_agent_status(core, rows)
    assert composed["bravo"]["in_flight_bodies"]["sid-a"]["goal_id"] == "g-1"


# ── 2. attribution filter: partner side (oldest wins) ───────────────────────


def test_partner_body_row_alone_marks_file_concurrent(tmp_path):
    """WAS BROKEN: a partner running as a worker Body has in_flight=None, so its
    concurrent edits were KEPT and swept into this agent's commit (guard-741)."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": None},
            "bravo": {"in_flight": None,
                      "in_flight_bodies": {"sid-b": _body("g-9", base)}},
        },
    )
    _touch_file(project_root, "core/scripts/x.py", base + 60)
    kept, dropped = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept == []
    assert dropped and dropped[0]["reason"] == "concurrent-partner"
    assert dropped[0]["owner"] == "bravo"

    # POSITIVE CONTROL (guard-2250): remove ONLY the body row and re-measure.
    # The file must flip back to KEPT — that is the pre-fix behaviour, and it
    # proves the drop above is attributable to the body row rather than to some
    # other source in the filter. Without this the assertion could pass for a
    # reason that has nothing to do with this change.
    _write_team_state(
        world,
        {"zeta": {"in_flight": None}, "bravo": {"in_flight": None}},
    )
    kept2, _ = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept2 == ["core/scripts/x.py"], "control failed: drop was not the body row"


def test_partner_window_takes_oldest_across_reducer_and_bodies(tmp_path):
    """Partner side takes the MIN: the widest window is the conservative one,
    because committing a partner's file is the unrecoverable direction."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": None},
            "bravo": {
                "in_flight": _body("g-reducer", base + 1000),
                "in_flight_bodies": {"sid-b": _body("g-worker", base)},
            },
        },
    )
    # mtime sits BETWEEN the body claim and the reducer claim. Only the older
    # (body) epoch makes it concurrent — a max-based pick would keep it.
    _touch_file(project_root, "core/scripts/x.py", base + 60)
    kept, dropped = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept == []
    assert dropped[0]["owner"] == "bravo"


def test_null_body_row_creates_no_phantom_partner_claim(tmp_path):
    """A CLEARED body row is set null (the daemon's `remove` is list-only), so a
    null must not read as a live claim — else a closed worker blocks forever."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": _body("g-self", base + 500)},
            "bravo": {"in_flight": None, "in_flight_bodies": {"sid-b": None}},
        },
    )
    _touch_file(project_root, "core/scripts/x.py", base + 600)
    kept, dropped = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept == ["core/scripts/x.py"]
    assert dropped == []


# ── 3. attribution filter: self side (newest wins) ──────────────────────────


def test_self_body_row_anchors_skip_concurrent(tmp_path):
    """WAS BROKEN: a worker Body had self_claimed_at=0, so skip_concurrent never
    fired and the worker's OWN in-claim edits were handed to a partner."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {"in_flight": None,
                     "in_flight_bodies": {"sid-z": _body("g-mine", base)}},
            "bravo": {"in_flight": _body("g-theirs", base)},
        },
    )
    _touch_file(project_root, "core/scripts/x.py", base + 60)
    kept, dropped = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept == ["core/scripts/x.py"], f"own in-claim work dropped: {dropped}"

    # POSITIVE CONTROL (guard-2250): drop ONLY zeta's body row. self_claimed_at
    # falls back to 0, skip_concurrent stops firing, and the worker's own edit is
    # handed to bravo — the exact pre-fix defect. If this control does not flip,
    # the assertion above was passing for an unrelated reason.
    _write_team_state(
        world,
        {"zeta": {"in_flight": None}, "bravo": {"in_flight": _body("g-theirs", base)}},
    )
    kept2, dropped2 = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept2 == [] and dropped2[0]["owner"] == "bravo", (
        "control failed: self anchor was not what kept the file"
    )


def test_self_anchor_takes_newest_across_reducer_and_bodies(tmp_path):
    """Self side takes the MAX: a later start claims LESS authorship, which is
    the conservative direction for this agent."""
    base = 1700000000
    project_root, world = _stage_project(tmp_path, "zeta", ["bravo"])
    _write_team_state(
        world,
        {
            "zeta": {
                "in_flight": _body("g-old", base),
                "in_flight_bodies": {"sid-z": _body("g-new", base + 1000)},
            },
            "bravo": {"in_flight": None},
        },
    )
    # mtime predates the NEWEST self anchor by more than the skew tolerance, so
    # Source 3 fires. Under a min-based pick it would sit at the anchor and stay.
    _touch_file(project_root, "core/scripts/x.py", base + 10)
    kept, dropped = filter_paths(
        ["core/scripts/x.py"], "zeta", str(project_root), str(world)
    )
    assert kept == []
    assert dropped[0]["reason"] == "pre-claim-mtime"


# ── 4. guard-741 collision probe consumes body rows ─────────────────────────


def _load_pickup_module():
    spec = importlib.util.spec_from_file_location(
        "gpcc", REPO_ROOT / "scripts" / "goal-pickup-coordination-check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_partner_in_flight_probe_sees_body_rows(tmp_path, monkeypatch):
    """WAS BROKEN: the probe built candidates from `in_flight` only, so a worker
    partner was invisible and the guard-741 gate silently opened."""
    import yaml

    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "team-state.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_status": {
                    "zeta": {"in_flight": None},
                    "bravo": {
                        "in_flight": None,
                        "in_flight_bodies": {
                            "sid-b": _body("g-9", 1700000000, "worker goal")
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    mod = _load_pickup_module()
    import _paths

    monkeypatch.setattr(_paths, "WORLD_DIR", str(world), raising=False)
    monkeypatch.setenv("MIND_AGENT", "zeta")
    got = mod._partner_in_flight()
    assert got is not None, "worker partner invisible to the guard-741 probe"
    assert got["name"] == "bravo" and got["goal_id"] == "g-9"


def test_partner_in_flight_probe_skips_null_body_rows(tmp_path, monkeypatch):
    """A cleared (null) body row must not resurrect as a phantom partner claim."""
    import yaml

    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "team-state.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_status": {
                    "zeta": {"in_flight": None},
                    "bravo": {"in_flight": None,
                              "in_flight_bodies": {"sid-b": None}},
                }
            }
        ),
        encoding="utf-8",
    )
    mod = _load_pickup_module()
    import _paths

    monkeypatch.setattr(_paths, "WORLD_DIR", str(world), raising=False)
    monkeypatch.setenv("MIND_AGENT", "zeta")
    assert mod._partner_in_flight() is None


# ── 5. worker close clears the body row ─────────────────────────────────────


def test_clear_body_row_requires_sid():
    """No SID means no row was ever written, so there is nothing to clear — and
    an unkeyed clear must never be attempted."""
    assert wc.clear_body_row("bravo", None) == "no-sid"
    assert wc.clear_body_row("bravo", "") == "no-sid"


def test_clear_body_row_targets_this_sid(monkeypatch):
    seen = {}

    def fake_call(method, path, query=None, **kw):
        seen["method"], seen["path"], seen["query"] = method, path, query
        return "{}"

    monkeypatch.setattr(wc._rt, "rt_call", fake_call)
    assert wc.clear_body_row("bravo", "sid-a") == "cleared"
    assert seen["method"] == "POST"
    # : the DEDICATED writer, not the generic field-path dispatch.
    # This assertion used to pin `/v1/team-state/update` with value="null",
    # because `_remove_nested` is list-only and silently no-ops on a dict key
    # while reporting ok:true — so a real removal was not expressible through
    # that endpoint and the row could only be nulled. The residue was one
    # permanent null key per SID this agent had ever run (guard-2305: a
    # structured team-state field gets its own writer).
    assert seen["path"] == "/v1/team-state/clear-body-row"
    assert seen["query"] == {"agent": "bravo", "sid": "sid-a"}
    # Pin the ABSENCE of the old shape too. `field`/`value` reappearing here is
    # exactly how this would silently regress to nulling — the return token is
    # "cleared" either way, so nothing else in this file would notice.
    assert "field" not in seen["query"]
    assert "value" not in seen["query"]


def test_clear_body_row_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(wc._rt, "rt_call", boom)
    assert wc.clear_body_row("bravo", "sid-a") == "failed"


def test_clear_body_row_skips_non_resident_agent(monkeypatch):
    """A row can only exist where it could have been WRITTEN, so the clear side
    carries the same residency predicate as the write side (guard-2611).

    Without it, the unconditional clear MINTED a shared-store row: setting a
    nested key to null does not only clear, it materialises the parent. Measured
    2026-08-04 -- the parametrized CLI test in
    test_worker_close_in_flight_clear.py passes a deliberately nonexistent
    `--agent`, and one full-suite run left a real shard for it in the LIVE
    team-state, which then reddened test_active_agents_tripwire in an entirely
    different suite. Nothing here could see it: the write prints no stdout, the
    return token is identical either way, and `in_flight` is never touched.

    The positive control is test_clear_body_row_targets_this_sid above, which
    uses a RESIDENT agent and DOES reach rt_call -- so this pin proves the gate
    DISCRIMINATES rather than merely never firing (guard-2250).
    """
    called = []

    def spy(*a, **k):
        called.append(1)
        return "{}"

    monkeypatch.setattr(wc._rt, "rt_call", spy)
    assert wc.clear_body_row("zz-no-such-agent-ever", "sid-a") == "not-resident"
    assert called == [], "a non-resident agent must not reach the daemon at all"


def test_write_side_carries_the_same_residency_predicate():
    """The write and clear sides must gate on the SAME thing -- an asymmetric
    pair re-opens the phantom-row hole from whichever end lacks the gate.

    Pinned structurally because the write side is bash. The anchor deliberately
    includes ` --query`: the bare command string also appears in this script's
    HEADER COMMENT, and anchoring on it pins prose rather than the call site
    (that exact mistake cost a false-failing assertion when this file was
    written -- a structural pin whose anchor occurs in prose is pinning prose).
    """
    src = (REPO_ROOT / "scripts" / "team-state-in-flight.sh").read_text(
        encoding="utf-8")
    gate = src.index('[ ! -d "$_AGENT_DIR" ]')
    post = src.index("rt_call POST /v1/team-state/in-flight --query")
    assert gate < post, "residency gate must precede the in_flight POST"


@pytest.mark.parametrize(
    "reducer_verdict",
    [
        {"verdict": "not-ours", "agent": "bravo", "goal_id": "g-1", "reason": "r"},
        {"verdict": "unknown-owner", "agent": "bravo", "goal_id": None, "reason": "r"},
        {"verdict": "cleared", "agent": "bravo", "goal_id": "g-1", "reason": "r"},
    ],
)
def test_body_row_cleared_on_every_reducer_path(monkeypatch, reducer_verdict):
    """The body row is ours by KEY, so it must clear even when the reducer row is
    explicitly not ours — which is precisely the worker Body's common case."""
    monkeypatch.setattr(wc, "clear_body_row", lambda a, s: "cleared")
    monkeypatch.setattr(wc, "_run_reducer_leg", lambda a, s: dict(reducer_verdict))
    out = wc.run("bravo", "sid-a")
    assert out["body_row"] == "cleared"
    assert out["verdict"] == reducer_verdict["verdict"], "reducer verdict perturbed"


def test_reducer_decide_branch_table_unchanged():
    """The reducer ownership table is the -d/ safety rail. This
    change must not perturb a single branch of it."""
    assert wc.decide("b", "s1", None, None)["verdict"] == "absent"
    assert wc.decide("b", "s1", {"x": 1}, None)["verdict"] == "unknown-owner"
    assert wc.decide("b", None, {"goal_id": "g"}, None)["verdict"] == "unknown-owner"
    assert wc.decide("b", "s1", {"goal_id": "g"}, None)["verdict"] == "unknown-owner"
    assert wc.decide("b", "s1", {"goal_id": "g"}, "s2")["verdict"] == "not-ours"
    assert wc.decide("b", "s1", {"goal_id": "g"}, "s1")["verdict"] == "ours"


# ── 6. the reducer stamp itself is untouched ────────────────────────────────


def test_reducer_stamp_gate_still_terminates_non_reducer_path():
    """Structural pin: the non-reducer branch must still `exit 0` BEFORE the
    in_flight POST. If a future edit lets a worker fall through to the reducer
    stamp, the g-306-132-d mutual-clobber defect returns."""
    src = (REPO_ROOT / "scripts" / "team-state-in-flight.sh").read_text(
        encoding="utf-8"
    )
    gate = src.index('"$_RSID" != "$MIND_SID"')
    # Anchor on the real INVOCATION, not the endpoint path — that same path
    # appears in the file's header comment at char ~265, and matching it there
    # made this assertion compare the gate against a comment (caught by this
    # test failing on its first run). A structural pin whose anchor also occurs
    # in prose is pinning prose.
    post = src.index('rt_call POST /v1/team-state/in-flight --query')
    assert gate < post, "reducer gate must precede the in_flight POST"
    # the branch entered at the gate terminates before reaching the POST
    assert "exit 0" in src[gate:post], "non-reducer branch must exit before POST"
    # and the body write lives inside that branch, not after it
    body = src.index("in_flight_bodies")
    assert gate < body < post, "body-row write must live inside the skip branch"
