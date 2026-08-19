"""test_completed_by_sid_stamp.py -- regression for the completed_by_sid stamp
(g-306-134, "cross-box two bodies" fix set B part 2).

Bug shape: the claim TRIPLE (claimed_by / claimed_at / claimed_by_sid) is popped
at every terminal transition, so once a goal closed there was no record of WHICH
BODY closed it. Rebuilding the g-115-3176 timeline required reading board posts.
`completed_by` (g-115-1562) records the AGENT; on a multi-box fleet one agent can
have several live bodies, and the agent name does not identify which one acted.

Fix: at each claim-clear site, stamp completed_by_sid BEFORE the pop.

WHEN each site stamps was reconciled in g-306-157 so that every site matches its
own `completed_by` neighbour rather than diverging from it:

  - update-goal: `value == "completed"` only, and only when unset. The claim
    triple is still popped at EVERY terminal transition (guard-151) -- only the
    STAMP is narrowed. So completed_at / completed_by / completed_by_sid form
    one first-wins triple describing a single completion event, and a SKIPPED or
    EXPIRED goal carries none of the three. Which body skipped a claimed goal is
    deliberately not recovered: different fact, would need its own name.
  - complete-by: unconditional, on BOTH arms -- mirroring `completed_by` there,
    which is likewise unconditional and likewise stamped on the recurring arm's
    cycle back to `pending`.

Source selection is asymmetric between the two implementations and that
asymmetry is the point:

  - daemon (mind_api/src/endpoints/aspirations_write.py::_completed_by_sid):
    request sid (ctx.query) preferred, claim sid as fallback. It MUST NOT read
    os.environ["MIND_SID"] -- the daemon is long-lived and carries its SPAWNER's
    sid, so an env read would stamp every daemon-routed close in the process
    lifetime with one arbitrary session's id.
  - CLI (core/scripts/aspirations.py::cmd_update_goal): env MIND_SID preferred,
    claim sid as fallback. Env is correct there -- the process IS the session.

Same env-vs-ctx split as `completed_by` (g-115-1562).

Pattern: DaemonFixture + direct HTTP POST (bash-free, exercises the LIVE daemon
path) -- mirrors test_completed_by_stamp.py, the g-115-1562 precedent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "aspirations.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"
UPDATE_WRAPPER = PROJECT_ROOT / "core" / "scripts" / "aspirations-update-goal.sh"
COMPLETE_WRAPPER = PROJECT_ROOT / "core" / "scripts" / "aspirations-complete-by.sh"

from _daemon_fixture import DaemonFixture  # noqa: E402

HOLDER_SID = "11111111-1111-1111-1111-111111111111"
ACTOR_SID = "22222222-2222-2222-2222-222222222222"
# A THIRD body, for the reopen-and-re-close case (). Distinct from both
# so a first-wins violation names WHICH body leaked in rather than just failing.
THIRD_SID = "33333333-3333-3333-3333-333333333333"


def _goal(gid: str, **extra) -> dict:
    g = {
        "id": gid, "title": f"Goal {gid}",
        "description": "completed_by_sid stamp regression fixture",
        "status": "pending", "priority": "MEDIUM", "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    g.update(extra)
    return g


def _make_world(tmp: Path) -> Path:
    """Tempdir world with four goals covering the source-selection matrix."""
    world = tmp / "world"
    world.mkdir()
    goals = [
        # claimed by HOLDER_SID -- request sid decides who is recorded
        _goal("g-100-01", claimed_by="delta", claimed_at="2026-08-03T10:00:00",
              claimed_by_sid=HOLDER_SID, status="in-progress"),
        # claimed, but the close arrives with NO request sid -> fallback
        _goal("g-100-02", claimed_by="delta", claimed_at="2026-08-03T10:00:00",
              claimed_by_sid=HOLDER_SID, status="in-progress"),
        # never claimed, no request sid -> nothing to stamp
        _goal("g-100-03"),
        # claimed -- used for the release() negative case
        _goal("g-100-04", claimed_by="delta", claimed_at="2026-08-03T10:00:00",
              claimed_by_sid=HOLDER_SID, status="in-progress"),
        # RECURRING -- complete-by forks on goal["recurring"] and the two arms
        # are separate stamp sites. Every other fixture here is non-recurring,
        # so without this one the recurring arm has no fixture at all and its
        # stamp is unreachable by the suite (guard-1999: a runtime-chosen fork
        # needs a fixture per arm, not per function).
        _goal("g-100-05", claimed_by="delta", claimed_at="2026-08-03T10:00:00",
              claimed_by_sid=HOLDER_SID, status="in-progress",
              recurring=True, interval_hours=24,
              lastAchievedAt="2026-08-02T10:00:00"),
    ]
    asp = {
        "id": "asp-100", "title": "completed_by_sid stamp regression",
        "motivation": "Test terminal-transition sid preservation", "scope": "project",
        "priority": "MEDIUM", "status": "active",
        "created": "2026-08-01T00:00:00", "goals": goals,
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "delta"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _post(port: int, path: str, query: dict, agent: str, body=None) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _update_goal(port, goal_id, field, value, agent, sid=None):
    q = {"id": goal_id, "field": field, "source": "world"}
    if sid is not None:
        q["sid"] = sid
    return _post(port, "/v1/aspirations/update-goal", q, agent, body=value)


def _find_goal(world: Path, goal_id: str) -> dict | None:
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def test_request_sid_is_preferred_over_claim_sid():
    """A non-holder body closing the goal is recorded as the COMPLETER.

    This is the interesting case, not an edge case: `_nonholder_claim_warning`
    exists precisely because a body other than the holder can take a goal
    terminal. The warning records who HELD; this field records who ACTED.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed",
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed", f"resp={out!r}"
            assert g.get("completed_by_sid") == ACTOR_SID, (
                "the REQUEST sid (the body that acted) must win over the claim "
                f"sid; got {g.get('completed_by_sid')!r}")


