# Layer 2 — SERVER: daemon implementation. See core/BOUNDARY.md.
"""framework-runtime — long-running localhost HTTP daemon.

Pre-imports framework helpers once and exposes their functionality over HTTP
so each call collapses from ~5s (bash + python launcher + path resolution)
to ~10ms (single TCP round trip).

Domain-agnostic. See mind_api/src/README.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# Frontier-lead invariant: core(ayoai-mind) >= core(claude-mind) >= core(zds-mind).
# ayoai-mind is the dev frontier — it develops core and promotes upstream, so its
# version always leads. Bumped 0.1.0 -> 0.2.0 (2026-06-04) to encode the lead ahead
# of the naming-overhaul release (the first breaking change to ship through the rails).
__version__ = "2.12.44"


def read_git_head_sha(project_root: Path) -> Optional[str]:
    """Read the current git HEAD SHA from `.git/HEAD` + `.git/refs/...`.

    Pure-Python — never shells out to `git`. Returns None when .git is
    missing/corrupt. Used by the health endpoint so wrappers can detect
    "the running daemon predates the current source code" (g-115-735).

    Resolves the two common cases:
      .git/HEAD == "ref: refs/heads/<branch>\\n"  → reads the ref file
      .git/HEAD == "<40 hex chars>\\n"            → detached HEAD, use directly
    Worktrees have a `.git` FILE pointing at the real gitdir; we follow it.
    """
    try:
        git_path = project_root / ".git"
        if git_path.is_file():
            # Worktree case: `.git` is a file like "gitdir: <abs path>"
            content = git_path.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir = Path(content.split(":", 1)[1].strip())
            else:
                return None
        elif git_path.is_dir():
            gitdir = git_path
        else:
            return None

        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref_path = gitdir / head[5:].strip()
            if not ref_path.is_file():
                # Worktree refs/heads files may live in the common-dir.
                common = gitdir / "commondir"
                if common.is_file():
                    common_dir = (gitdir / common.read_text(encoding="utf-8").strip()).resolve()
                    ref_path = common_dir / head[5:].strip()
            return ref_path.read_text(encoding="utf-8").strip() or None
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            return head
        return None
    except (OSError, UnicodeDecodeError):
        return None
