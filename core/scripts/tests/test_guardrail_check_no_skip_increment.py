"""test_guardrail_check_no_skip_increment.py — regression test for the
2026-05-09 fix to guardrail-check.py (P0 #4 from knowledge-system audit).

Pre-fix, `_check_store` incremented `utilization.times_skipped` on EVERY
non-matching active record per call. Audit found this fired hundreds of
times per session (every check call × every non-matching active record),
inflating skip counters by 2-15x retrieval_count. Post-fix only matched
records get incremented (times_active++); non-matched records are untouched.

These tests build a synthetic guardrails store, run `_check_store`, and
verify that:
  - matched records get times_active++
  - non-matched records are NOT modified at all (skip unchanged)
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# guardrail-check.py imports reasoning-bank.py which imports _paths which
# reads MIND_WORLD env. Set to a temp dir before module load.
#  capture-restore pattern: stash env BEFORE mutation so subsequent
# tests in the same pytest session don't inherit a popped MIND_AGENT. See
# test_applies_to_required.py for full rationale.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")

_TMPDIR = tempfile.mkdtemp(prefix="guardrail-check-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_GC_PATH = CORE_SCRIPTS / "guardrail-check.py"
_spec = importlib.util.spec_from_file_location("guardrail_check_mod", _GC_PATH)
_gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gc)

# Restore env so downstream tests inherit clean conftest defaults.
if _ORIG_MIND_WORLD is not None:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
elif "MIND_WORLD" in os.environ:
    del os.environ["MIND_WORLD"]
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _make_guard(gid, rule, category="framework-maintenance",
                created="2026-04-01", times_skipped=0, times_active=0,
                retrieval_count=0):
    return {
        "id": gid,
        "rule": rule,
        "trigger_condition": "test trigger",
        "source": "test",
        "category": category,
        "status": "active",
        "created": created,
        "tags": [],
        "experience_ref": None,
        "when_to_use": {"conditions": [], "category": category},
        "utilization": {
            "retrieval_count": retrieval_count,
            "last_retrieved": None,
            "times_helpful": 0,
            "times_noise": 0,
            "times_active": times_active,
            "times_skipped": times_skipped,
            "times_inferred_helpful": 0,
            "times_cited": 0,
            "times_inferred_unknown": 0,
            "utilization_score": 0,
            "utilization_score_v2": 0,
        },
    }


def _seed(records):
    p = Path(_TMPDIR) / "guardrails.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


def _read(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_matched_increments_active_only():
    """A guardrail whose text matches the requested context/outcome/phase
    gets times_active++. times_skipped stays at its pre-call value.
    """
    p = _seed([
        _make_guard("guard-001",
                    "After any infrastructure interaction, check email alerts.",
                    times_skipped=5, times_active=3, retrieval_count=10),
    ])
    _gc.GUARD_PATH = p

    matched = _gc._check_store(
        p, "guardrail",
        context="infrastructure", outcome="any", phase="post-execution",
        dry_run=False,
    )

    assert any(m["id"] == "guard-001" for m in matched), \
        f"expected guard-001 to match; got {matched}"

    after = _read(p)
    by_id = {r["id"]: r for r in after}
    util = by_id["guard-001"]["utilization"]
    assert util["times_active"] == 4, f"expected times_active 3→4, got {util['times_active']}"
    assert util["times_skipped"] == 5, \
        f"expected times_skipped unchanged at 5, got {util['times_skipped']}"


def test_non_matched_record_is_not_modified():
    """A guardrail that does NOT match the request is left completely
    untouched — pre-fix this incremented times_skipped, which inflated
    the counter by 2-15x retrieval_count over the lifetime of the bank.
    """
    p = _seed([
        _make_guard("guard-002",
                    "Before learning a new skill, do X.",  # not infra-related
                    category="learning-philosophy",
                    times_skipped=5, times_active=0, retrieval_count=10),
    ])
    _gc.GUARD_PATH = p

    matched = _gc._check_store(
        p, "guardrail",
        context="infrastructure", outcome="any", phase="post-execution",
        dry_run=False,
    )

    assert all(m["id"] != "guard-002" for m in matched), \
        f"guard-002 should NOT match; got matched={matched}"

    after = _read(p)
    by_id = {r["id"]: r for r in after}
    util = by_id["guard-002"]["utilization"]
    assert util["times_active"] == 0, f"times_active should stay 0, got {util['times_active']}"
    assert util["times_skipped"] == 5, \
        f"times_skipped should stay 5 (no inflation), got {util['times_skipped']}"


def test_mixed_population_only_matched_increments():
    """In a store with a mix of matching and non-matching records, only the
    matched ones see counter changes."""
    p = _seed([
        _make_guard("guard-A",
                    "After infrastructure failure, retry with backoff.",
                    times_skipped=2, times_active=0),
        _make_guard("guard-B",
                    "Always commit and push after a deploy.",  # mentions deploy → may match infrastructure context
                    times_skipped=3, times_active=0),
        _make_guard("guard-C",
                    "Pre-completion: re-read the goal verification.",
                    times_skipped=4, times_active=0,
                    category="reasoning-discipline"),
    ])
    _gc.GUARD_PATH = p

    matched = _gc._check_store(
        p, "guardrail",
        context="infrastructure", outcome="failed", phase="post-execution",
        dry_run=False,
    )
    matched_ids = {m["id"] for m in matched}

    after = _read(p)
    by_id = {r["id"]: r for r in after}

    # Anything that did NOT match must have skip and active unchanged.
    for gid in ("guard-A", "guard-B", "guard-C"):
        u = by_id[gid]["utilization"]
        if gid in matched_ids:
            assert u["times_active"] == 1, \
                f"{gid} matched, expected times_active 0→1, got {u['times_active']}"
        else:
            assert u["times_active"] == 0, f"{gid} not matched, times_active should stay 0"
        # Skip is invariant — never incremented for any record post-fix.
        original_skips = {"guard-A": 2, "guard-B": 3, "guard-C": 4}
        assert u["times_skipped"] == original_skips[gid], \
            f"{gid} times_skipped changed from {original_skips[gid]} to {u['times_skipped']}"


def test_dry_run_does_not_write():
    """Pre-existing dry-run behavior preserved — no counter changes in
    dry-run mode."""
    p = _seed([
        _make_guard("guard-D",
                    "After any infrastructure interaction, check alerts.",
                    times_skipped=7, times_active=2),
    ])
    _gc.GUARD_PATH = p

    _gc._check_store(
        p, "guardrail",
        context="infrastructure", outcome="any", phase="post-execution",
        dry_run=True,
    )

    after = _read(p)
    util = after[0]["utilization"]
    assert util["times_active"] == 2, "dry-run should not modify times_active"
    assert util["times_skipped"] == 7, "dry-run should not modify times_skipped"


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e) or "<no message>"))
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
