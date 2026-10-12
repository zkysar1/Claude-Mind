"""Tests for crosslink-sweep.py (): the citation-graph census, the seeded
review frame, and the guard-2019 exclusion of the sweep's own output.

The census is exact (a mention graph over two stores), so these assert counts, not
tolerances. The exclusion tests carry their own positive control: with the
exclusion switched off the answer must change, otherwise the test would pass on an
exclusion that never mattered.
"""
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "crosslink_sweep",
    Path(__file__).resolve().parents[1] / "crosslink-sweep.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def rb(i, goal, text="", **kw):
    return {"id": f"rb-{i}", "status": "active", "source_goal": goal,
            "title": f"t{i}", "content": text, **kw}


def gd(i, goal, text="", **kw):
    return {"id": f"guard-{i}", "status": "active", "source": goal,
            "rule": f"r{i}", "action_hint": text, **kw}


def _world(tmp_path, rbs, guards, ledger=None):
    world = tmp_path / "world"
    (world / "telemetry").mkdir(parents=True)
    for name, recs in (("reasoning-bank.jsonl", rbs), ("guardrails.jsonl", guards)):
        with open(world / name, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
    if ledger is not None:
        with open(world / "telemetry" / "adjudication-lane-ledger.jsonl", "w", encoding="utf-8") as f:
            for r in ledger:
                f.write(json.dumps(r) + "\n")
    return world


def _census(world, **kw):
    return mod.run(world, ledger=world / mod.LEDGER_REL, **kw)["census"]


def test_census_counts_inbound_and_outbound(tmp_path):
    # rb-1 cites guard-1; rb-2 cites nothing and is cited by nobody; guard-1 cites nothing.
    world = _world(tmp_path, [rb(1, "g-1", "see guard-1"), rb(2, "g-2")], [gd(1, "g-3")])
    c = _census(world)["all"]
    assert c["active"] == 3
    assert c["no_mention_anywhere"] == 2        # rb-1 and rb-2; guard-1 is cited
    assert c["no_cross_goal_mention"] == 2
    assert c["mentions_nothing"] == 2           # rb-2 and guard-1


def test_same_goal_mention_is_not_a_cross_goal_link(tmp_path):
    # rb-10588: an entry citing only its authorship cluster looks well-linked and is not.
    # rb-2 is cited only by its same-goal sibling rb-1; rb-3 (another goal) cites rb-4.
    world = _world(tmp_path, [rb(1, "g-1", "sibling of rb-2"), rb(2, "g-1"), rb(3, "g-2", "cites rb-4"), rb(4, "g-3")], [])
    c = _census(world)["all"]
    assert c["no_mention_anywhere"] == 2        # rb-1 and rb-3; rb-2 and rb-4 are each cited once
    assert c["no_cross_goal_mention"] == 3      # rb-1, rb-2 (sibling-only) and rb-3; only rb-4 has a cross-goal citer


def test_mention_in_tags_counts(tmp_path):
    world = _world(tmp_path, [rb(1, "g-1", tags=["guard-2-sibling"])], [gd(2, "g-2")])
    c = _census(world)["all"]
    assert c["no_mention_anywhere"] == 1        # rb-1 is uncited; guard-2 is cited through the tag


def test_inactive_entries_leave_the_population_and_the_graph(tmp_path):
    world = _world(tmp_path, [rb(1, "g-1"), rb(2, "g-2", "cites rb-1", status="retired")], [])
    c = _census(world)["all"]
    assert c["active"] == 1
    assert c["no_mention_anywhere"] == 1        # the retired citer does not count


def test_sweep_output_is_excluded_from_population_and_graph(tmp_path):
    # guard-2019: a record that enumerates a sweep slate must not make its ids look cited.
    slate = rb(9, "g-9", "slate: rb-1 rb-2", tags=[mod.OUTPUT_TAG])
    world = _world(tmp_path, [rb(1, "g-1"), rb(2, "g-2"), slate], [])
    excluded = mod.run(world)
    assert excluded["corpus"]["active_total"] == 2
    assert excluded["corpus"]["reasoning_bank"]["sweep_output_excluded"] == 1
    assert excluded["census"]["all"]["no_mention_anywhere"] == 2
    # positive control: without the exclusion the slate inflates the cited count.
    included = mod.run(world, include_tagged=True)
    assert included["corpus"]["active_total"] == 3
    assert included["census"]["all"]["no_mention_anywhere"] == 1   # only the slate itself is uncited


def test_second_run_is_identical(tmp_path):
    world = _world(tmp_path, [rb(i, f"g-{i}", f"cites rb-{i + 1}") for i in range(1, 7)],
                   [gd(i, f"g-{20 + i}") for i in range(1, 5)])
    first = mod.run(world, sample=4, seed=7)
    second = mod.run(world, sample=4, seed=7)
    for run in (first, second):
        run.pop("run_at")
    assert first == second


def test_sample_is_stratified_seeded_and_skips_pilot_reviewed(tmp_path):
    rbs = [rb(i, f"g-{i}") for i in range(1, 9)]
    guards = [gd(i, f"g-{50 + i}") for i in range(1, 9)]
    ledger = [{"kind": "review", "entry_id": "rb-1"}, {"kind": "review", "entry_id": "guard-1"}]
    world = _world(tmp_path, rbs, guards, ledger)
    out = mod.run(world, sample=5, seed=3, ledger=world / mod.LEDGER_REL)
    s = out["sample"]
    assert [r["store"] for r in s].count("reasoning_bank") == 3     # the odd one goes to reasoning_bank
    assert [r["store"] for r in s].count("guardrails") == 2
    assert not {r["id"] for r in s} & {"rb-1", "guard-1"}
    assert out["params"]["pilot_reviewed_excluded_from_sample"] == 2
    assert s == mod.run(world, sample=5, seed=3, ledger=world / mod.LEDGER_REL)["sample"]
    assert all({"inbound_any", "inbound_cross_goal", "outbound"} <= set(r) for r in s)


def test_missing_store_exits_2_not_clean(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mod.main(["--world", str(empty)]) == 2
    assert capsys.readouterr().out == ""
