#!/usr/bin/env python3
"""permissions-add.py — Merge external-path permissions into .claude/settings.local.json.

Called by core/scripts/permissions-add.sh from /start Phase B10. The shell
wrapper resolves WORLD_DIR / META_DIR / PROJECT_ROOT via _paths.sh; this
script handles the JSON manipulation.

Why this lives in core/scripts/ instead of being inlined into /start:
  The LLM cannot Edit/Write/MultiEdit .claude/settings.local.json directly —
  the file's own permissions.deny[] hard-blocks those tool calls (rb-931,
  CLAUDE.md "two-file settings rule" — the constitutional anchor). Bash-level
  filesystem writes via a sanctioned script ARE permitted: the deny patterns
  match Claude tool names (Edit/Write/MultiEdit), not the underlying syscall.

Behavior:
  - File does not exist: create with framework broad allows + constitutional
    deny baseline + the requested path-specific allows.
  - File exists: merge path-specific allows (idempotent), ensure baseline
    deny entries are present (idempotent), preserve everything else
    (env, statusLine, outputStyle, autoMemoryEnabled, user-added allows,
    user-added denies).

Atomic write: tempfile + replace. The file is never left half-written.
Refuses to clobber a file that doesn't parse as JSON.

Origin: 2026-05-19 zeta /start audit ("review start up from scratch" session)
Finding #1 — B10 was hard-blocked by the self-referential deny for any
customer with a pre-existing settings.local.json, and silently produced an
unprotected file for customers without one.
"""

import json
import os
import re
import sys
from pathlib import Path


# Constitutional baseline — these MUST be in permissions.deny[] to make the
# constitutional anchor (rb-931) effective. If creating fresh, seed all.
# If file exists and entries are missing, ADD them. Never remove existing
# deny entries the user or framework added.
BASELINE_DENY = [
    # Auto-memory blocks (the original anti-pattern this defends against —
    # see .claude/rules/no-auto-memory.md)
    "Write(*/.claude/projects/*/memory/*)",
    "Edit(*/.claude/projects/*/memory/*)",
    # Settings-structural-validator self-protection (rb-931 bootstrap paradox)
    "Edit(*core/scripts/settings-structural-validator.py)",
    "Write(*core/scripts/settings-structural-validator.py)",
    "MultiEdit(*core/scripts/settings-structural-validator.py)",
    "Edit(*core/scripts/settings-structural-validator.sh)",
    "Write(*core/scripts/settings-structural-validator.sh)",
    "MultiEdit(*core/scripts/settings-structural-validator.sh)",
    # Self-referential anchor — the keystone that makes everything else safe.
    # Mirrors core/config/conventions/constitutional-rings.md Ring 0.
    "Edit(*settings.local.json)",
    "Write(*settings.local.json)",
    "MultiEdit(*settings.local.json)",
    "Edit(**/.claude/settings.local.json)",
    "Write(**/.claude/settings.local.json)",
    "MultiEdit(**/.claude/settings.local.json)",
]

# Framework broad allows seeded ONLY on fresh-create. These are what the
# agent needs to function at all — Bash for the scripts, Read/Glob/Grep for
# any file, WebSearch/WebFetch for Tier-3 retrieval. Existing files are NOT
# touched beyond the path-specific add (we don't second-guess the user's
# customizations).
FRESH_INSTALL_ALLOWS = [
    "Bash(gh *)",
    "Bash(*)",
    "Read(*)",
    "Glob(*)",
    "Grep(*)",
    "WebSearch(*)",
    "WebFetch(*)",
]


def normalize_path(path: str) -> str:
    """Forward-slashes only. Strip trailing slash."""
    return path.replace("\\", "/").rstrip("/")


def path_match_form(path: str) -> str:
    """Convert a filesystem path to the matcher form Claude Code expects.

    Empirically (from the working settings.local.json on the original
    deployment), absolute Windows paths use a `//C:/...` prefix and POSIX
    absolute paths use the leading `/` as-is. Relative paths pass through.
    """
    if re.match(r"^[A-Za-z]:/", path):
        return "//" + path
    return path


