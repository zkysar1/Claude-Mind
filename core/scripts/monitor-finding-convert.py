#!/usr/bin/env python3
"""monitor-finding-convert.py -- FW-1b finding->goal converter ( / ).

The value-preserving half of FW-1b. When a demoted monitoring probe TRIPS (run by
monitor-tick.py), this converts the trip into ONE deduped Investigate/Unblock goal
filed into the probe's target_asp, with origin_signal "<prefix>:<probe-id>" and the
trip evidence in the description. The scan leaves the goal queue; the FINDING
re-enters it -- but only when there is work, and only ONCE per open finding
(guard-441 orphan-linkage answered; rb-428 dedup discipline).

Modeled on the existing finding->goal converters this generalizes:
  - g-115-105  (loop-stall warnings -> backlog goals)
  - g-115-754 / insight-trigger-sweep.py (insight_triggers -> goals; the canonical conversion)
  - silent-gap-audit.py file_investigate() (the _rt daemon add-goal + Duplication-override pattern)

DEDUP (the idempotency mechanism): before filing, scan OPEN goals (world+agent) for
the probe's origin_signal. A probe that trips every tick until fixed files ONE open
goal, not N -- the prior cadence's goal is now an open goal carrying the origin_signal,
so this scan suppresses the re-file. (Same pattern as the rb-428 sweep family.)

Importable: convert_finding(probe, evidence, ...) is the unit-tested seam; the test
injects `goals` (a fake open-goal corpus) and `filer` (a fake daemon) to stay hermetic.
The CLI path resolves both from the live daemon via _rt.

Guards: guard-614 (JSON stdout), guard-645 (field reads with defaults), guard-549
(ASCII output), guard-759 (no /tmp). FAIL-OPEN: a converter error must never abort the
monitor-tick that called it -- the CLI exits 0 and reports {"filed": false, "error": ...}.
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
try:
    import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)
except Exception:  # pragma: no cover - _rt always importable in-tree
    _rt = None


def origin_signal_for(probe):
    """origin_signal = '<prefix>:<probe-id>' (drives dedup + traceability)."""
    prefix = (probe.get("origin_signal_prefix") or "monitor-probe").strip()
    pid = (probe.get("id") or "unknown").strip()
    return "%s:%s" % (prefix, pid)


def _goal_text(g):
    return " ".join(str(g.get(k) or "") for k in ("title", "description", "origin_signal"))


def is_duplicate(origin_signal, probe_id, open_goals):
    """A finding is already covered when ANY open goal carries this origin_signal,
    OR its text references the probe id (handles a goal filed before the prefix
    convention, or hand-filed). Conservative: a single open match suppresses the
    re-file (the rb-428 idempotency contract)."""
    osig = (origin_signal or "").strip()
    pid = (probe_id or "").strip()
    for g in open_goals or []:
        if not isinstance(g, dict):
            continue
        if (g.get("origin_signal") or "").strip() == osig and osig:
            return True
        text = _goal_text(g)
        if osig and osig in text:
            return True
        if pid and pid in text:
            return True
    return False


def build_record(probe, evidence):
    """Construct the goal record for a tripped probe. Defaults keep the record
    valid when the registry entry is minimal (guard-645)."""
    pid = (probe.get("id") or "unknown").strip()
    osig = origin_signal_for(probe)
    title = (probe.get("title") or "Investigate: monitor-tick probe %s tripped" % pid).strip()
    ev = (evidence or "").strip()
    if len(ev) > 1800:
        ev = ev[:1800] + " ...[truncated]"
    desc = (
        "Demoted monitoring probe '%s' TRIPPED (FW-1b monitor-tick, g-317-13).\n\n"
        "Probe script: %s\n"
        "on_trip: %s\n"
        "Trip evidence (probe stdout/stderr):\n%s\n\n"
        "This goal is the value-preserving reaction to a probe that no longer "
        "occupies a selection slot while clean. Investigate the tripped condition; "
        "when resolved, the probe returns clean and files nothing further. "
        "Dedup origin_signal: %s." % (
            pid, probe.get("script") or "(unset)", probe.get("on_trip") or "file_goal",
            ev or "(no output captured)", osig)
    )
    return {
        "title": title[:140],
        "description": desc,
        "priority": (probe.get("priority") or "MEDIUM").strip().upper(),
        "participants": ["agent"],
        "category": (probe.get("category") or "framework-architecture").strip(),
        "intended_agent": "either",
        "origin_signal": osig,
        "tags": ["monitor-tick", "fw-1b", pid],
    }


def _read_open_goals(source):
    if _rt is None:
        return []
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except Exception as e:  # _rt.RtError or transport error
        print("[monitor-finding-convert] WARN: read %s goals failed: %s" % (source, e),
              file=sys.stderr)
        return []
    try:
        data = _rt.tolerant_decode_aggregate("monitor-finding-convert: %s" % source, out)
    except Exception:
        return []
    goals = []
    for asp in (data or []):
        if isinstance(asp, dict):
            for g in asp.get("goals", []) or []:
                if isinstance(g, dict) and g.get("status") in ("pending", "in-progress"):
                    goals.append(g)
    return goals


def _default_filer(target_asp, record, source, origin_signal):
    """Real daemon filer: _rt add-goal with a Duplication override justified by
    THIS converter's own (stricter) origin_signal dedup having already passed."""
    if _rt is None:
        raise RuntimeError("_rt unavailable")
    override = {"Duplication": (
        "monitor-tick dedup confirmed origin_signal '%s' not carried by any open "
        "goal (world+agent active scan)" % origin_signal)}
    resp = _rt.aspirations_add_goal(target_asp, record, source=source, overrides=override)
    gid = None
    if isinstance(resp, dict):
        g = resp.get("goal")
        if isinstance(g, dict):
            gid = g.get("id")
        gid = gid or resp.get("id")
    return gid


