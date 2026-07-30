"""Unit tests for coordination_merge — the commutative both-diverged merge
handlers used by OwnCloudBackend._put (gap #5, ).

Pure functions, no moto / no daemon / no I/O. The governing invariant under
test is COMMUTATIVITY: merge(a, b) must be BYTE-IDENTICAL to merge(b, a). That
byte-level symmetry is what makes the two machines converge (each computes the
merge from its own vantage and reaches the same result, so the fenced-PUT retry
loop terminates instead of ping-ponging).
"""
import ast
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


def test_rb_collision_reid_stamps_tombstone():
    #  (flat-record mirror of the  goals-array fix): the
    # displaced loser carries displaced_from = the contested id it lost.
    recA = {"id": "rb-2", "created": "2026-07-02T10:00:00", "title": "machineA"}
    recB = {"id": "rb-2", "created": "2026-07-02T11:00:00", "title": "machineB"}
    out = _recs(cm.merge_reasoning_bank(_rb([recA]), _rb([recB])))
    moved = next(r for r in out if r["title"] == "machineB")
    assert moved["id"] == "rb-3" and moved["displaced_from"] == "rb-2"
    keep = next(r for r in out if r["title"] == "machineA")
    assert "displaced_from" not in keep                    # winner untouched


def test_rb_collision_reid_associative_stale_replay():
    # Stale replay from EITHER pre-collision side must be a byte fixpoint —
    # settled re-id records never reshuffle when a stale replica rejoins.
    base = [{"id": f"rb-{n}", "created": f"2026-07-01T0{n}:00:00",
             "title": f"filler-{n}"} for n in range(3, 8)]
    recA = {"id": "rb-2", "created": "2026-07-02T10:00:00", "title": "machineA"}
    recB = {"id": "rb-2", "created": "2026-07-02T11:00:00", "title": "machineB"}
    a, b = _rb([recA] + base), _rb([recB])
    settled = cm.merge_reasoning_bank(a, b)
    moved = next(r for r in _recs(settled) if r["title"] == "machineB")
    assert moved["id"] == "rb-8"                           # displaced past fillers
    assert cm.merge_reasoning_bank(settled, a) == settled
    assert cm.merge_reasoning_bank(settled, b) == settled
    assert cm.merge_reasoning_bank(a, settled) == cm.merge_reasoning_bank(settled, a)


def test_rb_chained_displacement_stale_replay_no_refight():
    # Chain-displaced record (lost its first displacement slot to an
    # earlier-created contender; tombstone home outside its seen ids on stale
    # replay) must anchor its LATEST settled slot directly — never re-fight.
    recX = {"id": "rb-2", "created": "2026-07-02T09:00:00", "title": "X"}
    recY = {"id": "rb-2", "created": "2026-07-02T10:00:00", "title": "Y"}
    s1 = cm.merge_reasoning_bank(_rb([recX]), _rb([recY]))
    y1 = next(r for r in _recs(s1) if r["title"] == "Y")
    assert y1["id"] == "rb-3" and y1["displaced_from"] == "rb-2"
    recZ = {"id": "rb-3", "created": "2026-07-02T09:30:00", "title": "Z"}
    s2 = cm.merge_reasoning_bank(s1, _rb([recZ]))
    y2 = next(r for r in _recs(s2) if r["title"] == "Y")
    assert y2["id"] == "rb-4"                              # chained displacement
    assert y2["displaced_from"] == "rb-2"                  # first home preserved
    stale = _rb([dict(y1)])
    assert cm.merge_reasoning_bank(s2, stale) == s2, \
        "chained-displacement stale replay must be a byte fixpoint"
    assert cm.merge_reasoning_bank(stale, s2) == cm.merge_reasoning_bank(s2, stale)


def test_guard_collision_reid_tombstone_zero_pad():
    # The tombstone lane must respect each store's id_format byte-exactness
    # (guard ids are 3-pad): displaced guard carries displaced_from and settles
    # at a 3-pad id; stale replay is a fixpoint.
    gA = {"id": "guard-002", "created": "2026-07-02T10:00:00", "rule": "A",
          "status": "active"}
    gB = {"id": "guard-002", "created": "2026-07-02T11:00:00", "rule": "B",
          "status": "active"}
    settled = cm.merge_guardrails(_rb([gA]), _rb([gB]))
    moved = next(r for r in _recs(settled) if r["rule"] == "B")
    assert moved["id"] == "guard-003" and moved["displaced_from"] == "guard-002"
    assert cm.merge_guardrails(settled, _rb([gB])) == settled
    one = _rb([{"id": "rb-1", "created": "t"}])
    assert cm.merge_reasoning_bank(one, b"") == one
    assert cm.merge_reasoning_bank(b"", one) == one


# --- key-order byte-commutativity () -------------------------------

def test_rb_distinct_new_keys_byte_commutative():
    """Same record, each side adds a DIFFERENT new key. out=dict(a)+b-extras
    inherits a's key order, so pre-fix merge(a,b) != merge(b,a) at the byte
    level (identical values, different key order) — fenced-PUT ping-pong
    (bravo-fec-idkeyed-keyorder-noncommut-202607161052)."""
    base = {"id": "rb-5", "created": "2026-07-02T10:00:00", "title": "t",
            "status": "active"}
    ra = dict(base); ra["last_probe"] = "2026-07-15"
    rb_ = dict(base); rb_["sample_note"] = "n=9"
    ab = cm.merge_reasoning_bank(_rb([ra]), _rb([rb_]))
    ba = cm.merge_reasoning_bank(_rb([rb_]), _rb([ra]))
    assert ab == ba, "distinct-key adds must stay byte-commutative (guard-907)"
    rec = _recs(ab)[0]
    assert rec["last_probe"] == "2026-07-15" and rec["sample_note"] == "n=9"


def test_rb_distinct_new_keys_multiround_settles():
    """The diverged record settles: once both sides hold the merged result, a
    re-merge against either stale local is a byte no-op (loop terminates)."""
    base = {"id": "rb-5", "created": "2026-07-02T10:00:00", "title": "t"}
    ra = dict(base); ra["last_probe"] = "x"
    rb_ = dict(base); rb_["sample_note"] = "y"
    m = cm.merge_reasoning_bank(_rb([ra]), _rb([rb_]))
    assert cm.merge_reasoning_bank(m, m) == m
    assert cm.merge_reasoning_bank(m, _rb([rb_])) == m
    assert cm.merge_reasoning_bank(_rb([ra]), m) == m


def test_rb_same_keyset_order_divergence_heals():
    """Pre-existing on-disk order divergence (same keys, same values, different
    insertion order — e.g. left behind by pre-fix merges) must also emit
    side-independent bytes, not preserve each side's own order forever."""
    r1 = {"id": "rb-6", "created": "2026-07-02T10:00:00", "title": "t",
          "k1": 1, "k2": 2}
    r2 = {"id": "rb-6", "created": "2026-07-02T10:00:00", "title": "t",
          "k2": 2, "k1": 1}
    ab = cm.merge_reasoning_bank(_rb([r1]), _rb([r2]))
    ba = cm.merge_reasoning_bank(_rb([r2]), _rb([r1]))
    assert ab == ba


def test_rb_nested_counter_key_divergence_byte_commutative():
    """utilization sub-dict key order reaches the bytes too — divergent counter
    keys (one side bumps a counter the other doesn't carry) must merge to
    byte-identical output in both directions."""
    base = {"id": "rb-7", "created": "2026-07-02T10:00:00", "title": "t"}
    ca = dict(base); ca["utilization"] = {"times_helpful": 1, "times_probed": 3}
    cb = dict(base); cb["utilization"] = {"times_helpful": 2, "times_active": 1}
    ab = cm.merge_reasoning_bank(_rb([ca]), _rb([cb]))
    ba = cm.merge_reasoning_bank(_rb([cb]), _rb([ca]))
    assert ab == ba
    util = _recs(ab)[0]["utilization"]
    assert util == {"times_helpful": 2, "times_probed": 3, "times_active": 1}


def test_rb_matching_key_order_is_preserved_not_sorted():
    """No blanket re-order churn: records whose key sequences MATCH keep their
    on-disk (deliberately unsorted) order byte-for-byte — the reason the fix
    lives in the record merge, not as sort_keys in _dump_jsonl."""
    u = {"id": "rb-8", "created": "2026-07-02T10:00:00", "title": "t",
         "zeta_field": 1, "alpha_field": 2}
    blob = _rb([u])
    assert cm.merge_reasoning_bank(blob, blob) == blob


def test_guard_distinct_new_keys_byte_commutative():
    """merge_guardrails shares the chassis — same distinct-key-add shape must
    stay byte-commutative (values verbatim, order side-independent)."""
    base = {"id": "guard-005", "created": "2026-07-01T09:00:00", "rule": "r",
            "status": "active"}
    ga = dict(base); ga["action_hint"] = "probe first"
    gb = dict(base); gb["times_triggered"] = 4
    ab = cm.merge_guardrails(_rb([ga]), _rb([gb]))
    ba = cm.merge_guardrails(_rb([gb]), _rb([ga]))
    assert ab == ba
    rec = _recs(ab)[0]
    assert rec["action_hint"] == "probe first" and rec["times_triggered"] == 4


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


# --- team-state per-agent shards ( / rb-3150) ---------------------
# The  split moved per-agent liveness into world/team-state/agents/
# <name>.yaml. Those shard basenames are dynamic (alpha.yaml/bravo.yaml/...) so
# the basename-keyed _HANDLERS never registered them -> merge_handler_for
# returned None -> the backend froze peer shards on the both-diverged 412 ->
# every box saw fresh-SELF + stale-PEERS. The fix: a path-pattern dispatch
# branch + a whole-snapshot-LWW shard handler giving shards the same
# both-diverged self-heal the composite team-state.yaml already had.

def _shard(**fields):
    """Build a flat per-agent team-state shard (agents/<name>.yaml) as bytes."""
    return yaml.dump(fields, default_flow_style=False, sort_keys=False).encode()


def test_shard_dispatch_by_path_pattern():
    # Dynamic shard basenames route by PATH pattern (parent agents/ under
    # team-state/) so a NEW agent is covered without editing _HANDLERS.
    for p in ("world/team-state/agents/alpha.yaml",
              "team-state/agents/bravo.yaml",
              r"C:\mind\world\team-state\agents\zeta.yaml"):
        assert cm.merge_handler_for(p) is cm.merge_team_state_shard, p
    # The composite still routes by basename to the composite handler.
    assert cm.merge_handler_for("world/team-state.yaml") is cm.merge_team_state
    # Negatives: unregistered, a lookalike dir, and a non-yaml file under the
    # shard dir must NOT match the shard branch.
    assert cm.merge_handler_for("world/random.yaml") is None
    assert cm.merge_handler_for("world/myteam-state/agents/x.yaml") is None
    assert cm.merge_handler_for("world/team-state/agents/notes.txt") is None


def test_shard_newer_last_active_wins_and_commutative():
    # The peer-shard freeze case: local STALE, remote FRESH -> fresh wins, so a
    # box adopts the peer's current shard instead of freezing on both-diverged.
    stale = _shard(last_active="2026-07-07T22:17:09", current_focus="",
                   session_goals_completed=0, row_updated_by="bravo")
    fresh = _shard(last_active="2026-07-13T23:27:00", current_focus="asp-318",
                   session_goals_completed=5, row_updated_by="bravo")
    m = yaml.safe_load(cm.merge_team_state_shard(stale, fresh).decode())
    assert m["last_active"] == "2026-07-13T23:27:00"
    assert m["current_focus"] == "asp-318"
    assert m["session_goals_completed"] == 5     # whole-snapshot LWW, not max
    # COMMUTATIVITY: byte-identical regardless of local/remote order.
    assert (cm.merge_team_state_shard(stale, fresh)
            == cm.merge_team_state_shard(fresh, stale))


def test_shard_whole_snapshot_lww_no_field_stitch():
    # A partial field-merge could stitch an inconsistent focus/live_phase/
    # in_flight triple. Whole-snapshot LWW takes ALL of the winner's fields and
    # NONE of the loser's — the winner cleared in_flight, so it stays cleared.
    old = _shard(last_active="2026-07-13T10:00:00", live_phase="phase-4-execute",
                 current_focus="oldgoal", in_flight={"goal_id": "g-old"})
    new = _shard(last_active="2026-07-13T11:00:00", live_phase="between-phases",
                 current_focus="newgoal")   # in_flight intentionally absent
    m = yaml.safe_load(cm.merge_team_state_shard(old, new).decode())
    assert m["live_phase"] == "between-phases"
    assert m["current_focus"] == "newgoal"
    assert "in_flight" not in m            # not stitched from the loser


def test_shard_non_mapping_fallback_deterministic():
    # Valid YAML that parses to a non-dict (list/scalar) hits the deterministic
    # content-larger fallback rather than raising, and stays symmetric.
    a = b"[1, 2, 3]"
    b = b"just a scalar string that is clearly the larger content blob"
    fb1 = cm.merge_team_state_shard(a, b)
    fb2 = cm.merge_team_state_shard(b, a)
    assert isinstance(fb1, bytes) and isinstance(fb2, bytes)
    assert fb1 == fb2


# --- aspirations.jsonl ( follow-up) -------------------------------
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


