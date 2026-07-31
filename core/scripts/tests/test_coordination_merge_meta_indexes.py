"""Unit tests for the  meta RMW index handlers.

Sibling of test_coordination_merge.py and governed by the same invariant:
COMMUTATIVITY — merge(a, b) must be BYTE-IDENTICAL to merge(b, a), because that
byte-level symmetry is what lets two machines each compute the merge from their
own vantage and converge (guard-907).

What makes these three handlers different from the line-union family, and what
most of this file is actually pinning: each record carries DERIVED fields whose
correct merge is a RECOMPUTE, not a carry (guard-1153). A handler that unions
the source list correctly while carrying a stale derived field looks right in a
diff and is wrong in production — the union is visible, the staleness is not.

`test_sq_total_evaluations_is_max_not_len` is the load-bearing one: it pins the
single field on these three stores where the obvious implementation (derive it
from the list) is the WRONG one.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402
yaml = pytest.importorskip("yaml")


def _y(doc) -> bytes:
    return yaml.dump(doc, default_flow_style=False, sort_keys=False,
                     allow_unicode=True, width=200).encode("utf-8")


def _load(blob: bytes):
    return yaml.safe_load(blob.decode("utf-8"))


def _entry(n: int) -> dict:
    """imp@k entry whose learning_value == n, so a tail-window mean is exact."""
    return {"goal_id": f"g-{n:03d}", "date": f"2026-07-01T00:00:{n:02d}",
            "learning_value": float(n)}


def _eval(n: int, score: float = 1.0) -> dict:
    return {"goal_id": f"g-{n:03d}", "date": f"2026-07-01T00:00:{n:02d}",
            "safety": score, "completeness": score, "executability": score,
            "maintainability": score, "cost_awareness": score, "overall": score}


# --- improvement-velocity.yaml ----------------------------------------------

def test_impk_union_is_commutative_and_sorted():
    a = _y({"entries": [_entry(n) for n in (1, 2, 3, 4, 5)],
            "rolling_averages": {"window_5": 999.0}})
    b = _y({"entries": [_entry(n) for n in (1, 2, 3, 6, 7)],
            "rolling_averages": {"window_5": 888.0}})
    ab, ba = cm.merge_improvement_velocity(a, b), cm.merge_improvement_velocity(b, a)
    assert ab == ba, "merge is not byte-commutative (guard-907)"
    got = _load(ab)
    assert [e["goal_id"] for e in got["entries"]] == [f"g-{n:03d}" for n in range(1, 8)]


def test_impk_rolling_averages_are_recomputed_not_carried():
    """guard-1153: the derived window must come from the MERGED tail.

    Both sides carry a deliberately impossible stored value (999.0 / 888.0). A
    handler that carries the field from the base-pick side — the opaque LWW
    default this guardrail exists to forbid — republishes one of them.
    """
    a = _y({"entries": [_entry(n) for n in (1, 2, 3, 4, 5)],
            "rolling_averages": {"window_5": 999.0, "window_10": 999.0, "window_20": 999.0}})
    b = _y({"entries": [_entry(n) for n in (1, 2, 3, 6, 7)],
            "rolling_averages": {"window_5": 888.0, "window_10": 888.0, "window_20": 888.0}})
    got = _load(cm.merge_improvement_velocity(a, b))
    # merged tail is [3,4,5,6,7] -> mean 5.0; <10 and <20 entries -> 0.0
    assert got["rolling_averages"]["window_5"] == 5.0
    assert got["rolling_averages"]["window_10"] == 0.0
    assert got["rolling_averages"]["window_20"] == 0.0
    assert 999.0 not in got["rolling_averages"].values()
    assert 888.0 not in got["rolling_averages"].values()


def test_impk_sorting_makes_the_derived_window_order_independent():
    """The entry SET can match while the derived value does not.

    This is the reason sorting is load-bearing rather than cosmetic: the
    averages read the TAIL, so an unsorted union yields a different window
    depending on which side was concatenated first — merge(a,b) != merge(b,a)
    in VALUE, not merely in byte order.
    """
    a = _y({"entries": [_entry(n) for n in (7, 1, 3)]})
    b = _y({"entries": [_entry(n) for n in (5, 2, 6, 4)]})
    ab, ba = cm.merge_improvement_velocity(a, b), cm.merge_improvement_velocity(b, a)
    assert ab == ba
    assert _load(ab)["rolling_averages"]["window_5"] == 5.0   # mean(3..7)


def test_impk_emits_only_the_keys_its_validator_allows():
    """meta-impk.validate_velocity_structure RAISES on any other top-level key,
    so a passed-through key would yield a file its own writer cannot read back."""
    a = _y({"entries": [_entry(1)], "rolling_averages": {}, "stray_key": 1})
    b = _y({"entries": [_entry(2)], "rolling_averages": {}})
    got = _load(cm.merge_improvement_velocity(a, b))
    assert set(got) == {"entries", "rolling_averages"}


def test_impk_second_merge_converges():
    """rb-5718: verify merge N+1, not just N. Re-merging the result against
    either input must be a fixpoint, or the fenced-PUT loop never terminates."""
    a = _y({"entries": [_entry(n) for n in (1, 2, 3)]})
    b = _y({"entries": [_entry(n) for n in (3, 4, 5)]})
    once = cm.merge_improvement_velocity(a, b)
    assert cm.merge_improvement_velocity(once, a) == once
    assert cm.merge_improvement_velocity(once, b) == once
    assert cm.merge_improvement_velocity(once, once) == once


# --- skill-quality.yaml -----------------------------------------------------

def test_sq_total_evaluations_is_max_not_len():
    """THE load-bearing pin. total_evaluations is a lifetime counter the writer
    increments unconditionally (`get(...,0) + 1`), so it counts evaluations the
    FIFO cap has since evicted. Deriving it from the merged list — the obvious
    implementation — silently resets a long-lived skill's lifetime count.

    guard-1153 sanctions MAX for a monotonic never-repaired counter. MAX is
    knowingly lossy under true concurrent divergence (13 here, not 15); rb-5718
    is why that is the right trade: lossy-but-convergent beats faithful-but-not.
    """
    a = _y({"skills": {"x": {"evaluations": [_eval(1), _eval(2), _eval(3)],
                             "aggregate": {}, "total_evaluations": 13}}})
    b = _y({"skills": {"x": {"evaluations": [_eval(4), _eval(5)],
                             "aggregate": {}, "total_evaluations": 12}}})
    rec = _load(cm.merge_skill_quality(a, b))["skills"]["x"]
    assert len(rec["evaluations"]) == 5
    assert rec["total_evaluations"] == 13, "must be MAX of the two counters"
    assert rec["total_evaluations"] != len(rec["evaluations"]), \
        "regression: counter derived from the list, discarding evicted history"


def test_sq_total_evaluations_floored_at_len():
    """MAX alone can contradict the list it ships with (a counter behind the
    evaluation count is internally impossible); the floor forbids that state."""
    a = _y({"skills": {"x": {"evaluations": [_eval(n) for n in range(1, 6)],
                             "aggregate": {}, "total_evaluations": 0}}})
    b = _y({"skills": {"x": {"evaluations": [_eval(n) for n in range(6, 9)],
                             "aggregate": {}, "total_evaluations": 1}}})
    rec = _load(cm.merge_skill_quality(a, b))["skills"]["x"]
    assert rec["total_evaluations"] >= len(rec["evaluations"]) == 8


def test_sq_evaluations_recapped_at_rolling_window():
    """Without the re-cap a merge resurrects FIFO-evicted evaluations and grows
    the list past the writer's own ROLLING_WINDOW bound."""
    a = _y({"skills": {"x": {"evaluations": [_eval(n) for n in range(1, 16)],
                             "aggregate": {}, "total_evaluations": 15}}})
    b = _y({"skills": {"x": {"evaluations": [_eval(n) for n in range(16, 31)],
                             "aggregate": {}, "total_evaluations": 15}}})
    ab, ba = cm.merge_skill_quality(a, b), cm.merge_skill_quality(b, a)
    assert ab == ba
    rec = _load(ab)["skills"]["x"]
    assert len(rec["evaluations"]) == cm._SQ_ROLLING_WINDOW == 20
    # tail-capped: the OLDEST are dropped, newest survive
    assert rec["evaluations"][-1]["goal_id"] == "g-030"


