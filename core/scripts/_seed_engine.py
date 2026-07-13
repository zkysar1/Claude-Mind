"""Seed engine — orchestrator for /seed plant + verify + diff sub-commands.

CLI sub-commands (consumed by core/scripts/seed-*.sh wrappers):
  build-plan        — parse manifest + walk source, emit JSON plan
  copy-staged       — copy + transform source to <dest>/.seed-staging/
  swap              — atomically move staging contents to <dest>/
  backup            — back up dest framework files before overwrite
  clean-cruft       — remove cruft_patterns at destination
  verify-completeness  — assert manifest includes exist at destination
  diff              — source-vs-destination diff (post-transform aware)
  list-includes     — list resolved include file set (debug helper)

The engine NEVER touches source content. It reads source, transforms in memory
or via staging, then writes destination.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Allow import from sibling modules
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _seed_transforms as xform  # noqa: E402

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: PyYAML required (pip install pyyaml)\n")
    sys.exit(2)


PROJECT_ROOT = SCRIPT_DIR.parent.parent  # core/scripts/ -> core/ -> repo root
STAGING_DIRNAME = ".seed-staging"


# ============================================================================
# Manifest parsing
# ============================================================================

def load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        m = yaml.safe_load(f)
    if not isinstance(m, dict):
        raise ValueError(f"Manifest at {path} is not a YAML mapping")
    return m


def check_skill_version(manifest: dict, skill_version: int = 1):
    minv = int(manifest.get("min_skill_version", 1))
    maxv = int(manifest.get("max_skill_version", 1))
    if not (minv <= skill_version <= maxv):
        raise SystemExit(
            f"Skill version {skill_version} is outside manifest range [{minv},{maxv}]. "
            f"Upgrade skill or downgrade manifest."
        )


# ============================================================================
# Path resolution / glob matching (parallel to _seed_transforms but for plan walk)
# ============================================================================

def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _glob_match(rel_path: str, pattern: str) -> bool:
    return xform._glob_match(rel_path, pattern)


def _matches_any(rel_path: str, patterns) -> bool:
    return any(_glob_match(rel_path, p) for p in (patterns or []))


# ============================================================================
# Include / exclude resolution
# ============================================================================

def _dir_name_only(pattern: str) -> str | None:
    """If pattern is a bare directory name with trailing /, return the dir name.

    Returns None for patterns with slashes, wildcards, or no trailing /.
      "__pycache__/"  → "__pycache__"
      "logs/"         → "logs"
      "scripts/foo/"  → None  (contains /)
      "tmp_*/"        → None  (contains wildcard)
      ".pytest_cache/" → ".pytest_cache"
    """
    if not pattern.endswith("/"):
        return None
    inner = pattern.rstrip("/")
    if "/" in inner or "*" in inner or "?" in inner:
        return None
    return inner


def walk_include_entry(entry: dict, source_root: Path) -> list:
    """Walk one `include` entry and return relative paths to copy.

    Honors `exclude_patterns` (glob, plus directory-name-anywhere semantics for
    bare-dir-name patterns like `__pycache__/`) and `exclude_children`
    (top-level child dir names under the entry's path).
    """
    path = entry["path"]
    etype = entry.get("type", "file")
    abs_path = source_root / path

    if etype == "file":
        if not abs_path.exists():
            if entry.get("required", False):
                raise SystemExit(f"Required include missing: {path}")
            return []
        return [path]

    if not abs_path.is_dir():
        if entry.get("required", False):
            raise SystemExit(f"Required include directory missing: {path}")
        return []

    raw_patterns = entry.get("exclude_patterns", [])
    exclude_children = set(entry.get("exclude_children", []))

    # Pre-compute the set of bare directory names to exclude anywhere
    excluded_dir_names = set()
    other_patterns = []
    for pat in raw_patterns:
        dn = _dir_name_only(pat)
        if dn is not None:
            excluded_dir_names.add(dn)
        else:
            other_patterns.append(pat)

    results = []
    for root, dirs, files in os.walk(abs_path):
        root_path = Path(root)
        try:
            rel_to_entry = root_path.relative_to(abs_path)
        except ValueError:
            continue

        rel_parts = rel_to_entry.parts

        # If any ancestor dir component is excluded, skip this entire subtree
        if any(part in excluded_dir_names for part in rel_parts):
            dirs[:] = []
            continue

        # Top-level child exclusion (exclude_children)
        if len(rel_parts) >= 1:
            if rel_parts[0] in exclude_children:
                dirs[:] = []
                continue

        # At depth 0, also prune top-level children listed in exclude_children
        # and any dirs whose name is in excluded_dir_names
        if rel_to_entry == Path("."):
            dirs[:] = [
                d for d in dirs
                if d not in exclude_children and d not in excluded_dir_names
            ]
        else:
            # Prune nested dirs by name (catches __pycache__ etc. at any depth)
            dirs[:] = [d for d in dirs if d not in excluded_dir_names]

        for fname in files:
            full = root_path / fname
            try:
                rel = full.relative_to(source_root)
            except ValueError:
                continue
            rel_str = _norm(str(rel))
            # Pattern-based exclude (globs like scripts/tests/_tmp_*/)
            if _matches_any(rel_str, other_patterns):
                continue
            try:
                rel_in_entry = full.relative_to(abs_path)
                if _matches_any(_norm(str(rel_in_entry)), other_patterns):
                    continue
            except ValueError:
                pass
            results.append(rel_str)
    return results


def resolve_include_set(manifest: dict, source_root: Path) -> list:
    """Resolve all `include` entries to a flat list of repo-relative paths.

    Applies `exclude_always` as a final filter so things like `__pycache__/`
    are stripped regardless of which include entry surfaced them.
    """
    all_paths = []
    seen = set()
    for entry in manifest.get("include", []):
        for p in walk_include_entry(entry, source_root):
            if p in seen:
                continue
            if is_excluded_always(p, manifest):
                continue
            seen.add(p)
            all_paths.append(p)
    return sorted(all_paths)


def is_excluded_always(rel_path: str, manifest: dict) -> bool:
    """True if rel_path matches any exclude_always glob.

    Supports five pattern shapes:
      1. `/foo/`             — ANCHORED dir at top-level only (gitignore-style).
                                `/world/` excludes top-level `world/...` but NOT
                                `mind_api/src/world/...`. Use this when the same
                                basename also appears nested as legitimate code.
      2. `foo/`              — directory name `foo` ANYWHERE in path
                                (path-component match — legacy semantics; use
                                for cache dirs like `__pycache__/` that must be
                                excluded at every depth).
      3. `a/b/`              — path prefix `a/b/`
      4. `*.stackdump`       — fnmatch glob on basename
      5. `path/to/file.ext`  — exact path match

    The anchoring distinction matters because the seed plant ships a
    Python package at `mind_api/src/world/` (and `mind_api/src/meta/`) that
    shares basenames with the top-level domain dirs `world/` and `meta/`.
    Bare-name `world/` over-matches both; `/world/` matches only the top.
    """
    rel = _norm(rel_path)
    patterns = manifest.get("exclude_always", [])
    parts = rel.split("/")
    for p in patterns:
        # Shape 1: leading-`/` anchored (gitignore-style) — top-level only
        if p.startswith("/"):
            anchored = p[1:]  # strip leading slash; rest is top-level-relative
            if anchored.endswith("/"):
                inner = anchored.rstrip("/")
                # Prefix match against rel only; never matches mid-path
                if rel.startswith(inner + "/") or rel == inner:
                    return True
            else:
                # Anchored file: exact match against rel; or basename glob
                # restricted to top-level files (no slash in rel).
                if rel == anchored:
                    return True
                if ("*" in anchored or "?" in anchored) and "/" not in anchored:
                    if "/" not in rel and fnmatch.fnmatch(rel, anchored):
                        return True
            continue
        if p.endswith("/"):
            inner = p.rstrip("/")
            # Bare directory name (no slashes, no wildcards) → component match
            if "/" not in inner and "*" not in inner and "?" not in inner:
                if inner in parts:
                    return True
                continue
            # Otherwise, prefix match
            if rel.startswith(inner + "/") or rel == inner:
                return True
        else:
            # Exact path or glob on full path
            if fnmatch.fnmatch(rel, p):
                return True
            # Glob on basename for patterns without /
            if "/" not in p and fnmatch.fnmatch(parts[-1], p):
                return True
    return False


# ============================================================================
# Plan building
# ============================================================================

def build_plan(manifest: dict, source_root: Path) -> dict:
    """Produce a JSON-serializable plan.

    Plan shape:
      {
        "version": 1,
        "source_root": "<abs path>",
        "manifest_version": <int>,
        "files": [
          {
            "rel_path": "...",
            "is_binary": bool,
            "transformations": [<rule_id>, ...],   # ids that WILL apply (best-effort)
            "pending_template_skip": bool,
          },
          ...
        ],
        "excludes": [{"path": "...", "reason": "exclude_always | pattern"}],
        "cruft_patterns": [...],
        "post_copy_actions": [...],
      }

    The plan is informational; the actual transformations re-run at copy time
    (so the plan doesn't need to capture transformed CONTENT, only WHICH rules
    will apply).
    """
    check_skill_version(manifest)
    files_in = resolve_include_set(manifest, source_root)
    transformations = manifest.get("transformations", [])

    files = []
    for rel in files_in:
        if is_excluded_always(rel, manifest):
            # Shouldn't happen if includes are right, but defensive
            continue
        rules = xform.select_rules_for_file(rel, transformations)
        applied_ids = []
        if rules["file_replace"]:
            applied_ids.append(rules["file_replace"]["id"])
        else:
            applied_ids.extend([r["id"] for r in rules["inline_edit"]])
            applied_ids.extend([r["id"] for r in rules["global_regex"]])
            applied_ids.extend([r["id"] for r in rules["word_list_strip"]])
        files.append({
            "rel_path": rel,
            "is_binary": xform.is_binary_path(rel),
            "transformations": applied_ids,
            "pending_template_skip": rules["pending_template_skip"],
        })

    return {
        "version": 1,
        "source_root": str(source_root),
        "manifest_version": int(manifest.get("version", 1)),
        "files": files,
        "cruft_patterns": manifest.get("cruft_patterns", []),
        "post_copy_actions": manifest.get("post_copy_actions", []),
        "exclude_always": manifest.get("exclude_always", []),
    }


# ============================================================================
# Backup
# ============================================================================

def do_backup(dest_root: Path, manifest: dict, source_root: Path) -> Path:
    """Copy currently-manifested files at destination into a timestamped backup dir.

    Returns the backup dir path. Skips files that don't exist at destination yet.
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = dest_root / f".seed-backup-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    files_in = resolve_include_set(manifest, source_root)
    backed_up = 0
    for rel in files_in:
        src_at_dest = dest_root / rel
        if not src_at_dest.exists() or src_at_dest.is_dir():
            continue
        backup_target = backup_dir / rel
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_at_dest, backup_target)
        backed_up += 1
    return backup_dir


