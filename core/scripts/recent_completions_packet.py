#!/usr/bin/env python3
"""recent_completions_packet.py -- the recent-completions REVIEW PACKET (gap-079).

Why this exists: "generate hypotheses from recent work" re-derives the same join
by hand on every run, because no single store answers it. The packet joins:

  1. completed non-recurring goals  -- aspirations-query.sh --goal-status completed --full
                                       (the --full projection is required: the default
                                        output omits `description`, guard-884)
  2. their outcome_note             -- same records; ABSENCE is reported explicitly
                                       rather than silently omitted (see `note_status`)
  3. the hypothesis stage histogram -- pipeline-read.sh --counts

(3) is included because the duplicate-check half of the consumer's work reads the
hypothesis store, and picking a filter there is a known trap: the record field
`status` is null on the overwhelming majority of rows and the live discriminator
is `stage` (guard-2869). Emitting the histogram unfiltered means the reader never
has to choose -- and never has to believe a zero produced by the wrong field.

Deterministic inputs to deterministic output: this builds the packet, it does not
judge it. Reading the packet is the caller's job.

Usage:
  recent-completions-packet.sh [--limit N] [--since ISO] [--desc-chars N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

NOTE_FALLBACK = (
    "outcome_note absent -- fall back to the goal's journal entry and the "
    "experience archive for this goal id, in that order"
)


def _run(script: str, *args: str, timeout: int = 180) -> str:
    """Shell to a framework wrapper. Never a bare 'bash' argv[0] (guard-580)."""
    from _runtime_bash import bash_cmd  # noqa: WPS433

    r = subprocess.run(
        bash_cmd(str(SCRIPT_DIR / script), *args),
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "%s exited %d: %s" % (script, r.returncode, (r.stderr or "").strip()[:400])
        )
    return r.stdout


def fetch_completed() -> list:
    """aspirations-query.sh has NO --source selector -- its accepted flags are
    --goal-status / --goal-field / --title-contains / --description-contains /
    --full, and it REFUSES an unknown flag rather than ignoring it. So this
    exposes no source parameter: offering one would be a knob the underlying
    reader cannot honour."""
    out = _run("aspirations-query.sh", "--goal-status", "completed", "--full")
    data = json.loads(out) if out.strip() else []
    goals = data.get("goals") if isinstance(data, dict) else data
    if isinstance(goals, dict):
        goals = [goals]
    return goals or []


def fetch_stage_histogram() -> dict:
    """Stage, never status (guard-2869). Unfiltered -- the caller picks nothing."""
    out = _run("pipeline-read.sh", "--counts")
    return json.loads(out) if out.strip() else {}


def build_packet(goals: list, histogram: dict, limit: int = 10,
                 since: str = "", desc_chars: int = 400) -> dict:
    """PURE. Given records and a histogram, produce the packet. No I/O."""
    rows = [g for g in goals if isinstance(g, dict) and not g.get("recurring")]
    if since:
        rows = [g for g in rows if (g.get("completed_date") or "") >= since]
    rows.sort(key=lambda g: (g.get("completed_date") or ""), reverse=True)
    selected, missing = [], 0
    for g in rows[:limit]:
        note = (g.get("outcome_note") or "").strip()
        if not note:
            missing += 1
        desc = (g.get("description") or "").strip()
        selected.append({
            "goal_id": g.get("id"),
            "aspiration_id": g.get("aspiration_id"),
            "title": (g.get("title") or "").strip(),
            "completed_date": g.get("completed_date"),
            "outcome_class": g.get("outcome_class"),
            "completed_by": g.get("completed_by"),
            "description": desc[:desc_chars],
            "description_truncated": len(desc) > desc_chars,
            "outcome_note": note or None,
            "note_status": "present" if note else "absent",
            "note_fallback": None if note else NOTE_FALLBACK,
        })
    return {
        "completions": selected,
        "returned": len(selected),
        "eligible_total": len(rows),
        "window_saturated": len(rows) > limit,
        "notes_absent": missing,
        "hypothesis_stage_histogram": histogram,
    }


def render(p: dict) -> str:
    L = ["=== RECENT COMPLETIONS PACKET ===",
         "returned %d of %d eligible non-recurring closes%s"
         % (p["returned"], p["eligible_total"],
            "  (more available -- raise --limit)" if p["window_saturated"] else ""),
         "outcome_note absent on %d of %d shown" % (p["notes_absent"], p["returned"]),
         ""]
    for c in p["completions"]:
        L.append("- %s  [%s]  %s" % (c["goal_id"], c.get("outcome_class") or "-",
                                     c.get("completed_date") or "-"))
        L.append("    %s" % c["title"][:150])
        if c["description"]:
            L.append("    desc: %s%s" % (c["description"][:220],
                                         " ..." if c["description_truncated"] else ""))
        if c["note_status"] == "present":
            L.append("    note: %s ..." % c["outcome_note"][:220])
        else:
            L.append("    note: ABSENT -- %s" % c["note_fallback"])
        L.append("")
    L.append("hypothesis stages (unfiltered; stage is the live discriminator, not status):")
    for k, v in sorted(p["hypothesis_stage_histogram"].items(), key=lambda kv: -kv[1]):
        L.append("    %-22s %d" % (k, v))
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--since", default="")
    ap.add_argument("--desc-chars", type=int, default=400)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    packet = build_packet(fetch_completed(), fetch_stage_histogram(),
                          limit=a.limit, since=a.since, desc_chars=a.desc_chars)
    print(json.dumps(packet, indent=2) if a.json else render(packet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
