"""close-review-queue.py — the post-hoc review lane for uncalibrated closers ().

Pins the two properties that make the lane honest rather than merely present:

  * `list` offers a reviewer ONLY what it may independently review — a same-mind
    closure (completed_by == reviewer) is partitioned out and COUNTED, never ranked
    (coordination.md: independence is the agent name, not the session), and closures
    already carrying a verdict, resting recurring goals, non-completed rows and closures
    older than the window never reach the candidate list;
  * `list` offers the open review requests first (g-375-116): a goal carrying
    review_requested and no verdict made at or after it (g-375-119), with its executor as
    the closer, never to that executor, never when it names no executor, never once its
    goal is moot;
  * `stats` reads the verdict artifacts through the gate's own reader (last entry wins,
    list- and dict-shaped files both live) and applies the written relax rule exactly;
  * relaxing a role keeps a deterministic sample of its closures reviewed, never none
    (g-375-82), and `stats` keeps measuring the sampled role.

CLOSE_REVIEW_LEDGER_DIR is pinned to tmp_path in every case that touches artifacts,
so nothing here can read or write the real world ledger (the g-357-40 lesson), and
STORAGE_BACKEND=local (guard-955) on the one subprocess.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "close-review-queue.py"


def _mod():
    spec = importlib.util.spec_from_file_location("close_review_queue", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


crq = _mod()
NOW = datetime(2026, 9, 24, 21, 0, 0)


def closure(gid: str, **kw) -> dict:
    base = {
        "id": gid, "asp_id": "asp-375", "title": f"Fix: {gid} does a thing",
        "description": "plain prose, no named entities", "priority": "MEDIUM",
        "status": "completed", "completed_by": "alpha", "completed_by_role": "worker",
        "completed_by_sid": "72554e53-d4de-41cf-b350-4f4b2aff98dd",
        "completed_at": "2026-09-24T20:00:00", "participants": ["agent"],
    }
    base.update(kw)
    return base


def select(goals, **kw):
    args = dict(reviewed=set(), now=NOW, since_hours=72.0, reviewer="bravo", cap=3)
    args.update(kw)
    return crq.select_candidates(goals, **args)


# ─── list ─────────────────────────────────────────────────────────────────────

def test_a_same_mind_closure_is_partitioned_out_and_counted_never_ranked():
    res = select([closure("g-1-1", completed_by="alpha"), closure("g-1-2", completed_by="alpha")],
                 reviewer="alpha")
    assert res["candidates"] == []
    assert res["same_mind"] == ["g-1-1", "g-1-2"]
    assert res["eligible_total"] == 0
    # the same rows ARE offered to a different mind, case-insensitively
    res2 = select([closure("g-1-1"), closure("g-1-2")], reviewer="Bravo")
    assert [r["goal_id"] for r in res2["candidates"]] == ["g-1-1", "g-1-2"]
    assert res2["same_mind"] == []


def test_reviewed_recurring_pending_and_old_closures_never_become_candidates():
    goals = [
        closure("g-2-1"),                                            # eligible
        closure("g-2-2"),                                            # already reviewed
        closure("g-2-3", recurring=True, status="pending"),          # resting recurring
        closure("g-2-4", status="in-progress"),                      # not closed
        closure("g-2-5", completed_at="2026-09-20T09:00:00"),        # outside the window
        closure("g-2-6", completed_at="2026-09-24", completed_by_sid=None),  # bare date, today
    ]
    res = select(goals, reviewed={"g-2-2"})
    # g-2-6's bare date reads as that day's midnight, so it ranks OLDER than g-2-1
    assert [r["goal_id"] for r in res["candidates"]] == ["g-2-1", "g-2-6"]
    # the resting recurring goal is counted where its status puts it (not completed)
    assert res["skipped"] == {"not_completed": 2, "recurring": 0, "too_old": 1, "reviewed": 1,
                              "not_sampled": 0}


def test_ranking_puts_tier_2_first_then_HIGH_then_newest_and_the_cap_holds():
    goals = [
        closure("g-3-1", priority="LOW", completed_at="2026-09-24T20:30:00"),
        closure("g-3-2", priority="MEDIUM", completed_at="2026-09-24T19:00:00"),
        closure("g-3-3", priority="MEDIUM", completed_at="2026-09-24T20:00:00"),
        # HIGH + non-recurring is a tier-2 trigger in the gate's classifier
        closure("g-3-4", priority="HIGH", completed_at="2026-09-24T18:00:00"),
    ]
    res = select(goals, cap=3)
    ids = [r["goal_id"] for r in res["candidates"]]
    assert ids == ["g-3-4", "g-3-3", "g-3-2"], ids
    assert res["candidates"][0]["tier"] == 2
    assert any("high_prio" in r for r in res["candidates"][0]["tier_reasons"])
    assert res["eligible_total"] == 4 and res["cap"] == 3


def test_the_sample_is_deterministic_and_close_to_its_rate():
    # : relaxing a role keeps a sample of its closures reviewed, never none. The
    # sample is a hash of the goal id, so a rerun (or another box) picks the same closures.
    ids = [f"g-900-{i}" for i in range(2000)]
    picked = [g for g in ids if crq.in_sample(g, 0.2)]
    assert 0.17 <= len(picked) / len(ids) <= 0.23, len(picked)
    assert picked == [g for g in ids if crq.in_sample(g, 0.2)]
    assert all(crq.in_sample(g, 1.0) for g in ids)
    # a smaller rate picks a subset of a larger one, so raising the rate never drops a closure
    assert {g for g in ids if crq.in_sample(g, 0.1)} <= set(picked)


def test_a_relaxed_role_queues_only_its_sample_and_counts_the_rest():
    workers = [closure(f"g-6-{i}") for i in range(40)]
    full_role = [closure(f"g-7-{i}", completed_by_role="reducer") for i in range(5)]
    want = sorted(g["id"] for g in workers if crq.in_sample(g["id"], 0.2))
    assert 0 < len(want) < 40, "the sample must be neither empty nor everything"
    res = select(workers + full_role, cap=100, sampled_roles={"worker"}, sample_rate=0.2)
    got = sorted(r["goal_id"] for r in res["candidates"] if r["completed_by_role"] == "worker")
    assert got == want
    assert res["skipped"]["not_sampled"] == 40 - len(want)
    # a role still in full review keeps every closure
    assert sorted(r["goal_id"] for r in res["candidates"]
                  if r["completed_by_role"] == "reducer") == [g["id"] for g in full_role]
    # positive control: in full review the same closures are all offered
    full = select(workers + full_role, cap=100)
    assert len(full["candidates"]) == 45 and full["skipped"]["not_sampled"] == 0


def test_the_sample_plan_comes_from_the_gate_config_and_never_reads_as_zero(monkeypatch,
                                                                            capsys):
    assert crq.sample_plan_from_config() == ([], 0.2)       # the shipped config
    for bad in (0, -0.5, 1.5, "none"):
        monkeypatch.setattr(crq, "_gate_section", lambda bad=bad: {
            "review_sampled_roles": ["Worker"], "review_sample_rate": bad})
        assert crq.sample_plan_from_config() == (["worker"], crq.DEFAULT_SAMPLE_RATE)
        assert "not in (0, 1]" in capsys.readouterr().err
    monkeypatch.setattr(crq, "_gate_section", lambda: {
        "review_sampled_roles": ["worker"], "review_sample_rate": 0.5})
    assert crq.sample_plan_from_config() == (["worker"], 0.5)


def test_a_gate_section_that_is_not_a_mapping_reads_as_empty_loudly(monkeypatch, capsys):
    import yaml
    monkeypatch.setattr(yaml, "safe_load", lambda _text: {"close_review_gate": ["worker"]})
    assert crq.roles_from_config() == [] and crq.sample_plan_from_config() == ([], 0.2)
    assert "not a mapping" in capsys.readouterr().err
    # positive control: the same reader over a mapping returns its roles
    monkeypatch.setattr(yaml, "safe_load",
                        lambda _text: {"close_review_gate": {"review_closer_roles": ["Worker"]}})
    assert crq.roles_from_config() == ["worker"]


def test_candidate_rows_carry_what_the_reviewer_hands_to_the_producer():
    row = select([closure("g-4-1", commit_sha="abc123")])["candidates"][0]
    assert row["completed_by"] == "alpha"            # --closer for close-review-verdict.py
    assert row["completed_by_sid"].startswith("72554e53")
    assert row["completed_by_role"] == "worker"
    assert row["commit_sha"] == "abc123"
    assert row["asp_id"] == "asp-375"
    assert row["kind"] == "closure" and row["closer"] == "alpha"


# ─── list: review requests () ────────────────────────────────────────

def request(gid: str, **kw) -> dict:
    """A goal whose closer asked for a review: still open, done by alpha, no verdict."""
    base = closure(gid, status="pending", completed_by=None, completed_by_role=None,
                   completed_by_sid=None, completed_at=None, executed_by="alpha",
                   claimed_by="alpha", review_requested="2026-09-24T18:00:00")
    base.update(kw)
    return base


def select_req(goals, **kw):
    args = dict(reviewed=set(), reviewer="bravo")
    args.update(kw)
    return crq.select_requests(goals, **args)


def test_a_review_request_is_offered_to_another_mind_with_its_closer_never_to_its_own():
    res = select_req([request("g-8-1")])
    assert [r["goal_id"] for r in res["rows"]] == ["g-8-1"]
    row = res["rows"][0]
    assert row["kind"] == "request" and row["closer"] == "alpha"    # --closer for the producer
    assert row["status"] == "pending" and row["review_requested"] == "2026-09-24T18:00:00"
    # for alpha the same request is its own work: partitioned out and counted, never listed
    own = select_req([request("g-8-1")], reviewer="ALPHA")
    assert own["rows"] == [] and own["same_mind"] == ["g-8-1"]


def test_the_closer_is_who_closed_else_who_executed_else_who_claimed_and_none_is_declined():
    goals = [
        request("g-9-1", status="completed", completed_by="echo", executed_by="alpha"),
        request("g-9-2", executed_by="zeta", claimed_by="alpha"),
        request("g-9-3", executed_by=None, claimed_by="echo"),
        request("g-9-4", executed_by=None, claimed_by=None, filed_by_agent="alpha"),
    ]
    res = select_req(goals)
    assert {r["goal_id"]: r["closer"] for r in res["rows"]} == {
        "g-9-1": "echo", "g-9-2": "zeta", "g-9-3": "echo"}
    # a record that names no executor cannot show independence: declined and counted
    assert res["no_closer"] == ["g-9-4"]


def test_answered_moot_and_unrequested_goals_are_never_offered_and_a_request_never_ages_out():
    goals = [
        request("g-10-1"),                                    # open request: offered
        request("g-10-2"),                                    # a verdict answers it
        request("g-10-3", status="skipped"),                  # moot
        request("g-10-4", status="superseded"),               # moot
        request("g-10-5", status="expired"),                  # moot
        request("g-10-6", review_requested=None),             # never requested
        request("g-10-7", status="completed", completed_by="alpha",
                completed_at="2026-09-01T00:00:00", review_requested="2026-09-01T00:00:00"),
    ]
    res = select_req(goals, reviewed={"g-10-2"})
    # g-10-7 was closed weeks ago on the promise of a review: it is still owed one
    assert [r["goal_id"] for r in res["rows"]] == ["g-10-1", "g-10-7"]
    assert res["skipped"] == {"moot": 3, "reviewed": 1}


def test_requests_rank_open_first_then_tier_2_then_HIGH_then_the_oldest_request():
    goals = [
        request("g-11-1", status="completed", completed_by="alpha", priority="HIGH",
                review_requested="2026-09-20T00:00:00"),       # closed: after every open one
        request("g-11-2", priority="LOW", review_requested="2026-09-24T10:00:00"),
        request("g-11-3", priority="LOW", review_requested="2026-09-23T10:00:00"),
        request("g-11-4", priority="HIGH", review_requested="2026-09-24T12:00:00"),
    ]
    rows = select_req(goals)["rows"]
    assert [r["goal_id"] for r in rows] == ["g-11-4", "g-11-3", "g-11-2", "g-11-1"]
    assert rows[0]["tier"] == 2


def _list(monkeypatch, capsys, closures, requests, *argv, answered=frozenset()):
    monkeypatch.setattr(crq, "load_closures", lambda role: closures)
    monkeypatch.setattr(crq, "load_requests", lambda: requests)
    for name in ("reviewed_ids", "answered_ids"):
        monkeypatch.setattr(crq, name,
                            lambda goals: {crq.goal_id_of(g) for g in goals} & set(answered))
    assert crq.main(["list", "--reviewer", "bravo", "--roles", "worker", *argv]) == 0
    return capsys.readouterr().out


def test_list_never_offers_a_request_a_verdict_already_answers(monkeypatch, capsys):
    asked = ([request("g-16-1"), request("g-16-2")], None)
    out = json.loads(_list(monkeypatch, capsys, [], asked, "--json", answered={"g-16-1"}))
    assert [r["goal_id"] for r in out["candidates"]] == ["g-16-2"]
    assert out["requests"]["skipped"]["reviewed"] == 1
    # positive control: unanswered, both are offered
    out = json.loads(_list(monkeypatch, capsys, [], asked, "--json"))
    assert [r["goal_id"] for r in out["candidates"]] == ["g-16-1", "g-16-2"]


def test_list_offers_requests_ahead_of_closures_within_one_cap_and_each_goal_once(
        monkeypatch, capsys):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    closures = [closure(f"g-12-{i}", completed_at=now) for i in range(3)]
    # g-12-0 is a worker closure whose closer also asked for a review: offered once
    both = dict(closures[0], review_requested="2026-09-24T18:00:00")
    asked = [request("g-13-1"),
             request("g-13-2", status="completed", completed_by="alpha",
                     review_requested="2026-09-24T17:00:00"), both]
    out = json.loads(_list(monkeypatch, capsys, closures, (asked, None), "--cap", "4", "--json"))
    got = [(r["goal_id"], r["kind"]) for r in out["candidates"]]
    assert got == [("g-13-1", "request"), ("g-13-2", "request"), ("g-12-0", "request"),
                   ("g-12-1", "closure")], got
    assert out["requests"]["eligible_total"] == 3 and out["requests"]["read_error"] is None
    # positive control: with no request the same closures fill the cap
    out = json.loads(_list(monkeypatch, capsys, closures, ([], None), "--cap", "4", "--json"))
    assert [r["kind"] for r in out["candidates"]] == ["closure"] * 3


def test_a_request_row_prints_its_closer_and_a_failed_read_is_never_silent(monkeypatch,
                                                                             capsys):
    out = _list(monkeypatch, capsys, [], ([request("g-15-1")], None))
    assert "g-15-1 [asp-375] REQUEST open tier=1 MEDIUM closer=alpha" in out
    assert "READ FAILED" not in out
    out = _list(monkeypatch, capsys, [], ([], "store read failed rc=1 daemon down"))
    assert "READ FAILED (store read failed rc=1 daemon down)" in out


def test_load_requests_reads_every_wanting_status_in_one_query_and_keeps_the_requested(
        monkeypatch, capsys):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        rows = [request("g-14-1"), closure("g-14-2"), "not a record"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(rows), "")

    monkeypatch.setattr(crq.subprocess, "run", fake_run)
    rows, err = crq.load_requests()
    assert err is None and [r["id"] for r in rows] == ["g-14-1"]
    i = seen["cmd"].index("--goal-status")
    assert seen["cmd"][i + 1].split(",") == list(crq.REQUEST_STATUSES)
    assert "completed" in crq.REQUEST_STATUSES and "--full" in seen["cmd"]
    # a failed read is an error the listing prints, never an empty answer
    monkeypatch.setattr(crq.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "daemon down"))
    rows, err = crq.load_requests()
    assert rows == [] and "rc=1" in err and "daemon down" in err
    assert "requests store read failed" in capsys.readouterr().err


def test_a_verdict_answers_only_a_request_made_at_or_before_it():
    # : any verdict used to answer a request, so a REJECT, then rework, then a
    # fresh request was never offered again
    rejected = _verdict("g-17-1", "REJECT", "2026-10-02T10:00:00")
    assert crq.answers(rejected, "2026-10-02T12:00:00") is False   # the request after rework
    assert crq.answers(rejected, "2026-10-02T10:00:00") is True    # the same second answers
    assert crq.answers(rejected, "2026-10-02T09:59:59") is True    # positive control
    assert crq.answers(None, "2026-10-02T09:59:59") is False       # no verdict answers nothing
    # a zone is read as UTC, never compared naive to aware (which raises)
    assert crq.answers(rejected, "2026-10-02T12:00:00+02:00") is True   # 10:00 UTC
    assert crq.answers(rejected, "2026-10-02T10:00:01Z") is False
    assert crq.answers(dict(rejected, reviewed_at="2026-10-02T10:00:00.5"),
                       "2026-10-02T10:00:00") is True


def test_an_unreadable_request_stamp_takes_any_verdict_and_an_unreadable_verdict_stamp_none():
    v = _verdict("g-18-1", "APPROVE", "2026-10-02T10:00:00")
    # no verdict could ever be shown to follow an unreadable request: any verdict answers
    # it, or it would be listed forever
    # (a zone that carries a stamp past year 1..9999 makes the UTC conversion overflow)
    for asked in ("true", "soon", "", None, "0001-01-01T00:00:00+01:00"):
        assert crq.answers(v, asked) is True, asked
    # a verdict that cannot show it followed the request answers nothing: listed once more,
    # until the reviewer's next verdict, which close-review-verdict.py always stamps
    for at in ("yesterday", "", None, "9999-12-31T23:59:59-01:00"):
        assert crq.answers(dict(v, reviewed_at=at), "2026-10-02T09:00:00") is False, at
    assert crq.answers("APPROVE", "2026-10-02T09:00:00") is False    # an entry, not a record


def test_list_offers_a_request_again_when_its_current_verdict_predates_it(
        tmp_path, monkeypatch, capsys):
    # Through main and the gate's own reader, over real trails: the REJECT that sent g-19-1
    # back predates its fresh request, so the request is offered again. A verdict after the
    # request (g-19-2), or in the same second (g-19-3), answers it; g-19-4 has none.
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    asked = "2026-10-02T12:00:00"
    _write_artifact(d, "g-19-1", [_verdict("g-19-1", "REJECT", "2026-10-02T10:00:00")])
    _write_artifact(d, "g-19-2", [_verdict("g-19-2", "REJECT", "2026-10-02T10:00:00"),
                                  _verdict("g-19-2", "APPROVE", "2026-10-02T13:00:00")])
    _write_artifact(d, "g-19-3", [_verdict("g-19-3", "APPROVE", asked)])
    requests = [request(f"g-19-{i}", review_requested=asked) for i in range(1, 5)]
    monkeypatch.setattr(crq, "load_closures", lambda role: [])
    monkeypatch.setattr(crq, "load_requests", lambda: (requests, None))
    argv = ["list", "--reviewer", "bravo", "--roles", "worker", "--cap", "5", "--json"]
    assert crq.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["goal_id"] for r in out["candidates"]] == ["g-19-1", "g-19-4"]
    assert out["requests"]["skipped"]["reviewed"] == 2
    # positive control: the same trails answer a request made before every verdict
    requests[:] = [request(f"g-19-{i}", review_requested="2026-10-02T09:00:00")
                   for i in range(1, 5)]
    assert crq.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["goal_id"] for r in out["candidates"]] == ["g-19-4"]
    assert out["requests"]["skipped"]["reviewed"] == 3


# ─── stats ────────────────────────────────────────────────────────────────────

def _write_artifact(directory: Path, gid: str, entries, shape: str = "list") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = entries if shape == "list" else entries[-1]
    (directory / f"{gid}.json").write_text(json.dumps(payload), encoding="utf-8")


def _verdict(gid, verdict, at, reviewer="bravo"):
    return {"goal_id": gid, "verdict": verdict, "reviewer": reviewer, "reviewed_at": at}


def test_stats_group_by_the_closer_role_through_the_goal_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    _write_artifact(d, "g-5-1", [_verdict("g-5-1", "REJECT", "2026-09-24T10:00:00"),
                                 _verdict("g-5-1", "APPROVE", "2026-09-24T12:00:00")])  # last wins
    _write_artifact(d, "g-5-2", [_verdict("g-5-2", "APPROVE_WITH_NOTES", "2026-09-24T13:00:00")],
                    shape="dict")                                                     # dict shape
    _write_artifact(d, "g-5-3", [_verdict("g-5-3", "REJECT", "2026-09-24T14:00:00")])
    _write_artifact(d, "g-9-9", [_verdict("g-9-9", "APPROVE", "2026-09-24T15:00:00")])  # a Mind's own
    goals = {g["id"]: g for g in (closure("g-5-1"), closure("g-5-2"), closure("g-5-3"),
                                  closure("g-9-9", completed_by_role="reducer"))}
    verdicts = crq.read_all_verdicts(crq.artifacts_dir())
    assert crq.artifacts_dir() == d
    stats = crq.role_stats(verdicts, goals, ["worker"])
    w = stats["roles"]["worker"]
    assert (w["reviewed"], w["approved"], w["rejected"], w["other"]) == (3, 2, 1, 0)
    assert w["approve_rate"] == round(2 / 3, 3)
    assert w["recent"] == ["APPROVE", "APPROVE_WITH_NOTES", "REJECT"]   # reviewed_at order
    assert w["relax_ok"] is False
    assert stats["unattributed"]["role_not_reviewed"] == ["g-9-9"]      # the reducer's close
    assert stats["artifacts_total"] == 4 and stats["attributed_via"]["live"] == 3


def _jsonl(path: Path, *records) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_a_verdict_whose_goal_survives_only_in_an_archived_record_counts_under_its_role(
        tmp_path, monkeypatch, capsys):
    # : a goal leaves the live query when its aspiration is archived, and its
    # verdict (here a REJECT) fell out of the role's rate. Resolved through every store by
    # goal-resolve.py over a real world layout, it counts again. An evicted goal, a goal of
    # another role and a goal no store holds each land in their own bucket, none in the rate.
    world = tmp_path / "world"
    world.mkdir()
    _jsonl(world / "aspirations-archive.jsonl", {
        "id": "asp-73", "status": "completed",
        "goals": [closure("g-73-1"), closure("g-73-2", completed_by_role="reducer")],
        "archived_census": {"evicted_ids": {"completed": ["g-73-9"]}}})
    _jsonl(world / "aspirations.jsonl", {
        "id": "asp-74", "status": "active", "goals": [],
        "archived_census": {"evicted_ids": {"completed": ["g-74-5"]}}})
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    for gid, verdict in (("g-73-1", "REJECT"), ("g-73-2", "APPROVE"), ("g-73-9", "APPROVE"),
                         ("g-74-5", "APPROVE"), ("g-99-1", "APPROVE")):
        _write_artifact(d, gid, [_verdict(gid, verdict, "2026-09-26T12:17:00")])
    verdicts = crq.read_all_verdicts(crq.artifacts_dir())
    # the pre-fix join: no live closure, so nothing reaches the worker rate
    live_only = crq.role_stats(verdicts, {}, ["worker"])
    assert live_only["roles"]["worker"]["reviewed"] == 0
    assert live_only["unattributed"]["not_resolved"] == sorted(verdicts)
    resolved, err = crq.resolve_unattributed(sorted(verdicts), world=str(world))
    assert err is None
    stats = crq.role_stats(verdicts, {}, ["worker"], resolved=resolved)
    w = stats["roles"]["worker"]
    assert (w["reviewed"], w["rejected"], w["recent"]) == (1, 1, ["REJECT"])
    assert stats["attributed_via"] == {"verdict": 0, "live": 0, "archive": 1}
    assert stats["unattributed"] == {"role_not_reviewed": ["g-73-2"],
                                     "evicted_role_unknown": ["g-73-9", "g-74-5"],
                                     "not_found_anywhere": ["g-99-1"], "not_resolved": []}
    crq._print_stats(stats)
    out = capsys.readouterr().out
    assert "artifacts=5: attributed to a listed role 1" in out and "archived record 1" in out
    assert "goal evicted to an id-only census, role unknown: 2 [g-73-9 g-74-5]" in out
    # the CLI wires the same path: live closures first, then every store
    resolve, tmp_world = crq.resolve_unattributed, str(world)
    monkeypatch.setattr(crq, "load_closures", lambda role: [])
    monkeypatch.setattr(crq, "resolve_unattributed",
                        lambda gids, world=None: resolve(gids, world=tmp_world))
    out_of_live = crq.load_out_of_live
    monkeypatch.setattr(crq, "load_out_of_live",
                        lambda roles, world=None: out_of_live(roles, world=tmp_world))
    assert crq.main(["stats", "--json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["attributed_via"]["archive"] == 1 and cli["roles"]["worker"]["rejected"] == 1
    # and the coverage over the same stores: the one worker closure, reviewed ()
    cov = cli["roles"]["worker"]["coverage"]
    assert (cov["covered"], cov["population"], cov["lane_start"]) == (1, 1, "2026-09-26T12:17:00")


def test_a_verdict_the_producer_wrote_is_attributed_by_the_closer_it_names(tmp_path,
                                                                           monkeypatch):
    # , the producer's half: close-review-verdict.py copies the closer from the
    # goal record at write time, and stats reads it from the verdict with no goal record
    # at all. The verdict's own record wins over a later record of another role, because
    # it names the closure that was reviewed.
    spec = importlib.util.spec_from_file_location("close_review_verdict_for_queue",
                                                  SCRIPT.parent / "close-review-verdict.py")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(producer._gate(), "load_goal", lambda gid, source: closure(
        gid, completed_by_sid="5af0e472-0000") if gid == "g-76-1" else {})
    src, art = tmp_path / "source.txt", tmp_path / "artifact.txt"
    src.write_text("Record g-76-1 as done.", encoding="utf-8")
    art.write_text("- g-76-1: done", encoding="utf-8")
    for gid in ("g-76-1", "g-76-2"):
        assert producer.main(["--goal", gid, "--reviewer", "bravo", "--source-file", str(src),
                              "--artifact-file", str(art), "--approve", "--write"]) == 0
    verdicts = crq.read_all_verdicts(crq.artifacts_dir())
    stats = crq.role_stats(verdicts, {"g-76-1": closure("g-76-1", completed_by_role="reducer")},
                           ["worker"])
    assert stats["roles"]["worker"]["reviewed"] == 1
    assert stats["attributed_via"]["verdict"] == 1 and "worker/5af0e472" in stats["by_sid"]
    assert stats["unattributed"]["not_resolved"] == ["g-76-2"]    # no live record at write


def test_the_relax_rule_needs_ten_reviews_an_80_percent_rate_and_a_clean_recent_five():
    ok = crq.relax_rule(10, 0.8, ["APPROVE"] * 10)
    assert ok["relax_ok"] is True and ok["blocking_reasons"] == []
    few = crq.relax_rule(9, 1.0, ["APPROVE"] * 9)
    assert few["relax_ok"] is False and "only 9 reviewed" in few["blocking_reasons"][0]
    low = crq.relax_rule(12, 0.75, ["APPROVE"] * 12)
    assert low["relax_ok"] is False and any("approve rate" in r for r in low["blocking_reasons"])
    recent = crq.relax_rule(20, 0.95, ["APPROVE"] * 19 + ["REJECT"])
    assert recent["relax_ok"] is False and any("last 5" in r for r in recent["blocking_reasons"])
    old_reject = crq.relax_rule(20, 0.95, ["REJECT"] + ["APPROVE"] * 19)
    assert old_reject["relax_ok"] is True


def test_stats_keep_measuring_a_relaxed_role_and_say_relaxing_means_sampling(tmp_path,
                                                                             monkeypatch,
                                                                             capsys):
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    for i in range(10):
        _write_artifact(d, f"g-8-{i}", [_verdict(f"g-8-{i}", "APPROVE", f"2026-09-24T1{i}:00:00")])
    goals = {f"g-8-{i}": closure(f"g-8-{i}") for i in range(10)}
    verdicts = crq.read_all_verdicts(crq.artifacts_dir())
    full = crq.role_stats(verdicts, goals, ["worker"])
    assert full["roles"]["worker"]["review"] == "full" and full["roles"]["worker"]["relax_ok"]
    crq._print_stats(full)
    out = capsys.readouterr().out
    assert "role=worker review=full:" in out
    assert "move it to review_sampled_roles" in out and "never drop it" in out
    sampled = crq.role_stats(verdicts, goals, ["worker"], sampled={"worker"}, sample_rate=0.2)
    w = sampled["roles"]["worker"]
    assert (w["review"], w["sample_rate"], w["reviewed"]) == ("sampled", 0.2, 10)
    crq._print_stats(sampled)
    assert "role=worker review=sampled@0.2:" in capsys.readouterr().out


# ─── coverage () ──────────────────────────────────────────────────────

COV_NOW = datetime(2026, 9, 30, 12, 0, 0)   # a 72 h window opens at 09-27T12:00


def cover(live=(), archived=(), evicted=(), reviewed=(), lane_start="2026-09-24T12:00:00",
          **kw):
    args = dict(live=list(live), archived=list(archived), evicted_ids=set(evicted),
                reviewed=set(reviewed), verdict_ids=set(reviewed), lane_start=lane_start,
                now=COV_NOW, since_hours=72.0)
    args.update(kw)
    return crq.coverage_report("worker", **args)


def test_coverage_counts_the_misses_apart_from_what_predates_the_lane():
    # `list` puts every closure past its window in skipped.too_old, misses and history
    # alike, and the archived and evicted ones leave it with no count at all. The lane
    # began at its first verdict (09-24T12:00), so its reach began 72 h before that.
    live = [closure("g-1-1", completed_at="2026-09-29T10:00:00"),   # still in the window
            closure("g-1-2", completed_at="2026-09-25T10:00:00"),   # aged out unreviewed
            closure("g-1-3", completed_at="2026-09-22T10:00:00"),   # in the first run's reach
            closure("g-1-4", completed_at="2026-09-20T10:00:00"),   # before the reach
            closure("g-1-5", completed_at="2026-09-26T10:00:00"),   # reviewed
            closure("g-1-6", status="pending", recurring=True)]
    other_role = closure("g-1-7", completed_by_role="reducer", completed_at="2026-09-25T10:00:00")
    archived = [closure("g-2-1", completed_at="2026-09-28T10:00:00"),
                closure("g-2-2", completed_at="2026-09-10T10:00:00")]
    cov = cover(live + [other_role], archived, evicted={"g-3-1", "g-3-2"},
                reviewed={"g-1-5", "g-3-2"})
    assert cov["reach_start"] == "2026-09-21T12:00:00"
    assert cov["unreviewed"] == {"in_window": ["g-1-1"], "aged_out": ["g-1-2", "g-1-3"],
                                 "archived": ["g-2-1"], "evicted": ["g-3-1"]}
    assert cov["predates_lane"] == ["g-1-4", "g-2-2"]
    assert (cov["covered"], cov["population"], cov["coverage"]) == (2, 7, round(2 / 7, 3))
    # positive control: list's own reading of the same closures is one bucket of four, two
    # misses, a pre-lane closure and a reviewed one
    assert select(live, reviewed={"g-1-5"}, now=COV_NOW)["skipped"]["too_old"] == 4


def test_with_no_verdict_yet_nothing_predates_and_an_undated_closure_never_does():
    live = [closure("g-1-4", completed_at="2026-09-20T10:00:00"),
            closure("g-1-8", completed_at="", completed_date="")]
    none_yet = cover(live, lane_start=None)
    assert none_yet["reach_start"] is None and none_yet["predates_lane"] == []
    assert none_yet["unreviewed"]["aged_out"] == ["g-1-4", "g-1-8"]
    assert none_yet["coverage"] == 0.0
    started = cover(live)
    assert started["predates_lane"] == ["g-1-4"]
    assert started["unreviewed"]["aged_out"] == ["g-1-8"]      # undated: counted as a miss


def test_a_relaxed_role_counts_only_its_sample_as_due():
    ids = [f"g-6-{i}" for i in range(40)]
    gone = [f"g-7-{i}" for i in range(40)]
    live = [closure(g, completed_at="2026-09-25T10:00:00") for g in ids]
    cov = cover(live, evicted=set(gone), sampled=True, sample_rate=0.2)
    inside = [g for g in ids if crq.in_sample(g, 0.2)]
    assert 0 < len(inside) < len(ids) and cov["unreviewed"]["aged_out"] == sorted(inside)
    assert cov["unreviewed"]["evicted"] == sorted(g for g in gone if crq.in_sample(g, 0.2))
    full = cover(live, evicted=set(gone))
    assert len(full["unreviewed"]["aged_out"]) == 40 and len(full["unreviewed"]["evicted"]) == 40


def test_the_lane_starts_at_the_roles_own_first_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOSE_REVIEW_LEDGER_DIR", str(tmp_path))
    d = tmp_path / "audit-reports" / "close-reviews"
    _write_artifact(d, "g-9-1", [_verdict("g-9-1", "APPROVE", "2026-09-20T08:00:00")])
    _write_artifact(d, "g-5-1", [_verdict("g-5-1", "APPROVE", "2026-09-25T09:00:00")])
    _write_artifact(d, "g-5-2", [_verdict("g-5-2", "REJECT", "2026-09-24T11:00:00")])
    goals = {"g-9-1": closure("g-9-1", completed_by_role="reducer"),
             "g-5-1": closure("g-5-1"), "g-5-2": closure("g-5-2")}
    stats = crq.role_stats(crq.read_all_verdicts(crq.artifacts_dir()), goals, ["worker"])
    w = stats["roles"]["worker"]
    assert w["lane_start"] == "2026-09-24T11:00:00"           # not the reducer's 09-20 verdict
    assert w["reviewed_ids"] == ["g-5-1", "g-5-2"]
    assert crq.role_stats({}, {}, ["worker"])["roles"]["worker"]["lane_start"] is None


def test_load_out_of_live_reads_the_archive_and_both_censuses(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    _jsonl(world / "aspirations-archive.jsonl", {
        "id": "asp-73", "status": "completed",
        "goals": [closure("g-73-1"), closure("g-73-2", completed_by_role="reducer")],
        "archived_census": {"evicted_by_closer_role": {"worker": ["g-73-8"],
                                                       "reducer": ["g-73-7"]}}})
    _jsonl(world / "aspirations.jsonl", {
        "id": "asp-74", "status": "active", "goals": [closure("g-74-1")],
        "archived_census": {"evicted_ids": {"completed": ["g-74-5", "g-74-6"]},
                            "evicted_by_closer_role": {"worker": ["g-74-5"]}}})
    archived, evicted, err = crq.load_out_of_live(["worker"], world=str(world))
    assert err is None
    assert [g["id"] for g in archived] == ["g-73-1"]     # the live goals come from the query
    assert evicted == {"worker": {"g-73-8", "g-74-5"}}    # g-74-6 was evicted with no role


def test_a_failed_archive_read_reads_as_unknown_coverage_never_as_full(monkeypatch, capsys):
    def unreadable():
        raise OSError("store unreadable")
    monkeypatch.setattr(crq, "_resolver", unreadable)
    archived, evicted, err = crq.load_out_of_live(["worker"], world="/nonexistent-world")
    assert (archived, evicted) == ([], {}) and "store unreadable" in err
    assert "coverage not computed" in capsys.readouterr().err
    stats = crq.role_stats({}, {}, ["worker"])
    stats["coverage_error"] = err
    crq._print_stats(stats)
    out = capsys.readouterr().out
    assert "coverage=unknown" in out and "coverage NOT computed" in out


def test_stats_print_coverage_beside_the_approve_rate_and_name_the_misses(capsys):
    stats = crq.role_stats({}, {}, ["worker"])
    stats["roles"]["worker"]["coverage"] = cover(
        [closure("g-1-2", completed_at="2026-09-25T10:00:00")], evicted={"g-3-1"},
        reviewed={"g-1-5"})
    crq._print_stats(stats)
    out = capsys.readouterr().out
    assert "approve_rate=0.0 coverage=1/3=0.333 recent=" in out
    assert "aged_out=1 archived=0 evicted=1; predates the lane=0" in out
    assert "never reviewed: g-1-2 g-3-1" in out


def test_the_roles_come_from_the_gate_config_section():
    roles = crq.roles_from_config()
    assert roles == ["worker"], roles


def test_cli_help_runs_from_a_subprocess():
    env = dict(os.environ, STORAGE_BACKEND="local")
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0 and "post-hoc" in res.stdout.lower() or "list" in res.stdout