# ============================================================================
# Staged copy
# ============================================================================

def do_copy_staged(source_root: Path, dest_root: Path, manifest: dict) -> dict:
    """Copy include set to <dest>/.seed-staging/ applying transformations.

    Returns: {"staged": int, "transformed": int, "binary": int, "pending_skip": [rel,...], "failures": [...]}.
    Raises SystemExit on failure (after cleaning staging dir).
    """
    staging = dest_root / STAGING_DIRNAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    files_in = resolve_include_set(manifest, source_root)
    transformations = manifest.get("transformations", [])

    stats = {"staged": 0, "transformed": 0, "binary": 0, "pending_skip": [], "failures": []}

    for rel in files_in:
        src = source_root / rel
        dst = staging / rel
        try:
            if not src.exists():
                # required-flagged missing already caught in resolve; non-required just skip
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)

            if xform.is_binary_path(rel):
                shutil.copy2(src, dst)
                stats["binary"] += 1
                stats["staged"] += 1
                continue

            # Read as bytes first to check for embedded NUL (likely binary).
            raw = src.read_bytes()
            if xform.is_likely_binary_content(raw):
                shutil.copy2(src, dst)
                stats["binary"] += 1
                stats["staged"] += 1
                continue

            content = raw.decode("utf-8", errors="strict")
            new_content, applied_ids, pending_skip = xform.transform_file(
                rel, content, transformations, source_root,
            )
            dst.write_text(new_content, encoding="utf-8", newline="")  # preserve content as-is
            stats["staged"] += 1
            if applied_ids:
                stats["transformed"] += 1
            if pending_skip:
                stats["pending_skip"].append(rel)
        except UnicodeDecodeError as e:
            # Treat as binary fallback
            shutil.copy2(src, dst)
            stats["binary"] += 1
            stats["staged"] += 1
        except Exception as e:
            stats["failures"].append({"rel_path": rel, "error": str(e)})

    if stats["failures"]:
        # Clean up the partial staging dir
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit(
            "Staged copy failed:\n"
            + "\n".join(f"  {f['rel_path']}: {f['error']}" for f in stats["failures"])
        )
    return stats


