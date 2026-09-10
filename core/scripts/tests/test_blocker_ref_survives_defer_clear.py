""" — clearing defer_reason must NOT strip a still-live blocker_ref.

THE DEFECT. Both defer-clear sites popped `blocker_ref` unconditionally:

    else:                                   # defer_reason cleared
        goal["defer_reason_set_at"] = None
        goal.pop("blocker_ref", None)       # <- unconditional

on the stated premise that "a blocker_ref on an un-deferred goal is an orphan".
That premise is FALSE for one real consumer. `world/scripts/deploy-hold-check.sh`
reads a `deploy-hold:` blocker_ref on a PENDING, NON-DEFERRED goal as the deploy
gate itself — which is the shape `world/conventions/deploy-holds.md` prescribes.
So any defer-clear on such a goal silently disarmed the gate standing between an
unvalidated change and an auto-deploying production repo, with no log line.

THREE POP SITES EXIST, NOT ONE (guard-2717: a finding naming N sites reports
where its author LOOKED, not where the behavior lives). The goal named only the
first:

  1. core/scripts/aspirations.py           cmd_update_goal defer-clear   FIXED
  2. mind_api/src/endpoints/aspirations_write.py  defer-clear            FIXED
  3. core/scripts/aspirations.py:84        _normalize_terminal_goal      LEFT ALONE

Site 2 is the one that runs for every `aspirations-update-goal.sh` call — the
wrapper is daemon-only with no CLI fallback (`.claude/rules/
no-python-cli-fallback.md`), so a fix applied only to site 1 would have been
INERT while looking entirely correct in the diff. Site 1 is NOT dead either: the
unattended defer-clearing sweeps shell straight to `aspirations.py update-goal
<id> defer_reason null` (precondition-defer-recheck.py::_clear_defer,
defer-recheck.py, credential-defer-recheck.py), so it is the path that fires on a
cadence with nobody watching. Both therefore need their own test; a shared-
predicate test alone would stay green while either call site was reverted.

Site 3 is deliberately UNGATED and is pinned as such below: a TERMINAL goal's
reservation is finished whatever its expiry says.

WHAT THE FIX ASSERTS. `gates.blocker_ref.is_live_lease` — one implementation,
imported by both writers — preserves a ref only when it can PROVE it is still
inside its lease (an explicit, parseable, future `expires_at`). Everything else
still pops, so orphan cleanup is unchanged. That mirrors the domain rule these
leases already live under: "a lease nobody renewed has ended".

TEST SHAPE (guard-4166). The fix's effect is that something STOPS DISAPPEARING,
so each call site gets a POSITIVE-EXISTENCE pin (the live ref is still there) AND
an ABSENCE control (an expired ref is still gone). The pair is a partition: the
existence pin alone is satisfied by a "remove the pop entirely" mutant, and the
absence control alone is satisfied by the original unconditional pop. Expected
mutation outcomes are stated per test in each docstring — check BOTH halves.

Hermetic: tempdir world, in-process DaemonFixture for the daemon path, subprocess
with MIND_WORLD/MIND_META pinned for the CLI path. STORAGE_BACKEND=local is
forced on the subprocess (guard-955 — this box runs own-cloud, and OwnCloudBackend
derives its S3 key from the env id, NOT from MIND_WORLD, so an unpinned tmp-world
write lands on the PRODUCTION key).

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_blocker_ref_survives_defer_clear.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
for _p in (str(SCRIPT_DIR), str(CORE_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _daemon_fixture import DaemonFixture  # noqa: E402
from gates.blocker_ref import is_live_lease  # noqa: E402

GOAL_ID = "g-900-01"
ASP_ID = "asp-900"


def _ref(expires_at):
    """A deploy-hold reservation in the exact shape deploy-hold-check.sh reads."""
    return {
        "type": "infrastructure",
        "external_id": "deploy-hold:Some-Repo:2026-09-09",
        "created_at": "2026-09-09T00:00:00",
        "expires_at": expires_at,
        "owner": "alpha",
        "why": "soak in flight; clears when the soak reports green",
    }


def _future(hours=24):
    return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(hours=24):
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def _make_world(tmp: Path, blocker_ref) -> tuple[Path, Path]:
    """Tempdir world holding ONE pending goal that carries a defer + a ref.

    `pending` + a populated `blocker_ref` is exactly the deploy-hold shape; the
    defer_reason is what the test then clears.
    """
    world = tmp / "world"
    world.mkdir()
    goal = {
        "id": GOAL_ID,
        "title": "Goal carrying a deploy-hold reservation",
        "description": "Pending, holding a lease, deferred for an unrelated reason",
        "status": "pending",
        "priority": "MEDIUM",
        "blocked_by": [],
        "defer_reason": "precondition_unmet: waiting on an unrelated window",
        "defer_reason_set_at": "2026-09-09T00:00:00",
        "blocker_ref": blocker_ref,
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": ASP_ID,
        "title": "blocker_ref lease survival",
        "motivation": "Pin that a live lease survives a defer clear",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-09-01T00:00:00",
        "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _read_goal(world: Path) -> dict:
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == GOAL_ID:
                return g
    raise AssertionError(f"{GOAL_ID} vanished from the tmp world")


def _daemon_clear_defer(port: int) -> tuple[int, str]:
    """POST the same update-goal call aspirations-update-goal.sh makes."""
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={GOAL_ID}&field=defer_reason&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(None).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _cli_clear_defer(world: Path, agent_dir: Path) -> subprocess.CompletedProcess:
    """Run the CLI exactly as precondition-defer-recheck.py::_clear_defer does."""
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
         "--source", "world", "update-goal", GOAL_ID, "defer_reason", "null"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=120)


# --- the predicate itself (pure; fail-closed contract) -----------------------

def test_predicate_accepts_only_a_provably_live_lease():
    """Every input that cannot PROVE it is inside its lease must return False.

    Under any mutation that widens the predicate (e.g. `return True`), the
    absence controls below go RED — that is what makes this more than a
    restatement of the implementation.
    """
    assert is_live_lease(_ref(_future())) is True
    assert is_live_lease(_ref(_past())) is False, "an expired lease has ended"
    assert is_live_lease(_ref(None)) is False, "no expiry => cannot prove liveness"
    assert is_live_lease(_ref("not-a-date")) is False, "unparseable => refusal"
    assert is_live_lease({}) is False
    assert is_live_lease(None) is False
    assert is_live_lease("deploy-hold:X") is False, "a bare string is not a ref"


def test_predicate_boundary_is_strictly_future():
    """An expiry exactly at `now` is ENDED, not live — leases close at their edge."""
    now = datetime(2026, 9, 9, 12, 0, 0)
    assert is_live_lease(_ref("2026-09-09T12:00:00"), now=now) is False
    assert is_live_lease(_ref("2026-09-09T12:00:01"), now=now) is True


# --- site 2: the DAEMON path (every aspirations-update-goal.sh call) ---------

def test_daemon_defer_clear_preserves_live_lease():
    """EXISTENCE PIN for the daemon site.

    Mutation expectation: RED when aspirations_write.py's defer-clear branch is
    reverted to an unconditional pop. GREEN when the pop is removed entirely
    (that mutant is caught by the absence control below instead).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _ref(_future()))
        with DaemonFixture(world) as df:
            status, out = _daemon_clear_defer(df.port)
            assert status == 200, f"update-goal status={status}; body={out!r}"
        g = _read_goal(world)
        assert g.get("defer_reason") is None, "precondition: the defer must be cleared"
        assert g.get("blocker_ref") is not None, (
            "a LIVE deploy-hold lease was stripped by a defer clear on the daemon "
            "path — this silently disarms deploy-hold-check.sh (g-115-9535)")
        assert g["blocker_ref"]["external_id"] == "deploy-hold:Some-Repo:2026-09-09"


