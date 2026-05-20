#!/usr/bin/env python3
"""Content-trigger scan — match draft text against retrieval-keyword-catalog.

Sub-goal 2 of g-115-672 brief (msg-20260513-013402-zeta-1038). Reads
world/conventions/retrieval-keyword-catalog.yaml (created by g-115-678),
regex-scans the input blob, and emits JSON with matching convention
pointers + severity + when_to_use per match. Designed to be embedded in
pre-write hooks, brief-author handlers, and decompose-time checks so the
retrieval shim fires at write-time, not just at retrieve.sh invocation.

Usage:
  echo 'content' | content-trigger-scan.sh
  echo 'content' | content-trigger-scan.sh --context-trigger pre-write
  content-trigger-scan.sh --blob 'text'
  content-trigger-scan.sh --file path/to/draft.md

Exit codes:
  0 = scan completed (matches >= 0); JSON on stdout
  1 = framework error (missing catalog, bad YAML schema, unreadable input)

Fail-loud rule: catalog parse errors exit 1 with stderr explanation rather
than silently returning empty matches — silent empties produce false
all-clear signals at every caller.
"""

import argparse
import json
import os
import re
import sys

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Closes acceptance (4) of  — stdin-ingest sweep.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()
from pathlib import Path

import yaml

VALID_SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _load_catalog(catalog_path: Path) -> dict:
    if not catalog_path.exists():
        print(f"FAIL: catalog not found at {catalog_path}", file=sys.stderr)
        sys.exit(1)
    with catalog_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "entries" not in data:
        print(f"FAIL: catalog at {catalog_path} missing top-level 'entries' list", file=sys.stderr)
        sys.exit(1)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        print(f"FAIL: catalog 'entries' is not a list", file=sys.stderr)
        sys.exit(1)
    return data


def _read_blob(args) -> str:
    if args.blob is not None:
        return args.blob
    if args.file is not None:
        path = Path(args.file)
        if not path.exists():
            print(f"FAIL: input file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text(encoding="utf-8")
    # stdin
    return sys.stdin.read()


def _scan(blob: str, entries: list, context_trigger: str | None, min_severity: str) -> list:
    min_rank = SEVERITY_RANK.get(min_severity, 0)
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id", "")
        sev = entry.get("severity", "low")
        if SEVERITY_RANK.get(sev, 0) < min_rank:
            continue
        triggers = entry.get("context_triggers", []) or []
        if context_trigger and context_trigger not in triggers:
            continue
        keywords = entry.get("keywords", []) or []
        matched_keywords = []
        for kw in keywords:
            if not isinstance(kw, str):
                continue
            try:
                if re.search(kw, blob, flags=re.IGNORECASE):
                    matched_keywords.append(kw)
            except re.error as exc:
                # Bad regex in catalog — skip this keyword but report once
                print(f"WARN: bad regex in catalog entry {eid}: {kw!r}: {exc}", file=sys.stderr)
                continue
        if matched_keywords:
            matches.append({
                "id": eid,
                "convention_path": entry.get("convention_path", ""),
                "severity": sev,
                "when_to_use": entry.get("when_to_use", ""),
                "context_triggers": triggers,
                "matched_keywords": matched_keywords,
            })
    return matches


def _format_human(result: dict) -> str:
    lines = []
    n = result["match_count"]
    if n == 0:
        return f"[content-trigger-scan] no matches in {result['scanned_chars']} chars"
    lines.append(f"[content-trigger-scan] {n} match(es) in {result['scanned_chars']} chars")
    for m in result["matches"]:
        lines.append(f"  {m['id']} [{m['severity']}] -> {m['convention_path']}")
        lines.append(f"    matched: {', '.join(repr(k) for k in m['matched_keywords'])}")
        wt = m["when_to_use"].strip().splitlines()
        if wt:
            lines.append(f"    when_to_use: {wt[0][:200]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Scan content blob against retrieval-keyword-catalog.yaml.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--blob", help="Inline content blob to scan (mutually exclusive with --file and stdin).")
    ap.add_argument("--file", help="Read blob from file path.")
    ap.add_argument("--catalog-path", help="Override catalog path (defaults to WORLD_DIR/conventions/retrieval-keyword-catalog.yaml).")
    ap.add_argument(
        "--context-trigger",
        help="Filter to entries that include this trigger (e.g. pre-write, brief-author, decompose, pre-emit, pre-bridge).",
    )
    ap.add_argument(
        "--min-severity",
        choices=VALID_SEVERITIES,
        default="low",
        help="Suppress entries below this severity. Default: low (all entries).",
    )
    ap.add_argument(
        "--output",
        choices=("json", "human"),
        default="json",
        help="Output format. Default: json.",
    )
    args = ap.parse_args()

    # Resolve catalog path via _paths.py (the canonical Python path resolver)
    if args.catalog_path:
        catalog_path = Path(args.catalog_path)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _paths import WORLD_DIR
        catalog_path = Path(WORLD_DIR) / "conventions" / "retrieval-keyword-catalog.yaml"

    data = _load_catalog(catalog_path)
    blob = _read_blob(args)
    matches = _scan(blob, data.get("entries", []), args.context_trigger, args.min_severity)

    result = {
        "match_count": len(matches),
        "scanned_chars": len(blob),
        "catalog_path": str(catalog_path),
        "catalog_schema_version": data.get("schema_version"),
        "context_trigger": args.context_trigger,
        "min_severity": args.min_severity,
        "matches": matches,
    }

    if args.output == "human":
        print(_format_human(result))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
