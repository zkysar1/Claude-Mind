"""test_journal_tree_cite_scan.py — regression test for the journal-append.sh
tree-node citation scan added 2026-05-07.

Pre-fix, journal-append.sh only scanned for rb-NNN / guard-NNN citations;
tree node references in the summary produced no helpful signal at all.
Across 655 leaves on alpha's tree at audit time, 0/655 carried
times_helpful>0 — every retrieval-bumped node sat at utility_ratio=0.

The scan extracts kebab-case tokens from --summary, validates them against
existing _tree.yaml node keys, and increments times_inferred_helpful (the
half-weight counter, mirroring --infer semantics — citation is a weaker
signal than explicit attestation).

Transport (g-115-2351 rewrite): the scan's write half is
`tree-update.sh --increment`, a DAEMON-ONLY wrapper. On a `.mind-data/` box
the LIVE daemon resolves EVERY agent to the live world before consulting
local-paths.conf (agent_paths._resolve_src tier order: env -> .mind-data ->
conf), so the old shape — seed a conf, talk to the live daemon — could
never sandbox the write (observed: node_not_found against the LIVE tree,
silenced by the scan's fail-open `>/dev/null || true`, tmp tih stayed 0;
the quarantined red). The repair drives an in-process DaemonFixture daemon
rooted in a tmp project (no .mind-data, MIND_WORLD pinned to the fixture
world) and points the journal-append.sh subprocess at it via RT_DIR.

Self-contained: never touches the live tree or the live daemon.
"""

from __future__ import annotations

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
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

# Resolve bash via shared helper (). See _bash_helpers.py for the
# canonical resolution priority and the WSL-bash failure mode it works around.
from _bash_helpers import BASH  # noqa: E402
from _daemon_fixture import DaemonFixture  # noqa: E402


