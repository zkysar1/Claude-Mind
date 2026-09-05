#!/usr/bin/env python3
"""abandoned-claim-check.py — CLI for the abandoned-claim detector ().

Reads two FILES and writes a report. It deliberately spawns nothing: the
canonical reads (`team-state-read.sh --json`, `aspirations-query.sh`) are done
by the `.sh` wrapper and handed here as paths. Two reasons, both measured:

  * guard-580 refuses ad-hoc Python that builds a subprocess argv whose argv[0]
    is a bare "bash", and a Python->bash->daemon chain has hung on Windows
    (rb-225 / rb-247).
  * `_paths.sh` is the only thing that resolves WORLD_PATH from the per-agent
    local-paths.conf. A bare `py -3` of this file would have STORAGE_BACKEND set
    globally but no mappable world root, so it would silently read the LOCAL
    MIRROR instead of the authoritative store (g-115-6188 / guard-3864) — which
    for THIS detector means seeing zero in-flight rows and declaring the whole
    fleet abandoned.

Because that second failure is the dangerous one, the wrapper passes
`--authoritative` explicitly, and its ABSENCE zeroes every release. Report-only
is always safe; releasing on a mirror read is not.

Exit status is 0 on every path including findings — this is a report lane, and a
non-zero would make the precheck battery treat a healthy detection as a stage
failure. Read the OUTPUT, not the rc.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _abandoned_claim import (  # noqa: E402
    DEFAULT_THRESHOLD_MINUTES,
    find_abandoned,
)


def _load_json(path: str | None):
    """Load JSON from a file that may carry human preamble lines or be JSONL.

    Several canonical readers print a summary line BEFORE their JSON, and some
    emit JSONL. A parser that assumes one shape returns a confident empty, which
    here reads as "no in-flight rows" — the exact false positive this detector
    must never produce. So: try whole-document, then first-brace, then JSONL.
    """
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for opener in ("{", "["):
        idx = text.find(opener)
        if idx != -1:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                continue

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows or None


def _iter_goals(payload):
    """Yield goal dicts from whatever shape the queue reader produced."""
    if payload is None:
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                if "goals" in item and isinstance(item["goals"], list):
                    for goal in item["goals"]:
                        if isinstance(goal, dict):
                            yield goal
                else:
                    yield item
        return
    if isinstance(payload, dict):
        if isinstance(payload.get("goals"), list):
            for goal in payload["goals"]:
                if isinstance(goal, dict):
                    yield goal
            return
        for key in ("results", "aspirations", "matches"):
            if isinstance(payload.get(key), list):
                yield from _iter_goals(payload[key])
                return


def _emit_releasable(report: dict) -> None:
    """Machine-readable tail for the wrapper's --apply loop, emitted in BOTH
    output modes. One emitter, so text and JSON cannot drift apart again."""
    releasable = [r["goal_id"] for r in report["abandoned"] if r["releasable"]]
    if releasable:
        print("RELEASABLE_IDS " + " ".join(releasable))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--team-state", help="path to team-state-read.sh --json output")
    ap.add_argument("--goals", help="path to the goal-queue JSON")
    ap.add_argument(
        "--threshold-minutes",
        type=float,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"release age floor (default {DEFAULT_THRESHOLD_MINUTES})",
    )
    ap.add_argument(
        "--authoritative",
        action="store_true",
        help="the team-state read came from the authoritative store, not the "
        "local mirror. Without it NOTHING is releasable (guard-980).",
    )
    ap.add_argument("--json", action="store_true", help="emit the raw report")
    args = ap.parse_args()

    team_state = _load_json(args.team_state)
    goals_payload = _load_json(args.goals)
    goals = list(_iter_goals(goals_payload))

    # An unreadable team-state cannot be treated as "no rows" — that would make
    # every claim look abandoned. Downgrade to non-authoritative so the report
    # still prints but nothing can be released.
    authoritative = bool(args.authoritative and isinstance(team_state, dict))

    report = find_abandoned(
        goals,
        team_state,
        datetime.now(),
        threshold_minutes=args.threshold_minutes,
        authoritative=authoritative,
    )
    report["team_state_readable"] = isinstance(team_state, dict)
    report["goals_readable"] = bool(goals)

    if args.json:
        print(json.dumps(report, indent=2))
        # The machine-readable tail must be emitted in BOTH modes. It used to be
        # text-only, so the wrapper's --apply loop found no ids under --json and
        # reported "nothing met all four keep-safe conditions" over a report that
        # said releasable_count=1 — the report and the action disagreeing, with
        # nothing saying so. The wrapper strips this line (`grep -v
        # '^RELEASABLE_IDS '`), so stdout stays valid JSON.
        _emit_releasable(report)
        return 0

    # Population beside the count, always — a bare finding count cannot be told
    # apart from a scan that read nothing (guard-2298, guard-3830).
    print(
        f"[abandoned-claim] scanned={report['scanned_goals']} "
        f"claimed_in_progress={report['claimed_in_progress']} "
        f"in_flight_rows={report['in_flight_rows']} "
        f"abandoned={report['abandoned_count']} "
        f"releasable={report['releasable_count']} "
        f"threshold={report['threshold_minutes']:.0f}m "
        f"authoritative={report['authoritative']}"
    )

    if not report["team_state_readable"]:
        print(
            "[abandoned-claim] WARN: team-state was UNREADABLE — every claim "
            "would look abandoned, so nothing is releasable. This is a plumbing "
            "fault, not a finding."
        )
    if report["scanned_goals"] == 0:
        print(
            "[abandoned-claim] WARN: scanned ZERO goals — the queue read was "
            "empty or misshapen. An empty scan is not a clean result."
        )

    for row in report["abandoned"]:
        age = "unknown" if row["age_minutes"] is None else f"{row['age_minutes']:.0f}m"
        mark = "RELEASABLE" if row["releasable"] else "hold"
        print(
            f"  {mark:10s} {row['goal_id']} claimed_by={row['claimed_by']} "
            f"age={age} | {row['title']}"
        )
        if row["hold_reasons"]:
            print(f"             held because: {'; '.join(row['hold_reasons'])}")

    _emit_releasable(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
