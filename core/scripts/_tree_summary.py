"""Bounded LLM-facing projection of the knowledge-tree summary ().

WHY THIS IS A MODULE AND NOT INLINE IN THE LOADER. `load-tree-summary.sh`
previously piped `tree-read.sh --summary` straight to disk, so the projection
had no upper bound and no place to hang a regression pin. A shell pipeline
cannot be imported, so it cannot be unit-tested, so nothing could ever notice
it crossing the Read-tool cap. Two call sites -- the loader and
tests/test_tree_summary_budget.py -- so this is not a single-use abstraction.

THE DEFECT. `world/knowledge/tree/_summary.json` is a per-node projection over
a monotonically growing tree, which has no upper bound at all. Measured
2026-08-11 on cc-08 over 1,372 nodes: 950,359 bytes against a 262,144-byte
Read-tool cap -- 3.63x over. Its callers use the `IF path returned: Read it`
contract, which at 3.63x cannot complete, so every caller either skips the read
or hand-rolls a parser. An easy workaround is how a defect survives.

WHY FIELD-TRIMMING ALONE CANNOT FIX IT -- measured, not assumed, because the
motivating goal warns explicitly against assuming field bloat. Byte census over
the 1,372 nodes: summary 48.3%, file 25.2%, children 7.4%, capability_level
5.3%, last_updated 5.1%, confidence 3.4%, article_count 3.4%, depth 2.0%.
Dropping BOTH summary and file still leaves 1.34x the cap; dropping children as
well leaves 1.11x. The most aggressive field-narrowing that preserves any
usefulness (path stripped, summary truncated to 120, children reduced to a
count) measured 551,015 B = 2.10x. The population is the problem, so the bound
has to be a BYTE BUDGET over rows.

THE BOUND COMES FROM THE CAP, NOT FROM TODAY'S CORPUS. That is the point of
BUDGET_FRACTION: as the tree grows, more nodes are omitted and the file stays
under the cap. A constant tuned to today's node count would re-breach on the
next few hundred nodes, which is exactly how the original got here.

WHY DEPTH IS THE TIER AXIS. The one functional consumer is
`aspirations-select/SKILL.md` Phase 2.25, which Reads the file and matches
candidate goal CATEGORIES against tree nodes. Category-like structure lives in
the shallow nodes; a depth-9 leaf is a specific article, not a category. So
shallow-first ordering keeps precisely what that consumer filters on. Measured
tier sizes with the field narrowing below: depth<=2 is 0.09x cap, depth<=3 is
0.36x, depth<=4 is 0.61x, depth<=5 is 0.93x -- so the budget admits the whole
category backbone with room left for the deepest tier it can afford.

NOTHING IS DROPPED SILENTLY. The output carries `nodes_included`,
`nodes_omitted` and `omitted_by_depth` at the top level, and build_summary
returns a stats dict the loader prints to stderr. A projection that quietly
truncates reads as a complete tree, which is worse than one that is loudly
partial.

WHY THE GENERATOR IS LEFT ALONE. `tree-read.sh --summary` has a SEPARATE
consumer that does not go through this file: aspirations-strategic-scan reads
that command's stdout directly, at full fidelity. Bounding the generator would
silently narrow that consumer's input too -- a fix that breaks a second reader
is not a fix. So the bound lives in the loader's projection only, and
`tree-read.sh --summary` is byte-for-byte unchanged.

FIELD NOTE -- `file` IS NOT REDUNDANT WITH THE NODE KEY, despite appearing so.
The key is only the LEAF name; `file` carries the full ancestry path, and the
ancestry is the category information the consumer matches on. Measured: the
file path contains the key on 1370 of 1372 rows, which invites exactly the
wrong conclusion. What IS redundant is the constant tree prefix and the `.md`
suffix, stripped here.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The Read tool's hard refusal threshold, in bytes. A file at or above this
# cannot be read by the LLM at all -- the failure this module prevents, not
# merely an efficiency concern.
READ_TOOL_CAP = 262144

# Headroom below the cap. 0.75 leaves a quarter of the budget for growth
# between regenerations and for the encoding slack of non-ASCII escapes.
BUDGET_FRACTION = 0.75

# Per-node summary truncation. The corpus median is 155 chars, so most rows
# survive intact; the max observed was 16,062 chars in a single node, which is
# the case this exists for. Truncation is visible (the ellipsis) and lossless
# where it matters -- anything reasoning about full node text reads the node
# file, never this projection.
SUMMARY_MAX = 160

# Stripped from `file` because it is constant across every row.
TREE_PREFIX = "world/knowledge/tree/"

# Fields carried per node, after projection. `children` is replaced by
# `n_children` (a count): the name list was 7.4% of the file and the consumer
# matches categories, never child names.
PROJECTED_FIELDS = (
    "path", "summary", "depth", "capability_level", "confidence",
    "article_count", "last_updated", "n_children",
)


def default_budget():
    """Byte budget for the projection. Derived from the cap, never a constant."""
    return int(READ_TOOL_CAP * BUDGET_FRACTION)


def _strip_path(file_value):
    """Drop the constant tree prefix and the .md suffix from a node file path."""
    f = file_value or ""
    if f.startswith(TREE_PREFIX):
        f = f[len(TREE_PREFIX):]
    if f.endswith(".md"):
        f = f[:-3]
    return f


def project_node(row, summary_max=SUMMARY_MAX):
    """One node -> its narrowed projection. Pure; no I/O."""
    row = row or {}
    summary = row.get("summary") or ""
    if summary_max is not None and len(summary) > summary_max:
        summary = summary[:summary_max] + "..."
    out = {
        "path": _strip_path(row.get("file")),
        "summary": summary,
        "depth": row.get("depth"),
        "capability_level": row.get("capability_level"),
        "article_count": row.get("article_count"),
        "last_updated": row.get("last_updated"),
        "n_children": len(row.get("children") or []),
    }
    # confidence is absent on ~64% of rows; omit rather than carry an explicit
    # null, which costs bytes on the majority to describe the minority.
    if row.get("confidence") is not None:
        out["confidence"] = row["confidence"]
    return out


def _sort_key(item):
    """Shallow-first, then richer nodes first, then stable by key.

    Depth is the primary axis (see module docstring). Within a depth, prefer
    nodes with more articles and more children -- those are the ones a category
    match is most likely to land on. `-` on the counts sorts descending while
    keeping the whole key ascending, so the ordering is total and deterministic
    (no reliance on dict insertion order, which would make the omission set
    vary between runs on the same corpus).
    """
    key, row = item
    depth = row.get("depth")
    depth = 10 ** 6 if depth is None else depth
    return (depth, -(row.get("article_count") or 0),
            -len(row.get("children") or []), key)


def build_summary(raw, budget=None, summary_max=SUMMARY_MAX):
    """Bounded projection of a `tree-read.sh --summary` payload.

    `raw` is the parsed generator output: {"nodes": {key: {...}}, "total": N}.
    Returns (obj, stats). Nodes are admitted in tier order until the byte
    budget is spent; the remainder is REPORTED, never silently dropped.
    """
    if budget is None:
        budget = default_budget()
    nodes_in = (raw or {}).get("nodes") or {}
    ordered = sorted(nodes_in.items(), key=_sort_key)

    # Envelope cost is charged first so the budget covers the WHOLE file, not
    # just the rows. Measuring rows alone is how a bound overshoots by the size
    # of its own scaffolding.
    kept = {}
    omitted_by_depth = {}
    envelope = {
        "nodes": kept, "total": (raw or {}).get("total", len(nodes_in)),
        "nodes_included": 0, "nodes_omitted": 0, "omitted_by_depth": {},
        "projection_note": "",
    }
    note = (
        "BOUNDED PROJECTION (g-115-5857): nodes are admitted shallow-first "
        "until %d bytes (%.2f x the %d-byte Read-tool cap). Omitted nodes are "
        "counted in omitted_by_depth. For the full tree use tree-read.sh "
        "--summary or retrieve.sh; for one node read its file."
    ) % (budget, BUDGET_FRACTION, READ_TOOL_CAP)
    envelope["projection_note"] = note

    def encoded_len(obj):
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    used = encoded_len(envelope)
    for key, row in ordered:
        proj = project_node(row, summary_max=summary_max)
        # +cost of the key, the projection, and the joining punctuation.
        cost = encoded_len({key: proj}) + 1
        if used + cost > budget:
            d = row.get("depth")
            d = "unknown" if d is None else d
            omitted_by_depth[str(d)] = omitted_by_depth.get(str(d), 0) + 1
            continue
        kept[key] = proj
        used += cost

    envelope["nodes_included"] = len(kept)
    envelope["nodes_omitted"] = sum(omitted_by_depth.values())
    envelope["omitted_by_depth"] = dict(
        sorted(omitted_by_depth.items(),
               key=lambda kv: (kv[0] == "unknown", kv[0])))

    stats = {
        "nodes_total": len(nodes_in),
        "nodes_included": envelope["nodes_included"],
        "nodes_omitted": envelope["nodes_omitted"],
        "budget": budget,
        "bytes": encoded_len(envelope),
        "cap": READ_TOOL_CAP,
    }
    return envelope, stats


def main(argv=None):
    """Read generator JSON on stdin (or a path argv[1]); write bounded JSON."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        raw_text = Path(argv[0]).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # Fail LOUD and emit nothing. A malformed payload passed through would
        # replace a usable cached summary with garbage; the loader's caller
        # then reads a file that parses but describes nothing.
        sys.stderr.write("[tree-summary] refusing to project unparseable "
                         "generator output: %s\n" % exc)
        return 1
    obj, stats = build_summary(raw)
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    if stats["nodes_omitted"]:
        sys.stderr.write(
            "[tree-summary] %d of %d nodes omitted to stay under the "
            "Read-tool cap (%d B of %d budget, cap %d). See omitted_by_depth "
            "in the payload.\n" % (
                stats["nodes_omitted"], stats["nodes_total"],
                stats["bytes"], stats["budget"], stats["cap"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
