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
  2. A candidate .md must be attributable to THIS session. A node stamped with
     another session is a partner's encoding and is skipped; a node carrying no
     attribution the regex can see is FAIL-OPEN — kept — so the g-273-20
     auto-detect is never weakened below its prior behavior for that class.

     COVERAGE, AND IT IS NOT A RESIDUAL ANY MORE (re-measured 2026-09-09,
     g-115-9470, alpha/cc-10, over all 3,079 nodes with THIS module's own
     `_front_matter` + `_SESSION_RE` — a hand-rolled `^session:` grep answers
     wrongly because the real regex allows leading whitespace):

         BLOCK  — `_SESSION_RE` sees the stamp      1,461   47.5%
         INVIS  — stamped, but the anchored regex
                  cannot see it (mid-line, almost
                  all `last_update_trigger: {...}`)    44    1.4%
         NONE   — no `session:` anywhere           1,574   51.1%
         => FAIL-OPEN population (INVIS + NONE)    1,618   52.5%

     This block previously read "measured 2026-07-26: 1197 of 1259 nodes, 96%"
     and called the fail-open population "the residual ~4%". Both are stale by
     an order of magnitude: the corpus grew 1,259 -> 3,079 while the stamped
     fraction HALVED, so the fail-open silently went from admitting a 4%
     residual to admitting a 52.5% MAJORITY (guard-5996 caught the same
     inversion at 3,045 nodes on 2026-09-04).

     Per guard-1835 / guard-5996: this figure carries its DATE and N because a
     fail-open whose safety argument rests on a measured residual is only as
     good as that measurement's age. Re-measure before relying on it —
     `agents/<agent>/temp/g1159470-measure.py` is the predicate-identical
     probe, and re-measure over the SUBSET the caller actually filters on
     (recent-mtime), whose rate can differ sharply from the corpus.

     WHY THE DECAY IS *NOT* THE INLINE-FORM AUTO-FILL GAP (measured, g-115-9470;
     recorded because that hypothesis is the obvious one and chasing it is
     wasted work). tree-front-matter-sync.py deliberately skips the session
     auto-fill for the inline `last_update_trigger: {...}` form (guard-1817 —
     skills emitting it inline set it themselves), so it is a real gap. It is
     just not this one: decomposing the 1,574 NONE nodes gives 1,399 (88.9%)
     with NO `last_update_trigger` key AT ALL, 67 with no front matter, 57
     block-form, 35 scalar-form, and only 16 (1.0%) carrying the inline form.
     The INVIS population has also been FLAT at 44 across 2026-09-04 -> 09-09
     while NONE grew. Closing the inline gap would move ~16 nodes; the decay is
     driven by nodes written by paths that stamp no attribution at all.

Deliberately NOT routed through `_cross_agent_attribution_filter.filter_paths`:
that helper's partner-log source only covers git-tracked working-tree paths
(tree nodes live in the gitignored external world dir), and its mtime sources
skip the concurrent check whenever a file's mtime lands at or after this agent's
own claim — which is exactly when a partner node syncs down. Measured
2026-07-26: it KEEPS a partner-authored node whose mtime is now. RE-VERIFIED
against source 2026-09-09 (g-115-9470): core/scripts/_cross_agent_attribution_filter.py
sets `skip_concurrent = (self_claimed_at > 0) and (mtime + CLOCK_SKEW_SEC >=
self_claimed_at)` and gates Source 1 on `not skip_concurrent`, so a node whose
mtime is at or after this agent's claim never reaches the concurrent-partner
check and is kept.

Directional bias is deliberate. A false positive silently disables the
encoding-drift counter (the g-115-3115 defect); a false negative merely
increments it, which routes to the lightweight log-and-clear path in
aspirations-precheck Phase 0-pre. Prefer the cheap, self-limiting error.

DECISION 2026-09-09 (g-115-9470): `attributable_to_session` KEEPS FAIL-OPEN.
Recorded here rather than left implicit, because the argument above was
calibrated at a 4% residual and now runs against a 52.5% majority, which
inverts its own premise — at this coverage the fail-open produces the
EXPENSIVE direction (silently disabling the counter) by default, so the
paragraph above no longer justifies the branch on its own terms and something
had to be decided rather than inherited.

Flipping to fail-closed was REJECTED on the measurement, not on preference: it
refuses the SAME 1,618 nodes, so it converts over-crediting the majority into
under-crediting the majority — the auto-detect would then fail for every
genuine encoding on an unstamped node and the drift counter would fire almost
continuously, which is the louder failure but not the smaller one. Nor can a
backfill rescue the flip: 1,399 of the 1,574 unstamped nodes carry no
`last_update_trigger` key at all, so nothing on disk records who wrote them and
stamping them would FABRICATE attribution — the one outcome worse than either
error. A flip only becomes correct once stamped coverage is high enough that
the refused set is genuinely residual again, and nothing measured here shows it
recovering. Do NOT read that as "no path raises coverage" — one does, and the
distinction is what a repair would start from: tree-front-matter-sync.py
auto-fills `last_update_trigger.session`, and `_set_nested_scalar` CREATES the
whole block when the key is absent rather than skipping the node, so even a
trigger-less node gets stamped once it passes through that sync. Coverage still
fell 96% -> 47.5% while that path was running, so the population growing here is
the population that never reaches it. WHICH write paths bypass the sync is
UNMEASURED — this goal did not close that, and it is the question a coverage
repair has to answer first.

