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
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Allow import from sibling modules
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import _seed_transforms as xform  # noqa: E402
from _exec_bits import (  # noqa: E402  (; index-level carry + verify )
    carry_exec_bit, carry_index_exec_bits, index_exec_map, verify_index_exec_bits,
)

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

    # : auto-derive forged-skill exclusions from world/forged-skills.yaml
    # so a newly-forged domain skill can NEVER leak into the domain-free seed.
    # The manifest's static exclude_children was hand-mirrored against the forged
    # registry with no sync enforcement; that drift recurrently produced
    # promote-blocking leaks (v2.2.0 audit-roblox-deliverable, v2.4.0
    # build-operator-job). Scoped to the .claude/skills/ entry — forged skills
    # live only there. UNION, not replace: the static list legitimately also
    # carries non-forged ephemeral entries (worktrees, .history) AND remains the
    # fail-safe floor when the registry is unlocatable (_dest_forged_skill_names
    # returns None -> the union is a no-op and the static list still applies).
    if entry["path"].rstrip("/") == ".claude/skills":
        # Fail-safe: ANY registry-read error (e.g. a non-UTF-8 local-paths.conf
        # raising UnicodeDecodeError past _read_conf_path_key's OSError-only
        # guard) must fall back to the static exclude_children floor, NOT crash
        # seed-create — a crash blocks ALL promotion, strictly worse than the
        # leak the downstream domain-leak preflight already catches. Mirrors the
        # defensive wrap the scan caller uses (_seed_create_scan.py).
        try:
            _forged = _dest_forged_skill_names(source_root)
        except Exception:
            _forged = None
        if _forged:
            exclude_children |= _forged

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

def do_copy_staged(source_root: Path, dest_root: Path, manifest: dict, *,
                   preserve_deployment_local: bool = False) -> dict:
    """Copy include set to <dest>/.seed-staging/ applying transformations.

    Returns: {"staged": int, "transformed": int, "binary": int,
              "pending_skip": [rel,...], "failures": [...],
              "preserved_deployment_local": [rel,...]}.
    Raises SystemExit on failure (after cleaning staging dir).

    When *preserve_deployment_local* is True (``--living-prod`` mode), any
    include-set member that is protected at a living destination (deployment-
    local file, in-repo store root, dest-owned forged skill, operational dir —
    see _living_dest_preserve_predicate) AND already exists at the destination
    is NOT staged, so the downstream swap never overwrites the destination's
    own copy. A protected file ABSENT at the destination is still staged
    (planted fresh). Filtering here is sufficient and complete: do_swap only
    ever moves what this step stages, and staging is rebuilt fresh on every
    call — so do_swap needs no preserve flag of its own (Bug #1).
    """
    staging = dest_root / STAGING_DIRNAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    files_in = resolve_include_set(manifest, source_root)
    transformations = manifest.get("transformations", [])

    stats = {"staged": 0, "transformed": 0, "binary": 0, "pending_skip": [],
             "failures": [], "preserved_deployment_local": [],
             "exec_bits_carried": 0, "exec_source_executable": 0}

    # : the SOURCE's executable bits, index-preferred. Read ONCE --
    # one `git ls-tree` for the whole plant, not one per file. An empty map is
    # a DECLINE (see index_exec_map), and carry_exec_bit then falls back to the
    # filesystem per file rather than treating absence as "not executable".
    idx_exec = index_exec_map(source_root)

    # Skill dirs present in the SOURCE include set are base skills being
    # promoted — the preserve predicate must not freeze them at dest
    # ( overreach; see _living_dest_preserve_predicate docstring).
    src_skill_names = {p[2] for p in (r.split("/", 3) for r in files_in)
                       if len(p) == 4 and p[0] == ".claude" and p[1] == "skills"}
    preserve_pred = (_living_dest_preserve_predicate(dest_root, src_skill_names)
                     if preserve_deployment_local else None)

    for rel in files_in:
        # --living-prod: never stage a protected file that already exists at
        # the destination — leaving it out of staging keeps the dest's own
        # deployment-local content (do_swap moves only what is staged).
        if (preserve_pred is not None and preserve_pred(rel)
                and (dest_root / rel).is_file()):
            stats["preserved_deployment_local"].append(rel)
            continue
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
            # : write_text CREATES the file at the umask default (0644),
            # so this branch -- and ONLY this branch -- strips the exec bit; the
            # three shutil.copy2 paths around it already carry mode. Every .sh and
            # every git hook is text, which is why the strip measured 628/628.
            if carry_exec_bit(rel, src, dst, idx_exec):
                stats["exec_bits_carried"] += 1
                stats["exec_source_executable"] += 1
            elif idx_exec.get(rel) or (src.stat().st_mode & 0o111):
                # Source IS executable but the carry did not fire (already set, or
                # chmod refused). Counted so seed-verify can compare source-exec
                # against carried and see a gap instead of a bare zero.
                stats["exec_source_executable"] += 1
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

    # Success — remove staging dir. FAIL-LOUD (): the moves already
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

