"""close-review-queue.py — the post-hoc review lane for uncalibrated closers ().

Pins the two properties that make the lane honest rather than merely present:

  * `list` offers a reviewer ONLY what it may independently review — a same-mind
    closure (completed_by == reviewer) is partitioned out and COUNTED, never ranked
    (coordination.md: independence is the agent name, not the session), and closures
    already carrying a verdict, resting recurring goals, non-completed rows and closures
    older than the window never reach the candidate list;
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
    assert crq.main(["stats", "--json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["attributed_via"]["archive"] == 1 and cli["roles"]["worker"]["rejected"] == 1


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


def test_the_roles_come_from_the_gate_config_section():
    roles = crq.roles_from_config()
    assert roles == ["worker"], roles


def test_cli_help_runs_from_a_subprocess():
    env = dict(os.environ, STORAGE_BACKEND="local")
    res = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0 and "post-hoc" in res.stdout.lower() or "list" in res.stdout
