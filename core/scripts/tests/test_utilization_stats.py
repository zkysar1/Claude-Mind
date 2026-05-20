"""test_utilization_stats.py — coverage for the B.1 reporting tool.

Verifies:
  * Composite evidence formula matches the docstring (helpful + 0.5*inferred
    + 0.25*active + 1.0*cited).
  * Candidate filter excludes records with any positive evidence.
  * Candidate filter respects MIN_EXPOSURE (retrieval+skipped >= 50).
  * Candidate filter respects MIN_AGE_DAYS (>= 30).
  * Candidate filter excludes records still inside their next_review_eligible_at
    window.
  * `auto_flagged_for_review = true` forces inclusion regardless of evidence.
  * Sort order: lowest evidence first, oldest next, most-exposed next.
  * `rules audit` produces a JSON report for a small synthetic rules dir.

Self-contained: writes a tmpdir, points WORLD_DIR + AGENT_DIR + MIND_AGENT_DIR
at it via env vars, runs the script as a subprocess.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SCRIPT = CORE_SCRIPTS / "utilization-stats.py"
sys.path.insert(0, str(CORE_SCRIPTS))


def _run(args, env_overrides):
    """Run the script; return parsed JSON stdout."""
    env = os.environ.copy()
    env.update(env_overrides)
    # Ensure the script's _paths.py picks up our tmp dirs by clearing any
    # ambient MIND_AGENT that would override MIND_WORLD/META.
    env.pop("MIND_AGENT", None)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"non-zero exit {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return json.loads(proc.stdout)


def _make_guard(gid, **util_overrides):
    """Build a guardrail record with sensible defaults; util fields overridable."""
    util = {
        "retrieval_count": 0,
        "times_helpful": 0,
        "times_inferred_helpful": 0,
        "times_active": 0,
        "times_cited": 0,
        "times_skipped": 0,
        "utilization_score": 0,
        "last_retrieved": "",
    }
    util.update(util_overrides)
    return {
        "id": gid,
        "rule": f"Test rule {gid}",
        "category": "test",
        "trigger_condition": "test",
        "source": "test",
        "status": "active",
        "created": "2026-01-01",  # > 30 days ago vs the test fixture date
        "utilization": util,
    }


def _seed_world(tmp):
    """Create a tmp WORLD_DIR with empty stores. Returns Path."""
    world = tmp / "world"
    world.mkdir()
    (world / "guardrails.jsonl").write_text("", encoding="utf-8")
    (world / "reasoning-bank.jsonl").write_text("", encoding="utf-8")
    return world


def _seed_agent(tmp):
    """Create a tmp AGENT_DIR with empty journal/experience."""
    agent = tmp / "alpha"
    agent.mkdir()
    (agent / "journal.jsonl").write_text("", encoding="utf-8")
    (agent / "experience.jsonl").write_text("", encoding="utf-8")
    return agent


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_evidence_formula_matches_docstring():
    """Direct unit test of _evidence helper to lock the formula."""
    # Import via load — script has hyphenated name.
    import importlib.util
    spec = importlib.util.spec_from_file_location("utilization_stats",
                                                   str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    util = {
        "times_helpful": 2,
        "times_inferred_helpful": 4,
        "times_active": 8,
        "times_cited": 1,
    }
    # Expected: 2 + 0.5*4 + 0.25*8 + 1.0*1 = 2 + 2 + 2 + 1 = 7.0
    assert mod._evidence(util) == 7.0
    # Empty util → 0
    assert mod._evidence({}) == 0.0
    # Bool resistance: True/False are int subclasses; treat as zero (counters
    # should be ints, not bools).
    assert mod._evidence({"times_helpful": True}) == 0.0


def test_candidates_exclude_evidenced_and_low_exposure():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        guards = [
            # 1. Zero evidence + high exposure + old → CANDIDATE
            _make_guard("guard-001", retrieval_count=100, times_skipped=50),
            # 2. Has helpful=1 → NOT candidate (positive evidence)
            _make_guard("guard-002", retrieval_count=100, times_skipped=50,
                        times_helpful=1),
            # 3. Has cited=1 → NOT candidate
            _make_guard("guard-003", retrieval_count=100, times_skipped=50,
                        times_cited=1),
            # 4. Zero evidence + LOW exposure → NOT candidate
            _make_guard("guard-004", retrieval_count=10, times_skipped=10),
        ]
        _write_jsonl(world / "guardrails.jsonl", guards)
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "candidates", "--limit", "10"], env)
        ids = {it["id"] for it in out["items"]}
        assert ids == {"guard-001"}, f"unexpected candidate set: {ids}"


def test_candidates_age_gate():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        recent = (date.today() - timedelta(days=10)).isoformat()
        old = (date.today() - timedelta(days=60)).isoformat()
        guards = [
            # Recent zero-evidence — too young, should NOT be a candidate
            _make_guard("guard-young", retrieval_count=100, times_skipped=50),
            # Old zero-evidence — should be a candidate
            _make_guard("guard-old", retrieval_count=100, times_skipped=50),
        ]
        guards[0]["created"] = recent
        guards[1]["created"] = old
        _write_jsonl(world / "guardrails.jsonl", guards)
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "candidates", "--limit", "10"], env)
        ids = {it["id"] for it in out["items"]}
        assert ids == {"guard-old"}, f"age gate broken: {ids}"


def test_candidates_review_window_exemption():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        future = (date.today() + timedelta(days=15)).isoformat()
        past = (date.today() - timedelta(days=2)).isoformat()
        guards = [
            _make_guard("guard-exempt", retrieval_count=100, times_skipped=50),
            _make_guard("guard-aged-out", retrieval_count=100, times_skipped=50),
        ]
        guards[0]["next_review_eligible_at"] = future
        guards[1]["next_review_eligible_at"] = past
        _write_jsonl(world / "guardrails.jsonl", guards)
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "candidates", "--limit", "10"], env)
        ids = {it["id"] for it in out["items"]}
        assert ids == {"guard-aged-out"}, (
            f"review-window exemption not honored: {ids}"
        )


def test_auto_flagged_forces_inclusion_despite_evidence():
    """C.3 escape hatch: even with positive evidence, auto-flagged items
    appear in the candidate list."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        # This guard has positive evidence (would normally exclude it)
        # AND is auto-flagged (forces inclusion). Also low exposure to prove
        # auto-flag bypasses MIN_EXPOSURE too.
        flagged = _make_guard("guard-flagged", retrieval_count=2,
                              times_skipped=0, times_helpful=5)
        flagged["auto_flagged_for_review"] = True
        _write_jsonl(world / "guardrails.jsonl", [flagged])
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "candidates", "--limit", "10"], env)
        ids = {it["id"] for it in out["items"]}
        assert ids == {"guard-flagged"}, (
            f"auto_flagged_for_review did not force inclusion: {ids}"
        )