def test_sq_aggregate_recomputed_from_merged_evaluations():
    """aggregate is fully derived; a carried one describes one side's list."""
    a = _y({"skills": {"x": {"evaluations": [_eval(1, 1.0), _eval(2, 1.0)],
                             "aggregate": {d: 0.42 for d in cm._SQ_DIMENSIONS},
                             "total_evaluations": 2}}})
    b = _y({"skills": {"x": {"evaluations": [_eval(3, 0.0), _eval(4, 0.0)],
                             "aggregate": {d: 0.77 for d in cm._SQ_DIMENSIONS},
                             "total_evaluations": 2}}})
    rec = _load(cm.merge_skill_quality(a, b))["skills"]["x"]
    # merged list is two 1.0s and two 0.0s -> every dimension mean is 0.5
    for dim in cm._SQ_DIMENSIONS:
        assert rec["aggregate"][dim] == 0.5, f"{dim} not recomputed"
    assert rec["aggregate"]["overall"] == 0.5   # weights sum to 1.0


def test_sq_disjoint_skills_both_survive_and_last_updated_advances():
    a = _y({"last_updated": "2026-07-01T00:00:00",
            "skills": {"x": {"evaluations": [_eval(1)], "aggregate": {},
                             "total_evaluations": 1}}})
    b = _y({"last_updated": "2026-07-09T00:00:00",
            "skills": {"y": {"evaluations": [_eval(2)], "aggregate": {},
                             "total_evaluations": 1}}})
    ab, ba = cm.merge_skill_quality(a, b), cm.merge_skill_quality(b, a)
    assert ab == ba
    got = _load(ab)
    assert sorted(got["skills"]) == ["x", "y"]
    assert got["last_updated"] == "2026-07-09T00:00:00"


