"""The owner-split entry point both callers use ().

`core/scripts/reflection-ownership-split.py` is the ONE caller-facing door for
the ownership rule — the iteration-close nudge and review-hypotheses Mode 2 both
go through it, so that the rule cannot drift between them (guard-2676: one
implementation, N callers). These tests pin the door: the two line SHAPES the
close distinguishes on, the degraded path, and the guarantee that a caller
resolving identity for itself cannot change the answer.

THE LINE LABEL IS LOAD-BEARING, NOT COSMETIC. iteration-close.sh echoes this
script's line verbatim, so the script — the only place holding the split —
decides whether the close says LLM-ACTION (work exists for THIS agent) or INFO
(only other agents' work exists). A caller re-deriving that from a bare count is
exactly the drift the goal was filed about.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "reflection-ownership-split.py"

# RELATIVE, never absolute (guard-566 / guard-4364). `ownership_of` frees a LIVE
# owner's record once resolved_at is STRANDED_HOURS (72h) old, so a fixed
# "2026-09-17T10:00:00" here was a live-owner fixture only until 2026-09-20T10:00;
# after that the identity test's held record silently became `reclaimable` and
# this file read as a CLI identity regression (, measured 2026-09-22:
# liveness {"bravo": "alive"}, reclaimable ["h-1"], held []). Mirrors FRESH in
# test_reflection_ownership.py.
FRESH = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")

# An owner that is NOT a fleet agent (guard-1699): a real agent name standing in
# for "some other agent" changes what the test tests whenever that agent's
# liveness verdict moves (alive -> held, dormant/retired -> reclaimable). An
# off-roster owner probes as `unknown`, which abstains regardless of age, so the
# identity assertion depends on the caller-supplied --agent and nothing else.
OFFROSTER_OWNER = "offroster-owner-zz9"


def _run(records, agent, extra=()):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--stdin", "--agent", agent, *extra],
        input=json.dumps(records), text=True, capture_output=True, timeout=120)
    assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr[:400]}"
    return proc.stdout.strip()


def _rec(rid, owner, outcome="CONFIRMED", resolved_at=FRESH):
    r = {"id": rid, "outcome": outcome, "resolved_at": resolved_at}
    if owner is not None:
        r["resolved_by"] = owner
    return r


def test_script_exists_and_is_the_documented_entry_point():
    """Anchors the path both callers hardcode. If this file moves, the close
    silently loses its nudge (the `||` there is non-fatal by design), so the
    move must break a test instead."""
    assert SCRIPT.is_file()
    close = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    assert "reflection-ownership-split.py" in close, (
        "iteration-close.sh no longer invokes the owner split — the nudge has "
        "regressed to an ownerless count (g-115-10004)")


def test_own_records_produce_an_llm_action_line_naming_ids():
    out = _run([_rec("h-1", "bravo"), _rec("h-2", "bravo")], "bravo", ["--nudge"])
    assert out.startswith("LLM-ACTION:"), out
    assert "h-1" in out and "h-2" in out, "own records must be named by id"
    assert "mine: 2" in out


def test_unowned_records_are_actionable():
    out = _run([_rec("h-1", None)], "bravo", ["--nudge"])
    assert out.startswith("LLM-ACTION:")
    assert "unowned: 1" in out


def test_empty_queue_is_silent():
    """No line at all — the close must not print a nudge when there is nothing
    to say in either direction."""
    assert _run([], "bravo", ["--nudge"]) == ""


def test_non_reflectable_only_queue_is_silent():
    """The outcome filter still runs first: a queue of EXPIRED/UNRESOLVABLE
    records is not reflection work and must not fire the nudge (g-115-6173)."""
    recs = [_rec("a", "bravo", outcome="EXPIRED"),
            _rec("b", "bravo", outcome="UNRESOLVABLE"),
            {"id": "c", "resolved_by": "bravo"}]
    assert _run(recs, "bravo", ["--nudge"]) == ""


def test_json_mode_reports_every_bucket_and_the_identity_it_used():
    out = json.loads(_run([_rec("h-1", "bravo"), _rec("h-2", None)], "bravo"))
    assert out["self_agent"] == "bravo"
    assert out["mine"] == ["h-1"]
    assert out["unowned"] == ["h-2"]
    assert out["actionable"] == 2
    assert out["degraded"] is False


def test_identity_is_taken_from_the_caller_not_guessed():
    """guard-2601 at the process boundary: the SAME queue must classify
    differently for two different agents. If this ever returns the same answer
    for both, the script has started resolving identity for itself and the
    predicate is answering a question nobody asked."""
    recs = [_rec("h-1", OFFROSTER_OWNER)]
    as_owner = json.loads(_run(recs, OFFROSTER_OWNER))
    assert as_owner["mine"] == ["h-1"] and as_owner["actionable"] == 1
    # As zeta the same record is another (unknown-liveness) agent's -> abstain.
    as_zeta = json.loads(_run(recs, "zeta"))
    assert as_zeta["mine"] == [] and as_zeta["held"] == ["h-1"]


def test_unknown_owner_liveness_abstains_and_is_reported_by_owner():
    """An unreadable/absent liveness verdict must ABSTAIN (guard-5623 permits
    proceeding only on dormant/retired/unknown-WITH-corroboration, and this
    script sees no corroboration), and must name WHO it is waiting on."""
    out = json.loads(_run([_rec("h-1", "nosuchagent-xyz")], "bravo"))
    assert out["held"] == ["h-1"]
    assert out["held_by"] == {"nosuchagent-xyz": 1}
    assert out["actionable"] == 0


def test_held_only_queue_emits_info_not_llm_action():
    """The reverse half of the fix. It must still SAY something — a silent
    filter makes a live owner's backlog invisible to everyone at once — but it
    must not ask the agent to act on records it may not touch."""
    out = _run([_rec("h-1", "nosuchagent-xyz")], "bravo", ["--nudge"])
    assert out.startswith("INFO:"), out
    assert "guard-5623" in out
    assert "nosuchagent-xyz" in out


def test_unparseable_input_degrades_loudly_not_to_a_silent_zero():
    """A broken read must not render as 'no reflection work'. That is the one
    answer it is never safe to invent (guard-2298)."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--stdin", "--agent", "bravo"],
        input="{not json", text=True, capture_output=True, timeout=120)
    assert proc.returncode == 0, "must fail OPEN — a close may never wedge here"
    out = json.loads(proc.stdout)
    assert out["degraded"] is True and out["error"]
    assert out["actionable"] == 0


def test_empty_stdin_is_a_malfunction_not_an_empty_queue():
    """guard-3707: a zero-byte reply is a malfunction, not a result."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--stdin", "--agent", "bravo"],
        input="", text=True, capture_output=True, timeout=120)
    out = json.loads(proc.stdout)
    assert out["degraded"] is True
    assert "0 bytes" in out["error"]