def test_aspirations_fulltie_order_divergence_heals():
    """: VALUE-identical order-divergent copies (canon-blind corner —
    _canon sorts keys) tie on both last_modified/last_selected AND canon, so
    the pre-fix _order_by_ts full-tie picked the FIRST ARG and dict(win) key
    order reached the bytes: merge(a,b) != merge(b,a), permanent ping-pong.
    Both the GOAL dict and the ASPIRATION record are permuted here to pin the
    _merge_goal AND _merge_aspiration_record routings."""
    g1 = {"id": "g-9-1", "title": "t", "status": "pending",
          "last_modified": "2026-07-03T10:00:00", "xf": 1, "yf": 2}
    g2 = {"id": "g-9-1", "status": "pending", "yf": 2, "xf": 1,
          "last_modified": "2026-07-03T10:00:00", "title": "t"}
    a1 = {"id": "asp-9", "status": "active", "last_selected": "2026-07-03T09:00:00",
          "goals": [g1]}
    a2 = {"id": "asp-9", "last_selected": "2026-07-03T09:00:00", "status": "active",
          "goals": [g2]}
    ab, ba = cm.merge_aspirations(_rb([a1]), _rb([a2])), \
        cm.merge_aspirations(_rb([a2]), _rb([a1]))
    assert ab == ba, "full-tie order divergence must canonicalize (guard-907)"
    m = cm.merge_aspirations(ab, _rb([a1]))
    assert m == ab, "post-heal re-merge against a stale side must be a fixpoint"


def test_aspirations_concurrent_add_same_id_keeps_both():
    # : two boxes each add a DISTINCT new goal to the same aspiration
    # and both decentralized max+1 allocate the SAME id (same eventual-consistency
    # window). The merge must keep BOTH (loser re-id'd), never field-interleave
    # them into one franken-record (the old id-keyed union dropped a writer —
    # observed live  add-loss /  update-loss).
    base = _goal("g-115-01", title="base", created_at="2026-07-14T09:00:00")
    boxA = _goal("g-115-02", title="alpha work", created_at="2026-07-14T10:00:00",
                 description="ALPHA-CONTENT")
    boxB = _goal("g-115-02", title="bravo work", created_at="2026-07-14T10:00:05",
                 description="BRAVO-CONTENT")
    a = _rb([_asp("asp-115", [base, boxA])])
    b = _rb([_asp("asp-115", [base, boxB])])
    ab, ba = cm.merge_aspirations(a, b), cm.merge_aspirations(b, a)
    assert ab == ba                                        # commutative / byte-identical
    assert sorted(_goal_ids(ab)) == ["g-115-01", "g-115-02", "g-115-03"]  # loser re-id'd, none lost
    descs = {g.get("description") for asp in _recs(ab) for g in asp["goals"]}
    assert {"ALPHA-CONTENT", "BRAVO-CONTENT"} <= descs     # zero content loss
    keep = _find_goal(ab, "g-115-02")
    assert keep["description"] == "ALPHA-CONTENT"          # earlier-created keeps the id
    moved = _find_goal(ab, "g-115-03")
    assert moved["description"] == "BRAVO-CONTENT"         # later displaced forward


def test_aspirations_concurrent_add_converges_multiround():
    # The re-id must CONVERGE across the multi-round fenced-PUT loop (mirror of
    # test_rb_multiround_collision_converges): a goal one box re-id'd is
    # recognized by its (created_at, title) identity when it returns under its old
    # id, so it is NOT re-duplicated each round.
    base = _goal("g-115-01", title="base", created_at="2026-07-14T09:00:00")
    gA = _goal("g-115-02", title="alpha", created_at="2026-07-14T10:00:00")
    gB = _goal("g-115-02", title="bravo", created_at="2026-07-14T10:00:05")
    localA = _rb([_asp("asp-115", [base, gA])])
    localB = _rb([_asp("asp-115", [base, gB])])
    s3 = cm.merge_aspirations(localA, localB)              # A pushes
    for _ in range(4):                                     # B, A, B, A re-merge stale local
        s3_b = cm.merge_aspirations(localB, s3)
        s3_a = cm.merge_aspirations(localA, s3_b)
        assert s3_a == s3_b, "not converged: A and B disagree"
        s3 = s3_a
    titles = sorted(g["title"] for asp in _recs(s3) for g in asp["goals"])
    assert titles == ["alpha", "base", "bravo"]            # exactly one of each, no dupes


def test_aspirations_variant_id_not_reallocated():
    # A -letter variant id (-a) is OUTSIDE the sequential space and must
    # pass through unchanged — never displaced into the g-115-{seq} sequence.
    a = _rb([_asp("asp-115", [_goal("g-115-1991-a", title="variant",
                                    created_at="2026-07-14T10:00:00")])])
    b = _rb([_asp("asp-115", [_goal("g-115-05", title="seq",
                                    created_at="2026-07-14T10:00:00")])])
    ab, ba = cm.merge_aspirations(a, b), cm.merge_aspirations(b, a)
    assert ab == ba
    assert _goal_ids(ab) == {"g-115-1991-a", "g-115-05"}   # variant kept verbatim


def test_aspirations_reid_winner_no_duplicate_id():
    #  fresh-eyes finding: the SAME goal seen under two seq ids (its
    # original id + a peer's re-id) where _merge_goal's LWW picks the HIGHER-seq
    # id must NOT let the keeper carry that higher id into its (lower) home bucket
    # and collide with the DISTINCT goal legitimately at the higher seq. All output
    # ids must be distinct in ONE pass (not self-healed a round later). _goal_ids()
    # is a SET (dedups) so it cannot see the dup -- assert on the RAW list via _recs.
    shared_a = _goal("g-115-02", title="shared", created_at="2026-07-14T10:00:00",
                     last_modified="2026-07-14T10:00:00")
    other    = _goal("g-115-03", title="other", created_at="2026-07-14T10:30:00",
                     last_modified="2026-07-14T10:30:00")
    shared_b = _goal("g-115-03", title="shared", created_at="2026-07-14T10:00:00",
                     last_modified="2026-07-14T11:00:00")   # newer -> LWW picks id 
    a = _rb([_asp("asp-115", [shared_a, other])])
    b = _rb([_asp("asp-115", [shared_b])])
    ab, ba = cm.merge_aspirations(a, b), cm.merge_aspirations(b, a)
    assert ab == ba                                          # commutative
    raw_ids = [g["id"] for asp in _recs(ab) for g in asp.get("goals", [])]
    assert len(raw_ids) == len(set(raw_ids)), f"duplicate id in one pass: {raw_ids}"
    titles = {g["title"] for asp in _recs(ab) for g in asp.get("goals", [])}
    assert {"shared", "other"} <= titles                     # both distinct goals survive


def test_aspirations_collision_reid_stamps_tombstone():
    # : the displaced loser carries displaced_from = the contested id
    # it lost, so external references (claims, discovered_by, origin_signal
    # dedup) can be traced and re-merges recognize the settled displacement.
    gA = _goal("g-115-02", title="alpha work", created_at="2026-07-14T10:00:00")
    gB = _goal("g-115-02", title="bravo work", created_at="2026-07-14T10:00:05")
    ab = cm.merge_aspirations(_rb([_asp("asp-115", [gA])]),
                              _rb([_asp("asp-115", [gB])]))
    moved = _find_goal(ab, "g-115-03")
    assert moved is not None and moved["title"] == "bravo work"
    assert moved.get("displaced_from") == "g-115-02"         # tombstone stamped
    keep = _find_goal(ab, "g-115-02")
    assert "displaced_from" not in keep                      # winner untouched


def test_aspirations_collision_reid_associative_stale_replay():
    #  core property: replaying a STALE pre-collision side against the
    # settled result must be a FIXPOINT — merge(merge(a,b), a) == merge(a,b) on
    # the collision path. Pre-tombstone, the settled displaced goal was dragged
    # back into its home bucket and re-displaced to a pair-dependent next-free
    # id (proven on the 2026-07-16 healed x clobbered pair: settled ids
    # reshuffled by a very stale replica rejoin).
    base = [_goal(f"g-115-{n:02d}", title=f"filler-{n}",
                  created_at=f"2026-07-10T0{n}:00:00") for n in range(3, 8)]
    gA = _goal("g-115-02", title="alpha work", created_at="2026-07-14T10:00:00")
    gB = _goal("g-115-02", title="bravo work", created_at="2026-07-14T10:00:05")
    a = _rb([_asp("asp-115", [gA] + base)])
    b = _rb([_asp("asp-115", [gB])])
    settled = cm.merge_aspirations(a, b)
    # bravo work displaced past the filler block (next free after 02..07 = 08)
    moved = _find_goal(settled, "g-115-08")
    assert moved is not None and moved["title"] == "bravo work"
    # Stale replays from EITHER side are fixpoints — settled ids never reshuffle.
    assert cm.merge_aspirations(settled, a) == settled
    assert cm.merge_aspirations(settled, b) == settled
    assert cm.merge_aspirations(a, settled) == cm.merge_aspirations(settled, a)


def test_aspirations_chained_displacement_stale_replay_no_refight():
    #  second-pass hardening: a CHAIN-displaced goal (displaced 50->67,
    # then lost 67 to an earlier-created contender -> 68; tombstone stays 50) has
    # home OUTSIDE its seen-id set on stale replay {67, 68}. It must anchor its
    # LATEST settled slot (68) directly — never re-fight bucket 67 it already
    # lost and rely on vacated-slot dynamics to land back.
    X = _goal("g-115-50", title="X", created_at="2026-07-14T09:00:00")
    Y = _goal("g-115-50", title="Y", created_at="2026-07-14T10:00:00")
    fill = [_goal(f"g-115-{n}", title=f"f{n}",
                  created_at=f"2026-07-13T00:{n}:00") for n in range(51, 67)]
    s1 = cm.merge_aspirations(_rb([_asp("asp-115", [X] + fill)]),
                              _rb([_asp("asp-115", [Y])]))
    y1 = next(g for a in _recs(s1) for g in a["goals"] if g["title"] == "Y")
    assert y1["id"] == "g-115-67" and y1["displaced_from"] == "g-115-50"
    # Z allocated Y's slot on another box, created EARLIER -> wins the bucket.
    Z = _goal("g-115-67", title="Z", created_at="2026-07-14T09:30:00")
    s2 = cm.merge_aspirations(s1, _rb([_asp("asp-115", [Z])]))
    y2 = next(g for a in _recs(s2) for g in a["goals"] if g["title"] == "Y")
    assert y2["id"] == "g-115-68"                       # chained displacement
    assert y2["displaced_from"] == "g-115-50"           # first home preserved
    # Stale replica still holding Y at its FIRST displacement slot rejoins.
    stale = _rb([_asp("asp-115", [dict(y1)])])
    s3 = cm.merge_aspirations(s2, stale)
    assert s3 == s2, "chained-displacement stale replay must be a byte fixpoint"
    assert cm.merge_aspirations(stale, s2) == cm.merge_aspirations(s2, stale)


def test_aspirations_divergent_displacement_converges():
    # Two histories displaced the SAME goal to DIFFERENT ids (their pair
    # id-spaces differed). The exchange must converge commutatively to ONE id
    # and stay a fixpoint on further stale replay.
    gA = _goal("g-115-02", title="alpha work", created_at="2026-07-14T10:00:00")
    gB = _goal("g-115-02", title="bravo work", created_at="2026-07-14T10:00:05")
    h1 = cm.merge_aspirations(_rb([_asp("asp-115", [gA])]),
                              _rb([_asp("asp-115", [gB])]))       # bravo -> 
    filler = _goal("g-115-03", title="filler", created_at="2026-07-10T03:00:00")
    h2 = cm.merge_aspirations(_rb([_asp("asp-115", [gA, filler])]),
                              _rb([_asp("asp-115", [gB])]))       # bravo -> 
    ab, ba = cm.merge_aspirations(h1, h2), cm.merge_aspirations(h2, h1)
    assert ab == ba                                          # commutative exchange
    raw_ids = [g["id"] for asp in _recs(ab) for g in asp.get("goals", [])]
    assert len(raw_ids) == len(set(raw_ids)), f"duplicate id: {raw_ids}"
    titles = {g["title"] for asp in _recs(ab) for g in asp.get("goals", [])}
    assert {"alpha work", "bravo work", "filler"} <= titles  # zero loss
    assert cm.merge_aspirations(ab, h1) == ab                # fixpoint after exchange
    assert cm.merge_aspirations(ab, h2) == ab


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


# --- torn-line tolerance () ----------------------------------------
def _torn_tail(records, cut=38):
    """A franken blob: valid records + a truncated final append (the cc-04
    wedge shape — the writer died mid-line, leaving a torn half-line tail).
    cut=38 slices {"ts": "2026-07-03T12:00:00", "torn": "yes"} mid-value —
    past the "torn" key (so tests can assert the fragment surfaced) but
    before the closing brace (so the line stays unparseable)."""
    full = _rb(records)
    extra = json.dumps(_log("2026-07-03T12:00:00", torn="yes"),
                       ensure_ascii=True).encode()
    return full + extra[:cut]                                # no trailing \n


def test_append_only_torn_local_no_longer_wedges(capsys):
    # THE  behavior: a torn half-line on one side must NOT raise
    # (pre-fix: json.loads raised -> _merge_reconcile_put wrapped it in
    # ConflictError -> the union lane wedged forever on the franken local).
    base = _log("2026-07-03T09:00:00", n=1)
    torn_local = _torn_tail([base, _log("2026-07-03T10:00:00", n=2)])
    clean_remote = _rb([base, _log("2026-07-03T11:00:00", n=3)])
    out = _recs(cm.merge_append_only_jsonl(torn_local, clean_remote))
    assert [r["n"] for r in out] == [1, 2, 3]                # all parseable kept
    err = capsys.readouterr().err
    assert "dropped 1 torn line(s)" in err                   # loud, not silent
    assert '"torn"' in err                                   # bytes preserved in-band


def test_append_only_torn_commutative_and_convergent(capsys):
    # guard-907: tolerance is symmetric — the dropped set is a function of
    # content, never of the local-vs-remote role. Byte-identical both ways,
    # and re-merging the torn side against the merged result is a fixpoint
    # (the fenced-PUT retry loop still terminates).
    base = _log("2026-07-03T09:00:00", n=1)
    torn = _torn_tail([base, _log("2026-07-03T10:00:00", n=2)])
    clean = _rb([base, _log("2026-07-03T11:00:00", n=3)])
    ab = cm.merge_append_only_jsonl(torn, clean)
    ba = cm.merge_append_only_jsonl(clean, torn)
    assert ab == ba                                          # byte-identical
    assert cm.merge_append_only_jsonl(torn, ab) == ab        # fixpoint
    capsys.readouterr()                                      # drain warnings


