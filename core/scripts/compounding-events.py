#!/usr/bin/env python3
"""Compounding-knowledge metric -- load-bearing retrieval event store ().

Phase 2 of g-303-18. Implements the design at
core/config/rationale/compounding-metric.md (Sections 4-9).

The signal this metric adds (NOT a rename of an existing one):
  - tree retrieval_count answers "was it retrieved?"
  - RB/guardrail utilization.times_helpful answers "was it referenced (helpful)?"
  - compounding_events answers "was it LOAD-BEARING -- did it shape a decision
    that produced an external artifact (commit / encoding write / blocker
    resolution)?"  (a strict superset-condition on top of "helpful").

This module is the STORAGE + VALIDATION + AGGREGATION layer (design Section 5
sidecar `world/compounding-events.jsonl`, append-only, script-only access). The
load-bearing JOIN that DECIDES which events to add lives in the iteration-close
wiring (design Section 6); this script only validates + appends what the wiring
computes, and folds the stored events into value-densities on read.

Anti-inflation rules enforced here (design Section 8), conservative per C2
(bias toward was_load_bearing=false on any ambiguity -- a false-negative is
recoverable, a false-positive corrupts downstream retirement signals):
  - C1 self-citation: an entry never compounds NOR counts as retrieved-in via
    its own source goal (retrieved_by_goal_id != source_goal_of_entry).
  - C1 temporal order: a load-bearing credit requires retrieved_at to PRECEDE
    the artifact write it rests on (retrieved_at < artifact_write_time).
  - C2 confidence gate: was_load_bearing=true requires confidence in
    {high, medium} plus a valid artifact_produced + artifact_ref.

guard-841 (value-density, not raw count): the headline metrics are RATES and
DISTINCT-goal counts (Section 9), never len(events). The aggregate output leads
with load_bearing_rate / compounding_reach / per-category density; raw totals
are labelled descriptive-only.

guard-809 (never auto-prune on zero): this script emits NO prune/merge/delete/
archive action. It produces ADVISORY ranking signals only (Section 10). The D2
tree-archival and D1 guardrail-retirement consumers are SEPARATE follow-up
goals and must treat zero-compounding as a "review first" rank input, never a
deletion trigger.

CLI:
  echo '<event-json>' | py -3 core/scripts/compounding-events.py add
  py -3 core/scripts/compounding-events.py aggregate --entry rb-2443
  py -3 core/scripts/compounding-events.py aggregate --category framework --total-entries 80
  py -3 core/scripts/compounding-events.py aggregate            # descriptive summary
"""

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from _paths import WORLD_DIR, assert_world_dir, AGENT_DIR, retrieval_session_path
from _fileops import locked_append_jsonl

_SCHEMA_VERSION = 1
_EXPERIMENT_VERSION = "compounding-v1"
_VALID_KINDS = ("reasoning_bank", "guardrail", "tree_node")
_VALID_ARTIFACTS = ("commit", "encoding_write", "blocker_resolution")
_VALID_CONFIDENCE = ("high", "medium")  # was_load_bearing=true requires >= medium (C2)
_STORE_BASENAME = "compounding-events.jsonl"


def store_path(override=None):
    """Resolve the sidecar store path. Test/override precedence:
    explicit override > COMPOUNDING_EVENTS_PATH env > WORLD_DIR/<basename>."""
    if override:
        return Path(override)
    env = os.environ.get("COMPOUNDING_EVENTS_PATH")
    if env:
        return Path(env)
    assert_world_dir("compounding-events")
    return WORLD_DIR / _STORE_BASENAME


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return _dt.datetime.fromisoformat(value.strip())
    except Exception:
        return None


