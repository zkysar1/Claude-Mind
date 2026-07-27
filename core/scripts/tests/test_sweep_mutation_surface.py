#!/usr/bin/env python3
"""Tests for sweep-mutation-surface.py ().

The consumer surfaces silent auto-close sweep mutations by reading the 3
per-sweep metrics logs. These tests pin: (1) it picks only NEW apply-mutation
records (not run_summary, not pre-watermark), (2) the watermark advances
exactly-once semantics, (3) --no-advance leaves the watermark untouched,
(4) fail-open on torn lines / missing logs. No board post is exercised
(--announce omitted) so the tests are hermetic and daemon-free.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sweep-mutation-surface.py"

APPLY_TYPES = {
    "parent-supersession-sweep-metrics.jsonl": "parent_superseded",
    "unblock-parent-status-sweep-metrics.jsonl": "unblock_parent_resolved",
    "routing-audit-target-status-sweep-metrics.jsonl": "routing_audit_target_resolved",
}


def _write_log(metrics_dir, fname, records):
    p = Path(metrics_dir) / fname
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _run(metrics_dir, wm_file, now, extra=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"  # guard-955: never touch S3 from a test
    env["MIND_AGENT"] = "testagent"
    cmd = [sys.executable, str(SCRIPT),
           "--metrics-dir", str(metrics_dir),
           "--watermark-file", str(wm_file),
           "--now", now, "--output", "json"]
    if extra:
        cmd += extra
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, "must be fail-open exit 0: %s" % proc.stderr
    return json.loads(proc.stdout)


def test_picks_only_new_apply_mutations(tmp_path):
    """New apply-mutations count; run_summary + pre-watermark records excluded."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")  # watermark
    _write_log(md, "parent-supersession-sweep-metrics.jsonl", [
        {"type": "parent_superseded", "goal_id": "g-1-1",
         "timestamp": "2026-07-19T05:00:00", "agent": "alpha"},          # NEW apply
        {"type": "parent_superseded", "goal_id": "g-1-2",
         "timestamp": "2026-07-18T05:00:00", "agent": "alpha"},          # OLD (< wm)
        {"type": "run_summary", "timestamp": "2026-07-19T06:00:00",
         "applied": 1, "agent": "alpha"},                                # summary → excluded
    ])
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 1, out
    assert out["mutations"][0]["goal_id"] == "g-1-2" or out["mutations"][0]["goal_id"] == "g-1-1"
    ids = [m["goal_id"] for m in out["mutations"]]
    assert ids == ["g-1-1"], ids  # only the new apply record


def test_run_summary_never_counts_even_with_goal_id(tmp_path):
    """A run_summary is excluded by type even if it carried a goal_id."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    _write_log(md, "unblock-parent-status-sweep-metrics.jsonl", [
        {"type": "run_summary", "goal_id": "g-x-x",
         "timestamp": "2026-07-19T05:00:00", "applied": 3},
    ])
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 0, out


def test_all_three_sweeps_aggregated(tmp_path):
    """One new apply-mutation in each of the 3 logs → aggregated count 3."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    for fname, atype in APPLY_TYPES.items():
        _write_log(md, fname, [
            {"type": atype, "goal_id": "g-" + atype[:3],
             "timestamp": "2026-07-19T05:00:00", "agent": "bravo"},
        ])
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 3, out
    sweeps = {m["sweep"] for m in out["mutations"]}
    assert sweeps == {"parent-supersession", "unblock-parent-status",
                      "routing-audit-target"}, sweeps


def test_watermark_advances_exactly_once(tmp_path):
    """After a run, the same mutation is not re-surfaced (watermark advanced)."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    _write_log(md, "parent-supersession-sweep-metrics.jsonl", [
        {"type": "parent_superseded", "goal_id": "g-1-1",
         "timestamp": "2026-07-19T05:00:00", "agent": "alpha"},
    ])
    out1 = _run(md, wm, "2026-07-19T10:00:00")           # advances wm → 10:00
    assert out1["new_mutations"] == 1, out1
    assert wm.read_text().strip() == "2026-07-19T10:00:00"
    out2 = _run(md, wm, "2026-07-19T11:00:00")           # same mutation now < wm
    assert out2["new_mutations"] == 0, out2


def test_no_advance_leaves_watermark(tmp_path):
    """--no-advance must not rewrite the watermark file."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert wm.read_text().strip() == "2026-07-19T00:00:00"