def _seed_tree(world_dir: Path) -> None:
    """Write a minimal _tree.yaml with three valid nodes plus the root."""
    tree_dir = world_dir / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-05-07",
        "tree_growth_log": [],
        "nodes": {
            "root": {
                "file": None,
                "depth": 0,
                "parent": None,
                "children": ["test-domain"],
                "child_count": 1,
                "summary": "root",
            },
            "test-domain": {
                "file": "world/knowledge/tree/test-domain.md",
                "depth": 1,
                "parent": "root",
                "children": ["alpha-test-node", "bravo-test-node"],
                "child_count": 2,
                "summary": "test domain",
            },
            "alpha-test-node": {
                "file": "world/knowledge/tree/test-domain/alpha-test-node.md",
                "depth": 2,
                "parent": "test-domain",
                "children": [],
                "child_count": 0,
                "summary": "alpha test leaf",
                "retrieval_count": 5,
                "times_helpful": 0,
                "times_inferred_helpful": 0,
                "times_noise": 0,
                "utility_ratio": 0.0,
            },
            "bravo-test-node": {
                "file": "world/knowledge/tree/test-domain/bravo-test-node.md",
                "depth": 2,
                "parent": "test-domain",
                "children": [],
                "child_count": 0,
                "summary": "bravo test leaf",
                "retrieval_count": 5,
                "times_helpful": 0,
                "times_inferred_helpful": 0,
                "times_noise": 0,
                "utility_ratio": 0.0,
            },
        },
    }
    with open(tree_dir / "_tree.yaml", "w", encoding="utf-8") as f:
        yaml.dump(tree, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _read_tree(world_dir: Path) -> dict:
    with open(world_dir / "knowledge" / "tree" / "_tree.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _seed_real_repo_agent(repo_root: Path, world_dir: Path, meta_dir: Path) -> Path:
    """journal-append.sh writes the journal under the REAL repo's
    agents/<MIND_AGENT>/ (its _paths.sh agent_dir), so a real-repo test
    agent dir must exist. Post-relocation layout: under agents/ parent
    (g-115-960 polluter class; g-115-2351)."""
    agent_dir = repo_root / "agents" / "test-journal-cite-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    conf_text = (
        "# auto-generated test config — safe to delete\n"
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={meta_dir.as_posix()}\n"
    )
    (agent_dir / "local-paths.conf").write_text(conf_text, encoding="utf-8")
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    (agent_dir / "journal").mkdir(parents=True, exist_ok=True)
    return agent_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="journal-cite-test-"))
    world_dir = tmp / "world"
    meta_dir = tmp / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    _seed_tree(world_dir)

    agent_dir = None
    try:
        agent_dir = _seed_real_repo_agent(PROJECT_ROOT, world_dir, meta_dir)

        summary = (
            "Encoded findings to alpha-test-node — touched fail-open path. "
            "Investigated post-execution flow and pre-commit-hook race. "
            "Did not touch bravo-test-node this iteration."
        )

        with DaemonFixture(world_dir, agent="test-journal-cite-agent") as df:
            # os.environ now carries the fixture pins (__enter__): RT_DIR ->
            # fixture daemon.port, MIND_AGENT, STORAGE_BACKEND=local,
            # MIND_WORLD -> tmp world. The subprocess copy inherits them so
            # journal-append.sh's CLI half (scan against tmp _tree.yaml) and
            # its daemon half (tree-update.sh --increment via RT_DIR) both
            # land on the fixture world.
            env = os.environ.copy()
            env["MIND_META"] = meta_dir.as_posix()

            script_rel = (CORE_SCRIPTS / "journal-append.sh").relative_to(PROJECT_ROOT)
            result = subprocess.run(
                [
                    BASH,
                    script_rel.as_posix(),
                    "--goal", "g-test-001",
                    "--outcome-class", "deep",
                    "--summary", summary,
                ],
                capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
                timeout=60,
            )

        if result.stderr:
            sys.stderr.write(f"[journal-append stderr]\n{result.stderr}\n")

        if result.returncode != 0:
            print(f"FAIL: journal-append exited rc={result.returncode}", file=sys.stderr)
            return 1

        # Verify the citation scan landed in the FIXTURE world.
        tree = _read_tree(world_dir)
        nodes = tree["nodes"]
        alpha_tih = nodes["alpha-test-node"].get("times_inferred_helpful", 0)
        bravo_tih = nodes["bravo-test-node"].get("times_inferred_helpful", 0)
        alpha_ur = nodes["alpha-test-node"].get("utility_ratio", 0.0)

        if alpha_tih != 1:
            print(f"FAIL: alpha-test-node times_inferred_helpful expected 1, got {alpha_tih}",
                  file=sys.stderr)
            return 1
        # bravo was MENTIONED in the summary ("Did not touch bravo-test-node...")
        # — the scan does substring matching (it doesn't read intent). This is
        # acceptable: the half-weight signal composes with other signals;
        # a false positive means the node had SOME relevance to the goal.
        if bravo_tih != 1:
            print(f"FAIL: bravo-test-node times_inferred_helpful expected 1 "
                  f"(scan does substring match — 'Did not touch bravo-test-node' counts), "
                  f"got {bravo_tih}", file=sys.stderr)
            return 1

        # utility_ratio reflects the bump: (0 + 0.5*1) / 5 = 0.1. Tree nodes
        # deliberately KEEP the max(rc, 1) denominator ( changed
        # only rb/guardrails — tree th<=rc precondition holds).
        expected_ur = round((0 + 0.5 * 1) / 5, 4)
        if alpha_ur != expected_ur:
            print(f"FAIL: alpha-test-node utility_ratio expected {expected_ur}, got {alpha_ur}",
                  file=sys.stderr)
            return 1

        # Decoys ("fail-open", "post-execution", "pre-commit-hook") must NOT
        # appear as new nodes — the validator rejects non-key tokens.
        decoy_keys = {"fail-open", "post-execution", "pre-commit-hook"}
        intruders = decoy_keys & set(nodes.keys())
        if intruders:
            print(f"FAIL: decoy tokens leaked into tree as new nodes: {intruders}",
                  file=sys.stderr)
            return 1

        print(f"PASS: journal-append tree-cite scan bumped alpha-test-node "
              f"(tih=0→1, ur=0→{expected_ur}) through the fixture daemon; "
              f"decoys rejected; substring semantics confirmed.")
        return 0

    finally:
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
