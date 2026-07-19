"""Tests for weakness-signals.py (5 — windowed weakness deltas).

Pins the discrimination contract: ambient mass-matched guardrails (the
precheck guardrail-check inflates ~58 rules by the same amount every
iteration) must NOT flag, while a genuinely-hot guard whose delta stands
clear of the ambient median MUST. Also pins baseline seed/advance semantics.
"""

import json
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "weakness-signals.py"


def _write_stores(world: Path, guards, sigs):
    world.mkdir(parents=True, exist_ok=True)
    with open(world / "guardrails.jsonl", "w", encoding="utf-8") as f:
        for g in guards:
            f.write(json.dumps(g) + "\n")
    with open(world / "pattern-signatures.jsonl", "w", encoding="utf-8") as f:
        for s in sigs:
            f.write(json.dumps(s) + "\n")


def _run(world: Path, report: Path, *extra):
    cmd = [sys.executable, str(SCRIPT), "--world-dir", str(world),
           "--report-path", str(report), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _guard(gid, times_active, status="active"):
    return {"id": gid, "status": status, "rule": f"rule {gid}",
            "utilization": {"times_active": times_active}}


def _sig(sid, total, confirmed, status="active"):
    return {"id": sid, "status": status, "name": f"sig {sid}",
            "outcome_stats": {"total": total, "confirmed": confirmed,
                              "accuracy": (confirmed / total) if total else 0}}


def test_first_run_seeds_baseline_no_signals(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    _write_stores(world, [_guard("guard-001", 100)], [_sig("sig-001", 5, 5)])

    out = _run(world, report)

    assert out["seeded"] is True
    assert out["guardrail_signals"] == []
    assert out["signature_signals"] == []
    assert out["baseline_updated"] is True
    saved = yaml.safe_load(report.read_text(encoding="utf-8"))
    assert saved["signal_baseline"]["guardrail_times_active"]["guard-001"] == 100
    assert saved["signal_baseline"]["signature_outcomes"]["sig-001"]["total"] == 5


def test_ambient_mass_match_not_flagged_hot_guard_flagged(tmp_path):
    """58 guards gain the ambient +50; one guard gains +200 — only it flags."""
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    ambient = [_guard(f"guard-{i:03d}", 1000) for i in range(58)]
    hot = _guard("guard-hot", 1000)
    _write_stores(world, ambient + [hot], [])
    _run(world, report)  # seed

    ambient2 = [_guard(f"guard-{i:03d}", 1050) for i in range(58)]
    hot2 = _guard("guard-hot", 1200)
    _write_stores(world, ambient2 + [hot2], [])
    out = _run(world, report)

    flagged = {s["id"] for s in out["guardrail_signals"]}
    assert flagged == {"guard-hot"}
    sig = out["guardrail_signals"][0]
    assert sig["delta"] == 200
    assert out["guardrail_median_delta"] == 50


def test_uniform_ambient_yields_no_signals(tmp_path):
    """All guards move by the same ambient amount — nothing discriminates."""
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    _write_stores(world, [_guard(f"guard-{i}", 100) for i in range(10)], [])
    _run(world, report)
    _write_stores(world, [_guard(f"guard-{i}", 150) for i in range(10)], [])
    out = _run(world, report)
    assert out["guardrail_signals"] == []


def test_g115_2141_large_nonmover_majority_does_not_collapse_median(tmp_path):
    """1 regression: a large NON-MOVING guardrail majority must NOT
    collapse the discrimination median to 0.

    The script computes the ambient median over NONZERO movers only (the
    `if delta > 0` filter in compute_guardrail_signals), so a uniform
    mass-matched cohort of 61 guards at +31 amid 200 non-movers has median 31
    -> threshold max(3, 2*31)=62 -> the whole uniform block is suppressed,
    while a genuinely-hot guard (+200) still clears the bar and flags.

    Contrast the reported bug's surface — /reflect Step 5.55's LLM-side
    guardrail windowing pseudocode (the pre-extraction twin of this script,
    still live on the un-reconciled upstream tree) computes the median over
    ALL guards; the non-keyword-matching majority sits at delta 0, so the
    median collapses to 0, the threshold falls to min_delta=3, and the entire
    uniform +31 block false-passes. This test pins the CORRECT (mover-cohort)
    behavior so a reconciliation-time "fix" cannot regress the script into the
    median-over-all-guards trap the goal title superficially suggests.
    """
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    movers = [_guard(f"guard-mv{i:03d}", 1000) for i in range(61)]
    hot = _guard("guard-hot", 1000)
    nonmovers = [_guard(f"guard-still{i:03d}", 500) for i in range(200)]
    _write_stores(world, movers + [hot] + nonmovers, [])
    _run(world, report)  # seed

    # Window: 61 mass-matched guards +31 (ambient noise), one genuinely-hot
    # guard +200, 200 non-keyword-matching guards +0 (the large still majority).
    movers2 = [_guard(f"guard-mv{i:03d}", 1031) for i in range(61)]
    hot2 = _guard("guard-hot", 1200)
    nonmovers2 = [_guard(f"guard-still{i:03d}", 500) for i in range(200)]
    _write_stores(world, movers2 + [hot2] + nonmovers2, [])
    out = _run(world, report)

    flagged = {s["id"] for s in out["guardrail_signals"]}
    assert flagged == {"guard-hot"}          # only the genuine outlier
    assert out["guardrail_median_delta"] == 31  # NOT 0 — non-movers excluded


def test_new_guard_since_baseline_uses_lifetime_as_window(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    _write_stores(world, [_guard("guard-old", 100)], [])
    _run(world, report)
    # old guard idle (+0); brand-new guard fires 40x since baseline
    _write_stores(world, [_guard("guard-old", 100), _guard("guard-new", 40)], [])
    out = _run(world, report)
    flagged = {s["id"] for s in out["guardrail_signals"]}
    assert flagged == {"guard-new"}


def test_signature_window_accuracy(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    # lifetime looks fine (9/10); window will be 1/4 = 0.25 -> signal
    _write_stores(world, [], [_sig("sig-w", 10, 9), _sig("sig-ok", 10, 9)])
    _run(world, report)
    _write_stores(world, [], [_sig("sig-w", 14, 10), _sig("sig-ok", 14, 13)])
    out = _run(world, report)
    ids = {s["id"] for s in out["signature_signals"]}
    assert ids == {"sig-w"}
    s = out["signature_signals"][0]
    assert s["window_total"] == 4
    assert s["window_accuracy"] == 0.25


def test_signature_below_min_outcomes_not_flagged(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    _write_stores(world, [], [_sig("sig-thin", 10, 9)])
    _run(world, report)
    _write_stores(world, [], [_sig("sig-thin", 12, 9)])  # only 2 window outcomes
    out = _run(world, report)
    assert out["signature_signals"] == []


def test_no_baseline_update_flag_preserves_window(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    _write_stores(world, [_guard("guard-a", 10)], [])
    _run(world, report)
    _write_stores(world, [_guard("guard-a", 20)], [])
    out1 = _run(world, report, "--no-baseline-update")
    assert out1["baseline_updated"] is False
    # window unchanged on the next read — baseline did not advance
    out2 = _run(world, report, "--no-baseline-update")
    assert out2["guardrail_signals"] == out1["guardrail_signals"]


def test_missing_stores_fail_open(tmp_path):
    world = tmp_path / "empty-world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    world.mkdir(parents=True)
    out = _run(world, report)
    assert out["seeded"] is True
    assert out["guardrail_signals"] == []
    assert any("guardrails.jsonl" in n for n in out["notes"])


def test_corrupt_report_not_clobbered(tmp_path):
    """Present-but-unparseable report: signals fail-open AND the file is NOT
    rewritten (fresh-eyes F-1 — a corrupt-but-recoverable report must never be
    replaced with a bare signal_baseline)."""
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    report.parent.mkdir(parents=True)
    corrupt = "weaknesses: [{unclosed\n\t: bad yaml {{{"
    report.write_text(corrupt, encoding="utf-8")
    _write_stores(world, [_guard("guard-a", 5)], [])

    out = _run(world, report)

    assert out["seeded"] is False
    assert out["baseline_updated"] is False
    assert out["guardrail_signals"] == []
    assert any("unparseable" in n for n in out["notes"])
    assert report.read_text(encoding="utf-8") == corrupt  # byte-identical


def test_preserves_existing_report_fields(tmp_path):
    world = tmp_path / "world"
    report = tmp_path / "agent" / "weakness-report.yaml"
    report.parent.mkdir(parents=True)
    report.write_text(yaml.safe_dump({
        "last_analyzed": "2026-05-20T10:32:00",
        "analysis_count": 1,
        "weaknesses": [{"id": "wk-1", "severity": "HIGH"}],
    }), encoding="utf-8")
    _write_stores(world, [_guard("guard-a", 5)], [])
    _run(world, report)
    saved = yaml.safe_load(report.read_text(encoding="utf-8"))
    assert saved["analysis_count"] == 1
    assert saved["weaknesses"][0]["id"] == "wk-1"
    assert "signal_baseline" in saved