def test_append_only_torn_both_sides_and_mid_utf8_cut(capsys):
    # A tear can cut mid-UTF-8-sequence — strict decode would raise before
    # line-splitting. Both sides torn simultaneously must still union cleanly.
    base = _log("2026-07-03T09:00:00", n=1)
    a = _rb([base]) + '{"ts": "2026-07-03T10:00:00", "s": "café'.encode()[:-1]
    b = _torn_tail([base, _log("2026-07-03T10:30:00", n=2)])
    out = _recs(cm.merge_append_only_jsonl(a, b))
    assert [r["n"] for r in out] == [1, 2]
    err = capsys.readouterr().err
    assert err.count("dropped 1 torn line(s)") == 2          # one per side


def test_torn_tolerance_does_not_leak_to_id_keyed_handlers():
    # Scope boundary (): id-keyed stores keep the STRICT parse — a
    # parse failure there can mean real corruption of an editable record and
    # must freeze (ConflictError at the caller), never silently skip.
    torn = _rb([{"id": "rb-1", "created": "2026-07-02T10:00:00",
                 "title": "base"}])[:-10]                    # truncate a record
    clean = _rb([{"id": "rb-2", "created": "2026-07-02T11:00:00",
                  "title": "other"}])
    with pytest.raises(Exception):
        cm.merge_reasoning_bank(torn, clean)


# --- _HANDLERS registration () ------------------------------------
def test_handler_registration_board_and_override_g115_2006():
    # The non-default board channels + the Phase 4 bulk-override ledger are
    # shared append-only stores that were unregistered and wedged on
    # both-diverged. They MUST now route to merge_append_only_jsonl. Dispatch is
    # by BASENAME, so the leading board/ path segment is irrelevant.
    for path in ["board/reasoning.jsonl", "board/directives.jsonl",
                 "board/events.jsonl", "board/feedback.jsonl",
                 "override-bypass-ledger.jsonl"]:
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, \
            f"{path} not registered to the append-only handler"


def test_handler_registration_excludes_pruned_stores_g115_2006():
    # Regression guard (rb-245): stores that are REWRITTEN/PRUNED (not strictly
    # append-only) MUST NOT take the LINE-UNION handler — it would resurrect
    # pruned/superseded records. journal.jsonl gets index rewrites and stays
    # fully unregistered. The evolution streams (rewritten in place by
    # evolution-complete/stub-expiry) left this exclusion 2026-07-18
    # (): they are now registered with merge_evolution_stream — a
    # revision_id-keyed STATUS-MONOTONIC merge that handles the rewrite
    # correctly — but must NEVER regress to merge_append_only_jsonl.
    # (changelog.jsonl lived here until  — it is ROTATED into
    # changelog-archive.jsonl, a MOVE not a record-dropping rewrite, so the
    # pair is append-only-registered; see
    # test_handler_registration_changelog_g115_2173 below.)
    assert cm.merge_handler_for("journal.jsonl") is None, \
        "journal.jsonl is rewritten and must stay unregistered"
    for path in ["self-evolution.jsonl", "skill-evolution.jsonl",
                 "rule-evolution.jsonl", "script-evolution.jsonl",
                 "program-evolution.jsonl"]:
        h = cm.merge_handler_for(path)
        assert h is cm.merge_evolution_stream, \
            f"{path} must use the status-monotonic evolution handler"
        assert h is not cm.merge_append_only_jsonl, \
            f"{path} is rewritten in place — line-union would resurrect stubs"


def test_handler_registration_changelog_g115_2173():
    #  (ports cc-02 7b6801e1): changelog.jsonl is ROTATED into
    # changelog-archive.jsonl by store-hygiene.yaml — a MOVE, not a record-dropping
    # rewrite — so the pair carries the SAME bounded rotate+line-union tradeoff
    # already accepted for the board channels, and BOTH must route to the
    # append-only handler. Leaving them unregistered froze the fleet's entire
    # write-audit trail out of S3 for 5 weeks across all six changelog stores
    # (rb-3150 freeze class). Dispatch is by basename, so a leading world/ or
    # agents/<name>/ path segment is irrelevant.
    for path in ["changelog.jsonl", "changelog-archive.jsonl",
                 "world/changelog.jsonl", "agents/alpha/changelog.jsonl"]:
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, \
            f"{path} not registered to the append-only handler (g-115-2173)"


def test_changelog_two_box_concurrent_append_converges_g115_2173():
    # End-to-end two-box concurrent-append with the REAL changelog record shape
    # (timestamp/agent/file/action/summary/lines_changed): two boxes each append a
    # distinct audit record on top of a shared baseline; resolve the handler the
    # way OwnCloudBackend._put does (merge_handler_for) and confirm the fenced-PUT
    # retry loop CONVERGES (byte-identical from both vantages) with no wedge and no
    # lost append. Regression lock for the 5-week freeze: before ,
    # merge_handler_for("changelog.jsonl") returned None and the backend safe-froze
    # the store out of S3. Sort is by `timestamp` (a _log_ts field).
    handler = cm.merge_handler_for("agents/alpha/changelog.jsonl")
    assert handler is cm.merge_append_only_jsonl
    base = {"timestamp": "2026-07-14T09:00:00", "agent": "alpha",
            "file": "world/aspirations.jsonl", "action": "edit",
            "summary": "baseline", "lines_changed": 1}
    a = _rb([base, {"timestamp": "2026-07-14T10:00:00", "agent": "alpha",
                    "file": "world/pipeline.jsonl", "action": "append",
                    "summary": "A", "lines_changed": 1}])
    b = _rb([base, {"timestamp": "2026-07-14T10:05:00", "agent": "zeta",
                    "file": "world/guardrails.jsonl", "action": "append",
                    "summary": "B", "lines_changed": 1}])
    ab, ba = handler(a, b), handler(b, a)
    assert ab == ba                                          # converged, no ping-pong
    summaries = [r["summary"] for r in _recs(ab)]
    assert summaries == ["baseline", "A", "B"]               # baseline deduped, both kept, chronological


def test_board_reasoning_two_box_concurrent_append_converges_g115_2006():
    # End-to-end two-box concurrent-append simulation: two boxes each append a
    # distinct musing to the reasoning channel on top of a shared baseline;
    # resolve the handler the way OwnCloudBackend._put does (via
    # merge_handler_for) and confirm the fenced-PUT retry loop CONVERGES
    # (byte-identical from both vantages) with no wedge and no lost append.
    handler = cm.merge_handler_for("board/reasoning.jsonl")
    base = {"id": "m0", "ts": "2026-07-11T09:00:00", "author": "alpha", "text": "baseline"}
    a = _rb([base, {"id": "mA", "ts": "2026-07-11T10:00:00", "author": "alpha", "text": "A"}])
    b = _rb([base, {"id": "mB", "ts": "2026-07-11T10:05:00", "author": "bravo", "text": "B"}])
    ab, ba = handler(a, b), handler(b, a)
    assert ab == ba                                          # converged, no ping-pong
    ids = [r["id"] for r in _recs(ab)]
    assert ids == ["m0", "mA", "mB"]                         # baseline deduped, both kept


def test_handler_registration_g115_2009_verified_append_only():
    #  remainder: the lower-churn shared append-only stores, EACH
    # verified strictly append-only by reading its writer (rb-245). The 9 per-gate
    # override ledgers + 6 audit/telemetry logs MUST route to the append-only
    # handler. Dispatch is by basename, so a leading world/ path is irrelevant.
    for path in ["blocker-gate-overrides.jsonl", "goal-duplication-overrides.jsonl",
                 "loop-state-merge-overrides.jsonl", "origin-signal-overrides.jsonl",
                 "output-style-overrides.jsonl", "phase-4-26-overrides.jsonl",
                 "stale-read-overrides.jsonl", "uncommitted-work-overrides.jsonl",
                 "missing-artifact-overrides.jsonl", "skill-rejected-edits.jsonl",
                 "reflection-history.jsonl", "defer-date-extractions.jsonl",
                 "retrieval-trace.jsonl", "loop-death-detections.jsonl",
                 "description-length-telemetry.jsonl",
                 "world/blocker-gate-overrides.jsonl"]:
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, \
            f"{path} not registered to the append-only handler (g-115-2009)"


def test_handler_registration_g115_2009_excludes_rewritten_stores():
    # Regression guard (rb-245): the  audit DISQUALIFIED these because
    # their writers REWRITE the whole file — dead-ends via meta-dead-ends.py
    # write_all() -> locked_write_jsonl; knowledge-graph via knowledge-graph-
    # build.py rebuild. A line-union handler would resurrect deleted records, so
    # they MUST stay unregistered (safe-freeze). meta-log/l1-pick-log/scoring-
    # criterion-audit were DEFERRED (writer not confirmed) — also unregistered.
    # meta-log.jsonl + l1-pick-log.jsonl left the DEFERRED list 2026-07-18
    # (): writers read and verified append-only (meta-yaml.py
    # append_log open('a') — its open('r') is only mc-NNN allocation;
    # _l1_pick.py open('a') + l1-domain-rename.py append), now registered.
    for path in ["dead-ends.jsonl", "knowledge-graph.jsonl",
                 "scoring-criterion-audit.jsonl"]:
        assert cm.merge_handler_for(path) is None, \
            f"{path} is rewritten/unconfirmed and must NOT be append-only-registered"
    for path in ["meta-log.jsonl", "l1-pick-log.jsonl"]:
        assert cm.merge_handler_for(path) is cm.merge_append_only_jsonl, \
            f"{path} writer-verified append-only (g-115-2551) and must stay registered"


def test_override_ledger_two_box_concurrent_append_converges_g115_2009():
    # End-to-end: two boxes each append a distinct override record to
    # blocker-gate-overrides on a shared baseline; resolve the handler as
    # OwnCloudBackend._put does (merge_handler_for) and confirm the fenced-PUT
    # retry loop CONVERGES byte-identically with no wedge and no lost append.
    handler = cm.merge_handler_for("world/blocker-gate-overrides.jsonl")
    base = {"timestamp": "2026-07-11T09:00:00", "agent": "alpha", "gate": "blocker_create", "justification": "baseline"}
    a = _rb([base, {"timestamp": "2026-07-11T10:00:00", "agent": "alpha", "gate": "blocker_create", "justification": "A"}])
    b = _rb([base, {"timestamp": "2026-07-11T10:05:00", "agent": "bravo", "gate": "blocker_create", "justification": "B"}])
    ab, ba = handler(a, b), handler(b, a)
    assert ab == ba                                          # converged, no ping-pong
    justs = [r["justification"] for r in _recs(ab)]
    assert justs == ["baseline", "A", "B"]                   # baseline deduped, both appends kept


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
# --- pipeline ( / rb-2849 — the cc-04 NON-multipart no_clobber freeze) ---
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


def test_pipeline_fulltie_order_divergence_heals():
    """: two VALUE-identical copies whose key sequences diverged tie
    on stage rank AND canon (_canon sorts keys — blind to order); the pre-fix
    tiebreak picked the FIRST ARG, so merge(a,b) != merge(b,a) bytes and the
    fenced-PUT loop ping-ponged forever. Post-fix the diverged sequences
    canonicalize (sorted) and settle."""
    r1 = {"id": "2026-07-01_x", "title": "hyp", "stage": "active",
          "alpha_f": 1, "beta_f": 2}
    r2 = {"id": "2026-07-01_x", "stage": "active", "beta_f": 2,
          "alpha_f": 1, "title": "hyp"}
    ab, ba = cm.merge_pipeline(_rb([r1]), _rb([r2])), \
        cm.merge_pipeline(_rb([r2]), _rb([r1]))
    assert ab == ba
    assert cm.merge_pipeline(ab, _rb([r2])) == ab       # settles


def test_pipeline_three_box_manufacture_converges():
    """The reachability proof for the full-tie corner: THREE participants, two
    concurrent distinct-field adds, DIFFERENT merge encounter orders. Pre-fix
    this manufactures value-identical order-divergent copies (...,x,y vs
    ...,y,x) that then full-tie ping-pong forever. Post-fix every diverged
    merge emits sorted keys, so all encounter orders converge byte-identically."""
    base = {"id": "2026-07-01_h", "title": "hyp", "stage": "active"}
    rx = dict(base); rx["xf"] = 1
    ry = dict(base); ry["yf"] = 2
    box_a = cm.merge_pipeline(_rb([rx]), _rb([ry]))          # A: meets y first
    b1 = cm.merge_pipeline(_rb([ry]), _rb([base]))           # B: stale base first
    box_b = cm.merge_pipeline(b1, _rb([rx]))                 # ...then x
    assert box_a == box_b, "3-box encounter orders must converge (g-115-2355)"
    assert cm.merge_pipeline(box_a, box_b) == box_a          # fixpoint


def test_pipeline_matching_key_order_preserved_not_sorted():
    """No blanket churn: when both sides carry the SAME key sequence, on-disk
    order is preserved even if unsorted (self-merge is byte-identity)."""
    rec = {"id": "2026-07-02_y", "title": "keep order", "stage": "active",
           "zeta_f": 1, "alpha_f": 2}
    blob = _rb([rec])
    assert cm.merge_pipeline(blob, blob) == blob


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


def test_spark_fulltie_order_divergence_heals():
    """: same-type union path — VALUE-identical order-divergent
    copies tie on canon and pre-fix fell to first-arg-wins key order."""
    s1 = {"id": "sq-001", "type": "question", "text": "Q1?",
          "times_asked": 5, "mf": 1, "nf": 2}
    s2 = {"id": "sq-001", "type": "question", "text": "Q1?",
          "nf": 2, "mf": 1, "times_asked": 5}
    ab, ba = cm.merge_spark_questions(_rb([s1]), _rb([s2])), \
        cm.merge_spark_questions(_rb([s2]), _rb([s1]))
    assert ab == ba
    assert cm.merge_spark_questions(ab, _rb([s2])) == ab    # settles


