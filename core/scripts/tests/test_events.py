#!/usr/bin/env python3
"""test_events.py --  regression test for the events.jsonl access engine.

Pins the lock-safe, append-only / event-sourced contract of core/scripts/events.py
(g-306-19 Gap 10 child 2/3). The load-bearing invariant: every write is a
single-record APPEND (guard-832) -- update-status NEVER rewrites the file, it
appends a new record reusing the same event_id, and readers fold-by-latest.

Pure-function + read tests run in-process (explicit tmp file -- they never touch
the real world). Full write-stack tests run via subprocess with MIND_WORLD
pointed at an isolated tmp world, so the real world/board/events.jsonl is never
written and the _fileops locking + history + changelog path is exercised
end-to-end.

Refs: g-306-41 (this), g-306-40 (schema child 1/3), guard-832 + rb-2112
(own-cloud append-only event-sourcing), world/conventions/events.md.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import events  # noqa: E402

EVENTS_PY = SCRIPT_DIR / "events.py"


def _genesis(event_id="evt-test-1", status="proposed", owner="alpha"):
    return {
        "event_id": event_id,
        "owner": owner,
        "participants": [{"role": "implementer", "agent": "alpha"}],
        "decomposition": [{"part": "1/2", "goal_id": "g-1", "owner": "alpha", "summary": "x"}],
        "status": status,
        "completion_signals": ["g-1 done"],
    }


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _count_records(path):
    return sum(1 for line in open(path, encoding="utf-8") if line.strip())


# ---- pure-function tests (in-process, no I/O) ----

def test_validate_record():
    assert events.validate_record(_genesis()) == []
    assert events.validate_record("nope")  # not a dict -> errors
    assert any("event_id" in e for e in events.validate_record({"owner": "a", "status": "proposed"}))
    assert any("owner" in e for e in events.validate_record({"event_id": "e", "status": "proposed"}))
    assert any("status" in e for e in events.validate_record({"event_id": "e", "owner": "a", "status": "bogus"}))
    bad_parts = {"event_id": "e", "owner": "a", "status": "proposed", "participants": [{"role": "x"}]}
    assert any("participants" in e for e in events.validate_record(bad_parts))
    print("PASS: validate_record (required fields + status enum + participants shape)")


def test_fold_by_latest_tiebreak():
    recs = [
        {"event_id": "e1", "status": "proposed", "created_at": "2026-01-01T00:00:00"},
        {"event_id": "e1", "status": "in-progress", "created_at": "2026-01-01T00:00:05"},
        {"event_id": "e1", "status": "completed", "created_at": "2026-01-01T00:00:05"},  # same ts -> later wins
        {"event_id": "e2", "status": "proposed", "created_at": "2026-01-01T00:00:01"},
    ]
    folded = events._fold_by_latest(recs)
    assert folded["e1"]["status"] == "completed"  # later file-order wins on ts tie
    assert folded["e2"]["status"] == "proposed"
    print("PASS: fold_by_latest (latest created_at; later-on-tie)")


# ---- read tests (in-process, direct-write tmp file -- reads need no base_dir) ----

def test_reads():
    tmp = Path(tempfile.mkdtemp(prefix="events-read-"))
    try:
        f = tmp / "events.jsonl"
        _write_jsonl(f, [
            {"event_id": "e1", "owner": "alpha", "status": "proposed", "created_at": "2026-01-01T00:00:00", "completion_signals": ["s1"]},
            {"event_id": "e1", "owner": "alpha", "status": "completed", "created_at": "2026-01-01T00:01:00", "completion_signals": ["s1", "s2"]},
            {"event_id": "e2", "owner": "bravo", "status": "in-progress", "created_at": "2026-01-01T00:02:00", "completion_signals": []},
        ])
        assert events.read_event("e1", path=f)["status"] == "completed"
        assert events.read_event("missing", path=f) is None
        assert [r["event_id"] for r in events.read_by_status("completed", path=f)] == ["e1"]
        assert {r["event_id"] for r in events.list_active(path=f)} == {"e2"}  # e1 completed -> excluded
        cc = events.check_completion("e1", path=f)
        assert cc["status"] == "completed" and cc["completion_signals"] == ["s1", "s2"]
        assert events.check_completion("missing", path=f) is None
        print("PASS: reads (read_event/read_by_status/list_active/check_completion)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- full write-stack tests (subprocess, isolated MIND_WORLD) ----

def _run(args, world, stdin=None):
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = "test-agent-events"
    r = subprocess.run([sys.executable, str(EVENTS_PY), *args],
                       input=stdin, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _events_file(world):
    return Path(world) / "board" / "events.jsonl"


def test_cli_lifecycle_append_only():
    world = Path(tempfile.mkdtemp(prefix="events-world-"))
    try:
        rc, out, err = _run(["add"], world, stdin=json.dumps(_genesis("evt-cli", "proposed")))
        assert rc == 0, f"add rc={rc} err={err}"
        ef = _events_file(world)
        assert ef.exists(), "events.jsonl not created"
        assert _count_records(ef) == 1, "expected 1 record after add"

        # update-status MUST APPEND (event-sourced), not rewrite
        rc, out, err = _run(["update-status", "evt-cli", "--status", "in-progress"], world)
        assert rc == 0, f"update rc={rc} err={err}"
        assert _count_records(ef) == 2, "event-sourced update must APPEND a new record (expected 2)"

        rc, out, err = _run(["read", "--event-id", "evt-cli"], world)
        assert rc == 0
        rec = json.loads(out)
        assert rec["status"] == "in-progress", "read must fold-by-latest"
        assert rec["participants"] == [{"role": "implementer", "agent": "alpha"}], "prior fields copied"
        assert rec["recorded_by"] == "test-agent-events"

        rc, out, err = _run(["update-status", "evt-cli", "--status", "completed"], world)
        assert rc == 0
        assert _count_records(ef) == 3, "second transition appends a 3rd record"
        rc, out, err = _run(["list-active"], world)
        assert json.loads(out) == [], "completed is terminal -> not active"

        rc, out, err = _run(["check-completion", "evt-cli"], world)
        assert rc == 0 and json.loads(out)["status"] == "completed"
        print("PASS: CLI lifecycle (append-only event-sourcing 1->2->3 records, fold-by-latest)")
    finally:
        shutil.rmtree(world, ignore_errors=True)


def test_cli_exit_codes():
    world = Path(tempfile.mkdtemp(prefix="events-world-"))
    try:
        rc, out, err = _run(["add"], world, stdin=json.dumps({"event_id": "x"}))  # missing owner/status
        assert rc == 1, f"expected exit 1 on invalid add, got {rc}"
        rc, out, err = _run(["update-status", "nope", "--status", "completed"], world)
        assert rc == 2, f"expected exit 2 on missing event update, got {rc}"
        rc, out, err = _run(["check-completion", "nope"], world)
        assert rc == 2, f"expected exit 2 on missing check-completion, got {rc}"
        _run(["add"], world, stdin=json.dumps(_genesis("evt-dup")))
        rc, out, err = _run(["add"], world, stdin=json.dumps(_genesis("evt-dup")))
        assert rc == 1, f"expected exit 1 on duplicate add, got {rc}"
        print("PASS: CLI exit codes (1 invalid-input / 2 not-found / 1 duplicate)")
    finally:
        shutil.rmtree(world, ignore_errors=True)


def test_cli_overrides():
    world = Path(tempfile.mkdtemp(prefix="events-world-"))
    try:
        _run(["add"], world, stdin=json.dumps(_genesis("evt-ovr", "proposed")))
        rc, out, err = _run(["update-status", "evt-ovr", "--status", "completed",
                             "--overrides-json", json.dumps({"completion_signals": ["all done"]})], world)
        assert rc == 0, f"override rc={rc} err={err}"
        rec = json.loads(out)
        assert rec["completion_signals"] == ["all done"] and rec["status"] == "completed"
        print("PASS: CLI update-status --overrides-json")
    finally:
        shutil.rmtree(world, ignore_errors=True)


# ---- claim-role + re-bind tests (, child 3/3) ----

def test_claim_role_cli_append_idempotent():
    world = Path(tempfile.mkdtemp(prefix="events-claim-"))
    try:
        _run(["add"], world, stdin=json.dumps(_genesis("evt-claim", "in-progress")))
        ef = _events_file(world)
        assert _count_records(ef) == 1

        # First claim of a new (role, agent) APPENDS one record
        rc, out, err = _run(["claim-role", "evt-claim", "--role", "reviewer",
                             "--agent", "bravo"], world)
        assert rc == 0, f"claim rc={rc} err={err}"
        assert _count_records(ef) == 2, "first claim must append"
        rec = json.loads(out)
        assert {"role": "reviewer", "agent": "bravo"} in rec["participants"]
        assert {"role": "implementer", "agent": "alpha"} in rec["participants"], "genesis participant preserved"

        # Re-claiming the SAME (role, agent) is idempotent -> NO new record
        rc, out, err = _run(["claim-role", "evt-claim", "--role", "reviewer",
                             "--agent", "bravo"], world)
        assert rc == 0
        assert _count_records(ef) == 2, "idempotent re-claim must NOT append"

        # A DIFFERENT role for a DIFFERENT agent accumulates (no clobber)
        rc, out, err = _run(["claim-role", "evt-claim", "--role", "judge",
                             "--agent", "charlie"], world)
        assert rc == 0
        assert _count_records(ef) == 3
        rec = json.loads(out)
        pairs = {(p["role"], p["agent"]) for p in rec["participants"]}
        assert pairs == {("implementer", "alpha"), ("reviewer", "bravo"),
                         ("judge", "charlie")}, f"all claims accumulate: {pairs}"

        # One agent MAY hold a second role (dedup is per (role, agent) pair)
        rc, out, err = _run(["claim-role", "evt-claim", "--role", "analyst",
                             "--agent", "bravo"], world)
        assert rc == 0
        rec = json.loads(out)
        bravo_roles = {p["role"] for p in rec["participants"] if p["agent"] == "bravo"}
        assert bravo_roles == {"reviewer", "analyst"}, "agent can hold multiple roles"

        # claim-role on a missing event -> exit 2
        rc, out, err = _run(["claim-role", "evt-nope", "--role", "x", "--agent", "y"], world)
        assert rc == 2, f"expected exit 2 on missing event, got {rc}"
        print("PASS: claim-role (append, idempotent, multi-agent accumulation, multi-role, not-found)")
    finally:
        shutil.rmtree(world, ignore_errors=True)


def test_rebind_reads_held_roles():
    tmp = Path(tempfile.mkdtemp(prefix="events-rebind-"))
    try:
        f = tmp / "events.jsonl"
        _write_jsonl(f, [
            {"event_id": "evt-a", "owner": "alpha", "status": "in-progress",
             "participants": [{"role": "implementer", "agent": "alpha"},
                              {"role": "reviewer", "agent": "alpha"}],
             "created_at": "2026-01-01T00:00:00"},
            {"event_id": "evt-b", "owner": "bravo", "status": "claimed",
             "participants": [{"role": "analyst", "agent": "alpha"}],
             "created_at": "2026-01-01T00:01:00"},
            {"event_id": "evt-c", "owner": "charlie", "status": "completed",  # terminal -> excluded by default
             "participants": [{"role": "judge", "agent": "alpha"}],
             "created_at": "2026-01-01T00:02:00"},
            {"event_id": "evt-d", "owner": "bravo", "status": "in-progress",  # no alpha -> never returned
             "participants": [{"role": "implementer", "agent": "bravo"}],
             "created_at": "2026-01-01T00:03:00"},
        ])
        bound = events.rebind("alpha", path=f)
        assert [e["event_id"] for e in bound] == ["evt-a", "evt-b"], "non-terminal alpha events, sorted"
        assert bound[0]["roles"] == ["implementer", "reviewer"], "sorted roles"
        assert bound[1]["roles"] == ["analyst"] and bound[1]["status"] == "claimed"
        all_ids = [e["event_id"] for e in events.rebind("alpha", include_terminal=True, path=f)]
        assert all_ids == ["evt-a", "evt-b", "evt-c"], f"include_terminal surfaces completed: {all_ids}"
        assert events.rebind("delta", path=f) == [], "agent with no roles re-binds to nothing"
        print("PASS: rebind (held roles, terminal exclusion, include_terminal, empty)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_concurrent_claims_lock_safe():
    """N agents claim DISTINCT roles on ONE event simultaneously; ALL must
    survive. This is the load-bearing 'concurrent claims are lock-safe' check --
    a fold-outside-the-lock implementation (the update_status shape) loses all
    but one claim here, because each would copy the same prior snapshot."""
    import concurrent.futures
    world = Path(tempfile.mkdtemp(prefix="events-concur-"))
    try:
        _run(["add"], world, stdin=json.dumps(_genesis("evt-race", "in-progress")))
        N = 5

        def _claim(i):
            rc, out, err = _run(["claim-role", "evt-race", "--role", f"role{i}",
                                 "--agent", f"agent{i}"], world)
            return rc, err

        with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
            results = list(ex.map(_claim, range(N)))
        assert all(rc == 0 for rc, _ in results), f"all concurrent claims rc=0: {results}"

        rc, out, err = _run(["read", "--event-id", "evt-race"], world)
        rec = json.loads(out)
        pairs = {(p["role"], p["agent"]) for p in rec["participants"]}
        expected = {("implementer", "alpha")} | {(f"role{i}", f"agent{i}") for i in range(N)}
        assert pairs == expected, f"ALL {N} concurrent claims must survive; missing {expected - pairs}"
        print(f"PASS: concurrent claims lock-safe (all {N} distinct claims survived)")
    finally:
        shutil.rmtree(world, ignore_errors=True)


def main():
    tests = [test_validate_record, test_fold_by_latest_tiebreak, test_reads,
             test_cli_lifecycle_append_only, test_cli_exit_codes, test_cli_overrides,
             test_claim_role_cli_append_idempotent, test_rebind_reads_held_roles,
             test_concurrent_claims_lock_safe]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    if passed == len(tests):
        print(f"\nALL {passed} TESTS PASS -- events.jsonl access engine (append-only/event-sourced)")
        return 0
    print(f"\nFAIL: {len(tests) - passed}/{len(tests)} failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
