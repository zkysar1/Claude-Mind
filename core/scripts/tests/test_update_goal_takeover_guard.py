"""test_update_goal_takeover_guard.py -- cross-lane / cross-BODY takeover guard
on update-goal, BOTH entry points (g-306-230).

WHAT WAS ACTUALLY BROKEN, because it is not what the goal title says. The title
reads "extend the takeover guard to check claimed_by, not just intended_agent",
which presumes the guard existed on both sides and needed widening. Measured
2026-08-06: the DAEMON had no takeover guard AT ALL. `_routes_away_from` had
exactly two occurrences in aspirations_write.py -- its definition and ONE call,
inside claim(). update_goal() contained zero references to routes_away /
intended_agent / cross_lane.

That asymmetry IS the mechanism of the 2026-08-05 incident. aspirations-update-
goal.sh is a daemon-only wrapper (no CLI fallback since the 2026-05-14 cutover),
so every wrapper write lands on the daemon -- while the only takeover guard in
the system sat in cmd_update_goal, which the wrapper never reaches. claim()
REFUSED the goal and the very next update-goal write LANDED, because the refusal
and the write were enforced by different code and only one of them existed on
the travelled path.

It stayed that way for ~3 months behind a comment. cmd_update_goal carried
"MIRROR of the daemon guard in ... update_goal() (the `=== PR 7i in-lock status
guards ===` block)" -- naming a specific block, in a specific function, in a
specific file, that did not contain it. A reader checking whether the guard was
two-sided found an authoritative-sounding claim that it was. Hence Part A below:
the parity tests assert the guard's PRESENCE on both sides structurally, so the
claim in the comment is pinned by something that fails when it stops being true.
(guard-742 -- logic that lives on both sides needs a test that says so.)

THE SID CONDITION IS PRIMARY, not an addendum. foxtrot reported (2026-08-06
09:11) a worker Body and the reducer executing the SAME agent-queue goal
concurrently. Both Bodies are `alpha`, so every agent-name comparison is FALSE
for that collision and only `claimed_by_sid` separates them.

MISSING-SID SEMANTICS ARE ASYMMETRIC, and the asymmetry is the point:
  * STORED sid absent  -> ABSTAIN (pre-g-306-134 records legitimately have none;
    refusing them would wedge real work to close a hole)
  * REQUEST sid absent while a stored one exists -> REFUSE (the bypass vector --
    if the guard goes quiet whenever the caller omits the sid, then unsetting
    MIND_SID defeats it entirely). claim() reached this conclusion the hard way:
    its case 5b (g-306-132-b) had ALLOWED a no-sid claim, "which left the guard
    bypassable by omitting a param".
  * NEITHER side has a sid -> passes. Named as a residual rather than hidden.

SCOPE IS DELIBERATELY NARROW -- takeover only (status->in-progress, claimed_by).
The rb-428 sweeps mutate foreign-lane goals BY DESIGN (skipped / completed /
defer_reason), and a blanket cross-lane refusal breaks every one of them (the
g-115-744 over-fix trap). Part B pins that those still pass.

Run: py -3 -m pytest core/scripts/tests/test_update_goal_takeover_guard.py -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
CLI_PATH = CORE_SCRIPTS / "aspirations.py"
DAEMON_PATH = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"

# The four condition names the guard is built from. Both sides must carry all
# four -- that is what "enforce identically" means operationally.
CONDITIONS = ("_sid_conflict", "_sid_unprovable", "_agent_conflict",
              "_lane_conflict")

MY_SID = "11111111-1111-1111-1111-111111111111"
OTHER_SID = "22222222-2222-2222-2222-222222222222"


def _func_span(path: Path, pattern: str) -> str:
    """Return the source text of the first top-level def matching `pattern`.

    Deliberately anchored to a top-level `def` and terminated by the NEXT
    top-level `def`. A sloppier match here is how the first measurement of this
    very defect went wrong: `def .*update_goal` matched the helper
    `_run_update_goal_gates` (which really does lack the guard) instead of
    `update_goal` itself, and briefly produced a confident wrong reading.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start = i
            break
    assert start is not None, f"no def matching {pattern!r} in {path.name}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(async )?def ", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Part A -- structural parity. These are the tests that would have caught the