def validate_event(event):
    """Return (ok: bool, error: str|None, normalized: dict|None).

    Enforces the Section 8 anti-inflation rules conservatively (C2). Any
    ambiguity on a load-bearing claim rejects rather than records a
    possibly-inflated credit.
    """
    if not isinstance(event, dict):
        return False, "event must be a JSON object", None
    e = dict(event)

    for f in ("entry_id", "entry_kind", "retrieved_by_goal_id", "retrieved_at"):
        if not e.get(f):
            return False, "missing required field: %s" % f, None
    if e["entry_kind"] not in _VALID_KINDS:
        return False, "entry_kind must be one of %s" % (_VALID_KINDS,), None
    if not isinstance(e.get("was_load_bearing"), bool):
        return False, "was_load_bearing (bool) required", None

    src = e.get("source_goal_of_entry")
    consumer = e["retrieved_by_goal_id"]

    # C1 self-citation: excluded entirely (numerator AND denominator). An entry
    # never compounds by being written, and a self-retrieval should not count
    # toward "distinct goals that retrieved it" either.
    if src and src == consumer:
        return (False,
                "C1 self-citation: retrieved_by_goal_id == source_goal_of_entry (excluded)",
                None)

    if e["was_load_bearing"]:
        # Strict load-bearing gate (C2 conservative).
        if not src:
            return (False,
                    "C1: was_load_bearing=true requires source_goal_of_entry "
                    "(self-citation guard cannot be enforced without it)", None)
        if e.get("confidence") not in _VALID_CONFIDENCE:
            return (False,
                    "C2: was_load_bearing=true requires confidence in %s" % (_VALID_CONFIDENCE,),
                    None)
        if e.get("artifact_produced") not in _VALID_ARTIFACTS:
            return (False,
                    "was_load_bearing=true requires artifact_produced in %s" % (_VALID_ARTIFACTS,),
                    None)
        if not e.get("artifact_ref"):
            return False, "was_load_bearing=true requires artifact_ref", None
        rt = _parse_iso(e["retrieved_at"])
        at = _parse_iso(e.get("artifact_write_time"))
        if rt is None or at is None:
            return (False,
                    "C1 temporal: was_load_bearing=true requires parseable "
                    "retrieved_at + artifact_write_time", None)
        if not (rt < at):
            return False, "C1 temporal: retrieved_at must precede artifact_write_time", None
    # was_load_bearing=false: a denominator (retrieved-in) event; the minimal
    # required fields above are sufficient. No artifact/confidence/temporal
    # constraints because no credit is being claimed.

    e.setdefault("experiment_version", _EXPERIMENT_VERSION)
    e["schema_version"] = _SCHEMA_VERSION
    return True, None, e


def append_event(event, *, path=None, recorded_at=None):
    """Validate then locked-append one event. Returns {ok, event} or {ok:false, error}."""
    ok, err, norm = validate_event(event)
    if not ok:
        return {"ok": False, "error": err}
    norm["recorded_at"] = recorded_at or _dt.datetime.now().isoformat(timespec="seconds")
    locked_append_jsonl(store_path(path), norm)
    return {"ok": True, "event": norm}


def load_events(path=None):
    """Tolerant read of the sidecar JSONL. Skips malformed lines; [] if absent."""
    p = store_path(path)
    out = []
    if not p.exists():
        return out
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue  # tolerant: a half-written line never breaks aggregation
    except Exception:
        return out
    return out


def aggregate_entry(entry_id, *, path=None, events=None):
    """Per-entry value-density (Section 9). DISTINCT-goal, not event-count (rule 4)."""
    evs = events if events is not None else load_events(path)
    for_entry = [e for e in evs if e.get("entry_id") == entry_id]
    retrieved_goals = {e.get("retrieved_by_goal_id")
                       for e in for_entry if e.get("retrieved_by_goal_id")}
    lb_goals = {e.get("retrieved_by_goal_id") for e in for_entry
                if e.get("was_load_bearing") and e.get("retrieved_by_goal_id")}
    n_ret = len(retrieved_goals)
    rate = (len(lb_goals) / n_ret) if n_ret else 0.0
    return {
        "entry_id": entry_id,
        "distinct_goals_retrieved": n_ret,
        "distinct_goals_load_bearing": len(lb_goals),
        # HEADLINE value-density (guard-841): "when retrieved, how often load-bearing?"
        "load_bearing_rate": round(rate, 4),
        # distinct downstream goals this entry has actually shaped (rule 4 dedup)
        "compounding_reach": len(lb_goals),
    }


def aggregate_category(category, *, total_entries=None, path=None, events=None):
    """Per-category compounding density (Section 9).

    Numerator (distinct entry x goal load-bearing events in the category) is
    event-derivable. The denominator -- count of entries ENCODED in the
    category -- is a store count, not derivable from events; the caller (a
    velocity report) supplies it via total_entries.
    """
    evs = events if events is not None else load_events(path)
    for_cat = [e for e in evs if e.get("category") == category]
    lb_pairs = {(e.get("entry_id"), e.get("retrieved_by_goal_id"))
                for e in for_cat if e.get("was_load_bearing")}
    compounding_entries = {e.get("entry_id") for e in for_cat if e.get("was_load_bearing")}
    out = {
        "category": category,
        "compounding_entries": len(compounding_entries),
        "load_bearing_distinct_goal_events": len(lb_pairs),
        "density": None,
        "density_note": ("density requires --total-entries (count of entries "
                         "encoded in this category); not event-derivable"),
    }
    if total_entries:
        try:
            te = int(total_entries)
            if te > 0:
                out["density"] = round(len(lb_pairs) / te, 4)
                out["total_entries"] = te
                out.pop("density_note", None)
        except Exception:
            pass
    return out


