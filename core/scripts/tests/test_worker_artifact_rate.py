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
    """Build a minimal world dir.

    `goals` are (id_key, id, role, outcome) or (id_key, id, role, outcome, day),
    where `day` is a YYYY-MM-DD close date. DAY IS OPTIONAL AND DEFAULTS TO
    ABSENT, deliberately: an absent day makes the window UNMEASURABLE, which is
    the state the window guard must withhold a verdict on. Defaulting to a wide
    span instead would satisfy the guard in every case that forgot to think
    about it, and the guard would then be green-by-default (guard-2903).
    """
    w = tmp_path / "world"
    # parents=True: callers nest under tmp_path (e.g. tmp_path/"id"/"world") to
    # get one world per sub-case, and those intermediate dirs do not exist yet.
    w.mkdir(parents=True)
    (w / "aspirations.jsonl").write_text(
        json.dumps({
            "id": "asp-1",
            "goals": [
                dict({k: gid, "status": "completed",
                      "completed_by_role": role, "outcome_class": outcome},
                     **({"completed_at": g[4] + "T00:00:00"} if len(g) > 4 else {}))
                for g in goals
                for (k, gid, role, outcome) in [g[:4]]
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
    """RETARGETED, not opted out ( window guard, guard-4618).

    These goals now carry close dates spanning 2026-09-01..2026-09-21 (20 days,
    past the 14-day default), so this case ALSO proves the window guard does not
    block a legitimately-wide population. Passing --min-window-days 0 would have
    been the smaller edit and a strictly weaker test: it would prove the guard
    can be switched off, not that it lets real data through.
    """
    # 3 of 4 worker goals have an artifact -> 75%.
    days = ["2026-09-01", "2026-09-07", "2026-09-14", "2026-09-21"]
    goals = [("id", f"g-4-0{i}", "worker", "deep", days[i - 1])
             for i in range(1, 5)]
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


# ── The WINDOW guard ( unit artifact-rate-attribution-audit) ────────
#
# --min-sample bounds the population SIZE. It says nothing about its SPAN, and
# the soak gate this check serves is explicitly a two-week window. MEASURED
# 2026-09-05 on the live store: the stamped population went 0 -> 228 in FOUR
# DAYS, so --min-sample 10 was satisfied within hours of the first worker close
# and the check flipped straight to `FAIL: 10/228 (4.4%)` -- a confident verdict
# on a population four days old.


def test_window_guard_withholds_a_verdict_on_a_short_span(tmp_path, capsys,
                                                          monkeypatch):
    """A large population spanning too few days is INSUFFICIENT DATA, not FAIL."""
    days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    goals = [("id", f"g-6-0{i}", "worker", "deep", days[i - 1])
             for i in range(1, 5)]
    # 0 of 4 carry an artifact -> the rate is 0%, which WOULD read FAIL.
    w = _world(tmp_path, goals=goals, rb=["g-99-01"], guards=["g-6-01"])
    rc = _run(war, w, monkeypatch, ["--min-sample", "4", "--min-window-days", "14"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "INSUFFICIENT DATA (window)" in out, out
    assert not out.startswith("FAIL"), out
    # The rate is still printed, explicitly labelled as trend-only -- withholding
    # a verdict must not also withhold the number a reader is watching.
    assert "reported for trend only" in out, out
    assert "3 day(s)" in out, out


def test_CONTROL_the_same_short_span_DOES_fail_with_the_guard_disabled(
        tmp_path, capsys, monkeypatch):
    """POSITIVE CONTROL (guard-2903): prove the guard is what withheld it.

    Identical world, identical population, only --min-window-days changes. If
    this case did not FAIL, the test above would be green for some unrelated
    reason and would keep passing after the guard was removed.
    """
    days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    goals = [("id", f"g-6-0{i}", "worker", "deep", days[i - 1])
             for i in range(1, 5)]
    w = _world(tmp_path, goals=goals, rb=["g-99-01"], guards=["g-6-01"])
    rc = _run(war, w, monkeypatch, ["--min-sample", "4", "--min-window-days", "0"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("FAIL"), out


def test_an_unmeasurable_window_withholds_rather_than_passes(tmp_path, capsys,
                                                             monkeypatch):
    """No close dates at all -> span is None -> withhold.

    The fail-safe DIRECTION is the point. An unreadable window could resolve
    either way, and withholding says "not measured yet", which is true; passing
    would assert the bridge is healthy on evidence that cannot support it.
    """
    goals = [("id", f"g-7-0{i}", "worker", "deep") for i in range(1, 5)]
    w = _world(tmp_path, goals=goals, rb=["g-7-01"], guards=["g-7-02"])
    rc = _run(war, w, monkeypatch, ["--min-sample", "4", "--min-window-days", "14"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "INSUFFICIENT DATA (window)" in out, out
    assert "a single day" in out, out


def test_population_shortfall_outranks_window_shortfall_in_the_json_reason(
        tmp_path, capsys, monkeypatch):
    """Both guards can trip at once; the reason must name the binding one.

    A caller that reads `insufficient_reason` to decide what to WAIT for gets
    the wrong answer if a 1-goal population reports "window" -- more elapsed
    time will never fix a population of one.
    """
    w = _world(tmp_path, goals=[("id", "g-8-01", "worker", "deep", "2026-09-01")],
               rb=["g-8-01"])
    rc = _run(war, w, monkeypatch,
              ["--min-sample", "10", "--min-window-days", "14", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "INSUFFICIENT_DATA"
    assert payload["insufficient_reason"] == "population", payload


def test_window_fields_are_measured_not_assumed(tmp_path):
    """measure() reports the real span and both endpoints."""
    goals = [("id", "g-9-01", "worker", "deep", "2026-08-20"),
             ("id", "g-9-02", "worker", "deep", "2026-09-04"),
             ("id", "g-9-03", "", "deep", "2026-01-01")]   # unstamped: excluded
    m = war.measure(_world(tmp_path, goals=goals, rb=["g-9-01"]))
    assert m["worker_window_days"] == 15, m
    assert m["worker_window_first"] == "2026-08-20", m
    assert m["worker_window_last"] == "2026-09-04", m


# ── LIVE + ARCHIVE source ( unit unit-6-source-history, guard-676) ─
#
# The live file "stays small (only active aspirations)" (aspirations.md
# §Archival Rules): a completed goal leaves it when its parent is archived, so
# a live-only reader measures a ROLLING window and the 14-day soak gate can
# never be computed from it (zeta 2026-09-16: N 228 -> 30, span stuck at 2-4
# days). The fix reads live + archive; these cases pin that reading and its
# dedup, each paired with a control proving the archive file is what changed
# the answer (guard-2903).


def _add_archive(w: Path, archive_goals) -> Path:
    """Append an aspirations-archive.jsonl to an existing world dir."""
    (w / "aspirations-archive.jsonl").write_text(
        json.dumps({
            "id": "asp-arch",
            "goals": [
                dict({k: gid, "status": "completed",
                      "completed_by_role": role, "outcome_class": outcome},
                     **({"completed_at": g[4] + "T00:00:00"} if len(g) > 4 else {}))
                for g in archive_goals
                for (k, gid, role, outcome) in [g[:4]]
            ],
        }) + "\n",
        encoding="utf-8",
    )
    return w


def test_archive_rows_extend_the_population_and_the_window(
        tmp_path, capsys, monkeypatch):
    """guard-676 case: the span only exists across both files.

    One live close (2026-09-15) + ten archive closes spanning
    2026-09-01..2026-09-10: live-only that population is a ONE-day span, which
    the window guard must withhold; live+archive it is 14 days, and a verdict
    is issued. 4 of 11 have artifacts -> 36.4% < 60% -> FAIL, and the FAIL is
    the point: a live-only reader would have reported INSUFFICIENT DATA forever,
    which is the vacuous non-verdict the check exists to end.
    """
    goals = [("id", "g-10-01", "worker", "deep", "2026-09-15")]
    archive = [("id", f"g-10-1{i}", "worker", "deep", d)
               for i, d in enumerate(
                   ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
                    "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08",
                    "2026-09-09", "2026-09-10"])]
    w = _add_archive(_world(tmp_path, goals=goals,
                            rb=["g-10-01", "g-10-10", "g-10-11", "g-10-12"]),
                     archive)
    m = war.measure(w)
    assert m["worker_population"] == 11, m
    assert m["worker_window_days"] == 14, m
    assert m["worker_window_first"] == "2026-09-01", m
    assert m["worker_window_last"] == "2026-09-15", m

    rc = _run(war, w, monkeypatch,
              ["--min-sample", "10", "--min-window-days", "14"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("FAIL"), out
    assert "4/11" in out, out


def test_archive_dedup_the_live_record_wins(tmp_path):
    """A goal in BOTH files is counted once, with the live fields winning.

    g-11-01: archive says worker/deep (countable), live says unstamped/deep.
    If the archive row counted, worker_population would be 1; live wins, so it
    is unstamped. The control (archive-only world) returns 1 — proving the
    live row is what suppressed it, not a broken reader.
    """
    live = [("id", "g-11-01", "", "deep", "2026-09-10")]
    arch = [("id", "g-11-01", "worker", "deep", "2026-09-10")]
    w = _add_archive(_world(tmp_path, goals=live, rb=["g-11-01"]), arch)
    m = war.measure(w)
    assert m["worker_population"] == 0, m
    assert m["unstamped_population"] == 1, m
    assert m["total_completed_goals"] == 1, m      # not double-counted

    # Nested under tmp_path/"control" because _world mkdirs tmp_path/"world"
    # and the first world above already occupies it (parents=True, same as the
    # id-shape test above).
    control = _add_archive(
        _world(tmp_path / "control", goals=[], rb=["g-11-01"]), arch)
    mc = war.measure(control)
    assert mc["worker_population"] == 1, mc


def test_archive_file_is_optional(tmp_path):
    """A fresh world has no archive yet; the live-only path still works."""
    goals = [("id", "g-12-01", "worker", "deep", "2026-09-10")]
    w = _world(tmp_path, goals=goals, rb=["g-12-01"])
    m = war.measure(w)
    assert m["worker_population"] == 1, m
    assert m["worker_with_artifact"] == 1, m


def test_CONTROL_archive_removal_collapses_to_window_withhold(
        tmp_path, capsys, monkeypatch):
    """guard-2903: prove the archive read — not anything else — widened it.

    Live holds 10 closes over a 2-day span (09-13..09-15); the archive holds
    10 more over 08-24..08-31. With the archive, the union span is 22 days,
    the guard clears, and a verdict is issued. Delete ONLY the archive file
    and the same population must collapse back to INSUFFICIENT DATA (window):
    if it did not, the extending case above was green for an unrelated reason
    and would keep passing after the fix was removed.
    """
    live = ([("id", f"g-13-0{i}", "worker", "deep", "2026-09-13")
             for i in range(1, 5)]
            + [("id", f"g-13-1{i}", "worker", "deep", "2026-09-14")
               for i in range(1, 4)]
            + [("id", f"g-13-2{i}", "worker", "deep", "2026-09-15")
               for i in range(1, 4)])
    archive = [("id", f"g-13-3{i}", "worker", "deep",
                f"2026-08-{24 + i:02d}") for i in range(10)]
    w = _add_archive(_world(tmp_path, goals=live, rb=[]), archive)

    rc = _run(war, w, monkeypatch,
              ["--min-sample", "10", "--min-window-days", "14"])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert out.startswith("FAIL"), out       # verdict: the union window cleared

    (w / "aspirations-archive.jsonl").unlink()
    rc = _run(war, w, monkeypatch,
              ["--min-sample", "10", "--min-window-days", "14"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "INSUFFICIENT DATA (window)" in out, out
    assert "2 day(s)" in out, out
