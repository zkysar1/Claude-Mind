#!/usr/bin/env python3
"""verification-retrofit: parse prose 'Verification outcomes:' / 'Verification checks:'
blocks in a goal description into structured goal.verification.{outcomes,checks}.

Dual purpose (g-247-03):
  (a) one-shot repair for existing prose-only drift surfaced by /verify-learning S49.7,
  (b) opt-in fallback after g-247-02 hardened aspirations.py to reject prose-only writes.

Usage:
  verification-retrofit.sh <goal-id> [--dry-run|--apply] [--source world|agent]

Exit codes:
  0  success (dry-run printed JSON or apply completed; also when no prose headers found)
  2  ambiguous / malformed prose — refuses to guess
  3  goal not found
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402

HEADER_OUTCOMES = "Verification outcomes:"
HEADER_CHECKS = "Verification checks:"

BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def _extract_section(lines, start_idx):
    """Return (items, next_idx). Reads bullet lines after a header until blank line
    or another header or end. If ANY non-bullet, non-blank line appears before a
    bullet has been collected, treat as ambiguous.
    """
    items = []
    i = start_idx
    seen_content = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            if seen_content:
                return items, i + 1
            i += 1
            continue
        if stripped.startswith(HEADER_OUTCOMES) or stripped.startswith(HEADER_CHECKS):
            return items, i
        m = BULLET_RE.match(raw)
        if m:
            items.append(m.group(1).strip())
            seen_content = True
            i += 1
            continue
        # Non-blank, non-bullet, non-header — ambiguous if we haven't closed yet
        if seen_content:
            # Continuation of last bullet? For safety, treat as end-of-section
            # but only if the line is clearly prose (no leading bullet markers).
            # Conservative: stop here, let caller decide.
            return items, i
        # No bullets seen and non-blank prose — ambiguous header
        return None, i
    return items, i


def parse_prose(description):
    """Parse description into {outcomes, checks}. Returns dict or None if ambiguous.

    Contract:
      - If neither header appears: returns {'outcomes': [], 'checks': []}, parsed=False
      - If a header appears and produces ≥1 bullet: populates that list, parsed=True
      - If a header appears but the next non-blank line is prose (no bullets):
        returns None (ambiguous) so caller exits non-zero.
    """
    if not description:
        return {"outcomes": [], "checks": [], "parsed": False}
    lines = description.splitlines()
    out = {"outcomes": [], "checks": [], "parsed": False}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(HEADER_OUTCOMES):
            items, i = _extract_section(lines, i + 1)
            if items is None:
                return None
            out["outcomes"] = items
            out["parsed"] = True
            continue
        if stripped.startswith(HEADER_CHECKS):
            items, i = _extract_section(lines, i + 1)
            if items is None:
                return None
            out["checks"] = items
            out["parsed"] = True
            continue
        i += 1
    return out


def _source_paths(source):
    """Return list of (path, source_label) tuples to search."""
    paths = []
    if source in (None, "world") and WORLD_DIR is not None:
        p = WORLD_DIR / "aspirations.jsonl"
        if p.exists():
            paths.append((p, "world"))
    if source in (None, "agent") and AGENT_DIR is not None:
        p = AGENT_DIR / "aspirations.jsonl"
        if p.exists():
            paths.append((p, "agent"))
    return paths


def _load_goal(goal_id, source):
    """Find goal across world + agent aspirations.jsonl directly (no subprocess).
    Returns (goal_dict, asp_id, actual_source) or (None, None, None).
    """
    for path, src in _source_paths(source):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        asp = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for g in asp.get("goals", []):
                        if g.get("id") == goal_id:
                            return g, asp.get("id"), src
        except OSError:
            continue
    return None, None, None


def main(argv):
    ap = argparse.ArgumentParser(prog="verification-retrofit")
    ap.add_argument("goal_id", help="Goal ID to process (e.g. g-245-01)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Print parsed JSON; do not modify goal (default)")
    mode.add_argument("--apply", action="store_true",
                      help="Write parsed verification into the goal")
    ap.add_argument("--source", choices=["world", "agent"], default=None,
                    help="Queue to search (default: both)")
    args = ap.parse_args(argv)

    goal, asp_id, src = _load_goal(args.goal_id, args.source)
    if goal is None:
        print(f"Goal {args.goal_id} not found", file=sys.stderr)
        return 3

    desc = goal.get("description") or ""
    parsed = parse_prose(desc)
    if parsed is None:
        print(f"AMBIGUOUS: {args.goal_id} description contains Verification outcomes:/"
              f"checks: header but no bullet lines follow. Refusing to guess. Expected "
              f"lines starting with '- ', '* ', or '1. ' after each header.",
              file=sys.stderr)
        return 2

    if not parsed["parsed"]:
        print(json.dumps({
            "goal_id": args.goal_id,
            "source": src,
            "aspiration_id": asp_id,
            "parsed": False,
            "note": "no Verification outcomes:/checks: headers found in description",
        }, indent=2))
        return 0

    payload = {"outcomes": parsed["outcomes"], "checks": parsed["checks"]}

    if args.apply:
        # Invoke aspirations.py directly via the same Python interpreter to avoid
        # CRLF issues that bite bash-wrapper scripts under subprocess on Windows
        # (see rb-family: subprocess launching _paths.sh corrupts SCRIPT_DIR).
        aspirations_py = SCRIPT_DIR / "aspirations.py"
        cmd = [sys.executable, str(aspirations_py), "--source", src,
               "update-goal", args.goal_id, "verification", json.dumps(payload)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"update-goal failed (rc={r.returncode}): {r.stderr}",
                  file=sys.stderr)
            return 1
        print(json.dumps({
            "goal_id": args.goal_id,
            "source": src,
            "aspiration_id": asp_id,
            "applied": True,
            "verification": payload,
        }, indent=2))
        return 0

    # dry-run
    print(json.dumps({
        "goal_id": args.goal_id,
        "source": src,
        "aspiration_id": asp_id,
        "parsed": True,
        "verification": payload,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
