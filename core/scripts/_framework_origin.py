"""_framework_origin — does THIS deployment take its framework from somewhere else?

A deployment whose registry entry (core/config/environments/<ENVIRONMENT_ID>.yaml)
carries `framework_origin: <env-id>` receives core/, .claude/, mind_api/ and
CLAUDE.md through the promotion train (`.claude/rules/promotion-cycle.md`,
`core/config/conventions/pull-promotion.md` § g) and does not edit them locally.
Two consumers refuse local framework writes on such a deployment:

  path-resolution-hook.py            PreToolUse Write/Edit/MultiEdit — the write
                                     never lands, and the refusal says where the
                                     change goes instead
  check-framework-origin-writes.py   pre-commit Gate 15 — the Bash backstop
                                     (`cat >`, `sed -i`, heredocs walk past L1)

WHY (measured 2026-08-30, coach@zc-03, the third Mind deployment, run by
small-model Bodies): a Body executing /curriculum-gates used edit_file THREE
times on `.claude/skills/curriculum-gates/SKILL.md` to write its step RESULTS
under the step headings — "Gates evaluated: configured=false, all_passed=false,
gates=[] (15.2 complete)" — the skill file as a worksheet. Nothing refused it:
framework paths are agent-editable by design on the dev origin, and the
promotion cycle's "downstream refuses dev work" was honor-system. Left alone the
edit would have been loop-committed and collided with the next upstream merge.

Absent field (or one naming the deployment itself) = this deployment IS a
framework origin: local framework edits stay allowed and git-audited, exactly as
before. Every existing registry entry keeps its behaviour; only an entry that
opts in changes anything. Fail-open throughout: an unreadable registry, an
unknown ENVIRONMENT_ID or an import error all resolve to "origin" (no refusal)
— the dangerous error is blocking the dev loop, never letting one edit through.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Mirrors promotion-preflight.py FRAMEWORK_PATHS at the top level — the unit the
# promotion train carries. agents/, readme/, .env* and the world are not
# framework and stay writable.
FRAMEWORK_PREFIXES = ("core/", ".claude/", "mind_api/")
FRAMEWORK_FILES = frozenset({"CLAUDE.md"})
# Runtime output that happens to live under a framework prefix — never framework.
RUNTIME_PREFIXES = (
    "core/logs/", "core/.pycache/", "core/scripts/.python-shim/", "mind_api/state/",
)


def is_framework_path(rel_path: str) -> bool:
    """True for a repo-relative path the promotion train owns."""
    rel = str(rel_path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return False
    if rel in FRAMEWORK_FILES:
        return True
    if rel.startswith(RUNTIME_PREFIXES):
        return False
    return rel.startswith(FRAMEWORK_PREFIXES)


def self_env_id() -> str | None:
    """ENVIRONMENT_ID of this checkout: the env var, else .env.local via _paths."""
    env_id = os.environ.get("ENVIRONMENT_ID", "").strip()
    if env_id:
        return env_id
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import ENVIRONMENT_ID  # type: ignore
        return str(ENVIRONMENT_ID).strip() if ENVIRONMENT_ID else None
    except Exception:  # noqa: BLE001 — fail open
        return None


def framework_origin(project_root, env_id: str | None = None) -> str | None:
    """The env-id this deployment takes its framework FROM, or None when it is an
    origin itself (field absent, blank, self-referencing, or anything unreadable)."""
    try:
        env_id = (env_id or self_env_id() or "").strip()
        if not env_id:
            return None
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _peer_registry import load_env_registry  # type: ignore
        registry = load_env_registry(Path(project_root) / "core" / "config" / "environments")
        origin = str((registry.get(env_id) or {}).get("framework_origin") or "").strip()
        if not origin or origin == env_id:
            return None
        return origin
    except Exception:  # noqa: BLE001 — fail open
        return None