# --- pipeline-meta.json ( — rewritten by every pipeline mutation) ---
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


def test_pipeline_meta_micro_counters_numeric_max_not_lexicographic():
    #  (guard-1153 sweep): confirmed_all_time/corrected_all_time are
    # grow-only counters. The pre-fix per-key _canon compare ordered
    # lexicographically ("10" < "9"), so the higher count LOST on a same-key
    # clash — a counter went backward on merge. Pin numeric MAX + the derived
    # accuracy_all_time recompute, both arg orders.
    a = _pmeta("2026-07-06", micro={"confirmed_all_time": 10,
                                    "corrected_all_time": 2,
                                    "accuracy_all_time": 0.833})
    b = _pmeta("2026-07-05", micro={"confirmed_all_time": 9,
                                    "corrected_all_time": 3,
                                    "accuracy_all_time": 0.75})
    ab, ba = cm.merge_pipeline_meta(a, b), cm.merge_pipeline_meta(b, a)
    assert ab == ba
    m = json.loads(ab.decode("utf-8"))["micro_hypothesis_stats"]
    assert m["confirmed_all_time"] == 10               # numeric MAX (not "10"<"9")
    assert m["corrected_all_time"] == 3                # per-key MAX, not one side
    assert m["accuracy_all_time"] == round(10 / 13, 3)  # recomputed from merged
    # Non-numeric values keep the deterministic content-compare fallback.
    c = _pmeta("2026-07-06", micro={"last_session_stats": {"date": "2026-07-11", "total": 3}})
    d = _pmeta("2026-07-05", micro={"last_session_stats": {"date": "2026-07-10", "total": 5}})
    cd, dc = cm.merge_pipeline_meta(c, d), cm.merge_pipeline_meta(d, c)
    assert cd == dc                                    # commutative either way


def test_handler_registry_pipeline_spark_basenames():
    # Lock the  / rb-2849 registrations — an accidental removal
    # re-opens the cc-04 non-multipart both-diverged write-freeze.
    assert cm.merge_handler_for("world/pipeline.jsonl") is cm.merge_pipeline
    assert cm.merge_handler_for("world/pipeline-archive.jsonl") is cm.merge_pipeline
    assert cm.merge_handler_for("world/pipeline-meta.json") is cm.merge_pipeline_meta
    assert cm.merge_handler_for("meta/spark-questions.jsonl") is cm.merge_spark_questions
    # pattern-signatures.jsonl REGISTERED (, 2026-07-16) — the
    # -era None-lock's premise ("two live writers disagree on
    # serialization") was re-verified STALE: the H2 Wave 3 migration gutted the
    # CLI CRUD writers to the daemon generic store endpoint (ensure_ascii=True,
    # store.py), the bespoke record-outcome daemon endpoint emits True, and the
    # CLI record-outcome path routes through _fileops.locked_modify_jsonl
    # (True at every write site). The one remaining ensure_ascii=False emitter
    # is cmd_migrate_yaml — the one-time fresh-world bootstrap seeder, not a
    # steady-state writer (and the live store is ASCII-only, so even that form
    # is byte-identical). Full verification trail in merge_pattern_signatures'
    # docstring + test_pattern_signatures_merge.py.
    assert cm.merge_handler_for("world/pattern-signatures.jsonl") \
        is cm.merge_pattern_signatures


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
    # _tree.yaml is NOW registered — -c wired merge_tree (node reconcile
    # a/b + top-level assembly c). Lock the binding so an accidental removal
    # re-opens the ~1140-node tree's both-diverged freeze, and so a future edit
    # can't silently swap it for the wrong (flat) handler.
    assert cm.merge_handler_for("world/knowledge/tree/_tree.yaml") is cm.merge_tree
    assert cm.merge_handler_for("_tree.yaml") is cm.merge_tree  # basename resolution


# --- _tree.yaml per-node field reconcile (-a) ----------------------
# Sub-goal A: the FIELD-CLASSIFICATION map + the three NON-STRUCTURAL merge
# classes (MAX / NEWER / PROGRESSION). The STRUCTURAL reconcile is -b
# and the bytes<->bytes merge_tree handler + _HANDLERS registration is
# -c, so these tests exercise the dict-level helpers directly (there is
# no merge_tree bytes path yet -- _tree.yaml stays unregistered, asserted above).
def _tnode(**over):
    """A minimal tree node dict with one field per non-structural class plus a
    couple of BASE/STRUCTURAL fields, so a test can diverge exactly what it means."""
    n = {"summary": "node summary", "file": "path/to/node.md",
         "growth_state": "stable", "node_type": "leaf", "depth": 3,
         "parent": "root", "children": [],
         "retrieval_count": 10, "times_helpful": 4, "times_noise": 1,
         "last_updated": "2026-07-01T00:00:00",
         "last_retrieved": "2026-07-01T00:00:00",
         "confidence": 0.5, "capability_level": "CALIBRATE"}
    n.update(over)
    return n


# Observed _tree.yaml per-node fields (inventoried from the live ~1140-node tree,
# -a). The classification map MUST route every one to a known class --
# that is outcome #1 "field-classification map covers all per-node fields".
_OBSERVED_TREE_FIELDS = [
    "children", "retrieval_count", "parent", "node_type", "last_updated",
    "growth_state", "file", "depth", "capability_level", "article_count",
    "child_count", "utility_ratio", "summary", "times_noise", "last_retrieved",
    "times_helpful", "times_inferred_helpful", "backfill_reason", "confidence",
    "domain_confidence", "sample_size", "accuracy", "poignancy",
    "last_relevant_at", "valid_to", "valid_from", "domain_class",
    "origin_goal_id", "maintain_exempt", "last_update_trigger",
]


def test_tree_classify_covers_all_observed_fields():
    valid = {"MAX", "NEWER", "PROGRESSION", "STRUCTURAL", "BASE"}
    for f in _OBSERVED_TREE_FIELDS:
        assert cm._classify_tree_field(f) in valid, f
    # named classes land where the  spec says
    assert cm._classify_tree_field("retrieval_count") == "MAX"
    assert cm._classify_tree_field("times_inferred_helpful") == "MAX"
    assert cm._classify_tree_field("last_updated") == "NEWER"
    assert cm._classify_tree_field("last_retrieved") == "NEWER"
    assert cm._classify_tree_field("confidence") == "PROGRESSION"
    assert cm._classify_tree_field("capability_level") == "PROGRESSION"
    assert cm._classify_tree_field("children") == "STRUCTURAL"
    assert cm._classify_tree_field("parent") == "STRUCTURAL"
    # a future/unknown field defaults to the safe BASE class (total function)
    assert cm._classify_tree_field("some_future_field_zzz") == "BASE"


def test_tree_max_field_monotonic_and_commutative():
    a = _tnode(retrieval_count=10, times_helpful=7, times_noise=2)
    b = _tnode(retrieval_count=13, times_helpful=4, times_noise=5)
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba                                  # commutative
    assert ab["retrieval_count"] == 13               # MAX -- never lose a count
    assert ab["times_helpful"] == 7
    assert ab["times_noise"] == 5


def test_tree_newer_timestamp_wins_commutative():
    a = _tnode(last_retrieved="2026-07-05T09:00:00", last_updated="2026-07-05T09:00:00")
    b = _tnode(last_retrieved="2026-07-02T09:00:00", last_updated="2026-07-02T09:00:00")
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba
    assert ab["last_retrieved"] == "2026-07-05T09:00:00"   # strictly-newer wins
    assert ab["last_updated"] == "2026-07-05T09:00:00"


def test_tree_progression_later_last_updated_wins_even_if_lower():
    # confidence is LWW by last_updated: a LATER downgrade (0.9 -> 0.4) is
    # PRESERVED, not clobbered by a never-regress MAX. This is the whole reason
    # confidence is LWW-by-timestamp rather than a blind max.
    older = _tnode(confidence=0.9, last_updated="2026-07-01T00:00:00")
    newer = _tnode(confidence=0.4, last_updated="2026-07-06T00:00:00")
    ab, ba = cm._merge_tree_node(older, newer), cm._merge_tree_node(newer, older)
    assert ab == ba
    assert ab["confidence"] == 0.4                   # later edit wins (downgrade kept)


def test_tree_progression_never_regress_on_equal_timestamp():
    # equal last_updated -> ambiguous winner -> never-regress: higher confidence
    # AND more-mature capability_level win; commutative both directions.
    ts = "2026-07-04T12:00:00"
    a = _tnode(confidence=0.3, capability_level="CALIBRATE", last_updated=ts)
    b = _tnode(confidence=0.8, capability_level="EXPLOIT", last_updated=ts)
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba
    assert ab["confidence"] == 0.8                   # never regress: higher wins
    assert ab["capability_level"] == "EXPLOIT"       # EXPLOIT > CALIBRATE on the axis


def test_tree_progression_stamp_preserves_downgrade_on_equal_last_updated_g_115_2495():
    # : a data-derived confidence DOWNGRADE must survive an own-cloud
    # merge even when last_updated is EQUAL on both sides (the  bug:
    # accuracy-sync lowers confidence WITHOUT bumping last_updated, per the
    #  decoupling). progression_updated_at is the dedicated LWW key,
    # so the side that recalibrated (fresher stamp) wins and the downgrade holds.
    lu = "2026-07-01T00:00:00"                        # SAME article last_updated
    a = _tnode(confidence=0.5, last_updated=lu, progression_updated_at="2026-07-17")
    b = _tnode(confidence=0.7, last_updated=lu, progression_updated_at="2026-07-14")
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba                                  # commutative
    assert ab["confidence"] == 0.5                   # DOWNGRADE SURVIVES (the fix)
    assert ab["progression_updated_at"] == "2026-07-17"  # newer stamp kept (NEWER)


def test_tree_progression_stamp_absent_is_backfill_safe_g_115_2495():
    # Backfill-safe: with NO progression_updated_at on either side the merge
    # falls back to last_updated -> behavior IDENTICAL to pre-fix (equal
    # last_updated -> never-regress keeps the higher value; stamp not fabricated).
    lu = "2026-07-01T00:00:00"
    a = _tnode(confidence=0.5, last_updated=lu)
    b = _tnode(confidence=0.7, last_updated=lu)
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba
    assert ab["confidence"] == 0.7                   # unchanged: never-regress on equal ts
    assert "progression_updated_at" not in ab        # not fabricated


def test_tree_capability_reference_orthogonal_tiebreak_commutative():
    # REFERENCE is off the maturity axis -> an equal-timestamp tie falls to the
    # content tiebreak, which must STILL be commutative + deterministic.
    ts = "2026-07-04T12:00:00"
    a = _tnode(capability_level="REFERENCE", last_updated=ts)
    b = _tnode(capability_level="EXPLORE", last_updated=ts)
    assert cm._merge_tree_node(a, b) == cm._merge_tree_node(b, a)


def test_tree_class_field_present_on_one_side_kept():
    # a MAX/NEWER/PROGRESSION field on ONLY one side is never dropped by the base.
    a = _tnode()
    del a["times_helpful"]                            # a lacks this MAX field
    b = _tnode(times_helpful=9, last_updated="2026-07-09T00:00:00")  # b is the base
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba
    assert ab["times_helpful"] == 9                   # kept from the only side with it


def test_tree_all_classes_diverge_commutative_and_convergent():
    a = _tnode(retrieval_count=10, last_updated="2026-07-05T00:00:00",
               last_retrieved="2026-07-05T00:00:00", confidence=0.6,
               capability_level="EXPLOIT", summary="A-summary")
    b = _tnode(retrieval_count=14, last_updated="2026-07-02T00:00:00",
               last_retrieved="2026-07-08T00:00:00", confidence=0.9,
               capability_level="CALIBRATE", summary="B-summary")
    ab, ba = cm._merge_tree_node(a, b), cm._merge_tree_node(b, a)
    assert ab == ba                                   # commutative
    assert ab["retrieval_count"] == 14                # MAX
    assert ab["last_retrieved"] == "2026-07-08T00:00:00"  # NEWER
    assert ab["last_updated"] == "2026-07-05T00:00:00"    # NEWER
    assert ab["confidence"] == 0.6                    # PROGRESSION: a's last_updated newer
    assert ab["capability_level"] == "EXPLOIT"
    assert ab["summary"] == "A-summary"               # BASE rides newer-last_updated (a)
    # multiround fixpoint: re-merging the result with either input is stable
    assert cm._merge_tree_node(ab, b) == ab
    assert cm._merge_tree_node(a, ab) == ab


def test_tree_field_helpers_are_directly_commutative():
    # the three named non-structural merge functions, unit-tested in isolation.
    assert cm._merge_field_max(5, 9) == cm._merge_field_max(9, 5) == 9
    assert cm._merge_field_max(3, None) == cm._merge_field_max(None, 3) == 3
    t_old, t_new = "2026-07-01T00:00:00", "2026-07-09T00:00:00"
    assert cm._merge_field_newer(t_old, t_new) == cm._merge_field_newer(t_new, t_old) == t_new
    # progression: newer timestamp wins regardless of value or arg order
    assert cm._merge_field_progression(0.2, t_new, 0.9, t_old) == 0.2
    assert cm._merge_field_progression(0.9, t_old, 0.2, t_new) == 0.2
    # progression tie -> never regress (higher), commutative
    assert cm._merge_field_progression(0.2, t_old, 0.9, t_old) == 0.9
    assert cm._merge_field_progression(0.9, t_old, 0.2, t_old) == 0.9


def test_tree_node_non_dict_inputs_commutative():
    # -a fresh-eyes fix: the non-dict guard must be commutative too.
    # The dict side always wins; BOTH-non-dict falls to the content tiebreak so
    # the result is identical regardless of arg order (the prior guard returned
    # the first arg unconditionally: merge(3,5)->3 but merge(5,3)->5).
    node = _tnode()
    assert cm._merge_tree_node(node, 5) == cm._merge_tree_node(5, node) == node
    assert cm._merge_tree_node(3, 5) == cm._merge_tree_node(5, 3)   # both non-dict, symmetric
    assert cm._merge_tree_node(None, node) == cm._merge_tree_node(node, None) == node