# actual defect on the day it shipped.
# ---------------------------------------------------------------------------

def test_daemon_update_goal_carries_the_takeover_guard():
    """THE regression pin. This is what was missing for ~3 months.

    Scoped to update_goal() specifically -- NOT to the file. A file-wide grep
    passes on the broken state, because claim() has always had its own guard.
    The file-wide form is the check a reader would naturally reach for, and it
    is exactly the check that cannot see this bug.
    """
    body = _func_span(DAEMON_PATH, r"^(async )?def update_goal\(")
    missing = [c for c in CONDITIONS if c not in body]
    assert not missing, (
        f"daemon update_goal() is missing takeover conditions {missing}. "
        f"aspirations-update-goal.sh is daemon-only, so this function IS the "
        f"live path -- a guard absent here is a guard that does not exist in "
        f"production, however complete the CLI side looks."
    )


def test_cli_cmd_update_goal_carries_the_takeover_guard():
    body = _func_span(CLI_PATH, r"^def cmd_update_goal\(")
    missing = [c for c in CONDITIONS if c not in body]
    assert not missing, f"CLI cmd_update_goal is missing {missing}"


def test_both_entry_points_enforce_the_same_conditions():
    """guard-742 parity. Either side drifting alone is the whole failure mode."""
    daemon = _func_span(DAEMON_PATH, r"^(async )?def update_goal\(")
    cli = _func_span(CLI_PATH, r"^def cmd_update_goal\(")
    d_has = {c for c in CONDITIONS if c in daemon}
    c_has = {c for c in CONDITIONS if c in cli}
    assert d_has == c_has, (
        f"entry points diverged: daemon={sorted(d_has)} cli={sorted(c_has)}. "
        f"Keep both in sync or the guard is half-applied."
    )


def test_guard_is_not_reachable_only_via_claim():
    """Negative control for the parity tests above.

    Without this, the three tests above would still pass if someone deleted the
    update_goal guard and the CONDITIONS happened to appear in claim(). Pins the
    precise shape of the original defect: routing enforcement present in claim()
    and absent from update_goal().
    """
    claim = _func_span(DAEMON_PATH, r"^(async )?def claim\(")
    update = _func_span(DAEMON_PATH, r"^(async )?def update_goal\(")
    assert "_routes_away_from" in claim, (
        "claim() lost its routing check -- unrelated to this guard, but the "
        "premise of this test no longer holds; investigate before editing."
    )
    assert "_routes_away_from" in update, (
        "update_goal() does not call _routes_away_from. This is the EXACT "
        "2026-08-06 defect: the routing check lived only in claim(), so a "
        "write could land immediately after a claim was refused."
    )


def test_scope_stays_narrow():
    """The  over-fix trap: a blanket cross-lane refusal breaks every
    rb-428 sweep. Both sides must gate on the takeover branch specifically."""
    for path, pattern in ((DAEMON_PATH, r"^(async )?def update_goal\("),
                          (CLI_PATH, r"^def cmd_update_goal\(")):
        body = _func_span(path, pattern)
        assert 'field == "claimed_by"' in body, f"{path.name}: branch too narrow"
        assert 'value == "in-progress"' in body, (
            f"{path.name}: the takeover branch must key on status->in-progress, "
            f"not on status writes generally -- the rb-428 sweeps write "
            f"skipped / completed / defer_reason on foreign lanes BY DESIGN."
        )


# ---------------------------------------------------------------------------
# Part B -- behavioral, against the real CLI entry point (subprocess, real
# argparse, real guard). Not a transliteration: a transliterated predicate
# supplies its own expectation and would pass against a deleted guard
# (guard-1220).
# ---------------------------------------------------------------------------

