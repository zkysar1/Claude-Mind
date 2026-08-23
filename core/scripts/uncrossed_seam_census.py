#!/usr/bin/env python3
"""Uncrossed producer/consumer seam census (gap-117).

Answers ONE question about a repo: which string-keyed payload seams does no
single test file cross end-to-end? A seam is a key written by one production
class and read by another; it is UNCROSSED when no test file names both classes,
which means nothing exercises the two halves together and a producer-side rename
cannot be caught by the suite.

Deterministic enumeration only. WHICH uncrossed pair deserves a test is an agent
judgement and is deliberately not decided here (the gap-029 split).

TWO INDEPENDENT SLOTS, reported separately because they have different evidence
strength:

  (a) uncrossed pairs   -- the five-step filter below. Its accuracy was an open
                           question until hypothesis 2026-08-09_census-uncrossed-
                           overreports resolved CORRECTED (g-115-5578): a mutation
                           renaming a real producer key left ZERO of 67 test
                           classes red, so an UNCROSSED verdict is load-bearing.
  (b) zero-reader keys  -- keys written in main with no reader anywhere in main
                           OR test. Never affected by that hypothesis: a key with
                           no reader has no reader to cross to.

THE FIVE STEPS (from gap-117; do not reorder, each narrows the last):
  1. parse every main + test source; extract every string-keyed put()/get() and
     the class that owns it
  2. keep keys written in main file A and read in main file B, B != A
  3. keep those READ WITH A DEFAULT by a non-writer -- a "soft read". A break
     renders a legitimate-looking branch instead of throwing, so no test can
     notice by crashing
  4. keep those also hand-built via put() in a TEST file -- the synthetic fixture
     that lets both halves agree forever
  5. keep single-writer keys only (drops generic-name collisions), then report
     every pair no test file NAMES BOTH classes of

STEP 4 IS THE LOAD-BEARING FILTER AND IS VALIDATED, NOT SUSPECT. g-115-5578
measured exactly the population it selects for: `tasksExecuted` is hand-built via
put() in three test files, every one asserting against a fixture it constructed
itself, so none could fail on a producer rename. Do NOT "simplify" this by
replacing step 4 with a key-name grep -- that scores the seam covered by three
files and is wrong in the opposite direction (guard-3986).

THE POSITIVE CONTROL AT STEP 5 IS MANDATORY, not optional output (guard-1941).
An all-uncrossed verdict and a broken class-name matcher are the same output. So
the census always reports how many test files DO name 2+ production classes; if
that number is 0, the verdict is a parser artifact and the run says so.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# .put("key", ...) -- a WRITE.
WRITE_RE = re.compile(r'\.put\(\s*"([^"\\]+)"')

# .getString("key")            -> hard read  (trailing ')')
# .getString("key", "default") -> SOFT read  (trailing ',')
# Also matches getJsonObject/getInteger/getBoolean/getLong/getDouble/getValue/get.
READ_RE = re.compile(r'\.get([A-Za-z]*)\(\s*"([^"\\]+)"\s*(,|\))')

# .getOrDefault("key", d) -- always a soft read by construction.
GET_OR_DEFAULT_RE = re.compile(r'\.getOrDefault\(\s*"([^"\\]+)"\s*,')

# Keys this census cannot reason about: too generic to attribute to one seam, or
# not payload keys at all. Kept deliberately SHORT -- an over-broad skip list
# silently shrinks the finding set, which is the failure direction that looks
# like success.
SKIP_KEYS = frozenset({"", " "})


def java_sources(root: Path):
    """Every .java under root, skipping build output."""
    for p in sorted(root.rglob("*.java")):
        parts = set(p.parts)
        if "build" in parts or "out" in parts or "generated" in parts:
            continue
        yield p


def scan(path: Path):
    """Return (writes, hard_reads, soft_reads) as sets of key names for one file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), set(), set()

    writes = {k for k in WRITE_RE.findall(text) if k not in SKIP_KEYS}
    hard, soft = set(), set()
    for _accessor, key, terminator in READ_RE.findall(text):
        if key in SKIP_KEYS:
            continue
        (soft if terminator == "," else hard).add(key)
    for key in GET_OR_DEFAULT_RE.findall(text):
        if key not in SKIP_KEYS:
            soft.add(key)
    return writes, hard, soft


