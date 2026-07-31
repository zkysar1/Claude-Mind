#!/usr/bin/env python3
"""tree-split-verify.py — content-level verbatim-preservation verifier for
over-cap tree-node splits (/tree split-overcap Step 5; gap-051, g-115-3994).

Proves that every content line of the ORIGINAL node body survives, as a
multiset, in the union of the OUTPUT files (children + stub + optional
archive). This is the check whose absence is a SILENT failure mode: an
identifier-level diff (headers/datestamps) reported 0 orphans of 18 while a
content-level check found 2 whole sections missing (g-115-4069, 2026-07-30).

Design constraints, each earned in a live run:
  - CONTENT level, never identifiers/headers alone (g-115-3994 spec change).
  - DECORATION-TOLERANT normalization: one agent writes ~~2026-..~~, another
    **2026-..** — a bare regex matched zero rows and reported "0 missing"
    against a denominator of 0.
  - DENOMINATORS ALWAYS: "lines=N missing=M", never a bare "missing=0".
    A zero denominator is exit 2 (VACUOUS), not a pass — "rows=0 missing=0
    announces its own vacuity".

NAMED EXCLUSIONS (guard-1462 — what this seam cannot falsify):
  - The ORIGINAL's leading YAML front-matter block is excluded: metadata is
    EXPECTED to change at split time and the parent-reconcile step owns it.
    Front-matter loss is therefore not detected here.
  - The ORIGINAL's first H1 title line (`# ...`) is excluded for the same
    reason: children/stub retitle it by design. ALL deeper headers (##, ###)
    remain in the comparable set — section identity is content.
  - _tree.yaml registration, stub creation, and parent-reconcile correctness
    are NOT verified by this tool — they stay checklist steps in the
    /tree split-overcap procedure.
  - Within-multiset REORDERING passes (the check targets LOSS, not order;
    verbatim section moves preserve order by construction).

Usage:
  py -3 core/scripts/tree-split-verify.py --original <backup.md> \
      --outputs <child1.md> <child2.md> [...] [--json]

Exit codes: 0 = all content preserved; 1 = content MISSING (listed);
            2 = VACUOUS (original yielded 0 comparable lines) or usage error.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_DECOR_EDGE = re.compile(r"^[\s>#*\-+|]+|[\s|]+$")          # leading markers / table pipes
_DECOR_INLINE = re.compile(r"(\*\*|~~|__|`)")                # emphasis / strike / code
_WS = re.compile(r"\s+")


def normalize(line: str) -> str:
    """Decoration-tolerant canonical form of one line ('' = not comparable)."""
    s = _DECOR_EDGE.sub("", line)
    s = _DECOR_INLINE.sub("", s)
    s = _WS.sub(" ", s).strip().lower()
    # A line that was PURE decoration (---, ===, ***, empty table row) reduces
    # to '' or a short run of punctuation — exclude from the comparable set.
    if not s or re.fullmatch(r"[-=*_.:|+ ]+", s):
        return ""
    return s


def body_lines(text: str, strip_front_matter: bool) -> list[str]:
    lines = text.splitlines()
    if strip_front_matter and lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break
    if strip_front_matter:
        # Original side only: drop the single H1 title line — retitled by
        # design at split time (named exclusion; deeper headers stay).
        for i, ln in enumerate(lines):
            if ln.startswith("# "):
                lines = lines[:i] + lines[i + 1:]
                break
            if ln.strip():
                break
    out = []
    for ln in lines:
        n = normalize(ln)
        if n:
            out.append(n)
    return out


def main() -> int:
    # The missing-sample loop below prints tree-node content verbatim, and that
    # content is em-dash/arrow-heavy. On native Windows with PIPED stdout Python
    # picks the locale encoding (cp1252) and a non-cp1252 char raises
    # UnicodeEncodeError -> traceback, destroying the lines=/missing=/verdict
    # diagnostic exactly in the failure case it exists to explain. Exit stays
    # nonzero either way (fail-closed), so this only protects the diagnostic.
    # Guarded: reconfigure is absent on some stream wrappers. (foxtrot,
    # msg-20260730-213552-foxtrot-5246)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--original", required=True, help="backup of the pre-split node")
    ap.add_argument("--outputs", required=True, nargs="+",
                    help="every post-split file content may have moved to "
                         "(children + stub/index + archive)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    orig_p = Path(args.original)
    if not orig_p.is_file():
        print(f"ERROR: original not found: {orig_p}", file=sys.stderr)
        return 2
    missing_outputs = [p for p in args.outputs if not Path(p).is_file()]
    if missing_outputs:
        print(f"ERROR: output file(s) not found: {missing_outputs}", file=sys.stderr)
        return 2

    orig = Counter(body_lines(orig_p.read_text(encoding="utf-8-sig", errors="replace"),
                              strip_front_matter=True))
    union: Counter = Counter()
    for p in args.outputs:
        union.update(body_lines(Path(p).read_text(encoding="utf-8-sig", errors="replace"),
                                strip_front_matter=False))

    total = sum(orig.values())
    if total == 0:
        msg = "VACUOUS: original yielded 0 comparable content lines — a pass here proves nothing"
        if args.as_json:
            print(json.dumps({"verdict": "vacuous", "lines": 0, "distinct": 0,
                              "matched": 0, "missing": 0}))
        else:
            print(f"lines=0 distinct=0 matched=0 missing=0\n{msg}")
        return 2

    deficit = {ln: cnt - union.get(ln, 0) for ln, cnt in orig.items()
               if union.get(ln, 0) < cnt}
    missing_instances = sum(deficit.values())
    matched = total - missing_instances

    result = {
        "verdict": "pass" if missing_instances == 0 else "MISSING_CONTENT",
        "lines": total,
        "distinct": len(orig),
        "matched": matched,
        "missing": missing_instances,
        "missing_sample": [
            {"line": ln, "deficit": d}
            for ln, d in sorted(deficit.items(), key=lambda kv: -kv[1])[:20]
        ],
    }
    if args.as_json:
        print(json.dumps(result, indent=1))
    else:
        print(f"lines={total} distinct={len(orig)} matched={matched} "
              f"missing={missing_instances}")
        for row in result["missing_sample"]:
            print(f"  MISSING x{row['deficit']}: {row['line'][:120]}")
        print(f"verdict: {result['verdict']}")
    return 0 if missing_instances == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
