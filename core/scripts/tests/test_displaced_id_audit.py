"""displaced-id-audit: nested goals, the missing surfaces, and per-file sites.

g-115-11630. The audit's collect() read `displaced_from` on TOP-LEVEL JSONL
records only, so in aspirations.jsonl -- where the top-level records are
aspirations and the goals sit nested in `goals[]` -- a goal's displacement was
invisible, in the world queue and in every agent queue. And surfaces() missed
world/scripts and agents/*/experience, where stale citations of a displaced id
also sit (ZDS counted 10 and 20 there).

g-115-11662 (merged in, same file). surfaces() cited each site by BASENAME and
set()-deduped, so two same-named files (80 SKILL.md files measured on ZDS)
collapsed into ONE where entry and the where-list under-reported the citations
total.

THE LOAD-BEARING INVARIANT (verification outcome 3): on a run, the sum of the
parsed per-entry counts in `where` must equal `citations` for the id. It holds
only while a where entry is distinct per FILE -- a basename dedup breaks it.
The tests below pin it hermetically (a live run is the same assertion at fleet
scale, re-verified at close).

g-115-8934. The audit now emits the CLONE vs TRUE-DISPLACEMENT split itself:
each --json event carries a machine-readable clone_check + reason derived
from displaced_from + created (never text similarity), and the text summary
splits UNRELATED / NEAR-TWIN by that determination. The both-conditions
rule is pinned below, including the goal's counterexample (displaced_from
absent but created stamps differ => true_displacement).
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))  # core/scripts


def _load():
    spec = importlib.util.spec_from_file_location(
        "dia_under_test", str(SCRIPT_DIR / "displaced-id-audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _pairs(pairs):
    return {(p["old"], p["new"]) for p in pairs}


def _where_counts(where):
    """Parse the (n) suffix of each where entry, the same way a reader would."""
    return [int(re.match(r".*\((\d+)\)$", e).group(1)) for e in where]


# ----  item 1: nested goals[] + the agent queues -----------------

def test_nested_goal_displacement_is_reported(tmp_path):
    """A goal nested in goals[] inside a world aspiration is an event now.

    Also pins the REGRESSION half: a top-level displaced record in the same
    store is still reported (the fix must not narrow the old scan).
    """
    mod = _load()
    world, meta, agents = tmp_path / "world", tmp_path / "meta", tmp_path / "agents"
    world.mkdir()
    meta.mkdir()
    _write(world / "aspirations.jsonl",
           # top-level displaced record (pre-existing behaviour)
           json.dumps({"id": "rb-9", "title": "top-level moved record",
                       "displaced_from": "rb-9-old"}) + "\n"
           # the aspiration nests its goals
           + json.dumps({"id": "asp-1", "title": "an aspiration",
                         "goals": [
                             {"id": "g-1-01", "title": "a plain nested goal"},
                             {"id": "g-1-02", "title": "the moved nested goal",
                              "displaced_from": "g-1-02-old"}]}) + "\n")
    pairs, occ, _meta, stats = mod.collect(world, meta, agents)
    got = _pairs(pairs)
    assert ("rb-9-old", "rb-9") in got            # top-level: unchanged
    assert ("g-1-02-old", "g-1-02") in got        # nested: the defect fix
    assert "g-1-02" in occ                        # the nested goal's id is
    assert "g-1-01" in occ                        # part of the occupancy map
    # 2 top-level rows + 2 nested goals = 4 records walked
    assert stats["records"] == 4


def test_agent_queue_nested_goal_is_reported(tmp_path):
    """Every agent queue is scanned; a nested goal there is an event too."""
    mod = _load()
    world, meta, agents = tmp_path / "world", tmp_path / "meta", tmp_path / "agents"
    world.mkdir()
    meta.mkdir()
    _write(agents / "alpha" / "aspirations.jsonl",
           json.dumps({"id": "asp-9", "title": "agent aspiration",
                       "goals": [
                           {"id": "g-9-02", "title": "moved in the agent queue",
                            "displaced_from": "g-9-02-old"}]}) + "\n")
    # a non-queue agent jsonl must NOT be pulled in (only aspirations.jsonl
    # under the agents root can hold a reid-merged record; )
    _write(agents / "alpha" / "experience.jsonl",
           json.dumps({"id": "g-9-99", "displaced_from": "g-9-99-old"}) + "\n")
    pairs, _, _meta, _ = mod.collect(world, meta, agents)
    got = _pairs(pairs)
    assert ("g-9-02-old", "g-9-02") in got
    assert ("g-9-99-old", "g-9-99") not in got    # non-queue agent file excluded


# ----  item 2: the missing surfaces ------------------------------

def test_surfaces_cover_world_scripts_and_agent_experience(tmp_path):
    mod = _load()
    world, repo, agents = tmp_path / "world", tmp_path / "repo", tmp_path / "agents"
    world.mkdir()
    repo.mkdir()
    _write(world / "scripts" / "probe.py", "pass  # world/scripts probe\n")
    _write(agents / "alpha" / "experience" / "exp-g-1-01-20261008.md",
           "an experience trace\n")
    by_label = {}
    for label, p, _rel in mod.surfaces(world, repo, agents):
        by_label.setdefault(label, []).append(p)
    assert [p.name for p in by_label["world-scripts"]] == ["probe.py"]
    assert [p.name for p in by_label["experience:alpha"]
            ] == ["exp-g-1-01-20261008.md"]


# ---- : per-file sites + the sum invariant ------------------------

def _full_run(tmp_path, monkeypatch, capsys):
    """Run main() --json over a hermetic world; return the parsed json doc.

    One displacement: g-7 -> g-8. The old id g-7 is DANGLING (no record at the
    old id), the class whose stale citations the audit reports. The moved
    record's own displaced_from field is itself a citation of g-7 sitting in the
    guardrails store, so the citation total includes that self-site.
    """
    mod = _load()
    world, meta, repo, agents = (tmp_path / n for n in
                                 ("world", "meta", "repo", "agents"))
    world.mkdir()
    meta.mkdir()
    repo.mkdir()
    _write(world / "guardrails.jsonl",
           json.dumps({"id": "g-8", "rule": "the moved rule text here",
                       "displaced_from": "g-7"}) + "\n")
    # two SAME-NAMED files in different directories cite the old id the SAME
    # number of times (2 + 2): the regression case. With a basename-keyed
    # where entry, both files produce the identical string "tree:node.md(2)"
    # and the set() dedup collapses them into ONE entry.
    _write(world / "knowledge" / "aa" / "node.md",
           "cite g-7 once and g-7 again; guard-321 is the control\n")
    _write(world / "knowledge" / "bb" / "node.md",
           "cite g-7 and g-7 here\n")
    # a world/scripts site and an experience site, to exercise the new surfaces
    _write(world / "scripts" / "probe.py", "ref g-7 in code\n")
    _write(agents / "alpha" / "experience" / "exp.md", "g-7 in the trace\n")
    monkeypatch.setattr(mod, "_roots", lambda: (world, meta, repo, agents))
    monkeypatch.setattr(sys, "argv", ["displaced-id-audit.py", "--json"])
    mod.main()
    return json.loads(capsys.readouterr().out)


def test_same_named_files_produce_two_where_entries(tmp_path, monkeypatch,
                                                    capsys):
    """Outcome 2: two same-named files citing the old id produce two where
    entries, not one collapsed basename entry."""
    doc = _full_run(tmp_path, monkeypatch, capsys)
    ev = [e for e in doc["events"] if e["old"] == "g-7"]
    assert len(ev) == 1
    where = ev[0]["where"]
    # two DISTINCT entries for the two aa/bb node.md files (the pre-fix code
    # emitted a single "tree:node.md(N)" for both)
    assert "tree:aa/node.md(2)" in where
    assert "tree:bb/node.md(2)" in where
    assert len([w for w in where if "node.md(" in w]) == 2


def test_where_entry_counts_sum_to_citations(tmp_path, monkeypatch, capsys):
    """Outcome 3 (hermetic twin of the live-run check): for every event, the
    sum of the parsed per-entry counts equals the citations total."""
    doc = _full_run(tmp_path, monkeypatch, capsys)
    assert doc["control_hits"] >= 1   # the positive control fired; the regex is
    for ev in doc["events"]:          # not broken (guard-2298: an unverified
        assert ev["citations"] > 0    # zero would make every row meaningless)
        assert sum(_where_counts(ev["where"])) == ev["citations"], (
            f"where entries no longer sum to citations for {ev['old']}: "
            f"{ev['where']} != {ev['citations']}")
    # the two NEW surfaces actually landed citations on the g-7 event
    ev = [e for e in doc["events"] if e["old"] == "g-7"][0]
    assert "world-scripts:probe.py(1)" in ev["where"]
    assert "experience:alpha:exp.md(1)" in ev["where"]
    # 2 (aa) + 2 (bb) + 1 (scripts) + 1 (experience) + 1 (store self-site)
    assert ev["citations"] == 7


# ---- : the clone vs true-displacement determination --------------

def _determine_case(mod, old_df, old_created, new_created, old_id="g-1"):
    """One event plus its old-id occupant, straight through _determine
    (g-115-8934)."""
    r = {"old": old_id, "new": "g-2", "new_created": new_created}
    occ_meta = {old_id: {"df": old_df, "created": old_created}}
    return mod._determine(r, occ_meta)


def test_both_conditions_met_is_clone_candidate():
    """A (record at old id never moved) + B (created stamps equal) =>
    clone_candidate. The marker is CANDIDATE-ONLY: the row is never
    suppressed on its strength (measured 0/6 precision on live UNRELATED)."""
    mod = _load()
    check, reason = _determine_case(mod, None, "2026-01-01T00:00:00",
                                    "2026-01-01T00:00:00")
    assert check == "clone_candidate"
    assert "CANDIDATE ONLY" in reason


def test_none_df_but_differing_stamps_is_true_displacement():
    """THE GOAL'S COUNTEREXAMPLE (outcome 3): displaced_from absent so
    condition A holds, but the created stamps differ so condition B fails.
    Both conditions are REQUIRED -- this is a true displacement, not a
    clone (the guard-343 -> guard-388 shape)."""
    mod = _load()
    check, _ = _determine_case(mod, None, "2026-01-01T00:00:00",
                               "2026-01-05T09:00:00")
    assert check == "true_displacement"


def test_old_occupant_with_displaced_from_is_true_displacement():
    """Condition A fails outright: the record at the old id is itself a
    successor (carries displaced_from), regardless of the stamps."""
    mod = _load()
    check, _ = _determine_case(mod, "g-0", "2026-01-01T00:00:00",
                               "2026-01-01T00:00:00")
    assert check == "true_displacement"


def test_unreadable_stamp_cannot_satisfy_both_conditions():
    """Either stamp missing => the both-conditions rule cannot be met;
    the event reports true_displacement, never clone_candidate."""
    mod = _load()
    assert _determine_case(mod, None, "2026-01-01T00:00:00", None)[0] \
        == "true_displacement"
    assert _determine_case(mod, None, None, "2026-01-01T00:00:00")[0] \
        == "true_displacement"


def test_dangling_old_id_is_undetermined():
    """No text-bearing record at the old id => condition A is unreadable;
    the determination is undetermined, not guessed."""
    mod = _load()
    check, reason = mod._determine(
        {"old": "g-9", "new": "g-10", "new_created": "2026-01-01"}, {})
    assert check == "undetermined"
    assert "unreadable" in reason


def _determination_world(tmp_path, monkeypatch):
    """Hermetic world with two UNRELATED displacements, one per branch of the
    rule (g-115-8934). g-2 moved off g-1 (occupant never moved, SAME created
    stamp => clone_candidate). g-4 moved off g-3 (occupant never moved,
    DIFFERENT created stamp => true_displacement). Token-disjoint snippets
    keep both events in the UNRELATED class so the text summary split is
    exercised."""
    mod = _load()
    world, meta, repo, agents = (tmp_path / n for n in
                                 ("world", "meta", "repo", "agents"))
    world.mkdir()
    meta.mkdir()
    repo.mkdir()
    _write(world / "guardrails.jsonl",
           json.dumps({"id": "g-2", "rule": "zebra migration note",
                       "displaced_from": "g-1",
                       "created": "2026-01-01T00:00:00"}) + "\n"
           + json.dumps({"id": "g-1", "rule": "quiet harbor rule",
                         "created": "2026-01-01T00:00:00"}) + "\n"
           + json.dumps({"id": "g-4", "rule": "vortex ledger entry",
                         "displaced_from": "g-3",
                         "created": "2026-01-02T00:00:00"}) + "\n"
           + json.dumps({"id": "g-3", "rule": "amber lantern code",
                         "created": "2026-01-09T00:00:00"}) + "\n")
    # the positive control must fire or every zero below is meaningless
    _write(world / "knowledge" / "node.md",
           "guard-321 is the control\n")
    monkeypatch.setattr(mod, "_roots", lambda: (world, meta, repo, agents))
    return mod


def test_json_events_carry_machine_readable_determination(tmp_path, monkeypatch,
                                                          capsys):
    """Outcome 1: every --json event carries clone_check +
    clone_check_reason + new_created, derived from displaced_from + created."""
    mod = _determination_world(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["displaced-id-audit.py", "--json"])
    mod.main()
    doc = json.loads(capsys.readouterr().out)
    assert doc["control_hits"] >= 1
    ev = {e["old"]: e for e in doc["events"]}
    assert ev["g-1"]["clone_check"] == "clone_candidate"
    assert ev["g-3"]["clone_check"] == "true_displacement"
    for e in ev.values():
        assert isinstance(e["clone_check_reason"], str)
        assert e["clone_check_reason"]
        assert e["new_created"]
    # neither branch may be suppressed: both rows report their citations
    assert ev["g-1"]["cls"] == "UNRELATED"
    assert ev["g-3"]["cls"] == "UNRELATED"


def test_text_summary_splits_by_determination(tmp_path, monkeypatch, capsys):
    """Outcome 2: the text summary splits the work-list classes by the
    determination (and flags the candidates as candidates, not exemptions)."""
    mod = _determination_world(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["displaced-id-audit.py"])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    line = next(l for l in out.splitlines()
                if "by determination" in l and "UNRELATED" in l)
    assert "1 clone_candidate" in line
    assert "1 true_displacement" in line
    assert "clone CANDIDATES" in out    # the precision caveat rendered


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
