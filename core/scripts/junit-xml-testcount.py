#!/usr/bin/env python3
"""Prove a targeted test run actually EXECUTED tests, from JUnit result XML.

Mechanizes `.claude/rules/gradle-tests-pattern.md` rule 3 -- "a run that reports
zero tests executed is a FAILED measurement, not a pass" -- which was mandated
but hand-rolled every time (gap-050, encountered twice by two different agents,
each tripping a DIFFERENT silent trap).

THE THREE TRAPS THIS EXISTS TO CLOSE. Each is silent, and each points the WRONG
WAY -- they manufacture a false alarm or a false all-clear, never an error.

1. NAME-ATTRIBUTE MATCHING (rb-5425 misread 1). The `name` attribute on
   <testsuite> carries the @DisplayName, NOT the class name. Matching on it
   returns nothing for any class that declares a display name, which reads
   exactly like "the class never ran". Measured: a class matched nothing and
   had in fact run 2/2 green. So this tool keys on the FILENAME.

2. NESTED CONTAINERS SPLIT ACROSS FILES (rb-5425 misread 2). Each @Nested class
   gets its OWN result file, named by its display name, so the outer class's
   file holds only part of the total. Measured: 3 reported against 5 declared.
   So this tool SUMS every file matching the class, never reads one file.

3. UP-TO-DATE WRITES NOTHING (gap-050 encounter 2). After `rm -rf` of the
   results dir, a re-run whose inputs are unchanged is UP-TO-DATE and writes no
   XML at all -- so absence reads as zero-executed. ABSENCE AND ZERO ARE
   DIFFERENT VERDICTS here, and the remedy differs: absence means re-run
   forcing execution, zero means the selector matched nothing.

A fourth trap is documented but deliberately NOT mechanized: grepping the
console log by class name shares root cause 1 (the log prints the display name
too), so it is not an independent second signal -- confirming a zero there
raises confidence while adding no information.

Usage:
    py -3 core/scripts/junit-xml-testcount.py --results-dir <dir>
    py -3 core/scripts/junit-xml-testcount.py --results-dir <dir> --class MyTest
    py -3 core/scripts/junit-xml-testcount.py --results-dir <dir> \
        --class MyTest --source-dir src/test/java --newer-than src/main/java/Thing.java
    ... --json

Exit: 0 = every requested class verified as executed
      1 = at least one class is NOT proven executed (absent, zero, or stale)
      2 = usage / environment error
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

# Annotations that declare an executable test case. @Nested is NOT here -- it
# declares a CONTAINER, and counting it would inflate the declared total against
# which the executed count is checked.
_TEST_ANNOTATIONS = ("@Test", "@ParameterizedTest", "@RepeatedTest", "@TestFactory", "@TestTemplate")

# A line that is commented out must not count toward the declared total.
_COMMENT = re.compile(r"^\s*(//|\*|/\*)")


def _int_attr(node, key: str) -> int:
    try:
        return int(node.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def result_files(results_dir: str, class_name: str | None) -> list[str]:
    """Every result file for a class, matched by FILENAME (trap 1) so nested
    containers come along (trap 2). With no class, every result file."""
    pattern = "TEST-*.xml" if not class_name else f"TEST-*{class_name}*.xml"
    return sorted(glob.glob(os.path.join(results_dir, pattern)))


def sum_files(paths: list[str]) -> dict:
    """Sum the testsuite counters across every file. Unparseable files are
    reported, never silently skipped -- a swallowed parse error is a zero."""
    out = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
           "files": len(paths), "unparseable": [], "oldest_mtime": None}
    for p in paths:
        try:
            root = ET.parse(p).getroot()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            out["unparseable"].append({"file": os.path.basename(p), "error": str(exc)[:120]})
            continue
        suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
        for s in suites:
            for k in ("tests", "failures", "errors", "skipped"):
                out[k] += _int_attr(s, k)
        m = os.path.getmtime(p)
        out["oldest_mtime"] = m if out["oldest_mtime"] is None else min(out["oldest_mtime"], m)
    return out


def declared_count(source_dir: str | None, class_name: str) -> dict:
    """Positive control: how many test cases does the SOURCE declare?

    Returns count None when the source file cannot be located -- an absent
    control is reported as absent, never as a passing comparison.
    """
    if not source_dir:
        return {"count": None, "source_file": None, "reason": "no --source-dir given"}
    hits = sorted(glob.glob(os.path.join(source_dir, "**", f"{class_name}.*"), recursive=True))
    hits = [h for h in hits if h.rsplit(".", 1)[-1] in ("java", "kt", "groovy", "scala")]
    if not hits:
        return {"count": None, "source_file": None,
                "reason": f"no source file named {class_name}.(java|kt|groovy|scala) under {source_dir}"}
    src = hits[0]
    n = 0
    try:
        with open(src, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if _COMMENT.match(line):
                    continue
                if any(a in line for a in _TEST_ANNOTATIONS):
                    n += 1
    except OSError as exc:
        return {"count": None, "source_file": src, "reason": f"unreadable: {exc}"}
    return {"count": n, "source_file": src, "reason": None}


def assess(results_dir: str, class_name: str | None, source_dir: str | None,
           newer_than: str | None) -> dict:
    paths = result_files(results_dir, class_name)
    label = class_name or "<all classes>"

    if not paths:
        # Trap 3: absence is NOT zero-executed. Say so, and name the remedy.
        return {
            "class": label, "verdict": "NO_RESULT_FILE", "ok": False,
            "files": 0, "tests": 0,
            "detail": ("No result file matched. This is NOT the same as zero tests executed: "
                       "a re-run whose inputs are unchanged is UP-TO-DATE and writes no XML "
                       "(so absence follows a cleaned results dir). Force execution "
                       "(e.g. gradle --rerun / --rerun-tasks) and measure again."),
        }

    agg = sum_files(paths)
    rec = {"class": label, "files": agg["files"], "tests": agg["tests"],
           "failures": agg["failures"], "errors": agg["errors"], "skipped": agg["skipped"],
           "matched_files": [os.path.basename(p) for p in paths]}
    if agg["unparseable"]:
        rec["unparseable"] = agg["unparseable"]

    if agg["tests"] == 0:
        rec.update(verdict="ZERO_EXECUTED", ok=False,
                   detail=("Result files exist but report 0 tests. Rule 3: a run that reports "
                           "zero tests executed is a FAILED measurement, not a pass. The usual "
                           "cause is a selector that matched nothing."))
        return rec

    # Positive control against the source's declared cases.
    if class_name:
        dec = declared_count(source_dir, class_name)
        rec["declared"] = dec["count"]
        rec["source_file"] = dec["source_file"]
        if dec["count"] is None:
            rec["declared_note"] = dec["reason"]
        elif agg["tests"] < dec["count"]:
            rec.update(verdict="UNDER_DECLARED", ok=False,
                       detail=(f"Executed {agg['tests']} < {dec['count']} declared. Nested "
                               f"containers write their own files; if one is missing the sum is "
                               f"short. Matched {agg['files']} file(s)."))
            return rec

    # Staleness: results must be newer than the edits under test.
    if newer_than:
        if not os.path.exists(newer_than):
            rec["stale_note"] = f"--newer-than path does not exist: {newer_than}"
        elif agg["oldest_mtime"] is not None and agg["oldest_mtime"] < os.path.getmtime(newer_than):
            rec.update(verdict="STALE_RESULTS", ok=False,
                       detail=("Oldest result file predates the edits under test, so these "
                               "counts describe a previous build. Re-run before trusting them."))
            return rec

    rec.update(verdict="EXECUTED", ok=True)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prove a test run executed tests, from JUnit result XML.")
    ap.add_argument("--results-dir", required=True, help="Directory holding TEST-*.xml result files")
    ap.add_argument("--class", dest="classes", action="append", default=[],
                    help="Class to assert (repeatable). Omit for a suite-level total.")
    ap.add_argument("--source-dir", help="Test source root, for the declared-count positive control")
    ap.add_argument("--newer-than", help="Path whose mtime the results must post-date")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.results_dir):
        print(f"ERROR: results dir not found: {args.results_dir}", file=sys.stderr)
        print("If the build was cleaned, an UP-TO-DATE re-run writes nothing -- force execution.",
              file=sys.stderr)
        return 2

    targets = args.classes or [None]
    records = [assess(args.results_dir, c, args.source_dir, args.newer_than) for c in targets]
    ok = all(r["ok"] for r in records)

    if args.json:
        print(json.dumps({"ok": ok, "results_dir": args.results_dir, "records": records}, indent=1))
    else:
        for r in records:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"[{mark}] {r['class']}: {r['verdict']} — "
                  f"tests={r['tests']} failures={r.get('failures', 0)} "
                  f"errors={r.get('errors', 0)} skipped={r.get('skipped', 0)} "
                  f"files={r['files']}")
            if r.get("declared") is not None:
                print(f"       declared in source: {r['declared']} ({r.get('source_file')})")
            if r.get("declared_note"):
                print(f"       declared-count control UNAVAILABLE: {r['declared_note']}")
            if r.get("unparseable"):
                print(f"       UNPARSEABLE: {r['unparseable']}")
            if r.get("stale_note"):
                print(f"       {r['stale_note']}")
            if r.get("detail"):
                print(f"       {r['detail']}")
        print(f"\nVERDICT: {'EXECUTED — counts are trustworthy' if ok else 'NOT PROVEN EXECUTED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