def test_tree_node_loser_only_base_field_preserved():
    # -a fresh-eyes fix: a BASE field present ONLY on the loser side
    # (older last_updated) must survive — authored fields like origin_goal_id are
    # NOT self-correcting, so dropping them loses data. Commutative both ways.
    older = _tnode(origin_goal_id="g-115-42", last_updated="2026-07-01T00:00:00")
    newer = _tnode(last_updated="2026-07-06T00:00:00")   # newer base, no origin_goal_id
    assert "origin_goal_id" not in newer
    ab, ba = cm._merge_tree_node(older, newer), cm._merge_tree_node(newer, older)
    assert ab == ba
    assert ab["origin_goal_id"] == "g-115-42"             # loser-only authored field kept
    assert ab["last_updated"] == "2026-07-06T00:00:00"    # NEWER class still takes the newer


# --- _tree.yaml node-MAP structural merge (-b) ---------------------
# The dedicated adversarial structural-integrity pass. _merge_tree_nodes_map is
# the parent-authoritative map merge; _tree_structural_integrity is the checker.
# HIGH-blast-radius (a bug corrupts the ~1140-node tree once -c wires
# it), so these assert the three invariants directly on both-diverged inputs.
def _tmap(*nodes):
    """Build a `nodes:` map from (key, parent, last_updated) tuples. last_updated
    is stamped so _merge_tree_node's LWW parent reconcile is deterministic."""
    m = {}
    for key, parent, lu in nodes:
        m[key] = {"parent": parent, "last_updated": lu,
                  "summary": f"{key} sum", "retrieval_count": 1}
    return m


def test_tree_map_union_preserves_node_count():
    a = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("x", "root", "2026-07-01T00:00:00"),
              ("a_only", "root", "2026-07-01T00:00:00"))
    b = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("x", "root", "2026-07-01T00:00:00"),
              ("b_only", "root", "2026-07-01T00:00:00"))
    m = cm._merge_tree_nodes_map(a, b)
    assert set(m) == {"root", "x", "a_only", "b_only"}          # union, zero node loss
    assert cm._merge_tree_nodes_map(b, a) == m                  # commutative
    assert cm._tree_structural_integrity(m) == []              # clean


def test_tree_map_children_derived_symmetric_no_orphans():
    # concurrent-add: A adds child p under root, B adds child q under root.
    a = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("p", "root", "2026-07-02T00:00:00"))
    b = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("q", "root", "2026-07-02T00:00:00"))
    ab, ba = cm._merge_tree_nodes_map(a, b), cm._merge_tree_nodes_map(b, a)
    assert ab == ba
    assert ab["root"]["children"] == ["p", "q"]                # derived + SORTED
    assert ab["root"]["child_count"] == 2
    assert ab["root"]["node_type"] == "interior"
    assert ab["p"]["node_type"] == "leaf"
    assert cm._tree_structural_integrity(ab) == []             # symmetry + no orphans


def test_tree_map_parent_move_no_symmetry_violation():
    # THE canonical failure a naive children-union would corrupt: A moves z from p
    # to n (newer last_updated), B keeps z under p. Parent-authoritative reconcile
    # -> z.parent = n (LWW), z appears in n.children ONLY, never in p.children.
    a = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("p", "root", "2026-07-01T00:00:00"),
              ("n", "root", "2026-07-01T00:00:00"),
              ("z", "n", "2026-07-05T00:00:00"))     # A: z moved under n (newer)
    b = _tmap(("root", None, "2026-07-01T00:00:00"),
              ("p", "root", "2026-07-01T00:00:00"),
              ("n", "root", "2026-07-01T00:00:00"),
              ("z", "p", "2026-07-02T00:00:00"))     # B: z still under p (older)
    ab, ba = cm._merge_tree_nodes_map(a, b), cm._merge_tree_nodes_map(b, a)
    assert ab == ba                                            # commutative
    assert ab["z"]["parent"] == "n"                           # LWW: the later move wins
    assert ab["n"]["children"] == ["z"]                       # z under n only
    assert ab["p"]["children"] == []                          # NOT double-listed
    assert cm._tree_structural_integrity(ab) == []            # zero symmetry violations


def test_tree_map_depth_follows_reconciled_parent():
    # depth recomputes from the reconciled parent chain: root keeps its merged
    # depth, each descendant = parent depth + 1 — so a stale stored depth is
    # corrected to match the actual structure.
    a = {"root": {"parent": None, "depth": 1, "last_updated": "2026-07-01T00:00:00"},
         "mid": {"parent": "root", "depth": 2, "last_updated": "2026-07-01T00:00:00"},
         "leaf": {"parent": "mid", "depth": 9, "last_updated": "2026-07-01T00:00:00"}}
    m = cm._merge_tree_nodes_map(a, dict(a))
    assert m["root"]["depth"] == 1                            # root keeps merged depth
    assert m["mid"]["depth"] == 2                             # root + 1
    assert m["leaf"]["depth"] == 3                            # mid + 1 (stale 9 corrected)
    assert cm._tree_structural_integrity(m) == []


def test_tree_structural_integrity_catches_bad_map():
    # negative test: the checker MUST flag a hand-built inconsistent map.
    bad = {"n": {"parent": None, "children": ["ghost", "c"]},
           "c": {"parent": "other", "children": []}}          # ghost missing; c.parent != n
    issues = cm._tree_structural_integrity(bad)
    assert any("orphan" in i for i in issues)                 # ghost is an orphan child
    assert any("asymmetry" in i for i in issues)              # c.parent != n
    assert cm._tree_structural_integrity({}) == []            # empty map is clean


def test_tree_structural_integrity_catches_dangling_parent():
    #  (fresh-eyes review of -b): a node whose parent points to
    # a slug ABSENT from the map is a parentless orphan with a broken reference.
    # Post-rebuild it appears in NO node's children (the parent doesn't exist to
    # list it), so the orphan- and symmetry-checks in the children loop never see
    # it. The old `p in keys` guard silently passed this as clean; the dangling-
    # parent branch now catches it.
    merged = {"root": {"parent": None, "children": [], "child_count": 0,
                       "node_type": "leaf", "depth": 1},
              "widget": {"parent": "DELETED_NODE", "children": [], "child_count": 0,
                         "node_type": "leaf", "depth": 1}}
    issues = cm._tree_structural_integrity(merged)
    assert any("dangling" in i for i in issues)                 # NOW flagged
    assert any("widget" in i and "DELETED_NODE" in i for i in issues)
    # the dangling ref is the ONLY violation — no false orphan/asymmetry noise
    assert not any(i.startswith("orphan:") for i in issues)
    assert not any(i.startswith("asymmetry:") for i in issues)


def test_tree_map_merge_preserves_dangling_input_and_checker_flags():
    # End-to-end: an already-inconsistent INPUT (B's leaf points to a parent that
    # B does not have, and A has no such node either) flows through the merge.
    # Key-union PRESERVES leaf (merge must not silently drop or "fix" it), and the
    # checker now flags the surviving dangling reference instead of a false-clean.
    # (This is the only way a dangling parent reaches a merged map — two INTERNALLY
    # CONSISTENT inputs cannot produce one, since LWW always picks a parent that
    # existed on its source side and the union preserves it.)
    a = _tmap(("root", None, "2026-07-01T00:00:00"))
    b = {"root": {"parent": None, "last_updated": "2026-07-01T00:00:00",
                  "summary": "r", "retrieval_count": 1},
         "leaf": {"parent": "vanished", "last_updated": "2026-07-05T00:00:00",
                  "summary": "l", "retrieval_count": 1}}
    ab = cm._merge_tree_nodes_map(a, b)
    ba = cm._merge_tree_nodes_map(b, a)
    assert ab == ba                                             # still commutative
    assert "leaf" in ab and ab["leaf"]["parent"] == "vanished"  # node + ref preserved
    assert "vanished" not in ab                                 # parent genuinely absent
    issues = cm._tree_structural_integrity(ab)
    assert any("dangling" in i and "vanished" in i for i in issues)


def test_tree_map_multiround_convergence():
    a = _tmap(("root", None, "2026-07-01T00:00:00"), ("p", "root", "2026-07-02T00:00:00"))
    b = _tmap(("root", None, "2026-07-01T00:00:00"), ("q", "root", "2026-07-02T00:00:00"))
    m = cm._merge_tree_nodes_map(a, b)
    assert cm._merge_tree_nodes_map(m, b) == m                # fixpoint
    assert cm._merge_tree_nodes_map(a, m) == m


# --- _tree.yaml TOP-LEVEL merge_tree handler (-c) -------------------
# merge_tree assembles the a/b node reconcile with the 7 top-level field merges,
# emits byte-exact _tree.yaml, and is the registered _HANDLERS entry. These lock
# commutativity + each top-level rule + CRLF tolerance on the FULL document — the
# integration-test half of sub-goal C (a real both-diverged _tree.yaml converges).
def _tree_bytes(nodes=None, growth=None, last_updated="2026-07-01", total_entities=0,
                unmapped=None, xrefs=None, entity_index=None, maintenance=None):
    """Build a realistic _tree.yaml doc as bytes via the module's own serializer."""
    doc = {
        "last_updated": last_updated,
        "tree_growth_log": growth if growth is not None else [],
        "unmapped_categories": unmapped if unmapped is not None else [],
        "cross_references": xrefs if xrefs is not None else [],
        "entity_index": entity_index if entity_index is not None else {},
        "total_entities": total_entities,
        "nodes": nodes if nodes is not None else {},
        "maintenance": maintenance if maintenance is not None else {},
    }
    return cm._dump_tree_yaml(doc)


def test_merge_tree_commutative_full_doc():
    # THE integration test: a real both-diverged _tree.yaml (divergent nodes +
    # growth log + top-level fields) converges byte-identically either arg order.
    a = _tree_bytes(
        nodes=_tmap(("root", None, "2026-07-01T00:00:00"), ("p", "root", "2026-07-02T00:00:00")),
        growth=[{"op": "ADD", "node": "p", "date": "2026-07-02", "reason": "a-add"}],
        last_updated="2026-07-05", total_entities=3, entity_index={"e1": {"v": 1}})
    b = _tree_bytes(
        nodes=_tmap(("root", None, "2026-07-01T00:00:00"), ("q", "root", "2026-07-03T00:00:00")),
        growth=[{"op": "ADD", "node": "q", "date": "2026-07-03", "reason": "b-add"}],
        last_updated="2026-07-04", total_entities=5, entity_index={"e2": {"v": 2}})
    ab, ba = cm.merge_tree(a, b), cm.merge_tree(b, a)
    assert ab == ba                                            # byte-identical (commutative)
    m = yaml.safe_load(ab.decode())
    assert set(m["nodes"]) == {"root", "p", "q"}               # node union, zero loss
    assert m["last_updated"] == "2026-07-05"                   # strictly-newer
    assert m["total_entities"] == 5                            # MAX
    assert m["entity_index"] == {"e1": {"v": 1}, "e2": {"v": 2}}  # key union
    ops = [(e["op"], e["node"], e["date"]) for e in m["tree_growth_log"]]
    assert ops == [("ADD", "p", "2026-07-02"), ("ADD", "q", "2026-07-03")]  # chronological
    assert cm._tree_structural_integrity(m["nodes"]) == []     # structurally clean


def test_merge_tree_growth_log_dedup_and_chronological_order():
    # same (op,node,date) identity dedups to ONE; distinct events kept; emitted in
    # chronological (date,op,node) order regardless of input order.
    shared = {"op": "DECOMPOSE", "node": "x", "date": "2026-06-01", "reason": "same"}
    a = _tree_bytes(growth=[{"op": "ADD", "node": "late", "date": "2026-07-01", "reason": "a"}, shared])
    b = _tree_bytes(growth=[shared, {"op": "ADD", "node": "early", "date": "2026-05-01", "reason": "b"}])
    ab, ba = cm.merge_tree(a, b), cm.merge_tree(b, a)
    assert ab == ba
    log = yaml.safe_load(ab.decode())["tree_growth_log"]
    keys = [(e["op"], e["node"], e["date"]) for e in log]
    assert keys == [("ADD", "early", "2026-05-01"),
                    ("DECOMPOSE", "x", "2026-06-01"),
                    ("ADD", "late", "2026-07-01")]             # chronological
    assert keys.count(("DECOMPOSE", "x", "2026-06-01")) == 1   # shared deduped


def test_merge_tree_crlf_tolerance_converges_to_lf():
    a = _tree_bytes(nodes=_tmap(("root", None, "2026-07-01T00:00:00")), last_updated="2026-07-02")
    a_crlf = a.replace(b"\n", b"\r\n")                         # simulate Windows-written file
    b = _tree_bytes(nodes=_tmap(("root", None, "2026-07-01T00:00:00")), last_updated="2026-07-01")
    ab, ba = cm.merge_tree(a_crlf, b), cm.merge_tree(b, a_crlf)
    assert ab == ba                                           # commutative w/ mixed line endings
    assert b"\r\n" not in ab                                  # output LF (matches tree.write_tree)


def test_merge_tree_idempotent_fixpoint():
    a = _tree_bytes(nodes=_tmap(("root", None, "2026-07-01T00:00:00"), ("p", "root", "2026-07-02T00:00:00")),
                    growth=[{"op": "ADD", "node": "p", "date": "2026-07-02", "reason": "a"}], last_updated="2026-07-03")
    b = _tree_bytes(nodes=_tmap(("root", None, "2026-07-01T00:00:00"), ("q", "root", "2026-07-02T00:00:00")),
                    growth=[{"op": "ADD", "node": "q", "date": "2026-07-02", "reason": "b"}], last_updated="2026-07-02")
    m = cm.merge_tree(a, b)
    assert cm.merge_tree(m, b) == m                           # fixpoint: re-merging loser is a no-op
    assert cm.merge_tree(a, m) == m