def census(repo: Path, main_rel: str, test_rel: str) -> dict:
    main_root, test_root = repo / main_rel, repo / test_rel

    missing = [str(p) for p in (main_root, test_root) if not p.is_dir()]
    if missing:
        return {"error": "source root(s) not found", "missing": missing,
                "repo": str(repo)}

    # class -> sets, plus key -> classes, for main and test separately.
    main_writes_by_key = defaultdict(set)      # key -> {producer class}
    main_hard_by_key = defaultdict(set)        # key -> {reader class}
    main_soft_by_key = defaultdict(set)        # key -> {soft reader class}
    test_writes_by_key = defaultdict(set)      # key -> {test class}
    test_reads_by_key = defaultdict(set)
    main_classes, test_files = [], []

    for p in java_sources(main_root):
        cls = p.stem
        main_classes.append(cls)
        w, hard, soft = scan(p)
        for k in w:
            main_writes_by_key[k].add(cls)
        for k in hard:
            main_hard_by_key[k].add(cls)
        for k in soft:
            main_soft_by_key[k].add(cls)

    for p in java_sources(test_root):
        test_files.append(p)
        w, hard, soft = scan(p)
        for k in w:
            test_writes_by_key[k].add(p.stem)
        for k in hard | soft:
            test_reads_by_key[k].add(p.stem)

    main_class_set = set(main_classes)

    # ---- STEP 1 -------------------------------------------------------------
    all_written = set(main_writes_by_key)

    # ---- STEP 2: cross-class (written in A, read in B != A) -----------------
    def readers(key):
        return main_hard_by_key.get(key, set()) | main_soft_by_key.get(key, set())

    cross_class = {k for k in all_written if readers(k) - main_writes_by_key[k]}

    # ---- STEP 3: soft-read by a NON-WRITER ----------------------------------
    soft_read = {k for k in cross_class
                 if main_soft_by_key.get(k, set()) - main_writes_by_key[k]}

    # ---- STEP 4: also hand-built via put() in a TEST file --------------------
    test_hand_built = {k for k in soft_read if test_writes_by_key.get(k)}

    # ---- STEP 5: single-writer only, then the crossing test -----------------
    single_writer = {k for k in test_hand_built if len(main_writes_by_key[k]) == 1}

    # Which production classes does each test file NAME? Word-boundary match on
    # the class name -- a class is "named" if the identifier appears at all.
    named_by_test = {}
    for p in test_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        named_by_test[p.stem] = {
            c for c in main_class_set
            if re.search(r"\b%s\b" % re.escape(c), text)
        }

    # POSITIVE CONTROL (guard-1941): how many test files name 2+ production
    # classes at all? If zero, an "everything is uncrossed" verdict says nothing
    # about the repo and everything about the matcher.
    control_crossing_files = sum(1 for names in named_by_test.values() if len(names) >= 2)

    pairs, crossed_pairs = [], []
    for k in sorted(single_writer):
        producer = sorted(main_writes_by_key[k])[0]
        for consumer in sorted(main_soft_by_key.get(k, set()) - main_writes_by_key[k]):
            crossing = sorted(t for t, names in named_by_test.items()
                              if producer in names and consumer in names)
            row = {"key": k, "producer": producer, "consumer": consumer,
                   "crossing_test_files": crossing}
            (crossed_pairs if crossing else pairs).append(row)

    # ---- SLOT (b): zero-reader keys -----------------------------------------
    zero_reader = sorted(
        ({"key": k, "writers": sorted(main_writes_by_key[k])}
         for k in all_written
         if not readers(k) and not test_reads_by_key.get(k)),
        key=lambda r: r["key"])

    # The gap's own prose counts CLASS PAIRS ("5 of 5 uncrossed, forming exactly 2
    # producer/consumer pairs"), while the rows above are per-KEY. Report both, or
    # a reader comparing this run against the hand-roll is comparing two different
    # units and will read agreement as drift.
    uncrossed_class_pairs = sorted({(r["producer"], r["consumer"]) for r in pairs})

    return {
        "repo": str(repo),
        "main_files": len(main_classes),
        "test_files": len(test_files),
        "uncrossed_class_pairs": [{"producer": p, "consumer": c}
                                  for p, c in uncrossed_class_pairs],
        "funnel": {
            "1_distinct_written_keys": len(all_written),
            "2_cross_class": len(cross_class),
            "3_soft_read_by_non_writer": len(soft_read),
            "4_hand_built_in_a_test": len(test_hand_built),
            "5_single_writer": len(single_writer),
        },
        "positive_control": {
            "test_files_naming_2plus_production_classes": control_crossing_files,
            "test_files_total": len(test_files),
            "valid": control_crossing_files > 0,
            "note": ("A verdict is only meaningful when this is non-zero -- otherwise "
                     "the class-name matcher found nothing anywhere and every pair "
                     "would read as uncrossed (guard-1941)."),
        },
        "uncrossed_pairs": pairs,
        "crossed_pairs": crossed_pairs,
        "zero_reader_keys": zero_reader,
    }