def convert_finding(probe, evidence, *, source="world", goals=None, filer=None,
                    dry_run=False):
    """Convert a tripped probe into ONE deduped goal. Returns a result dict.

    Injection seams (hermetic test): `goals` overrides the open-goal corpus
    (default: live daemon read of world+agent); `filer(target_asp, record,
    source, origin_signal) -> goal_id` overrides the daemon add-goal call.
    Fail-open: any error yields {"filed": false, "error": ...}, never raises.
    """
    osig = origin_signal_for(probe)
    pid = (probe.get("id") or "unknown").strip()
    on_trip = (probe.get("on_trip") or "file_goal").strip()
    target_asp = (probe.get("target_asp") or "asp-115").strip()
    result = {"probe_id": pid, "origin_signal": osig, "on_trip": on_trip,
              "filed": False, "deduped": False, "goal_id": None, "error": None}

    if on_trip == "post_finding":
        # Findings-board path: dedup is recency-based at the board layer; we keep
        # the converter focused on the file_goal value-path and post a finding.
        result["error"] = "post_finding not handled by converter (post via board)"
        return result

    # Dedup against open goals (live read unless injected).
    if goals is None:
        open_goals = _read_open_goals("world") + _read_open_goals("agent")
    else:
        open_goals = goals
    if is_duplicate(osig, pid, open_goals):
        result["deduped"] = True
        return result

    record = build_record(probe, evidence)
    if dry_run:
        result["record"] = record
        return result

    fn = filer or _default_filer
    try:
        gid = fn(target_asp, record, source, osig)
        result["filed"] = gid is not None
        result["goal_id"] = gid
    except Exception as e:
        result["error"] = str(e)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="FW-1b finding->goal converter: a tripped monitor-tick probe "
                    "-> ONE deduped Investigate goal. (g-317-13)")
    ap.add_argument("--probe-id", required=True)
    ap.add_argument("--script", default="")
    ap.add_argument("--on-trip", default="file_goal", choices=["file_goal", "post_finding"])
    ap.add_argument("--target-asp", default="asp-115")
    ap.add_argument("--origin-prefix", default="monitor-probe")
    ap.add_argument("--title", default="")
    ap.add_argument("--priority", default="MEDIUM")
    ap.add_argument("--category", default="framework-architecture")
    # WORLD_AGENT_ONLY: cross-agent routes via MIND_AGENT env override ()
    ap.add_argument("--source", default="world", choices=["world", "agent"])
    ap.add_argument("--evidence", default="")
    ap.add_argument("--evidence-file", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    evidence = a.evidence
    if a.evidence_file:
        try:
            evidence = Path(a.evidence_file).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print("[monitor-finding-convert] WARN: evidence-file unreadable: %s" % e,
                  file=sys.stderr)
    probe = {
        "id": a.probe_id, "script": a.script, "on_trip": a.on_trip,
        "target_asp": a.target_asp, "origin_signal_prefix": a.origin_prefix,
        "title": a.title or None, "priority": a.priority, "category": a.category,
    }
    result = convert_finding(probe, evidence, source=a.source, dry_run=a.dry_run)
    print(json.dumps(result))
    # FAIL-OPEN: always exit 0 -- a convert failure must never abort monitor-tick.
    raise SystemExit(0)


if __name__ == "__main__":
    main()