# ============================================================================
# Atomic swap
# ============================================================================

def do_swap(dest_root: Path) -> dict:
    """Move staged files from <dest>/.seed-staging/ to <dest>/, overwriting.

    Walks the staging tree, copies each file to its real path. We use
    copy-then-replace (not rename) because cross-filesystem moves on Windows
    can fail for files held open by editors. After all copies succeed, we
    remove the staging dir.

    On Windows, file replace can fail if the target is locked. We attempt
    up to 3 retries with short sleeps before reporting a fail.
    """
    import time
    staging = dest_root / STAGING_DIRNAME
    if not staging.is_dir():
        raise SystemExit(f"No staging dir to swap: {staging}")

    # Defensive: sweep any orphan .seed-tmp files from a prior failed swap.
    # These can be left behind on Windows when os.replace fails after the
    # copy succeeded — see BUG 3 from fresh-eyes review (2026-05-19).
    for orphan in dest_root.rglob("*.seed-tmp"):
        try:
            orphan.unlink()
        except OSError:
            pass

    moved = 0
    failures = []

    for root, dirs, files in os.walk(staging):
        root_p = Path(root)
        try:
            rel_dir = root_p.relative_to(staging)
        except ValueError:
            continue
        for fname in files:
            src = root_p / fname
            dst = dest_root / rel_dir / fname
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = None
            last_err = None
            for attempt in range(3):
                try:
                    # On Windows, copy2 + replace via os.replace is safest.
                    if dst.exists():
                        # Use os.replace for atomicity where supported
                        tmp = dst.with_suffix(dst.suffix + ".seed-tmp")
                        shutil.copy2(src, tmp)
                        os.replace(tmp, dst)
                        tmp = None  # consumed by replace
                    else:
                        shutil.copy2(src, dst)
                    last_err = None
                    moved += 1
                    break
                except (OSError, PermissionError) as e:
                    last_err = e
                    time.sleep(0.25 * (attempt + 1))
            # If retries exhausted with a temp file still on disk, clean it
            if tmp is not None and tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if last_err:
                failures.append({"rel_path": str(rel_dir / fname), "error": str(last_err)})

    if failures:
        return {"moved": moved, "failures": failures}

    # Success — remove staging dir. FAIL-LOUD (0): the moves already
    # succeeded, so a staging-dir cleanup error must NOT fail the swap, but it
    # MUST be surfaced as a NAMED field rather than silently swallowed by
    # ignore_errors=True — a silent post-move raise on this step was the
    # hypothesized origin of the v2.1.1 silent-post-swap-death. Try a clean
    # removal to capture any error, then a best-effort ignore_errors sweep to
    # remove whatever can still be removed. Never raises (cleanup is best-effort
    # once the swap itself has landed).
    result = {"moved": moved, "failures": []}
    try:
        shutil.rmtree(staging)
    except OSError as e:
        result["staging_cleanup_error"] = f"rmtree({staging}): {e}"
        shutil.rmtree(staging, ignore_errors=True)
    return result