def render(result: dict) -> str:
    if result.get("error"):
        return "ERROR: %s\n  missing: %s" % (result["error"], ", ".join(result["missing"]))

    f, pc = result["funnel"], result["positive_control"]
    out = [
        "uncrossed producer/consumer seam census — %s" % result["repo"],
        "  sources: %d main / %d test .java" % (result["main_files"], result["test_files"]),
        "",
        "  FUNNEL (each step narrows the last)",
        "    1. distinct written keys        %5d" % f["1_distinct_written_keys"],
        "    2. cross-class                  %5d" % f["2_cross_class"],
        "    3. soft-read by a non-writer    %5d" % f["3_soft_read_by_non_writer"],
        "    4. hand-built in a test         %5d" % f["4_hand_built_in_a_test"],
        "    5. single-writer                %5d" % f["5_single_writer"],
        "",
        "  POSITIVE CONTROL (guard-1941): %d of %d test files name 2+ production classes"
        % (pc["test_files_naming_2plus_production_classes"], pc["test_files_total"]),
    ]
    if not pc["valid"]:
        out.append("    ⚠ ZERO — the verdict below is a PARSER ARTIFACT, not a finding.")
        out.append("      Do not file test work from this run; fix the matcher first.")

    out.append("")
    out.append("  SLOT (a) — UNCROSSED: %d key-rows across %d CLASS PAIRS"
               % (len(result["uncrossed_pairs"]), len(result["uncrossed_class_pairs"])))
    out.append("    (the two counts differ — several keys usually share one class pair;")
    out.append("     gap-117's own prose counts CLASS PAIRS)")
    for r in result["uncrossed_pairs"]:
        out.append("    %s: %s -> %s" % (r["key"], r["producer"], r["consumer"]))
    if result["crossed_pairs"]:
        out.append("  (crossed, for contrast: %d)" % len(result["crossed_pairs"]))
        for r in result["crossed_pairs"][:5]:
            out.append("    %s: %s -> %s  [crossed by %s]"
                       % (r["key"], r["producer"], r["consumer"],
                          ", ".join(r["crossing_test_files"][:3])))

    out.append("")
    out.append("  SLOT (b) — ZERO-READER KEYS: %d" % len(result["zero_reader_keys"]))
    for r in result["zero_reader_keys"][:25]:
        out.append("    %s  (written by %s)" % (r["key"], ", ".join(r["writers"])))
    if len(result["zero_reader_keys"]) > 25:
        out.append("    ... and %d more" % (len(result["zero_reader_keys"]) - 25))

    out.append("")
    out.append("  WHICH pair deserves a test is a judgement call and is NOT decided here.")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="path to the repo to census")
    ap.add_argument("--main-dir", default="src/main", help="main sources (default src/main)")
    ap.add_argument("--test-dir", default="src/test", help="test sources (default src/test)")
    ap.add_argument("--json", action="store_true", help="emit the raw result object")
    args = ap.parse_args(argv)

    result = census(Path(args.repo).expanduser().resolve(), args.main_dir, args.test_dir)
    print(json.dumps(result, indent=2) if args.json else render(result))

    if result.get("error"):
        return 1
    # Exit 3 when the positive control is dead: a caller must be able to tell a
    # meaningless run from a clean one WITHOUT parsing the prose. 1 stays
    # reserved for usage/plumbing so a broken run is never read as a verdict.
    return 3 if not result["positive_control"]["valid"] else 0


if __name__ == "__main__":
    sys.exit(main())
