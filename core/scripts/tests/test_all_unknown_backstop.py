"""test_all_unknown_backstop.py — regression test for the --all-unknown
backstop added 2026-05-07.

Pre-fix, utilization-gate.sh fell back to --all-noise when the LLM skipped
Phase 4.26. --all-noise increments times_noise on every retrieved item,
which over many iterations falsely pushed unattested-but-relevant nodes
toward distill/prune candidacy (tree.py:540-588 has_feedback gate consumes
times_noise).

The new --all-unknown mode is a no-op on counters: it just records
utilization_method=all_unknown and clears utilization_pending, so:
  1. The retrieval-session is no longer pending (no infinite-loop hook fire).
  2. No times_noise pollution.
  3. phase-4-26-gate STILL blocks goal completion (same as --all-noise) —
     forcing the LLM to either run --infer/--helpful or pass
     --no-retrieval-applicable.

This test:
  1. Seeds a fake retrieval-session.json with two tree_nodes_loaded.
  2. Seeds a temp _tree.yaml with corresponding nodes (rc>0, all counters=0).
  3. Runs utilization-feedback.py --all-unknown.
  4. Asserts: utilization_pending=false, utilization_method='all_unknown',
     no counter changes on tree nodes.
  5. Runs phase-4-26-gate.py and asserts the verdict is BLOCK with the
     all_unknown reason.

Self-contained: never touches the live world directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority and the WSL-bash failure mode it works around. Module-level
# BASH is computed once at helper-module import time.
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


def _seed_tree(world_dir: Path) -> None:
    tree_dir = world_dir / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-05-07",
        "tree_growth_log": [],
        "nodes": {
            "root": {"file": None, "depth": 0, "parent": None,
                     "children": ["node-alpha", "node-bravo"], "child_count": 2,
                     "summary": "root"},
            "node-alpha": {
                "file": "world/knowledge/tree/node-alpha.md",
                "depth": 1, "parent": "root", "children": [], "child_count": 0,
                "summary": "alpha node",
                "retrieval_count": 5, "times_helpful": 0,
                "times_inferred_helpful": 0, "times_noise": 0,
                "utility_ratio": 0.0,
            },
            "node-bravo": {
                "file": "world/knowledge/tree/node-bravo.md",
                "depth": 1, "parent": "root", "children": [], "child_count": 0,
                "summary": "bravo node",
                "retrieval_count": 5, "times_helpful": 0,
                "times_inferred_helpful": 0, "times_noise": 0,
                "utility_ratio": 0.0,
            },
        },
    }
    with open(tree_dir / "_tree.yaml", "w", encoding="utf-8") as f:
        yaml.dump(tree, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _seed_session(agent_dir: Path, goal_id: str) -> None:
    sess_dir = agent_dir / "session"
    sess_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "schema_version": 2,
        "goal_id": goal_id,
        "retrieval_performed": True,
        "tree_nodes_loaded": ["node-alpha", "node-bravo"],
        "tree_nodes_detail": [
            {"key": "node-alpha", "distinctive_tokens": ["alpha"]},
            {"key": "node-bravo", "distinctive_tokens": ["bravo"]},
        ],
        "supplementary_items": [],
        "supplementary_detail": [],
        "utilization_pending": True,
    }
    with open(sess_dir / "retrieval-session.json", "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)


def _seed_paths_conf(repo_root: Path, world_dir: Path, meta_dir: Path) -> Path:
    # Post-relocation (AGENTS_PARENT_DIR="agents") the resolver looks under
    # agents/<name>/ — seeding at PROJECT_ROOT/<name> made _paths fall
    # through to first-available conf, so utilization-feedback wrote its
    # session state under the WRONG agent dir while this test asserted its
    # own tmp path ( polluter warning observed live; ).
    agent_dir = repo_root / "agents" / "test-all-unknown-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    conf_text = (
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={meta_dir.as_posix()}\n"
    )
    (agent_dir / "local-paths.conf").write_text(conf_text, encoding="utf-8")
    return agent_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="all-unknown-test-"))
    world_dir = tmp / "world"
    meta_dir = tmp / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    _seed_tree(world_dir)

    agent_dir = None
    try:
        agent_dir = _seed_paths_conf(PROJECT_ROOT, world_dir, meta_dir)
        goal_id = "g-test-unknown-001"
        _seed_session(agent_dir, goal_id)

        env = os.environ.copy()
        env["MIND_AGENT"] = agent_dir.name
        env["MIND_WORLD"] = world_dir.as_posix()
        env["MIND_META"] = meta_dir.as_posix()
        # main()-style file runs OUTSIDE pytest — no conftest autouse pin
        # (): pin the backend so nothing routes to own-cloud S3
        # keys (guard-955;  class-A repair).
        env["STORAGE_BACKEND"] = "local"

        # Step 1: run --all-unknown
        feedback_rel = (CORE_SCRIPTS / "utilization-feedback.sh").relative_to(PROJECT_ROOT)
        result = subprocess.run(
            [BASH, feedback_rel.as_posix(),
             "--goal", goal_id, "--all-unknown"],
            capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
            timeout=30,
        )
        if result.stderr:
            sys.stderr.write(f"[utilization-feedback stderr]\n{result.stderr}\n")
        if result.returncode != 0:
            print(f"FAIL: utilization-feedback exited rc={result.returncode}",
                  file=sys.stderr)
            print(f"stdout={result.stdout}", file=sys.stderr)
            return 1

        # Step 2: verify session state
        sess_path = agent_dir / "session" / "retrieval-session.json"
        with open(sess_path, "r", encoding="utf-8") as f:
            session = json.load(f)

        if session.get("utilization_pending") is not False:
            print(f"FAIL: utilization_pending should be False, got {session.get('utilization_pending')}",
                  file=sys.stderr)
            return 1
        if session.get("utilization_method") != "all_unknown":
            print(f"FAIL: utilization_method should be 'all_unknown', "
                  f"got {session.get('utilization_method')}", file=sys.stderr)
            return 1

        # Step 3: verify tree state — counters MUST NOT have moved
        with open(world_dir / "knowledge" / "tree" / "_tree.yaml", "r",
                  encoding="utf-8") as f:
            tree = yaml.safe_load(f)
        for k in ("node-alpha", "node-bravo"):
            n = tree["nodes"][k]
            for field in ("times_helpful", "times_inferred_helpful", "times_noise"):
                if n.get(field, 0) != 0:
                    print(f"FAIL: {k}.{field} expected 0 (no-op semantics), "
                          f"got {n.get(field)}", file=sys.stderr)
                    return 1
            if n.get("utility_ratio", 0.0) != 0.0:
                print(f"FAIL: {k}.utility_ratio should still be 0.0, "
                      f"got {n.get('utility_ratio')}", file=sys.stderr)
                return 1

        # Step 4: phase-4-26-gate must still BLOCK
        gate_path = CORE_SCRIPTS / "phase-4-26-gate.py"
        result = subprocess.run(
            [sys.executable, str(gate_path), "--goal", goal_id],
            capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
            timeout=10,
        )
        # Expected: rc=1 (block), stdout has verdict='block' and method='all_unknown'
        if result.returncode != 1:
            print(f"FAIL: phase-4-26-gate should block (rc=1) on all_unknown, "
                  f"got rc={result.returncode}", file=sys.stderr)
            print(f"stdout={result.stdout}", file=sys.stderr)
            print(f"stderr={result.stderr}", file=sys.stderr)
            return 1
        try:
            verdict = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"FAIL: phase-4-26-gate stdout not JSON: {result.stdout}",
                  file=sys.stderr)
            return 1
        if verdict.get("verdict") != "block":
            print(f"FAIL: gate verdict should be 'block', got {verdict.get('verdict')}",
                  file=sys.stderr)
            return 1
        if verdict.get("method") != "all_unknown":
            print(f"FAIL: gate method should be 'all_unknown', got {verdict.get('method')}",
                  file=sys.stderr)
            return 1
        if "all_unknown" not in (verdict.get("reason") or ""):
            print(f"FAIL: gate reason should mention 'all_unknown', "
                  f"got {verdict.get('reason')}", file=sys.stderr)
            return 1

        print("PASS: --all-unknown is no-op on counters; "
              "utilization_method='all_unknown'; "
              "phase-4-26-gate still blocks goal completion.")
        return 0

    finally:
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