def test_daemon_defer_clear_still_drops_expired_lease():
    """ABSENCE CONTROL for the daemon site — orphan cleanup must still work.

    Mutation expectation: RED when the daemon pop is removed entirely. GREEN
    under the revert-to-unconditional-pop mutant. If this test ever goes red
    together with the existence pin above, it has stopped being a control and
    has become a second copy of the same assertion (guard-4166).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _ref(_past()))
        with DaemonFixture(world) as df:
            status, out = _daemon_clear_defer(df.port)
            assert status == 200, f"update-goal status={status}; body={out!r}"
        g = _read_goal(world)
        assert g.get("blocker_ref") is None, (
            "an EXPIRED lease must still be dropped on a defer clear — the fix "
            "narrows the pop, it does not remove it")


def test_daemon_defer_clear_still_drops_ref_without_expiry():
    """ABSENCE CONTROL 2: a ref that carries no expiry cannot prove liveness."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd), _ref(None))
        with DaemonFixture(world) as df:
            status, out = _daemon_clear_defer(df.port)
            assert status == 200, f"update-goal status={status}; body={out!r}"
        assert _read_goal(world).get("blocker_ref") is None


# --- site 1: the CLI path (the unattended defer-recheck sweeps) --------------

def test_cli_defer_clear_preserves_live_lease():
    """EXISTENCE PIN for the CLI site.

    This is the path the unattended sweeps take, so it is the one that fires
    with nobody watching. Mutation expectation: RED when aspirations.py's
    defer-clear branch is reverted to an unconditional pop.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world(Path(tmpd), _ref(_future()))
        proc = _cli_clear_defer(world, agent_dir)
        assert proc.returncode == 0, (
            f"CLI update-goal failed rc={proc.returncode}\n"
            f"stdout={proc.stdout[-1500:]}\nstderr={proc.stderr[-1500:]}")
        g = _read_goal(world)
        assert g.get("defer_reason") is None, "precondition: the defer must be cleared"
        assert g.get("blocker_ref") is not None, (
            "a LIVE deploy-hold lease was stripped by a defer clear on the CLI "
            "path — this is the path precondition-defer-recheck.py takes")


def test_cli_defer_clear_still_drops_expired_lease():
    """ABSENCE CONTROL for the CLI site.

    Mutation expectation: RED when the CLI pop is removed entirely; GREEN under
    the revert-to-unconditional-pop mutant.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world(Path(tmpd), _ref(_past()))
        proc = _cli_clear_defer(world, agent_dir)
        assert proc.returncode == 0, (
            f"CLI update-goal failed rc={proc.returncode}\n"
            f"stdout={proc.stdout[-1500:]}\nstderr={proc.stderr[-1500:]}")
        assert _read_goal(world).get("blocker_ref") is None


