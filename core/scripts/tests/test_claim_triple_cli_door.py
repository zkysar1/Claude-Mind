"""The CLI door clears the claim TRIPLE on a terminal transition — .

A claim is (claimed_by, claimed_at, claimed_by_sid). `claimed_by_sid` was added
later (g-115-3176) and propagated to the daemon's pop sites but NOT to
`aspirations.py::cmd_update_goal` — before this fix that file contained ZERO
occurrences of the field, so a terminal transition through the CLI door left an
orphaned sid on a now-unclaimed goal.

WHY A COLD LANE STILL GETS A TEST. `aspirations-update-goal.sh` is daemon-only
(rt_call, no CLI fallback), so this door carries no production traffic today and
the DAEMON twin is the hot one — it is already paired and already covered by
test_claim_same_agent_session_exclusion.py case 9. This file is therefore a
PARITY pin, not a defect regression, and it is deliberately labelled as such
rather than dressed up as the burning path. The both-doors precedent
(test_credential_enum_both_doors.py) is what makes it worth pinning at all: that
file exists because a fix wired into exactly one of these two doors went inert,
and the doors drift apart silently because nothing compares them.

An orphaned sid is not inert. No consumer-side guard reads a sid on an
unclaimed goal, so the residue accumulates unnoticed from whichever producer is
still unpaired, and the next claimer can inherit a previous holder's session
label — making a later collision LESS diagnosable than before the field existed.

HERMETIC: drives the real argparse entry point in a tmp world via
sys.executable (never a bare "bash" argv[0] — guard-580/581), with
STORAGE_BACKEND=local pinned so the tmp write cannot collide with the
production S3 key (guard-955 / rb-2983).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = str(REPO / "core" / "scripts" / "aspirations.py")
GOAL = "g-300-01"
SID = "SID-CLI-DOOR"


def _make_world(root: Path) -> Path:
    """A tmp world holding one claimed, non-terminal goal."""
    world = root / "world"
    world.mkdir(parents=True, exist_ok=True)
    asp = {
        "id": "asp-300",
        "title": "CLI door fixture",
        "status": "active",
        "priority": "LOW",
        "goals": [{
            "id": GOAL,
            "title": "claimed goal",
            "status": "in-progress",
            "claimed_by": "zeta",
            "claimed_at": "2026-08-03T01:00:00",
            "claimed_by_sid": SID,
        }],
        "progress": {"completed_goals": 0, "total_goals": 1},
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=True) + "\n", encoding="utf-8")
    return world


def _goal(world: Path) -> dict:
    line = (world / "aspirations.jsonl").read_text(encoding="utf-8").strip()
    return json.loads(line)["goals"][0]


def _update(world: Path, field: str, value: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["STORAGE_BACKEND"] = "local"     # guard-955: never let a tmp write reach S3
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "zeta"
    # --source is a TOP-LEVEL argument and must precede the subcommand:
    #   aspirations.py [--source {world,agent}] update-goal <id> <field> <value>
    # Placing it after the subcommand exits rc=2 "unrecognized arguments".
    return subprocess.run(
        [sys.executable, SCRIPT, "--source", "world",
         "update-goal", GOAL, field, value],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.mark.parametrize("terminal", ["skipped", "expired"])
def test_cli_terminal_transition_clears_the_whole_triple(terminal):
    """A terminal transition through the CLI door must pop all three, not two.

    WHY NOT "completed": that value additionally routes through the completion
    gates (uncommitted-work, completion-artifact, ...), and the
    uncommitted-work gate reads the LIVE repo's git status rather than
    MIND_WORLD — so it refuses whenever the working tree is dirty, which is
    exactly when someone is developing this fix. Overriding it would make the
    fixture depend on a ledger write and on gate behaviour unrelated to the
    claim hook.

    Parametrising the OTHER two terminal statuses is strictly better than
    special-casing one: the hook is keyed on `value in TERMINAL_GOAL_STATUSES`,
    so covering two members proves it is set-keyed rather than
    completed-keyed — a distinction a single-value test cannot make. The
    "completed" path is covered on the (hot) daemon side by
    test_claim_same_agent_session_exclusion.py case 9.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        r = _update(world, "status", terminal)

        g = _goal(world)
        assert g.get("status") == terminal, (
            f"fixture did not reach terminal status; "
            f"rc={r.returncode} stderr={r.stderr[-400:]}")
        # The pair was already correct before this fix — assert it so a
        # regression in either half is attributable.
        assert "claimed_by" not in g, "claimed_by must clear on terminal"
        assert "claimed_at" not in g, "claimed_at must clear on terminal"
        assert "claimed_by_sid" not in g, (
            "claimed_by_sid outlived its claim on the CLI door — a stamp that "
            "outlives the claim it names is worse than no stamp, because the "
            "next claimer inherits a session label that was never theirs")


def test_cli_non_terminal_update_preserves_the_triple():
    """A non-terminal field update must not disturb a LIVE claim.

    The clearing hook is gated on `field == "status" and value in
    TERMINAL_GOAL_STATUSES`. Without this case, a fix that popped the sid
    unconditionally would pass the test above while silently destroying live
    claims on every unrelated field write.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _make_world(Path(tmpd))
        r = _update(world, "priority", "HIGH")

        g = _goal(world)
        assert g.get("priority") == "HIGH", (
            f"fixture update did not land; rc={r.returncode} "
            f"stderr={r.stderr[-400:]}")
        assert g.get("claimed_by") == "zeta"
        assert g.get("claimed_at") == "2026-08-03T01:00:00"
        assert g.get("claimed_by_sid") == SID, (
            "a non-terminal field update must leave the live claim intact")