def _goal(goal_id, *, claimed_by=None, claimed_by_sid=None,
          intended_agent=None, status="pending"):
    g = {
        "id": goal_id,
        "title": f"Test goal {goal_id}",
        "description": "fixture goal for the takeover guard matrix",
        "status": status,
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "framework-architecture",
        "created": "2026-08-06T00:00:00",
    }
    if claimed_by:
        g["claimed_by"] = claimed_by
        g["claimed_at"] = "2026-08-06T00:00:00"
    if claimed_by_sid:
        g["claimed_by_sid"] = claimed_by_sid
    if intended_agent:
        g["intended_agent"] = intended_agent
    return g


@pytest.fixture()
def world(tmp_path):
    """A tmp world. STORAGE_BACKEND=local is MANDATORY, not hygiene (guard-955):
    under own-cloud, OwnCloudBackend._s3_key derives its key from the customer
    prefix + env id + filename and IGNORES the MIND_WORLD tmp override, so a
    fixture write collides on the PRODUCTION S3 key. That truncated the real
    world/aspirations.jsonl on 2026-07-09."""
    wd = tmp_path / "world"
    wd.mkdir()
    asp = {
        "id": "asp-999",
        "title": "takeover guard fixtures",
        "status": "active",
        "goals": [
            _goal("g-999-01", claimed_by="alpha", claimed_by_sid=MY_SID),
            _goal("g-999-02", claimed_by="alpha", claimed_by_sid=OTHER_SID),
            _goal("g-999-03", claimed_by="bravo", claimed_by_sid=OTHER_SID),
            _goal("g-999-04", claimed_by="alpha"),          # legacy: no stored sid
            _goal("g-999-05", intended_agent="bravo"),      # lane conflict only
            _goal("g-999-06"),                              # unclaimed
            _goal("g-999-07", claimed_by="bravo", claimed_by_sid=OTHER_SID,
                  intended_agent="bravo"),                  # rb-428 sweep target
        ],
    }
    (wd / "aspirations.jsonl").write_text(
        json.dumps(asp) + "\n", encoding="utf-8")
    return wd


def _update(world, goal_id, field, value, *, agent="alpha", sid=MY_SID,
            cross_lane=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = agent
    if sid:
        env["MIND_SID"] = sid
    else:
        env.pop("MIND_SID", None)
    cmd = [sys.executable, str(CLI_PATH), "update-goal", goal_id, field, value]
    if cross_lane:
        cmd += ["--cross-lane", cross_lane]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=120)


def _refused(res):
    return res.returncode != 0 and "TAKEOVER" in (res.stderr or "")


@pytest.mark.parametrize("goal_id,agent,sid,should_refuse,why", [
    # The case that must NOT break: an agent writing on its own claim. This is
    # every worker's per-unit write; a false refusal here wedges the fleet.
    ("g-999-01", "alpha", MY_SID, False, "own claim, own session"),
    # PRIMARY (foxtrot 2026-08-06): same agent, different Body.
    ("g-999-02", "alpha", MY_SID, True, "same agent, other session"),
    # ...and the reverse direction, which the goal asks for by name.
    ("g-999-01", "alpha", OTHER_SID, True, "reverse: other session writes"),
    # Cross-agent takeover after a refused claim -- the 2026-08-05 incident.
    ("g-999-03", "alpha", MY_SID, True, "claimed by bravo, alpha writes"),
    # Legacy record: stored sid absent -> abstain, allowed.
    ("g-999-04", "alpha", MY_SID, False, "no stored sid -> abstain"),
    # Bypass vector: stored sid present, caller omits its own.
    ("g-999-01", "alpha", None, True, "caller omits sid"),
    # Lane routing, unchanged from .
    ("g-999-05", "alpha", MY_SID, True, "routed to bravo"),
    ("g-999-06", "alpha", MY_SID, False, "unclaimed goal"),
])
def test_takeover_matrix(world, goal_id, agent, sid, should_refuse, why):
    res = _update(world, goal_id, "status", "in-progress", agent=agent, sid=sid)
    if should_refuse:
        assert _refused(res), f"expected refusal ({why}); stderr={res.stderr!r}"
    else:
        assert not _refused(res), f"unexpected refusal ({why}); {res.stderr!r}"