# --- site 3: the terminal normalizer stays UNGATED (goal outcome 2) ---------

def test_terminal_goal_still_drops_even_a_live_lease():
    """The sibling pop at aspirations.py:84 is deliberately NOT gated.

    A terminal goal's reservation is finished whatever its expiry says, so the
    normalizer must keep dropping it. Mutation expectation: RED when that pop is
    removed. This is the pin the goal's outcome 2 asks for, and it is what stops
    a future reader "simplifying" the fix by routing site 3 through the same
    predicate.
    """
    sys.path.insert(0, str(CORE_SCRIPTS))
    import importlib
    asp_mod = importlib.import_module("aspirations")
    goal = {
        "id": GOAL_ID,
        "status": "completed",
        "defer_reason": None,
        "blocker_ref": _ref(_future()),
    }
    asp_mod._normalize_terminal_goal(goal)
    assert goal.get("blocker_ref") is None, (
        "a TERMINAL goal must drop its blocker_ref even when the lease is still "
        "live — the narrowing applies to the defer-clear sites only")


def test_terminal_normalizer_leaves_a_pending_goal_alone():
    """Positive control for the test above: the normalizer is status-gated.

    Without this, `test_terminal_goal_still_drops_even_a_live_lease` would pass
    against a normalizer that popped unconditionally on every goal.
    """
    import importlib
    asp_mod = importlib.import_module("aspirations")
    goal = {
        "id": GOAL_ID,
        "status": "pending",
        "defer_reason": None,
        "blocker_ref": _ref(_future()),
    }
    asp_mod._normalize_terminal_goal(goal)
    assert goal.get("blocker_ref") is not None, (
        "the terminal normalizer must not touch a non-terminal goal")
