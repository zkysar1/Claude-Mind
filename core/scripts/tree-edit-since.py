#!/usr/bin/env python3
"""tree-edit-since.py — detect whether THIS agent encoded a knowledge-tree
node since a given ISO timestamp.

Used by iteration-close.sh do_state_update() to auto-set TREE_UPDATED=true
when the LLM forgot to pass --tree-updated explicitly. Filed by g-273-20
after observing alpha session-60 iter-17/19/20 fire force_tree_encoding=true
three times despite g-273-18 actively encoding tree (because --tree-updated
was LLM-residue and got dropped every iteration).

AUTHORSHIP (g-115-3245). The detector originally answered "did the tree
change?" when the caller needs "did I encode?" — two different questions in
a multi-agent world where own-cloud syncs partner encodings into the local
tree mid-iteration. Two corrections:

  1. `_tree.yaml` is NOT consulted. Its mtime is an ACCESS stamp, not an
     encoding signal: an ordinary (non-`--read-only`) retrieve.sh call rewrites
     it, so the mandatory read path alone keeps it perpetually fresh — measured
     2026-07-26 against an idle control, `--read-only` leaves it untouched.
     It is also a single SHARED index with no front matter, so it can never
     carry authorship even in principle, and it moves whenever any agent's
     encoding arrives. In the originating incident (g-115-3115) it was stamped
     20:37:37, one second before the partner node that actually changed —
     checking it first short-circuited the whole scan on a partner's write.
  2. A candidate .md must be attributable to THIS session. Node front matter
     carries `session:` (measured 2026-07-26: 1197 of 1259 nodes, 96%);
     a node stamped with another session is a partner's encoding and is
     skipped. Nodes carrying no attribution at all (the residual ~4%) are
     FAIL-OPEN — kept — so the g-273-20 auto-detect is never weakened below
     its prior behavior for that class.

Deliberately NOT routed through `_cross_agent_attribution_filter.filter_paths`:
that helper's partner-log source only covers git-tracked working-tree paths
(tree nodes live in the gitignored external world dir), and its mtime sources
skip the concurrent check whenever a file's mtime lands at/after this agent's
own claim — which is exactly when a partner node syncs down. Measured
2026-07-26: it KEEPS a partner-authored node whose mtime is now.

Directional bias is deliberate. A false positive silently disables the
encoding-drift counter (the g-115-3115 defect); a false negative merely
increments it, which routes to the lightweight log-and-clear path in
aspirations-precheck Phase 0-pre. Prefer the cheap, self-limiting error.

Usage:
    py -3 tree-edit-since.py <ISO-8601-timestamp>

Exit codes:
    0 — at least one .md under WORLD_DIR/knowledge/tree was modified after
        the given timestamp AND is attributable to this session
        (auto-detect should set TREE_UPDATED=true).
    1 — no such file (or could not parse timestamp / could not access tree
        dir). Caller treats this as "no tree edit detected" and falls through
        to the LLM-passed flag (default false).

Fail-open: any error returns exit 1 with stderr message — never blocks
state-update.
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR  # type: ignore

# Front matter is small; never read more than this many lines looking for the
# closing delimiter (a node missing its terminator must not cost a full read).
_FRONT_MATTER_MAX_LINES = 40
_SESSION_RE = re.compile(r"^\s*session:\s*(\S+)\s*$", re.MULTILINE)


def _front_matter(path):
    """Return the YAML front-matter block text, '' when the node is READABLE
    but carries no front matter, or None when the node could not be READ.

    The '' / None split is load-bearing (g-115-3250). Both used to return '',
    which collapsed two opposite conditions onto one permissive branch in
    attributable_to_session: a node that genuinely has no session stamp (the
    deliberate legacy fail-open) and a node we never managed to open at all.
    The second must never count as an encoding — the gate would be reporting
    an encoding for an entry it never read.

    The realistic trigger is NOT the contrived unreadable path but the sync
    race: a node removed between the stat that enumerated it and the open
    here, which is exactly the churn window this scan runs in.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return ""
            lines = []
            for line in fh:
                if line.strip() == "---":
                    break
                lines.append(line)
                if len(lines) >= _FRONT_MATTER_MAX_LINES:
                    break
            return "".join(lines)
    except OSError as e:
        # Diagnose rather than swallow: silence here is what let an unread
        # node be reported as a detected encoding.
        print(f"tree-edit-since: unreadable node {path}: {e}", file=sys.stderr)
        return None


def attributable_to_session(path, sid):
    """True when this node is THIS session's encoding, or carries no
    attribution at all (fail-open for legacy nodes with no session stamp).

    False when the node could not be read — an unread node is not evidence
    of anything, and must not be credited as this session's encoding.
    """
    fm = _front_matter(path)
    if fm is None:
        # Unreadable. NOT the legacy no-stamp fail-open below: we have no
        # evidence either way, so do not credit it. (Also guards the regex —
        # _SESSION_RE.search(None) raises TypeError.)
        return False
    match = _SESSION_RE.search(fm)
    if not match:
        return True
    return match.group(1).strip().strip("\"'") == sid


def main():
    if len(sys.argv) != 2:
        print("usage: tree-edit-since.py <ISO-8601-timestamp>", file=sys.stderr)
        sys.exit(1)

    iso = sys.argv[1].strip()
    try:
        # Accepts both "2026-05-04T15:35:00" and "2026-05-04T15:35:00.123"
        cutoff = datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError) as e:
        print(f"tree-edit-since: bad timestamp {iso!r}: {e}", file=sys.stderr)
        sys.exit(1)

    tree_dir = Path(WORLD_DIR) / "knowledge" / "tree"
    if not tree_dir.is_dir():
        print(f"tree-edit-since: no tree dir at {tree_dir}", file=sys.stderr)
        sys.exit(1)

    # _tree.yaml is deliberately NOT checked — it is a shared, unattributable
    # index. See the AUTHORSHIP section of the module docstring.

    # An unset SID means authorship cannot be established at all. Fall back to
    # the pre- behavior (any .md edit counts) rather than reporting
    # "no encoding" for every iteration in an environment without the binding.
    sid = os.environ.get("MIND_SID", "").strip()

    # Scan .md files. Short-circuit on the first ATTRIBUTED match — no need to
    # enumerate all 1200+ files when one hit is sufficient signal.
    skipped = 0
    for md in tree_dir.rglob("*.md"):
        try:
            if md.stat().st_mtime <= cutoff:
                continue
        except OSError as e:
            # Skipping is already the safe direction here (an un-stat-able node
            # is never credited), but diagnose it anyway — : silence
            # in BOTH OSError handlers is what made the sync-race window
            # invisible. Same churn window as _front_matter's.
            print(f"tree-edit-since: unstattable node {md}: {e}", file=sys.stderr)
            continue
        if sid and not attributable_to_session(md, sid):
            skipped += 1
            continue
        rel = md.relative_to(tree_dir)
        print(f"tree-edit-since: detected {rel} modified after {iso}")
        sys.exit(0)

    if skipped:
        print(
            f"tree-edit-since: {skipped} node(s) modified after {iso} but "
            "attributed to another session — not this agent's encoding",
            file=sys.stderr,
        )
    sys.exit(1)


if __name__ == "__main__":
    main()
