#!/usr/bin/env python3
"""Consolidation precheck — check all encoding queues in one shot.

Returns JSON verdict: FULL (encoding work exists) or FAST (queues empty).
Called by the aspirations orchestrator and /stop before deciding whether to
invoke the full /aspirations-consolidate skill or load the housekeeping digest.

IMPORTANT: This logic must match Step 0.1 (CONSOLIDATION TRIAGE GATE) in
aspirations-consolidate/SKILL.md. If you add a check there, add it here too.

Reads five files directly (no subprocess spawning):
  1. <agent>/session/working-memory.yaml  — micro_hypotheses, encoding_queue,
     knowledge_debt, conclusions, violations, sensory_buffer
  2. <world>/pipeline.jsonl + pipeline-archive.jsonl — REFLECTABLE unreflected
     hypotheses (shared _reflectable filter — g-115-6173; matches Step 0.1's
     endpoint-array + count_reflectable form on both corpus and predicate)
  3. <agent>/session/overflow-queue.yaml   — overflow encoding candidates
  4. <agent>/session/handoff.yaml          — consecutive_lean_sessions (ceiling)

Output (stdout): single-line JSON
  {"verdict":"FAST","micro_hypotheses":0,"unreflected":0,"encoding_queue":0,
   "knowledge_debt":0,"conclusions":0,"violations":0,"overflow_queue":0,
   "sensory_buffer":0,"lean_ceiling_hit":false,"total":0}

"total" = sum of all fields EXCEPT sensory_buffer (journal-only, not encoding).
verdict = "FULL" if total > 0 OR lean_ceiling_hit.
On any error: verdict = "FULL" (safe fallback — never skip encoding due to bug).
"""

import json
import sys
import threading
from pathlib import Path

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
    # Can't check — safe fallback
    print(json.dumps({"verdict": "FULL", "error": "PyYAML not installed"}))
    sys.exit(0)

from _paths import AGENT_DIR, WORLD_DIR
import _reflectable  # : shared reflectable-vs-backlog split


def _safe_len(val):
    """Return length of a list/dict, 0 for None/scalars."""
    if isinstance(val, (list, tuple)):
        return len(val)
    if isinstance(val, dict):
        return len(val)
    return 0


def main():
    result = {
        "verdict": "FULL",  # safe default
        "micro_hypotheses": 0,
        "unreflected": 0,
        "encoding_queue": 0,
        "knowledge_debt": 0,
        "conclusions": 0,
        "violations": 0,
        "overflow_queue": 0,
        "sensory_buffer": 0,
        "lean_ceiling_hit": False,
        "total": 0,
    }

    if AGENT_DIR is None:
        result["error"] = "no agent bound"
        print(json.dumps(result))
        return

    # --- 1. Working Memory (single YAML read) ---
    from wm import wm_path as _resolve_wm_path  # Phase 1A per-Body WM routing ()
    wm_path = _resolve_wm_path()
    if wm_path.exists():
        with open(wm_path, "r", encoding="utf-8") as f:
            wm = yaml.safe_load(f) or {}

        slots = wm.get("slots", {})

        result["micro_hypotheses"] = _safe_len(slots.get("micro_hypotheses"))
        result["knowledge_debt"] = _safe_len(slots.get("knowledge_debt"))
        result["sensory_buffer"] = _safe_len(slots.get("sensory_buffer"))
        # Must match Step 0.1 in aspirations-consolidate/SKILL.md
        result["conclusions"] = _safe_len(slots.get("conclusions"))
        result["violations"] = _safe_len(slots.get("recent_violations"))

        # encoding_queue is a top-level key, not inside slots
        result["encoding_queue"] = _safe_len(wm.get("encoding_queue"))

    # --- 2. Pipeline — REFLECTABLE unreflected hypotheses ---
    # : this block used to hand-roll live-only stage=="resolved",
    # which diverged from the widened --unreflected endpoint (live+archive,
    # stage in resolved/archived) the moment  landed — 0 here vs
    # 377 there on the same store. Parity is restored by matching BOTH halves
    # of the SKILL.md Step 0.1 instrument: same corpus (live + archive files —
    # this script's no-subprocess/10s-self-destruct contract forbids calling
    # the endpoint itself) and same predicate (the shared _reflectable filter;
    # Step 0.1 applies it to the endpoint's array). Reflectable-only is the
    # right semantic for a consolidation triage count: the widened TOTAL is
    # dominated by UNRESOLVABLE/EXPIRED/no-outcome records nothing can ever
    # reflect on (), and folding those into data_total made the
    # lean fast path structurally dead.
    # Union semantics mirror the endpoint (mind_api/src/world/pipeline.py
    # --unreflected branch): dedup by id with the LIVE copy winning (a record
    # can exist in BOTH stores — the archived-in-live tombstone case the
    # endpoint's union exists for; counting files independently double-counts
    # it), stage in (resolved, archived), not reflected — then the shared
    # reflectable filter, which is what Step 0.1 applies to the endpoint's
    # array. Same corpus + same predicate = same count (outcome 1).
    _by_id = {}
    for _pname in ("pipeline-archive.jsonl", "pipeline.jsonl"):  # live second: live wins
        pipeline_path = WORLD_DIR / _pname
        if not pipeline_path.exists():
            continue
        with open(pipeline_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("id")
                if rid is not None:
                    _by_id[rid] = rec
    result["unreflected"] = sum(
        1 for rec in _by_id.values()
        if rec.get("stage") in ("resolved", "archived")
        and not rec.get("reflected", False)
        and _reflectable.is_reflectable(rec)
    )

    # --- 3. Overflow queue ---
    overflow_path = AGENT_DIR / "session" / "overflow-queue.yaml"
    if overflow_path.exists():
        with open(overflow_path, "r", encoding="utf-8") as f:
            overflow = yaml.safe_load(f)
        if isinstance(overflow, list):
            result["overflow_queue"] = len(overflow)
        elif isinstance(overflow, dict):
            items = overflow.get("items", overflow.get("queue", []))
            result["overflow_queue"] = _safe_len(items)

    # --- 4. Lean ceiling — anti-suppression safety rail ---
    # Must match Step 0.1 SAFETY RAILS in aspirations-consolidate/SKILL.md
    handoff_path = AGENT_DIR / "session" / "handoff.yaml"
    if handoff_path.exists():
        with open(handoff_path, "r", encoding="utf-8") as f:
            handoff = yaml.safe_load(f) or {}
        meta = handoff.get("consolidation_meta", {})
        if isinstance(meta, dict):
            prior_lean = meta.get("consecutive_lean_sessions", 0)
            if isinstance(prior_lean, int) and prior_lean >= 3:
                result["lean_ceiling_hit"] = True

    # --- Verdict ---
    # sensory_buffer excluded from total (journal-only, not encoding work)
    total = (result["micro_hypotheses"]
             + result["unreflected"]
             + result["encoding_queue"]
             + result["knowledge_debt"]
             + result["conclusions"]
             + result["violations"]
             + result["overflow_queue"])
    result["total"] = total

    if result["lean_ceiling_hit"]:
        result["verdict"] = "FULL"
    elif total > 0:
        result["verdict"] = "FULL"
    else:
        result["verdict"] = "FAST"

    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Safe fallback — never skip encoding due to script bug
        print(json.dumps({"verdict": "FULL", "error": str(e)}))
