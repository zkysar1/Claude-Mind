"""test_residual_gate_cross_queue_source.py — regression for .

THE DEFECT. `cmd_update_goal` (and its daemon twin) built the residual-work
gate's inputs as:

    items       = the queue being WRITTEN (the --source target)
    agent_items = read UNCONDITIONALLY from AGENT_DIR/aspirations.jsonl

`gates.residual_work` then scans `(items, agent_items)`. With `--source world`
that pair is {world, agent} and every carrier is visible. With `--source
agent` BOTH entries are the AGENT queue and THE WORLD QUEUE IS NEVER LOADED —
so closing an agent-queue goal that cites a live WORLD carrier could never
satisfy the gate. Measured on g-001-80: three world-queue carriers, all
`pending` at that moment, all reported `live:false status:null`, and the gate
then auto-filed a duplicate successor for work g-115-6252 already owned. The
false negative does not merely annoy — it POLLUTES the queue.

WHY THIS TEST LIVES AT THE CALLER AND NOT IN core/tests/gates/. The gate
itself was never wrong: it faithfully scans the two lists it is handed, and
every gate-level test passes both before and after the fix because they pass
two genuinely different queues by hand. The defect is entirely in what the
CALLER puts in the second slot, so a gate-level pin is structurally incapable
of catching it. This test drives the real CLI end-to-end with a hermetic
world + agent queue and asserts on the EXIT CODE — the thing an operator
actually experiences.

POSITIVE CONTROL IS MANDATORY HERE (guard-2421). "Close accepted" is also what
a gate that never blocks anything would produce, so an accept-only assertion
would pass against a completely disabled gate. `test_unknown_carrier_still_
blocks` is the control: same shape, carrier id that exists in NEITHER queue,
must still refuse. Do not delete it to make a future refactor green.

Hermetic: MIND_WORLD + MIND_AGENT_DIR point at tmp dirs, STORAGE_BACKEND is
pinned to `local` (guard-955 — an own-cloud box derives the S3 key from
customer_prefix+env_id+filename, NOT from the tmp path, so an unpinned
subprocess write here would collide with the PRODUCTION store).
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
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
ASP_PY = CORE_SCRIPTS / "aspirations.py"

CARRIER_NOTE = ("Analysis complete. No product code was written this pass; "
                "residual carried by {carrier}.")


def _asp(asp_id, *goals):
    return {"id": asp_id, "title": f"t {asp_id}", "status": "active",
            "goals": list(goals)}


def _goal(gid, status="pending", **over):
    return {"id": gid, "title": f"t {gid}", "description": "",
            "status": status, "priority": "MEDIUM", **over}


def _write(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _run_close(tmp: Path, goal_id: str, source: str):
    """Drive the REAL CLI close path. Returns CompletedProcess."""
    env = dict(os.environ)
    env.update({
        "STORAGE_BACKEND": "local",          # guard-955 — mandatory
        "MIND_WORLD": str(tmp / "world"),
        "MIND_META": str(tmp / "meta"),
        "MIND_AGENT": "alpha",
        "MIND_AGENT_DIR": str(tmp / "agent"),
    })
    # --override-uncommitted is REQUIRED and is not a workaround: the close
    # path also runs gates.uncommitted_work, which inspects the REAL repo
    # working tree rather than this test's tmp world. Without the override
    # these cases pass or fail according to whether the repo happens to be
    # dirty — i.e. they would go red for every agent running them mid-change,
    # including the one landing this fix. Scoped to that one gate so the
    # residual gate under test is still exercised for real; its ledger write
    # lands in the tmp world_dir, so nothing production is touched.
    return subprocess.run(
        [sys.executable, str(ASP_PY), "--source", source, "update-goal",
         goal_id, "status", "completed",
         "--override-uncommitted",
         "hermetic residual-gate test; repo dirtiness is unrelated"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def _seed(tmp: Path, *, world_goals, agent_goals):
    (tmp / "meta").mkdir(parents=True, exist_ok=True)
    _write(tmp / "world" / "aspirations.jsonl", [_asp("asp-115", *world_goals)])
    _write(tmp / "agent" / "aspirations.jsonl", [_asp("asp-001", *agent_goals)])


def _assert_gate_blocked(res, why: str):
    """Assert a RESIDUAL-GATE refusal, not merely a non-zero exit.

    `returncode != 0` is satisfied by an argparse error, a missing file, or a
    traceback — none of which exercise the gate. Measured while writing this
    file: the first run passed both control assertions on rc=2 from
    `unrecognized arguments: --source`, having never reached the gate at all.
    A control that cannot distinguish "refused" from "never ran" is not a
    control (guard-1641)."""
    blob = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 1, (
        f"{why}\nexpected the gate's exit 1, got {res.returncode} — a "
        f"different failure mode, so the gate was never reached.\n"
        f"output:\n{blob[-1500:]}")
    assert "residual" in blob.lower(), (
        f"{why}\nexit 1 but no residual-gate text in the output, so "
        f"something ELSE refused this close.\noutput:\n{blob[-1500:]}")


def _status_of(path: Path, gid: str):
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == gid:
                return g.get("status")
    return None


# --- THE REGRESSION: agent-queue close citing a WORLD-queue carrier --------

def test_agent_source_close_sees_world_queue_carrier():
    """The  case. RED before the fix: the world queue was never
    loaded on a --source agent close, so the live carrier read as dead and
    the gate refused."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(
            tmp,
            world_goals=[_goal("g-115-01", "pending")],       # the carrier
            agent_goals=[_goal("g-001-01", "pending",
                               outcome_note=CARRIER_NOTE.format(
                                   carrier="g-115-01"))],
        )
        res = _run_close(tmp, "g-001-01", "agent")
        assert res.returncode == 0, (
            "close refused despite a LIVE world-queue carrier — the world "
            f"queue was not loaded.\nstderr:\n{res.stderr[-2000:]}")
        assert _status_of(tmp / "agent" / "aspirations.jsonl",
                          "g-001-01") == "completed"


