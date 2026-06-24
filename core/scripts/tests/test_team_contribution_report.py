"""test_team_contribution_report.py --  (asp-318, Team-eval T2).

Pins the team-contribution-report.py aggregator. Each test seeds synthetic
telemetry JSONL into tmp world/meta dirs, runs the script as a subprocess with
--world-dir/--meta-dir/--no-commits/--now (so windowing is deterministic and the
git section is skipped), and asserts the emitted scorecard.

The load-bearing correctness property is `test_cumulative_counts_not_summed`:
productivity-snapshots carry SESSION-CUMULATIVE running totals, so the aggregator
must report the LATEST snapshot's counts (not a sum across the window).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "team-contribution-report.py"
PYTHON = sys.executable

# Pinned window upper bound; cutoff for --window-days 7 is 2026-06-07T12:00:00.
NOW = "2026-06-14T12:00:00"
IN_WINDOW = "2026-06-12T09:00:00"
IN_WINDOW_2 = "2026-06-13T09:00:00"
OUT_OF_WINDOW = "2026-05-30T09:00:00"


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _snap(agent, ts, score, goals_completed=1, deep_ratio=0.8, tree_writes=0,
          hyp_resolved=0):
    return {"ts": ts, "agent": agent, "score": score, "threshold": 0.25,
            "decision": "above_threshold",
            "breakdown": {"deep_ratio": deep_ratio, "encoding_ratio": 1.0,
                          "routine_ratio": round(1 - deep_ratio, 2),
                          "counts": {"tree_writes": tree_writes, "rb": 0, "guards": 0,
                                     "hyp_created": 0, "hyp_resolved": hyp_resolved,
                                     "experience": 0}},
            "goals_completed": goals_completed, "productive_goals": goals_completed}


def _seed(tmp_path, snaps=None, gates=None, overrides=None, dups=None):
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    _write_jsonl(world / "productivity-snapshots.jsonl", snaps or [])
    _write_jsonl(meta / "gate-firings.jsonl", gates or [])
    _write_jsonl(world / "override-bypass-ledger.jsonl", overrides or [])
    _write_jsonl(world / "goal-duplication-overrides.jsonl", dups or [])
    return world, meta


def _run(world, meta, *extra):
    r = subprocess.run(
        [PYTHON, str(SCRIPT), "--world-dir", str(world), "--meta-dir", str(meta),
         "--no-commits", "--now", NOW, "--format", "json", *extra],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    return json.loads(r.stdout)


def test_contribution_aggregation(tmp_path):
    """iterations = in-window snapshot count; avg_score = mean."""
    world, meta = _seed(tmp_path, snaps=[
        _snap("delta", IN_WINDOW, 0.6),
        _snap("delta", IN_WINDOW_2, 0.8),
        _snap("alpha", IN_WINDOW, 0.9),
    ])
    out = _run(world, meta)
    d = out["agents"]["delta"]["contribution"]
    assert d["iterations"] == 2
    assert d["avg_score"] == 0.7  # mean(0.6, 0.8)
    assert out["agents"]["alpha"]["contribution"]["iterations"] == 1


def test_window_filter_excludes_old(tmp_path):
    """A snapshot before the cutoff is excluded from the window."""
    world, meta = _seed(tmp_path, snaps=[
        _snap("delta", OUT_OF_WINDOW, 0.5),
        _snap("delta", IN_WINDOW, 0.9),
    ])
    out = _run(world, meta)
    d = out["agents"]["delta"]["contribution"]
    assert d["iterations"] == 1          # only the in-window snapshot
    assert d["avg_score"] == 0.9


def test_cumulative_counts_not_summed(tmp_path):
    """The double-count guard: two cumulative snapshots -> latest_snapshot uses
    the LATEST values, NOT a sum. goals_completed 10 then 20 -> 20, not 30."""
    world, meta = _seed(tmp_path, snaps=[
        _snap("delta", IN_WINDOW, 0.6, goals_completed=10, tree_writes=4, hyp_resolved=2),
        _snap("delta", IN_WINDOW_2, 0.8, goals_completed=20, tree_writes=9, hyp_resolved=5),
    ])
    out = _run(world, meta)
    latest = out["agents"]["delta"]["contribution"]["latest_snapshot"]
    assert latest["goals_completed"] == 20      # latest, not 30
    assert latest["tree_writes"] == 9           # latest, not 13
    assert latest["hyp_resolved"] == 5          # latest, not 7
    assert out["agents"]["delta"]["contribution"]["iterations"] == 2


def test_harm_aggregation(tmp_path):
    """gate blocks = non-pass decisions; overrides + dup-overrides counted;
    block_ratio = blocks/total."""
    world, meta = _seed(
        tmp_path,
        snaps=[_snap("delta", IN_WINDOW, 0.7)],
        gates=[
            {"ts": IN_WINDOW, "gate_id": "g1", "decision": "pass", "agent": "delta"},
            {"ts": IN_WINDOW, "gate_id": "g2", "decision": "block", "agent": "delta"},
            {"ts": IN_WINDOW, "gate_id": "g3", "decision": "refuse", "agent": "delta"},
        ],
        overrides=[{"ts": IN_WINDOW, "agent": "delta", "override_token": "x"}],
        dups=[{"timestamp": IN_WINDOW, "agent": "delta", "title": "t"}],
    )
    out = _run(world, meta)
    h = out["agents"]["delta"]["harm"]
    assert h["gate_blocks"] == 2            # block + refuse (pass excluded)
    assert h["block_ratio"] == round(2 / 3, 4)
    assert h["override_count"] == 1
    assert h["dup_override_count"] == 1


def test_harm_per_iteration(tmp_path):
    """harm_per_iteration = (blocks + overrides + dups + reverts) / iterations."""
    world, meta = _seed(
        tmp_path,
        snaps=[_snap("delta", IN_WINDOW, 0.7), _snap("delta", IN_WINDOW_2, 0.7)],
        gates=[{"ts": IN_WINDOW, "gate_id": "g", "decision": "block", "agent": "delta"},
               {"ts": IN_WINDOW, "gate_id": "g", "decision": "block", "agent": "delta"}],
        overrides=[{"ts": IN_WINDOW, "agent": "delta"}],
        dups=[{"timestamp": IN_WINDOW, "agent": "delta"}],
    )
    out = _run(world, meta)
    # 2 blocks + 1 override + 1 dup = 4 harm events over 2 iterations = 2.0
    assert out["agents"]["delta"]["harm_per_iteration"] == 2.0


def test_agent_filter(tmp_path):
    world, meta = _seed(tmp_path, snaps=[
        _snap("delta", IN_WINDOW, 0.7), _snap("alpha", IN_WINDOW, 0.9)])
    out = _run(world, meta, "--agent", "delta")
    assert set(out["agents"].keys()) == {"delta"}


def test_empty_inputs_exit_zero(tmp_path):
    """Missing/empty telemetry -> empty agents, clean exit (no crash)."""
    world, meta = _seed(tmp_path)
    out = _run(world, meta)
    assert out["agents"] == {}
    assert out["window_days"] == 7


def test_malformed_line_tolerated(tmp_path):
    """A half-written tail line is skipped, not fatal."""
    world, meta = _seed(tmp_path, snaps=[_snap("delta", IN_WINDOW, 0.7)])
    # Append a malformed line to the snapshots file.
    snap_file = world / "productivity-snapshots.jsonl"
    with snap_file.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-06-12T10:00:00", "agent": "delta", BROKEN\n')
    out = _run(world, meta)
    assert out["agents"]["delta"]["contribution"]["iterations"] == 1
