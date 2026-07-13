"""test_seed_engine_store_preserve.py — in-repo world/meta store protection.

Background (2026-07-07 ZDS .mind-data wipe): the v2.3.1 seed-transplant's
orphan-removal sweep (mirror semantics) walked the destination repo and
unlinked the ENTIRE world+meta store, because the static preserve lists
(_ORPHAN_SCAN_SKIP_TOP: agents/world/meta legacy names) did not know the
in-repo own-cloud store name (.mind-data/, adopted 2026-06-30 when the
external OneDrive location was retired). Restored from a dormant backup;
loss window ~1 week.

The fix is two-layer:
  1. STATIC: ".mind-data" added to _ORPHAN_SCAN_SKIP_TOP (belt).
  2. DYNAMIC: _in_repo_store_tops(dest_root) resolves every destination
     agents/*/local-paths.conf WORLD_PATH/META_PATH and preserves any root
     that lives inside dest_root — so a renamed or novel in-repo store is
     protected with no code change (suspenders).

These tests pin both layers for do_remove_orphans AND do_clean_cruft
(cruft protection is UNCONDITIONAL — not gated on --living-prod).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

ENGINE_PATH = CORE_SCRIPTS / "_seed_engine.py"
_spec = importlib.util.spec_from_file_location("_seed_engine_store_t", ENGINE_PATH)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)


MANIFEST = {"include": [{"path": "core/keep.py", "type": "file"}]}


def _mk_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "core").mkdir(parents=True)
    (src / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    return src


def _mk_dest(tmp_path: Path, store_top: str = ".custom-store",
             write_conf: bool = True) -> Path:
    """Destination with an in-repo store under a NON-static name.

    `.custom-store` deliberately does NOT appear in _ORPHAN_SCAN_SKIP_TOP —
    only the dynamic conf resolution can protect it, which is the point.
    """
    dest = tmp_path / "dest"
    (dest / "agents" / "omni").mkdir(parents=True)
    (dest / "core").mkdir(parents=True)
    (dest / "core" / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")

    world = dest / store_top / "world"
    meta = dest / store_top / "meta"
    (world / "knowledge").mkdir(parents=True)
    meta.mkdir(parents=True)
    (world / "aspirations.jsonl").write_text('{"id": "asp-001"}\n', encoding="utf-8")
    (world / "knowledge" / "node.md").write_text("# node\n", encoding="utf-8")
    (meta / "strategy.yaml").write_text("x: 1\n", encoding="utf-8")

    if write_conf:
        conf = dest / "agents" / "omni" / "local-paths.conf"
        conf.write_text(
            "# external paths\n"
            f"WORLD_PATH={dest.as_posix()}/{store_top}/world\n"
            f"META_PATH={dest.as_posix()}/{store_top}/meta\n",
            encoding="utf-8",
        )
    return dest


# ============================================================================
# _in_repo_store_tops resolution
# ============================================================================

def test_in_repo_store_tops_resolves_conf(tmp_path):
    dest = _mk_dest(tmp_path)
    assert _engine._in_repo_store_tops(dest) == {".custom-store"}


def test_in_repo_store_tops_ignores_external_paths(tmp_path):
    dest = _mk_dest(tmp_path, write_conf=False)
    external = tmp_path / "elsewhere"
    (external / "world").mkdir(parents=True)
    conf = dest / "agents" / "omni" / "local-paths.conf"
    conf.write_text(
        f"WORLD_PATH={external.as_posix()}/world\n"
        f"META_PATH={external.as_posix()}/meta\n",
        encoding="utf-8",
    )
    assert _engine._in_repo_store_tops(dest) == set()


def test_in_repo_store_tops_empty_without_agents_dir(tmp_path):
    dest = tmp_path / "fresh"
    dest.mkdir()
    assert _engine._in_repo_store_tops(dest) == set()


# ============================================================================
# do_remove_orphans — dynamic + static protection
# ============================================================================

def test_orphan_removal_preserves_dynamic_store(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_dest(tmp_path)
    (dest / "stale.py").write_text("ORPHAN = 1\n", encoding="utf-8")

    result = _engine.do_remove_orphans(dest, MANIFEST, src)

    # The genuinely-orphaned file is removed…
    assert "stale.py" in result["removed"]
    assert not (dest / "stale.py").exists()
    # …but the in-repo store (custom name, dynamic-only protection) survives.
    assert (dest / ".custom-store" / "world" / "aspirations.jsonl").exists()
    assert (dest / ".custom-store" / "world" / "knowledge" / "node.md").exists()
    assert (dest / ".custom-store" / "meta" / "strategy.yaml").exists()
    assert not any(r.startswith(".custom-store/") for r in result["removed"])


def test_orphan_removal_preserves_static_mind_data_without_conf(tmp_path):
    """Belt check: .mind-data survives even with NO local-paths.conf."""
    src = _mk_source(tmp_path)
    dest = _mk_dest(tmp_path, store_top=".mind-data", write_conf=False)

    result = _engine.do_remove_orphans(dest, MANIFEST, src)

    assert (dest / ".mind-data" / "world" / "aspirations.jsonl").exists()
    assert not any(r.startswith(".mind-data/") for r in result["removed"])


def test_orphan_removal_dry_run_reports_store_as_preserved(tmp_path):
    src = _mk_source(tmp_path)
    dest = _mk_dest(tmp_path)
    result = _engine.do_remove_orphans(dest, MANIFEST, src, dry_run=True)
    assert not any(r.startswith(".custom-store/") for r in result["removed"])
    # Store files still on disk (dry run removes nothing regardless).
    assert (dest / ".custom-store" / "world" / "aspirations.jsonl").exists()


# ============================================================================
# do_clean_cruft — UNCONDITIONAL store protection
# ============================================================================

def test_clean_cruft_never_deletes_store_even_without_living_prod(tmp_path):
    dest = _mk_dest(tmp_path)
    (dest / "stale-dir").mkdir()
    (dest / "stale-dir" / "junk.txt").write_text("x\n", encoding="utf-8")

    manifest = {"cruft_patterns": ["stale-dir/", ".custom-store/"]}
    result = _engine.do_clean_cruft(dest, manifest,
                                    preserve_deployment_local=False)

    # Real cruft removed…
    assert not (dest / "stale-dir").exists()
    # …store dir named directly by a cruft pattern is skipped + reported.
    assert (dest / ".custom-store" / "world" / "aspirations.jsonl").exists()
    assert ".custom-store/" in result["skipped_preserved"]


def test_clean_cruft_glob_pattern_cannot_reach_store(tmp_path):
    dest = _mk_dest(tmp_path)
    (dest / ".custom-store" / "world" / "junk.tmp").write_text("x\n",
                                                               encoding="utf-8")
    (dest / "top.tmp").write_text("x\n", encoding="utf-8")

    manifest = {"cruft_patterns": ["**/*.tmp"]}
    _engine.do_clean_cruft(dest, manifest, preserve_deployment_local=False)

    assert not (dest / "top.tmp").exists()
    assert (dest / ".custom-store" / "world" / "junk.tmp").exists()
