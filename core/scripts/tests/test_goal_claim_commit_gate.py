#!/usr/bin/env python3
"""Pins for goal-claim-commit-gate.py (Gate M2).

The gate refuses a commit whose message names a goal another SESSION holds a
live claim on — the backstop for user-directed chat work, which never calls
aspirations-claim.sh and therefore has no coordination edge at all.

The dangerous failure here is NOT a missed conflict. It is a gate that cannot
refuse: this gate's normal reading is "allow", so a refactor that breaks the
refusal path produces a permanently green gate and nothing ever notices
(guard-4338 — the citation-drift check went from correctly FAILING on 5 missing
records to vacuously PASSING on 0 checked, which is worse than absent).
test_positive_control_gate_can_refuse pins that directly, and
test_hook_invokes_the_gate pins the call site, because a gate with no caller is
indistinguishable from one that always returns clean.

Tests read the SHIPPED module (guard-920) rather than re-declaring its
predicate, so a change to the real file is what they measure.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "goal_claim_commit_gate", SCRIPT_DIR / "goal-claim-commit-gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

REPO_ROOT = SCRIPT_DIR.parent.parent
MY_SID = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER_SID = "bbbbbbbb-5555-6666-7777-888888888888"


def _iso(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).replace(
        microsecond=0).isoformat()


def _queue(tmp_path, goals, name="aspirations.jsonl"):
    """A queue file in the real on-disk shape: one ASPIRATION per line."""
    p = tmp_path / name
    p.write_text(json.dumps({"id": "asp-115", "goals": goals}) + "\n", encoding="utf-8")
    return p


# --- goal-id extraction -----------------------------------------------------

def test_finds_the_documented_id_shapes():
    ids = gate.find_goal_ids(
        "fix(g-115-6689): thing\n\nalso g-001-01 and g-326-283-a and g-xw-1755-07\n")
    assert "g-115-6689" in ids
    assert "g-001-01" in ids, "2-digit counter form (CLAUDE.md ID Formats) must match"
    assert "g-326-283-a" in ids, "the -a suffix form must match"
    assert "g-xw-1755-07" in ids, "cross-world g-xw-<ts>-NN form must match"


def test_ids_are_deduped_in_order():
    assert gate.find_goal_ids("g-115-1 then g-115-2 then g-115-1") == ["g-115-1", "g-115-2"]


def test_git_comment_lines_are_not_scanned():
    """The hook sees the RAW message file, before git strips its `#` help text.

    git's commit template names branches and paths in `#` lines; scanning them
    would resolve ids the author never wrote.
    """
    assert gate.find_goal_ids("chore: tidy\n# On branch fix/g-115-6706\n") == []


# --- the decision table -----------------------------------------------------

def _claims(sid=OTHER_SID, minutes=10, **over):
    rec = {"claimed_by": "alpha", "claimed_by_sid": sid, "claimed_at": _iso(minutes),
           "status": "pending", "title": "t", "source": "world"}
    rec.update(over)
    return {"g-115-1": rec}


def test_foreign_live_session_conflicts():
    conflicts, checked = gate.evaluate(["g-115-1"], _claims(), MY_SID)
    assert checked == 1
    assert [c["goal_id"] for c in conflicts] == ["g-115-1"]


def test_same_agent_different_body_still_conflicts():
    """THE founding case: both Bodies are `alpha`, so only the sid separates them.

    An agent-name comparison is FALSE here. If this ever passes by name, the
    gate has stopped covering the collision it was built for.
    """
    claims = _claims(claimed_by="alpha")
    conflicts, _ = gate.evaluate(["g-115-1"], claims, MY_SID)
    assert conflicts, "two Bodies of one agent must still conflict — sid is the discriminator"


def test_my_own_claim_does_not_conflict():
    conflicts, _ = gate.evaluate(["g-115-1"], _claims(sid=MY_SID), MY_SID)
    assert conflicts == []


def test_absent_stored_sid_abstains():
    """Matches aspirations.py:1697 — pre- records carry no claim sid.

    Refusing them would wedge real work to close a hole, so the sid axis simply
    does not vote.
    """
    conflicts, _ = gate.evaluate(["g-115-1"], _claims(sid=None), MY_SID)
    assert conflicts == []
    conflicts, _ = gate.evaluate(["g-115-1"], _claims(sid=""), MY_SID)
    assert conflicts == []


def test_stale_claim_is_left_to_the_sweep():
    conflicts, _ = gate.evaluate(
        ["g-115-1"], _claims(minutes=gate.STALE_GRACE_MINUTES + 1), MY_SID)
    assert conflicts == [], "past the grace window this is stranded-claim-sweep's call"


def test_claim_at_the_grace_boundary_still_conflicts():
    conflicts, _ = gate.evaluate(
        ["g-115-1"], _claims(minutes=gate.STALE_GRACE_MINUTES - 1), MY_SID)
    assert conflicts, "inside the window the claim is live"


def test_unparseable_claimed_at_conflicts_rather_than_passing():
    """An unreadable timestamp must not become a free pass.

    Age is what EXCUSES a foreign claim, so a broken date failing open on the
    age axis would let any malformed record through. Absent age => still live.
    """
    conflicts, _ = gate.evaluate(["g-115-1"], _claims(claimed_at="not-a-date"), MY_SID)
    assert conflicts and conflicts[0]["age_minutes"] is None


def test_unset_request_sid_abstains():
    """Documented fail direction: a commit hook must not wedge on a missing var."""
    conflicts, checked = gate.evaluate(["g-115-1"], _claims(), "")
    assert conflicts == [] and checked == 0


def test_unknown_goal_id_abstains():
    conflicts, checked = gate.evaluate(["g-999-9"], _claims(), MY_SID)
    assert conflicts == [] and checked == 0


# --- SSOT pin ---------------------------------------------------------------

def test_stale_grace_matches_stranded_sweep():
    """The grace window is deliberately the sweep's DEFAULT_FOREIGN_SID_GRACE_MINUTES.

    If this gate refused past the point the sweep would REAP the claim, a dead
    holder would freeze commits indefinitely. Read from the sweep's SOURCE (it
    imports _rt and a daemon client, so importing it here would drag the daemon
    into a unit test) — a textual read is enough to catch divergence.
    """
    src = (SCRIPT_DIR / "stranded-claim-sweep.py").read_text(encoding="utf-8")
    m = re.search(r"^DEFAULT_FOREIGN_SID_GRACE_MINUTES\s*=\s*(\d+)", src, re.M)
    assert m, "stranded-claim-sweep.py no longer declares DEFAULT_FOREIGN_SID_GRACE_MINUTES"
    assert gate.STALE_GRACE_MINUTES == int(m.group(1)), (
        f"grace windows diverged: gate={gate.STALE_GRACE_MINUTES} "
        f"sweep={m.group(1)} — one of them moved without the other")


# --- override trailer -------------------------------------------------------

def test_override_accepted_with_a_real_justification():
    just, note = gate.parse_override(
        "fix(g-115-1): x\n\ngoal-claim-override: merging the holder's own work\n")
    assert just == "merging the holder's own work" and note == ""


def test_override_rejected_when_justification_is_token():
    just, note = gate.parse_override("fix: x\n\ngoal-claim-override: nope\n")
    assert just is None and "too short" in note


def test_override_in_a_comment_line_does_not_count():
    just, _ = gate.parse_override("fix: x\n# goal-claim-override: this is git help text\n")
    assert just is None


# --- store reading ----------------------------------------------------------

def test_reads_a_goal_out_of_a_queue(tmp_path):
    p = _queue(tmp_path, [{"id": "g-115-1", "claimed_by": "alpha",
                           "claimed_by_sid": OTHER_SID, "claimed_at": _iso(5),
                           "status": "pending", "title": "some goal"}])
    claims = gate.load_claims(["g-115-1"], paths=[("world", p)])
    assert claims["g-115-1"]["claimed_by_sid"] == OTHER_SID
    assert claims["g-115-1"]["source"] == "world"


def test_substring_prefilter_does_not_lose_a_later_goal(tmp_path):
    """The prefilter skips json.loads for lines mentioning no wanted id.

    A goal sharing a line with an already-found one must still resolve — an
    over-eager `wanted - found` filter would skip the line and silently drop it.
    """
    p = _queue(tmp_path, [
        {"id": "g-115-1", "claimed_by_sid": OTHER_SID, "claimed_at": _iso(5)},
        {"id": "g-115-2", "claimed_by_sid": OTHER_SID, "claimed_at": _iso(5)},
    ])
    claims = gate.load_claims(["g-115-1", "g-115-2"], paths=[("world", p)])
    assert set(claims) == {"g-115-1", "g-115-2"}


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "aspirations.jsonl"
    p.write_text("{not json\n" + json.dumps(
        {"id": "asp-1", "goals": [{"id": "g-115-1", "claimed_by_sid": OTHER_SID,
                                   "claimed_at": _iso(5)}]}) + "\n", encoding="utf-8")
    assert "g-115-1" in gate.load_claims(["g-115-1"], paths=[("world", p)])


def test_missing_queue_file_is_not_fatal(tmp_path):
    assert gate.load_claims(["g-115-1"], paths=[("world", tmp_path / "nope.jsonl")]) == {}


# --- the gate end to end ----------------------------------------------------

def _run(tmp_path, monkeypatch, message, goals, sid=MY_SID):
    p = _queue(tmp_path, goals)
    monkeypatch.setattr(gate, "_queue_paths", lambda: [("world", p)])
    monkeypatch.setattr(gate, "in_replay", lambda repo: False)
    monkeypatch.setenv("MIND_SID", sid)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message, encoding="utf-8")
    import io
    buf = io.StringIO()
    rc = gate.run_gate(REPO_ROOT, msg, out=buf)
    return rc, buf.getvalue()


_LIVE = [{"id": "g-115-1", "claimed_by": "alpha", "claimed_by_sid": OTHER_SID,
          "claimed_at": _iso(10), "status": "pending", "title": "held goal"}]


def test_positive_control_gate_can_refuse(tmp_path, monkeypatch):
    """THE anti-vacuity pin. This gate's normal answer is "allow"; if the refusal
    path ever breaks, every other test here still passes and the gate is dead."""
    rc, out = _run(tmp_path, monkeypatch, "fix(g-115-1): thing\n", _LIVE)
    assert rc == 1, f"gate failed to refuse a live foreign claim: {out}"
    assert "REFUSED" in out and "g-115-1" in out


def test_refusal_names_the_holder_and_the_escape(tmp_path, monkeypatch):
    """A refusal an agent cannot act on becomes an override reflex."""
    _, out = _run(tmp_path, monkeypatch, "fix(g-115-1): thing\n", _LIVE)
    assert OTHER_SID[:8] in out, "must name the holding session"
    assert MY_SID[:8] in out, "must name this session, so the two are comparable"
    assert "goal-claim-override:" in out, "must name the sanctioned escape"


def test_clean_commit_passes(tmp_path, monkeypatch):
    rc, out = _run(tmp_path, monkeypatch, "chore: tidy whitespace\n", _LIVE)
    assert rc == 0 and out == ""


def test_override_allows_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "write_ledger", lambda *a, **k: "")
    rc, out = _run(tmp_path, monkeypatch,
                   "fix(g-115-1): x\n\ngoal-claim-override: merging the holder's own work\n",
                   _LIVE)
    assert rc == 0 and "OVERRIDE accepted" in out


def test_ledger_failure_does_not_wedge_the_commit(tmp_path, monkeypatch):
    """The audit write must never cost availability — it WARNs and allows."""
    monkeypatch.setattr(gate, "write_ledger", lambda *a, **k: "disk on fire")
    rc, out = _run(tmp_path, monkeypatch,
                   "fix(g-115-1): x\n\ngoal-claim-override: a real justification here\n",
                   _LIVE)
    assert rc == 0 and "disk on fire" in out


def test_merge_commit_is_skipped(tmp_path, monkeypatch):
    p = _queue(tmp_path, _LIVE)
    monkeypatch.setattr(gate, "_queue_paths", lambda: [("world", p)])
    monkeypatch.setattr(gate, "in_replay", lambda repo: True)
    monkeypatch.setenv("MIND_SID", MY_SID)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix(g-115-1): thing\n", encoding="utf-8")
    assert gate.run_gate(REPO_ROOT, msg) == 0


def test_broken_store_fails_open(tmp_path, monkeypatch):
    """Fail-open on plumbing: a gate that wedges every commit is worse than the bug."""
    def boom():
        raise RuntimeError("world unreachable")
    monkeypatch.setattr(gate, "_queue_paths", boom)
    monkeypatch.setattr(gate, "in_replay", lambda repo: False)
    monkeypatch.setenv("MIND_SID", MY_SID)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix(g-115-1): thing\n", encoding="utf-8")
    import io
    buf = io.StringIO()
    assert gate.run_gate(REPO_ROOT, msg, out=buf) == 0
    assert "WARN" in buf.getvalue()


def test_unreadable_message_fails_open(tmp_path):
    import io
    buf = io.StringIO()
    assert gate.run_gate(REPO_ROOT, tmp_path / "does-not-exist", out=buf) == 0
    assert "WARN" in buf.getvalue()


# --- timezone normalisation -------------------------------------------------

def test_timezone_aware_claimed_at_does_not_disable_the_gate():
    """An AWARE claimed_at must not raise out of evaluate().

    It parses fine and then explodes on `now - then` with "can't subtract
    offset-naive and offset-aware datetimes". run_gate's fail-open handler
    swallows that, so the gate silently ALLOWS — the vacuity class this whole
    file exists to prevent. Both offset spellings are covered because
    fromisoformat rejects a bare "Z" on older Pythons unless it is translated.
    """
    for suffix in ("+00:00", "Z"):
        # RELATIVE, never an absolute literal: a hardcoded stamp ages past
        # STALE_GRACE_MINUTES and the test then fails forever. This one did,
        # 40 minutes after it was written.
        claims = _claims(claimed_at=_iso(10) + suffix)
        stamp = _iso(10) + suffix
        conflicts, _ = gate.evaluate(["g-115-1"], claims, MY_SID)
        assert conflicts, f"{stamp} produced no conflict — gate disabled itself"
        assert conflicts[0]["age_minutes"] is not None, (
            f"{stamp} did not yield a usable age, so the stale-grace axis is dead")


def test_aware_and_naive_same_instant_agree():
    """Discriminating control: normalisation must be a CONVERSION, not a discard.

    Returning None for an aware stamp would also pass the test above (absent age
    still conflicts), so without this the fix could be wrong and look right.
    """
    naive = gate._age_minutes("2026-08-19T00:57:34",
                              now=datetime(2026, 8, 19, 2, 57, 34))
    aware = gate._age_minutes("2026-08-19T00:57:34+00:00",
                              now=datetime(2026, 8, 19, 2, 57, 34))
    assert naive == 120.0, naive
    assert aware == naive, f"aware={aware} naive={naive} — conversion is not exact"


def test_predates_separates_replay_artifacts_from_real_hits():
    """--audit judges OLD commits against TODAY's claims, so a goal claimed
    after a commit landed scores as a would-refuse the gate could never have
    produced. Measured live: the raw count went 0 -> 2 in an hour for exactly
    that reason. Misreading it as an FP rate would mis-tune the gate.

    Absolute literals are safe HERE because both sides are explicit — there is
    no comparison against now().
    """
    commit, earlier, later = "2026-08-19T01:50:13+00:00", "2026-08-19T02:22:04", "2026-08-19T01:00:00"
    assert gate._predates(commit, earlier) is True, "claim AFTER commit = artifact"
    assert gate._predates(commit, later) is False, "claim BEFORE commit = reachable"
    assert gate._predates(commit, None) is False, "no claim time => not an artifact"
    assert gate._predates("garbage", earlier) is False, "unparseable => not an artifact"


def test_audit_classifies_holder_own_commits_as_artifacts(tmp_path, monkeypatch):
    """--audit judges every commit against the CURRENT session's sid, so a peer
    committing its OWN claimed work scores as a conflict that its own session
    would never have seen. Measured live: 3 raw hits, of which 2 were peers
    committing their own goals. Left uncorrected the number reads as an FP rate.

    Author-name matching is a heuristic — git records an author, never a session
    — so a same-agent second Body is excused with the holder. That under-reports,
    which is the safe direction for a number whose job is catching noise.
    """
    import subprocess
    repo = tmp_path / "r"; repo.mkdir()

    def run(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "foxtrot@example.invalid")
    run("config", "user.name", "foxtrot")
    (repo / "f.txt").write_text("1\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "chore(g-115-1): foxtrot's own work")

    claims = {"g-115-1": {"claimed_by": "foxtrot", "claimed_by_sid": OTHER_SID,
                          "claimed_at": _iso(5), "status": "pending",
                          "title": "t", "source": "world"}}
    monkeypatch.setattr(gate, "load_claims", lambda ids, paths=None: claims)
    monkeypatch.setenv("MIND_SID", MY_SID)
    import io
    buf = io.StringIO()
    assert gate.run_audit(repo, 10, out=buf) == 0
    out = buf.getvalue()
    assert "holder's own commit" in out, f"not classified as an artifact:\n{out}"
    assert "REACHABLE REFUSALS   : 0" in out, (
        f"a peer committing its own claimed goal counted as reachable:\n{out}")

    # DISCRIMINATING CONTROL. Without this, classifying EVERYTHING as an
    # artifact passes the two assertions above while making REACHABLE read 0
    # forever — the misleading-clean-number failure this classification exists
    # to prevent. Mutation-tested: `own = True` survives the assertions above
    # and is caught only here.
    run("config", "user.name", "someone-else")
    (repo / "f.txt").write_text("2\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "chore(g-115-1): NOT the holder")
    buf2 = io.StringIO()
    assert gate.run_audit(repo, 10, out=buf2) == 0
    out2 = buf2.getvalue()
    assert "REACHABLE REFUSALS   : 1" in out2, (
        f"a non-holder commit postdating the claim must stay REACHABLE:\n{out2}")


# --- replay operations ------------------------------------------------------

def _throwaway_repo(tmp_path):
    """A REAL git repo. in_replay reads git state, so a fake cannot exercise it."""
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*a):
        return subprocess.run(["git", "-C", str(repo), *a],
                              capture_output=True, text=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    run("config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("1\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "one")
    (repo / "f.txt").write_text("2\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "two")
    return repo, run


def test_in_replay_is_false_on_a_clean_repo(tmp_path):
    """POSITIVE CONTROL (guard-2903): an invariance test is green by default when
    its fixture is broken. If in_replay returned True unconditionally, every
    skip assertion below would pass while the gate was dead. This pins False."""
    repo, _ = _throwaway_repo(tmp_path)
    assert gate.in_replay(repo) is False


def test_in_replay_detects_a_real_revert(tmp_path):
    repo, run = _throwaway_repo(tmp_path)
    run("revert", "-n", "HEAD")          # -n leaves REVERT_HEAD in place
    assert gate.in_replay(repo) is True, (
        "a revert in progress was not detected — refusing someone's revert "
        "because a peer holds that goal's claim is exactly backwards")


def test_in_replay_detects_a_conflicted_cherry_pick(tmp_path):
    """The CONFLICTED form, because it is the only one that reaches this gate.

    Measured on this box: a CLEAN cherry-pick or revert never fires commit-msg
    at all (git reuses the original message), and a clean `cherry-pick -n`
    writes no sentinel — correctly, since the follow-up `git commit` is an
    ordinary commit you author, which the gate SHOULD check. Only a CONFLICTED
    replay leaves its sentinel and then fires commit-msg on the resolving
    commit. Testing the clean form instead would have pinned an unreachable
    path and left the reachable one uncovered.
    """
    repo, run = _throwaway_repo(tmp_path)
    run("checkout", "-q", "-b", "side", "HEAD~1")
    (repo / "f.txt").write_text("conflicting\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "side")
    run("cherry-pick", "main")           # conflicts -> CHERRY_PICK_HEAD persists
    assert gate.in_replay(repo) is True, (
        "a conflicted cherry-pick was not detected; the resolving `git commit` "
        "would be gated on a goal id the ORIGINAL author wrote")


def test_in_replay_detects_a_real_merge(tmp_path):
    repo, run = _throwaway_repo(tmp_path)
    run("checkout", "-q", "-b", "side", "HEAD~1")
    (repo / "g.txt").write_text("s\n", encoding="utf-8")
    run("add", "-A"); run("commit", "-q", "-m", "side")
    run("checkout", "-q", "main")
    run("merge", "--no-commit", "--no-ff", "side")
    assert gate.in_replay(repo) is True


def test_replay_skip_is_what_suppresses_the_refusal(tmp_path, monkeypatch):
    """The pair that makes the skip meaningful: SAME inputs, only replay differs."""
    p = _queue(tmp_path, _LIVE)
    monkeypatch.setattr(gate, "_queue_paths", lambda: [("world", p)])
    monkeypatch.setenv("MIND_SID", MY_SID)
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("fix(g-115-1): thing\n", encoding="utf-8")

    monkeypatch.setattr(gate, "in_replay", lambda repo: False)
    assert gate.run_gate(REPO_ROOT, msg, out=__import__("io").StringIO()) == 1, (
        "positive control failed — the refusal path is dead, so the skip below "
        "proves nothing")

    monkeypatch.setattr(gate, "in_replay", lambda repo: True)
    assert gate.run_gate(REPO_ROOT, msg, out=__import__("io").StringIO()) == 0


# --- call site --------------------------------------------------------------

def test_hook_invokes_the_gate():
    """A gate with no call site is indistinguishable from one that returns clean.

    Gate M1's own history is the precedent: a merge dropped board-citation-check
    from BOTH its SKILL.md call sites, leaving an 11kB tool with zero callers
    and 8 pins still green (guard-4338).
    """
    hook = (REPO_ROOT / "core" / "githooks" / "commit-msg").read_text(encoding="utf-8")
    assert "goal-claim-commit-gate.py" in hook, (
        "core/githooks/commit-msg no longer invokes the goal-claim gate — "
        "the backstop is installed nowhere")
    assert "--commit-msg-file" in hook


def test_gate_is_registered_in_gates_yaml():
    """Registration is how gate-stats / retirement-eval learn the gate exists."""
    reg = (REPO_ROOT / "core" / "config" / "gates.yaml").read_text(encoding="utf-8")
    assert "goal-claim-commit-gate.py" in reg