def test_falls_back_to_claim_sid_when_request_carries_none():
    """No request sid (un-hooked launch / direct caller) -> claim sid is kept.

    Without the fallback the stamp would be absent on every close arriving
    without MIND_SID, while the code still looked complete.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-02", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-02")
            assert g is not None and g.get("status") == "completed"
            assert g.get("completed_by_sid") == HOLDER_SID, (
                "with no request sid the claim's sid must be preserved; "
                f"got {g.get('completed_by_sid')!r}")


def test_no_sid_anywhere_writes_no_field():
    """Absent evidence beats invented evidence -- no sid source, no stamp."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-03", "status", "completed", "delta")
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-03")
            assert g is not None and g.get("status") == "completed"
            assert "completed_by_sid" not in g or not g.get("completed_by_sid"), (
                "no sid source must leave the field unwritten, not empty-stringed; "
                f"got {g.get('completed_by_sid')!r}")


def test_claim_sid_is_popped_but_completed_by_sid_survives():
    """The whole point: the claim triple clears, the forensic record does not.

    Pins BOTH directions -- a fix that kept claimed_by_sid would violate the
    claim-clearing invariant (convention Rule 3 / g-306-145), and one that
    dropped completed_by_sid would restore the unrecoverability this closes.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed",
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None
            assert "claimed_by_sid" not in g, (
                "terminal transition must still clear the claim triple; "
                f"claimed_by_sid survived as {g.get('claimed_by_sid')!r}")
            assert "claimed_by" not in g and "claimed_at" not in g
            assert g.get("completed_by_sid") == ACTOR_SID


def test_complete_by_door_also_stamps():
    """The OTHER terminal door (complete-by) stamps too.

    update-goal and complete-by are separate endpoints with separate
    claim-clearing sites. Covering only the one the other tests exercise would
    leave the explicit-completion path guarded by a shape assertion alone --
    the half-a-fix shape guard-742 names.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _post(df.port, "/v1/aspirations/complete-by",
                                {"goal_id": "g-100-01", "source": "world",
                                 "agent_name": "delta", "sid": ACTOR_SID},
                                "delta")
            assert status == 200, f"complete-by status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed", f"resp={out!r}"
            assert "claimed_by_sid" not in g, "complete-by must clear the claim sid"
            assert g.get("completed_by_sid") == ACTOR_SID, (
                "the complete-by door must stamp the completing body too; "
                f"got {g.get('completed_by_sid')!r}")


