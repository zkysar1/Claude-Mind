"""test_migrate_to_mind_data.py -  durable regression guard for
core/scripts/migrate-to-mind-data.sh (asp-330 M4 / g-330-04).

Replaces the throwaway isolated test (8/8 mechanics + real _paths._resolve_tier)
with a PERMANENT guard. The migration script is destructive against its own
PROJECT_ROOT (it renames every agents/*/local-paths.conf -> .bak and copies the
external world/meta into .mind-data/), so every case here runs the REAL script
against a SYNTHETIC temp PROJECT_ROOT built from a copy of the script -- never
the live repo, world dir, or .mind-data.

Because the script derives PROJECT_ROOT from its own location
(`SCRIPT_DIR/../..`), dropping a copy at <tmp>/core/scripts/ makes it operate
entirely inside <tmp>. MIND_AGENT selects the bound conf; MIND_WORLD/META and
STORAGE_BACKEND are scrubbed so neither the script's conf read nor the later
_resolve_tier check is short-circuited by an inherited env tier.

Coverage:
  - dry-run makes no changes
  - core mechanics: world+meta -> .mind-data/{world,meta} (content intact),
    .env.local STORAGE_BACKEND=local marker, local-paths.conf -> .bak
  - real _paths._resolve_tier resolves world/meta from the migrated layout
  - --agent-dirs copies agents/ -> .mind-data/agents
  - --no-backup keeps the conf (no .bak)
  - missing WORLD_PATH source dir errors (exit 1)
  - cp -r over a deep nested tree (exercise) + the Windows >260-char MAX_PATH
    residual that _transplant_pack._win_long covers for the transplant path
    (documented; migrate-to-mind-data uses plain cp -r, so very long paths
    remain a known residual rather than a guaranteed-handled case)

Live-daemon safe (guard-672): pure filesystem + bash subprocess against a tmp
dir; never touches the live daemon or any real data root.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _bash_helpers import BASH  # noqa: E402

REAL_SCRIPT = CORE_SCRIPTS / "migrate-to-mind-data.sh"
AGENT = "tester"


def _build_root(tmp: Path, *, world_files: dict, meta_files: dict) -> tuple:
    """Construct a synthetic PROJECT_ROOT and external world/meta sources.

    Returns (root, world_src, meta_src). `root` carries a copy of the migration
    script under core/scripts/, an agents/<AGENT>/local-paths.conf pointing at
    the synthetic sources, and an agent marker file (for the --agent-dirs case).
    """
    root = tmp / "proj"
    (root / "core" / "scripts").mkdir(parents=True)
    shutil.copy2(REAL_SCRIPT, root / "core" / "scripts" / "migrate-to-mind-data.sh")

    world_src = tmp / "ext_world"
    meta_src = tmp / "ext_meta"
    world_src.mkdir()
    meta_src.mkdir()
    for rel, content in world_files.items():
        p = world_src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel, content in meta_files.items():
        p = meta_src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    agent_dir = root / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    # Forward-slash paths match the real local-paths.conf format and are what
    # git-bash `[ -d "$WORLD_SRC" ]` expects on Windows.
    (agent_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={world_src.as_posix()}\nMETA_PATH={meta_src.as_posix()}\n",
        encoding="utf-8",
    )
    (agent_dir / "self.md").write_text("AGENT-MARKER", encoding="utf-8")
    return root, world_src, meta_src


def _run(root: Path, *flags: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MIND_AGENT"] = AGENT
    # Scrub env tiers that would short-circuit the conf read / _resolve_tier.
    for k in ("MIND_WORLD", "MIND_META", "STORAGE_BACKEND"):
        env.pop(k, None)
    script = str(root / "core" / "scripts" / "migrate-to-mind-data.sh")
    return subprocess.run(
        [BASH, script, *flags],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _basic_files() -> tuple:
    return (
        {"knowledge/tree/_tree.yaml": "nodes: {}\n", "program.md": "WORLD-MARKER"},
        {"reflection-strategy.yaml": "META-MARKER\n"},
    )


def test_dry_run_makes_no_changes():
    with tempfile.TemporaryDirectory(prefix="m2md-dry-") as tmpd:
        wf, mf = _basic_files()
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root, "--dry-run")
        assert r.returncode == 0, r.stderr
        assert not (root / ".mind-data").exists(), "dry-run must not create .mind-data"
        # conf untouched, no .bak
        assert (root / "agents" / AGENT / "local-paths.conf").exists()
        assert not (root / "agents" / AGENT / "local-paths.conf.bak").exists()


def test_core_mechanics():
    with tempfile.TemporaryDirectory(prefix="m2md-core-") as tmpd:
        wf, mf = _basic_files()
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root)
        assert r.returncode == 0, r.stderr
        md = root / ".mind-data"
        # world + meta copied with content intact
        assert (md / "world" / "program.md").read_text(encoding="utf-8") == "WORLD-MARKER"
        assert (md / "world" / "knowledge" / "tree" / "_tree.yaml").exists()
        assert (md / "meta" / "reflection-strategy.yaml").read_text(encoding="utf-8") == "META-MARKER\n"
        # .env.local STORAGE_BACKEND=local marker
        env_local = (md / ".env.local").read_text(encoding="utf-8")
        assert "STORAGE_BACKEND=local" in env_local
        # local-paths.conf backed up -> .bak (default), original removed
        assert (root / "agents" / AGENT / "local-paths.conf.bak").exists()
        assert not (root / "agents" / AGENT / "local-paths.conf").exists()


def test_resolve_tier_after_migration():
    """The whole point of the migration: _paths._resolve_tier must resolve
    world/meta to .mind-data/{world,meta} from the bare-default tier once the
    migrated .env.local (STORAGE_BACKEND only, no *_PATH override) exists."""
    import _paths  # imported lazily so a _paths import error is a clear test failure

    with tempfile.TemporaryDirectory(prefix="m2md-resolve-") as tmpd:
        wf, mf = _basic_files()
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root)
        assert r.returncode == 0, r.stderr
        md = root / ".mind-data"
        mind_env = _paths._parse_conf(md / ".env.local")
        # The migrated env.local carries the backend marker but NO *_PATH override,
        # so resolution falls through to the .mind-data/{world,meta} bare default.
        assert "WORLD_PATH" not in mind_env and "META_PATH" not in mind_env

        # MIND_WORLD/META must be absent in this process or the env tier wins.
        saved = {k: os.environ.pop(k, None) for k in ("MIND_WORLD", "MIND_META")}
        try:
            w = _paths._resolve_tier(
                "MIND_WORLD", "WORLD_PATH",
                mind_data_dir=md, mind_data_env=mind_env, local_conf={},
            )
            m = _paths._resolve_tier(
                "MIND_META", "META_PATH",
                mind_data_dir=md, mind_data_env=mind_env, local_conf={},
            )
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        assert w == md / "world", f"world resolved to {w}, expected {md / 'world'}"
        assert m == md / "meta", f"meta resolved to {m}, expected {md / 'meta'}"


def test_agent_dirs_flag():
    with tempfile.TemporaryDirectory(prefix="m2md-agentdirs-") as tmpd:
        wf, mf = _basic_files()
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root, "--agent-dirs")
        assert r.returncode == 0, r.stderr
        # agents/ copied into .mind-data/agents/ (the agent marker file survives)
        copied = root / ".mind-data" / "agents" / AGENT / "self.md"
        assert copied.exists(), f"--agent-dirs did not copy agents/ ({copied} missing)"
        assert copied.read_text(encoding="utf-8") == "AGENT-MARKER"


def test_no_backup_flag_keeps_conf():
    with tempfile.TemporaryDirectory(prefix="m2md-nobackup-") as tmpd:
        wf, mf = _basic_files()
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root, "--no-backup")
        assert r.returncode == 0, r.stderr
        # conf kept, no .bak written
        assert (root / "agents" / AGENT / "local-paths.conf").exists()
        assert not (root / "agents" / AGENT / "local-paths.conf.bak").exists()
        # .mind-data still written (the migration otherwise proceeds)
        assert (root / ".mind-data" / "world" / "program.md").exists()


def test_missing_world_source_errors():
    with tempfile.TemporaryDirectory(prefix="m2md-missing-") as tmpd:
        wf, mf = _basic_files()
        root, world_src, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        # Remove the world source AFTER the conf points at it -> the script's
        # `[ ! -d "$WORLD_SRC" ]` guard must fire (exit 1, no .mind-data).
        shutil.rmtree(world_src)
        r = _run(root)
        assert r.returncode == 1, f"expected exit 1 on missing world src, got {r.returncode}: {r.stdout}"
        assert "WORLD_PATH source dir missing" in (r.stdout + r.stderr)
        assert not (root / ".mind-data").exists()


def test_cp_deep_tree_copied():
    """Exercise the copy engine over a deep nested tree (rsync -a OR the cp -r
    fallback, whichever this git-bash provides).

    DOCUMENTED RESIDUAL (Windows MAX_PATH): migrate-to-mind-data.sh copies with
    plain `cp -r` when rsync is absent, which has NO >260-char extended-length
    handling. The transplant path solves the same hazard via
    `_transplant_pack._win_long` (prefixes paths with the `\\\\?\\` marker) -- the
    migration script does NOT, so a destination path exceeding MAX_PATH remains a
    known residual. We exercise an ordinary deep tree (well under MAX_PATH) here
    rather than forcing a >260-char path, which is platform-flaky and not what
    the script claims to guarantee."""
    deep_rel = "a/b/c/d/e/f/g/deep.txt"
    with tempfile.TemporaryDirectory(prefix="m2md-deep-") as tmpd:
        wf, mf = _basic_files()
        wf = dict(wf)
        wf[deep_rel] = "DEEP-MARKER"
        root, _, _ = _build_root(Path(tmpd), world_files=wf, meta_files=mf)
        r = _run(root)
        assert r.returncode == 0, r.stderr
        copied = root / ".mind-data" / "world" / deep_rel
        assert copied.exists(), f"deep tree not copied: {copied}"
        assert copied.read_text(encoding="utf-8") == "DEEP-MARKER"


if __name__ == "__main__":
    test_dry_run_makes_no_changes()
    test_core_mechanics()
    test_resolve_tier_after_migration()
    test_agent_dirs_flag()
    test_no_backup_flag_keeps_conf()
    test_missing_world_source_errors()
    test_cp_deep_tree_copied()
    print("PASS: g-115-1616 migrate-to-mind-data.sh durable regression guard")