# --- POSITIVE CONTROL: the gate can still refuse --------------------------

def test_unknown_carrier_still_blocks():
    """Without this, an accept-everything regression passes the test above.
    Same shape, carrier present in NEITHER queue — must still refuse."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(
            tmp,
            world_goals=[_goal("g-115-01", "pending")],
            agent_goals=[_goal("g-001-01", "pending",
                               outcome_note=CARRIER_NOTE.format(
                                   carrier="g-999-99"))],
        )
        res = _run_close(tmp, "g-001-01", "agent")
        _assert_gate_blocked(res, (
            "gate accepted a close whose only carrier exists nowhere — the "
            "residual gate is not refusing at all, so the accept assertions "
            "in this file prove nothing."))
        assert _status_of(tmp / "agent" / "aspirations.jsonl",
                          "g-001-01") == "pending"


# --- THE INVERSE, so a fix that merely SWAPS the hardcode also fails ------

def test_world_source_close_still_sees_agent_queue_carrier():
    """The direction that already worked. Pinned because the cheapest wrong
    fix is to hardcode the WORLD queue instead of the agent one, which moves
    the blind spot rather than removing it."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(
            tmp,
            world_goals=[_goal("g-115-01", "pending",
                               outcome_note=CARRIER_NOTE.format(
                                   carrier="g-001-07"))],
            agent_goals=[_goal("g-001-07", "pending")],       # the carrier
        )
        res = _run_close(tmp, "g-115-01", "world")
        assert res.returncode == 0, (
            "close refused despite a LIVE agent-queue carrier — the inverse "
            f"direction regressed.\nstderr:\n{res.stderr[-2000:]}")
        assert _status_of(tmp / "world" / "aspirations.jsonl",
                          "g-115-01") == "completed"


# --- A DEAD carrier in the other queue must NOT lift the block ------------

def test_completed_carrier_in_other_queue_does_not_lift():
    """Loading the other queue must not degrade into 'any id that exists
    anywhere passes'. Only ACTIVE_STATUSES count."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(
            tmp,
            world_goals=[_goal("g-115-01", "completed")],     # dead carrier
            agent_goals=[_goal("g-001-01", "pending",
                               outcome_note=CARRIER_NOTE.format(
                                   carrier="g-115-01"))],
        )
        res = _run_close(tmp, "g-001-01", "agent")
        _assert_gate_blocked(res, (
            "a COMPLETED carrier lifted the block — the cross-queue load is "
            "matching on existence rather than on live status."))