def test_sq_second_merge_converges():
    a = _y({"skills": {"x": {"evaluations": [_eval(1)], "aggregate": {},
                             "total_evaluations": 9}}})
    b = _y({"skills": {"x": {"evaluations": [_eval(2)], "aggregate": {},
                             "total_evaluations": 4}}})
    once = cm.merge_skill_quality(a, b)
    assert cm.merge_skill_quality(once, a) == once
    assert cm.merge_skill_quality(once, once) == once


# --- strategy-archive.yaml --------------------------------------------------

def test_strategy_archive_id_union_is_commutative():
    """Same-id DIVERGENCE is the only case that exercises the tiebreak.

    An earlier version of this test gave both sides a byte-identical sa-001, so
    the same-id branch never had to choose between differing records — and a
    mutation replacing the content tiebreak with keep-first-seen left it GREEN.
    It asserted commutativity while constructing inputs in which the deciding
    branch was unreachable (rb-5737 class: the test could not fail on the defect
    it names). The `note` fields below diverge so the tiebreak must actually run.
    """
    a = _y({"archive": [{"id": "sa-001", "name": "one", "note": "A-side"},
                        {"id": "sa-002", "name": "two"}]})
    b = _y({"archive": [{"id": "sa-001", "name": "one", "note": "B-side"},
                        {"id": "sa-003", "name": "three"}]})
    ab, ba = cm.merge_strategy_archive(a, b), cm.merge_strategy_archive(b, a)
    assert ab == ba, "same-id divergence must resolve by CONTENT, not arg order"
    rows = _load(ab)["archive"]
    assert [r["id"] for r in rows] == ["sa-001", "sa-002", "sa-003"]
    # canon-max is a deterministic content order: "B-side" > "A-side".
    assert rows[0]["note"] == "B-side"


