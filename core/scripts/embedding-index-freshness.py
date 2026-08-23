#!/usr/bin/env python3
"""embedding-index-freshness.py — per-box staleness tick for the retrieval
embedding index (g-306-84; wired into iteration-close.sh productivity-check
beside agent-watchdog --tick / monitor-tick).

Why a LOCAL tick and not a recurring goal: the index is PER-BOX state
(mind_api/state/retrieval-embedding-index/ — the daemon's cache), while
recurring goals are world-scoped and execute on exactly ONE box per firing.
A tick that runs wherever a loop runs keeps every box's own index fresh.

Behavior (all paths fail-open; this must never delay loop continuation):
  1. `embedding_blend_enabled` false (tree.yaml retrieval:) → exit 0 silent.
     The whole check costs one small YAML read while the feature is off.
  2. No index on disk → exit 0 silent. The INITIAL full build (~11-45 min
     of CPU embedding) is a deliberate operator/goal action, never spawned
     from a hook. Only incremental freshness (--update: re-embeds changed
     docs only, typically seconds) is automated.
  3. Index fresh (meta.json mtime >= newest source-store mtime) → exit 0.
  4. Stale + debounce clear → record the attempt marker, spawn
     `embedding-index-build.py --update` DETACHED (never waits), print one
     JSON status line. Debounce: at most one spawn per 6h per box — a
     persistently-failing update retries next window instead of storming.

The updater itself pins the index's existing model (g-306-82), so a config
model change can never be half-applied by this tick.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

#  watch-set roots that live under the REPO (the WORLD_DIR
# conventions root joins them at call time inside _source_mtime). Hoisted to a
# module constant so tests can monkeypatch them away: sweeping the real repo
# from inside the function made every consumer test time-dependent -- red for
# exactly 1h after ANY commit touching these roots (measured 2026-08-21,
# cc-13: a convention refreshed at 08:31 by a fleet merge vs a fixture index
# aged to 08:12 flipped the REFUSE-case test to would_spawn=True).
_FRAMEWORK_MD_ROOTS = (
    SCRIPT_DIR.parent.parent / ".claude" / "rules",
    SCRIPT_DIR.parent.parent / "core" / "config" / "conventions",
)
sys.path.insert(0, str(SCRIPT_DIR))

DEBOUNCE_SECONDS = 6 * 3600  # one spawn attempt per box per 6h window
INDEX_DIR = SCRIPT_DIR.parent.parent / "mind_api" / "state" / "retrieval-embedding-index"
UPDATE_LOG = SCRIPT_DIR.parent / "logs" / "embedding-index-update.log"


def _blend_enabled():
    try:
        import yaml
        cfg_path = SCRIPT_DIR.parent / "config" / "tree.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return bool((cfg.get("retrieval") or {}).get("embedding_blend_enabled",
                                                     False))
    except Exception:
        return False


def _source_mtime():
    """Newest mtime of the corpus source stores.

    The watch-set MUST mirror embedding-index-build.load_corpus, which indexes
    guardrails + reasoning-bank + KNOWLEDGE TREE NODES. It did not until
    g-115-3763: the tree was in the corpus but not here, so a tree-only
    encoding never marked the index stale and the new node stayed invisible to
    retrieve.sh until an unrelated rb/guardrail write happened to fire the
    tick. Measured (bravo, cc-05, 2026-07-28): 85 unindexed entries had
    accumulated, and three queries that should have matched a freshly-added
    node returned it in 0/15 results each — then at ranks 7/4/4 after a manual
    --update. The node content was fine; only the index was stale.

    BOTH tree surfaces are watched because the embedded text is
    humanized-key + _tree.yaml summary + the node .md's first body paragraph
    (build.tree_doc_text): a new node or a summary edit moves _tree.yaml, while
    a body edit moves only the .md. Watching either one alone leaves the other
    class of edit silently unindexed.

    The .md sweep is a stat-only rglob — 11.8ms over 1291 nodes measured on
    cc-04, against a tick that already runs once per iteration close. An
    over-trigger costs one incremental --update (seconds); an under-trigger
    costs invisibility, so the sweep deliberately does not filter out
    non-node .md files that may sit under the tree root."""
    try:
        from _paths import WORLD_DIR
    except Exception:
        return None
    newest = None
    for name in ("reasoning-bank.jsonl", "guardrails.jsonl"):
        p = Path(WORLD_DIR) / name
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    tree_root = Path(WORLD_DIR) / "knowledge" / "tree"
    try:
        m = (tree_root / "_tree.yaml").stat().st_mtime
        newest = m if newest is None else max(newest, m)
    except OSError:
        pass
    try:
        for p in tree_root.rglob("*.md"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            newest = m if newest is None else max(newest, m)
    except OSError:
        pass
    # : framework docs joined the corpus, so they join the watch-set
    # in the SAME change — this is the  decision rule applied
    # prospectively rather than after the fact. _FRAMEWORK_MD_ROOTS + the
    # WORLD_DIR conventions root here mirror
    # retrieve._framework_file_sources, which is the SSOT; keep them in step.
    #
    # Hardcoded rather than imported ON PURPOSE, and the asymmetry with
    # embedding-index-build.py (which calls R._build_framework_index directly)
    # is deliberate: the builder ALREADY imports retrieve, so sharing there is
    # free, while this module is a per-iteration-close hook that imports only
    # _paths lazily. Pulling all of retrieve.py in for three directory names
    # would put a large import on a tick that exists to be cheap. A missed
    # framework edit costs one stale-index window; the import costs every tick.
    for root in _FRAMEWORK_MD_ROOTS + (Path(WORLD_DIR) / "conventions",):
        try:
            for p in root.rglob("*.md"):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                newest = m if newest is None else max(newest, m)
        except OSError:
            continue
    return newest


def main():
    dry_run = os.environ.get("EMBED_FRESHNESS_DRYRUN") == "1"
    index_dir = Path(os.environ.get("EMBED_FRESHNESS_INDEX_DIR") or INDEX_DIR)
    meta = index_dir / "meta.json"

    if not _blend_enabled():
        return 0
    if not meta.exists():
        return 0  # initial build is deliberate, never hook-spawned

    src_m = _source_mtime()
    if src_m is None or src_m <= meta.stat().st_mtime:
        return 0  # fresh (or sources unreadable — fail quiet)

    marker = index_dir / ".last-update-attempt"
    now = time.time()
    try:
        if marker.exists() and now - marker.stat().st_mtime < DEBOUNCE_SECONDS:
            return 0  # attempted recently — wait out the window
    except OSError:
        pass

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    except OSError:
        return 0  # can't record the attempt → don't risk a spawn storm

    if dry_run:
        print(json.dumps({"op": "freshness-tick", "would_spawn": True,
                          "index_dir": str(index_dir)}))
        return 0

    try:
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(UPDATE_LOG, "ab")
        kwargs = {"stdout": log_f, "stderr": log_f,
                  "cwd": str(SCRIPT_DIR.parent.parent)}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survives the
            # parent bash exiting (nohup/disown are flaky on Git Bash).
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        args = [sys.executable, str(SCRIPT_DIR / "embedding-index-build.py"),
                "--update"]
        if os.environ.get("EMBED_FRESHNESS_INDEX_DIR"):
            args += ["--out", str(index_dir)]
        subprocess.Popen(args, **kwargs)
        print(json.dumps({"op": "freshness-tick", "spawned": True,
                          "index_dir": str(index_dir)}))
    except Exception as exc:  # fail-open — never abort the caller's phase
        print(json.dumps({"op": "freshness-tick", "error": str(exc)[:200]}),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
