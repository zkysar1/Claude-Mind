"""test_infer_fallback_active.py — C.2 fallback-active backstop coverage.

Verifies that infer_feedback's third-stage backstop promotes a supplementary
item from `unknown` → `helpful` when its current times_active counter has
incremented since the times_active_at_retrieve baseline captured by retrieve.py.

Setup:
  * tmpdir MIND_WORLD/MIND_AGENT_DIR
  * world/guardrails.jsonl seeded with two guards:
      guard-active   — times_active=5 currently  (delta 5 vs baseline 0)
      guard-quiet    — times_active=0 currently  (delta 0 vs baseline 0)
  * synthetic retrieval-session.json with both guards in supplementary_detail,
    distinctive_tokens deliberately set to bogus values so token classification
    routes BOTH to the `noise` bucket (zero structural matches). Since
    g-115-3144 `unknown` means "SOME structural evidence but below threshold"
    and is unreachable at the default min_distinctive=1 — the point of the
    fixture is that classification finds NO evidence, so the backstop is the
    only thing that can rescue guard-active.
  * synthetic goal record (so _fetch_goal_text returns prose)

Assertion: after infer_feedback runs:
  * guard-active is in `helpful` (active_promotions == 1) — rescued from
    `noise` purely by its times_active delta, which is what this suite exists
    to pin: the backstop discards from `noise` AND `unknown`, so it does not
    care which bucket token classification chose.
  * guard-quiet stays out of `helpful` (delta 0)
  * stats.active_promotions reflects the count
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _seed_session(agent_dir: Path):
    """Write retrieval-session.json with two guards, both in unknown."""
    session = {
        # Must be >= the version infer_feedback accepts (3 since ,
        # which refuses older sessions because their distinctive_tokens were
        # split on -/_ and prose-dominated). The version is incidental
        # scaffolding here — this suite's subject is the C.2 times_active
        # backstop, which is independent of token vintage. Bump alongside any
        # future token-contract change rather than pinning an old version.
        "schema_version": 3,
        "goal_id": "g-test-c2-001",
        "timestamp": "2026-05-09T12:00:00",
        "categories": ["test"],
        "tree_nodes_loaded": [],
        "tree_nodes_detail": [],
        "supplementary_items": [
            {"id": "guard-active", "type": "guardrail"},
            {"id": "guard-quiet", "type": "guardrail"},
        ],
        "supplementary_detail": [
            {
                "id": "guard-active",
                "type": "guardrail",
                "summary": "active guard",
                "distinctive_tokens": ["zzzqqq", "wwwxxx"],  # won't match
                "times_active_at_retrieve": 0,
            },
            {
                "id": "guard-quiet",
                "type": "guardrail",
                "summary": "quiet guard",
                "distinctive_tokens": ["zzzqqq", "wwwxxx"],
                "times_active_at_retrieve": 0,
            },
        ],
        "utilization_pending": True,
    }
    sess_dir = agent_dir / "session"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "retrieval-session.json").write_text(
        json.dumps(session), encoding="utf-8"
    )


def _seed_world(world_dir: Path):
    """Write guardrails.jsonl with one guard whose times_active>0."""
    world_dir.mkdir(parents=True, exist_ok=True)
    guards = [
        {
            "id": "guard-active",
            "rule": "test", "category": "test",
            "trigger_condition": "test", "source": "test",
            "status": "active", "created": "2026-01-01",
            "utilization": {
                "retrieval_count": 0, "times_helpful": 0,
                "times_inferred_helpful": 0, "times_active": 5,  # <-- delta
                "times_cited": 0, "times_skipped": 0, "times_noise": 0,
                "utilization_score": 0, "last_retrieved": "",
            },
        },
        {
            "id": "guard-quiet",
            "rule": "test", "category": "test",
            "trigger_condition": "test", "source": "test",
            "status": "active", "created": "2026-01-01",
            "utilization": {
                "retrieval_count": 0, "times_helpful": 0,
                "times_inferred_helpful": 0, "times_active": 0,
                "times_cited": 0, "times_skipped": 0, "times_noise": 0,
                "utilization_score": 0, "last_retrieved": "",
            },
        },
    ]
    with open(world_dir / "guardrails.jsonl", "w", encoding="utf-8") as f:
        for g in guards:
            f.write(json.dumps(g) + "\n")
    # empty rb so _live_times_active_index doesn't fail
    (world_dir / "reasoning-bank.jsonl").write_text("", encoding="utf-8")
    # empty aspirations.jsonl so _fetch_goal_text returns gracefully
    (world_dir / "aspirations.jsonl").write_text("", encoding="utf-8")


def _seed_agent(agent_dir: Path):
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    sess = agent_dir / "session"
    sess.mkdir(exist_ok=True)
    (sess / "execution-diary.jsonl").write_text("", encoding="utf-8")


def test_fallback_active_promotes_unknown_to_helpful():
    # Snapshot env-vars and sys.modules entries this test mutates so we can
    # restore them on exit. Without this, MIND_WORLD / MIND_AGENT_DIR /
    # MIND_AGENT leak into later tests' os.environ.copy() — most visibly
    # to subprocess-spawning tests like test_stale_sentinel_canary.py, whose
    # canary subprocess then resolves AGENT_DIR to a now-deleted tmp and
    # produces a `sentinels` dict missing the test fixture's slot
    # (KeyError 'force_tree_maintain'). Canonical incident: 
    # (zeta investigation 2026-05-18) — bisected 18 full-suite failures to
    # this test's missing teardown. (Cat D-2 of .)
    _env_snapshot = {
        "MIND_WORLD": os.environ.get("MIND_WORLD"),
        "MIND_AGENT_DIR": os.environ.get("MIND_AGENT_DIR"),
        "MIND_AGENT": os.environ.get("MIND_AGENT"),
    }
    # `retrieve` joined this list in : utilization_feedback now
    # imports it (for the shared tokenizer + structural test) at classify time,
    # so re-exec'ing utilization_feedback under the tmp MIND_WORLD drags
    # `retrieve` into sys.modules bound to a tmp dir that is deleted on exit.
    # Restoring utilization_feedback without restoring retrieve leaves the
    # poisoned module behind for every later test — the same leak class the
    # env snapshot above was added for.
    _mod_snapshot = {
        name: sys.modules.get(name)
        for name in ("_paths", "utilization_feedback", "retrieve")
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            world = tmp / "world"
            agent = tmp / "alpha"
            _seed_world(world)
            _seed_agent(agent)
            _seed_session(agent)

            # Point _paths at our tmp dirs by env. Re-import utilization-feedback
            # in a clean module namespace so it picks up the env-resolved paths.
            os.environ["MIND_WORLD"] = str(world)
            os.environ["MIND_AGENT_DIR"] = str(agent)
            os.environ.pop("MIND_AGENT", None)

            # Force-reload _paths and utilization_feedback in the test process.
            for mod in ("_paths", "utilization_feedback"):
                sys.modules.pop(mod, None)

            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "utilization_feedback",
                str(CORE_SCRIPTS / "utilization-feedback.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            with open(agent / "session" / "retrieval-session.json") as f:
                session = json.load(f)

            result = mod.infer_feedback(session, confidence="balanced")
            assert result is not None, "infer returned None on schema_version=3 session"
            helpful, noise, unknown, stats = result

            # Balanced confidence + 0 token matches puts both items in `noise`
            # initially. The fallback-active backstop must rescue guard-active
            # (delta=5) and leave guard-quiet (delta=0) where token classification
            # placed it.
            assert "guard-active" in helpful, (
                f"fallback-active failed to promote guard-active: "
                f"helpful={helpful} noise={noise} unknown={unknown}"
            )
            assert "guard-quiet" in noise or "guard-quiet" in unknown, (
                f"guard-quiet should NOT be helpful (delta=0); "
                f"helpful={helpful} noise={noise} unknown={unknown}"
            )
            assert "guard-quiet" not in helpful, (
                "fallback-active wrongly promoted guard-quiet (delta=0)"
            )
            assert stats["active_promotions"] == 1, (
                f"expected 1 active promotion, got {stats['active_promotions']}"
            )
    finally:
        # Restore env-vars to their pre-test state.
        for name, prev in _env_snapshot.items():
            if prev is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prev
        # Restore sys.modules so the conftest's first-import-locks-AGENT_DIR
        # pattern (see core/scripts/tests/conftest.py) isn't broken for the
        # remainder of the session. The popped modules must come back so
        # subsequent tests that import them see the conftest-locked AGENT_DIR.
        for name, prev_mod in _mod_snapshot.items():
            if prev_mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev_mod


if __name__ == "__main__":
    test_fallback_active_promotes_unknown_to_helpful()
    print("PASS: fallback-active backstop promotes unknown→helpful on times_active delta")