def test_strategy_archive_keeps_idless_rows():
    """The union promise holds for malformed rows too — deduped, sorted last,
    never silently dropped."""
    a = _y({"archive": [{"id": "sa-001"}, {"no_id": True}]})
    b = _y({"archive": [{"id": "sa-002"}, {"no_id": True}]})
    ab, ba = cm.merge_strategy_archive(a, b), cm.merge_strategy_archive(b, a)
    assert ab == ba
    rows = _load(ab)["archive"]
    assert sum(1 for r in rows if r.get("no_id")) == 1
    assert len(rows) == 3


def test_strategy_archive_second_merge_converges():
    a = _y({"archive": [{"id": "sa-001", "name": "one"}]})
    b = _y({"archive": [{"id": "sa-002", "name": "two"}]})
    once = cm.merge_strategy_archive(a, b)
    assert cm.merge_strategy_archive(once, b) == once


# --- strategy-generations.yaml () ---------------------------------
#
# The other three handlers here union rows that ONE side added. This one merges
# a row BOTH sides mutated: meta-generations.py rewrites generations[-1] on
# every goal close, so the interesting divergence is same-key, not disjoint.

def _gen(n: int, goals: int, total: float, *, ended=None, best=0.0,
         worst=1.0, started=None) -> dict:
    avg = round(total / goals, 4) if goals else 0.0
    return {"generation": n, "started": started or f"2026-07-01T00:{n:02d}:00",
            "ended": ended, "goals_completed": goals,
            "parameter_snapshot": {"w.priority": 1.0},
            "metrics": {"avg_learning_value": avg,
                        "total_learning_value": total},
            "best_score": best, "worst_score": worst}


def _sg(gens, current=None, peak_gen=1, peak=0.0) -> bytes:
    return _y({"version": 1,
               "current_generation": current if current is not None
               else max((g["generation"] for g in gens), default=0),
               "generations": gens, "peak_generation": peak_gen,
               "peak_score": peak})


def test_sg_same_generation_field_merge_is_commutative():
    a = _sg([_gen(42, 100, 50.0, best=0.9, worst=0.2)])
    b = _sg([_gen(42, 158, 79.0, best=0.7, worst=0.1)])
    assert cm.merge_strategy_generations(a, b) == cm.merge_strategy_generations(b, a)


def test_sg_goals_completed_is_max_not_sum():
    """The load-bearing one. Both boxes increment the SAME counter from a common
    ancestor, so summing double-counts every already-shared goal — and the error
    compounds on each re-merge, which is how a counter ledger stops converging."""
    a = _sg([_gen(42, 100, 50.0)])
    b = _sg([_gen(42, 158, 79.0)])
    g = _load(cm.merge_strategy_generations(a, b))["generations"][0]
    assert g["goals_completed"] == 158        # not 258
    assert g["metrics"]["total_learning_value"] == 79.0


def test_sg_avg_learning_value_is_recomputed_not_carried():
    """guard-1153: derived from the MERGED total and count. Both sides carry a
    self-consistent average, so a carried value looks correct in a diff."""
    a = _sg([_gen(42, 100, 50.0)])            # avg 0.5
    b = _sg([_gen(42, 158, 79.0)])            # avg 0.5
    g = _load(cm.merge_strategy_generations(a, b))["generations"][0]
    assert g["metrics"]["avg_learning_value"] == round(79.0 / 158, 4)


def test_sg_best_and_worst_track_their_own_direction():
    a = _sg([_gen(42, 10, 5.0, best=0.9, worst=0.4)])
    b = _sg([_gen(42, 10, 5.0, best=0.6, worst=0.1)])
    g = _load(cm.merge_strategy_generations(a, b))["generations"][0]
    assert (g["best_score"], g["worst_score"]) == (0.9, 0.1)