def summary(path=None, events=None):
    """Descriptive store summary. Raw counts are DESCRIPTIVE ONLY (guard-841):
    the retirement/weakness signal is the per-entry/per-category density."""
    evs = events if events is not None else load_events(path)
    lb = [e for e in evs if e.get("was_load_bearing")]
    return {
        "total_events": len(evs),
        "load_bearing_events": len(lb),
        "distinct_entries": len({e.get("entry_id") for e in evs if e.get("entry_id")}),
        "distinct_goals": len({e.get("retrieved_by_goal_id")
                               for e in evs if e.get("retrieved_by_goal_id")}),
        "note": ("raw counts above are descriptive only; the headline signal is "
                 "value-density (per-entry load_bearing_rate / per-category density) "
                 "-- use `aggregate --entry` or `aggregate --category` (guard-841). "
                 "This report emits NO retirement/prune/archive action (guard-809); "
                 "it is advisory ranking input to existing human/agent-gated review only."),
    }


# ---------------------------------------------------------------------------
# Iteration-close emission (design Section 6) -- the dormant wiring entry point.
# Self-gates on compounding_metric.enabled (default OFF). Ships INERT: until the
# flag flips, emit() no-ops. When ON it derives events from the retrieval
# manifest + the iteration's artifact signal, strict-HIGH-only (Section 7):
# load-bearing ONLY for entries EXPLICITLY cited (--cited entry_id:source_goal);
# every other retrieved entry is a non-load-bearing denominator event (needed
# for the Section 9 rate). add() applies the C1/temporal/C2 gates per event.
# ---------------------------------------------------------------------------