def test_release_does_not_stamp_completed_by_sid():
    """release() clears the claim WITHOUT a terminal transition -- no stamp.

    Nothing was completed, so a completed_by_sid there would be a false
    positive on every abandoned claim.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _post(df.port, "/v1/aspirations/release",
                                {"id": "g-100-04", "source": "world", "sid": ACTOR_SID},
                                "delta")
            assert status == 200, f"release status={status}; body={out!r}"
            g = _find_goal(world, "g-100-04")
            assert g is not None
            assert "claimed_by_sid" not in g, "release must clear the claim sid"
            assert not g.get("completed_by_sid"), (
                "release is not a completion -- it must NOT stamp completed_by_sid; "
                f"got {g.get('completed_by_sid')!r}")


def test_daemon_helper_never_reads_env_sid():
    """The daemon MUST NOT source the sid from os.environ (measured hazard).

    A long-lived daemon holds its SPAWNER's MIND_SID (measured cc-02: pid
    3155606 holding zeta's sid) and serves every agent, so an env read would
    attribute every close in the process lifetime to one arbitrary session --
    systematically wrong attribution presented as fact, which is strictly worse
    than an absent field.
    """
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    start = daemon.index("def _completed_by_sid(")
    end = daemon.index("\ndef ", start + 1)
    body = daemon[start:end]
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    # strip the docstring, which legitimately NAMES the forbidden pattern
    if '"""' in code:
        first = code.index('"""')
        second = code.index('"""', first + 3)
        code = code[:first] + code[second + 3:]
    assert "environ" not in code, (
        "_completed_by_sid must not read os.environ -- the daemon carries its "
        f"spawner's sid. Body after comment/docstring strip:\n{code}")
    assert "ctx.query" in body, "the daemon must source the sid from the request context"


def test_cli_daemon_completed_by_sid_parity():
    """Both write-path doors carry the stamp (guard-742).

    A fix wired into only one door is inert on the other. This guard fails if
    either side loses the stamp or the compute-before-pop ordering.
    """
    assert CLI_FILE.is_file() and DAEMON_FILE.is_file()
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    for name, text in (("CLI", cli), ("daemon", daemon)):
        assert "g-306-134" in text, f"{name} lost the g-306-134 marker"
        assert 'goal["completed_by_sid"]' in text, f"{name} lost the stamp assignment"
        # ordering: every stamp must precede its claimed_by_sid pop
        stamp = text.index('goal["completed_by_sid"]')
        pop = text.index('goal.pop("claimed_by_sid"')
        assert stamp < pop, (
            f"{name}: the stamp must be computed BEFORE the sid is popped, "
            "otherwise it reads the value it is meant to preserve after deletion")
    # the CLI is the side where env IS correct
    assert 'os.environ.get("MIND_SID"' in cli, (
        "the CLI must source the sid from env -- there the process IS the session")


def test_update_goal_wrapper_sends_sid():
    """The most-travelled terminal door must carry `&sid=` (parity with complete-by).

    Without it the daemon can only ever fall back to the claim's sid on the
    majority path, so the field would record the CLAIMING body rather than the
    completing one -- correct-looking and wrong exactly when they differ.
    """
    upd = UPDATE_WRAPPER.read_text(encoding="utf-8")
    cmp_ = COMPLETE_WRAPPER.read_text(encoding="utf-8")
    for name, text in (("aspirations-update-goal.sh", upd),
                       ("aspirations-complete-by.sh", cmp_)):
        assert 'rt_url_encode "$MIND_SID"' in text, f"{name} must url-encode the sid"
        assert "&sid=" in text, f"{name} must append the sid query param"
        assert 'if [ -n "${MIND_SID:-}" ]; then' in text, (
            f"{name} must guard the append -- an unset MIND_SID sends no param, "
            "it does not send an empty one")