def test_sg_a_close_dominates_an_open_generation():
    """`ended` is monotonic: one box closing it must not be undone by the other
    still holding it open. `started` runs the other way — the generation began
    when the FIRST box opened it, so the earlier stamp wins."""
    a = _sg([_gen(42, 10, 5.0, ended=None, started="2026-07-28T09:00:00")])
    b = _sg([_gen(42, 12, 6.0, ended="2026-07-29T10:00:00",
                  started="2026-07-28T11:00:00")])
    g = _load(cm.merge_strategy_generations(a, b))["generations"][0]
    assert str(g["ended"]).replace(" ", "T") == "2026-07-29T10:00:00"
    assert str(g["started"]).replace(" ", "T") == "2026-07-28T09:00:00"


def test_sg_disjoint_generations_both_survive_and_current_is_recomputed():
    a = _sg([_gen(41, 10, 5.0), _gen(42, 10, 5.0)], current=42)
    b = _sg([_gen(41, 10, 5.0), _gen(43, 3, 1.0)], current=43)
    m = _load(cm.merge_strategy_generations(a, b))
    assert [g["generation"] for g in m["generations"]] == [41, 42, 43]
    assert m["current_generation"] == 43


def test_sg_peak_is_a_high_water_mark_not_a_recompute():
    """peak_score is an ACCUMULATOR, not a pure function of current state — the
    writer only ever raises it. Recomputing over merged rows would silently drop
    a historical peak whose generation's running average has since fallen."""
    a = _sg([_gen(42, 100, 10.0)], peak_gen=7, peak=0.93)   # live avg 0.1
    b = _sg([_gen(42, 100, 10.0)], peak_gen=9, peak=0.41)
    m = _load(cm.merge_strategy_generations(a, b))
    assert (m["peak_score"], m["peak_generation"]) == (0.93, 7)
    # score and generation must travel together under an argument swap — picking
    # the max score from one side and the generation from the other is the
    # silent way this pair goes wrong.
    assert cm.merge_strategy_generations(a, b) == cm.merge_strategy_generations(b, a)


def test_sg_peak_tie_resolves_independent_of_arg_order():
    a = _sg([_gen(42, 5, 1.0)], peak_gen=9, peak=0.5)
    b = _sg([_gen(42, 5, 1.0)], peak_gen=7, peak=0.5)
    assert cm.merge_strategy_generations(a, b) == cm.merge_strategy_generations(b, a)
    assert _load(cm.merge_strategy_generations(a, b))["peak_generation"] == 7


def test_sg_second_merge_converges():
    a = _sg([_gen(41, 10, 5.0), _gen(42, 100, 50.0)])
    b = _sg([_gen(42, 158, 79.0), _gen(43, 3, 1.0)])
    once = cm.merge_strategy_generations(a, b)
    assert cm.merge_strategy_generations(once, b) == once
    assert cm.merge_strategy_generations(once, a) == once


def test_sg_idless_rows_survive_rather_than_being_dropped():
    a = _sg([_gen(42, 10, 5.0)])
    b = _y({"version": 1, "current_generation": 42,
            "generations": [_gen(42, 10, 5.0), {"note": "malformed"}],
            "peak_generation": 1, "peak_score": 0.0})
    m = _load(cm.merge_strategy_generations(a, b))
    assert {"note": "malformed"} in m["generations"]


def test_sg_dispatches_by_basename():
    assert cm.merge_handler_for(
        "meta/strategy-generations.yaml") is cm.merge_strategy_generations


# --- dispositions (the disqualified 7) --------------------------------------