# ============================================================================
# Cruft cleanup
# ============================================================================

def do_clean_cruft(dest_root: Path, manifest: dict, *,
                   preserve_deployment_local: bool = False) -> dict:
    """Remove cruft_patterns at destination.

    When *preserve_deployment_local* is True (``--living-prod`` mode),
    every candidate is checked against ``_is_preserved_at_dest`` and
    ``_is_protected_dest_skill`` before deletion. Matches are skipped
    and reported in ``skipped_preserved`` so callers can see what was
    protected.
    """
    patterns = manifest.get("cruft_patterns", [])
    removed = []
    skipped_preserved = []

    # In-repo world/meta stores are protected UNCONDITIONALLY (not gated on
    # --living-prod): a data store must never be cruft-swept in any mode
    # (2026-07-07 ZDS wipe). Empty set on fresh plants (no agents/ dir).
    store_tops = _in_repo_store_tops(dest_root)

    # Build dest forged-skill protection set (same logic as do_remove_orphans)
    if preserve_deployment_local:
        dest_forged = _dest_forged_skill_names(dest_root)
        protect_all_skills = dest_forged is None
        forged_prefixes = set() if dest_forged is None else {
            f".claude/skills/{n}" for n in dest_forged
        }
    else:
        protect_all_skills = False
        forged_prefixes = set()

    def _should_preserve(rel: str) -> bool:
        """Return True if *rel* must be kept at a living destination."""
        if rel.split("/", 1)[0] in store_tops:
            return True
        if not preserve_deployment_local:
            return False
        if _is_preserved_at_dest(rel):
            return True
        if _is_protected_dest_skill(rel, forged_prefixes, protect_all_skills):
            return True
        return False

    for p in patterns:
        # Patterns can be exact paths, glob, or directory paths with trailing /
        if p.endswith("/"):
            target = dest_root / p.rstrip("/")
            if target.exists() and target.is_dir():
                rel = _norm(str(target.relative_to(dest_root)))
                # Check if any file inside the directory tree is preserved
                if rel.split("/", 1)[0] in store_tops:
                    skipped_preserved.append(p)
                    continue
                if preserve_deployment_local:
                    has_preserved = False
                    for child in target.rglob("*"):
                        if child.is_file():
                            child_rel = _norm(str(child.relative_to(dest_root)))
                            if _should_preserve(child_rel):
                                has_preserved = True
                                break
                    if has_preserved or _should_preserve(rel):
                        skipped_preserved.append(p)
                        continue
                shutil.rmtree(target, ignore_errors=True)
                removed.append(p)
        else:
            # Try exact file first
            target = dest_root / p
            if target.exists() and target.is_file():
                rel = _norm(str(target.relative_to(dest_root)))
                if _should_preserve(rel):
                    skipped_preserved.append(p)
                    continue
                target.unlink()
                removed.append(p)
                continue
            # Glob match. To recurse, callers must use a `**/` prefix
            # explicitly (e.g., `**/__pycache__/`). The plain pattern only
            # matches at the top level — prevents accidental destruction of
            # user data deep in the tree (BUG 5 from fresh-eyes review).
            if "*" in p or "?" in p:
                if p.startswith("**/"):
                    matches = list(dest_root.rglob(p[3:]))
                else:
                    matches = list(dest_root.glob(p))
                for m in matches:
                    try:
                        m_rel = _norm(str(m.relative_to(dest_root)))
                        if _should_preserve(m_rel):
                            skipped_preserved.append(m_rel)
                            continue
                        if m.is_dir():
                            shutil.rmtree(m, ignore_errors=True)
                        else:
                            m.unlink()
                        removed.append(str(m.relative_to(dest_root)))
                    except (OSError, FileNotFoundError):
                        pass
    return {"removed": removed, "skipped_preserved": skipped_preserved}


# ============================================================================
# Orphan removal — mirror semantics
# ============================================================================
# Files removed from source's manifest-resolved include set must also disappear
# at destination. Without this step, destination accumulates files that were
# refactored away upstream — a classic "dev vs prod" drift.

