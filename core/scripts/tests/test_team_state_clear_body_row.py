"""test_team_state_clear_body_row.py —  dict-key REMOVE for body rows.

`worker_close_in_flight_clear.clear_body_row` used to clear
`agent_status.<agent>.in_flight_bodies.<sid>` by SETTING NULL, because the
generic `POST /v1/team-state/update` dispatch's `_remove_nested` returns early
unless the target is a LIST — so `--operation remove` on a dict key was a silent
no-op that reported ok:true having done nothing. Residue: one permanent
null-valued key per SID an agent had ever run, on a shared synced store.

The fix is a dedicated op (guard-2305: a structured team-state field gets its own
writer, not the generic dotpath setter), built once in `_team_state` and used by
BOTH the CLI (`team-state.py clear-body-row`) and the daemon
(`POST /v1/team-state/clear-body-row`) — guard-742 parity by construction, which
is exactly what widening the hand-mirrored `_remove_nested` PAIR would not have
given.

Covers:
  - removes the named sid; leaves LIVE siblings standing (the fail-safe half)
  - sweeps null siblings, so pre-fix residue drains on first close
  - pops in_flight_bodies entirely when it empties (no `{}` residue one level up)
  - stamps last_active — the property that makes the removal DURABLE under
    merge_team_state_shard's whole-snapshot LWW, verified end-to-end
  - no-op leaves the row byte-identical (no timestamp movement)
  - only exact None is swept; a stray non-dict value survives
  - status resets per invocation (the own-cloud conflict-retry property)
  - body_row_shard_present is False for a missing shard (guard-2611: writing
    anyway would CREATE it)
  - CLI subprocess: removes a real row, drains nulls, refuses to create a shard
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

from _team_state import (  # noqa: E402
    body_row_shard_present,
    make_clear_body_row_modifier,
    row_path,
)

SID = "sid-live-0001"
OTHER = "sid-live-0002"
DEAD = "sid-dead-0003"


def _row(bodies, last_active="2026-01-01T00:00:00"):
    return {"last_active": last_active, "in_flight_bodies": dict(bodies)}


def _apply(row, sid=SID, author="bravo", now="2026-08-04T14:00:00"):
    status = {}
    mod = make_clear_body_row_modifier(author, sid, now_fn=lambda: now,
                                       status=status)
    return mod(row), status


# --- removal + sibling preservation -----------------------------------------

def test_removes_named_sid():
    out, st = _apply(_row({SID: {"goal_id": "g-1"}}))
    assert st["removed"] is True
    assert "in_flight_bodies" not in out  # emptied -> key popped entirely


def test_live_sibling_survives():
    """The fail-safe half: clearing one Body must never blank a concurrent one."""
    out, st = _apply(_row({SID: {"goal_id": "g-1"}, OTHER: {"goal_id": "g-2"}}))
    assert st["removed"] is True
    assert st["remaining"] == 1
    assert out["in_flight_bodies"] == {OTHER: {"goal_id": "g-2"}}


def test_absent_sid_is_a_supported_no_op():
    out, st = _apply(_row({OTHER: {"goal_id": "g-2"}}))
    assert st["removed"] is False
    assert out["in_flight_bodies"] == {OTHER: {"goal_id": "g-2"}}


# --- null sweep (drains pre-fix residue) ------------------------------------

def test_sweeps_null_siblings():
    out, st = _apply(_row({SID: {"goal_id": "g-1"}, DEAD: None,
                           OTHER: {"goal_id": "g-2"}}))
    assert (st["removed"], st["nulls_swept"], st["remaining"]) == (True, 1, 1)
    assert out["in_flight_bodies"] == {OTHER: {"goal_id": "g-2"}}


def test_sweep_runs_even_when_the_sid_is_absent():
    """This is what makes the CLI a drain path for residue whose sessions are
    gone: naming any sid still clears the accumulated nulls."""
    out, st = _apply(_row({DEAD: None, "sid-dead-0004": None}), sid="whatever")
    assert (st["removed"], st["nulls_swept"]) == (False, 2)
    assert "in_flight_bodies" not in out


def test_only_exact_none_is_swept():
    """A non-dict, non-None value is unexplained content this op has no mandate
    to destroy — consumers skip it either way."""
    out, _ = _apply(_row({SID: {"goal_id": "g-1"}, "sid-odd": "hand-edited"}))
    assert out["in_flight_bodies"] == {"sid-odd": "hand-edited"}


# --- durability under merge --------------------------------------------------

def test_stamps_last_active():
    out, _ = _apply(_row({SID: {"goal_id": "g-1"}}, last_active="2026-01-01T00:00:00"))
    assert out["last_active"] == "2026-08-04T14:00:00"


def test_removal_survives_a_merge_with_a_stale_peer_copy():
    """The stamp is not cosmetic. merge_team_state_shard reconciles a
    both-diverged shard by whole-snapshot LWW on last_active, so an UNSTAMPED
    pop would lose to any newer peer snapshot still carrying the key — the
    removal would silently come back."""
    from coordination_merge import merge_team_state_shard

    pre = _row({SID: {"goal_id": "g-1"}}, last_active="2026-08-04T13:00:00")
    post, _ = _apply(dict(pre))  # stamped 14:00:00
    peer = yaml.safe_dump(pre, sort_keys=False).encode("utf-8")
    mine = yaml.safe_dump(post, sort_keys=False).encode("utf-8")

    for a, b in ((mine, peer), (peer, mine)):  # commutative (guard-907)
        merged = yaml.safe_load(merge_team_state_shard(a, b).decode("utf-8"))
        assert "in_flight_bodies" not in merged, "removal resurrected by merge"


# --- no-op / hostile shapes --------------------------------------------------

def test_no_op_does_not_move_timestamps():
    row = _row({OTHER: {"goal_id": "g-2"}}, last_active="2026-01-01T00:00:00")
    out, st = _apply(dict(row), sid="absent-sid")
    assert out["last_active"] == "2026-01-01T00:00:00"
    assert (st["removed"], st["nulls_swept"]) == (False, 0)


def test_missing_or_non_dict_in_flight_bodies_is_left_alone():
    for bodies in (None, "not-a-dict", 7, []):
        row = {"last_active": "2026-01-01T00:00:00", "in_flight_bodies": bodies}
        out, st = _apply(row)
        assert out["in_flight_bodies"] == bodies
        assert st["removed"] is False
    out, st = _apply({"last_active": "2026-01-01T00:00:00"})
    assert st["removed"] is False


def test_non_dict_row_does_not_raise():
    status = {}
    mod = make_clear_body_row_modifier("bravo", SID, status=status)
    assert mod(None) == {}


def test_status_resets_per_invocation():
    """locked_modify_yaml re-invokes the SAME modifier object on an own-cloud
    If-Match conflict, so a losing attempt's verdict must not survive into the
    winning one (g-306-163, learned on the sibling clear-in-flight factory)."""
    status = {}
    mod = make_clear_body_row_modifier("bravo", SID, now_fn=lambda: "t",
                                       status=status)
    mod(_row({SID: {"goal_id": "g-1"}, DEAD: None}))
    assert (status["removed"], status["nulls_swept"]) == (True, 1)
    mod(_row({OTHER: {"goal_id": "g-2"}}))          # fresher row, nothing to do
    assert (status["removed"], status["nulls_swept"]) == (False, 0)


# --- shard-presence guard (guard-2611) --------------------------------------

def test_shard_presence_guard():
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        assert body_row_shard_present(world, "ghost-agent") is False
        p = row_path(world, "real-agent")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("last_active: '2026-01-01T00:00:00'\n", encoding="utf-8")
        assert body_row_shard_present(world, "real-agent") is True


# --- CLI (isolated tmp world, never the live store) --------------------------

def _cli(world, *args):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "bravo"
    env["STORAGE_BACKEND"] = "local"          # guard-955
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "team-state.py"), "clear-body-row",
         *args],
        capture_output=True, text=True, env=env, cwd=str(CORE_SCRIPTS.parent.parent))


def test_cli_removes_row_and_drains_nulls():
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        p = row_path(world, "bravo")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(
            _row({SID: {"goal_id": "g-1"}, DEAD: None, OTHER: {"goal_id": "g-2"}}),
            sort_keys=False), encoding="utf-8")

        r = _cli(world, "--agent", "bravo", "--sid", SID)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert (out["removed"], out["nulls_swept"], out["remaining"]) == (True, 1, 1)

        after = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert after["in_flight_bodies"] == {OTHER: {"goal_id": "g-2"}}


def test_cli_refuses_to_create_a_shard_for_a_missing_agent():
    """The pre-fix null-write manufactured a real `no-such-agent-xyz` shard in
    the SHARED store from a test fixture's nonexistent --agent, which then
    tripped an unrelated suite's roster detector. This op must not."""
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        r = _cli(world, "--agent", "no-such-agent-xyz", "--sid", SID)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["no_shard"] is True
        assert not row_path(world, "no-such-agent-xyz").exists()


