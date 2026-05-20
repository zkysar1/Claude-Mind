#!/usr/bin/env python3
"""Goal-Script-Orphan Gate — detect goals referencing nonexistent scripts.

Inverse direction of scripts-referenced-gate.py:
- scripts-referenced-gate.py catches core/scripts/*.{sh,py} files orphan
  FROM references.
- this gate catches goal description+skill fields naming core/scripts/*
  paths NOT present on disk.

Canonical incident (g-115-905, filed 2026-05-18 by delta after hitting it
on g-115-903): g-115-603 (Recurring weekly override-ledger sweep) named
core/scripts/override-ledger-consume.sh and .py which were deleted in
zeta commit 45713066 (2026-05-11). Last successful run was 2026-05-10;
deletion landed 2026-05-11; the orphan goal reference went undetected for
1 week until the goal failed in execution. scripts-referenced-gate.py
caught the script-side gap (the gate exempts the deletion implicitly by
seeing zero refs); but no gate caught the goal-side gap.

Scope:
  - Walks world/aspirations.jsonl + <agent>/aspirations.jsonl.
  - Scans each goal's `description` and `skill` fields for patterns
    matching `core/scripts/<name>.{sh,py}` (with or without an invoker
    prefix like `bash`, `python3`, `py -3`).
  - Flags any extracted name not present in core/scripts/.
  - Deduplicates (goal_id, script_name) so the same goal does not
    multi-report when it names the script in both fields.

Default scope is conservative: only `pending` and `in-progress` goals are
scanned (completed/skipped/superseded goals are historical, and their
orphan refs are harmless artifacts of past deletions). Use
--include-completed-goals to widen, and --include-archived to also walk
aspirations-archive.jsonl.

Contract
  --json (default) | --text
  --include-archived          also scan aspirations-archive.jsonl files
  --include-completed-goals   scan completed/skipped/superseded goals too

Exit code 1 if any orphan reference found; exit 0 otherwise.

Fail-open: unreadable JSONL yields empty result, exit 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR

SCRIPTS_DIR = CORE_ROOT / "scripts"

# Match script invocations in goal text. Captures the basename.
# Matches the literal substring `core/scripts/<name>.{sh,py}` regardless of
# any leading invoker (bash, python3, py -3) and rejects trailing word
# characters (so `core/scripts/foo.sh.bak` does NOT match `foo.sh`).
# Word-boundary on the leading side handled by the `core/scripts/` anchor
# itself — the slash before `<name>` is non-word so any prefix is fine.
_SCRIPT_REF_RE = re.compile(
    r"core/scripts/([\w.-]+\.(?:sh|py))(?![\w.-])"
)

# Goal statuses to scan by default. completed/skipped/superseded goals
# are historical — their script refs may legitimately point at scripts
# deleted after the goal closed, and re-flagging those is noise.
LIVE_STATUSES = {"pending", "in-progress"}


def _existing_scripts() -> set:
    """Return set of script basenames currently present in core/scripts/."""
    out: set = set()
    if not SCRIPTS_DIR.is_dir():
        return out
    for p in SCRIPTS_DIR.iterdir():
        if p.is_file() and p.suffix in (".sh", ".py"):
            out.add(p.name)
    return out


def _scan_aspirations(path: Path, source_label: str,
                       include_completed: bool):
    """Yield ref dicts for each `core/scripts/<name>` mention in a goal's
    description or skill field.

    Fail-open: unreadable file, JSONL parse error on any line, missing
    `goals` array all produce empty/partial yields rather than raising.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
        except json.JSONDecodeError:
            continue
        asp_id = asp.get("id", "")
        for g in asp.get("goals", []) or []:
            if not isinstance(g, dict):
                continue
            status = g.get("status", "")
            if not include_completed and status not in LIVE_STATUSES:
                continue
            gid = g.get("id", "")
            for field in ("description", "skill"):
                v = g.get(field) or ""
                if not isinstance(v, str):
                    continue
                for m in _SCRIPT_REF_RE.finditer(v):
                    yield {
                        "goal_id": gid,
                        "aspiration_id": asp_id,
                        "status": status,
                        "field": field,
                        "script_name": m.group(1),
                        "source": source_label,
                    }


def _dedup_orphans(orphan_refs: list) -> list:
    """Deduplicate by (goal_id, script_name). Keeps first encounter."""
    seen: set = set()
    unique: list = []
    for o in orphan_refs:
        key = (o["goal_id"], o["script_name"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(o)
    return unique


def main():
    ap = argparse.ArgumentParser(
        description="Flag goals whose description/skill names "
                    "core/scripts/<name>.{sh,py} not present on disk.",
    )
    ap.add_argument("--text", action="store_true",
                    help="Human summary on stdout (JSON on stderr).")
    ap.add_argument("--include-archived", action="store_true",
                    help="Also scan aspirations-archive.jsonl "
                         "(default: skip; archive is historical).")
    ap.add_argument("--include-completed-goals", action="store_true",
                    help="Also scan completed/skipped/superseded goals "
                         "(default: pending+in-progress only).")
    args = ap.parse_args()

    existing = _existing_scripts()

    sources = [
        (WORLD_DIR / "aspirations.jsonl", "world"),
        (AGENT_DIR / "aspirations.jsonl", "agent"),
    ]
    if args.include_archived:
        sources.append(
            (WORLD_DIR / "aspirations-archive.jsonl", "world-archive")
        )
        sources.append(
            (AGENT_DIR / "aspirations-archive.jsonl", "agent-archive")
        )

    orphan_refs: list = []
    total_refs = 0
    for path, label in sources:
        for ref in _scan_aspirations(
                path, label, args.include_completed_goals):
            total_refs += 1
            if ref["script_name"] not in existing:
                orphan_refs.append(ref)

    unique_orphans = _dedup_orphans(orphan_refs)

    report = {
        "sources_scanned": [str(p) for p, _ in sources if p.is_file()],
        "scripts_on_disk": len(existing),
        "total_references": total_refs,
        "orphan_count": len(unique_orphans),
        "orphan_references": unique_orphans,
        "would_block": len(unique_orphans) > 0,
    }

    if args.text:
        print(
            f"goal-script-orphan-gate: {total_refs} script references "
            f"across {len(report['sources_scanned'])} sources, "
            f"{len(unique_orphans)} orphans"
        )
        for o in unique_orphans:
            print(
                f"  [orphan-ref] {o['source']} {o['goal_id']} "
                f"({o['status']}, {o['field']}): {o['script_name']}"
            )
        print(json.dumps(report), file=sys.stderr)
    else:
        print(json.dumps(report))

    sys.exit(1 if unique_orphans else 0)


if __name__ == "__main__":
    main()