# Top-level paths the engine MUST NEVER walk during orphan detection, even if
# they happen to exist at destination. Each is preserved for a specific reason.
_ORPHAN_SCAN_SKIP_TOP = {
    ".git",                 # version control
    ".seed-staging",        # transient staging from current/in-flight plant
    "agents",               # per-deployment state (created by /start)
    "world",                # per-deployment domain state (external path in source)
    "meta",                 # per-deployment strategy state (external path in source)
    ".mind-data",           # in-repo own-cloud world/meta store (ZDS layout since
                            # 2026-06-30). EMERGENCY STOPGAP 2026-07-07 (applied by omni
                            # with user's explicit direction, all three repos): the
                            # sweep unlinked ZDS's entire world+meta because this name
                            # was missing (commit fb3634a transplant; restored from the
                            # dormant OneDrive backup). PROPER FIX (dev-chain): resolve
                            # every dest agents/*/local-paths.conf WORLD_PATH/META_PATH
                            # and preserve any root inside dest_root — do not rely on
                            # this hardcoded name surviving future layout renames.
}

# ── Unified deployment-local preservation (single source of truth) ──
# Files that legitimately differ per deployment and MUST survive transplant.
# Union of the three previously-independent lists (orphan-preserve, verify-
# leak-check, promotion-preflight DEPLOYMENT_LOCAL). Consulted by BOTH
# do_clean_cruft and do_remove_orphans via _is_preserved_at_dest.
_DEPLOYMENT_LOCAL_FILES = {
    ".env.local",
    ".claude/settings.local.json",
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/rules/promotion-cycle.md",
}

# Gitignored operational directories that must survive at a living production
# destination. Paths are relative to dest_root; prefix-matched.
_OPERATIONAL_DIRS = {
    "core/scripts/.python-shim",
    "core/logs",
    "mind_api/state",
}

# Backward-compatible alias — existing code that references _ORPHAN_PRESERVE_FILES
# continues to work, but the canonical set is _DEPLOYMENT_LOCAL_FILES.
_ORPHAN_PRESERVE_FILES = _DEPLOYMENT_LOCAL_FILES


def _is_preserved_at_dest(rel: str, extra_tops=frozenset()) -> bool:
    if rel.split("/", 1)[0] in extra_tops:
        return True
    if rel in _DEPLOYMENT_LOCAL_FILES:
        return True
    # .seed-backup-<timestamp>/ from prior plants
    first = rel.split("/", 1)[0]
    if first.startswith(".seed-backup-"):
        return True
    if first in _ORPHAN_SCAN_SKIP_TOP:
        return True
    # Gitignored operational directories (prefix match)
    for op_dir in _OPERATIONAL_DIRS:
        if rel == op_dir or rel.startswith(op_dir + "/"):
            return True
    return False


def _read_conf_path_key(conf: Path, want_key: str):
    """Extract *want_key* (e.g. WORLD_PATH / META_PATH) from a local-paths.conf.

    Format: KEY=value lines, `#` comments, optional surrounding quotes.
    Returns the value string, or None if absent/unreadable.
    """
    try:
        text = conf.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == want_key:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val or None
    return None


def _read_world_path_from_conf(conf: Path):
    """Extract WORLD_PATH from a local-paths.conf (see _read_conf_path_key)."""
    return _read_conf_path_key(conf, "WORLD_PATH")


def _in_repo_store_tops(dest_root: Path) -> set:
    """Top-level dir names under *dest_root* that hold an agent world/meta store.

    Resolves every dest agents/*/local-paths.conf WORLD_PATH/META_PATH; any
    configured root that lives INSIDE dest_root contributes its first path
    segment. Orphan-removal and cruft sweeps must never walk these — they are
    per-deployment data stores, not framework files. Dynamic complement to the
    static ".mind-data" entry in _ORPHAN_SCAN_SKIP_TOP: a renamed or novel
    in-repo store is protected without a code change (2026-07-07 ZDS incident:
    the static lists did not know .mind-data and the sweep unlinked the entire
    world+meta; commit fb3634a era, restored from the dormant OneDrive backup).

    External paths (the normal layout) resolve outside dest_root and are
    ignored. Unreadable confs and unresolvable paths fail toward EMPTY —
    the static skip list remains the floor.
    """
    tops = set()
    agents_dir = dest_root / "agents"
    if not agents_dir.is_dir():
        return tops
    try:
        dest_resolved = dest_root.resolve()
    except OSError:
        return tops
    for conf in sorted(agents_dir.glob("*/local-paths.conf")):
        for key in ("WORLD_PATH", "META_PATH"):
            val = _read_conf_path_key(conf, key)
            if not val:
                continue
            try:
                rel = Path(val).resolve().relative_to(dest_resolved)
            except (ValueError, OSError):
                continue
            if rel.parts:
                tops.add(rel.parts[0])
    return tops


