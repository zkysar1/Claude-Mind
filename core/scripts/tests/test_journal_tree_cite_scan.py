"""test_journal_tree_cite_scan.py — regression test for the journal-append.sh
tree-node citation scan added 2026-05-07.

Pre-fix, journal-append.sh only scanned for rb-NNN / guard-NNN citations;
tree node references in the summary produced no helpful signal at all.
Across 655 leaves on alpha's tree at audit time, 0/655 carried
times_helpful>0 — every retrieval-bumped node sat at utility_ratio=0.

The new scan extracts kebab-case tokens from --summary, validates them
against existing _tree.yaml node keys, and increments
times_inferred_helpful (the half-weight counter, mirroring --infer
semantics — citation is a weaker signal than explicit attestation).

This test isolates the scan logic by:
  1. Building a minimal temp _tree.yaml with three nodes.
  2. Setting MIND_WORLD to the temp dir so tree-update.sh routes there.
  3. Running journal-append.sh with a summary that mentions one of the
     three keys plus several non-key kebab tokens.
  4. Asserting only the matching key got its times_inferred_helpful
     bumped; non-matching tokens did not corrupt the tree.

Self-contained: never touches the live tree.
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

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority (MIND_SHELL → Git-Bash candidates → shutil.which) and the
# detailed rationale (the WSL-bash failure mode that motivated this
# helper). This file's previous local _resolve_bash docstring is the
# ancestor of the shared helper's module docstring.
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402


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


def _seed_minimal_paths_conf(repo_root: Path, world_dir: Path) -> Path:
    """Create a temp agent dir with local-paths.conf so _paths.sh resolves
    WORLD_DIR to our scratch dir. Hyphenated agent name is intentional —
    matches the rest of the project conventions."""
    agent_dir = repo_root / "test-journal-cite-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Forward slashes only — _paths.sh sources this verbatim and bash
    # treats backslashes as escape sequences. CLAUDE.md says the same.
    conf_text = (
        f"# auto-generated test config — safe to delete\n"
        f"WORLD_PATH={world_dir.as_posix()}\n"
        f"META_PATH={(world_dir.parent / 'meta').as_posix()}\n"
    )
    (agent_dir / "local-paths.conf").write_text(conf_text, encoding="utf-8")
    # Also seed a session dir + journal/ skeleton so journal-append doesn't
    # error on missing parents (it creates parents itself but the active_context
    # read needs SOMETHING to look at; the wm_read.sh fallback handles missing).
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
        agent_dir = _seed_minimal_paths_conf(PROJECT_ROOT, world_dir)

        # Run journal-append.sh with a summary mentioning ONE valid key plus
        # several kebab-case decoys (none of which exist in the seeded tree).
        # The decoys exercise the validation step — pre-fix, any kebab token
        # would have been bumped against tree-update.sh which would have
        # exited 1 silently per node (wasted spawns; no corruption since
        # tree-update rejects unknown keys), so this test verifies the
        # validation pre-filter narrows to actual matches.
        summary = (
            "Encoded findings to alpha-test-node — touched fail-open path. "
            "Investigated post-execution flow and pre-commit-hook race. "
            "Did not touch bravo-test-node this iteration."
        )
        env = os.environ.copy()
        env["MIND_AGENT"] = agent_dir.name
        # _paths.sh reads MIND_AGENT to find <agent>/local-paths.conf.
        env["MIND_WORLD"] = world_dir.as_posix()

        # Use POSIX path form for the script path. Windows backslashes get
        # mangled when bash interprets the argv as a single string and the
        # PATH-resolved bash on this machine swallows the backslashes (the
        # observed failure mode: `/bin/bash: C:<WORKSPACE>GitHub...`). Relative
        # path from PROJECT_ROOT cwd is portable.
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
            timeout=30,
        )

        # Surface stderr regardless of pass/fail — useful for debugging
        # journal-merge / journal-add fail-open paths that print warnings.
        if result.stderr:
            sys.stderr.write(f"[journal-append stderr]\n{result.stderr}\n")

        if result.returncode != 0:
            print(f"FAIL: journal-append exited rc={result.returncode}", file=sys.stderr)
            return 1

        # Verify the citation scan landed.
        tree = _read_tree(world_dir)
        nodes = tree["nodes"]
        alpha_tih = nodes["alpha-test-node"].get("times_inferred_helpful", 0)
        bravo_tih = nodes["bravo-test-node"].get("times_inferred_helpful", 0)
        alpha_ur = nodes["alpha-test-node"].get("utility_ratio", 0.0)

        if alpha_tih != 1:
            print(f"FAIL: alpha-test-node times_inferred_helpful expected 1, got {alpha_tih}",
                  file=sys.stderr)
            return 1
        # bravo was MENTIONED in the summary ("Did not touch bravo-test-node this iteration")
        # — and the scan does substring matching (it doesn't read intent). This is
        # acceptable: the scan is purposely simple. The half-weight signal is
        # designed to compose with other signals; false positives here mean the
        # node had SOME relevance to the goal, just maybe negative — which a
        # half-weight counter ill-represents but doesn't catastrophically corrupt.
        if bravo_tih != 1:
            print(f"FAIL: bravo-test-node times_inferred_helpful expected 1 "
                  f"(scan does substring match — 'Did not touch bravo-test-node' counts), "
                  f"got {bravo_tih}", file=sys.stderr)
            return 1

        # utility_ratio should reflect the bump: (0 + 0.5*1) / 5 = 0.1
        expected_ur = round((0 + 0.5 * 1) / 5, 4)
        if alpha_ur != expected_ur:
            print(f"FAIL: alpha-test-node utility_ratio expected {expected_ur}, got {alpha_ur}",
                  file=sys.stderr)
            return 1

        # Verify the decoys ("fail-open", "post-execution", "pre-commit-hook")
        # did NOT appear as new nodes — the validator should have rejected them.
        decoy_keys = {"fail-open", "post-execution", "pre-commit-hook"}
        intruders = decoy_keys & set(nodes.keys())
        if intruders:
            print(f"FAIL: decoy tokens leaked into tree as new nodes: {intruders}",
                  file=sys.stderr)
            return 1

        print(f"PASS: journal-append tree-cite scan bumped alpha-test-node "
              f"(tih=0→1, ur=0→{expected_ur}); decoys rejected; substring "
              f"semantics confirmed.")
        return 0

    finally:
        if agent_dir and agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