def do_clean_cruft(dest_root: Path, manifest: dict, source_root: Path = None, *,
                   preserve_deployment_local: bool = False) -> dict:
    """Remove cruft_patterns at destination.

    A cruft pattern that names a *source-include* path is NEVER swept: when
    *source_root* is provided the include set is resolved and any ``rel`` in it
    is preserved unconditionally — parity with do_remove_orphans' ``rel in
    expected`` gate (g-115-2739). Passing source_root=None disables that gate
    (expected stays empty), preserving legacy behavior for callers/tests that
    don't supply a source.

    When *preserve_deployment_local* is True (``--living-prod`` mode),
    every candidate is additionally checked against ``_is_preserved_at_dest``
    and ``_is_protected_dest_skill`` (registry-forged AND SKILL.md-present
    skills) before deletion. Matches are skipped and reported in
    ``skipped_preserved`` so callers can see what was protected.
    """
    patterns = manifest.get("cruft_patterns", [])
    removed = []
    skipped_preserved = []

    # In-repo world/meta stores are protected UNCONDITIONALLY (not gated on
    # --living-prod): a data store must never be cruft-swept in any mode
    # (2026-07-07 ZDS wipe). Empty set on fresh plants (no agents/ dir).
    store_tops = _in_repo_store_tops(dest_root)

    # Source-include members are never cruft (parity with do_remove_orphans).
    # None source_root -> empty set -> gate is a no-op (legacy/test callers).
    expected = set(resolve_include_set(manifest, source_root)) if source_root else set()

    # Build dest forged-skill protection set (same logic as do_remove_orphans):
    # registry-forged UNION SKILL.md-present (root cause A, ).
    if preserve_deployment_local:
        dest_forged = _dest_forged_skill_names(dest_root)
        protect_all_skills = dest_forged is None
        _skill_names = (set() if dest_forged is None else set(dest_forged)) \
            | _dest_skill_names_with_skillmd(dest_root)
        forged_prefixes = {f".claude/skills/{n}" for n in _skill_names}
    else:
        protect_all_skills = False
        forged_prefixes = set()

    def _should_preserve(rel: str) -> bool:
        """Return True if *rel* must be kept at a living destination."""
        if rel.split("/", 1)[0] in store_tops:
            return True
        if rel in expected:
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
    # .gitignore is deployment-local because what a deployment must TRACK varies
    # by its storage backend (). A local-backend deployment un-ignored
    # its in-repo storage root on an explicit 2026-07-28 user directive — ~950MB
    # single-copy in a private repo, so tracking IS the backup — and each plant
    # re-added the blanket ignore. That was the THIRD such revert by a sync (an
    # earlier one is recorded in that deployment's session-manifest, 2026-07-27),
    # which is the signature of a file that keeps being restored by hand instead
    # of being declared deployment-local.
    #
    # The tradeoff is the same one CLAUDE.md and settings.json already carry and
    # is deliberate: a NEW framework .gitignore rule will no longer propagate
    # downstream on its own and must be applied per deployment. Preferred over
    # the alternative, because the failure modes are not symmetric — an
    # un-propagated ignore rule leaves junk tracked and visible in git status,
    # while an overwritten .gitignore silently stops tracking a deployment's only
    # copy of its data.
    ".gitignore",
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


