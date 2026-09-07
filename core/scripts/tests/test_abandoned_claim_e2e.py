"""The abandoned-claim lane END TO END through the real wrapper — .

WHAT WAS UNCOVERED. `core/config/rationale/abandoned-claim-lane.md` closes with
the admission this file exists to retire:

    "abandoned-claim-check's own integration path is pinned only STATICALLY --
     checks 5-7 read the wrapper text and test 7 exercises the CLI, but nothing
     exercises wrapper -> aspirations-release.sh -> released claim end to end.
     The three known defects are covered; an unknown fourth in that path would
     not be."

That is exactly right about `test_abandoned_claim.py`. Its first nine tests
drive `find_abandoned`, a pure predicate. The three that reach further
(`test_release_invocation_passes_reason_alongside_reason_kind`,
`test_apply_is_not_gated_on_text_output_mode`,
`test_releasable_ids_emitted_in_both_output_modes`) pin the wrapper's TEXT and
the CLI's stdout — i.e. that the right argv is CONSTRUCTED and the marker is
EMITTED. None of them runs `aspirations-release.sh`, so none can observe a claim
actually leaving a record. Every LINK was pinned and the CHAIN was not, which is
the same shape as the sibling `test_agent_queue_release_e2e.py` docstring
describes, and the same shape as the three original defects: all three lived in
the wrapper, and four green predicate tests never saw them (guard-920).

WHAT THIS FILE PINS — the chain, in one run:

    abandoned-claim-check.sh --apply
      -> RELEASABLE_IDS marker
        -> aspirations-release.sh --source world --reason-kind progress --reason
          -> the WORLD record loses claimed_by / claimed_at / claimed_by_sid
            -> and both notes survive BYTE-IDENTICAL

The note assertion is not decoration. `--reason-kind progress` is the whole
reason this lane is safe to run: the motivating record carried 219,496 chars of
prior work, and a note-destroying release would make the lane worse than the bug
it fixes. That property has never been asserted anywhere; the rationale doc
states it as an observation from one live run.

THE DISCRIMINATOR (guard-1220 — a suite that cannot go RED proves nothing). A
SECOND goal sits in the same fixture queue, claimed by the same absent SID, and
differs in exactly ONE way: its claim is YOUNGER than the threshold. Keep-safe
condition 2 must spare it. Without that arm a wrapper that released
indiscriminately — or one whose RELEASABLE_IDS parsing grabbed the whole line —
would pass on the first assertion alone. The young goal is the positive control
for the threshold, and the released goal is the positive control for the chain.

ISOLATION. Same seam as `test_agent_queue_release_e2e.py`, for the same reason:
RT_DIR points the wrapper and every script it shells out to (team-state-read.sh,
aspirations-query.sh, aspirations-release.sh — all daemon-only) at the FIXTURE
daemon. Without it they resolve PROJECT_ROOT/mind_api/state/daemon.port and
drive the LIVE fleet daemon while the test reports success (guard-2484 seam 1).
The agent NAME is the bound agent per guard-2484; the GOAL IDS are synthetic and
were verified absent from every live queue before this file was written, so a
leaked release 404s on an id that exists nowhere.

WHAT THIS FILE DELIBERATELY DOES NOT REACH. The authoritative-read gate
(keep-safe condition 4) is satisfied here by a seeded fixture team-state, so
this file proves the chain RUNS when the read succeeds. It does not prove the
guard-980 read-through-cache behaviour the `--authoritative` flag exists for —
that is a property of the real own-cloud mirror and no fixture can honestly
stand in for it. `test_abandoned_claim.py::test_non_authoritative_read_releases_nothing`
covers the predicate half of that gate and remains the coverage for it. Naming
the excluded layer rather than implying the fixture covers it: guard-1462.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])
from _daemon_fixture import DaemonFixture  # noqa: E402

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
LANE = CORE_SCRIPTS / "abandoned-claim-check.sh"

AGENT = "alpha"
# Synthetic, and verified absent from every live world and agent queue before
# this file was written. These drive a wrapper that resolves its daemon from the
# environment, so a real id here could escape the fixture.
ABANDONED = "g-001-9902"
YOUNG = "g-001-9903"
ASP_ID = "asp-900"
# A SID no in-flight row of either shape names — that absence IS the class.
GHOST_SID = "dddddddd-9902-4444-8888-dddddddddddd"

# Long enough that a REPLACE-shaped write would be obvious, and both fields are
# checked because release touches neither.
OUTCOME_NOTE = "OUTCOME EVIDENCE — " + ("x" * 4000)
PROGRESS_NOTE = "PROGRESS TRACE — " + ("y" * 6000)


def _stamp(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _goal(goal_id: str, minutes_ago: int) -> dict:
    return {
        "id": goal_id,
        "title": "Abandoned-claim fixture goal",
        "description": "seeded by test_abandoned_claim_e2e",
        "status": "in-progress",
        "priority": "MEDIUM",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "participants": ["agent"],
        "outcome_note": OUTCOME_NOTE,
        "progress_note": PROGRESS_NOTE,
        "claimed_by": AGENT,
        "claimed_at": _stamp(minutes_ago),
        "claimed_by_sid": GHOST_SID,
    }


def _make_world(tmp: Path) -> Path:
    world = tmp / "world"
    world.mkdir()
    asp = {
        "id": ASP_ID, "title": "fixture aspiration",
        "motivation": "hold the fixture goals", "scope": "project",
        "priority": "MEDIUM", "status": "active",
        "created": "2026-07-01T00:00:00",
        # 240m old: past the 60m threshold the lane is invoked with below.
        # 5m old: inside it. One field apart, opposite verdicts.
        "goals": [_goal(ABANDONED, 240), _goal(YOUNG, 5)],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    # Keep-safe condition 4: the wrapper withholds --authoritative unless
    # team-state-read.sh returns non-empty, and without that flag the detector
    # releases NOTHING. A fixture with no team-state would make every assertion
    # below vacuous — the run would look clean and act on nothing.
    (world / "team-state.yaml").write_text(
        "agent_status:\n"
        f"  {AGENT}:\n"
        "    last_active: '%s'\n" % _stamp(1) +
        "    in_flight: null\n"
        "    in_flight_bodies: {}\n",
        encoding="utf-8")
    return world


def _env(df: DaemonFixture) -> dict:
    env = os.environ.copy()
    env["RT_DIR"] = str(df.runtime_dir)          # the seam that actually holds
    env["MIND_AGENT"] = AGENT
    env["MIND_SID"] = "eeeeeeee-9902-4444-8888-eeeeeeeeeeee"
    env["STORAGE_BACKEND"] = "local"             # guard-955
    env["MIND_WORLD"] = str(df.world)
    env["MIND_META"] = str(df.project_root / "meta")
    env["MIND_AGENT_DIR"] = str(df.project_root / "agents" / AGENT)
    return env


def _run_lane(env: dict, *args: str):
    # .as_posix(), never str(Path) — bash silently strips a WindowsPath's
    # backslashes (guard-581).
    p = subprocess.run(
        [BASH, LANE.as_posix(), *args],
        capture_output=True, text=True, timeout=180, env=env,
    )
    return p.returncode, p.stdout, p.stderr


def _read_goal(world: Path, goal_id: str) -> dict | None:
    with open(world / "aspirations.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for g in (json.loads(line).get("goals") or []):
                if g.get("id") == goal_id:
                    return g
    return None


def _claim_fields(g: dict) -> dict:
    return {k: g.get(k) for k in ("claimed_by", "claimed_at", "claimed_by_sid")}


def _lane_once():
    """Drive the whole chain once; return (world, stdout+stderr)."""
    tmpd = tempfile.TemporaryDirectory()
    world = _make_world(Path(tmpd.name))
    df = DaemonFixture(world, agent=AGENT)
    df.__enter__()
    rc, out, err = _run_lane(_env(df), "--apply", "--threshold-minutes", "60")
    # Contract: this lane ALWAYS exits 0 ("judge by OUTPUT"), so rc is asserted
    # as a contract check, never as the success signal.
    assert rc == 0, f"the lane's exit-0 contract broke: rc={rc}\n{out}\n{err}"
    return tmpd, df, world, out + err


def test_the_chain_releases_the_abandoned_claim():
    """wrapper -> aspirations-release.sh -> the WORLD record loses its claim."""
    tmpd, df, world, log = _lane_once()
    try:
        assert f"released {ABANDONED}" in log, (
            "the lane must report the release it performed; a silent no-op here "
            "is defect (2) from the rationale doc returning:\n" + log)
        g = _read_goal(world, ABANDONED)
        assert g is not None, "the record must SURVIVE a release, not vanish"
        assert _claim_fields(g) == {
            "claimed_by": None, "claimed_at": None, "claimed_by_sid": None
        }, f"every claim field must be cleared together: {_claim_fields(g)}"
    finally:
        df.__exit__(None, None, None)
        tmpd.cleanup()


def test_the_release_preserves_both_notes_byte_for_byte():
    """CHARACTERIZATION pin, NOT a regression detector — read this before
    "fixing" a failure here.

    The property is real and worth pinning: the motivating record carried
    219,496 chars of prior work, and a release that dropped notes would make
    this lane worse than the bug it fixes. Nothing asserted it before.

    But it does NOT discriminate on the mechanism the rationale doc credits.
    That doc says "--reason-kind progress preserves the outcome/progress
    notes", which reads as a causal claim. MEASURED here via mutation-proof
    (g-115-9037): substituting the VALID alternate kind `other` for `progress`
    leaves this test GREEN — the release succeeds and both notes survive
    byte-identical anyway. `mutation-proof-test.sh` returned
    verdict=FAIL/"VACUOUS TEST" for that pair, which is the correct verdict
    about THIS assertion's discriminating power and not about the code.
    (Substituting an INVALID kind is not a counter-example: the release is
    refused outright and the chain test above goes red instead, which is what
    the first mutation measured.)

    So what survives is: notes are preserved across this release path, kind or
    no kind. Whether `--reason-kind progress` is load-bearing somewhere else,
    or is belt-and-braces here, is NOT established by this file — do not cite
    it as evidence for that claim (rb-734: causal language needs observed
    cause->effect, and this is correlation).
    """
    tmpd, df, world, log = _lane_once()
    try:
        g = _read_goal(world, ABANDONED)
        assert g.get("outcome_note") == OUTCOME_NOTE, (
            "outcome_note was altered by the release: "
            f"{len(g.get('outcome_note') or '')} chars vs {len(OUTCOME_NOTE)}")
        assert g.get("progress_note") == PROGRESS_NOTE, (
            "progress_note was altered by the release: "
            f"{len(g.get('progress_note') or '')} chars vs {len(PROGRESS_NOTE)}")
    finally:
        df.__exit__(None, None, None)
        tmpd.cleanup()


def test_a_claim_younger_than_the_threshold_is_spared():
    """THE DISCRIMINATOR. Same queue, same absent SID, one field different.

    Keep-safe condition 2 bounds the claim-write -> first-row-write race: a Body
    that has claimed but not yet written its in-flight row holds a REAL claim
    that looks abandoned. If this arm ever goes green alongside the first test,
    the lane is releasing live claims and the first test's PASS means nothing.
    """
    tmpd, df, world, log = _lane_once()
    try:
        g = _read_goal(world, YOUNG)
        assert g is not None, "the young record must be untouched, not removed"
        assert _claim_fields(g) == {
            "claimed_by": AGENT,
            "claimed_at": g.get("claimed_at"),
            "claimed_by_sid": GHOST_SID,
        }, f"a claim inside the threshold must be SPARED: {_claim_fields(g)}"
        assert f"released {YOUNG}" not in log, (
            "the lane reported releasing a claim it must have spared:\n" + log)
    finally:
        df.__exit__(None, None, None)
        tmpd.cleanup()