# --- parity (guard-742): one factory, both callers ---------------------------

def test_cli_and_daemon_share_one_implementation():
    """Not a style check. The rejected alternative (teaching `_remove_nested`
    about dict keys) would have required editing a hand-mirrored PAIR with no
    structural guarantee the copies agree; this asserts the property that
    replaced it."""
    cli = (CORE_SCRIPTS / "team-state.py").read_text(encoding="utf-8")
    daemon = (CORE_SCRIPTS.parent.parent / "mind_api" / "src" / "world"
              / "team_state_write.py").read_text(encoding="utf-8")
    for src, who in ((cli, "CLI"), (daemon, "daemon")):
        assert "make_clear_body_row_modifier" in src, f"{who} lost the shared factory"
        assert "body_row_shard_present" in src, f"{who} lost the shared guard"
    assert "def make_clear_body_row_modifier" not in cli
    assert "def make_clear_body_row_modifier" not in daemon


def test_worker_close_uses_the_dedicated_endpoint():
    """The call site is the whole point — a shared op nothing routes through
    would leave the null-write in place while every unit test above passed."""
    src = (CORE_SCRIPTS / "worker_close_in_flight_clear.py").read_text(encoding="utf-8")
    assert "/v1/team-state/clear-body-row" in src
    assert "in_flight_bodies.{sid}" not in src, "still writing the null field path"