@pytest.mark.parametrize("path", [
    # DERIVED CACHE: regenerated from _tree.yaml by load-tree-summary.sh. A
    # union would fabricate a summary matching NEITHER side's tree.
    "world/knowledge/tree/_summary.json",
    # BASENAME COLLISION: merge_handler_for dispatches on basename, so all four
    # collapse to one key "_index.yaml" despite NOT sharing a shape. Registering
    # any one silently applies it to the other three.
    "world/knowledge/patterns/_index.yaml",
    "world/knowledge/strategies/_index.yaml",
    "meta/meta-knowledge/_index.yaml",
    "meta/transfer/_index.yaml",
    # writerless empty stubs — freeze is a no-op on a file nothing writes
    # (the world/journal.jsonl precedent).
    "world/knowledge/beliefs.yaml",
    "world/knowledge/transitions.yaml",
])
def test_disqualified_stores_stay_unregistered(path):
    """Pins the  dispositions so a later well-meaning registration has
    to confront the reasoning rather than silently reintroduce the defect.

    If you are here because this test failed: registering `_index.yaml` by
    BASENAME cannot be correct for four differently-shaped stores — the fix is a
    path-pattern branch in merge_handler_for (the merge_team_state_shard
    precedent), and this test should then be narrowed, not deleted.
    """
    assert cm.merge_handler_for(path) is None


def test_registered_trio_dispatches():
    assert cm.merge_handler_for("meta/improvement-velocity.yaml") is cm.merge_improvement_velocity
    assert cm.merge_handler_for("meta/skill-quality.yaml") is cm.merge_skill_quality
    assert cm.merge_handler_for("meta/strategy-archive.yaml") is cm.merge_strategy_archive


# --- : the follow-on 18, and why 9 of them were never in scope -----

def test_g3992_registered_pair_dispatches():
    """The two synced append-only logs, each writer-read (rb-245)."""
    assert cm.merge_handler_for(
        "world/audit-reports/alert-sweep-seen.jsonl") is cm.merge_append_only_jsonl
    assert cm.merge_handler_for(
        "meta/missing-verification-criteria.jsonl") is cm.merge_append_only_jsonl


def test_g3992_registered_pair_merge_is_commutative():
    """guard-907 on the newly-registered pair: byte-identical in both arg orders."""
    a = b'{"k":"shared"}\n{"k":"only-a"}\n'
    b = b'{"k":"shared"}\n{"k":"only-b"}\n'
    ab = cm.merge_append_only_jsonl(a, b)
    ba = cm.merge_append_only_jsonl(b, a)
    assert ab == ba
    # the shared baseline line collapses to one; each side's own append survives
    assert ab.count(b'"shared"') == 1
    assert b'"only-a"' in ab and b'"only-b"' in ab


@pytest.mark.parametrize("path", [
    # `presence` is in owncloud_sync._EXCLUDE_DIRS
    "world/presence/bravo.jsonl",
    # _EXCLUDE_NAMES — per-box spool, drained into the SHARED gate-firings.jsonl
    "meta/gate-firings.spool.jsonl",
    # *-telemetry.jsonl — per-machine append logs
    "meta/history-save-telemetry.jsonl",
    "meta/write-queue-telemetry.jsonl",
    # world/*-log.jsonl — per-machine (log_script_decision)
    "world/handoff-yaml-build-log.jsonl",
    "world/pending-questions-sweep-log.jsonl",
    "world/precheck-eval-log.jsonl",
    "world/reflect-bookkeeping-log.jsonl",
    "world/state-update-audit-log.jsonl",
    # synced, but append-only is NOT provable: no code writer (appended by an
    # ad-hoc `echo >>` in skill pseudocode), an observed concurrent-append race,
    # and an unexplained line-count SHRINK — a removal path would make a
    # line-union resurrect deleted records (guard-1816).
    "meta/gate-eval-recommendations.jsonl",
])
def test_g3992_out_of_scope_stores_stay_unregistered(path):
    """Pins the  dispositions.

    If you are here because this test failed: the nine machine-local paths can
    never reach S3, so they can never both-diverge, so a handler for them is
    dead code — confirm against `test_g3992_machine_local_claim_holds` below
    before registering. Registering `gate-eval-recommendations.jsonl` needs the
    line-count shrink explained first, not a plausibility argument.
    """
    assert cm.merge_handler_for(path) is None


