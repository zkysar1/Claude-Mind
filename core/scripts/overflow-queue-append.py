#!/usr/bin/env python3
"""Append a curator-rejected insight to the agent's overflow queue.

g-115-11580 — the curator demotion previously wrote the single scalar WM
slot `curator_overflow` (via `wm-set.sh`), which had NO reader:
/aspirations-consolidate reads a DIFFERENT store, `agents/<agent>/session/
overflow-queue.yaml` (Step 0.1 triage gate + Overflow Queue Management), and
`wm-set` REPLACES the slot, so a second rejection in one session overwrote
the first. On top of that `wm-prune.sh` age-evicts untouched scalar slots
after 120 minutes (core/config/memory-pipeline.yaml evict_threshold_minutes),
so even a late reader would find the slot already gone (live loss measured
by bravo 2026-09-30: evicted_slots [{"slot": "curator_overflow",
"minutes_stale": 158}]).

This is the writer half of the fix. It APPENDS one item to the file that
consolidation actually reads, so all three defects die at once:

  * no reader          -> the target file IS consolidation's store;
  * single-slot clobber -> append, never replace (two rejections both
                          survive — the goal's positive control);
  * 120-min age loss   -> overflow-queue.yaml is a session-manifest file
                          (sync_tier: continuity), not a WM slot; wm-prune
                          does not age it out.

Input (stdin, JSON): the SAME shape the demotion call site already composed
for wm-set.sh, plus one optional field:
    {
      "observation":   "<insight text>",        (required, non-empty)
      "target_node":   "<node key>",            (required)
      "curator_score": 0.31,                    (required, number)
      "reason":        "below_threshold",       (optional)
      "source_goal":   "g-NNN-MMMM",            (optional; the goal whose
                                                 encoding was rejected)
      "category":      "<goal category>",       (optional)
      "verified_values": { ... }                (optional; exact evidence
                                                 tokens the item was carried
                                                 with — rides through to the
                                                 reader so encoding does not
                                                 re-verify from prose)
    }

Output (stdout): one JSON line describing the write:
    {"ok": true, "queue_path": "...", "items_total": 2, "appended": {...}}
    or {"ok": false, "error": "<reason>"} with exit 1.

Refusals (exit 1, file untouched): no agent bound; missing/empty
`observation` or `target_node`; `curator_score` not a number; existing file
that is neither a YAML list nor a mapping with an `items`/`queue` list
(refusing rather than clobbering unreadable prior work); malformed stdin.

The file shape is a top-level LIST of item mappings. That is the FIRST
branch consolidation-precheck.py already reads
(`isinstance(overflow, list) -> len(overflow)`) and the shape the skill's
per-item fields (original_score, current_score, deferred_count, first_seen,
...) enumerate. Item fields:

    observation, target_node, curator_score, reason, source_goal, category,
    verified_values (optional), original_score, current_score,
    deferred_count, first_seen, session_first_seen, origin, appended_at

`origin` is "curator_gate" so a reader can tell a curator demotion from a
consolidation-deferred item. `first_seen`/`session_first_seen` are naive UTC
ISO 8601 (naming rule; `date +%Y-%m-%dT%H:%M:%S` posture).

The 20-item queue cap and the re-encounter boost/decay are CONSUMPTION
policy — they belong to /aspirations-consolidate's Overflow Queue Management
step, which owns the file between sessions. This writer appends and stops;
inventing a second cap here would be a drifting copy of a bound consolidation
already owns (same class as the caller-side interval check g-306-432
forbade).
"""

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

# Self-destruct after 10s — prevents zombie processes (Windows compat)
_timer = threading.Timer(10, lambda: sys._exit(0))
_timer.daemon = True
_timer.start()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print(json.dumps({"ok": False, "error": "PyYAML not installed"}))
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import AGENT_DIR  # noqa: E402


def _fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(1)


def _read_queue(queue_path):
    """Return the existing queue as a list, or [] if the file is absent.

    Refuses (via _fail) any existing file that is neither an empty file,
    a YAML list, nor a mapping holding an `items`/`queue` list — clobbering
    unreadable prior work would be the same silent loss this fix removes.
    """
    if not os.path.exists(queue_path):
        return []
    with open(queue_path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return []
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        _fail(f"existing {queue_path} is not parseable YAML ({e}); "
              "refusing to overwrite it")
    if doc is None:
        return []
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("items", "queue"):
            if isinstance(doc.get(key), list):
                return doc[key]
    _fail(f"existing {queue_path} is neither a list nor a mapping with an "
          "'items'/'queue' list; refusing to overwrite it")


def _write_queue(queue_path, items):
    """Atomic write (temp file + os.replace), top-level list shape."""
    queue_dir = os.path.dirname(queue_path)
    os.makedirs(queue_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=queue_dir, prefix=".overflow-queue.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(items, f, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)
        os.replace(tmp, queue_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    if AGENT_DIR is None:
        _fail("no agent bound (AGENT_DIR is None)")

    raw = sys.stdin.read()
    try:
        item = json.loads(raw)
    except json.JSONDecodeError as e:
        _fail(f"stdin is not valid JSON ({e})")
    if not isinstance(item, dict):
        _fail("stdin JSON must be an object")

    observation = str(item.get("observation") or "").strip()
    target_node = str(item.get("target_node") or "").strip()
    score = item.get("curator_score")
    if not observation:
        _fail("'observation' is required (non-empty string)")
    if not target_node:
        _fail("'target_node' is required (non-empty string)")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        _fail("'curator_score' is required (number)")

    now = datetime.now(timezone.utc).replace(tzinfo=None).strftime(
        "%Y-%m-%dT%H:%M:%S")
    entry = {
        "observation": observation,
        "target_node": target_node,
        "curator_score": score,
        "original_score": score,
        "current_score": score,
        "deferred_count": 1,
        "first_seen": now,
        "session_first_seen": now,
        "appended_at": now,
    }
    entry["origin"] = str(item.get("origin") or "curator_gate")
    for opt in ("reason", "source_goal", "category", "verified_values"):
        val = item.get(opt)
        if val not in (None, ""):
            entry[opt] = val

    queue_path = os.path.join(AGENT_DIR, "session", "overflow-queue.yaml")
    items = _read_queue(queue_path)
    items.append(entry)
    _write_queue(queue_path, items)

    print(json.dumps({"ok": True, "queue_path": queue_path,
                      "items_total": len(items), "appended": entry}))
    sys.exit(0)


if __name__ == "__main__":
    main()
