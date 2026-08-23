#!/usr/bin/env python3
"""crit3 SHAPE-FORK measurement: heading map -> per-section BYTE spans -> distribution.

Mechanizes the measurement half of the MANDATORY shape fork at
`.claude/skills/tree/SKILL.md` Step 1.6 (guard-2109 / rb-6055), which must run
before ANY destructive reduction of an over-cap knowledge-tree node. Until now
that step was hand-work: grep the headings, derive per-section spans by eye,
compare halves for inversion.

IT DELIBERATELY WITHHOLDS THE (a)/(b)/(c)/(d) VERDICT. Routing needs judgment
this tool cannot have -- whether early sections are REFERENCED BY later ones,
and whether a dominant section is a mis-nested series or the node's quantitative
index. Emitting a shape here would convert a judgment call into an automated
one, and the whole reason the fork exists is that keep-newest-N silently
destroys two of the four shapes. So: facts out, call withheld.

WHY BYTES ARE THE PRIMARY UNIT. Step 1.6 said "line spans" until 2026-08-17 and
the unit is wrong by up to 7x, silently, in the direction that HIDES the
dominant section. Measured that day on a per-agent series shard: its `## Series`
TABLE section is 456 B/line against 63 B/line for narrative siblings, so by
LINES it is 6.4% of the node and by BYTES it is 32.5%. A line-based fork
reported a 0.94x uniformity ratio (shape (a): roll up) while the byte
distribution showed the node was shape (b)/(d) (split). Rolling it up would have
destroyed rows that cite each other by N, unrecoverably -- node bodies are
written UNFENCED.

THRESHOLDS ARE READ, NEVER HARDCODED. `core/config/tree.yaml` says of the
chars-per-token ratio: "tree.py owns the ratio; this comment must not restate it
as a second source of truth." This module obeys that for every number it uses --
the token cap, the crit3 ratio and CHARS_PER_TOKEN all come from those two
files at runtime. A copy here would be exactly the second source of truth the
config forbids, and it would rot the same way the "line spans" unit did.

Usage:
    py -3 core/scripts/tree_shape_fork.py <node.md> [--json]
    bash core/scripts/tree-shape-fork.sh <node.md> [--json]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

# A single section holding more than this fraction of the node's BYTES is the
# shape-(d) tell. Step 1.6: "A single section holding >30% of the node's bytes
# is the finding, whatever its line count says."
DOMINANT_SECTION_FRACTION = 0.30

# Above this bytes-per-line a section is TABLE-shaped: `|`-rows, id lists and
# timestamps, where the heading map cannot see the real partition and a ranged
# Read can blow the token cap while reporting a small line count. Measured
# examples in Step 1.6: 456 B/line and 763/837 B/line for table sections against
# 63-97 B/line for heading-shaped prose siblings.
TABLE_SHAPED_BYTES_PER_LINE = 300

HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')

# A dated / goal-id-stamped subsection is the append-series signature. Kept
# deliberately loose: this is REPORTED as a hint, never used to route.
SERIES_STAMP_RE = re.compile(r'(\d{4}-\d{2}-\d{2}|\bg-\d+-\d+\b|\bn=\d+\b)')


def _load_tree_module():
    """Import core/scripts/tree.py for CHARS_PER_TOKEN (its declared SSOT).

    tree.py imports sibling modules by bare name, so core/scripts must be on
    sys.path before exec_module or it dies on `_stdio`.
    """
    if str(CORE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(CORE_SCRIPTS))
    spec = importlib.util.spec_from_file_location("_tsf_tree", CORE_SCRIPTS / "tree.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_thresholds():
    """chars-per-token + crit3 budget, read from their owning files.

    Returns a dict carrying the PROVENANCE of each number, so a reader of the
    JSON can tell a real config read from a fallback without re-deriving it.
    """
    out = {}
    try:
        tree_mod = _load_tree_module()
        out["chars_per_token"] = float(tree_mod.CHARS_PER_TOKEN)
        out["chars_per_token_source"] = "core/scripts/tree.py::CHARS_PER_TOKEN"
    except Exception as exc:  # noqa: BLE001 -- degrade loudly, never silently
        out["chars_per_token"] = None
        out["chars_per_token_source"] = f"UNAVAILABLE ({type(exc).__name__}: {exc})"

    cfg_path = PROJECT_ROOT / "core" / "config" / "tree.yaml"
    token_cap = ratio = None
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for section in (cfg, *(v for v in cfg.values() if isinstance(v, dict))):
            if "distill_token_cap" in section and token_cap is None:
                token_cap = section["distill_token_cap"]
            if "distill_token_ratio" in section and ratio is None:
                ratio = section["distill_token_ratio"]
        out["token_cap_source"] = "core/config/tree.yaml"
    except Exception as exc:  # noqa: BLE001
        out["token_cap_source"] = f"UNAVAILABLE ({type(exc).__name__}: {exc})"

    out["distill_token_cap"] = token_cap
    out["distill_token_ratio"] = ratio

    cpt = out["chars_per_token"]
    # crit3 fires at ratio * cap TOKENS; the byte budget is that in chars.
    out["crit3_trigger_bytes"] = (
        int(token_cap * ratio * cpt) if None not in (token_cap, ratio, cpt) else None
    )
    out["read_cap_bytes"] = int(token_cap * cpt) if None not in (token_cap, cpt) else None
    return out


def parse_sections(text):
    """Split on `##` headings (Step 1.6's awk uses `^## `), preserving a PREAMBLE.

    Byte counts use UTF-8 encoded length, not len(str): a node full of em-dashes
    and box-drawing characters is measurably larger on disk than its character
    count, and the cap this fork protects is about bytes/tokens, not characters.
    """
    lines = text.splitlines()
    sections = []
    cur = {"heading": "[PREAMBLE]", "level": 0, "start_line": 1, "lines": 0, "bytes": 0,
           "body": []}
    for idx, line in enumerate(lines, start=1):
        m = HEADING_RE.match(line)
        if m and len(m.group(1)) == 2:  # `## ` only, matching Step 1.6
            if cur["lines"] or cur["bytes"] or cur["heading"] != "[PREAMBLE]":
                sections.append(cur)
            cur = {"heading": line.strip()[:100], "level": 2, "start_line": idx,
                   "lines": 0, "bytes": 0, "body": []}
            continue
        cur["lines"] += 1
        cur["bytes"] += len(line.encode("utf-8")) + 1
        cur["body"].append(line)
    sections.append(cur)
    # Drop an empty synthetic preamble (a node whose very first line is `## `).
    if sections and sections[0]["heading"] == "[PREAMBLE]" and sections[0]["bytes"] == 0:
        sections = sections[1:]
    return sections


def profile(path, thresholds):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    total_bytes = len(raw)
    sections = parse_sections(text)
    cpt = thresholds.get("chars_per_token")

    rows = []
    for s in sections:
        b, ln = s["bytes"], s["lines"]
        body = "\n".join(s["body"])
        rows.append({
            "heading": s["heading"],
            "start_line": s["start_line"],
            "lines": ln,
            "bytes": b,
            "pct_of_node": round(100.0 * b / total_bytes, 2) if total_bytes else 0.0,
            "bytes_per_line": round(b / ln, 1) if ln else 0.0,
            "table_shaped": bool(ln and (b / ln) > TABLE_SHAPED_BYTES_PER_LINE),
            "est_tokens": int(b / cpt) if cpt else None,
            "series_stamps": len(SERIES_STAMP_RE.findall(body)),
        })

    read_cap_b = thresholds.get("read_cap_bytes")
    crit3_b = thresholds.get("crit3_trigger_bytes")

    for r in rows:
        r["over_read_cap_alone"] = bool(read_cap_b and r["bytes"] > read_cap_b)

    dominant = max(rows, key=lambda r: r["bytes"]) if rows else None
    findings = []

    if dominant and dominant["pct_of_node"] >= DOMINANT_SECTION_FRACTION * 100:
        findings.append(
            f"DOMINANT SECTION: {dominant['heading']!r} holds {dominant['pct_of_node']}% "
            f"of the node's bytes (>{int(DOMINANT_SECTION_FRACTION * 100)}%). This is the "
            f"shape-(d) tell -- shapes (a)-(c) all reason about newest-vs-oldest ENTRIES, "
            f"so none of them tests it and the fork defaults such a node to (a). "
            f"keep-newest-N cannot reach the cap no matter how many entries it deletes."
        )
    for r in rows:
        if r["over_read_cap_alone"]:
            findings.append(
                f"SECTION OVER THE READ CAP BY ITSELF: {r['heading']!r} is {r['bytes']} B "
                f"(~{r['est_tokens']} est tokens) against a ~{read_cap_b} B read cap. A "
                f"ranged Read of this section alone will be REFUSED."
            )
        if r["table_shaped"]:
            findings.append(
                f"TABLE-SHAPED: {r['heading']!r} is {r['bytes_per_line']} B/line "
                f"(>{TABLE_SHAPED_BYTES_PER_LINE}). The heading map cannot see the partition "
                f"inside a table, and a line-count profile will understate it."
            )

    # Inversion check (shape (c) tell): newest half vs oldest half by BYTES, in
    # document order. Reported as a ratio; the CALL is still withheld.
    inversion = None
    if len(rows) >= 4:
        mid = len(rows) // 2
        old_b = sum(r["bytes"] for r in rows[:mid])
        new_b = sum(r["bytes"] for r in rows[mid:])
        if old_b:
            inversion = round(new_b / old_b, 2)
            if inversion >= 2.0:
                findings.append(
                    f"INVERTED BLOAT (shape-(c) tell): the newer half is {inversion}x the "
                    f"bytes of the older half. keep-newest-N would remove the cheap dense "
                    f"history and retain nearly all the cost."
                )

    # Suggested partition count. The instinct is ONE child, and one child is
    # often not enough: a single split of a 284 KB node leaves a ~138 KB shard,
    # still 2.4x cap. Five were needed in the worked precedent.
    partitions = None
    if crit3_b:
        partitions = max(1, -(-total_bytes // crit3_b))  # ceil
        if partitions > 1:
            findings.append(
                f"SUGGESTED PARTITION COUNT: {partitions} (node {total_bytes} B / crit3 "
                f"budget {crit3_b} B). One child is often NOT enough -- size the split "
                f"from the budget, not from instinct."
            )

    return {
        "file": str(path),
        "total_bytes": total_bytes,
        "total_lines": len(text.splitlines()),
        "est_tokens": int(total_bytes / cpt) if cpt else None,
        "section_count": len(rows),
        "sections": rows,
        "dominant_section": dominant["heading"] if dominant else None,
        "dominant_pct": dominant["pct_of_node"] if dominant else None,
        "newest_vs_oldest_byte_ratio": inversion,
        "suggested_partitions": partitions,
        "thresholds": thresholds,
        "findings": findings,
        "shape_verdict": None,
        "shape_verdict_note": (
            "WITHHELD BY DESIGN. Routing to (a) append-grown series / (b) catalog / "
            "(c) inverted-bloat / (d) one-section-dominates needs judgment this tool "
            "does not have: whether early sections are REFERENCED BY later ones, and "
            "whether a dominant section is a mis-nested series or the node's "
            "quantitative index. See tree/SKILL.md Step 1.6."
        ),
    }


def render(p):
    out = []
    out.append("=" * 100)
    out.append(f"SHAPE FORK  {p['file']}")
    out.append(f"  {p['total_bytes']} B / {p['total_lines']} lines / ~{p['est_tokens']} est tokens"
               f" / {p['section_count']} `##` sections")
    th = p["thresholds"]
    out.append(f"  thresholds: chars_per_token={th.get('chars_per_token')} "
               f"({th.get('chars_per_token_source')})")
    out.append(f"              read_cap={th.get('read_cap_bytes')} B  "
               f"crit3_budget={th.get('crit3_trigger_bytes')} B ({th.get('token_cap_source')})")
    out.append("=" * 100)
    out.append(f"{'SECTION':<62}{'BYTES':>9}{'%':>7}{'LINES':>7}{'B/LN':>8}{'~TOK':>8}  FLAGS")
    for r in sorted(p["sections"], key=lambda x: -x["bytes"]):
        flags = []
        if r["table_shaped"]:
            flags.append("TABLE")
        if r["over_read_cap_alone"]:
            flags.append("OVER-CAP-ALONE")
        if r["series_stamps"]:
            flags.append(f"stamps={r['series_stamps']}")
        out.append(f"{r['heading'][:60]:<62}{r['bytes']:>9}{r['pct_of_node']:>7}"
                   f"{r['lines']:>7}{r['bytes_per_line']:>8}{str(r['est_tokens']):>8}  "
                   f"{','.join(flags)}")
    out.append("")
    if p["newest_vs_oldest_byte_ratio"] is not None:
        out.append(f"newest-half / oldest-half bytes: {p['newest_vs_oldest_byte_ratio']}x")
    out.append("")
    out.append("--- FINDINGS ---")
    for f in p["findings"] or ["  (none -- distribution is unremarkable)"]:
        out.append(f"  * {f}")
    out.append("")
    out.append("--- SHAPE VERDICT ---")
    out.append(f"  {p['shape_verdict_note']}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("node", help="path to the tree node .md file")
    ap.add_argument("--json", action="store_true", help="emit the profile as JSON")
    args = ap.parse_args(argv)

    path = Path(args.node)
    if not path.exists():
        print(f"tree-shape-fork: no such file: {path}", file=sys.stderr)
        return 2
    # A tree node can be a DIRECTORY (a node that was already split into a
    # parent dir of shards, e.g. directive-lane-series-echo/). exists() is True
    # for one, so without this the next line raises IsADirectoryError as a raw
    # traceback -- measured live while validating this tool. Fail with the
    # actionable message instead: the caller wants a shard, and naming them is
    # cheaper than making them look it up.
    if path.is_dir():
        shards = sorted(p.name for p in path.glob("*.md"))
        print(f"tree-shape-fork: {path} is a DIRECTORY, not a node file "
              f"(this node is already split into shards). Profile a shard instead.",
              file=sys.stderr)
        if shards:
            print("  shards: " + ", ".join(shards[:12])
                  + (f" ... (+{len(shards) - 12} more)" if len(shards) > 12 else ""),
                  file=sys.stderr)
        return 2

    p = profile(path, load_thresholds())
    print(json.dumps(p, indent=1) if args.json else render(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