@pytest.mark.parametrize("rel,expect_local", [
    ("world/presence/bravo.jsonl", True),
    ("meta/gate-firings.spool.jsonl", True),
    ("meta/history-save-telemetry.jsonl", True),
    ("meta/write-queue-telemetry.jsonl", True),
    ("world/handoff-yaml-build-log.jsonl", True),
    ("world/pending-questions-sweep-log.jsonl", True),
    ("world/precheck-eval-log.jsonl", True),
    ("world/reflect-bookkeeping-log.jsonl", True),
    ("world/state-update-audit-log.jsonl", True),
    # controls — these DO sync, which is why their handler question was real
    ("world/audit-reports/alert-sweep-seen.jsonl", False),
    ("meta/missing-verification-criteria.jsonl", False),
    ("meta/skill-gaps.yaml", False),
])
def test_g3992_machine_local_claim_holds(tmp_path, rel, expect_local):
    """Pins the EVIDENCE behind the disqualifications above, not just the verdict.

    The disqualification is only sound while these paths stay machine-local, so
    a change to the sync exclusion policy must fail HERE — otherwise nine stores
    would silently become divergence-capable with no handler and no signal.

    Replicates owncloud_backend._machine_local exactly: the _EXCLUDE_DIRS
    directory prune PLUS _is_machine_local. Checking _is_machine_local alone is
    the trap — it deliberately does not test _EXCLUDE_DIRS (see the NOTE in its
    caller), and that omission reports `presence/` as syncing.
    """
    osync = pytest.importorskip("owncloud_sync")
    prefix, sub = rel.split("/", 1)
    root = tmp_path / prefix
    full = root / sub
    parts = Path(sub).parts[:-1]
    actual = (any(seg in osync._EXCLUDE_DIRS for seg in parts)
              or osync._is_machine_local(full.name, prefix,
                                         full_path=full, root_path=root))
    assert actual is expect_local


# ── backpressure.yaml () ───────────────────────────────────────────
#
# Different from its three siblings above: the trap here is not a DERIVED field,
# it is that the file's two halves need OPPOSITE merges. rollback_history is a
# pure append (safe to union); active_monitors is a DRAINING queue with two
# removal paths the writer owns -- a cap eviction (meta-backpressure.py
# cmd_monitor:94-96 `monitors.pop(0)`) and a status-filter rebuild (cmd_check:225
# `[m for m in monitors if m["status"]=="monitoring"]`). Unioning that half
# resurrects monitors the other box already retired.
#
# This is the file that actually stranded a box: routed by the .mind-data
# wildcard with no handler, it arrived with an unmerged index and ZERO conflict
# markers, wedging cc-06 for 6.2h / 58 commits on 2026-07-31 and then the
# Windows clone hours later.


def _bp(active=None, history=None, skips=None):
    doc = {"version": 1, "active_monitors": active or [], "rollback_history": history or []}
    if skips is not None:
        doc["audit_only_skips"] = skips
    return _y(doc)


def _mon(mid, status="monitoring", **kw):
    return dict(meta_change_id=mid, status=status, **kw)


def test_backpressure_rollback_history_unions_and_loses_nothing():
    """The append-only half. Neither side may lose a rollback record."""
    a = _bp(history=[{"meta_change_id": f"mc-{n}"} for n in (44, 45)])
    b = _bp(history=[{"meta_change_id": f"mc-{n}"} for n in (45, 46)])
    got = _load(cm.merge_backpressure(a, b))
    assert sorted(r["meta_change_id"] for r in got["rollback_history"]) == \
        ["mc-44", "mc-45", "mc-46"]


def test_backpressure_does_not_resurrect_a_rolled_back_monitor():
    """The REAL cc-06 divergence, replayed.

    Ours rolled mc-44/45 back (so they left active_monitors and appear in
    rollback_history); theirs has not seen that yet and still lists them active.
    A blind id-union puts them back into the active queue.
    """
    ours = _bp(active=[], history=[{"meta_change_id": "mc-44"}, {"meta_change_id": "mc-45"}])
    theirs = _bp(active=[_mon("mc-44"), _mon("mc-45")], history=[])
    got = _load(cm.merge_backpressure(ours, theirs))
    assert got["active_monitors"] == [], "resurrected a rolled-back monitor"
    assert len(got["rollback_history"]) == 2