So the branch is unchanged and the CLAIM is what narrows. This detector answers
"modified after T and NOT PROVABLY another session's" — never "encoded by this
session" (guard-5996's action hint, and the /encode-session Lane 5 incident in
g-115-9470 where `--list` returned 188 nodes for a window that encoded zero).
Callers that need real authorship must check the node's front matter for their
own sid, accepting BOTH the block and inline-dict forms.

LIST MODE (g-115-4714). `--list` answers the question /encode-session Lane 5
actually has — WHICH nodes did I encode — instead of the boolean the
state-update caller needs. It reuses attributable_to_session() rather than
reimplementing attribution, so the two modes cannot drift apart, and its exit
code is defined so that `non-empty list iff detector exits 0` holds by
construction rather than by convention.

The default (no-flag) invocation is BYTE-IDENTICAL to the pre-g-115-4714
behavior — same short-circuit on the first hit, same stdout line, same exit
codes (guard-1479: a script that gains an entry mode must leave the existing
one traced and unchanged; rb-538: the flag is parsed off an explicit
whitelist, so an unknown flag is REFUSED rather than silently dropped into
the timestamp slot).

SCOPE IS TREE-ONLY AND SAYS SO. Conventions are deliberately not covered:
only 6 of 66 carry any front matter, so there is no attribution to filter on
(measured g-115-3392). A caller must present the result as tree-only; the
`--list` header line states it so the scope cannot be lost in the handoff.

Usage:
    py -3 tree-edit-since.py <ISO-8601-timestamp>
    py -3 tree-edit-since.py <ISO-8601-timestamp> --list

Exit codes:
    0 — at least one .md under WORLD_DIR/knowledge/tree was modified after
        the given timestamp AND is attributable to this session
        (auto-detect should set TREE_UPDATED=true). In --list mode the
        attributable nodes are printed to stdout, one relative path per line.
    1 — no such file (or could not parse timestamp / could not access tree
        dir). Caller treats this as "no tree edit detected" and falls through
        to the LLM-passed flag (default false). In --list mode stdout is
        empty.

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
    # Explicit flag whitelist (rb-538): an unknown flag must be REFUSED, not
    # silently dropped — a multi-layer parser that ignores what it does not
    # recognise would accept `--lst` and answer the boolean question while the
    # caller believed it asked for a list.
    args = sys.argv[1:]
    list_mode = False
    positional = []
    for a in args:
        if a == "--list":
            list_mode = True
        elif a.startswith("-"):
            print(f"tree-edit-since: unknown flag {a!r}", file=sys.stderr)
            sys.exit(1)
        else:
            positional.append(a)

    if len(positional) != 1:
        print("usage: tree-edit-since.py <ISO-8601-timestamp> [--list]",
              file=sys.stderr)
        sys.exit(1)

    iso = positional[0].strip()
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

    # Scan .md files. In the default (boolean) mode, short-circuit on the first
    # ATTRIBUTED match — no need to enumerate all 1200+ files when one hit is
    # sufficient signal. In --list mode, enumerate them all: the caller needs
    # the set, not the existence.
    skipped = 0
    found = []
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
        if list_mode:
            found.append(rel)
            continue
        print(f"tree-edit-since: detected {rel} modified after {iso}")
        sys.exit(0)

    # ONE copy of the skipped note, shared by both modes. It was briefly
    # duplicated per-branch; two copies of a message string is the drift
    # hazard, not a convenience.
    if skipped:
        print(
            f"tree-edit-since: {skipped} node(s) modified after {iso} but "
            "attributed to another session — not this agent's encoding",
            file=sys.stderr,
        )

    if list_mode:
        # Scope is stated on the header, not left to the caller to remember
        # (: conventions carry no attribution and are NOT covered).
        if found:
            print(f"tree-edit-since: {len(found)} tree node(s) encoded by this "
                  f"session after {iso} (TREE-ONLY — conventions carry no "
                  f"attribution and are not covered)", file=sys.stderr)
            for rel in sorted(found, key=str):
                print(rel)
        # Exit code is the SAME predicate as the boolean mode, so
        # "non-empty list iff detector exits 0" holds by construction.
        sys.exit(0 if found else 1)

    sys.exit(1)


if __name__ == "__main__":
    main()
