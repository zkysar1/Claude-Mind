"""Helper for seed-verify.sh — runs an engine subcommand and pretty-prints the JSON output.

Replaces the previous inline `py -3 -c "..."` blocks in seed-verify.sh
(BUG 4 from fresh-eyes review — shell injection risk when destination
paths contained quotes or backslashes).

Usage:
  _seed_verify_format.py <check_id> --engine <path-to-engine.py> \\
                                    --cmd <subcommand> \\
                                    --manifest <path> \\
                                    [--source <path>] \\
                                    --dest <path>

Exit code 0 = PASS, 1 = FAIL/WARN (caller decides which based on check type).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def _run_engine(engine: str, cmd: str, extra: dict) -> dict:
    cmdline = [sys.executable, engine, cmd]
    for k, v in extra.items():
        cmdline.append(f"--{k}")
        cmdline.append(v)
    try:
        result = subprocess.run(cmdline, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"_error": "engine timeout", "_rc": -1}
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_error": "JSON parse failed", "_stdout": result.stdout[:500], "_stderr": result.stderr[:500]}
    data["_rc"] = result.returncode
    return data


def main():
    p = argparse.ArgumentParser()
    p.add_argument("check_id")
    p.add_argument("--engine", required=True)
    p.add_argument("--cmd", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--source", default=None)
    p.add_argument("--dest", required=True)
    args = p.parse_args()

    extra = {"manifest": args.manifest, "dest": args.dest}
    if args.source:
        extra["source"] = args.source
    data = _run_engine(args.engine, args.cmd, extra)

    cid = args.check_id

    # Per-check formatting
    if "_error" in data:
        print(f"   FAIL: {data['_error']}")
        return 1

    if args.cmd == "verify-completeness":
        n_missing = len(data.get("missing_required", []))
        if n_missing == 0:
            print("   PASS")
            return 0
        print(f"   FAIL: {n_missing} required include(s) missing")
        for m in data.get("missing_required", []):
            print(f"   - {m['path']} ({m['type']})")
        return 1

    if args.cmd == "verify-leak-check":
        leaked = data.get("leaked", [])
        info = data.get("info", [])
        for p_info in info:
            print(f"   INFO: {p_info} present at destination (preserved, not leaked)")
        if not leaked:
            print("   PASS")
            return 0
        print("   FAIL: excluded paths present:")
        for p_leak in leaked:
            print(f"   - {p_leak}")
        return 1

    if args.cmd == "verify-cruft":
        present = data.get("present", [])
        if not present:
            print("   PASS")
            return 0
        print("   WARN: cruft patterns still present at destination:")
        for p_c in present:
            print(f"   - {p_c}")
        return 1

    if args.cmd == "verify-integrity":
        matches = data.get("match_count", 0)
        mismatches = data.get("mismatch_count", 0)
        total = matches + mismatches
        if mismatches == 0 and matches > 0:
            print(f"   PASS ({matches}/{total} match)")
            return 0
        print(f"   WARN: {mismatches} sample(s) mismatched (may indicate transform divergence)")
        for r in data.get("results", []):
            if r["status"] != "MATCH":
                print(f"   - {r['file']}: {r['status']}")
        return 1

    if args.cmd == "verify-exec-bits":
        # : index-mode comparison. SKIP is loud and distinct from PASS
        # (a destination with no git index has no modes to compare yet);
        # an unreadable side is a FAIL, never a clean read (guard-1947).
        if data.get("skipped"):
            print(f"   SKIP: {data['skipped']}")
            return 0
        if data.get("error"):
            print(f"   FAIL: {data['error']}")
            return 1
        stripped = data.get("stripped", [])
        checked = data.get("checked", 0)
        if not stripped:
            print(f"   PASS ({checked} source-executable path(s) carry 100755 in the destination index)")
            return 0
        print(f"   FAIL: {len(stripped)} path(s) executable at the source but 100644 in the destination index")
        for rel in stripped[:20]:
            print(f"   - {rel}")
        if len(stripped) > 20:
            print(f"   ... and {len(stripped) - 20} more")
        print("   Fix: git -C <dest> update-index --chmod=+x -- <path>, then commit."
              " A chmod never reaches the index on a core.fileMode=false clone (g-360-16).")
        return 1

    # Default
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