def test_fail_open_on_torn_line(tmp_path):
    """A torn JSON line is skipped, not fatal; valid records still counted."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    p = md / "routing-audit-target-status-sweep-metrics.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type": "routing_audit_target_resolved", "goal_id": "g-a", '
                '"timestamp": "2026-07-19T05:00:00"}\n')
        f.write('{"type": "routing_audit_target_resolved", "goal_id": "g-b"  # TORN\n')
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 1, out
    assert out["mutations"][0]["goal_id"] == "g-a"


def test_missing_metrics_dir_is_empty_not_fatal(tmp_path):
    """A metrics dir with no logs → 0 mutations, exit 0."""
    md = tmp_path / "empty"
    md.mkdir()
    wm = tmp_path / "wm"
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 0, out


def test_own_announce_filter_prevents_cross_agent_spam(tmp_path):
    """Only the SWEEPING agent's own mutations are announce-candidates; a
    mutation applied by another agent surfaces in the header but is NOT
    re-announced by this agent (else N agents → N board posts per mutation)."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "wm"
    wm.write_text("2026-07-19T00:00:00")
    _write_log(md, "parent-supersession-sweep-metrics.jsonl", [
        {"type": "parent_superseded", "goal_id": "g-mine",
         "timestamp": "2026-07-19T05:00:00", "agent": "testagent"},   # own
        {"type": "parent_superseded", "goal_id": "g-theirs",
         "timestamp": "2026-07-19T06:00:00", "agent": "alpha"},       # other agent
    ])
    out = _run(md, wm, "2026-07-19T10:00:00", extra=["--no-advance"])
    assert out["new_mutations"] == 2, out                 # both visible in header
    assert out["own_announce_candidates"] == 1, out       # only own is announce-eligible


def test_first_run_window_default(tmp_path):
    """No watermark file → default window lookback; a mutation inside the
    window is surfaced, one outside is not."""
    md = tmp_path / "metrics"
    md.mkdir()
    wm = tmp_path / "nonexistent-wm"  # missing → window fallback
    _write_log(md, "parent-supersession-sweep-metrics.jsonl", [
        {"type": "parent_superseded", "goal_id": "g-in",
         "timestamp": "2026-07-19T06:00:00", "agent": "alpha"},   # 4h before now → in 24h window
        {"type": "parent_superseded", "goal_id": "g-out",
         "timestamp": "2026-07-17T06:00:00", "agent": "alpha"},   # 52h before now → outside
    ])
    out = _run(md, wm, "2026-07-19T10:00:00",
               extra=["--no-advance", "--window-hours", "24"])
    ids = [m["goal_id"] for m in out["mutations"]]
    assert ids == ["g-in"], ids


def _load_module():
    """Import sweep-mutation-surface.py in-process (its name is not importable)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_sms", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_watermark_path_is_project_rooted_not_core(tmp_path):
    """Default watermark lands at agents/<agent>/session/, never core/agents/... ().

    THE BUG: the default branch read `repo = Path(__file__).resolve().parent.parent`,
    which is CORE_ROOT (this file lives in core/scripts), so the watermark was written
    to <root>/core/agents/<agent>/session/sweep-surface-watermark — a path nothing
    reads. _read_watermark then fell back to the window default on EVERY run, so the
    same mutations re-surfaced forever while local stdout still looked correct.

    This was the SECOND instance of the `.parent`-as-PROJECT_ROOT class in this one
    file: the g-115-2681 fix corrected the board-post site and missed this one. It
    survived because every test above passes --watermark-file explicitly, so the
    default branch — the only branch production uses — was never executed.

    The fix routes through _paths.agent_state_dir rather than re-deriving a root,
    per CLAUDE.md "Agent-dir Resolution" (never join PROJECT_ROOT/<agent> by hand);
    that also makes the path track an AGENTS_PARENT_DIR rename for free.
    """
    mod = _load_module()
    assert mod._agent_state_dir is not None, (
        "_paths.agent_state_dir failed to import — the default branch would fall "
        "back to the manual derivation, which is exactly what regressed"
    )

    got = Path(mod._agent_state_dir("someagent")) / "sweep-surface-watermark"
    parts = got.parts

    assert "core" not in parts, f"watermark path is core-rooted again: {got}"
    assert parts[-3:] == ("someagent", "session", "sweep-surface-watermark"), (
        f"expected .../<agent>/session/sweep-surface-watermark, got {got}"
    )
    assert got.is_absolute(), f"watermark path must be absolute, got {got}"


def test_manual_fallback_derivation_is_project_rooted():
    """The no-_paths fallback counts TWO parents from core/scripts, not one.

    Pins the arithmetic of the else-branch independently, since a test env without
    _paths takes it and would otherwise reintroduce the same off-by-one silently.
    """
    mod = _load_module()
    # SCRIPT_DIR is core/scripts; the fallback uses SCRIPT_DIR.parent.parent.
    assert mod.SCRIPT_DIR.name == "scripts"
    assert mod.SCRIPT_DIR.parent.name == "core"
    fallback_root = mod.SCRIPT_DIR.parent.parent
    assert fallback_root.name != "core", (
        f"fallback root resolved to {fallback_root} — that is CORE_ROOT, not the "
        "project root (g-335-253 regression)"
    )
    assert (fallback_root / "core" / "scripts").is_dir(), (
        f"fallback root {fallback_root} does not look like a project root"
    )


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