def _dest_forged_skill_names(dest_root: Path):
    """Return the set of forged/domain skill NAMES registered at the
    destination, or None if no registry can be located or parsed.

    Lookup order:
      1. <dest>/world/forged-skills.yaml  (local world dir, if present)
      2. WORLD_PATH from <dest>/agents/*/local-paths.conf -> <world>/forged-skills.yaml
         (external world dir — the normal layout, world/ lives off-repo)

    Returns None (NOT an empty set) when no registry is found OR a located
    registry is unparseable, so the caller fails SAFE toward preserving every
    skill dir. An empty set is returned only when a registry IS found and
    parsed but genuinely lists zero skills.
    """
    candidates = []
    local = dest_root / "world" / "forged-skills.yaml"
    if local.is_file():
        candidates.append(local)
    agents_dir = dest_root / "agents"
    if agents_dir.is_dir():
        for conf in sorted(agents_dir.glob("*/local-paths.conf")):
            world_path = _read_world_path_from_conf(conf)
            if world_path:
                ext = Path(world_path) / "forged-skills.yaml"
                if ext.is_file():
                    candidates.append(ext)
    if not candidates:
        return None
    names = set()
    found_any = False
    for reg in candidates:
        try:
            data = yaml.safe_load(reg.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            # A located-but-unparseable registry => fail safe (preserve all)
            return None
        if isinstance(data, dict) and isinstance(data.get("skills"), dict):
            names.update(data["skills"].keys())
            found_any = True
    if not found_any:
        return None
    return names


def _is_protected_dest_skill(rel: str, forged_prefixes: set,
                             protect_all_skills: bool) -> bool:
    """Whether `rel` belongs to a destination-owned skill orphan-removal must
    never delete.

    The destination may carry forged/domain skills absent from the source
    include set (stripped during frontier->seed promotion). Those skill dirs
    are registered in the destination's OWN forged-skills.yaml; deleting them
    destroys the downstream world's domain capability — the failure mode this
    guard prevents (omni, 2026-06-06).

    - protect_all_skills=True (registry unlocatable/unparseable): preserve
      EVERY `.claude/skills/<name>/` path — fail-safe toward preservation.
    - otherwise: preserve only paths under a registered forged skill dir.

    Base skills present in the source manifest are kept by the caller's
    `rel in expected` check BEFORE this is consulted, so this only governs
    skill dirs that are NOT in the source include set.
    """
    if not rel.startswith(".claude/skills/"):
        return False
    if protect_all_skills:
        return True
    for prefix in forged_prefixes:
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def do_remove_orphans(dest_root: Path, manifest: dict, source_root: Path,
                      dry_run: bool = False) -> dict:
    """Remove files at destination that are NOT in the manifest-resolved include set.

    Semantics: destination = mirror of (manifest ∩ source). Files that exist at
    destination but no longer at source (or are now excluded by manifest) are
    deleted. Preserved paths (.git, .env.local, .claude/settings.local.json,
    .seed-backup-*, agents/, world/, meta/, .mind-data/, plus any in-repo
    world/meta store root resolved from agents/*/local-paths.conf — see
    _in_repo_store_tops) are never touched. Additionally,
    destination-owned forged/domain skill dirs (`.claude/skills/<name>/` for
    <name> in the destination's own forged-skills.yaml) are preserved even
    though they are absent from the source include set — see
    _is_protected_dest_skill (omni orphan-removal guard, 2026-06-06).

    Returns {"removed": [...], "kept_preserved": [...], "dry_run": bool}.
    """
    expected = set(resolve_include_set(manifest, source_root))

    # Destination-owned skill protection (omni orphan-removal guard, 2026-06-06).
    # The destination may carry forged/domain skills absent from the source
    # include set (stripped during frontier->seed promotion). Deleting them
    # would destroy the downstream world's domain capability. Read the dest's
    # OWN forged-skills.yaml and protect those skill dirs. Fail-safe: an
    # unlocatable/unparseable registry protects ALL `.claude/skills/<name>/`.
    dest_forged = _dest_forged_skill_names(dest_root)
    protect_all_skills = dest_forged is None
    forged_prefixes = set() if dest_forged is None else {
        f".claude/skills/{n}" for n in dest_forged
    }

    # In-repo world/meta stores (e.g. .mind-data/) — dynamic protection.
    # The static _ORPHAN_SCAN_SKIP_TOP covers known names; this resolves the
    # ACTUAL configured roots from every dest agent local-paths.conf so a
    # renamed store can never be swept (2026-07-07 ZDS wipe, fb3634a era).
    store_tops = _in_repo_store_tops(dest_root)

    removed = []
    preserved = []

    # Walk destination, collecting all files outside the preserve set
    for path in dest_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(dest_root)).replace("\\", "/")
        except ValueError:
            continue
        if _is_preserved_at_dest(rel, store_tops):
            preserved.append(rel)
            continue
        if rel in expected:
            continue
        if _is_protected_dest_skill(rel, forged_prefixes, protect_all_skills):
            preserved.append(rel)
            continue
        # Orphan — would be removed
        if dry_run:
            removed.append(rel)
        else:
            try:
                path.unlink()
                removed.append(rel)
            except (OSError, FileNotFoundError):
                pass

    # Second pass: remove empty directories left behind (skip preserved tops)
    if not dry_run:
        for path in sorted(dest_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                rel = str(path.relative_to(dest_root)).replace("\\", "/")
            except ValueError:
                continue
            first = rel.split("/", 1)[0]
            if (first in _ORPHAN_SCAN_SKIP_TOP or first.startswith(".seed-backup-")
                    or first in store_tops):
                continue
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                pass

    return {"removed": sorted(removed), "kept_preserved_count": len(preserved),
            "dry_run": dry_run}


# ============================================================================
# Completeness verification
# ============================================================================

def do_verify_completeness(dest_root: Path, manifest: dict, source_root: Path) -> dict:
    """For each include entry, verify it exists at destination."""
    results = []
    for entry in manifest.get("include", []):
        path = entry["path"]
        etype = entry.get("type", "file")
        abs_dst = dest_root / path

        # Check what we EXPECT at destination
        if etype == "file":
            ok = abs_dst.exists() and abs_dst.is_file()
        else:
            ok = abs_dst.exists() and abs_dst.is_dir()

        results.append({
            "path": path,
            "type": etype,
            "required": entry.get("required", False),
            "exists": ok,
        })

    # Aggregate
    missing_required = [r for r in results if r["required"] and not r["exists"]]
    return {
        "results": results,
        "missing_required": missing_required,
        "pass": len(missing_required) == 0,
    }


def do_verify_leak_check(dest_root: Path, manifest: dict) -> dict:
    """Verify that exclude_always paths did NOT land at destination.

    Special-cases per-deployment / per-machine paths that the destination is
    EXPECTED to own its own copy of:
      - Domain dirs (agents/, world/, meta/) — agent runtime state
      - .git/                                — destination has its own git history
      - .env.local                           — per-machine secrets file
      - .claude/settings.local.json          — per-machine Claude Code config

    These appear in `exclude_always` to prevent COPYING from source, but they
    SHOULD exist at destination. Report as INFO, not leaks.

    Anchored patterns (leading `/`, gitignore-style introduced 2026-05-20)
    are normalized before matching against the special-case sets so `/world/`,
    `/agents/`, `/meta/`, `/.git/` route to the same preservation logic as
    their legacy bare-name forms.
    """
    PRESERVED_AT_DEST = {
        # Directory basenames (matched against target.name)
        "agents", "world", "meta",
    }
    # Derive from the module-level single source of truth
    PRESERVED_PATHS = {".git/"}
    for _dlf in _DEPLOYMENT_LOCAL_FILES:
        PRESERVED_PATHS.add(_dlf)
    leaked = []
    info = []
    for p in manifest.get("exclude_always", []):
        # Normalize leading `/` (anchored gitignore-style) — semantically
        # equivalent at top level, which is the only place anchored patterns
        # can match. This keeps the preservation special-cases insensitive
        # to whether the manifest opts into anchoring.
        norm = p[1:] if p.startswith("/") else p
        if norm in PRESERVED_PATHS:
            target = dest_root / norm.rstrip("/")
            if target.exists():
                info.append(p)
            continue
        if norm.endswith("/"):
            inner = norm.rstrip("/")
            target = dest_root / inner
            if target.exists() and target.is_dir():
                if target.name in PRESERVED_AT_DEST:
                    info.append(p)
                    continue
                leaked.append(p)
        else:
            if "*" in norm or "?" in norm:
                # Top-level glob only (don't rglob — too aggressive)
                if list(dest_root.glob(norm)):
                    leaked.append(p)
            else:
                if (dest_root / norm).exists():
                    leaked.append(p)
    return {"leaked": leaked, "info": info, "pass": len(leaked) == 0}


def do_verify_cruft(dest_root: Path, manifest: dict) -> dict:
    """Verify that cruft_patterns are NOT present at destination."""
    present = []
    for p in manifest.get("cruft_patterns", []):
        if p.endswith("/"):
            inner = p.rstrip("/")
            if (dest_root / inner).exists():
                present.append(p)
        elif "*" in p or "?" in p:
            if p.startswith("**/"):
                if list(dest_root.rglob(p[3:])):
                    present.append(p)
            else:
                if list(dest_root.glob(p)):
                    present.append(p)
        else:
            if (dest_root / p).exists():
                present.append(p)
    return {"present": present, "pass": len(present) == 0}


def do_verify_integrity(dest_root: Path, manifest: dict, source_root: Path) -> dict:
    """SHA-256 sample integrity: source post-transform vs destination current."""
    samples_default = [
        "CLAUDE.md",
        "core/scripts/_paths.sh",
        ".claude/settings.json",
        "core/config/tree.yaml",
    ]
    # Include mind_api sample if present
    mind_sample = "mind_api/src/server.py"
    if (source_root / mind_sample).exists():
        samples_default.append(mind_sample)

    transformations = manifest.get("transformations", [])
    results = []
    for rel in samples_default:
        src = source_root / rel
        dst = dest_root / rel
        if not src.exists() or not dst.exists():
            results.append({
                "file": rel, "status": "MISSING",
                "src_exists": src.exists(), "dst_exists": dst.exists(),
            })
            continue
        try:
            raw = src.read_bytes()
            if xform.is_binary_path(rel) or xform.is_likely_binary_content(raw):
                src_h = hashlib.sha256(raw).hexdigest()
                dst_h = hashlib.sha256(dst.read_bytes()).hexdigest()
            else:
                content = raw.decode("utf-8")
                transformed, _, _ = xform.transform_file(rel, content, transformations, source_root)
                src_h = hashlib.sha256(transformed.encode("utf-8")).hexdigest()
                dst_h = hashlib.sha256(dst.read_bytes()).hexdigest()
            results.append({
                "file": rel,
                "status": "MATCH" if src_h == dst_h else "MISMATCH",
                "src": src_h[:12],
                "dst": dst_h[:12],
            })
        except Exception as e:
            results.append({"file": rel, "status": "ERROR", "error": str(e)})
    mismatches = sum(1 for r in results if r["status"] == "MISMATCH")
    matches = sum(1 for r in results if r["status"] == "MATCH")
    return {
        "results": results,
        "match_count": matches,
        "mismatch_count": mismatches,
        "pass": mismatches == 0,
    }


# ============================================================================
# Diff
# ============================================================================

def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def do_diff(source_root: Path, dest_root: Path, manifest: dict) -> dict:
    """Per-file diff: source post-transform vs destination current."""
    files_in = resolve_include_set(manifest, source_root)
    transformations = manifest.get("transformations", [])

    new_at_dest = []
    modified = []
    missing_at_dest = []
    identical = 0

    for rel in files_in:
        src = source_root / rel
        dst = dest_root / rel
        if not src.exists():
            continue

        if xform.is_binary_path(rel):
            # Compare raw bytes
            if not dst.exists():
                missing_at_dest.append(rel)
                continue
            if src.read_bytes() == dst.read_bytes():
                identical += 1
            else:
                modified.append(rel)
            continue

        try:
            raw = src.read_bytes()
            if xform.is_likely_binary_content(raw):
                if not dst.exists():
                    missing_at_dest.append(rel)
                    continue
                if raw == dst.read_bytes():
                    identical += 1
                else:
                    modified.append(rel)
                continue
            content = raw.decode("utf-8")
            transformed, _, _ = xform.transform_file(rel, content, transformations, source_root)
            if not dst.exists():
                missing_at_dest.append(rel)
                continue
            dst_content = dst.read_text(encoding="utf-8", errors="replace")
            if transformed == dst_content:
                identical += 1
            else:
                modified.append(rel)
        except Exception as e:
            modified.append(f"{rel} (error: {e})")

    return {
        "missing_at_dest": missing_at_dest,
        "modified": modified,
        "identical_count": identical,
        "new_at_dest": new_at_dest,  # populated by scan-dest if requested; empty by default
    }


# ============================================================================
# CLI
# ============================================================================

def _parse_args():
    p = argparse.ArgumentParser(prog="_seed_engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("build-plan")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))

    sp = sub.add_parser("backup")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("copy-staged")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("swap")
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("clean-cruft")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--dest", required=True)
    sp.add_argument("--preserve-deployment-local", action="store_true",
                    help="Guard deployment-local and domain files from deletion")

    sp = sub.add_parser("remove-orphans")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)
    sp.add_argument("--dry-run", action="store_true",
                    help="Report orphans without deleting")

    sp = sub.add_parser("verify-completeness")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("verify-leak-check")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("verify-cruft")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("verify-integrity")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("diff")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("list-includes")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))

    return p.parse_args()


