#!/usr/bin/env python3
"""Generic trigger-firings counter store.

g-304-07: surfaces "which encoding / retrieval triggers actually fire across the
loop" — telemetry that's invisible from the trigger catalogs alone
(`core/config/conventions/encoding-triggers.md`, `retrieval-triggers.md`).

Counter store: `meta/trigger-firings.jsonl`
Row schema:  {"trigger_id": str, "ts": ISO, "agent": str, "sid": str (8-char),
              "context": dict (optional)}

Append-only. FIFO-capped at TRIGGER_FIRINGS_CAP entries (default 5000) — when
the cap is exceeded, the file is rewritten with the trailing N entries.

USAGE — record from Python:
    from trigger_firings import record_firing
    record_firing("T1", context={"node": "foo", "score": 0.7})

USAGE — record from bash:
    bash core/scripts/trigger-firings.sh record T1 --context '{"node":"foo"}'

USAGE — report:
    py -3 core/scripts/trigger-firings.py report
    py -3 core/scripts/trigger-firings.py report --since 24h
    py -3 core/scripts/trigger-firings.py report --trigger T1
    py -3 core/scripts/trigger-firings.py report --json

Fail-open: any write failure logs to stderr but never crashes the caller.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Reuse path discipline + locked I/O from the rest of the framework.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _paths import META_DIR  # noqa: E402
from _fileops import (  # noqa: E402
    locked_append_jsonl,
    acquire_lock,
    release_lock,
)

FIRINGS_PATH = META_DIR / "trigger-firings.jsonl" if META_DIR else None
TRIGGER_FIRINGS_CAP = 5000


def record_firing(trigger_id, context=None):
    """Append a {trigger_id, ts, agent, sid, context} row to trigger-firings.jsonl.

    Fail-open: any write failure logs to stderr but never raises.
    """
    if not trigger_id:
        return
    if FIRINGS_PATH is None:
        # No meta dir resolved (config not loaded). Silent no-op.
        return
    agent = os.environ.get("MIND_AGENT", "unknown")
    sid_full = os.environ.get("MIND_SID", "")
    sid = sid_full[:8] if isinstance(sid_full, str) and sid_full else "unknown"
    row = {
        "trigger_id": str(trigger_id),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "sid": sid,
    }
    if isinstance(context, dict) and context:
        # Soft size cap on context to prevent runaway entries; serialize-check.
        try:
            payload = json.dumps(context, ensure_ascii=True)
            if len(payload) <= 2000:
                row["context"] = context
            else:
                row["context"] = {"_truncated": True,
                                  "_size": len(payload)}
        except Exception:
            row["context"] = {"_unserializable": True}
    try:
        FIRINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        locked_append_jsonl(FIRINGS_PATH, row)
    except Exception as e:
        print(f"[trigger-firings] append failed for {trigger_id}: {e}",
              file=sys.stderr)
        return

    # Lazy FIFO cap: only check on every Nth call to avoid scanning every row.
    # Pick N=50 so the cap is enforced within ~1% over the limit. This is
    # cheaper than locking + counting every call.
    if hash((row["ts"], row["trigger_id"])) % 50 == 0:
        _enforce_cap()


def _enforce_cap():
    """If trigger-firings.jsonl exceeds TRIGGER_FIRINGS_CAP, rewrite with the
    trailing N entries. Locked to avoid concurrent append clobbering.
    """
    if FIRINGS_PATH is None or not FIRINGS_PATH.exists():
        return
    lock_path = FIRINGS_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        try:
            with open(FIRINGS_PATH, encoding="utf-8") as f:
                rows = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if len(rows) > TRIGGER_FIRINGS_CAP:
                rows = rows[-TRIGGER_FIRINGS_CAP:]
                # Write inside the SAME lock — locked_write_jsonl would try
                # to re-acquire, so write directly.
                tmp = FIRINGS_PATH.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=True) + "\n")
                os.replace(str(tmp), str(FIRINGS_PATH))
        finally:
            release_lock(lock_path)
    except Exception as e:
        print(f"[trigger-firings] cap enforce failed: {e}", file=sys.stderr)


def _parse_since(s):
    if not s:
        return None
    s = s.strip().lower()
    now = datetime.now()
    if s.endswith("d"):
        return now - timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return now - timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return now - timedelta(minutes=int(s[:-1]))
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        sys.stderr.write(f"unparseable --since value: {s!r}\n")
        sys.exit(2)


def _load_firings(since_dt=None, trigger_filter=None, agent_filter=None):
    if FIRINGS_PATH is None or not FIRINGS_PATH.exists():
        return []
    out = []
    with open(FIRINGS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_dt:
                try:
                    ts = datetime.fromisoformat(row.get("ts", ""))
                except (ValueError, TypeError):
                    continue
                if ts < since_dt:
                    continue
            if trigger_filter and row.get("trigger_id") != trigger_filter:
                continue
            if agent_filter and row.get("agent") != agent_filter:
                continue
            out.append(row)
    return out


def cmd_record(args):
    """Record a single firing from CLI."""
    context = None
    if args.context:
        try:
            context = json.loads(args.context)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"--context not valid JSON: {e}\n")
            sys.exit(2)
    record_firing(args.trigger_id, context=context)


def cmd_report(args):
    """Print firing counts and recent entries."""
    since_dt = _parse_since(args.since)
    rows = _load_firings(since_dt=since_dt, trigger_filter=args.trigger,
                          agent_filter=args.agent)
    by_trigger = {}
    by_agent = {}
    for r in rows:
        tid = r.get("trigger_id", "")
        if tid:
            by_trigger[tid] = by_trigger.get(tid, 0) + 1
        ag = r.get("agent", "")
        if ag:
            by_agent[ag] = by_agent.get(ag, 0) + 1
    if args.json:
        print(json.dumps({
            "window_since": args.since or "all_time",
            "total_firings": len(rows),
            "by_trigger": dict(sorted(by_trigger.items(),
                                       key=lambda kv: -kv[1])),
            "by_agent": by_agent,
            "recent": rows[-20:],
        }, indent=2, default=str))
    else:
        print(f"=== trigger-firings report ===")
        print(f"  window: {args.since or 'all_time'}")
        if args.trigger:
            print(f"  filter trigger: {args.trigger}")
        if args.agent:
            print(f"  filter agent: {args.agent}")
        print(f"  total firings: {len(rows)}")
        print()
        if not rows:
            print("  (no firings recorded yet — "
                  "instrument trigger sites with record_firing() to populate)")
            return 0
        print("  by trigger:")
        for tid, n in sorted(by_trigger.items(), key=lambda kv: -kv[1]):
            print(f"    {tid:20}  {n}")
        print()
        print("  by agent:")
        for ag, n in sorted(by_agent.items(), key=lambda kv: -kv[1]):
            print(f"    {ag:20}  {n}")
        print()
        print("  recent (last 10):")
        for r in rows[-10:]:
            ctx = r.get("context")
            ctx_s = f" ctx={json.dumps(ctx)[:60]}" if ctx else ""
            print(f"    [{r.get('ts','?')}] {r.get('trigger_id','?'):8} "
                  f"agent={r.get('agent','?'):8} sid={r.get('sid','?')}{ctx_s}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Append a firing row")
    rec.add_argument("trigger_id", help="Trigger ID (e.g., T1, R3, E11)")
    rec.add_argument("--context", help="JSON dict of context (optional)")

    rep = sub.add_parser("report", help="Print aggregate firings report")
    rep.add_argument("--since", default="", help="Time window: 7d, 24h, 30m, ISO")
    rep.add_argument("--trigger", help="Filter to one trigger_id")
    rep.add_argument("--agent", help="Filter to one agent")
    rep.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()
    dispatch = {"record": cmd_record, "report": cmd_report}
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main() or 0)