def is_enabled():
    """Read the feature flag. Fail-CLOSED (dormant) on any error -- a flag we
    cannot read is treated as OFF, never ON. Test/override: COMPOUNDING_METRIC_ENABLED."""
    env = os.environ.get("COMPOUNDING_METRIC_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        import yaml  # local import: the flag-read must not hard-require yaml at module load
        cfg_path = Path(__file__).resolve().parents[2] / "core" / "config" / "aspirations.yaml"
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return bool((cfg.get("compounding_metric") or {}).get("enabled", False))
    except Exception:
        return False


def _default_manifest_path():
    if AGENT_DIR is None:
        return None
    return retrieval_session_path(AGENT_DIR)  # body-aware, 


# supplementary_detail `type` -> our entry_kind. pattern_signature is
# DELIBERATELY excluded (guard-575: pattern outcomes are recorded via
# reflect-on-outcome's CONFIRMED/CORRECTED path, not double-counted here).
_TYPE_TO_KIND = {"reasoning_bank": "reasoning_bank", "guardrail": "guardrail"}


def read_manifest_entries(path):
    """Return retrieved [{entry_id, entry_kind, retrieved_at}] from a
    retrieval-session.json. Tolerant: [] on missing/malformed/no-retrieval."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    supp = m.get("supplementary_detail") or []
    trees = m.get("tree_nodes_loaded") or []
    if m.get("retrieval_performed") is False and not supp and not trees:
        return []  # explicit no-retrieval manifest
    ts = m.get("timestamp") or m.get("retrieved_at")
    out = []
    for d in supp:
        kind = _TYPE_TO_KIND.get(d.get("type"))
        if kind and d.get("id"):
            out.append({"entry_id": d["id"], "entry_kind": kind, "retrieved_at": ts})
    for key in trees:
        if key:
            out.append({"entry_id": key, "entry_kind": "tree_node", "retrieved_at": ts})
    return out


def emit(goal_id, *, artifact_produced="commit", artifact_ref="",
         artifact_write_time="", cited=None, manifest_path=None,
         store=None, enabled=None):
    """Derive + append compounding events for one iteration-close (design Section 6).

    cited: {entry_id: source_goal_of_entry} explicit HIGH-confidence citations.
    Returns counts. NEVER raises (called fail-open from iteration-close)."""
    try:
        if enabled is None:
            enabled = is_enabled()
        if not enabled:
            return {"emitted_load_bearing": 0, "emitted_denominator": 0,
                    "skipped": 0, "reason": "disabled"}
        cited = cited or {}
        mp = manifest_path if manifest_path is not None else _default_manifest_path()
        retrieved = read_manifest_entries(mp)
        res = {"emitted_load_bearing": 0, "emitted_denominator": 0, "skipped": 0}
        for ent in retrieved:
            eid = ent["entry_id"]
            ev = {
                "entry_id": eid,
                "entry_kind": ent["entry_kind"],
                "retrieved_by_goal_id": goal_id,
                "retrieved_at": ent.get("retrieved_at"),
            }
            if eid in cited:
                ev.update({
                    "was_load_bearing": True,
                    "confidence": "high",                  # explicit citation == HIGH (Section 7)
                    "artifact_produced": artifact_produced,
                    "artifact_ref": artifact_ref,
                    "artifact_write_time": artifact_write_time,
                    "source_goal_of_entry": cited[eid],
                })
            else:
                ev["was_load_bearing"] = False             # denominator (retrieved-in)
            out = append_event(ev, path=store)
            if out.get("ok"):
                if ev["was_load_bearing"]:
                    res["emitted_load_bearing"] += 1
                else:
                    res["emitted_denominator"] += 1
            else:
                res["skipped"] += 1   # add() rejected (C1/temporal/C2) -- conservative
        return res
    except Exception as ex:
        # Fail-open: emission must NEVER break the iteration-close path.
        return {"emitted_load_bearing": 0, "emitted_denominator": 0,
                "skipped": 0, "reason": "error: %s" % ex}


def _cmd_add(args):
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except Exception as ex:
        print(json.dumps({"ok": False, "error": "invalid JSON on stdin: %s" % ex}),
              file=sys.stderr)
        return 1
    res = append_event(event, path=args.store)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def _cmd_aggregate(args):
    if args.entry:
        out = aggregate_entry(args.entry, path=args.store)
    elif args.category:
        out = aggregate_category(args.category, total_entries=args.total_entries, path=args.store)
    else:
        out = summary(path=args.store)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _cmd_emit(args):
    cited = {}
    for c in (args.cited or []):
        if ":" in c:
            eid, src = c.split(":", 1)
            if eid.strip() and src.strip():
                cited[eid.strip()] = src.strip()
    res = emit(
        args.goal,
        artifact_produced=args.artifact_produced,
        artifact_ref=args.artifact_ref,
        artifact_write_time=args.artifact_write_time,
        cited=cited,
        manifest_path=args.manifest,
        store=args.store,
    )
    print(json.dumps(res, ensure_ascii=False))
    return 0  # fail-open: emission never fails the caller


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Compounding-knowledge metric event store (g-303-35).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--store",
        help=("override store path (testing); else COMPOUNDING_EVENTS_PATH env "
              "or WORLD_DIR/compounding-events.jsonl"))
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add", parents=[common],
                        help="validate + locked-append one event (JSON on stdin)")
    pa.set_defaults(func=_cmd_add)
    pg = sub.add_parser("aggregate", parents=[common],
                        help="read-time value-density folds (guard-841)")
    pg.add_argument("--entry", help="per-entry load-bearing rate + compounding reach")
    pg.add_argument("--category", help="per-category compounding density")
    pg.add_argument("--total-entries", type=int,
                    help="denominator (entries encoded in category) for per-category density")
    pg.set_defaults(func=_cmd_aggregate)
    pe = sub.add_parser(
        "emit", parents=[common],
        help="iteration-close emission (self-gates on compounding_metric.enabled; Section 6)")
    pe.add_argument("--goal", required=True, help="the closing goal id (retrieved_by_goal_id)")
    pe.add_argument("--artifact-produced", default="commit",
                    choices=["commit", "encoding_write", "blocker_resolution"])
    pe.add_argument("--artifact-ref", default="", help="commit-sha / node-key / blocker-id")
    pe.add_argument("--artifact-write-time", default="", help="ISO timestamp the artifact was written")
    pe.add_argument("--manifest", help="retrieval-session.json (default AGENT_DIR/session/retrieval-session.json)")
    pe.add_argument("--cited", action="append",
                    help="entry_id:source_goal explicit HIGH-confidence citation (repeatable)")
    pe.set_defaults(func=_cmd_emit)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