# ---------------------------------------------------------------------------
# : the three branch arms the 9 tests above left behind.
#
# Measured by single-site mutation (mutation-proof-test.sh, sabotage_sites=1)
# against the suite as it stood:
#   daemon update-goal stamp      :2385 -> KILLED
#   daemon complete-by RECURRING  :3290 -> SURVIVED   <- test_recurring_* below
#   daemon complete-by non-recur  :3354 -> KILLED
#   CLI door stamp                :2223 -> SURVIVED   <- test_cli_* below
#   daemon helper preference flip :4157 -> KILLED
#   CLI preference flip (text-preserving) -> SURVIVED <- test_cli_* below
#
# The CLI preference flip is the instructive one. Rewriting the source so the
# literal `os.environ.get("MIND_SID"` disappeared went RED -- but only because
# test_cli_daemon_completed_by_sid_parity greps for that string. Swapping the
# two `or` operands while LEAVING the text in place inverts the preference and
# the whole suite stayed green. A shape assertion cannot distinguish those, so
# the kill it produced was attribution noise, not coverage (guard-2291/2395).
# ---------------------------------------------------------------------------


def test_recurring_complete_by_arm_also_stamps():
    """complete-by forks on `recurring`, and BOTH arms are stamp sites.

    The non-recurring arm is covered by test_complete_by_door_also_stamps. The
    recurring arm cycles the goal back to `pending` rather than to a terminal
    status, which is exactly why it reads as "not a completion" and got no
    fixture -- but it IS the completion of a cycle and the shipped code stamps
    there deliberately. Neutralising that stamp alone left the suite green.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _post(df.port, "/v1/aspirations/complete-by",
                                {"goal_id": "g-100-05", "source": "world",
                                 "agent_name": "delta", "sid": ACTOR_SID},
                                "delta")
            assert status == 200, f"complete-by status={status}; body={out!r}"
            g = _find_goal(world, "g-100-05")
            assert g is not None, f"resp={out!r}"
            assert g.get("status") == "pending", (
                "a recurring goal must cycle back to pending, not go terminal; "
                f"got {g.get('status')!r} -- if this fails the fixture stopped "
                "exercising the recurring ARM and the stamp assertion below is "
                "no longer testing what it names")
            assert "claimed_by_sid" not in g, (
                "the recurring arm must still clear the claim sid")
            assert g.get("completed_by_sid") == ACTOR_SID, (
                "the recurring arm stamps the completing body too; "
                f"got {g.get('completed_by_sid')!r}")


def _cli_world(root: Path) -> Path:
    """Tmp world for the CLI door: one claimed, non-terminal goal."""
    world = root / "world"
    world.mkdir(parents=True, exist_ok=True)
    asp = {
        "id": "asp-101", "title": "CLI door completed_by_sid fixture",
        "status": "active", "priority": "LOW",
        "goals": [_goal("g-101-01", claimed_by="delta",
                        claimed_at="2026-08-03T10:00:00",
                        claimed_by_sid=HOLDER_SID, status="in-progress")],
        "progress": {"completed_goals": 0, "total_goals": 1},
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    return world


def _cli_update(world: Path, sid: str | None,
                terminal: str) -> subprocess.CompletedProcess:
    """Drive the real argparse entry point; `sid=None` means MIND_SID UNSET.

    sys.executable, never a bare "bash" argv[0] (guard-580/581).
    STORAGE_BACKEND=local so a tmp write cannot reach the production S3 key
    (guard-955 / rb-2983). `--source` is TOP-LEVEL and must precede the
    subcommand -- the reversed order exits rc=2 "unrecognized arguments".

    `--override-uncommitted` is appended for `completed` and ONLY for
    `completed`: that value alone runs the pre-completion uncommitted-work gate
    (aspirations.py:1706), which inspects the REAL repo working tree, so without
    it the test would pass or fail on whether the developer happens to have dirty
    framework files -- including, circularly, the very files this suite guards.
    MIND_OVERRIDE_ALL does NOT cover that gate (measured 2026-08-03, cc-02:
    with core/scripts/aspirations.py dirty, `completed` exits rc=1 with and
    without the blanket override; `skipped` exits rc=0 either way).

    The override's audit line does NOT reach the live world, and the reason is
    NOT MIND_WORLD -- `uncommitted-work-gate.py::_resolve_world_dir` ignores
    that var and resolves WORLD_PATH from `agent_dir(MIND_AGENT)/
    local-paths.conf`, returning None when there is none. `delta` is a fixture
    name with no agent dir in PROJECT_ROOT, so the ledger write is skipped
    entirely (verified 2026-08-03: the live ledger's mtime and 1902-line count
    were unchanged across these runs, with zero rows naming g-101-01). KEEP THE
    FIXTURE AGENT A NON-AGENT. Renaming it to a real agent would start appending
    audit rows to that agent's PRODUCTION world on every run of this test.
    """
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "delta"
    env["MIND_OVERRIDE_ALL"] = "test-fixture"
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    extra = (["--override-uncommitted", "hermetic completed_by_sid fixture"]
             if terminal == "completed" else [])
    return subprocess.run(
        [sys.executable, str(CLI_FILE), "--source", "world",
         "update-goal", "g-101-01", "status", terminal, *extra],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        timeout=180,
    )


# INVERTED by . This list used to be ["skipped", "decomposed"] on the
# reasoning that the stamp block was keyed on the whole TERMINAL_GOAL_STATUSES
# set, so any member exercised it, and `completed` was avoidable ceremony
# (it additionally runs the uncommitted-work gate against the real working
# tree). That reasoning was sound about the code AS IT THEN STOOD and is now
# exactly backwards: the stamp is scoped to `completed`, so `completed` is the
# ONLY value that reaches it and the two former members now prove the NEGATIVE.
#
# Worth keeping as a note rather than deleting: the list was chosen to avoid a
# gate, and that made it a silent proxy for the behaviour under test. When the
# behaviour moved, the proxy did not fail loudly -- it kept asserting a stamp on
# values that no longer produce one, which is a red for a reason unrelated to
# what the test names. A parametrisation picked for convenience is a
# parametrisation that will mislead you the day the scope changes.
#
# The negative arms live in test_non_completed_terminal_does_not_stamp (daemon
# door, hermetic, no gate); `superseded` is excluded there and here because a
# separate gate refuses it and it never transitions.
_CLI_TERMINALS = ["completed"]


@pytest.mark.parametrize("terminal", _CLI_TERMINALS)
def test_cli_door_stamps_and_prefers_env_over_claim_sid(terminal):
    """The CLI door stamps, and env WINS over the claim sid.

    Both halves were unpinned. The door had no behavioural test at all -- only
    text greps -- so neutralising its stamp left the suite green, and so did
    inverting its preference in a way that kept the grepped literal in place.

    The claim carries HOLDER_SID and the process carries ACTOR_SID, so the two
    sources disagree: this asserts WHICH one is recorded, not merely that the
    field is non-empty. Env is correct HERE (the CLI process IS the session);
    the daemon sibling must never read env, which
    test_daemon_helper_never_reads_env_sid pins from the other side.

    The door is live, not a cold parity lane: core/scripts/monitor-stale-check.py
    drives `aspirations.py update-goal <id> status completed` as a direct
    subprocess, bypassing the daemon-only wrapper. Note that driver's value --
    `completed`. Until g-306-157 this test drove `skipped`/`decomposed`, which no
    live caller of this door uses, so it was exercising a shape production never
    takes (guard-920). The scope narrowing forced the alignment; it was worth
    having anyway.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _cli_world(Path(tmpd))
        proc = _cli_update(world, sid=ACTOR_SID, terminal=terminal)
        assert proc.returncode == 0, (
            f"CLI update-goal rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}")
        g = _find_goal(world, "g-101-01")
        assert g is not None and g.get("status") == terminal
        assert "claimed_by_sid" not in g, "the CLI door must clear the claim sid"
        assert g.get("completed_by_sid") == ACTOR_SID, (
            "the CLI door must stamp THIS process's MIND_SID in preference to "
            f"the claim's sid ({HOLDER_SID}); got {g.get('completed_by_sid')!r}")