def test_backpressure_does_not_resurrect_a_graduated_monitor():
    """Graduation leaves NO tombstone, so the terminal STATUS is the only signal.
    A terminal status is a decision one box made that the other has not seen."""
    ours = _bp(active=[_mon("mc-9", "graduated")])
    theirs = _bp(active=[_mon("mc-9", "monitoring", goals_since_change=3)])
    got = _load(cm.merge_backpressure(ours, theirs))
    assert got["active_monitors"] == [], "resurrected a graduated monitor"


def test_backpressure_keeps_genuinely_live_monitors_and_maxes_counters():
    """The negative control. A handler that simply emptied active_monitors would
    pass all three tests above; this one fails it."""
    ours = _bp(active=[_mon("mc-7", goals_since_change=9, imp_k_samples=[.1, .2, .3])])
    theirs = _bp(active=[_mon("mc-7", goals_since_change=4, imp_k_samples=[.1]),
                         _mon("mc-8")])
    got = _load(cm.merge_backpressure(ours, theirs))
    live = {m["meta_change_id"]: m for m in got["active_monitors"]}
    assert sorted(live) == ["mc-7", "mc-8"]
    assert live["mc-7"]["goals_since_change"] == 9          # MAX, not last-writer
    assert len(live["mc-7"]["imp_k_samples"]) == 3          # longer sample run kept


def test_backpressure_audit_only_skips_also_tombstone():
    """audit_only_skips is the SECOND append-only exit route (cmd_check:198)."""
    ours = _bp(active=[], skips=[{"meta_change_id": "mc-3"}])
    theirs = _bp(active=[_mon("mc-3")], skips=[])
    got = _load(cm.merge_backpressure(ours, theirs))
    assert got["active_monitors"] == []
    assert len(got["audit_only_skips"]) == 1


@pytest.mark.parametrize("case", ["rollback", "graduate", "live", "mixed"])
def test_backpressure_is_commutative(case):
    """guard-907. Byte-identical both ways or the two boxes ping-pong forever."""
    pairs = {
        "rollback":  (_bp(active=[], history=[{"meta_change_id": "mc-1"}]),
                      _bp(active=[_mon("mc-1")], history=[])),
        "graduate":  (_bp(active=[_mon("mc-2", "graduated")]),
                      _bp(active=[_mon("mc-2", goals_since_change=5)])),
        "live":      (_bp(active=[_mon("mc-3", goals_since_change=9)]),
                      _bp(active=[_mon("mc-3", goals_since_change=2), _mon("mc-4")])),
        "mixed":     (_bp(active=[_mon("mc-5")], history=[{"meta_change_id": "mc-6"}]),
                      _bp(active=[_mon("mc-6"), _mon("mc-5", goals_since_change=1)],
                          history=[{"meta_change_id": "mc-7"}])),
    }
    a, b = pairs[case]
    assert cm.merge_backpressure(a, b) == cm.merge_backpressure(b, a)


def test_backpressure_is_registered_for_the_wildcard_path():
    """The wedge was a ROUTING gap, not a logic gap: .gitattributes routed the
    path to the driver and nothing was behind it. Pin the resolution."""
    assert cm.merge_handler_for(".mind-data/meta/backpressure.yaml") is cm.merge_backpressure
    assert cm.merge_handler_for("meta/backpressure.yaml") is cm.merge_backpressure


def test_backpressure_unparseable_side_falls_back_not_raises():
    """A handler that raises re-creates the wedge it was written to remove."""
    got = cm.merge_backpressure(b"{{{ not yaml", _bp(history=[{"meta_change_id": "mc-1"}]))
    assert isinstance(got, bytes) and got
