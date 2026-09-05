#!/usr/bin/env python3
"""test_abandoned_claim.py — pins the abandoned-claim predicate ().

The four checks below are the goal's own verification criteria, in order. Two of
them are guarding against a WRONG DETECTOR rather than a missing one, which is
the point: this lane's failure mode is a false positive that releases a live
partner's claim, so `test_live_body_row_is_not_abandoned` and
`test_young_claim_is_reported_but_not_releasable` matter more than the happy path.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _abandoned_claim import (  # noqa: E402
    find_abandoned,
    held_goal_ids,
)

NOW = datetime(2026, 9, 4, 23, 0, 0)


def _goal(gid, *, status="in-progress", claimed_by="alpha", age_minutes=600):
    claimed_at = (NOW - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
    return {
        "id": gid,
        "title": f"title for {gid}",
        "status": status,
        "claimed_by": claimed_by,
        "claimed_at": claimed_at,
        "aspiration_id": "asp-369",
    }


def _team_state(**agents):
    return {"agent_status": agents}


# ---- check 1: abandoned claim is reported, with the population ----------------

def test_abandoned_claim_is_reported_with_population():
    report = find_abandoned(
        [_goal("g-369-08"), _goal("g-1", status="pending"), {"id": "g-2"}],
        _team_state(alpha={"in_flight": None, "in_flight_bodies": {}}),
        NOW,
    )
    assert report["abandoned_count"] == 1
    assert report["abandoned"][0]["goal_id"] == "g-369-08"
    assert report["abandoned"][0]["releasable"] is True
    # The population must travel with the count — a bare "1" cannot be told
    # apart from a scan that read almost nothing (guard-3830).
    assert report["scanned_goals"] == 3
    assert report["claimed_in_progress"] == 1


# ---- check 2: a live body row means NOT abandoned -----------------------------

def test_live_body_row_is_not_abandoned():
    """The in_flight_bodies half. Reading only `in_flight` opens the detector
    completely (g-306-276) — a worker Body is invisible in `in_flight`."""
    report = find_abandoned(
        [_goal("g-369-08")],
        _team_state(
            alpha={
                "in_flight": None,  # reducer row empty — the trap
                "in_flight_bodies": {"sid-abc123": {"goal_id": "g-369-08"}},
            }
        ),
        NOW,
    )
    assert report["abandoned_count"] == 0, (
        "a goal held by a live WORKER Body was reported abandoned — the detector "
        "is reading only the reducer-owned in_flight shape"
    )
    assert report["in_flight_rows"] == 1


def test_legacy_in_flight_shape_also_counts_as_held():
    report = find_abandoned(
        [_goal("g-326-85")],
        _team_state(foxtrot={"in_flight": {"goal_id": "g-326-85"}}),
        NOW,
    )
    assert report["abandoned_count"] == 0


def test_held_goal_ids_names_the_holder():
    held = held_goal_ids(
        _team_state(
            alpha={"in_flight_bodies": {"sid-deadbeef00": {"goal_id": "g-1"}}},
            zeta={"in_flight": {"goal_id": "g-2"}},
        )
    )
    assert "alpha:body:sid-dead" in held["g-1"]
    assert held["g-2"] == ["zeta:in_flight"]


# ---- check 3: young claim reported but NOT released ---------------------------

def test_young_claim_is_reported_but_not_releasable():
    """The claim-write -> first-row-write race. A Body that has claimed but not
    yet written its in_flight row holds a REAL claim; reaping it is the failure
    this threshold exists to prevent."""
    report = find_abandoned(
        [_goal("g-fresh", age_minutes=5)],
        _team_state(alpha={"in_flight_bodies": {}}),
        NOW,
        threshold_minutes=180,
    )
    assert report["abandoned_count"] == 1, "a young claim must still be REPORTED"
    assert report["releasable_count"] == 0, "a young claim must NOT be releasable"
    assert "younger than threshold" in report["abandoned"][0]["hold_reasons"][0]


def test_unparseable_claimed_at_blocks_release():
    goal = _goal("g-bad")
    goal["claimed_at"] = "not-a-timestamp"
    goal.pop("started", None)
    report = find_abandoned([goal], _team_state(alpha={}), NOW)
    assert report["abandoned_count"] == 1
    assert report["releasable_count"] == 0


def test_non_authoritative_read_releases_nothing():
    """guard-980: the local tree is a read-through cache. A mirror read showing
    zero in-flight rows makes every claim look abandoned, so an unauthoritative
    read must report and release NOTHING."""
    report = find_abandoned(
        [_goal("g-369-08")],
        _team_state(alpha={}),
        NOW,
        authoritative=False,
    )
    assert report["abandoned_count"] == 1
    assert report["releasable_count"] == 0
    assert any("NOT authoritative" in r for r in report["abandoned"][0]["hold_reasons"])


def test_unclaimed_and_terminal_goals_are_ignored():
    report = find_abandoned(
        [
            _goal("g-unclaimed", claimed_by=None),
            _goal("g-done", status="completed"),
        ],
        _team_state(alpha={}),
        NOW,
    )
    assert report["abandoned_count"] == 0
    assert report["claimed_in_progress"] == 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(__import__("pytest").main([__file__, "-q"]))


# ---- check 5: the WRAPPER's production arg shape -----------------------------
# The four tests above pin the pure predicate, and every one of them passed
# while the lane's remediation half was dead on arrival: `--apply` called
# `aspirations-release.sh --reason-kind progress` with no `--reason`, which that
# script REFUSES (exit 1, "the token types the reason; it does not replace it"),
# and `>/dev/null 2>&1` swallowed the message. Detection worked from day one;
# nothing ever released. That is guard-920 exactly — a regression test must
# replicate the LITERAL production arg shape, not the contract-ideal one — so
# these two tests read the wrapper and the CLI as they will actually be invoked.

import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
WRAPPER = SCRIPTS / "abandoned-claim-check.sh"
CLI = SCRIPTS / "abandoned-claim-check.py"


def test_release_invocation_passes_reason_alongside_reason_kind():
    """`--reason-kind` TYPES a reason, it does not replace one."""
    src = WRAPPER.read_text(encoding="utf-8")
    calls = [
        m for m in re.finditer(r"aspirations-release\.sh.*?(?=\n\s*(?:then|2>&1|\)))",
                               src, re.S)
    ]
    assert calls, "no aspirations-release.sh invocation found in the wrapper"
    for m in calls:
        call = m.group(0)
        if "--reason-kind" in call:
            assert re.search(r"--reason\s+[\"']", call), (
                "the wrapper passes --reason-kind with no --reason; "
                "aspirations-release.sh refuses that shape and every release fails"
            )


def test_apply_is_not_gated_on_text_output_mode():
    """`--apply --json` must not be a silent no-op."""
    # Comment lines are stripped first: the wrapper QUOTES the old gate in the
    # comment explaining why it was removed, and a whole-file substring check
    # would fail on the explanation rather than on the code (this test did
    # exactly that on its first run).
    code = "\n".join(
        ln for ln in WRAPPER.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "APPLY -eq 1 && $JSON -eq 0" not in code, (
        "the apply path is gated off in --json mode; the machine-readable form "
        "would produce output byte-identical to a dry run while releasing nothing"
    )


def test_releasable_ids_emitted_in_both_output_modes(tmp_path):
    """The wrapper's --apply loop reads this marker; text-only emission made the
    report (`releasable_count: 1`) and the action ("nothing met all four
    keep-safe conditions") disagree with nothing saying so."""
    goals = tmp_path / "goals.json"
    ts = tmp_path / "ts.json"
    goals.write_text(json.dumps([_goal("g-369-08")]), encoding="utf-8")
    ts.write_text(json.dumps(_team_state(alpha={})), encoding="utf-8")
    base = [sys.executable, str(CLI), "--goals", str(goals),
            "--team-state", str(ts), "--authoritative"]

    text = subprocess.run(base, capture_output=True, text=True, check=True).stdout
    js = subprocess.run(base + ["--json"], capture_output=True, text=True,
                        check=True).stdout

    assert "RELEASABLE_IDS g-369-08" in text
    assert "RELEASABLE_IDS g-369-08" in js, (
        "the marker is missing in --json mode, so the wrapper's --apply loop "
        "finds no ids and releases nothing"
    )
    # ...and stripping the marker (what the wrapper does) must leave valid JSON.
    stripped = "\n".join(
        ln for ln in js.splitlines() if not ln.startswith("RELEASABLE_IDS ")
    )
    assert json.loads(stripped)["releasable_count"] == 1