def build_path_allows(path: str, existing_allow: "list[str]") -> "list[str]":
    """For a given external path, return the allow patterns the agent needs
    for Read/Edit/Write/MultiEdit on the path's subtree.

    Skips any pattern whose corresponding tool already has a wildcard allow
    in `existing_allow` — e.g. if `Read(*)` is present, `Read({path}/**)`
    would be redundant.

    Uses /** for recursive matching — the proven-working pattern, not /*.
    """
    match = path_match_form(path)
    wildcards = set(existing_allow)
    patterns = []
    for tool in ("Read", "Edit", "Write", "MultiEdit"):
        if f"{tool}(*)" in wildcards:
            continue
        patterns.append(f"{tool}({match}/**)")
    return patterns


def merge_unique(existing: list, additions: list) -> "tuple[list, list]":
    """Append items from additions that aren't already in existing.
    Returns (existing_list_modified, items_actually_added).
    """
    seen = set(existing)
    added = []
    for item in additions:
        if item not in seen:
            existing.append(item)
            seen.add(item)
            added.append(item)
    return existing, added


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: permissions-add.py <world_path> <meta_path> [<project_root>]",
            file=sys.stderr,
        )
        return 2

    world_path = normalize_path(sys.argv[1])
    meta_path = normalize_path(sys.argv[2])
    project_root = normalize_path(sys.argv[3]) if len(sys.argv) >= 4 else normalize_path(os.getcwd())

    if not world_path or not meta_path:
        print("ERROR: world_path and meta_path are required.", file=sys.stderr)
        return 2

    settings_path = Path(project_root) / ".claude" / "settings.local.json"

    file_existed = settings_path.exists()
    if file_existed:
        try:
            raw = settings_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            print(
                f"ERROR: {settings_path} exists but is not valid JSON: {exc}",
                file=sys.stderr,
            )
            print(
                "       Refusing to clobber. Inspect the file manually and fix the JSON.",
                file=sys.stderr,
            )
            return 3
        if not isinstance(data, dict):
            print(
                f"ERROR: {settings_path} root is not a JSON object — refusing to clobber.",
                file=sys.stderr,
            )
            return 3
    else:
        data = {
            "permissions": {
                "allow": list(FRESH_INSTALL_ALLOWS),
                "deny": [],
            },
            "autoMemoryEnabled": False,
        }

    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        print(
            f"ERROR: {settings_path} permissions is not a dict — refusing to clobber.",
            file=sys.stderr,
        )
        return 3
    allow = perms.setdefault("allow", [])
    deny = perms.setdefault("deny", [])
    if not isinstance(allow, list) or not isinstance(deny, list):
        print(
            f"ERROR: {settings_path} permissions.allow/deny are not lists — refusing to clobber.",
            file=sys.stderr,
        )
        return 3

    # Compute path allows AFTER loading the file so we can skip patterns
    # whose tool already has a wildcard allow (e.g. Read(*) makes
    # Read(path/**) redundant). On fresh-create, `allow` was seeded with
    # FRESH_INSTALL_ALLOWS which includes Read(*) — so the per-path Read
    # entries get skipped on fresh installs too.
    new_path_allows = (
        build_path_allows(world_path, allow)
        + build_path_allows(meta_path, allow)
        + build_path_allows(project_root, allow)
    )

    _, allows_added = merge_unique(allow, new_path_allows)
    _, denies_added = merge_unique(deny, BASELINE_DENY)

    tmp = settings_path.with_suffix(".local.json.tmp")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(settings_path)

    if not file_existed:
        print(f"[permissions-add] Created {settings_path} with framework defaults.")
    else:
        print(f"[permissions-add] Merged into existing {settings_path}.")
    if allows_added:
        print(f"  Added {len(allows_added)} allow rule(s):")
        for a in allows_added:
            print(f"    + {a}")
    if denies_added:
        print(f"  Seeded {len(denies_added)} constitutional deny rule(s):")
        for d in denies_added:
            print(f"    + {d}")
    if not allows_added and not denies_added:
        print("  (no changes needed — all rules already present)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