@pytest.mark.parametrize("field,value", [
    ("status", "skipped"),
    ("status", "completed"),
    ("defer_reason", "human_blocked: waiting on review"),
])
def test_rb428_sweeps_still_write_foreign_lanes(world, field, value):
    """The over-fix trap. These sweeps mutate foreign-lane goals BY DESIGN --
    g-999-07 is claimed by bravo, sid-mismatched AND lane-routed to bravo, so it
    trips all three conditions. It must still pass, because the branch is never
    entered for these fields."""
    res = _update(world, "g-999-07", field, value)
    assert not _refused(res), (
        f"{field}={value} on a foreign lane was refused -- the takeover branch "
        f"has widened beyond status->in-progress and every rb-428 sweep is now "
        f"broken. stderr={res.stderr!r}"
    )


def test_cross_lane_override_permits_and_is_audited(world):
    res = _update(world, "g-999-03", "status", "in-progress",
                  cross_lane="g-306-230 test: deliberate override")
    assert not _refused(res), f"override did not permit the write: {res.stderr!r}"
    ledger = world / "override-bypass-ledger.jsonl"
    assert ledger.exists(), "override was permitted but nothing was logged"
    rows = [json.loads(x) for x in
            ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any("g-999-03" in json.dumps(r) for r in rows), (
        "override-bypass-ledger has no row naming the overridden goal")


def test_refusal_names_the_holder_and_the_claim_time(world):
    """A refusal that does not say WHO holds the goal sends the reader to the
    wrong place. The 2026-08-05 incident cost hours partly because the refusal
    and the successful write were attributed to different mechanisms."""
    res = _update(world, "g-999-03", "status", "in-progress")
    assert _refused(res)
    assert "bravo" in res.stderr, "refusal does not name the holder"
    assert "2026-08-06" in res.stderr, "refusal does not name the claim time"


def test_refusal_does_not_misname_a_cross_agent_takeover_as_two_bodies(world):
    """Reason ORDER is not check order, deliberately.

    On a cross-agent takeover both the agent and the sid differ. Naming the sid
    there would print "two Bodies of 'alpha'" about a goal held by bravo -- a
    diagnosis that is not merely unhelpful but points at a mechanism that is not
    occurring. Caught by the truth table before this shipped."""
    res = _update(world, "g-999-03", "status", "in-progress")
    assert _refused(res)
    assert "two Bodies" not in res.stderr, (
        f"cross-agent takeover was reported as a two-Body collision: "
        f"{res.stderr!r}")


# ---------------------------------------------------------------------------
# : the override's TRAVELLED path.
#
# Every test above drives CLI_PATH (aspirations.py) as a subprocess, so
# test_cross_lane_override_permits_and_is_audited is green against the defect
# below and always was. But aspirations-update-goal.sh is daemon-only (no CLI
# fallback since the 2026-05-14 cutover), so every real `--cross-lane` write
# lands on the DAEMON -- and the wrapper shipped it as an `X-Mind-Cross-Lane`
# HEADER while update_goal() reads `ctx.query.get("cross_lane")`. The flag was
# therefore inert on the only path production takes.
#
# This is the same shape as the guard asymmetry the module docstring opens with:
# a mechanism that exists, is tested, and is not on the road. The sibling
# aspirations-claim.sh had it right (it appends `&cross_lane=`), which is what
# makes the wrapper the defective half rather than the daemon.
#
# Severity is set by WHAT the override unlocks: update_goal's own refusal text
# says "Pass cross_lane=<justification> to override". So the wrapper flag for
# following the daemon's own instruction did nothing, and the documented escape
# hatch from a takeover refusal was unreachable for every wrapper caller.
#
# These tests drive the WRAPPER against a fixture daemon (guard-920 -- the
# literal production call shape). A text grep for "&cross_lane=" would be the
# shape assertion the  section of the sibling completed_by_sid suite
# calls attribution noise: it cannot tell a working param from a renamed one.
# ---------------------------------------------------------------------------

sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402

UPDATE_WRAPPER = CORE_SCRIPTS / "aspirations-update-goal.sh"


def _wrapper_world(root: Path) -> Path:
    """A tmp world holding one goal claimed by ANOTHER agent.

    bravo holds it, the caller is alpha, so `_agent_conflict` trips and the
    takeover guard refuses unless the override reaches the daemon. The archive
    file is seeded because the endpoints expect it to exist.
    """
    world = root / "world"
    world.mkdir(parents=True, exist_ok=True)
    asp = {
        "id": "asp-998",
        "title": "cross-lane wrapper-path fixtures",
        "motivation": "g-306-253 regression",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-08-06T00:00:00",
        "goals": [_goal("g-998-01", claimed_by="bravo",
                        claimed_by_sid=OTHER_SID)],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _wrapper_update(df, world, goal_id, field, value, *, cross_lane=None):
    """Drive aspirations-update-goal.sh itself -- the daemon-only production door.

    RT_DIR points the wrapper's rt_call at the fixture daemon instead of the live
    one. STORAGE_BACKEND=local is mandatory, not hygiene (guard-955): under
    own-cloud the S3 key ignores the tmp world override and a fixture write
    lands on the PRODUCTION key.
    """
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "alpha"
    env["MIND_SID"] = MY_SID
    env["RT_DIR"] = str(df.runtime_dir)
    cmd = [BASH, str(UPDATE_WRAPPER), goal_id, field, value]
    if cross_lane:
        cmd += ["--cross-lane", cross_lane]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=120)


def _goal_in(world: Path, goal_id: str):
    for line in (world / "aspirations.jsonl").read_text(
            encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == goal_id:
                return g
    return None


def test_wrapper_without_cross_lane_is_still_refused():
    """NEGATIVE CONTROL -- and it is what makes the next test mean anything.

    Without this, a mutation that disabled the takeover guard entirely would
    turn the positive test green, and it would read as "the override works".
    This pins that the fixture genuinely trips the guard, so a permitted write
    there can only come from the override being consumed (guard-1220: prove it
    fails against the defect it claims to catch, in both directions).
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _wrapper_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            res = _wrapper_update(df, world, "g-998-01", "status", "in-progress")
            assert res.returncode != 0, (
                "a bare wrapper takeover of bravo's goal must be REFUSED; "
                f"rc={res.returncode} stdout={res.stdout!r}")
            combined = (res.stderr or "") + (res.stdout or "")
            assert "takeover_refused" in combined or "TAKEOVER" in combined, (
                f"refused for the wrong reason: {combined!r}")


def test_wrapper_cross_lane_flag_reaches_the_daemon():
    """THE REGRESSION. `--cross-lane` on the wrapper must permit the takeover.

    RED before the fix: the wrapper sent only `X-Mind-Cross-Lane`, update_goal
    reads ctx.query, so `_xl` was None and the write was refused exactly as if
    the flag had never been passed.

    The ledger assertion is not decoration -- it is the second half of the
    proof. A permitted write alone is also what you would see if the guard had
    stopped firing; a ledger row naming this goal can only be written from
    inside the override branch, so it pins that the override was CONSUMED
    rather than merely not needed.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _wrapper_world(Path(tmpd))
        with DaemonFixture(world, agent="alpha") as df:
            res = _wrapper_update(df, world, "g-998-01", "status", "in-progress",
                                  cross_lane="g-306-253 test: deliberate override")
            assert res.returncode == 0, (
                "--cross-lane must reach the daemon and permit the takeover -- "
                "the wrapper is the ONLY production path to update-goal, so a "
                "refusal here means the flag is inert for every real caller. "
                f"rc={res.returncode} stderr={res.stderr!r}")
            g = _goal_in(world, "g-998-01")
            assert g is not None and g.get("status") == "in-progress", (
                f"override was accepted but the write did not land; goal={g!r}")
            ledger = world / "override-bypass-ledger.jsonl"
            assert ledger.exists(), (
                "the override permitted the write but logged nothing -- an "
                "unaudited bypass is exactly what the ledger exists to prevent")
            rows = [json.loads(x) for x in
                    ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
            assert any("g-998-01" in json.dumps(r) for r in rows), (
                "override-bypass-ledger has no row naming the overridden goal")
