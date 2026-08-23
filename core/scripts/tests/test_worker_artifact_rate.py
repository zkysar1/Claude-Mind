"""worker-artifact-rate.py — the  learning-artifact rate check.

Pins the four behaviours that decide whether this check can be trusted:

  1. INSUFFICIENT DATA below --min-sample. The stamp it keys on is
     going-forward, so N=0 is the expected state at ship time. A rate over N=0
     is not 0% and not 100% — it is unmeasured, and printing a verdict for it
     would be the vacuous-zero failure the check exists to detect (guard-4093).
  2. PASS / FAIL either side of the threshold, with the boundary pinned on BOTH
     sides (guard-4374: when code sorts into two buckets, pin both).
  3. THE POSITIVE CONTROL. Every number this script emits is an INTERSECTION of
     two id sets, and an intersection's failure mode is a clean zero that reads
     as a measurement. The script must refuse (rc=2) when goals were scanned and
     artifacts were indexed but nothing matched. This is not hypothetical: the
     script's own first run reported 0 of 636 because it read `goal_id` (the
     aspirations-query PROJECTION key) against the raw store, which keys goals
     on `id` (guard-4024). The control is what turns that silent wrong answer
     into a loud refusal.
  4. BOTH id shapes are accepted, since both are real in this tree.

Hermetic: every case builds its own tiny world in a tmp dir and calls measure()
/ main() directly. Never reads the live store.

Run:
  py -3 -m pytest core/scripts/tests/test_worker_artifact_rate.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = CORE_ROOT / "scripts" / "worker-artifact-rate.py"

# Hyphenated filename -> not importable by name; load it by path.
_spec = importlib.util.spec_from_file_location("worker_artifact_rate", SCRIPT)
war = importlib.util.module_from_spec(_spec)
sys.modules["worker_artifact_rate"] = war
_spec.loader.exec_module(war)


def _world(tmp_path, goals, rb=(), guards=()):
    """Build a minimal world dir. `goals` are (id_key, id, role, outcome)."""
    w = tmp_path / "world"
    # parents=True: callers nest under tmp_path (e.g. tmp_path/"id"/"world") to
    # get one world per sub-case, and those intermediate dirs do not exist yet.
    w.mkdir(parents=True)
    (w / "aspirations.jsonl").write_text(
        json.dumps({
            "id": "asp-1",
            "goals": [
                {k: gid, "status": "completed",
                 "completed_by_role": role, "outcome_class": outcome}
                for (k, gid, role, outcome) in goals
            ],
        }) + "\n",
        encoding="utf-8",
    )
    (w / "reasoning-bank.jsonl").write_text(
        "".join(json.dumps({"origin_goal_id": g}) + "\n" for g in rb),
        encoding="utf-8",
    )
    (w / "guardrails.jsonl").write_text(
        "".join(json.dumps({"source": g}) + "\n" for g in guards),
        encoding="utf-8",
    )
    return w


def test_worker_population_counts_only_stamped_non_routine_goals(tmp_path):
    w = _world(
        tmp_path,
        goals=[
            ("id", "g-1-01", "worker", "deep"),      # counted
            ("id", "g-1-02", "worker", "routine"),   # excluded: routine
            ("id", "g-1-03", "", "deep"),            # excluded: unstamped
        ],
        rb=["g-1-01"],
    )
    m = war.measure(w)
    assert m["worker_population"] == 1, m
    assert m["worker_with_artifact"] == 1, m
    assert m["unstamped_population"] == 1, m


def test_both_id_key_shapes_are_accepted(tmp_path):
    """The raw store uses `id`; the query projection uses `goal_id`. Reading
    only the projection's name against the raw store is guard-4024 and yields a
    silent zero — so both must resolve.
    """
    for key in ("id", "goal_id"):
        w = _world(tmp_path / key, goals=[(key, "g-2-01", "worker", "deep")],
                   guards=["g-2-01"])
        m = war.measure(w)
        assert m["worker_population"] == 1, f"{key}: {m}"
        assert m["worker_with_artifact"] == 1, (
            f"{key}: goal id did not resolve — the join key is wrong ({m})"
        )


def test_insufficient_data_below_min_sample(tmp_path, capsys, monkeypatch):
    w = _world(tmp_path, goals=[("id", "g-3-01", "worker", "deep")],
               rb=["g-3-01"])
    rc = _run(war, w, monkeypatch, ["--min-sample", "10"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "INSUFFICIENT DATA" in out, out
    # The unfiltered population must appear beside the filtered one, so a small
    # number can never be read without its denominator (guard-2298).
    assert "completed_goals=" in out, out


def test_pass_and_fail_straddle_the_threshold(tmp_path, capsys, monkeypatch):
    # 3 of 4 worker goals have an artifact -> 75%.
    goals = [("id", f"g-4-0{i}", "worker", "deep") for i in range(1, 5)]
    w = _world(tmp_path, goals=goals, rb=["g-4-01", "g-4-02"],
               guards=["g-4-03"])

    rc = _run(war, w, monkeypatch, ["--min-sample", "4", "--threshold", "0.60"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("PASS"), out

    rc = _run(war, w, monkeypatch, ["--min-sample", "4", "--threshold", "0.90"])
    out = capsys.readouterr().out
    assert rc == 1 and out.startswith("FAIL"), out


def test_broken_join_is_refused_not_reported_as_zero(tmp_path, capsys,
                                                     monkeypatch):
    """The control. Goals scanned, artifacts indexed, nothing intersects ->
    rc=2 and a stderr explanation, NEVER a confident 0%.
    """
    w = _world(tmp_path,
               goals=[("id", f"g-5-0{i}", "", "deep") for i in range(1, 4)],
               rb=["g-99-01", "g-99-02"])      # index non-empty, disjoint
    rc = _run(war, w, monkeypatch, ["--min-sample", "1"])
    cap = capsys.readouterr()
    assert rc == 2, f"expected refusal, got rc={rc} out={cap.out!r}"
    assert "ZERO matches" in cap.err, cap.err
    assert "0.0%" not in cap.out, (
        f"a rate was reported from a broken join: {cap.out!r}"
    )


def _run(mod, world: Path, monkeypatch, argv: list) -> int:
    """Invoke main() with WORLD_DIR pointed at a tmp world.

    main() imports WORLD_DIR from _paths INSIDE the function, so patching the
    already-imported _paths module attribute is what reaches it.
    """
    sys.path.insert(0, str(CORE_ROOT / "scripts"))
    import _paths

    monkeypatch.setattr(_paths, "WORLD_DIR", world, raising=False)
    monkeypatch.setattr(sys, "argv", ["worker-artifact-rate.py"] + argv)
    return mod.main()