def test_merge_tree_maintenance_and_lists():
    a = _tree_bytes(maintenance={"last_maintain_at": "2026-07-05", "backlog": 10},
                    unmapped=["cat-a"], xrefs=[{"from": "x", "to": "y"}])
    b = _tree_bytes(maintenance={"last_maintain_at": "2026-07-03", "backlog": 15},
                    unmapped=["cat-b"], xrefs=[{"from": "x", "to": "y"}])   # xref dup
    ab, ba = cm.merge_tree(a, b), cm.merge_tree(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert m["maintenance"]["last_maintain_at"] == "2026-07-05"  # newer ISO via content-larger
    assert m["maintenance"]["backlog"] == 15                     # numeric MAX
    assert m["unmapped_categories"] == ["cat-a", "cat-b"]        # sorted union
    assert len(m["cross_references"]) == 1                       # identical xref deduped


def test_merge_tree_non_dict_input_commutative():
    good = _tree_bytes(nodes=_tmap(("root", None, "2026-07-01T00:00:00")))
    listy = b"- one\n- two\n"                                  # parses to a list -> non-dict guard
    ab, ba = cm.merge_tree(good, listy), cm.merge_tree(listy, good)
    assert ab == ba                                            # guard is commutative
    # Defect 4 fix: the guard now serializes the content-chosen input through the
    # canonical path (not raw bytes verbatim), so byte-differing-but-content-equal
    # non-dicts still converge. `good`'s dict _canon > the list's, so it is chosen;
    # the output parses back to good's CONTENT (canonicalized), never corrupt.
    assert yaml.safe_load(ab.decode()) == yaml.safe_load(good.decode())


def test_merge_tree_dict_key_order_canonicalized_commutative():
    # Defect 1 regression (guard-907): two docs with IDENTICAL content but different
    # dict-key INSERTION order (in a cross_reference list-dict AND an entity_index
    # value) must merge byte-identically either arg order. Pre-fix, _dump_tree_yaml's
    # sort_keys=False leaked the _canon-tie dedup survivor's key order arg-order-
    # dependently. dict literals preserve insertion order (py3.7+), so the two sides
    # serialize to different bytes before the terminal canonicalization folds them.
    a = _tree_bytes(xrefs=[{"from": "x", "to": "y"}], entity_index={"e": {"alpha": 1, "beta": 2}})
    b = _tree_bytes(xrefs=[{"to": "y", "from": "x"}], entity_index={"e": {"beta": 2, "alpha": 1}})
    assert a != b                                              # confirm key-order perturbation landed in bytes
    ab, ba = cm.merge_tree(a, b), cm.merge_tree(b, a)
    assert ab == ba                                            # byte-identical despite key-order divergence
    m = yaml.safe_load(ab.decode())
    assert m["cross_references"] == [{"from": "x", "to": "y"}]  # content preserved + deduped
    assert m["entity_index"]["e"] == {"alpha": 1, "beta": 2}


def test_merge_tree_int_float_scalar_canonicalized_commutative():
    # Defect 2 regression: total_entities 10 (int) vs 10.0 (float) is the same VALUE
    # but different bytes; max() returns whichever arg came first on a tie, so the
    # output type was arg-order-dependent. Canonicalization folds integral float->int.
    a = _tree_bytes(total_entities=10)
    b = _tree_bytes(total_entities=10.0)
    ab, ba = cm.merge_tree(a, b), cm.merge_tree(b, a)
    assert ab == ba                                            # commutative despite int/float split
    assert yaml.safe_load(ab.decode())["total_entities"] == 10
    assert b"10.0" not in ab                                   # serialized as int, not float


def test_merge_tree_str_vs_date_last_updated_commutative():
    # Defect 3 regression: last_updated as a quoted str vs an UNQUOTED YAML date
    # (which safe_load parses to datetime.date). Same calendar date, different type ->
    # different serialized bytes pre-fix. Canonicalization stringifies date->isoformat.
    a = _tree_bytes(last_updated="2026-07-01")                # str, _dump quotes it
    b_date = a.replace(b"last_updated: '2026-07-01'", b"last_updated: 2026-07-01")  # unquoted -> date
    assert b_date != a                                        # confirm the unquote perturbation landed
    ab, ba = cm.merge_tree(a, b_date), cm.merge_tree(b_date, a)
    assert ab == ba                                           # commutative despite str/date type split
    assert yaml.safe_load(ab.decode())["last_updated"] == "2026-07-01"


# --- _HANDLERS registry integrity () ------------------------------
# The _HANDLERS dict literal (coordination_merge.py) is edited from BOTH boxes at
# every fork reconcile (55+ keys). Python dict literals accept DUPLICATE keys
# SILENTLY (last value wins), so a merge that duplicates a store key with a
# DIFFERENT handler would silently misroute that store's both-diverged merge with
# ZERO runtime signal. This AST-level check (supersedes the ad-hoc re.findall used
# at the  resolution) asserts every registry key is unique in SOURCE --
# ast preserves duplicates because Python dedups only at dict CONSTRUCTION, not at
# parse. Complements guard-907 (handlers must be COMMUTATIVE): guard-907 governs
# each handler's behavior; this governs the registry's key-uniqueness.
_CM_SOURCE = Path(__file__).resolve().parent.parent / "coordination_merge.py"


def _handlers_key_nodes():
    """Source-order list of _HANDLERS dict-literal keys (duplicates PRESERVED) as
    (key_string, lineno) tuples, via AST. Handles both the annotated form
    (`_HANDLERS: Dict[...] = {...}`) and a bare `_HANDLERS = {...}`."""
    tree = ast.parse(_CM_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        is_handlers = (
            (isinstance(node, ast.AnnAssign)
             and isinstance(node.target, ast.Name)
             and node.target.id == "_HANDLERS")
            or (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_HANDLERS"
                        for t in node.targets))
        )
        if is_handlers:
            assert isinstance(node.value, ast.Dict), \
                "_HANDLERS is not a dict literal (AST expected ast.Dict)"
            out = []
            for k in node.value.keys:
                # k is None only for `**spread` entries — none exist here, and one
                # appearing would itself be worth failing on.
                assert isinstance(k, ast.Constant) and isinstance(k.value, str), \
                    f"_HANDLERS key is not a string literal: " \
                    f"{ast.dump(k) if k is not None else 'None (** unpacking)'}"
                out.append((k.value, k.lineno))
            return out
    raise AssertionError(
        "_HANDLERS assignment not found in coordination_merge.py — the registry "
        "was renamed or restructured; update this test.")


def test_handlers_registry_has_no_duplicate_keys():
    """: the _HANDLERS store->handler registry must have NO duplicate
    keys in source. A dup key with a different handler is a silent last-wins
    misroute at every fork reconcile (Python dict-literal semantics)."""
    keys = _handlers_key_nodes()
    seen = {}
    dups = []
    for key, lineno in keys:
        if key in seen:
            dups.append((key, seen[key], lineno))
        else:
            seen[key] = lineno
    assert not dups, (
        "_HANDLERS has duplicate key(s) — Python takes last-wins, silently "
        "misrouting the store's merge handler: "
        + "; ".join(f"{k!r} (lines {a} and {b})" for k, a, b in dups)
    )


def test_handlers_registry_is_nonempty_and_ast_findable():
    """Non-vacuity guard: if the AST extractor silently matched nothing (or the
    wrong node), the dup-key test above would pass VACUOUSLY on an empty key list.
    Pin the registry as AST-discoverable AND populated so the guard has teeth."""
    keys = _handlers_key_nodes()
    assert len(keys) >= 20, (
        f"_HANDLERS AST extraction found only {len(keys)} keys — expected the full "
        f"registry (~55). The extractor likely matched the wrong node; fix it "
        f"before trusting the dup-key check.")


# ---------------------------------------------------------------------------
# forged-skills.yaml — keyed dict union (; CURE for the  +
#  stale-base row-clobber incidents)
# ---------------------------------------------------------------------------

def _fs(skills: dict) -> bytes:
    return yaml.dump({"skills": skills}, default_flow_style=False,
                     sort_keys=False).encode()


def test_fs_disjoint_union_byte_commutative():
    a = _fs({"skill-a": {"parent": "p", "forged_date": "2026-07-01",
                         "forged_by": "alpha", "triggers": ["ta"]}})
    b = _fs({"skill-b": {"parent": "p", "forged_date": "2026-07-02",
                         "forged_by": "echo", "triggers": ["tb"]}})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert sorted(m["skills"]) == ["skill-a", "skill-b"]   # both rows survive


def test_fs_same_skill_newer_forged_date_wins():
    old = {"parent": "p", "forged_date": "2026-07-01", "triggers": ["old"]}
    new = {"parent": "p", "forged_date": "2026-07-10", "triggers": ["new"]}
    a, b = _fs({"skill-x": old}), _fs({"skill-x": new})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert m["skills"]["skill-x"]["triggers"] == ["new"]
    # Whole-record winner — the losing side's fields do NOT blend in.
    assert m["skills"]["skill-x"]["forged_date"] == "2026-07-10"


def test_fs_date_tie_richer_side_wins():
    lean = {"parent": "p", "forged_date": "2026-07-01"}
    rich = {"parent": "p", "forged_date": "2026-07-01",
            "gap_ref": "gap-9", "companion_scripts": ["s.sh"]}
    a, b = _fs({"skill-y": lean}), _fs({"skill-y": rich})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["skills"]["skill-y"].get("gap_ref") == "gap-9"


def test_fs_retired_status_row_survives_union():
    # Retirement is an explicit status field, never deletion — a retired row
    # present on ONE side must survive the merge (union semantics), so a
    # stale-base peer can never clobber it away.
    a = _fs({"skill-live": {"parent": "p", "forged_date": "2026-07-01"}})
    b = _fs({"skill-live": {"parent": "p", "forged_date": "2026-07-01"},
             "skill-retired": {"parent": "p", "forged_date": "2026-06-01",
                               "status": "retired"}})
    m = yaml.safe_load(cm.merge_forged_skills(a, b).decode())
    assert m["skills"]["skill-retired"]["status"] == "retired"
    assert cm.merge_forged_skills(a, b) == cm.merge_forged_skills(b, a)


def test_fs_multiround_settle_idempotent():
    a = _fs({"skill-a": {"forged_date": "2026-07-01", "parent": "p"}})
    b = _fs({"skill-a": {"forged_date": "2026-07-02", "parent": "p"},
             "skill-b": {"forged_date": "2026-07-01", "parent": "q"}})
    m1 = cm.merge_forged_skills(a, b)
    m2 = cm.merge_forged_skills(m1, m1)
    assert m1 == m2                        # fixpoint after one merge
    assert cm.merge_forged_skills(m1, b) == m1   # stale replay settles


# --- amendment lane () -------------------------------------------
# The keyed union above cured row DELETION and left row AMENDMENT broken.
# Amending an existing row (adding a trigger, fixing a companion_scripts path)
# bumps no forged_date and adds no FIELD, so pre-fix it fell to the _canon
# lexicographic tiebreak and could lose DETERMINISTICALLY — measured on cc-05
# 2026-07-28: a 4-trigger addition lost 10-to-6, byte-identically with the args
# swapped, so retrying could never win. `amended_at` is tier 0 (guard-1153:
# LWW on a timestamp written BY THE SAME MUTATION that writes the field).

_BASE = {"parent": "p", "type": "utility", "forged_date": "2026-07-11",
         "forged_by": "zeta", "gap_ref": "gap-12", "triggers": ["t1", "t2"]}


def _amended(**over):
    r = dict(_BASE)
    r.update(over)
    return r


def test_fs_amendment_with_stamp_beats_untouched_peer():
    """The live shape: an amended row vs an untouched peer copy. NOTE this case
    is NOT tier-0-discriminating — the stamp also adds a FIELD, so tier 2
    (more fields) already picks the amended side and the assertion holds with
    tier 0 removed. Kept as a consistency check of the real-world shape; the
    tier-0-load-bearing cases are the `outranks_forged_date` (tier 1) and
    `outranks_field_count` (tier 2) tests below. (Mutation-tested 2026-07-28:
    deleting the tier-0 block fails ONLY those two.)"""
    amended = _amended(triggers=["t1", "t2", "t3", "t4"],
                       amended_at="2026-07-28T08:05:00")
    stale = _amended()
    assert len(amended) == len(stale) + 1   # the stamp itself differs in arity
    a, b = _fs({"skill-z": amended}), _fs({"skill-z": stale})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba                                            # guard-907
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == \
        ["t1", "t2", "t3", "t4"]


def test_fs_newer_amendment_wins_over_older_amendment():
    older = _amended(triggers=["t1", "t2", "old"], amended_at="2026-07-28T08:00:00")
    newer = _amended(triggers=["t1", "t2", "new"], amended_at="2026-07-28T09:00:00")
    a, b = _fs({"skill-z": older}), _fs({"skill-z": newer})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == \
        ["t1", "t2", "new"]


def test_fs_amendment_stamp_outranks_forged_date():
    """Tier 0 beats tier 1: an amendment to an OLDER row still wins, because a
    re-forge would carry its own (newer) amended_at if it meant to supersede."""
    amended_old = _amended(forged_date="2026-07-01", triggers=["kept"],
                           amended_at="2026-07-28T08:05:00")
    plain_new = _amended(forged_date="2026-07-20", triggers=["dropped"])
    a, b = _fs({"skill-z": amended_old}), _fs({"skill-z": plain_new})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == ["kept"]


def test_fs_amendment_stamp_outranks_field_count():
    """Tier 0 beats tier 2 — the second load-bearing case, and the one the
    steady state produces. Once the writer contract is live BOTH sides carry a
    stamp, so the stamp no longer inflates arity and tier 2 stops rescuing the
    amendment for free. Here the peer is RICHER (two extra fields) but carries
    the OLDER stamp: without tier 0 the field-count tier picks the peer and the
    amendment is lost, which is the original defect wearing a different hat."""
    amended = _amended(triggers=["t1", "t2", "kept"],
                       amended_at="2026-07-28T09:00:00")
    richer_older = _amended(triggers=["dropped"], amended_at="2026-07-20T00:00:00",
                            note="n", restored="r")
    assert len(richer_older) > len(amended)          # tier 2 favours the peer
    a, b = _fs({"skill-z": amended}), _fs({"skill-z": richer_older})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba                                                  # guard-907
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == \
        ["t1", "t2", "kept"]


def test_fs_unquoted_amendment_beats_older_quoted():
    """guard-371 on the amended_at tier. Quoting is INVISIBLE in YAML, and
    amending is a hand-edit, so an unquoted stamp is the likely real-world
    form — PyYAML parses it as datetime, whose str() separator is a SPACE
    (0x20 < 'T'). Un-normalized, a newer unquoted stamp sorted BELOW any older
    quoted one sharing the date, inverting the tier. (The sibling resolver's
    forged_date leg was always safe — str(date) has no separator at all —
    which is how the datetime case rode along unnoticed.)"""
    newer_unquoted = (b"skills:\n  skill-z:\n    triggers: [kept]\n"
                      b"    amended_at: 2026-07-28T09:00:00\n")
    older_quoted = (b"skills:\n  skill-z:\n    triggers: [dropped]\n"
                    b"    amended_at: '2026-07-28T08:00:00'\n")
    ab = cm.merge_forged_skills(newer_unquoted, older_quoted)
    ba = cm.merge_forged_skills(older_quoted, newer_unquoted)
    assert ab == ba                                                  # guard-907
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == ["kept"]


def test_fs_removal_survives_amendment_stamp():
    """guard-1072 was NOT traded away. A field-level UNION of triggers — the
    obvious alternative fix — has no deletion semantics and would resurrect a
    deliberately removed trigger from any peer holding the old row. The
    whole-record stamp keeps removal expressible."""
    trimmed = _amended(triggers=["t1"], amended_at="2026-07-28T08:06:00")
    a, b = _fs({"skill-z": trimmed}), _fs({"skill-z": _amended()})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    got = yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"]
    assert got == ["t1"] and "t2" not in got      # removal NOT resurrected


def test_fs_no_stamp_falls_through_to_prior_tiers():
    """A writer that never sets amended_at is no worse off than pre-fix: the
    record still resolves on forged_date -> field count -> _canon."""
    old = _amended(forged_date="2026-07-01", triggers=["old"])
    new = _amended(forged_date="2026-07-10", triggers=["new"])
    a, b = _fs({"skill-z": old}), _fs({"skill-z": new})
    ab, ba = cm.merge_forged_skills(a, b), cm.merge_forged_skills(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["skills"]["skill-z"]["triggers"] == ["new"]


def test_fs_amendment_idempotent_and_settles():
    amended = _amended(triggers=["t1", "t2", "t3"], amended_at="2026-07-28T08:05:00")
    a, b = _fs({"skill-z": amended}), _fs({"skill-z": _amended()})
    m1 = cm.merge_forged_skills(a, b)
    assert cm.merge_forged_skills(m1, m1) == m1      # fixpoint
    assert cm.merge_forged_skills(m1, b) == m1       # stale peer replay settles


def test_fs_degenerate_inputs_commutative():
    good = _fs({"skill-a": {"forged_date": "2026-07-01"}})
    for bad in (b"", b"- just\n- a list\n", b"scalar\n"):
        ab = cm.merge_forged_skills(good, bad)
        ba = cm.merge_forged_skills(bad, good)
        assert ab == ba
        assert b"skill-a" in ab            # parseable dict side preferred


def test_fs_unquoted_yaml_date_newer_side_wins():
    # Unquoted YAML dates parse as datetime.date (merge_tree str-vs-date bug
    # class); str() coercion must let a date-typed NEWER side win against a
    # quoted-string older side, byte-commutatively.
    a = b'skills:\n  skill-x:\n    forged_date: 2026-07-10\n    parent: p\n'
    b_ = b'skills:\n  skill-x:\n    forged_date: "2026-07-01"\n    parent: q\n'
    ab, ba = cm.merge_forged_skills(a, b_), cm.merge_forged_skills(b_, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["skills"]["skill-x"]["parent"] == "p"


# --- skill-relations.yaml handler () -------------------------------


def _sr(rels=None, log=None, last_updated=None, extra=None) -> bytes:
    doc = {"last_updated": last_updated}
    if rels is not None:
        doc["forged_relations"] = rels
    if log is not None:
        doc["co_invocation_log"] = log
    if extra:
        doc.update(extra)
    return yaml.dump(doc, default_flow_style=False, sort_keys=False).encode()


def _rel(src, tgt, typ="compose_with", **kw):
    r = {"source": src, "target": tgt, "type": typ}
    r.update(kw)
    return r


def _logent(goal, skills, date):
    return {"goal_id": goal, "skills": list(skills), "date": date}


def test_sr_disjoint_union_byte_commutative():
    a = _sr(rels=[_rel("s1", "t1")], log=[_logent("g-1", ["x", "y"], "2026-07-01T10:00:00")])
    b = _sr(rels=[_rel("s2", "t2")], log=[_logent("g-2", ["p", "q"], "2026-07-02T10:00:00")])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert len(m["forged_relations"]) == 2 and len(m["co_invocation_log"]) == 2


def test_sr_stale_base_clobber_recovered():
    # The incident lane this handler closes: a stale-base full-file writer
    # (remote) lacks local's newest relation + log entry — union restores both.
    fresh = _sr(rels=[_rel("s1", "t1"), _rel("s2", "t2", confidence=0.9)],
                log=[_logent("g-1", ["a", "b"], "2026-07-01T10:00:00"),
                     _logent("g-2", ["c", "d"], "2026-07-02T10:00:00")])
    stale = _sr(rels=[_rel("s1", "t1")],
                log=[_logent("g-1", ["a", "b"], "2026-07-01T10:00:00")])
    for first, second in ((fresh, stale), (stale, fresh)):
        m = yaml.safe_load(cm.merge_skill_relations(first, second).decode())
        assert len(m["forged_relations"]) == 2
        assert len(m["co_invocation_log"]) == 2


def test_sr_same_edge_richer_side_wins():
    bare = _rel("s1", "t1")
    rich = _rel("s1", "t1", confidence=0.9, evidence="probe log")
    a, b = _sr(rels=[bare]), _sr(rels=[rich])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert len(m["forged_relations"]) == 1
    assert m["forged_relations"][0].get("confidence") == 0.9


def test_sr_log_union_respects_cap():
    # cmd_co_invoke tail-caps at co_invocation_log_cap (200) — the union must
    # re-apply it, keeping the NEWEST entries, byte-commutatively.
    cap = cm._co_invocation_log_cap()
    n = cap + 5
    ents = [_logent("g-%04d" % i, ["a", "b"], "2026-01-01T00:%02d:%02d" % (i // 60, i % 60))
            for i in range(n)]
    a = _sr(log=ents[: n // 2 + 3])          # overlapping halves
    b = _sr(log=ents[n // 2 - 3:])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    m = yaml.safe_load(ab.decode())
    assert len(m["co_invocation_log"]) == cap
    kept = {e["goal_id"] for e in m["co_invocation_log"]}
    assert "g-%04d" % (n - 1) in kept        # newest survives
    assert "g-0000" not in kept              # oldest capped out (5 dropped)


def test_sr_multiround_settle_idempotent():
    a = _sr(rels=[_rel("s1", "t1")], log=[_logent("g-1", ["a", "b"], "2026-07-01T10:00:00")])
    b = _sr(rels=[_rel("s2", "t2")], log=[_logent("g-2", ["c", "d"], "2026-07-02T10:00:00")])
    m1 = cm.merge_skill_relations(a, b)
    assert cm.merge_skill_relations(m1, m1) == m1
    assert cm.merge_skill_relations(m1, b) == m1


def test_sr_degenerate_inputs_commutative():
    good = _sr(rels=[_rel("s1", "t1")])
    for bad in (b"", b"- just\n- a list\n", b"scalar\n"):
        ab = cm.merge_skill_relations(good, bad)
        ba = cm.merge_skill_relations(bad, good)
        assert ab == ba
        assert b"s1" in ab


def test_sr_unquoted_date_entries_deterministic():
    # Unquoted YAML timestamps parse as datetime — str() coercion in the sort
    # key must keep the merge crash-free and byte-commutative.
    a = b"co_invocation_log:\n- goal_id: g-1\n  skills: [a, b]\n  date: 2026-07-01T10:00:00\n"
    b_ = b"co_invocation_log:\n- goal_id: g-2\n  skills: [c, d]\n  date: '2026-07-02T10:00:00'\n"
    ab, ba = cm.merge_skill_relations(a, b_), cm.merge_skill_relations(b_, a)
    assert ab == ba
    assert len(yaml.safe_load(ab.decode())["co_invocation_log"]) == 2


def test_sr_last_updated_max_nonnull():
    a = _sr(rels=[], last_updated=None)
    b = _sr(rels=[], last_updated="2026-07-01T00:00:00")
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["last_updated"] == "2026-07-01T00:00:00"
    both_null = cm.merge_skill_relations(_sr(rels=[]), _sr(rels=[]))
    assert yaml.safe_load(both_null.decode())["last_updated"] is None


def test_sr_config_shaped_doc_passthrough():
    # A doc sharing the basename but NOT the registry shape (core/config/
    # skill-relations.yaml — never synced, defensive) must not gain invented
    # forged_relations / co_invocation_log keys.
    cfg = yaml.dump({"config": {"co_invocation_log_cap": 200}},
                    default_flow_style=False, sort_keys=False).encode()
    m = yaml.safe_load(cm.merge_skill_relations(cfg, cfg).decode())
    assert "forged_relations" not in m and "co_invocation_log" not in m


def test_sr_unquoted_datetime_last_updated_newer_wins():
    # Fresh-eyes P1 (2026-07-17): str(datetime) uses a space where ISO strings
    # use 'T' — un-normalized, a chronologically NEWER unquoted datetime lost
    # to an older quoted string. Also pins the normalized-TIE case (same
    # instant, different types) to a deterministic arg-order-free winner.
    newer_dt = b"last_updated: 2026-07-17T05:00:00\nforged_relations: []\n"
    older_str = b"last_updated: '2026-07-17T04:00:00'\nforged_relations: []\n"
    ab = cm.merge_skill_relations(newer_dt, older_str)
    ba = cm.merge_skill_relations(older_str, newer_dt)
    assert ab == ba
    won = yaml.safe_load(ab.decode())["last_updated"]
    assert str(won).replace(" ", "T") == "2026-07-17T05:00:00"
    same_dt = b"last_updated: 2026-07-17T05:00:00\nforged_relations: []\n"
    same_str = b"last_updated: '2026-07-17T05:00:00'\nforged_relations: []\n"
    assert (cm.merge_skill_relations(same_dt, same_str)
            == cm.merge_skill_relations(same_str, same_dt))


# --- skill-relation amendment loss () ------------------------------
# Sibling of the _fs_ amendment family above (). _merge_skill_relation
# was MORE exposed than _merge_forged_skill, not less: it has no date leg at
# all, so before tier 0 an amendment that did not change the field COUNT was
# decided by an arbitrary _canon compare. The three load-bearing cases below
# are the ones where tier 0 must OVERRIDE the field-count tier; each was
# mutation-proven by deleting the tier-0 block in coordination_merge.py.


def test_sr_amendment_stamp_outranks_field_count():
    """Tier 0 beats tier 2 — the steady-state case, once the writer contract is
    live and BOTH sides carry a stamp. The peer is RICHER (two extra fields)
    but carries the OLDER stamp: without tier 0 the field-count tier picks the
    peer and the confidence bump is silently lost, which is the defect."""
    amended = _rel("s1", "t1", confidence=0.7,
                   amended_at="2026-07-28T09:00:00")
    richer_older = _rel("s1", "t1", confidence=0.5, evidence="stale",
                        note="n", amended_at="2026-07-20T00:00:00")
    assert len(richer_older) > len(amended)         # tier 2 favours the peer
    a, b = _sr(rels=[amended]), _sr(rels=[richer_older])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba                                                  # guard-907
    assert yaml.safe_load(ab.decode())["forged_relations"][0]["confidence"] == 0.7


def test_sr_unstamped_peer_sorts_oldest():
    """The MIGRATION-WINDOW case, and the one that pins the "" sentinel: a peer
    copy written before the writer contract existed has no amended_at at all.
    It must sort OLDEST (not tie, not win), even though it is richer — else the
    first amendment after rollout loses to every un-upgraded peer."""
    amended = _rel("s1", "t1", amended_at="2026-07-28T09:00:00")
    unstamped_richer = _rel("s1", "t1", confidence=0.5, evidence="pre-fix")
    assert len(unstamped_richer) > len(amended)     # tier 2 favours the peer
    a, b = _sr(rels=[amended]), _sr(rels=[unstamped_richer])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba                                                  # guard-907
    assert "confidence" not in yaml.safe_load(ab.decode())["forged_relations"][0]


def test_sr_removal_survives_amendment_stamp():
    """guard-1072 was NOT traded away. A field-level UNION of confidence/
    evidence — the obvious alternative fix — has no deletion semantics and
    would resurrect a retracted evidence string from any peer holding the old
    row. The whole-record stamp keeps removal expressible."""
    trimmed_newer = _rel("s1", "t1", confidence=0.7,
                         amended_at="2026-07-28T09:00:00")
    full_older = _rel("s1", "t1", confidence=0.7, evidence="retracted probe log",
                      amended_at="2026-07-20T00:00:00")
    a, b = _sr(rels=[trimmed_newer]), _sr(rels=[full_older])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    won = yaml.safe_load(ab.decode())["forged_relations"][0]
    assert "evidence" not in won                  # removal NOT resurrected


def test_sr_unquoted_amendment_beats_older_quoted():
    """guard-371, and the case this fix most needed: cmd_add REFUSES duplicates,
    so amending an edge is a HAND-EDIT — and a hand-editor typing YAML omits
    quotes, which PyYAML parses as datetime whose str() separator is a SPACE
    (0x20 < 'T'). Un-normalized, the newer hand-amended row lost to ANY older
    quoted peer sharing the date, silently reinstating the exact defect on the
    exact workflow the fix documents. Measured, then fixed, via _ts_key."""
    newer_unquoted = (b"forged_relations:\n- source: s1\n  target: t1\n"
                      b"  type: compose_with\n  confidence: 0.9\n"
                      b"  amended_at: 2026-07-28T09:00:00\nlast_updated: null\n")
    older_quoted = (b"forged_relations:\n- source: s1\n  target: t1\n"
                    b"  type: compose_with\n  confidence: 0.1\n"
                    b"  amended_at: '2026-07-28T08:00:00'\nlast_updated: null\n")
    ab = cm.merge_skill_relations(newer_unquoted, older_quoted)
    ba = cm.merge_skill_relations(older_quoted, newer_unquoted)
    assert ab == ba                                                  # guard-907
    assert yaml.safe_load(ab.decode())["forged_relations"][0]["confidence"] == 0.9


def test_sr_newer_amendment_wins_over_older_amendment():
    """The plain two-amendments shape. NOTE this case is NOT
    tier-0-discriminating: with equal field counts the _canon tiebreak sorts
    keys, `amended_at` sorts first among them, so the newer side wins on
    content anyway. Kept as a consistency check of the steady-state shape —
    the tier-0-load-bearing cases are the three above."""
    older = _rel("s1", "t1", confidence=0.5, amended_at="2026-07-28T08:00:00")
    newer = _rel("s1", "t1", confidence=0.9, amended_at="2026-07-28T09:00:00")
    assert len(older) == len(newer)
    a, b = _sr(rels=[older]), _sr(rels=[newer])
    ab, ba = cm.merge_skill_relations(a, b), cm.merge_skill_relations(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["forged_relations"][0]["confidence"] == 0.9


# --- evolution event streams () -----------------------------------
def _evo(recs):
    return _rb(recs)


def test_evo_stub_vs_final_final_wins_both_orders():
    # Box A finalized the stub; box B still holds awaiting_completion.
    # Line-union would keep BOTH lines (the bug this handler exists to avoid);
    # the keyed merge must emit exactly one record, status=final.
    stub = {"revision_id": "self-x-0001", "ts": "2026-07-18T00:00:00",
            "status": "awaiting_completion", "reasoning": None}
    final = {"revision_id": "self-x-0001", "ts": "2026-07-18T00:00:00",
             "status": "final", "reasoning": "did the thing because reasons"}
    a, b = _evo([stub]), _evo([final])
    ab, ba = cm.merge_evolution_stream(a, b), cm.merge_evolution_stream(b, a)
    assert ab == ba
    recs = _recs(ab)
    assert len(recs) == 1 and recs[0]["status"] == "final"


def test_evo_final_beats_expired():
    # Box A expired the stub (24h honest fallback); box B completed it with a
    # real rationale. The completion carries the WHY — it must win.
    exp = {"revision_id": "skill-y-0002", "ts": "2026-07-17T09:00:00",
           "status": "expired"}
    fin = {"revision_id": "skill-y-0002", "ts": "2026-07-17T09:00:00",
           "status": "final", "reasoning": "reconstructed from journal"}
    ab = cm.merge_evolution_stream(_evo([exp]), _evo([fin]))
    ba = cm.merge_evolution_stream(_evo([fin]), _evo([exp]))
    assert ab == ba
    recs = _recs(ab)
    assert len(recs) == 1 and recs[0]["status"] == "final"


def test_evo_disjoint_union_sorted_and_idempotent():
    r1 = {"revision_id": "rule-a-0001", "ts": "2026-07-16T10:00:00", "status": "final"}
    r2 = {"revision_id": "rule-b-0001", "ts": "2026-07-17T10:00:00", "status": "awaiting_completion"}
    ab = cm.merge_evolution_stream(_evo([r1]), _evo([r2]))
    ba = cm.merge_evolution_stream(_evo([r2]), _evo([r1]))
    assert ab == ba
    assert [r["revision_id"] for r in _recs(ab)] == ["rule-a-0001", "rule-b-0001"]
    # idempotent: merging the merged result with either side is a fixed point
    assert cm.merge_evolution_stream(ab, _evo([r1])) == ab
    assert cm.merge_evolution_stream(ab, ab) == ab


def test_evo_malformed_record_without_revision_id_survives():
    good = {"revision_id": "self-z-0003", "ts": "2026-07-18T01:00:00", "status": "final"}
    stray = {"note": "malformed line without revision_id"}
    ab = cm.merge_evolution_stream(_evo([good, stray]), _evo([good]))
    ba = cm.merge_evolution_stream(_evo([good]), _evo([good, stray]))
    assert ab == ba
    assert len(_recs(ab)) == 2


# --- infra-health.yaml () -----------------------------------------
def _ih(components):
    return yaml.dump({"components": components}).encode()


def test_ih_component_newest_activity_wins_whole_record():
    # Box A probed efs-ssh later (success); box B holds an older failure streak.
    # The WHOLE newer component record must win — no field-mixing.
    a = _ih({"efs-ssh": {"last_success": "2026-07-18T01:00:00", "last_failure": None,
                          "consecutive_failures": 0}})
    b = _ih({"efs-ssh": {"last_success": "2026-07-16T00:00:00",
                          "last_failure": "2026-07-17T00:00:00",
                          "consecutive_failures": 3}})
    ab, ba = cm.merge_infra_health(a, b), cm.merge_infra_health(b, a)
    assert ab == ba
    comp = yaml.safe_load(ab.decode())["components"]["efs-ssh"]
    assert comp["consecutive_failures"] == 0 and comp["last_failure"] is None


def test_ih_component_key_union_both_survive():
    a = _ih({"only-a": {"last_success": "2026-07-18T00:00:00"}})
    b = _ih({"only-b": {"last_failure": "2026-07-18T00:30:00"}})
    ab, ba = cm.merge_infra_health(a, b), cm.merge_infra_health(b, a)
    assert ab == ba
    comps = yaml.safe_load(ab.decode())["components"]
    assert set(comps) == {"only-a", "only-b"}


def test_ih_unparseable_side_byte_tiebreak_commutative():
    good = _ih({"x": {"last_success": "2026-07-18T00:00:00"}})
    bad = b": not yaml : ["
    assert cm.merge_infra_health(good, bad) == cm.merge_infra_health(bad, good)


# --- goal-selection-strategy.yaml () ------------------------------
def _gss(version, last_updated, weights, log):
    return yaml.dump({"version": version, "last_updated": last_updated,
                      "weights": weights, "applications_log": log}).encode()


def test_gss_higher_version_wins_body_log_unions():
    a = _gss(5, "2026-06-14", {"priority": 1.0},
             [{"ts": "2026-07-01T00:00:00", "agent": "alpha", "summary": "a"}])
    b = _gss(6, "2026-07-10", {"priority": 1.0, "opportunity_boost": 0.4},
             [{"ts": "2026-07-11T00:00:00", "agent": "bravo", "summary": "b"}])
    ab, ba = cm.merge_goal_selection_strategy(a, b), cm.merge_goal_selection_strategy(b, a)
    assert ab == ba
    d = yaml.safe_load(ab.decode())
    assert d["version"] == 6 and "opportunity_boost" in d["weights"]
    # applications_log entry-union from BOTH sides, chronological
    assert [e["agent"] for e in d["applications_log"]] == ["alpha", "bravo"]


def test_gss_log_union_respects_writer_cap():
    log_a = [{"ts": f"2026-07-01T00:{i:02d}:00", "agent": "a", "summary": str(i)}
             for i in range(0, 60)]
    log_b = [{"ts": f"2026-07-02T00:{i:02d}:00", "agent": "b", "summary": str(i)}
             for i in range(0, 55)]
    filler = [{"ts": f"2026-06-30T{h:02d}:{i:02d}:00", "agent": "f", "summary": "x"}
              for h in range(3) for i in range(40)]
    a = _gss(5, "2026-06-14", {}, filler + log_a)
    b = _gss(5, "2026-06-14", {}, filler + log_b)
    ab, ba = cm.merge_goal_selection_strategy(a, b), cm.merge_goal_selection_strategy(b, a)
    assert ab == ba
    log = yaml.safe_load(ab.decode())["applications_log"]
    assert len(log) == cm._GSS_APPLICATIONS_LOG_CAP  # re-capped, newest kept
    assert log[-1]["agent"] == "b"


def test_gss_version_tie_newer_last_updated_wins():
    a = _gss(5, "2026-06-14", {"priority": 1.0}, [])
    b = _gss(5, "2026-07-01", {"priority": 2.0}, [])
    ab, ba = cm.merge_goal_selection_strategy(a, b), cm.merge_goal_selection_strategy(b, a)
    assert ab == ba
    assert yaml.safe_load(ab.decode())["weights"]["priority"] == 2.0


# --- hypothesis-category-bindings.json () --------------------------
def test_hcb_key_union_and_deterministic_value_conflict():
    a = json.dumps({"arc": "node-1", "only-a": "x"}).encode()
    b = json.dumps({"arc": "node-2", "only-b": "y"}).encode()
    ab, ba = (cm.merge_hypothesis_category_bindings(a, b),
              cm.merge_hypothesis_category_bindings(b, a))
    assert ab == ba
    d = json.loads(ab.decode())
    assert set(d) == {"arc", "only-a", "only-b"}
    assert d["arc"] in ("node-1", "node-2")  # deterministic canon winner


def test_hcb_output_matches_writer_dump_style():
    # tree-accuracy-sync.py dumps indent=2, sort_keys=True, NO trailing newline
    # — byte-matching it means a semantically-null merge converges.
    a = json.dumps({"k": "v"}, indent=2, sort_keys=True).encode()
    assert cm.merge_hypothesis_category_bindings(a, a) == a


# --- alloc_nonce goal identity () ---------------------------------
# ROOT CAUSE (): _goal_identity keyed on (created_at, title). title is
# MUTABLE, so a title edit racing a stale snapshot gave the two copies different
# identities -- they never collapsed in step 1, both landed in the same seq
# bucket, and step 3 displaced one to a fresh id. ONE GOAL BECAME TWO, silently
# (proven live:  carries displaced_from=''). The fix keys on an
# immutable, unique allocation nonce minted at the add-goal chokepoint.

def _nonce_goal(gid, title, nonce=None, created="2026-07-01T00:00:00"):
    g = {"id": gid, "title": title, "created_at": created, "status": "pending"}
    if nonce:
        g["alloc_nonce"] = nonce
    return g


def test_alloc_nonce_collapses_title_edit():
    # THE regression: same logical goal, retitled on one side. Must stay ONE goal.
    n = "n" * 32
    a = [_nonce_goal("g-315-515", "Apply: do the thing", nonce=n)]
    b = [_nonce_goal("g-315-515", "Unblock: do the thing", nonce=n)]
    out = cm._merge_goals(a, b, "315")
    assert len(out) == 1, f"title edit split one goal into {len(out)}"
    assert out[0]["id"] == "g-315-515"
    assert "displaced_from" not in out[0]


def test_alloc_nonce_keeps_distinct_goals_distinct():
    # The  guarantee: two DISTINCT goals that collided on one id must
    # NOT be field-merged into a franken-record. Different nonces => 2 goals.
    a = [_nonce_goal("g-315-90", "Distinct goal A", nonce="a" * 32)]
    b = [_nonce_goal("g-315-90", "Distinct goal B", nonce="b" * 32)]
    out = cm._merge_goals(a, b, "315")
    assert len(out) == 2
    assert {g["title"] for g in out} == {"Distinct goal A", "Distinct goal B"}


def test_alloc_nonce_distinct_goals_same_second_survive():
    # Discriminates the SHIPPED fix from the tempting cheap one. Keying identity
    # on (id, created_at) would collapse these two DISTINCT same-second goals and
    # lose a writer's content; keying on the nonce keeps both. This test FAILS
    # under that alternative, which is why it is here rather than the assertion
    # above alone.
    same = "2026-07-01T00:00:00"
    a = [_nonce_goal("g-315-91", "Same-second A", nonce="1" * 32, created=same)]
    b = [_nonce_goal("g-315-91", "Same-second B", nonce="2" * 32, created=same)]
    assert len(cm._merge_goals(a, b, "315")) == 2


def test_no_nonce_preserves_legacy_identity():
    # Goals predating the field must behave EXACTLY as before -- the change is a
    # no-op for them. A legacy title edit still splits (that is the pre-fix
    # behaviour, deliberately unchanged), and a legacy same-title pair still
    # collapses by (created_at, title).
    a = [_nonce_goal("g-315-515", "Apply: do the thing")]
    b = [_nonce_goal("g-315-515", "Unblock: do the thing")]
    assert len(cm._merge_goals(a, b, "315")) == 2

    same = [_nonce_goal("g-315-515", "Apply: do the thing")]
    assert len(cm._merge_goals(same, list(same), "315")) == 1


def test_alloc_nonce_merge_is_byte_commutative():
    # guard-907: identity must stay a pure symmetric function of (a, b) so both
    # machines converge byte-identically under the fenced-PUT loop.
    n = "c" * 32
    a = [_nonce_goal("g-315-515", "Apply: t", nonce=n)]
    b = [_nonce_goal("g-315-515", "Unblock: t", nonce=n)]
    ab = json.dumps(cm._merge_goals(a, b, "315"), sort_keys=True)
    ba = json.dumps(cm._merge_goals(b, a, "315"), sort_keys=True)
    assert ab == ba
