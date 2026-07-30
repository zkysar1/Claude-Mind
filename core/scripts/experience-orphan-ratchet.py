#!/usr/bin/env python3
"""experience-orphan-ratchet.py — advisory drift check with baseline ratchet for
ORPHANED experience traces: an `experience/<id>.md` on disk that no index row
references (g-115-3786).

WHY THIS EXISTS. The failure is silent in BOTH directions, which is why nothing
surfaced it until it was measured on purpose:
  * `experience-add.sh` returns a JSON error OBJECT on failure, so a caller that
    reads `.id` off the response gets None and can report success while nothing
    was written. Observed: a Lane 1.5 registration failed 6x with write_conflict
    while the trace .md sat on disk unindexed.
  * An orphaned trace is invisible to `retrieve.sh`, so the loss never surfaces
    as an error — only as knowledge that mysteriously fails to resurface.
Nothing counted the two sides against each other. This does.

Root cause of the write failures is NOT established here and is tracked
separately under g-115-3780. This script is only the regression check.

── JOIN KEY: `content_path` basename. Chosen deliberately; state it when quoting
a number from this script, because the OTHER key gives a different answer. ──
`id` is NOT the join key even though `id` equals the .md basename for most rows.
Measured 2026-07-29 across the live fleet, both keys, same corpus:

    agent     md    orphan_by_id   orphan_by_content_path
    alpha    976        182                117
    bravo   1005        101                 49
    echo     467         43                 23
    foxtrot  638         84                 29
    zeta      648         71                 26

`content_path` is the field that actually REFERENCES the file, so a row whose
`id` diverges from its filename is correctly counted as indexed under this key
and spuriously counted as orphaned under `id`. The goal that requested this
check flagged the divergence (76 rows at filing time) and required the choice be
made explicitly rather than inherited.

── BOTH stores are scanned: experience.jsonl AND experience-archive.jsonl. ──
This is the correction that most changes the picture, and it is why the
filing-time numbers should NOT be transcribed anywhere. That measurement scanned
the LIVE index alone and reported ~450-505 orphans for alpha; including the
archive — where the bulk of older rows live — the real figure is 117. The
archive is an index, not a graveyard: a row there still references its trace.
Reporting a ~50% orphan rate when the true rate is ~12% would have motivated a
very different, much larger remediation than the one actually needed.

── BASELINE IS SEEDED, NOT ZERO. ── A large pre-existing backlog means this
check cannot start at 0 without failing on day one for reasons nobody
introduced. Per core/config/conventions/audit-baselines.md this is the
`seeded` case: measure current, seed it, ratchet down. The check then catches
NEW orphans while the historical backlog is worked under g-115-3780. The
baseline never GROWS on a regression — that is the whole point of a ratchet.

Fleet total is the ratchet metric (the schema carries one integer); per-agent
counts ride along in `breakdown`, which is where to look when the total moves.

Exit codes:
  0  always (advisory), unless VERIFY_LEARNING_DRIFT_HARD_GATE=1 and regressed
  2  script error (unreadable store, unwriteable baseline file)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _paths import META_DIR, agents_root  # type: ignore
from _fileops import locked_modify_yaml  # type: ignore

BASELINES_PATH = META_DIR / "audit-baselines.yaml"
KEY = "experience_orphan_traces"

# Both index stores. An archived row still references its trace — scanning the
# live store alone inflates the orphan count roughly 4x (see module docstring).
INDEX_FILES = ("experience.jsonl", "experience-archive.jsonl")


def _indexed_basenames(agent_dir: Path) -> set:
    """Every .md basename referenced by any index row, via content_path.

    Falls back to `id` ONLY for rows carrying no content_path at all — such a row
    still asserts a trace exists, and counting its file as orphaned would be a
    false positive attributable to the row's shape rather than to a lost write.
    """
    out = set()
    for fn in INDEX_FILES:
        p = agent_dir / fn
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # a malformed row indexes nothing; not this check's business
            cp = rec.get("content_path")
            if cp:
                out.add(PurePosixPath(str(cp).replace("\\", "/")).stem)
            elif rec.get("id"):
                out.add(str(rec["id"]))
    return out


def _compute_orphans():
    per_agent, total, scanned = {}, 0, 0
    root = agents_root()
    for agent_dir in sorted(p for p in root.glob("*") if p.is_dir()):
        trace_dir = agent_dir / "experience"
        if not trace_dir.is_dir():
            continue
        # An agent dir with traces but NO index at all is a different failure
        # (uninitialised store), not orphan drift — skip rather than report every
        # trace as orphaned.
        if not any((agent_dir / fn).is_file() for fn in INDEX_FILES):
            continue
        scanned += 1
        on_disk = {p.stem for p in trace_dir.glob("*.md")}
        orphans = on_disk - _indexed_basenames(agent_dir)
        per_agent[agent_dir.name] = len(orphans)
        total += len(orphans)
    return {"total": total, "breakdown": per_agent, "agents_scanned": scanned}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report without touching the baseline file")
    args = ap.parse_args()

    try:
        current = _compute_orphans()
    except Exception as e:
        print(f"ERROR: orphan audit failed: {e}", file=sys.stderr)
        return 2

    if current["agents_scanned"] == 0:
        # No agent carries both a trace dir and an index — nothing to compare.
        # Reporting 0 orphans here would be a vacuous PASS (rb-245: verify the
        # population exists before believing a zero).
        msg = "no agent dir carries both experience/ traces and an index store — nothing measured"
        print(json.dumps({"verdict": "skipped", "message": msg}, indent=2)
              if args.json else f"[experience-orphan-ratchet] SKIPPED: {msg}")
        return 0

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    captured: dict = {}

    def _modify(baselines):
        # Read the prior baseline INSIDE the lock so the verdict and the value we
        # commit see the same number. Sibling ratchets share this file and this
        # lock (core/config/conventions/audit-baselines.md); without the locked
        # RMW two writers each "ratchet" against an already-stale baseline and
        # the second silently reverts the first.
        if not isinstance(baselines, dict):
            baselines = {}
        entry = baselines.get(KEY) or {}
        prior = entry.get("baseline")
        cur = current["total"]

        if prior is None:
            verdict, new_baseline = "seeded", cur
            message = (f"Seeded baseline at {cur} orphaned trace(s) across "
                       f"{current['agents_scanned']} agent(s). Future runs compare against it.")
        elif cur > prior:
            verdict, new_baseline = "regressed", prior  # never raise the baseline
            message = (f"WARN: orphaned traces grew from baseline {prior} to {cur} (+{cur - prior}). "
                       "A trace whose index write failed is INVISIBLE to retrieve.sh — the "
                       "knowledge is on disk and unreachable. Check for experience-add callers "
                       "reading .id off an error object (g-115-3786; root cause g-115-3780).")
        elif cur < prior:
            verdict, new_baseline = "ratcheted", cur
            message = (f"OK: orphaned traces shrank from baseline {prior} to {cur} "
                       f"(-{prior - cur}). Baseline lowered.")
        else:
            verdict, new_baseline = "stable", prior
            message = f"OK: orphaned traces stable at baseline {cur}."

        history = entry.get("history") or []
        history.append({
            "recorded_at": now_iso,
            "drift_total": cur,
            "verdict": verdict,
            "breakdown": dict(current["breakdown"]),
        })
        baselines[KEY] = {
            "baseline": new_baseline,
            "last_recorded": now_iso,
            "last_verdict": verdict,
            "join_key": "content_path_basename",  # see module docstring — the other key differs
            "history": history[-50:],
        }
        captured.update(verdict=verdict, new_baseline=new_baseline, message=message)
        return baselines

    if args.dry_run:
        entry = {}
        try:
            import yaml  # type: ignore
            if BASELINES_PATH.is_file():
                entry = (yaml.safe_load(BASELINES_PATH.read_text(encoding="utf-8")) or {}).get(KEY) or {}
        except Exception:
            entry = {}
        prior = entry.get("baseline")
        captured.update(
            verdict="dry-run",
            new_baseline=prior,
            message=f"current={current['total']} prior_baseline={prior} (no write)",
        )
    else:
        try:
            locked_modify_yaml(BASELINES_PATH, _modify, initial={})
        except Exception as e:
            print(f"WARN: could not persist baseline to {BASELINES_PATH}: {e}", file=sys.stderr)
            captured.setdefault("verdict", "error")
            captured.setdefault("new_baseline", None)
            captured.setdefault("message", f"baseline operation failed: {e}")

    result = {
        "verdict": captured["verdict"],
        "baseline": captured["new_baseline"],
        "current": current,
        "join_key": "content_path_basename",
        "message": captured["message"],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[experience-orphan-ratchet] {captured['verdict'].upper()}: {captured['message']}")
        print(f"  per-agent: {current['breakdown']}")

    if os.environ.get("VERIFY_LEARNING_DRIFT_HARD_GATE") == "1":
        return 1 if captured["verdict"] == "regressed" else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