def main():
    args = _parse_args()
    manifest_path = Path(args.manifest) if hasattr(args, "manifest") else None
    source_root = Path(getattr(args, "source", PROJECT_ROOT)).resolve()
    dest_root = Path(args.dest).resolve() if hasattr(args, "dest") else None

    if manifest_path is not None and not manifest_path.is_absolute():
        manifest_path = (PROJECT_ROOT / manifest_path).resolve()

    manifest = load_manifest(manifest_path) if manifest_path else None

    if args.cmd == "build-plan":
        plan = build_plan(manifest, source_root)
        print(json.dumps(plan, indent=2))
    elif args.cmd == "backup":
        bd = do_backup(dest_root, manifest, source_root)
        print(json.dumps({"backup_dir": str(bd)}, indent=2))
    elif args.cmd == "copy-staged":
        stats = do_copy_staged(source_root, dest_root, manifest)
        print(json.dumps(stats, indent=2))
    elif args.cmd == "swap":
        result = do_swap(dest_root)
        print(json.dumps(result, indent=2))
        if result["failures"]:
            sys.exit(1)
    elif args.cmd == "clean-cruft":
        pdl = getattr(args, "preserve_deployment_local", False)
        result = do_clean_cruft(dest_root, manifest,
                                preserve_deployment_local=pdl)
        print(json.dumps(result, indent=2))
    elif args.cmd == "remove-orphans":
        result = do_remove_orphans(dest_root, manifest, source_root,
                                   dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.cmd == "verify-completeness":
        result = do_verify_completeness(dest_root, manifest, source_root)
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit(1)
    elif args.cmd == "verify-leak-check":
        result = do_verify_leak_check(dest_root, manifest)
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit(1)
    elif args.cmd == "verify-cruft":
        result = do_verify_cruft(dest_root, manifest)
        print(json.dumps(result, indent=2))
        # cruft is a WARN not a FAIL — exit 0 even with hits
    elif args.cmd == "verify-integrity":
        result = do_verify_integrity(dest_root, manifest, source_root)
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit(1)
    elif args.cmd == "diff":
        result = do_diff(source_root, dest_root, manifest)
        print(json.dumps(result, indent=2))
    elif args.cmd == "list-includes":
        files = resolve_include_set(manifest, source_root)
        print(json.dumps({"count": len(files), "files": files}, indent=2))


if __name__ == "__main__":
    main()