def test_cli_door_falls_back_to_claim_sid_when_env_unset():
    """MIND_SID unset (un-hooked launch) -> the claim's sid is preserved.

    The fallback arm of the same expression. Without it the stamp would simply
    be absent on every un-hooked CLI close while the code still looked whole --
    and absent is indistinguishable from "this door was never wired".
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _cli_world(Path(tmpd))
        proc = _cli_update(world, sid=None, terminal="completed")
        assert proc.returncode == 0, (
            f"CLI update-goal rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}")
        g = _find_goal(world, "g-101-01")
        assert g is not None and g.get("status") == "completed"
        assert "claimed_by_sid" not in g, "the CLI door must clear the claim sid"
        assert g.get("completed_by_sid") == HOLDER_SID, (
            "with no MIND_SID the claim's sid must be preserved; "
            f"got {g.get('completed_by_sid')!r}")


# ---------------------------------------------------------------------------
# : the two semantics the 12 tests above could not discriminate.
#
# The shipped stamp keyed off the whole terminal set and assigned
# unconditionally; `completed_by`, the field it was modelled on, is scoped to
# `completed` and assigns only when unset. EVERY test above passes under BOTH
# readings -- each drives exactly one close, on `completed`, of a goal with no
# prior stamp -- which is why the divergence survived review. These two are the
# discriminators.
# ---------------------------------------------------------------------------


def test_recompletion_keeps_the_first_completion_triple():
    """completed -> reopened -> re-completed keeps completion #1 in all three.

    Reopening is not hypothetical: stranded-claim-sweep.py --apply flips a
    stranded goal back to `pending` BY DESIGN, so a goal can be taken terminal
    twice by different bodies. Under the pre-fix unconditional stamp the record
    ended up carrying completion #1's agent (completed_by is idempotent) beside
    completion #2's session -- a pair that never co-occurred, presented as fact
    to anyone joining the two fields.

    The agent is held constant across both closes on purpose: DaemonFixture
    binds one agent, and varying the sid alone is what discriminates the two
    readings. `completed_at` is asserted alongside because it is the third
    member of the triple and pins that all three describe ONE event.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed",
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"first close status={status}; body={out!r}"
            first = _find_goal(world, "g-100-01")
            assert first is not None and first.get("completed_by_sid") == ACTOR_SID, (
                f"first close must stamp; got {first}")
            first_at = first.get("completed_at")
            first_by = first.get("completed_by")

            status, out = _update_goal(df.port, "g-100-01", "status", "pending",
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"reopen status={status}; body={out!r}"

            status, out = _update_goal(df.port, "g-100-01", "status", "completed",
                                       "delta", sid=THIRD_SID)
            assert status == 200, f"second close status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed"
            assert g.get("completed_by_sid") == ACTOR_SID, (
                "the FIRST completion's session must survive a re-close, exactly "
                "as completed_by does -- otherwise the pair reports an "
                f"agent/session combination that never co-occurred; got "
                f"{g.get('completed_by_sid')!r} (THIRD_SID means the stamp "
                "overwrote; the pre-fix behaviour)")
            assert g.get("completed_by") == first_by, "completed_by must not move"
            assert g.get("completed_at") == first_at, "completed_at must not move"


@pytest.mark.parametrize("terminal", ["skipped", "expired", "decomposed"])
def test_non_completed_terminal_does_not_stamp(terminal):
    """A non-completed terminal transition stamps NEITHER half of the pair.

    Pre-fix, the stamp keyed off _TERMINAL_GOAL_STATUSES while `completed_by`
    keyed off `completed`, so a skipped goal received a field whose name says
    completed with no completed_by beside it -- 4 such rows existed live within
    a day of the stamp shipping. Parametrised over the reachable members of the
    set so this pins the PROPERTY rather than one instance (guard-1726);
    `superseded` is excluded because a separate gate refuses it via update-goal.

    The claim triple must STILL clear here -- narrowing the stamp must not
    narrow the pop, which would strand a claim on a skipped goal (guard-151).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", terminal,
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == terminal, f"resp={out!r}"
            assert not g.get("completed_by_sid"), (
                f"{terminal} is not a completion -- it must not stamp a field "
                f"named completed_by_sid; got {g.get('completed_by_sid')!r}")
            assert not g.get("completed_by"), (
                f"{terminal} must not stamp completed_by either -- the two "
                "fields are only useful as a joinable pair; got "
                f"{g.get('completed_by')!r}")
            assert "claimed_by_sid" not in g, (
                "narrowing the STAMP must not narrow the POP -- the claim triple "
                "clears at every terminal transition")
            assert "claimed_by" not in g and "claimed_at" not in g


def test_cli_door_does_not_stamp_on_non_completed_terminal():
    """The CLI half of the scope narrowing.

    Both doors carry the same two-part condition, and a fix wired into one door
    is inert on the other (guard-742 / guard-2323). The daemon negative arm is
    test_non_completed_terminal_does_not_stamp; without this one, sabotaging the
    CLI scope alone leaves the whole suite green -- the SURVIVED shape the
    g-306-159 section above documents for this exact door.

    `skipped` needs no --override-uncommitted: the uncommitted-work gate is
    keyed on `completed` alone.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _cli_world(Path(tmpd))
        proc = _cli_update(world, sid=ACTOR_SID, terminal="skipped")
        assert proc.returncode == 0, (
            f"CLI update-goal rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}")
        g = _find_goal(world, "g-101-01")
        assert g is not None and g.get("status") == "skipped"
        assert not g.get("completed_by_sid"), (
            "skipped is not a completion -- the CLI door must not stamp; "
            f"got {g.get('completed_by_sid')!r}")
        assert "claimed_by_sid" not in g, (
            "narrowing the stamp must not narrow the pop")


def test_cli_door_recompletion_keeps_the_first_sid():
    """The CLI half of the idempotency fix.

    Same door-parity argument as above: the daemon arm is
    test_recompletion_keeps_the_first_completion_triple, and without this one a
    CLI-only overwrite regression is invisible.

    Three subprocess closes rather than one, so it is the slowest test here;
    that is the price of covering the door behaviourally instead of by grep.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _cli_world(Path(tmpd))
        assert _cli_update(world, sid=ACTOR_SID, terminal="completed").returncode == 0
        first = _find_goal(world, "g-101-01")
        assert first is not None and first.get("completed_by_sid") == ACTOR_SID
        assert _cli_update(world, sid=ACTOR_SID, terminal="pending").returncode == 0
        proc = _cli_update(world, sid=THIRD_SID, terminal="completed")
        assert proc.returncode == 0, (
            f"re-close rc={proc.returncode}\nstderr={proc.stderr[-2000:]}")
        g = _find_goal(world, "g-101-01")
        assert g is not None and g.get("status") == "completed"
        assert g.get("completed_by_sid") == ACTOR_SID, (
            "the FIRST completion's session must survive a re-close on the CLI "
            f"door too; got {g.get('completed_by_sid')!r}")
        assert g.get("completed_at") == first.get("completed_at"), (
            "completed_at must not move either -- all three are one triple")


# ── : the pair must land TOGETHER, or not at all ────────────────────
# Everything above pins each half's own first-wins behaviour, and each half was
# correct on its own terms. The defect lives BETWEEN them: two independent
# guards on one conceptual pair, so a write can fill the empty half while
# first-wins preserves the other -- producing a name and a session that never
# co-occurred. Measured 2026-08-09 over the full store (8686 goals): 4239
# completed goals carry completed_by with NO completed_by_sid and ZERO carry the
# sid without the name, so that 4239-row reservoir is exactly the population
# where the next `update-goal status=completed` would stamp a FOREIGN sid. 6 of
# 14 distinct completion-SIDs already carry more than one completed_by value.
#
# Absent is the correct outcome on that backlog, not a regression:
# `_completed_by_sid` states the principle verbatim -- an absent sid beats a
# wrong one.

def _preclaimed_world(root: Path, *, cli: bool) -> Path:
    """A world whose goal ALREADY carries completed_by and no sid.

    This is the state 4239 live rows are in. `status` stays non-terminal so the
    close is a real transition; `completed_by` is a DIFFERENT agent from the one
    closing, which is what makes a foreign stamp visible rather than plausible.
    """
    world = root / "world"
    world.mkdir(parents=True, exist_ok=True)
    gid = "g-101-01" if cli else "g-100-01"
    asp = {
        "id": "asp-101" if cli else "asp-100",
        "title": "completed_by already set, sid still free",
        "status": "active", "priority": "LOW",
        "goals": [_goal(gid, claimed_by="delta", claimed_at="2026-08-03T10:00:00",
                        claimed_by_sid=HOLDER_SID, status="in-progress",
                        completed_by="zeta")],
        "progress": {"completed_goals": 0, "total_goals": 1},
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    return world


def test_daemon_door_leaves_sid_absent_when_the_name_is_already_set():
    """Daemon door: name already claimed -> the sid stays ABSENT, not foreign.

    Reverting the guard makes this fail with completed_by_sid == ACTOR_SID
    beside completed_by == "zeta" -- a pair that never occurred, and one no
    downstream join can detect, because both values are individually legitimate.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _preclaimed_world(Path(tmpd), cli=False)
        with DaemonFixture(world, agent="delta") as df:
            status, out = _update_goal(df.port, "g-100-01", "status", "completed",
                                       "delta", sid=ACTOR_SID)
            assert status == 200, f"update-goal status={status}; body={out!r}"
            g = _find_goal(world, "g-100-01")
            assert g is not None and g.get("status") == "completed", f"resp={out!r}"
            assert g.get("completed_by") == "zeta", (
                "first-wins on the NAME is unchanged by this fix; "
                f"got {g.get('completed_by')!r}")
            assert "completed_by_sid" not in g, (
                "the sid must not be stamped by a write that did not also stamp "
                f"the name; got {g.get('completed_by_sid')!r} beside "
                f"completed_by={g.get('completed_by')!r}")
            assert "claimed_by_sid" not in g, (
                "narrowing the stamp must not narrow the pop (guard-151)")


def test_cli_door_leaves_sid_absent_when_the_name_is_already_set():
    """CLI half of the same invariant.

    Carried on its own rather than assumed from the daemon arm: the two doors
    are separate implementations that only a parity test ties together, and
    every other behaviour in this file is pinned on both sides for that reason.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _preclaimed_world(Path(tmpd), cli=True)
        proc = _cli_update(world, sid=ACTOR_SID, terminal="completed")
        assert proc.returncode == 0, (
            f"CLI update-goal rc={proc.returncode}\nstderr={proc.stderr[-2000:]}")
        g = _find_goal(world, "g-101-01")
        assert g is not None and g.get("status") == "completed"
        assert g.get("completed_by") == "zeta", (
            f"first-wins on the NAME is unchanged; got {g.get('completed_by')!r}")
        assert "completed_by_sid" not in g, (
            "the CLI door must not stamp a sid on a write that did not also "
            f"stamp the name; got {g.get('completed_by_sid')!r}")
        assert "claimed_by_sid" not in g, (
            "narrowing the stamp must not narrow the pop (guard-151)")


if __name__ == "__main__":
    test_request_sid_is_preferred_over_claim_sid()
    test_falls_back_to_claim_sid_when_request_carries_none()
    test_no_sid_anywhere_writes_no_field()
    test_claim_sid_is_popped_but_completed_by_sid_survives()
    test_complete_by_door_also_stamps()
    test_release_does_not_stamp_completed_by_sid()
    test_daemon_helper_never_reads_env_sid()
    test_cli_daemon_completed_by_sid_parity()
    test_update_goal_wrapper_sends_sid()
    test_recurring_complete_by_arm_also_stamps()
    test_cli_door_stamps_and_prefers_env_over_claim_sid("completed")
    test_cli_door_falls_back_to_claim_sid_when_env_unset()
    test_recompletion_keeps_the_first_completion_triple()
    for _t in ("skipped", "expired", "decomposed"):
        test_non_completed_terminal_does_not_stamp(_t)
    test_cli_door_does_not_stamp_on_non_completed_terminal()
    test_cli_door_recompletion_keeps_the_first_sid()
    test_daemon_door_leaves_sid_absent_when_the_name_is_already_set()
    test_cli_door_leaves_sid_absent_when_the_name_is_already_set()
    print("ok")