# --- own-cloud read-through cache (, guard-980) ---------------------
#
# The presence guard ran a bare `.exists()` on a read-through cache whose shard
# mirror sweep is PUSH-ONLY, so a partner's shard written on another box read as
# ABSENT until something pulled it, and `clear-body-row --agent <partner>`
# returned ok:true/no_shard:true having done nothing. Invisible from the sole
# production caller (agent=self, whose shard is always local); the exposed
# surface is the CLI's `--agent`.
#
# The stubs below answer by ARGUMENT IDENTITY — which path was asked about —
# never by a positional queue of returns (guard-2220): a `side_effect=[...]`
# list would silently encode the production call ORDER, so adding one upstream
# call would shift every later answer and fail these tests with a message
# naming the wrong subsystem.


class _FakeBackend:
    """Materializes exactly the paths registered as remote-resident.

    `ensure_local` is identity on LocalBackend and a download elsewhere, so the
    faithful fake is "write the file iff the store has that object" — keyed on
    the path, so call order is irrelevant.
    """

    def __init__(self, remote_paths=()):
        self._remote = {str(p) for p in remote_paths}
        self.ensure_local_calls = []

    def ensure_local(self, path):
        self.ensure_local_calls.append(str(path))
        p = Path(path)
        if str(p) in self._remote and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("last_active: '2026-01-01T00:00:00'\n", encoding="utf-8")
        return p


def _patch_backend(monkeypatch, fake):
    import storage_backend
    monkeypatch.setattr(storage_backend, "get_backend", lambda: fake)


def test_shard_present_materializes_a_remote_only_shard(monkeypatch):
    """THE BUG: remote object present, no local file. Must answer True."""
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        target = row_path(world, "partner-agent")
        assert not target.exists()                      # the absent-local state
        fake = _FakeBackend(remote_paths=[target])
        _patch_backend(monkeypatch, fake)

        assert body_row_shard_present(world, "partner-agent") is True
        # asked about the ROW path specifically, not merely "called something"
        assert str(target) in fake.ensure_local_calls


def test_shard_present_materializes_before_the_exists_check(monkeypatch):
    """Order matters, and it is asserted by CONSEQUENCE rather than by spying on
    a call sequence: the fake only creates the file during ensure_local, so a
    True answer is only reachable if ensure_local ran FIRST. An .exists() called
    first would have seen nothing and short-circuited."""
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        target = row_path(world, "partner-agent")
        fake = _FakeBackend(remote_paths=[target])
        _patch_backend(monkeypatch, fake)

        assert body_row_shard_present(world, "partner-agent") is True
        assert target.exists(), "materialization did not happen before the check"


def test_absent_everywhere_still_creates_nothing(monkeypatch):
    """guard-2611 must survive the fix: ensure_local materializes only an object
    that ALREADY exists remotely, so a genuinely absent shard stays absent."""
    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        fake = _FakeBackend(remote_paths=[])            # store has nothing
        _patch_backend(monkeypatch, fake)

        assert body_row_shard_present(world, "ghost-agent") is False
        assert not row_path(world, "ghost-agent").exists()


def test_backend_failure_degrades_to_the_local_answer(monkeypatch):
    """This predicate gates a write, so a backend hiccup must fall back to the
    old local-only answer rather than raise."""
    class _Exploding:
        def ensure_local(self, path):
            raise RuntimeError("backend down")

    with tempfile.TemporaryDirectory() as d:
        world = Path(d)
        _patch_backend(monkeypatch, _Exploding())

        assert body_row_shard_present(world, "ghost-agent") is False
        p = row_path(world, "real-agent")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("last_active: '2026-01-01T00:00:00'\n", encoding="utf-8")
        assert body_row_shard_present(world, "real-agent") is True


def test_invalid_agent_name_never_reaches_the_backend(monkeypatch):
    """row_path validates at the file boundary; an escaping name must be refused
    before any backend call, so a bad name cannot become a network round-trip."""
    fake = _FakeBackend()
    _patch_backend(monkeypatch, fake)
    with tempfile.TemporaryDirectory() as d:
        assert body_row_shard_present(Path(d), "../escape") is False
        assert fake.ensure_local_calls == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