def _dest_skill_names_with_skillmd(dest_root: Path) -> set:
    """Return the set of skill NAMES present at the destination whose dir
    carries a SKILL.md — a filesystem-presence signal INDEPENDENT of the
    forged-skills.yaml registry.

    Root cause A (g-115-2738): forged-skill REGISTRATION lives in the external,
    gitignored world/forged-skills.yaml, so it does NOT travel with a git
    promotion. A skill promoted frontier->seed->prod arrives as a real
    .claude/skills/<name>/SKILL.md dir but is ABSENT from the downstream
    registry — so _dest_forged_skill_names (registry-based) does not protect it
    and orphan-removal deletes it (the notify-user deletion, twice). A dir with
    a SKILL.md IS a live skill regardless of registration; protecting it by
    filesystem presence closes that gap. Un-forging a skill must be a
    deliberate act, never a transplant side-effect. Empty set when no skills
    dir exists (fresh plant).
    """
    skills_dir = dest_root / ".claude" / "skills"
    if not skills_dir.is_dir():
        return set()
    names = set()
    try:
        for child in skills_dir.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                names.add(child.name)
    except OSError:
        pass
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

    This helper only correctly governs skill dirs that are NOT in the source
    include set — but that precondition is the CALLER's to establish, and the
    two lanes do it differently. The sweep/deletion lanes (do_clean_cruft,
    do_remove_orphans) run `rel in expected` BEFORE consulting this, so base
    skills never reach it. The plant lane (_living_dest_preserve_predicate)
    has no include-set gate and must instead subtract source_skill_names from
    the prefix set AND bypass the protect_all fail-safe for source-owned
    skills — a caller that does neither freezes every base skill at dest
    (the 2026-07-19..31 partial-plant incident, g-115-2739 overreach).
    """
    if not rel.startswith(".claude/skills/"):
        return False
    if protect_all_skills:
        return True
    for prefix in forged_prefixes:
        if rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def _living_dest_preserve_predicate(dest_root: Path, source_skill_names=None):
    """Return a predicate rel->bool: True if an include-set member *rel* must be
    KEPT at a living-prod destination instead of overwritten by copy-staged/swap.

    Unifies the preservation already enforced by do_clean_cruft (its
    _should_preserve closure) and do_remove_orphans so the PLANT step
    (copy-staged) and the PLAN report (do_plan §1) agree on exactly which
    deployment-local / dest-owned paths survive under --living-prod. The union
    is: in-repo store roots + _DEPLOYMENT_LOCAL_FILES + .seed-backup-* +
    _ORPHAN_SCAN_SKIP_TOP + _OPERATIONAL_DIRS (via _is_preserved_at_dest) and
    dest-owned forged skills (via _is_protected_dest_skill). The predicate is
    existence-agnostic; callers gate on `(dest_root/rel).is_file()` so a file
    ABSENT at dest is still planted fresh while a PRESENT one is preserved.

    *source_skill_names* (skill-dir names present in the SOURCE include set)
    bounds the skill protection: a skill actively promoted from source is a
    BASE skill, not a dest-owned one, and must stay plantable — under BOTH
    protection branches (readable registry AND the protect_all_skills
    fail-safe). Without this bound the g-115-2739 SKILL.md-presence union
    marks EVERY dest skill dir protected, freezing all base SKILL.md files
    at the destination forever: measured 2026-07-31, Claude-Mind's 51 base
    skills sat frozen at 2026-07-19 content across three consecutive plants
    (#13, #14, v2.8.7) while core/ files planted normally, and the freeze
    is self-concealing because each partial plant makes the dest copy look
    MORE dest-ahead to the next plan verdict. The sweep/deletion lanes
    (do_clean_cruft, do_remove_orphans) never needed this bound — they gate
    on the include set BEFORE consulting skill protection. Dest-owned skills
    (forged/domain — absent from the source include set) remain protected
    exactly as before (omni orphan guard 2026-06-06; root cause A,
    g-115-2738).
    """
    store_tops = _in_repo_store_tops(dest_root)
    dest_forged = _dest_forged_skill_names(dest_root)
    protect_all_skills = dest_forged is None
    src_skills = frozenset(source_skill_names or ())
    # SKILL.md-present skills are protected too (root cause A, ) so the
    # PLANT/copy-staged step and the PLAN report agree with the sweep lanes —
    # MINUS source-owned base skills (see docstring).
    _skill_names = ((set() if dest_forged is None else set(dest_forged))
                    | _dest_skill_names_with_skillmd(dest_root)) - src_skills
    forged_prefixes = {f".claude/skills/{n}" for n in _skill_names}

    def _skill_dir_name(rel: str):
        # parts len 4 = a member under a skill dir (parts[2] is the name);
        # len 3 = a top-level index file (_triggers.yaml/_tree.yaml), not a
        # skill dir — those keep the legacy protection branches below.
        if not rel.startswith(".claude/skills/"):
            return None
        parts = rel.split("/", 3)
        return parts[2] if len(parts) == 4 else None

    def _pred(rel: str) -> bool:
        if _skill_dir_name(rel) in src_skills:
            # Source-owned base skill: plantable. Only the general
            # store-root/deployment-local protection still applies.
            return _is_preserved_at_dest(rel, store_tops)
        return (_is_preserved_at_dest(rel, store_tops)
                or _is_protected_dest_skill(rel, forged_prefixes, protect_all_skills))

    return _pred


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

    Orphans are ARCHIVED BEFORE DELETION (archive-before-delete.md, g-115-4471).
    An orphan is untracked at the destination by construction — git holds no
    copy — so a bare unlink is unrecoverable. Each removal cycle enumerates the
    orphan set (path + bytes + sha256), copies it to
    `<dest>/.seed-backup-orphans-<timestamp>/`, verifies every copy byte-for-byte,
    writes a RECEIPT.json with restore instructions, and only then deletes.
    The sequence fails CLOSED: if any file fails to archive or verify, or the
    receipt cannot be written, NOTHING is deleted and `removed` comes back empty.
    Note `do_backup()` does not cover this case — it archives the manifest
    include-set, i.e. the files that get OVERWRITTEN, never the ones deleted here.

    Returns {"removed": [...], "kept_preserved_count": <int>, "dry_run": bool,
    "archive": {"archived", "path", "verified", "count", "bytes", "failures"}}
    — note the preserved paths are returned as a COUNT, not a list (callers
    needing the preserved sublist must recompute it; see do_plan §5).
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
    # Union registry-forged names with SKILL.md-present names so a
    # promoted-but-unregistered real skill survives too (root cause A,
    # ). When protect_all_skills is True every skill is already
    # preserved, so the union only matters when the registry IS locatable.
    _skill_names = (set() if dest_forged is None else set(dest_forged)) \
        | _dest_skill_names_with_skillmd(dest_root)
    forged_prefixes = {f".claude/skills/{n}" for n in _skill_names}

    # In-repo world/meta stores (e.g. .mind-data/) — dynamic protection.
    # The static _ORPHAN_SCAN_SKIP_TOP covers known names; this resolves the
    # ACTUAL configured roots from every dest agent local-paths.conf so a
    # renamed store can never be swept (2026-07-07 ZDS wipe, fb3634a era).
    store_tops = _in_repo_store_tops(dest_root)

    removed = []
    preserved = []
    candidates = []          # rel paths that WOULD be removed (enumerate first)

    # Walk destination, collecting all files outside the preserve set.
    # NOTE: this pass no longer deletes. Deletion happens only after the
    # orphan set has been archived and the archive VERIFIED — see below.
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
        candidates.append(rel)

    # ── ENUMERATE -> ARCHIVE -> VERIFY -> DELETE -> RECEIPT ──────────────
    # .claude/rules/archive-before-delete.md. An orphan is a file the
    # destination holds and the source does not, so it is UNTRACKED there by
    # construction: git has no copy and deletion is unrecoverable. do_backup()
    # does NOT cover these — it archives the manifest include-set, i.e. exactly
    # the files that get OVERWRITTEN, never the ones that get DELETED.
    #
    # Graveyard name starts with ".seed-backup-" deliberately: that prefix is
    # already preserved by _is_preserved_at_dest (and by the empty-dir pass
    # below), so the archive cannot be swept by a later plant. No new preserve
    # rule is introduced.
    archive: dict = {"archived": False, "path": None, "verified": False,
                     "count": 0, "bytes": 0, "failures": []}

    if dry_run:
        removed = candidates
    elif not candidates:
        removed = []
    else:
        # MICROSECONDS are load-bearing, not cosmetic. At second resolution two
        # sweeps in the same wall-clock second share one graveyard dir, and the
        # second run's RECEIPT.json OVERWRITES the first's — the archived FILES
        # survive but the receipt rows documenting them do not, which is exactly
        # archive-before-delete.md's "an archive nobody can find or restore from
        # is not an archive". Measured on the second-resolution version
        # ( fresh-eyes probe): two back-to-back sweeps produced ONE
        # graveyard and sweep 1's row for core/first.py was gone from the
        # receipt while the file itself sat beside it, unlisted. Sequential
        # re-entry is the real case (retry, plan-then-apply, tests), and
        # microsecond precision closes it.
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S-%f")
        graveyard = dest_root / f".seed-backup-orphans-{timestamp}"
        # ...on a clock that actually ticks per microsecond. Windows' does not
        # (~1-15 ms), so %f repeats across two back-to-back sweeps and both land
        # in ONE graveyard again — the second RECEIPT.json overwriting the first,
        # exactly the defect the precision was added for. Measured 2026-09-02
        # (DESKTOP-O91DLK2, test_graveyard_survives_a_subsequent_sweep red
        # in-suite, green solo). Uniqueness is a property of the DIRECTORY, so
        # check the directory: a taken name gets a counter suffix.
        n = 1
        while graveyard.exists():
            n += 1
            graveyard = dest_root / f".seed-backup-orphans-{timestamp}-{n}"
        entries = []
        failures = []

        # ENUMERATE + ARCHIVE (copy2, never move — a move IS a delete of the
        # original, which would leave nothing to verify against).
        for rel in candidates:
            src = dest_root / rel
            try:
                raw = src.read_bytes()
            except (OSError, FileNotFoundError) as exc:
                failures.append({"path": rel, "stage": "read", "error": str(exc)})
                continue
            digest, size = _sha256(raw), len(raw)
            target = graveyard / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            except (OSError, shutil.Error) as exc:
                failures.append({"path": rel, "stage": "copy", "error": str(exc)})
                continue
            entries.append({"path": rel, "bytes": size, "sha256": digest})

        # VERIFY the archive against the enumeration — full re-read, per file,
        # never a sample. Only files that verify become eligible for deletion.
        verified_rels = []
        for e in entries:
            copy_at = graveyard / e["path"]
            try:
                got = copy_at.read_bytes()
            except (OSError, FileNotFoundError) as exc:
                failures.append({"path": e["path"], "stage": "verify-read",
                                 "error": str(exc)})
                continue
            if len(got) != e["bytes"] or _sha256(got) != e["sha256"]:
                failures.append({"path": e["path"], "stage": "verify-mismatch",
                                 "error": "bytes/sha256 differ from source"})
                continue
            verified_rels.append(e["path"])

        archive.update({
            "archived": True,
            "path": str(graveyard),
            "verified": not failures,
            "count": len(verified_rels),
            "bytes": sum(e["bytes"] for e in entries
                         if e["path"] in set(verified_rels)),
            "failures": failures,
        })

        # RECEIPT alongside the archive, written BEFORE any deletion so an
        # interrupt leaves the recoverable state, not the destroyed one.
        receipt = {
            "event": "seed-transplant orphan removal",
            "created": datetime.now().isoformat(timespec="seconds"),
            "dest_root": str(dest_root),
            "source_root": str(source_root),
            "rationale": "Files present at destination but absent from the "
                         "manifest-resolved include set (mirror semantics: "
                         "destination = manifest AND source).",
            "enumerated": len(candidates),
            "archived_verified": len(verified_rels),
            "failures": failures,
            "entries": entries,
            "restore": {
                "how": "Copy a file back from this directory to the same "
                       "relative path under dest_root.",
                "do_not": "Do NOT restore into the source repo, and do NOT "
                          "restore a path that the manifest still excludes — "
                          "the next plant would re-detect it as an orphan and "
                          "archive-then-delete it again.",
                "example": "cp <this-dir>/<rel-path> <dest_root>/<rel-path>",
            },
        }
        try:
            graveyard.mkdir(parents=True, exist_ok=True)
            (graveyard / "RECEIPT.json").write_text(
                json.dumps(receipt, indent=2), encoding="utf-8")
        except OSError as exc:
            failures.append({"path": "RECEIPT.json", "stage": "receipt",
                             "error": str(exc)})
            archive["failures"] = failures
            archive["verified"] = False

        # DELETE — fail CLOSED. If ANY file failed to archive or verify, or the
        # receipt could not be written, delete NOTHING. A partial sweep with an
        # incomplete archive is the exact failure this block exists to prevent;
        # leaving orphans in place is recoverable, deleting them is not.
        if archive["verified"]:
            for rel in verified_rels:
                try:
                    (dest_root / rel).unlink()
                    removed.append(rel)
                except (OSError, FileNotFoundError):
                    pass
        else:
            removed = []

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
            "dry_run": dry_run, "archive": archive}


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
# Plan — read-only blast-radius report ( / , P0 keystone)
# ============================================================================
# A pre-promote observability pass. It NEVER mutates: it re-derives what each
# real plant step WOULD do for a given (manifest, source, dest, living_prod)
# tuple using the SAME helpers those steps use, then classifies the blast
# radius into six sections + a verdict. Automates the manual guard-119/121
# pre-promote review the operator otherwise does by eye.
#
# Grounding facts the sections encode (verified 2026-07-14 against the live
# engine — the  spec was authored from an older ZDS snapshot and is
# partly stale; these reflect CURRENT behavior):
#   * copy-staged + swap take NO preserve flag — they overwrite the full
#     include set unconditionally. So deployment-local files IN the include
#     set are overwritten regardless of --living-prod (spec Bug #1, live).
#   * clean-cruft preservation is --living-prod-gated (store roots excepted:
#     those are unconditional). Without the flag, operational dirs matched by
#     a cruft pattern (core/logs/, mind_api/state/, ...) ARE deleted (Bug #2).
#   * remove-orphans protects dest-owned forged skills + store roots
#     UNCONDITIONALLY (commits 3e3a7a5d7 + 7988999ca) — the ZDS forged-skill
#     wipe vector is closed there.

def _substantive_lines(text: str) -> set:
    """Line set for divergence detection — trailing ws stripped, blanks dropped.

    Post-transform comparison already neutralizes the brand/env-var rename
    (gotcha #4); dropping blank/whitespace-only lines further suppresses pure
    formatting noise so a reported dest-only line is genuine content.
    """
    out = set()
    for ln in text.splitlines():
        s = ln.rstrip()
        if s.strip():
            out.add(s)
    return out


# Dest HEAD subject that proves the last write was a promote-PR plant.
# Same pattern promotion-plan-triage.py keys on — kept in sync deliberately;
# both answer "was the last thing to touch this repo a plant?".
_PROMOTE_MERGE_RE = re.compile(
    r"Merge pull request #\d+ .*promote/(?P<tag>v\d+\.\d+\.\d+)")


def _dest_frozen_at_last_plant(dest_root: Path) -> dict:
    """Repo-level proof that NOTHING at dest can be locally authored.

    True only when dest HEAD *is* the newest promote-PR merge AND the tree is
    clean — i.e. zero commits since the last plant. Under that condition every
    "dest-only line" is necessarily seed-forward-motion (the frontier moved at
    the source), not prod authorship, so flagging it as prod-ahead is a false
    positive. This is discriminator (1) of g-115-4389; it cleared 18/18 flags
    at the staging hop of v2.8.10 where staging is unstaffed.

    FAILS CLOSED (guard-487): a suppression gate whose input cannot be read
    must not suppress. Any missing git, non-zero rc, timeout, or decode fault
    returns frozen=False, which leaves every flag standing and preserves the
    DO-NOT-PROMOTE verdict. The dangerous direction here is a silent excusal,
    never a spurious block.
    """
    ev = {"frozen": False, "head_subject": "", "dirty_files": 0, "error": None}
    try:
        def _git(*args: str) -> str:
            r = subprocess.run(
                ["git", "-C", str(dest_root), *args],
                capture_output=True, text=False, check=False, timeout=30,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"git {' '.join(args)} rc={r.returncode}: "
                    f"{r.stderr.decode('utf-8', errors='replace')[:120]}")
            return r.stdout.decode("utf-8", errors="replace").strip()

        head_subject = _git("log", "-1", "--format=%s")
        dirty = _git("status", "--porcelain")
    except Exception as exc:                      # noqa: BLE001 — fail closed
        ev["error"] = f"{type(exc).__name__}: {exc}"
        return ev

    ev["head_subject"] = head_subject
    ev["dirty_files"] = len(dirty.splitlines()) if dirty else 0
    ev["frozen"] = bool(_PROMOTE_MERGE_RE.search(head_subject)) and not dirty
    return ev


def _compare_dest_vs_seed(rel: str, source_root: Path, dst: Path,
                          transformations: list):
    """(diverged, dest_only_line_count) — dest vs POST-TRANSFORM seed content.

    Transform the source first (reusing xform.transform_file, exactly as
    copy-staged/diff/verify-integrity do) so the MIND_->MIND_ style rename is
    applied before comparison and never shows as a spurious diff. dest_only is
    the count of substantive lines present at dest but absent from the
    transformed seed — the prod-ahead (back-port-up) signal.
    """
    src = source_root / rel
    try:
        raw = src.read_bytes()
    except OSError:
        return (False, 0)
    if xform.is_binary_path(rel) or xform.is_likely_binary_content(raw):
        try:
            return (raw != dst.read_bytes(), 0)  # no line-diff for binary
        except OSError:
            return (False, 0)
    try:
        content = raw.decode("utf-8")
        transformed, _, _ = xform.transform_file(rel, content, transformations, source_root)
        dst_text = dst.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return (False, 0)
    if transformed == dst_text:
        return (False, 0)
    dest_only = _substantive_lines(dst_text) - _substantive_lines(transformed)
    return (True, len(dest_only))


def _cruft_protection_class(rel: str, forged_prefixes: set,
                            protect_all_skills: bool, store_tops: set):
    """Name why *rel* SHOULD survive a cruft sweep, or None if it is plain cruft.

    Ordered most-specific-first so the report labels the strongest reason.
    """
    if rel.split("/", 1)[0] in store_tops:
        return "in-repo-store"
    if _is_protected_dest_skill(rel, forged_prefixes, protect_all_skills):
        return "forged-skill"
    if rel in _DEPLOYMENT_LOCAL_FILES:
        return "deployment-local"
    for op_dir in _OPERATIONAL_DIRS:
        if rel == op_dir or rel.startswith(op_dir + "/"):
            return "operational-dir"
    return None


def do_plan(source_root: Path, dest_root: Path, manifest: dict,
            living_prod: bool = False) -> dict:
    """Read-only blast-radius plan for a prospective seed-plant to *dest_root*.

    Returns a JSON-serializable dict with six sections + a verdict. NEVER
    writes: do_remove_orphans is invoked dry_run=True; every other helper is a
    pure read.
    """
    include_set = resolve_include_set(manifest, source_root)
    include_lookup = set(include_set)
    transformations = manifest.get("transformations", [])
    cruft_patterns = manifest.get("cruft_patterns", [])

    store_tops = _in_repo_store_tops(dest_root)
    dest_forged = _dest_forged_skill_names(dest_root)
    protect_all_skills = dest_forged is None
    # Registry-forged UNION SKILL.md-present (root cause A, ) so the
    # plan's protection classes match what the sweep lanes actually apply.
    skillmd_names = _dest_skill_names_with_skillmd(dest_root)
    _skill_names = (set() if dest_forged is None else set(dest_forged)) | skillmd_names
    forged_prefixes = {f".claude/skills/{n}" for n in _skill_names}

    # ── §1 DEPLOYMENT-LOCAL OVERWRITES ──
    # Deployment-local include files present at dest. Under --living-prod,
    # copy-staged SKIPS these (Bug #1 fixed) so they are PRESERVED, not
    # overwritten — the overwrite list goes empty and the divergent dest
    # content is kept. Without the flag, copy/swap overwrite them (warned).
    dl_overwrites = []
    dl_preserved = []
    for rel in include_set:
        if rel not in _DEPLOYMENT_LOCAL_FILES:
            continue
        dst = dest_root / rel
        if not dst.is_file():
            continue  # absent at dest -> nothing to overwrite (planted fresh)
        diverged, _ = _compare_dest_vs_seed(rel, source_root, dst, transformations)
        entry = {"rel": rel, "diverged": diverged}
        if living_prod:
            dl_preserved.append(entry)   # copy-staged skips -> dest kept
        else:
            dl_overwrites.append(entry)
    dl_diverged = [d for d in dl_overwrites if d["diverged"]]

    # ── §2 CRUFT-SWEEP DELETIONS (Bug #2/#3 — clean-cruft, flag-gated) ──
    # For each cruft pattern present at dest, would clean-cruft delete it under
    # the current flag, and does it carry a protection class it SHOULD keep?
    # LIMITATION: glob patterns (containing * or ?) are NOT expanded here — a
    # literal `dest/.active-agent-*` never .exists() — so only EXACT-PATH cruft
    # patterns are classified. Safe for the current manifest (every
    # protected-class pattern — skill dirs, operational dirs — is exact-path;
    # the globs are all disposable session-state cruft). A future protected
    # path matched only by a glob would be under-reported here; add glob
    # expansion if the manifest ever gains one.
    cruft_deletions = []
    for p in cruft_patterns:
        target = dest_root / p.rstrip("/")
        if not target.exists():
            continue
        rel = _norm(str(target.relative_to(dest_root)))
        cls = _cruft_protection_class(rel, forged_prefixes, protect_all_skills, store_tops)
        # Replicate do_clean_cruft._should_preserve for this flag:
        if rel.split("/", 1)[0] in store_tops:
            would_delete = False           # store roots: unconditional keep
        elif rel in include_lookup:
            would_delete = False           # source-include member: never cruft ()
        elif not living_prod:
            would_delete = True            # no flag -> nothing else preserved
        else:
            would_delete = not (_is_preserved_at_dest(rel)
                                or _is_protected_dest_skill(rel, forged_prefixes, protect_all_skills))
        cruft_deletions.append({"pattern": p, "rel": rel,
                                "protection_class": cls,
                                "would_delete": would_delete})
    # Dangerous = would be deleted AND (carries a protection class it should
    # keep, OR is a .claude/skills/<name>/ dir). A skill dir is domain
    # capability, never disposable session-state cruft — so a would-delete
    # skill-dir sweep is ALWAYS dangerous, even when it carries NO
    # protection_class (i.e. it is neither a dest-registered forged skill nor a
    # source-include base skill). Without the skills-prefix clause an
    # unregistered-at-dest forged skill (protection_class=None, would_delete=
    # True) lands in cruft_deletions["all"] but is EXCLUDED from this headline +
    # the verdict — the exact blind spot that let a --living-prod plant delete
    # .claude/skills/notify-user/ at ZDS while the plan reported "0 dangerous
    # cruft-sweeps" (; recurrence of the v2.4.0 SES-transport
    # deletion). Skill-dir removal is orphan-removal's jurisdiction (stronger
    # protection: it also gates on `rel in expected`); surfacing any skill-dir
    # cruft-sweep here makes the plan faithful to what clean-cruft actually does.
    cruft_dangerous = [
        c for c in cruft_deletions
        if c["would_delete"]
        and (c["protection_class"] or c["rel"].startswith(".claude/skills/"))
    ]

    # ── §3 PROD-AHEAD FRAMEWORK FILES (guard-119 — hard DO-NOT-PROMOTE) ──
    # Framework (non-deployment-local) include files present at both where dest
    # carries substantive lines the transformed seed lacks -> downstream is
    # AHEAD; promoting would overwrite a downstream framework change. Back-port
    # UP instead. Deliberately conservative (over-flags toward caution).
    prod_ahead = []
    for rel in include_set:
        if rel in _DEPLOYMENT_LOCAL_FILES:
            continue  # handled in §1
        dst = dest_root / rel
        if not dst.is_file():
            continue
        diverged, dest_only = _compare_dest_vs_seed(rel, source_root, dst, transformations)
        if diverged and dest_only > 0:
            prod_ahead.append({"rel": rel, "dest_only_lines": dest_only})
    prod_ahead.sort(key=lambda e: e["dest_only_lines"], reverse=True)

    # ── AUTO-EXCUSAL: repo-level dest-frozen proof () ──
    # A flag means "dest carries lines the seed lacks", which has more than one
    # cause. When dest is provably frozen at the last plant, prod authorship is
    # impossible, so every flag is seed-forward-motion. Excused entries are
    # RE-LABELLED and still reported (§3), never dropped: a detector that goes
    # quiet is indistinguishable from one that was fixed (guard-2499).
    dest_frozen = _dest_frozen_at_last_plant(dest_root)
    seed_motion_excused = []
    if prod_ahead and dest_frozen["frozen"]:
        for e in prod_ahead:
            e = dict(e)
            e["excused_by"] = "dest-frozen"
            e["evidence"] = (
                f"dest HEAD is the promote-PR merge "
                f"('{dest_frozen['head_subject'][:70]}') with a clean tree — "
                f"0 commits since the plant, so no dest-only line can be "
                f"prod-authored")
            seed_motion_excused.append(e)
        prod_ahead = []

    # ── §4 STORE BLAST RADIUS (guard-121) ──
    store_status = {
        "in_repo_store_roots": sorted(store_tops),
        "protected": True,   # store_tops protection is unconditional in both sweeps
        "layout": "in-repo" if store_tops else "external (no in-repo store roots at dest)",
    }

    # ── §5 ORPHAN DELETIONS (dry-run — all preservation already applied) ──
    # do_remove_orphans returns only a COUNT of preserved paths, so the
    # dest-owned forged-skill sublist is recomputed here directly: a skill in
    # the dest registry whose dir exists at dest but is absent from the source
    # include set is one that orphan-removal preserves UNCONDITIONALLY (the ZDS
    # forged-skill-wipe class closed by commit 3e3a7a5d7). Note a skill can be
    # BOTH preserved here (orphan sweep) AND deletable in §2 (a cruft pattern
    # matches it and --living-prod was not passed) — two distinct sweep steps.
    orphan = do_remove_orphans(dest_root, manifest, source_root, dry_run=True)
    real_orphans = orphan.get("removed", [])
    # Itemize every dest skill dir orphan-removal preserves though it is absent
    # from the source include set: registry-forged AND SKILL.md-present
    # (root cause A, ). Both classes are protected by do_remove_orphans.
    forged_preserved = []
    for name in sorted(_skill_names):
        prefix = f".claude/skills/{name}/"
        if (dest_root / ".claude" / "skills" / name).exists() \
           and not any(f.startswith(prefix) for f in include_set):
            forged_preserved.append(f".claude/skills/{name}")

    # ── §6 OPERATIONAL DIRS + DEST-BEHIND ──
    op_dirs_present = []
    for op_dir in sorted(_OPERATIONAL_DIRS):
        if (dest_root / op_dir).exists():
            # At risk only if a cruft pattern would delete it under this flag.
            at_risk = any(c["rel"] == op_dir and c["would_delete"] for c in cruft_deletions)
            op_dirs_present.append({"dir": op_dir, "at_risk": at_risk})
    diff = do_diff(source_root, dest_root, manifest)
    dest_behind = {
        "missing_at_dest": len(diff.get("missing_at_dest", [])),
        "modified": len(diff.get("modified", [])),
        "identical": diff.get("identical_count", 0),
    }

    # ── VERDICT ──
    if prod_ahead:
        verdict = "DO NOT PROMOTE"
        verdict_reason = (f"{len(prod_ahead)} framework file(s) are prod-ahead "
                          f"(dest carries lines the seed lacks) — back-port up first")
    elif dl_diverged or cruft_dangerous or real_orphans:
        parts = []
        if dl_diverged:
            parts.append(f"{len(dl_diverged)} diverged deployment-local overwrite(s)")
        if cruft_dangerous:
            parts.append(f"{len(cruft_dangerous)} protected path(s) cruft-swept")
        if real_orphans:
            parts.append(f"{len(real_orphans)} real orphan deletion(s)")
        verdict = "REVIEW REQUIRED"
        verdict_reason = " | ".join(parts)
    else:
        verdict = "SAFE"
        verdict_reason = "no diverged deployment-local, no protected-path deletion, no real orphans"

    return {
        "version": 1,
        "source_root": str(source_root),
        "dest_root": str(dest_root),
        "living_prod": living_prod,
        "include_count": len(include_set),
        "sections": {
            "deployment_local_overwrites": {
                "all": dl_overwrites, "diverged": dl_diverged,
                "preserved": dl_preserved,
            },
            "cruft_sweep_deletions": {
                "all": cruft_deletions, "dangerous": cruft_dangerous,
            },
            "prod_ahead_framework_files": prod_ahead,
            "seed_motion_excused": seed_motion_excused,
            "dest_frozen_at_last_plant": dest_frozen,
            "store_blast_radius": store_status,
            "orphan_deletions": {
                "real_orphans": real_orphans,
                "forged_skills_preserved": forged_preserved,
            },
            "operational_dirs_and_dest_behind": {
                "operational_dirs_present": op_dirs_present,
                "dest_behind": dest_behind,
            },
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }


def _render_plan_report(plan: dict) -> str:
    """Human-readable text rendering of a do_plan dict (for the operator)."""
    s = plan["sections"]
    L = []
    L.append("═══ SEED-PLANT BLAST-RADIUS PLAN (read-only) ═══════════════════")
    L.append(f"source     : {plan['source_root']}")
    L.append(f"dest       : {plan['dest_root']}")
    L.append(f"living-prod: {plan['living_prod']}   include-set: {plan['include_count']} files")
    L.append("")

    # §1
    dl = s["deployment_local_overwrites"]
    if plan["living_prod"]:
        L.append("§1 DEPLOYMENT-LOCAL OVERWRITES — --living-prod preserves these at dest (Bug #1 fixed)")
        preserved = dl.get("preserved", [])
        if not preserved:
            L.append("   no deployment-local files present at dest — clean")
        else:
            for d in preserved:
                tag = "diverged (dest content KEPT)" if d["diverged"] else "identical"
                L.append(f"   {d['rel']}  →  preserved ({tag})")
    else:
        L.append("§1 DEPLOYMENT-LOCAL OVERWRITES — copy/swap overwrite these without --living-prod (Bug #1)")
        if not dl["all"]:
            L.append("   none in include-set present at dest — clean")
        else:
            for d in dl["all"]:
                tag = "⚠ DIVERGED (KEEP DEST — pass --living-prod)" if d["diverged"] else "identical (harmless)"
                L.append(f"   {d['rel']}  →  {tag}")
    L.append("")

    # §2
    cs = s["cruft_sweep_deletions"]
    L.append("§2 CRUFT-SWEEP DELETIONS — clean-cruft (--living-prod-gated; store roots unconditional)")
    if not cs["all"]:
        L.append("   no cruft patterns present at dest")
    else:
        for c in cs["all"]:
            if c["would_delete"] and c["protection_class"]:
                L.append(f"   ⚠ {c['rel']}  →  WOULD DELETE (protected class: {c['protection_class']}) — pass --living-prod")
            elif c["would_delete"]:
                L.append(f"   {c['rel']}  →  delete (plain cruft, expected)")
            else:
                L.append(f"   {c['rel']}  →  preserved ({c['protection_class'] or 'store'})")
    L.append("")

    # §3
    pa = s["prod_ahead_framework_files"]
    # .get() — a summary produced before  carries neither key.
    excused = s.get("seed_motion_excused") or []
    frozen = s.get("dest_frozen_at_last_plant") or {}
    L.append("§3 PROD-AHEAD FRAMEWORK FILES — dest carries lines the seed lacks → BACK-PORT UP (guard-119)")
    if not pa:
        L.append("   none — no framework file is ahead at dest")
    else:
        for e in pa:
            L.append(f"   ⛔ {e['rel']}  ({e['dest_only_lines']} dest-only line(s)) — DO NOT PROMOTE OVER")
    if excused:
        L.append(f"   ── {len(excused)} flag(s) AUTO-EXCUSED as SEED-MOTION (g-115-4389) ──")
        L.append(f"   REPO-LEVEL PROOF: dest HEAD is the promote-PR merge "
                 f"('{frozen.get('head_subject', '')[:70]}'), tree clean, 0 commits "
                 f"since — no dest-only line can be prod-authored.")
        for e in excused:
            L.append(f"   ✓ {e['rel']}  ({e['dest_only_lines']} dest-only line(s)) — "
                     f"SEED-MOTION, not prod-ahead")
    elif pa and frozen.get("error"):
        # Fail-closed path made visible: the excusal COULD not run, so these
        # flags may include seed-motion. Say so rather than letting the
        # operator read a full block as if the check had cleared them.
        L.append(f"   (auto-excusal unavailable — {frozen['error']}; "
                 f"flags above are unclassified, run promotion-plan-triage.sh)")
    L.append("")

    # §4
    st = s["store_blast_radius"]
    L.append("§4 STORE BLAST RADIUS — in-repo world/meta data stores (guard-121)")
    L.append(f"   layout: {st['layout']}")
    if st["in_repo_store_roots"]:
        L.append(f"   roots : {', '.join(st['in_repo_store_roots'])}  →  PROTECTED (unconditional, both sweeps)")
    L.append("")

    # §5
    od = s["orphan_deletions"]
    L.append("§5 ORPHAN DELETIONS — files at dest absent from include-set (all preservation applied)")
    L.append(f"   real orphans to delete: {len(od['real_orphans'])}")
    for r in od["real_orphans"][:12]:
        L.append(f"     - {r}")
    if len(od["real_orphans"]) > 12:
        L.append(f"     … +{len(od['real_orphans']) - 12} more")
    if od["forged_skills_preserved"]:
        L.append(f"   dest-owned forged-skill paths PROTECTED from orphan-sweep: {len(od['forged_skills_preserved'])}")
    L.append("")

    # §6
    ob = s["operational_dirs_and_dest_behind"]
    L.append("§6 OPERATIONAL DIRS + DEST-BEHIND")
    if ob["operational_dirs_present"]:
        for o in ob["operational_dirs_present"]:
            tag = "⚠ AT RISK (would be cruft-deleted — pass --living-prod)" if o["at_risk"] else "present (safe)"
            L.append(f"   {o['dir']}  →  {tag}")
    else:
        L.append("   no operational dirs present at dest")
    db = ob["dest_behind"]
    L.append(f"   dest-behind: {db['missing_at_dest']} missing + {db['modified']} modified "
             f"({db['identical']} identical) — magnitude of change this plant applies")
    L.append("")

    L.append(f"═══ VERDICT: {plan['verdict']} ═══")
    L.append(f"    {plan['verdict_reason']}")
    L.append("    (read-only report — no files were changed)")
    return "\n".join(L)


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
    sp.add_argument("--preserve-deployment-local", action="store_true",
                    help="--living-prod: skip staging deployment-local/dest-owned "
                         "files that already exist at dest (do not overwrite them)")

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

    # : the git-level exec-bit carry and its verifier. --manifest is
    # accepted (seed-verify's formatter always passes one) and unused: index
    # modes are compared for every path BOTH repos carry, manifest-free.
    sp = sub.add_parser("carry-exec-bits")
    sp.add_argument("--manifest", default=None)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("verify-exec-bits")
    sp.add_argument("--manifest", default=None)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("diff")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)

    sp = sub.add_parser("list-includes")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))

    sp = sub.add_parser("plan")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--source", default=str(PROJECT_ROOT))
    sp.add_argument("--dest", required=True)
    sp.add_argument("--living-prod", dest="living_prod", action="store_true",
                    help="Model the preservation clean-cruft applies with --living-prod")
    sp.add_argument("--json", dest="as_json", action="store_true",
                    help="Emit the raw plan dict instead of the rendered report")

    return p.parse_args()


def main():
    args = _parse_args()
    manifest_path = Path(args.manifest) if getattr(args, "manifest", None) else None
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
        pdl = getattr(args, "preserve_deployment_local", False)
        stats = do_copy_staged(source_root, dest_root, manifest,
                               preserve_deployment_local=pdl)
        print(json.dumps(stats, indent=2))
    elif args.cmd == "swap":
        result = do_swap(dest_root)
        print(json.dumps(result, indent=2))
        if result["failures"]:
            sys.exit(1)
    elif args.cmd == "clean-cruft":
        pdl = getattr(args, "preserve_deployment_local", False)
        result = do_clean_cruft(dest_root, manifest, source_root=source_root,
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
    elif args.cmd == "carry-exec-bits":
        result = carry_index_exec_bits(source_root, dest_root)
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit(1)
    elif args.cmd == "verify-exec-bits":
        result = verify_index_exec_bits(source_root, dest_root)
        print(json.dumps(result, indent=2))
        if not result["pass"]:
            sys.exit(1)
    elif args.cmd == "diff":
        result = do_diff(source_root, dest_root, manifest)
        print(json.dumps(result, indent=2))
    elif args.cmd == "list-includes":
        files = resolve_include_set(manifest, source_root)
        print(json.dumps({"count": len(files), "files": files}, indent=2))
    elif args.cmd == "plan":
        plan = do_plan(source_root, dest_root, manifest,
                       living_prod=getattr(args, "living_prod", False))
        if getattr(args, "as_json", False):
            print(json.dumps(plan, indent=2))
        else:
            print(_render_plan_report(plan))
        # THE EXIT CODE CARRIES THE VERDICT (). Still read-only — the
        # plan mutates nothing — but a report whose refusal is invisible to its
        # caller is not a gate, and this one WAS being read as one.
        #
        # This comment used to read "ALWAYS exit 0. The plan is a report, not a
        # gate; wiring the verdict as a promote GATE is P1.5, not P0." That was
        # a deliberate, correct-at-the-time contract, and it is quoted here to
        # retract it rather than silently replace it. What made it fail was not
        # the contract but the DISAGREEMENT it left standing: promote-to-upstream
        # labelled its own call site "living-prod blast-radius gate" and wrote
        # `|| fail`, so the one consumer believed P1.5 had landed here while this
        # function believed it had not. Each file was internally consistent; the
        # gate existed in neither. Measured cost on Hop 2 (Claude-Mind -> ZDS
        # v2.8.4, 2026-07-30): VERDICT DO NOT PROMOTE printed over 151 prod-ahead
        # files, planted anyway, 142 files lost 1183 lines, 2 genuine casualties
        # restored by hand.
        #
        # Codes are 20/21 rather than the 2-8 range seed-transplant.sh already
        # uses for its own failures, so a verdict can never be confused with a
        # usage error or a mutation fault (the --plan path's own `exit 2` for a
        # missing destination sits in that range). SSOT for the vocabulary:
        #   0  = SAFE
        #   20 = REVIEW REQUIRED  (advisory — caller decides)
        #   21 = DO NOT PROMOTE   (refusal — caller MUST NOT plant unforced)
        # seed-transplant.sh propagates these verbatim; promote-to-upstream.sh
        # aborts on 21 and warns on 20.
        verdict = plan.get("verdict")
        if verdict == "DO NOT PROMOTE":
            return 21
        if verdict == "REVIEW REQUIRED":
            return 20
        return 0


if __name__ == "__main__":
    # sys.exit(main()) — NOT a bare main(). The `plan` branch returns its verdict
    # as an exit code (0/20/21, ); a bare call discards that return and
    # the process exits 0 regardless, which would leave the gate structurally
    # inert while every hand-test still looked green. Every OTHER subcommand
    # returns None, and sys.exit(None) is exit 0, so this changes nothing for
    # them. Verified 2026-07-31: no pre-existing dispatch branch returned a value.
    sys.exit(main())