def test_sort_order_evidence_then_age_then_exposure():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        # All zero-evidence; tiebreaker by age desc, then exposure desc.
        old = (date.today() - timedelta(days=200)).isoformat()
        older = (date.today() - timedelta(days=400)).isoformat()
        guards = [
            _make_guard("guard-young-low",
                         retrieval_count=50, times_skipped=10),
            _make_guard("guard-old-high",
                         retrieval_count=300, times_skipped=300),
            _make_guard("guard-old-low",
                         retrieval_count=50, times_skipped=10),
            _make_guard("guard-older-low",
                         retrieval_count=50, times_skipped=10),
        ]
        guards[0]["created"] = old
        guards[1]["created"] = old
        guards[2]["created"] = old
        guards[3]["created"] = older
        _write_jsonl(world / "guardrails.jsonl", guards)
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "candidates", "--limit", "10"], env)
        order = [it["id"] for it in out["items"]]
        # All four are candidates. Sort: evidence(0), -age, -exposure.
        # Older = larger age = smaller -age → comes first.
        # Within same age, larger exposure → smaller -exposure → comes first.
        assert order[0] == "guard-older-low", (
            f"older entry should sort first; got order={order}"
        )
        # guard-old-high has same age as guard-young-low and guard-old-low,
        # but higher exposure → comes before them.
        assert order.index("guard-old-high") < order.index("guard-old-low")


def test_rb_subcommand_works():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        rb = {
            "id": "rb-001",
            "title": "test",
            "type": "success",
            "category": "test",
            "content": "test",
            "status": "active",
            "created": "2026-01-01",
            "utilization": {
                "retrieval_count": 100, "times_skipped": 50,
                "times_helpful": 0, "times_inferred_helpful": 0,
                "times_active": 0, "times_cited": 0,
                "utilization_score": 0, "last_retrieved": "",
            },
        }
        _write_jsonl(world / "reasoning-bank.jsonl", [rb])
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["rb", "candidates", "--limit", "10"], env)
        assert out["candidate_count"] == 1
        assert out["items"][0]["id"] == "rb-001"
        assert out["items"][0]["kind"] == "reasoning_bank"


def test_report_subcommand_returns_full_active_set():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        world = _seed_world(tmp)
        agent = _seed_agent(tmp)
        guards = [
            _make_guard("guard-a"),
            _make_guard("guard-b", times_cited=5),
        ]
        # Plus a retired one — must NOT appear in report
        retired = _make_guard("guard-retired")
        retired["status"] = "retired"
        _write_jsonl(world / "guardrails.jsonl", guards + [retired])
        env = {
            "MIND_WORLD": str(world),
            "MIND_AGENT_DIR": str(agent),
            "MIND_META": str(tmp / "meta-fake"),
        }
        out = _run(["guardrails", "report"], env)
        ids = {it["id"] for it in out["items"]}
        assert ids == {"guard-a", "guard-b"}
        assert out["active_count"] == 2


def test_rules_audit_runs_against_real_repo():
    """Smoke test: rules audit returns sensible output against the live
    .claude/rules/ directory."""
    env = {}  # use ambient env (real WORLD_DIR / AGENT_DIR)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "rules", "audit"],
        env={**os.environ, "MIND_AGENT": "alpha"},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, (
        f"rules audit failed: stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    out = json.loads(proc.stdout)
    assert out["kind"] == "rules_audit"
    assert out["rule_count"] >= 10  # we have ~23
    # Each item should have required fields
    for it in out["items"]:
        assert "filename" in it
        assert "citation_count" in it
        assert "stale_paths" in it
        assert "stale_path_count" in it
