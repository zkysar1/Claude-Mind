"""Unit tests for coordination_merge — the commutative both-diverged merge
handlers used by OwnCloudBackend._put (gap #5, ).

Pure functions, no moto / no daemon / no I/O. The governing invariant under
test is COMMUTATIVITY: merge(a, b) must be BYTE-IDENTICAL to merge(b, a). That
byte-level symmetry is what makes the two machines converge (each computes the
merge from its own vantage and reaches the same result, so the fenced-PUT retry
loop terminates instead of ping-ponging).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402
yaml = pytest.importorskip("yaml")


def _rb(recs):
    return ("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs)).encode()


def _recs(blob):
    return [json.loads(l) for l in blob.decode().splitlines() if l.strip()]


# --- reasoning-bank ---------------------------------------------------------
def test_rb_disjoint_union_is_commutative():
    a = _rb([{"id": "rb-1", "created": "2026-07-02T10:00:00", "title": "base"},
             {"id": "rb-2", "created": "2026-07-02T11:00:00", "title": "a-new"}])
    b = _rb([{"id": "rb-1", "created": "2026-07-02T10:00:00", "title": "base"},
             {"id": "rb-3", "created": "2026-07-02T11:30:00", "title": "b-new"}])
    ab, ba = cm.merge_reasoning_bank(a, b), cm.merge_reasoning_bank(b, a)
    assert ab == ba                                   # byte-identical
    assert [r["id"] for r in _recs(ab)] == ["rb-1", "rb-2", "rb-3"]


def test_rb_true_collision_reids_and_preserves_both():
    # Two DISTINCT records (different `created`) allocated the same id rb-2 during
    # a lock stale-break. Both must survive; ids must stay unique.
    a = _rb([{"id": "rb-1", "created": "2026-07-02T10:00:00", "title": "base"},
             {"id": "rb-2", "created": "2026-07-02T11:00:00", "title": "machineA"}])
    b = _rb([{"id": "rb-1", "created": "2026-07-02T10:00:00", "title": "base"},
             {"id": "rb-2", "created": "2026-07-02T12:00:00", "title": "machineB"}])
    ab, ba = cm.merge_reasoning_bank(a, b), cm.merge_reasoning_bank(b, a)
    assert ab == ba
    recs = _recs(ab)
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))                  # ids unique post-merge
    titles = {r["title"] for r in recs}
    assert {"machineA", "machineB"} <= titles         # zero data loss
    # earlier-`created` keeps the contested id; later is re-id'd forward
    keep = next(r for r in recs if r["title"] == "machineA")
    moved = next(r for r in recs if r["title"] == "machineB")
    assert keep["id"] == "rb-2"
    assert moved["id"] == "rb-3"


def test_rb_same_record_field_merge_monotonic():
    # Same id AND same created => the SAME record edited on both machines.
    # utilization counters MAX; a retire dominates.
    a = _rb([{"id": "rb-5", "created": "2026-07-02T10:00:00", "status": "active",
              "utilization": {"times_helpful": 3, "retrieval_count": 8}}])
    b = _rb([{"id": "rb-5", "created": "2026-07-02T10:00:00", "status": "retired",
              "utilization": {"times_helpful": 5, "retrieval_count": 2}}])
    ab, ba = cm.merge_reasoning_bank(a, b), cm.merge_reasoning_bank(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["status"] == "retired"                 # retire dominates
    assert rec["utilization"]["times_helpful"] == 5   # max
    assert rec["utilization"]["retrieval_count"] == 8  # max


def test_rb_output_sorted_by_numeric_id():
    # Distinct records (distinct created+title identities) -> no collapse.
    a = _rb([{"id": "rb-10", "created": "t10", "title": "ten"},
             {"id": "rb-2", "created": "t2", "title": "two"}])
    b = _rb([{"id": "rb-1", "created": "t1", "title": "one"}])
    out = _recs(cm.merge_reasoning_bank(a, b))
    assert [r["id"] for r in out] == ["rb-1", "rb-2", "rb-10"]


def test_rb_multiround_collision_converges():
    """Regression: the collision re-id must CONVERGE across the multi-round
    cross-machine fenced-PUT loop, not duplicate the record each round. An
    earlier id-keyed merge grew machineB to 2x, 3x... every round B re-merged
    its stale local (which still held the record under its pre-re-id id)."""
    recA = {"id": "rb-2", "created": "2026-07-02T10:00:00", "title": "machineA"}
    recB = {"id": "rb-2", "created": "2026-07-02T11:00:00", "title": "machineB"}
    base = {"id": "rb-1", "created": "2026-07-02T09:00:00", "title": "base"}
    localA, localB = _rb([base, recA]), _rb([base, recB])
    s3 = cm.merge_reasoning_bank(localA, localB)          # A pushes
    for _ in range(4):                                     # B, A, B, A re-merge stale local
        s3_b = cm.merge_reasoning_bank(localB, s3)
        s3_a = cm.merge_reasoning_bank(localA, s3_b)
        assert s3_a == s3_b, "not converged: A and B disagree"
        s3 = s3_a
    titles = [r["title"] for r in _recs(s3)]
    assert titles.count("machineB") == 1, f"duplicated: {titles}"
    assert titles.count("machineA") == 1
    assert sorted(titles) == ["base", "machineA", "machineB"]


def test_rb_empty_inputs():
    assert cm.merge_reasoning_bank(b"", b"") == b""
    one = _rb([{"id": "rb-1", "created": "t"}])
    assert cm.merge_reasoning_bank(one, b"") == one
    assert cm.merge_reasoning_bank(b"", one) == one


# --- team-state -------------------------------------------------------------
def _ts(**kw):
    base = {
        "last_updated": None, "last_updated_by": None,
        "strategic_focus": {"primary": None, "set_at": None, "acknowledged_by": []},
        "active_blockers": [], "recent_completions": [], "agent_status": {},
        "critical_blockers": [], "inbox_alert_backlog": None,
    }
    base.update(kw)
    return yaml.dump(base, default_flow_style=False, sort_keys=False).encode()


def test_ts_agent_status_union_commutative():
    a = _ts(last_updated="2026-07-02T10:00:00", last_updated_by="echo",
            agent_status={"echo": {"last_active": "2026-07-02T10:00:00",
                                   "current_focus": "laneA"}})
    b = _ts(last_updated="2026-07-02T10:05:00", last_updated_by="zeta",
            agent_status={"zeta": {"last_active": "2026-07-02T10:05:00",
                                   "current_focus": "laneB"}})
    ab, ba = cm.merge_team_state(a, b), cm.merge_team_state(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert sorted(m["agent_status"]) == ["echo", "zeta"]   # both preserved
    assert m["last_updated"] == "2026-07-02T10:05:00"      # newer wins
    assert m["last_updated_by"] == "zeta"


def test_ts_same_agent_newer_last_active_wins():
    a = _ts(agent_status={"echo": {"last_active": "2026-07-02T10:00:00",
                                   "current_focus": "old"}})
    b = _ts(agent_status={"echo": {"last_active": "2026-07-02T10:09:00",
                                   "current_focus": "new"}})
    m = yaml.safe_load(cm.merge_team_state(a, b).decode())
    assert m["agent_status"]["echo"]["current_focus"] == "new"
    assert cm.merge_team_state(a, b) == cm.merge_team_state(b, a)


def test_ts_acknowledged_by_and_completions_union():
    a = _ts(last_updated="2026-07-02T10:00:00",
            strategic_focus={"primary": "X", "set_at": "2026-07-02T09:00:00",
                             "acknowledged_by": ["echo"]},
            recent_completions=[{"goal_id": "g-1", "completed_at": "2026-07-02T09:30:00"}])
    b = _ts(last_updated="2026-07-02T10:05:00",
            strategic_focus={"primary": "X", "set_at": "2026-07-02T09:00:00",
                             "acknowledged_by": ["zeta"]},
            recent_completions=[{"goal_id": "g-2", "completed_at": "2026-07-02T10:01:00"}])
    ab = cm.merge_team_state(a, b)
    assert ab == cm.merge_team_state(b, a)
    m = yaml.safe_load(ab.decode())
    assert m["strategic_focus"]["acknowledged_by"] == ["echo", "zeta"]
    assert sorted(c["goal_id"] for c in m["recent_completions"]) == ["g-1", "g-2"]


def test_ts_recent_completions_trims_to_ceiling():
    many_a = [{"goal_id": f"g-{i}", "completed_at": f"2026-07-02T10:{i:02d}:00"}
              for i in range(40)]
    many_b = [{"goal_id": f"g-{i}", "completed_at": f"2026-07-02T11:{i:02d}:00"}
              for i in range(40, 80)]
    m = yaml.safe_load(cm.merge_team_state(
        _ts(recent_completions=many_a), _ts(recent_completions=many_b)).decode())
    assert len(m["recent_completions"]) == cm._MAX_RECENT_COMPLETIONS
    # newest-first: the 11:xx entries (from b) come first
    assert m["recent_completions"][0]["goal_id"] == "g-79"


def test_ts_unknown_future_key_follows_winner():
    # Forward-compat: a key not special-cased rides along from the newer document.
    a = _ts(last_updated="2026-07-02T10:00:00")
    b = _ts(last_updated="2026-07-02T10:05:00")
    bd = yaml.safe_load(b.decode()); bd["future_field"] = {"z": 1}
    b2 = yaml.dump(bd, default_flow_style=False, sort_keys=False).encode()
    m = yaml.safe_load(cm.merge_team_state(a, b2).decode())
    assert m["future_field"] == {"z": 1}


def test_ts_active_blockers_union_dedup_by_id():
    a = _ts(active_blockers=[{"id": "blk-1", "note": "x"}])
    b = _ts(active_blockers=[{"id": "blk-1", "note": "x"},
                             {"id": "blk-2", "note": "y"}])
    m = yaml.safe_load(cm.merge_team_state(a, b).decode())
    assert sorted(x["id"] for x in m["active_blockers"]) == ["blk-1", "blk-2"]


# --- aspirations.jsonl (1 follow-up) -------------------------------
def _asp(aid, goals, **kw):
    a = {"id": aid, "status": "active", "last_selected": "2026-07-03T09:00:00",
         "selection_count": 1, "sessions_active": 1, "goals": goals}
    a.update(kw)
    return a


def _goal(gid, **kw):
    g = {"id": gid, "status": "pending", "recurring": False,
         "last_modified": "2026-07-03T10:00:00", "created_at": "2026-07-01T00:00:00"}
    g.update(kw)
    return g


def _goal_ids(blob):
    return {g["id"] for a in _recs(blob) for g in a.get("goals", [])}


def _find_goal(blob, gid):
    for a in _recs(blob):
        for g in a.get("goals", []):
            if g["id"] == gid:
                return g
    return None


def test_aspirations_disjoint_goals_commutative():
    a = _rb([_asp("asp-1", [_goal("g-1-1", status="completed")])])
    b = _rb([_asp("asp-1", [_goal("g-1-2", status="in-progress")])])
    ab, ba = cm.merge_aspirations(a, b), cm.merge_aspirations(b, a)
    assert ab == ba                                     # byte-identical
    assert _goal_ids(ab) == {"g-1-1", "g-1-2"}          # no goal loss


def test_aspirations_no_goal_loss_two_agent_closes():
    # The freeze fix: two agents close DIFFERENT goals; a union loses neither.
    a = _rb([_asp("asp-115", [_goal("g-115-105", recurring=True, status="pending",
                                    lastAchievedAt="2026-07-03T13:05:00",
                                    last_modified="2026-07-03T13:05:00")])])
    b = _rb([_asp("asp-115", [_goal("g-115-999", status="completed",
                                    last_modified="2026-07-03T13:10:00")])])
    assert _goal_ids(cm.merge_aspirations(a, b)) == {"g-115-105", "g-115-999"}


def test_aspirations_lastachievedat_advance_preserved():
    # The core freeze harm: a recurring close's lastAchievedAt advance must not be
    # rolled back by a stale-base remote (strictly-newer wins, MAX on achievedCount).
    local = _rb([_asp("asp-1", [_goal("g-1-1", recurring=True, status="pending",
                                      lastAchievedAt="2026-07-03T13:26:00", achievedCount=10,
                                      last_modified="2026-07-03T13:26:00")])])
    remote = _rb([_asp("asp-1", [_goal("g-1-1", recurring=True, status="pending",
                                       lastAchievedAt="2026-07-02T08:08:17", achievedCount=9,
                                       last_modified="2026-07-02T08:08:17")])])
    g = _find_goal(cm.merge_aspirations(local, remote), "g-1-1")
    assert g["lastAchievedAt"] == "2026-07-03T13:26:00"
    assert g["achievedCount"] == 10


def test_aspirations_nonrecurring_terminal_dominates():
    # delta 2026-07-03 07:19 bug: a NEWER in-progress must NOT revert a completed.
    comp = _rb([_asp("asp-1", [_goal("g-1-1", status="completed",
                                     last_modified="2026-07-03T10:00:00")])])
    inprog = _rb([_asp("asp-1", [_goal("g-1-1", status="in-progress",
                                       last_modified="2026-07-03T11:00:00")])])
    assert _find_goal(cm.merge_aspirations(comp, inprog), "g-1-1")["status"] == "completed"
    assert _find_goal(cm.merge_aspirations(inprog, comp), "g-1-1")["status"] == "completed"


def test_aspirations_recurring_cycle_pending_flip_wins():
    # A recurring goal's recover-recurring pending flip (newer last_modified) must
    # win over a stale completed — recurring status CYCLES, so no terminal-dominance.
    done = _rb([_asp("asp-1", [_goal("g-1-1", recurring=True, status="completed",
                                     lastAchievedAt="2026-07-03T13:00:00",
                                     last_modified="2026-07-03T13:00:00")])])
    flip = _rb([_asp("asp-1", [_goal("g-1-1", recurring=True, status="pending",
                                     lastAchievedAt="2026-07-03T13:00:00",
                                     last_modified="2026-07-03T13:30:00")])])
    g = _find_goal(cm.merge_aspirations(done, flip), "g-1-1")
    assert g["status"] == "pending"
    assert g["lastAchievedAt"] == "2026-07-03T13:00:00"   # achievement kept


def test_aspirations_multiround_convergence():
    # merge(merge(a,b), b) == merge(a,b): the fenced-PUT retry loop terminates.
    a = _rb([_asp("asp-1", [_goal("g-1-1", status="completed",
                                  last_modified="2026-07-03T11:00:00", achievedCount=5)])])
    b = _rb([_asp("asp-1", [_goal("g-1-1", status="in-progress",
                                  last_modified="2026-07-03T10:00:00", achievedCount=4)])])
    m1 = cm.merge_aspirations(a, b)
    assert cm.merge_aspirations(m1, b) == m1
    assert cm.merge_aspirations(a, m1) == m1


# --- guardrails (id-keyed like reasoning-bank; 3-pad ids + times_triggered) --
def _guard(gid, **kw):
    g = {"id": gid, "created": "2026-07-02T10:00:00", "rule": f"rule-{gid}",
         "status": "active"}
    g.update(kw)
    return g


def test_guard_disjoint_union_commutative():
    # Two agents add DIFFERENT guardrails; the union loses neither, byte-identical.
    a = _rb([_guard("guard-001", rule="base"), _guard("guard-054", rule="a-new")])
    b = _rb([_guard("guard-001", rule="base"), _guard("guard-104", rule="b-new")])
    ab, ba = cm.merge_guardrails(a, b), cm.merge_guardrails(b, a)
    assert ab == ba                                       # byte-identical
    assert [r["id"] for r in _recs(ab)] == ["guard-001", "guard-054", "guard-104"]


def test_guard_zero_pad_id_preserved():
    # CRITICAL: guard ids are 3-pad on disk. The re-stamp must NOT reformat
    # guard-001 -> guard-1 (that would churn every id, break references, and
    # never byte-converge). Distinct identities -> no collapse; ids untouched.
    a = _rb([_guard("guard-001", rule="one"), _guard("guard-009", rule="nine")])
    b = _rb([_guard("guard-912", rule="hi")])
    out = _recs(cm.merge_guardrails(a, b))
    assert [r["id"] for r in out] == ["guard-001", "guard-009", "guard-912"]
    assert all(len(r["id"].split("-")[1]) >= 3 for r in out)   # zero-pad retained


def test_guard_same_record_field_merge():
    # Same (created, rule) => SAME guardrail edited on both machines: retire
    # dominates; utilization counters MAX; top-level times_triggered MAX (the
    # counter reasoning-bank lacks); valid_to set-dominates-null.
    a = _rb([_guard("guard-005", rule="R", status="active", times_triggered=2,
                    valid_to=None,
                    utilization={"times_active": 7, "retrieval_count": 3})])
    b = _rb([_guard("guard-005", rule="R", status="retired", times_triggered=9,
                    valid_to="2026-07-03",
                    utilization={"times_active": 4, "retrieval_count": 8})])
    ab, ba = cm.merge_guardrails(a, b), cm.merge_guardrails(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["status"] == "retired"                     # retire dominates
    assert rec["times_triggered"] == 9                    # top-level MAX (not "2">"9")
    assert rec["valid_to"] == "2026-07-03"                # set dominates null
    assert rec["utilization"]["times_active"] == 7        # counter MAX
    assert rec["utilization"]["retrieval_count"] == 8     # counter MAX


def test_guard_true_collision_reids_zero_pad():
    # Two DISTINCT guards (different created) allocated the same guard-005 in a
    # lock stale-break. Both survive; the displaced one re-ids forward, 3-padded.
    a = _rb([_guard("guard-005", created="2026-07-02T10:00:00", rule="machineA")])
    b = _rb([_guard("guard-005", created="2026-07-02T12:00:00", rule="machineB")])
    ab, ba = cm.merge_guardrails(a, b), cm.merge_guardrails(b, a)
    assert ab == ba
    recs = _recs(ab)
    assert {"machineA", "machineB"} <= {r["rule"] for r in recs}   # zero data loss
    ids = [r["id"] for r in recs]
    assert len(ids) == len(set(ids))                      # unique
    keep = next(r for r in recs if r["rule"] == "machineA")
    moved = next(r for r in recs if r["rule"] == "machineB")
    assert keep["id"] == "guard-005"                      # earlier-created keeps
    assert moved["id"] == "guard-006"                     # re-id'd forward, 3-pad


def test_guard_multiround_convergence():
    # The fenced-PUT retry loop must TERMINATE (converge, not duplicate).
    recA = {"id": "guard-005", "created": "2026-07-02T10:00:00", "rule": "machineA"}
    recB = {"id": "guard-005", "created": "2026-07-02T11:00:00", "rule": "machineB"}
    base = {"id": "guard-001", "created": "2026-07-02T09:00:00", "rule": "base"}
    localA, localB = _rb([base, recA]), _rb([base, recB])
    s3 = cm.merge_guardrails(localA, localB)
    for _ in range(4):
        s3_b = cm.merge_guardrails(localB, s3)
        s3_a = cm.merge_guardrails(localA, s3_b)
        assert s3_a == s3_b, "not converged"
        s3 = s3_a
    rules = [r["rule"] for r in _recs(s3)]
    assert rules.count("machineB") == 1                   # not duplicated
    assert sorted(rules) == ["base", "machineA", "machineB"]


def test_guard_empty_inputs():
    assert cm.merge_guardrails(b"", b"") == b""
    one = _rb([_guard("guard-001", rule="x")])
    assert cm.merge_guardrails(one, b"") == one
    assert cm.merge_guardrails(b"", one) == one


# --- append-only logs (line-union) ------------------------------------------
def _log(ts, **kw):
    r = {"ts": ts}
    r.update(kw)
    return r


def test_append_only_disjoint_union_commutative():
    # Two machines each append a DIFFERENT event on top of a shared baseline;
    # the union keeps all three, byte-identical regardless of arg order.
    base = _log("2026-07-03T10:00:00", gate_id="g0", decision="allow")
    a = _rb([base, _log("2026-07-03T11:00:00", gate_id="gA", agent="alpha")])
    b = _rb([base, _log("2026-07-03T11:05:00", gate_id="gB", agent="bravo")])
    ab, ba = cm.merge_append_only_jsonl(a, b), cm.merge_append_only_jsonl(b, a)
    assert ab == ba                                          # byte-identical
    assert [r["gate_id"] for r in _recs(ab)] == ["g0", "gA", "gB"]  # chronological


def test_append_only_baseline_dedup():
    # THE freeze-fix behavior: the already-synced baseline prefix is present in
    # BOTH blobs and MUST collapse to one copy (never be duplicated). 3 shared +
    # 1 local + 1 remote = 5 unique, NOT 3+3=6.
    shared = [_log("2026-07-03T09:00:00", n=1), _log("2026-07-03T09:01:00", n=2),
              _log("2026-07-03T09:02:00", n=3)]
    a = _rb(shared + [_log("2026-07-03T10:00:00", n=4, who="a")])
    b = _rb(shared + [_log("2026-07-03T10:01:00", n=5, who="b")])
    out = _recs(cm.merge_append_only_jsonl(a, b))
    assert len(out) == 5                                     # baseline NOT duplicated
    assert [r["n"] for r in out] == [1, 2, 3, 4, 5]


def test_append_only_chronological_sort_mixed_fields():
    # Output is chronological by the first-present ts field — ts / timestamp /
    # date all recognized — so the merged log keeps append order (and hygiene's
    # 'keep newest' cap/rotate stays valid). Mix three field names + arg order.
    a = _rb([{"timestamp": "2026-07-03T12:00:00", "x": "noon"},
             {"date": "2026-07-01", "x": "day1"}])
    b = _rb([{"ts": "2026-07-03T08:00:00", "x": "morning"}])
    out = _recs(cm.merge_append_only_jsonl(a, b))
    # day1 (date-only 2026-07-01) < morning (08:00) < noon (12:00)
    assert [r["x"] for r in out] == ["day1", "morning", "noon"]


def test_append_only_no_field_merge_distinct_events():
    # Contrast the id-keyed stores: an append-only record is NEVER edited, so two
    # records that share an id-like field but differ in content are DISTINCT
    # events and BOTH survive (a field-merge would wrongly collapse them).
    a = _rb([{"ts": "2026-07-03T10:00:00", "gate_id": "g1", "decision": "allow"}])
    b = _rb([{"ts": "2026-07-03T10:00:05", "gate_id": "g1", "decision": "deny"}])
    out = _recs(cm.merge_append_only_jsonl(a, b))
    assert len(out) == 2                                     # both kept, not merged
    assert {r["decision"] for r in out} == {"allow", "deny"}


def test_append_only_multiround_convergence():
    # Fenced-PUT retry loop must TERMINATE — re-merging is a fixpoint, no dup.
    base = _log("2026-07-03T09:00:00", n=0)
    a = _rb([base, _log("2026-07-03T10:00:00", n=1, who="a")])
    b = _rb([base, _log("2026-07-03T10:00:00", n=2, who="b")])
    s3 = cm.merge_append_only_jsonl(a, b)
    for _ in range(4):
        s3_b = cm.merge_append_only_jsonl(b, s3)
        s3_a = cm.merge_append_only_jsonl(a, s3_b)
        assert s3_a == s3_b, "not converged"
        s3 = s3_a
    whos = [r.get("who") for r in _recs(s3)]
    assert whos.count("a") == 1 and whos.count("b") == 1     # neither duplicated


def test_append_only_byte_exact_writer_format():
    # Merged bytes must match the writers' json.dumps(rec, ensure_ascii=True)+"\n"
    # exactly (else every merge flips the file style and churns the sync manifest).
    recs = [_log("2026-07-03T10:00:00", gate_id="g1"),
            _log("2026-07-03T11:00:00", gate_id="g2")]
    out = cm.merge_append_only_jsonl(_rb(recs), b"")
    assert out == cm._dump_jsonl(_recs(out))                 # re-dump round-trips
    assert out == _rb(sorted(recs, key=lambda r: r["ts"]))   # exact writer bytes


def test_append_only_empty_inputs():
    assert cm.merge_append_only_jsonl(b"", b"") == b""
    one = _rb([_log("2026-07-03T10:00:00", gate_id="g1")])
    assert cm.merge_append_only_jsonl(one, b"") == one
    assert cm.merge_append_only_jsonl(b"", one) == one


# --- field-level YAML/JSON reconcile (module-health.yaml, aspirations-meta.json,
#     ) ------------------------------------------------------------------
def _mh(modules):
    # byte-exact to module_health.save_module_health (default Dumper, width=200)
    return yaml.dump({"modules": modules}, default_flow_style=False, sort_keys=False,
                     allow_unicode=True, width=200).encode()


def _meta(doc):
    # byte-exact to aspirations_write meta_update (json indent=2 + trailing newline)
    return (json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode()


def _mod(ti, su, fa=0, nu=0, sr=None, lat=0.0):
    return {"total_invocations": ti, "successful": su, "failed": fa,
            "null_returns": nu, "avg_latency_ms": lat,
            "success_rate": (round(su / ti, 4) if ti else 0.0) if sr is None else sr}


def test_module_health_union_commutative():
    a = _mh({"mod-a": _mod(12, 10, 2), "mod-b": _mod(3, 3), "mod-c": _mod(1, 1)})
    b = _mh({"mod-a": _mod(11, 8, 3), "mod-b": _mod(3, 3), "mod-d": _mod(2, 2)})
    ab, ba = cm.merge_module_health(a, b), cm.merge_module_health(b, a)
    assert ab == ba                                          # byte-identical
    md = yaml.safe_load(ab.decode())["modules"]
    assert set(md) == {"mod-a", "mod-b", "mod-c", "mod-d"}   # union, zero loss
    assert list(md) == sorted(md)                            # emitted sorted by id


def test_module_health_counter_max_and_success_rate_recompute():
    # local +2 success on mod; remote +1 with a failure. Counters MAX; the
    # DERIVED rate is recomputed from merged counters (not a blind MAX on 0.83).
    a = _mh({"m": _mod(12, 10, 2, sr=0.8333)})
    b = _mh({"m": _mod(11, 8, 3, sr=0.7273)})
    md = yaml.safe_load(cm.merge_module_health(a, b).decode())["modules"]["m"]
    assert md["total_invocations"] == 12 and md["successful"] == 10 and md["failed"] == 3
    assert md["success_rate"] == round(10 / 12, 4)           # 0.8333, recomputed


def test_module_health_byte_exact_writer_format():
    merged = cm.merge_module_health(_mh({"z": _mod(2, 2)}), _mh({"a": _mod(1, 1)}))
    reparse = yaml.safe_load(merged.decode())
    assert merged == yaml.dump(reparse, default_flow_style=False, sort_keys=False,
                               allow_unicode=True, width=200).encode()


def test_module_health_multiround_convergence():
    a = _mh({"m": _mod(12, 10, 2), "c": _mod(1, 1)})
    b = _mh({"m": _mod(11, 8, 3), "d": _mod(2, 2)})
    merged = cm.merge_module_health(a, b)
    assert cm.merge_module_health(merged, b) == merged       # fixpoint
    assert cm.merge_module_health(a, merged) == merged


def test_module_health_empty_side():
    one = _mh({"m": _mod(1, 1)})
    got = yaml.safe_load(cm.merge_module_health(one, b"modules: {}\n").decode())["modules"]
    assert got == yaml.safe_load(one.decode())["modules"]


def _base_meta(**over):
    d = {"last_updated": "2026-07-03", "last_evolution": "2026-06-23",
         "session_count": 98, "readiness_gates": {}, "annecs_solved": 13,
         "confidence_calibration_bias": "underconfident",
         "tree.last_maintain_at": "2026-04-27T15:47:42",
         "calibration_finding": "AAA", "last_calibration_check": "2026-06-25T08:20:21"}
    d.update(over)
    return d


def test_aspirations_meta_lww_base_and_monotonic():
    a = _meta(_base_meta())
    b = _meta(_base_meta(last_updated="2026-07-04", session_count=101, annecs_solved=12,
                         confidence_calibration_bias="calibrated",
                         last_calibration_check="2026-07-01T10:00:00"))
    ab, ba = cm.merge_aspirations_meta(a, b), cm.merge_aspirations_meta(b, a)
    assert ab == ba
    d = json.loads(ab.decode())
    assert d["confidence_calibration_bias"] == "calibrated"  # LWW base = newer last_updated
    assert d["session_count"] == 101                         # MAX
    assert d["annecs_solved"] == 13                          # MAX (base is the smaller here)
    assert d["last_updated"] == "2026-07-04"                 # newer wins
    assert d["last_calibration_check"] == "2026-07-01T10:00:00"


def test_aspirations_meta_byte_exact_trailing_newline():
    merged = cm.merge_aspirations_meta(_meta(_base_meta()),
                                       _meta(_base_meta(last_updated="2026-07-04")))
    d = json.loads(merged.decode())
    assert merged == (json.dumps(d, indent=2, ensure_ascii=True) + "\n").encode()
    assert merged.endswith(b"}\n")                           # writer's trailing newline


def test_aspirations_meta_same_day_tiebreak_commutative():
    # equal last_updated -> content-tiebreak base; session_count still MAX both ways
    a = _meta(_base_meta(session_count=200, calibration_finding="ZZZ"))
    b = _meta(_base_meta(session_count=150, calibration_finding="AAA"))
    assert cm.merge_aspirations_meta(a, b) == cm.merge_aspirations_meta(b, a)
    assert json.loads(cm.merge_aspirations_meta(a, b).decode())["session_count"] == 200


def test_aspirations_meta_readiness_gates_union():
    a = _meta(_base_meta(readiness_gates={"g1": True}))
    b = _meta(_base_meta(readiness_gates={"g2": False}, last_updated="2026-07-04"))
    assert cm.merge_aspirations_meta(a, b) == cm.merge_aspirations_meta(b, a)
    assert json.loads(cm.merge_aspirations_meta(a, b).decode())["readiness_gates"] == \
        {"g1": True, "g2": False}                            # per-key union, zero loss


def test_aspirations_meta_multiround_and_empty():
    a = _meta(_base_meta(session_count=200))
    b = _meta(_base_meta(session_count=150, last_updated="2026-07-04"))
    merged = cm.merge_aspirations_meta(a, b)
    assert cm.merge_aspirations_meta(merged, b) == merged    # fixpoint
    one = _meta(_base_meta(session_count=99))
    assert json.loads(cm.merge_aspirations_meta(one, b"{}").decode())["session_count"] == 99


# --- registry ---------------------------------------------------------------
# --- pipeline (7 / rb-2849 — the cc-04 NON-multipart no_clobber freeze) ---
def _hyp(rec_id, **kw):
    base = {"id": rec_id, "title": f"hyp {rec_id}", "stage": "active",
            "horizon": "short", "type": "calibration", "confidence": 0.5,
            "position": "YES — a multi-word testable claim here",
            "formed_date": rec_id[:10], "category": "framework-architecture",
            "outcome": None, "reflected": False, "surprise": None}
    base.update(kw)
    return base


def test_pipeline_disjoint_union_commutative():
    a = _rb([_hyp("2026-07-01_base"), _hyp("2026-07-05_machine-a")])
    b = _rb([_hyp("2026-07-01_base"), _hyp("2026-07-06_machine-b")])
    ab, ba = cm.merge_pipeline(a, b), cm.merge_pipeline(b, a)
    assert ab == ba                                   # byte-identical
    assert [r["id"] for r in _recs(ab)] == [
        "2026-07-01_base", "2026-07-05_machine-a", "2026-07-06_machine-b"]


def test_pipeline_resolution_never_reverted():
    # Machine A RESOLVED the hypothesis; machine B concurrently bumped
    # last_reviewed on its stale ACTIVE copy. The merge must keep the
    # resolution (stage-monotonic base) AND B's newer review timestamp.
    a = _rb([_hyp("2026-07-01_x", stage="resolved", outcome="CONFIRMED",
                  outcome_date="2026-07-05", surprise=3,
                  last_reviewed="2026-07-05")])
    b = _rb([_hyp("2026-07-01_x", stage="active",
                  last_reviewed="2026-07-06")])
    ab, ba = cm.merge_pipeline(a, b), cm.merge_pipeline(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["stage"] == "resolved"                 # never reverted
    assert rec["outcome"] == "CONFIRMED"
    assert rec["surprise"] == 3
    assert rec["last_reviewed"] == "2026-07-06"       # B's newer bump kept


def test_pipeline_reflected_flag_monotonic():
    # reflected=True on either side survives the merge (the reflect flag is
    # monotonic — this is exactly the frozen-write class from cc-04: the 5
    # blocked H1 reflected-retries).
    a = _rb([_hyp("2026-07-01_x", stage="resolved", outcome="CORRECTED",
                  reflected=True)])
    b = _rb([_hyp("2026-07-01_x", stage="resolved", outcome="CORRECTED",
                  reflected=False, last_reviewed="2026-07-07")])
    ab, ba = cm.merge_pipeline(a, b), cm.merge_pipeline(b, a)
    assert ab == ba
    assert _recs(ab)[0]["reflected"] is True


def test_pipeline_side_only_fields_unioned_and_formed_date_older_wins():
    a = _rb([_hyp("2026-07-01_x", formed_date="2026-07-01",
                  measurement_channel="efs session logs")])
    b = _rb([_hyp("2026-07-01_x", formed_date="2026-07-02",
                  resolves_by="2026-08-01")])
    ab, ba = cm.merge_pipeline(a, b), cm.merge_pipeline(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["measurement_channel"] == "efs session logs"  # side-only kept
    assert rec["resolves_by"] == "2026-08-01"                # side-only kept
    assert rec["formed_date"] == "2026-07-01"                # older wins


def test_pipeline_multiround_convergence():
    recA = _hyp("2026-07-05_machine-a")
    recB = _hyp("2026-07-06_machine-b")
    base = _hyp("2026-07-01_base")
    localA, localB = _rb([base, recA]), _rb([base, recB])
    s3 = cm.merge_pipeline(localA, localB)
    for _ in range(4):
        s3_b = cm.merge_pipeline(localB, s3)
        s3_a = cm.merge_pipeline(localA, s3_b)
        assert s3_a == s3_b, "not converged: A and B disagree"
        s3 = s3_a
    ids = [r["id"] for r in _recs(s3)]
    assert sorted(ids) == ids and len(ids) == len(set(ids)) == 3


def test_pipeline_intra_blob_duplicates_fold_commutatively():
    # The LIVE pipeline-archive.jsonl carries duplicate-id lines WITHIN one
    # file (5 byte-identical groups from historical re-appends — 2026-07-07
    # probe). With a differing copy on the peer side, a naive
    # encounter-ordered fold would make the merge depend on the
    # (local, remote) argument order; the content-sorted fold keeps it
    # commutative AND collapses the identical dup lines for free.
    dup = _hyp("2026-07-01_x", stage="archived", outcome="EXPIRED")
    a = _rb([dup, dup, dup])                      # one side: 3 identical copies
    b = _rb([_hyp("2026-07-01_x", stage="archived", outcome="EXPIRED",
                  reflected=True, last_reviewed="2026-07-06")])
    ab, ba = cm.merge_pipeline(a, b), cm.merge_pipeline(b, a)
    assert ab == ba
    recs = _recs(ab)
    assert len(recs) == 1                         # dups collapsed, peer merged
    assert recs[0]["reflected"] is True
    assert recs[0]["last_reviewed"] == "2026-07-06"


def test_spark_intra_blob_duplicates_fold_commutatively():
    dup = _sq("sq-001", "Q1?", times_asked=5)
    a = _rb([dup, dup])
    b = _rb([_sq("sq-001", "Q1?", times_asked=9)])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    recs = _recs(ab)
    assert len(recs) == 1 and recs[0]["times_asked"] == 9


def test_pipeline_byte_exact_writer_format():
    # Output records must match the writers' exact per-record bytes
    # (json.dumps(rec, ensure_ascii=True) + "\n" — pipeline.py locked_*_jsonl
    # AND pipeline_write._atomic_write_jsonl).
    rec = _hyp("2026-07-01_x", title="unicode — em-dash")
    out = cm.merge_pipeline(_rb([rec]), b"")
    lines = out.decode("utf-8").splitlines()
    assert lines == [json.dumps(r, ensure_ascii=True) for r in _recs(out)]
    assert "\\u2014" in out.decode("utf-8")           # ensure_ascii honored


def test_pipeline_empty_inputs():
    assert cm.merge_pipeline(b"", b"") == b""
    one = _rb([_hyp("2026-07-01_x")])
    assert cm.merge_pipeline(one, b"") == one
    assert cm.merge_pipeline(b"", one) == one


# --- spark-questions (rb-2849 — frozen alongside pipeline.jsonl) -------------
def _sq(rec_id, text, **kw):
    base = {"id": rec_id, "text": text, "times_asked": 0,
            "sparks_generated": 0, "yield_rate": 0.0, "status": "active",
            "category": "discovery", "type": "question"}
    base.update(kw)
    return base


def _sqc(rec_id, text, **kw):
    base = {"id": rec_id, "text": text, "category": "discovery",
            "type": "candidate", "proposed_session": 1}
    base.update(kw)
    return base


def test_spark_disjoint_union_commutative():
    a = _rb([_sq("sq-001", "Q1?"), _sq("sq-002", "Q2?")])
    b = _rb([_sq("sq-001", "Q1?"), _sqc("sq-c01", "C1?")])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    assert [r["id"] for r in _recs(ab)] == ["sq-001", "sq-002", "sq-c01"]


def test_spark_counter_max_and_yield_recompute():
    a = _rb([_sq("sq-001", "Q1?", times_asked=1250, sparks_generated=33,
                 yield_rate=0.0264)])
    b = _rb([_sq("sq-001", "Q1?", times_asked=1274, sparks_generated=30,
                 yield_rate=0.0235)])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["times_asked"] == 1274                 # max never regresses
    assert rec["sparks_generated"] == 33              # max never regresses
    assert rec["yield_rate"] == round(33 / 1274, 4)   # derived: recomputed


def test_spark_promote_collapses_stale_candidate():
    # Machine A promoted candidate sq-c07 -> question sq-021 (promote rewrites
    # id + type, resets counters, keeps text). Machine B still holds the stale
    # candidate. The text-identity union collapses them to ONE question —
    # the candidate must NOT resurrect as a duplicate.
    a = _rb([_sq("sq-021", "New question?", times_asked=2)])
    b = _rb([_sqc("sq-c07", "New question?", proposed_session=42)])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    recs = _recs(ab)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["id"] == "sq-021" and rec["type"] == "question"
    assert rec["times_asked"] == 2                    # promoted side wholesale
    assert "proposed_session" not in rec              # candidate fields don't bleed


def test_spark_retired_dominates():
    a = _rb([_sq("sq-001", "Q1?", status="retired")])
    b = _rb([_sq("sq-001", "Q1?", status="active", times_asked=9)])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["status"] == "retired" and rec["times_asked"] == 9


def test_spark_true_id_collision_reids_per_family():
    # Two DISTINCT texts allocated the same candidate id during a lock
    # stale-break: both survive; the displaced one is re-id'd within its OWN
    # family (sq-cNN 2-pad), never into the question sequence.
    a = _rb([_sqc("sq-c07", "Candidate A?")])
    b = _rb([_sqc("sq-c07", "Candidate B?")])
    ab, ba = cm.merge_spark_questions(a, b), cm.merge_spark_questions(b, a)
    assert ab == ba
    recs = _recs(ab)
    ids = sorted(r["id"] for r in recs)
    assert len(recs) == 2 and ids == ["sq-c07", "sq-c08"]
    assert {r["text"] for r in recs} == {"Candidate A?", "Candidate B?"}


def test_spark_multiround_convergence_and_empty():
    localA = _rb([_sq("sq-001", "Q1?", times_asked=5)])
    localB = _rb([_sq("sq-001", "Q1?", times_asked=7), _sqc("sq-c01", "C1?")])
    s3 = cm.merge_spark_questions(localA, localB)
    for _ in range(3):
        s3_b = cm.merge_spark_questions(localB, s3)
        s3_a = cm.merge_spark_questions(localA, s3_b)
        assert s3_a == s3_b
        s3 = s3_a
    assert [r["id"] for r in _recs(s3)] == ["sq-001", "sq-c01"]
    assert _recs(s3)[0]["times_asked"] == 7
    assert cm.merge_spark_questions(b"", b"") == b""
    one = _rb([_sq("sq-001", "Q1?")])
    assert cm.merge_spark_questions(one, b"") == one


# --- pipeline-meta.json (7 — rewritten by every pipeline mutation) ---
def _pmeta(last_updated, micro=None, **kw):
    d = {"last_updated": last_updated,
         "stage_counts": {"discovered": 1, "active": 2,
                          "measurement-pending": 0, "resolved": 3,
                          "archived": 4},
         "accuracy": {"total_resolved": 3, "confirmed": 2, "corrected": 1,
                      "accuracy_pct": 66.7}}
    if micro is not None:
        d["micro_hypothesis_stats"] = micro
    d.update(kw)
    return (json.dumps(d, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def test_pipeline_meta_lww_and_micro_stats_union():
    a = _pmeta("2026-07-05", micro={"batch_a": {"processed": 4}})
    b = _pmeta("2026-07-06", micro={"batch_b": {"processed": 9}})
    ab, ba = cm.merge_pipeline_meta(a, b), cm.merge_pipeline_meta(b, a)
    assert ab == ba
    out = json.loads(ab.decode("utf-8"))
    assert out["last_updated"] == "2026-07-06"        # newer snapshot is base
    assert set(out["micro_hypothesis_stats"]) == {"batch_a", "batch_b"}  # union


def test_pipeline_meta_byte_exact_and_same_day_tiebreak():
    a = _pmeta("2026-07-06", accuracy={"total_resolved": 5})
    b = _pmeta("2026-07-06")
    ab, ba = cm.merge_pipeline_meta(a, b), cm.merge_pipeline_meta(b, a)
    assert ab == ba                                   # same-day: content tiebreak
    assert ab.endswith(b"\n")                          # writer's trailing newline
    json.loads(ab.decode("utf-8"))                     # valid indent-2 JSON
    assert cm.merge_pipeline_meta(a, b"") == cm.merge_pipeline_meta(b"", a)


def test_handler_registry_pipeline_spark_basenames():
    # Lock the 7 / rb-2849 registrations — an accidental removal
    # re-opens the cc-04 non-multipart both-diverged write-freeze.
    assert cm.merge_handler_for("world/pipeline.jsonl") is cm.merge_pipeline
    assert cm.merge_handler_for("world/pipeline-archive.jsonl") is cm.merge_pipeline
    assert cm.merge_handler_for("world/pipeline-meta.json") is cm.merge_pipeline_meta
    assert cm.merge_handler_for("meta/spark-questions.jsonl") is cm.merge_spark_questions
    # pattern-signatures.jsonl is DELIBERATELY unregistered in the 7
    # pass: its two live writers disagree on serialization (CLI
    # pattern-signatures.py emits ensure_ascii=False; the daemon store endpoint
    # emits ensure_ascii=True), so the byte-exact contract cannot be satisfied
    # until that writer split is reconciled (follow-up filed via BRD notes).
    # Locking None keeps a future edit from registering it half-verified.
    assert cm.merge_handler_for("world/pattern-signatures.jsonl") is None


def test_handler_registry_by_basename():
    assert cm.merge_handler_for("world/reasoning-bank.jsonl") is cm.merge_reasoning_bank
    assert cm.merge_handler_for("world/guardrails.jsonl") is cm.merge_guardrails
    assert cm.merge_handler_for("/abs/path/team-state.yaml") is cm.merge_team_state
    assert cm.merge_handler_for("world/aspirations.jsonl") is cm.merge_aspirations
    assert cm.merge_handler_for("random.txt") is None


def test_handler_registry_append_only_basenames():
    # Lock every append-only line-union registration () — an accidental
    # removal re-opens that store's both-diverged write-freeze.
    for base in ("evolution-log.jsonl", "productivity-snapshots.jsonl",
                 "gate-firings.jsonl", "trigger-firings.jsonl",
                 "coordination.jsonl", "general.jsonl", "findings.jsonl",
                 "decisions.jsonl", "defer-recheck-metrics.jsonl",
                 "credential-defer-recheck-metrics.jsonl",
                 "precondition-defer-recheck-metrics.jsonl",
                 "parent-supersession-sweep-metrics.jsonl",
                 "unblock-parent-status-sweep-metrics.jsonl",
                 "routing-audit-target-status-sweep-metrics.jsonl"):
        assert cm.merge_handler_for(f"world/{base}") is cm.merge_append_only_jsonl, base
    # basename resolution ignores the directory (board channels live under board/)
    assert cm.merge_handler_for("world/board/coordination.jsonl") is cm.merge_append_only_jsonl
    assert cm.merge_handler_for("meta/gate-firings.jsonl") is cm.merge_append_only_jsonl


def test_handler_registry_field_merge_basenames():
    # Lock the field-level YAML/JSON reconcile registrations () — an
    # accidental removal re-opens that store's both-diverged write-freeze.
    assert cm.merge_handler_for("world/module-health.yaml") is cm.merge_module_health
    assert cm.merge_handler_for("world/aspirations-meta.json") is cm.merge_aspirations_meta
    # basename resolution ignores directory + agent-store path
    assert cm.merge_handler_for("agents/alpha/aspirations-meta.json") is cm.merge_aspirations_meta
    # _tree.yaml is DELIBERATELY unregistered — carved to the higher-risk
    # follow-up  (1134-node structural tree). Locking None here keeps a
    # future edit from silently registering it against the wrong (flat) handler.
    assert cm.merge_handler_for("world/knowledge/tree/_tree.yaml") is None
