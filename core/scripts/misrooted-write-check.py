#!/usr/bin/env python3
"""BR17 (): find mis-rooted writes that BOTH the L1 hook and git miss.

The L1 path-resolution hook only guards the governed roots (WORLD, META, the
bound agent dir); a write landing elsewhere under PROJECT_ROOT is out of its
scope. That was survivable while such paths stayed visible in `git status` --
but a path matching a RECURSIVE ignore glob is invisible to both, so it can sit
for weeks with nothing reporting it (measured: a stray `core/agents/<name>/
session/` sat 7 days and silently narrowed a roster glob to one entry, rb-5190).

DERIVE THE GLOBS BY CONTENT, NEVER BY LINE NUMBER. The goal that filed this
cited five .gitignore line numbers; four had already drifted by the time it was
executed (108 -> 117, 137 -> 146, 141 -> 150, 173 -> 182), and three of the
stale ones now point at a comment or a blank line. Reading the patterns out of
the file is the same amount of code and cannot rot.

DRIFT IS REPORTED, NOT SWALLOWED. If .gitignore grows a recursive glob this
check has no allowlist reasoning for, that is said out loud. A hygiene check
whose coverage silently narrows as the ignore file grows is worse than none:
its PASS keeps meaning less every month and nothing announces it.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# Sanctioned homes, each with the reason it is NOT a mis-rooted write. A bare
# path list would rot into "things that were noisy once"; the reason is what
# lets the next reader re-derive whether the entry still earns its exemption.
EXEMPT = {
    ".git": "git internals",
    "agents": "agent dirs OWN session/ and sessions/ (CLAUDE.md Agent-dir Resolution)",
    "core/.pycache": (
        "deliberate cache, explicitly ignored (not by a recursive glob). It MIRRORS "
        "ABSOLUTE paths, so it necessarily contains dirs named session/ and sessions/ "
        "belonging to other trees; 27k+ files, so it is pruned during the walk rather "
        "than filtered after"
    ),
    ".claude/.history": (
        "MEASURED sanctioned, not mis-rooted (g-115-3225): _fileops._classify_base "
        "recognises PROJECT/.claude as a first-class base kind, so the writer targets "
        "it by design, and history-list.sh resolves snapshots there (probe-verified). "
        "SEPARATE known defect, deliberately NOT this check's business: no GC sweeps "
        "it -- history.py cmd_prune/cmd_prune_legacy and history-vacuum-tick.sh all "
        "enumerate WORLD and META only, so it grows unbounded"
    ),
}


def _recursive_globs(gitignore: pathlib.Path):
    """Basenames of `**/`-rooted ignore patterns, read from the file itself."""
    names = []
    if not gitignore.exists():
        return names
    for raw in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("**/") or line.startswith("#"):
            continue
        # `**/session/crash-marker` -> guard the DIR component, not the leaf:
        # the leaf is only reachable through a dir this check already walks.
        tail = line[3:].rstrip("/")
        names.append(tail.split("/", 1)[0])
    return sorted(set(names))


def _is_exempt(rel: str) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in EXEMPT)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    names = _recursive_globs(root / ".gitignore")
    if not names:
        print("WARN: no `**/` recursive globs found in .gitignore — this check "
              "derives its target set from that file, so it is currently inert")
        return 0

    # WORLD/META are the sanctioned .history homes. They are EXTERNAL paths and
    # usually live outside PROJECT_ROOT (in which case the walk never reaches
    # them), but on a box where they sit inside it they must not be flagged.
    extra = []
    try:
        from _paths import WORLD_DIR, META_DIR  # noqa: E402
        for d in (WORLD_DIR, META_DIR):
            if not d:
                continue
            try:
                extra.append(str(pathlib.Path(d).resolve().relative_to(root)))
            except ValueError:
                pass  # outside PROJECT_ROOT — the walk cannot reach it anyway
    except Exception:
        pass  # fail-open: a resolver failure must not turn into a false hit

    hits, scanned = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        # Prune DURING the walk. Filtering after would descend 27k cache files.
        keep = []
        for d in dirnames:
            child = f"{rel_dir}/{d}" if rel_dir else d
            if _is_exempt(child) or any(
                    child == e or child.startswith(e + "/") for e in extra):
                continue
            keep.append(d)
        dirnames[:] = keep
        scanned += 1
        for d in dirnames:
            if d in names:
                hits.append((f"{rel_dir}/{d}" if rel_dir else d) + "/")
        for f in filenames:
            if f in names:
                hits.append(f"{rel_dir}/{f}" if rel_dir else f)

    unknown = [n for n in names
               if n not in {"session", "sessions", ".history", "local-paths.conf"}]
    if unknown:
        print(f"NOTE: .gitignore carries recursive glob(s) this check has no "
              f"allowlist reasoning for: {', '.join(unknown)} — extend EXEMPT's "
              f"reasoning or confirm they need none")

    if hits:
        print(f"WARN: {len(hits)} mis-rooted path(s) hidden by a recursive ignore "
              f"glob, outside every sanctioned home ({scanned} dirs scanned): "
              + ", ".join(sorted(hits)[:8]))
    else:
        print(f"PASS: 0 mis-rooted paths under {len(names)} recursive ignore "
              f"glob(s) ({scanned} dirs scanned; {len(EXEMPT)} sanctioned homes "
              f"exempt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
