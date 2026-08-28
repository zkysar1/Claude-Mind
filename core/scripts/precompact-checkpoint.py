#!/usr/bin/env python3
"""PreCompact checkpoint — save encoding state before context compression.

Called by the PreCompact hook (via precompact-checkpoint.sh).
Reads working memory, writes a checkpoint file that survives compaction.
The aspirations loop Phase -0.5c consumes this checkpoint on re-entry.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime

#  / : force utf-8 on stdin/stdout/stderr (covers Windows
# cp1252 fallback when callers bypass the _platform.sh PYTHONIOENCODING=utf-8
# shim). Replaces the prior stdout/stderr-only inline fix — stdin was the
# gap that  acceptance (4) closes. Helper is idempotent.
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

import yaml

from _paths import AGENT_DIR, assert_agent_dir, body_state_path
from wm import read_wm, WM_PATH  # noqa: E402


def _box_identity():
    """MEASURED box identity for the checkpoint ().

    Reuses the EXISTING resolver in _session_telemetry (`MACHINE_ID` env, else
    socket.gethostname(), memoized, fail-safe to "unknown") rather than adding a
    second one -- there must be exactly one answer to "which box is this"
    (communication-clarity rule 5).

    WHY THE CHECKPOINT NEEDS IT: without a measured stamp, the only box identity
    present at restore is whatever the resume SUMMARY says in prose, and prose is
    exactly what goes stale. Originating incident: a resume summary carried a
    PARTNER's hostname and it reached 5 durable records before anyone ran
    `hostname`. A value measured at checkpoint-write time cannot be inherited
    from another box's narrative.

    FAIL-OPEN, always. Box identity is a rider on the checkpoint; slot recovery
    is its purpose. Any failure here returns "unknown" and the checkpoint still
    writes -- never let an identity stamp cost a slot restore.
    """
    ident = {"machine_id": "unknown", "platform_uname": "unknown"}
    try:
        from _session_telemetry import _machine_id
        ident["machine_id"] = _machine_id()
    except Exception:
        pass
    try:
        import platform
        ident["platform_uname"] = "%s %s" % (platform.system(), platform.release())
    except Exception:
        pass
    return ident

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("precompact-checkpoint")

# Body-keyed, agent-wide when unbodied (). Unlike postcompact-restore.sh,
# THIS hook's launcher has no runner-identity guard — it fires for any session with
# a bound agent, including a worker body. The write below is an os.replace, so
# without body-keying a worker body autocompacting clobbers the reducer's
# checkpoint outright. Bound to a constant (not resolved per-call) because this is
# a one-shot hook process: import time IS call time, and the existing tests that
# monkeypatch this constant keep working.
CHECKPOINT_PATH = body_state_path(AGENT_DIR.name, "compact-checkpoint.yaml")


def log(msg):
    print(f"[precompact] {msg}", file=sys.stderr)


def main():
    # Read hook input from stdin (JSON with session_id, transcript_path, trigger)
    hook_input = {}
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if not WM_PATH.exists():
        log("no working memory -- skip")
        return

    wm = read_wm()
    slots = wm.get("slots") or {}

    # Read existing checkpoint if precompact fired multiple times this session
    existing = {}
    if CHECKPOINT_PATH.exists():
        existing = yaml.safe_load(CHECKPOINT_PATH.read_text(encoding="utf-8")) or {}

    compact_count = existing.get("compact_count", 0) + 1

    # Accumulate prior encoding items across multiple compactions
    prior_encoding = existing.get("encoding_queue", [])
    if compact_count > 1 and prior_encoding:
        prior_all = existing.get("prior_encoding_items", [])
        prior_all.extend(prior_encoding)
        prior_encoding = prior_all

    # Retrieval manifest — top-level for direct access by postcompact-restore
    active_ctx = slots.get("active_context") or {}
    retrieval_manifest = active_ctx.get("retrieval_manifest")

    # Pending background agents (informational — actual data in pending-agents.yaml)
    pending_agents_file = AGENT_DIR / "session" / "pending-agents.yaml"
    pending_agents_count = 0
    if pending_agents_file.exists():
        try:
            pa_data = yaml.safe_load(pending_agents_file.read_text(encoding="utf-8")) or {}
            pending_agents_count = len(pa_data.get("agents", []))
        except Exception:
            pass

    checkpoint = {
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # MEASURED at write time on THIS box -- never inherited from summary
        # prose, which is the thing that goes stale ().
        "box_identity": _box_identity(),
        "compact_count": compact_count,
        "session_id": wm.get("session_id"),
        "trigger": hook_input.get("trigger", "auto"),
        # encoding_queue is TOP-LEVEL in working-memory.yaml (not inside slots)
        "encoding_queue": wm.get("encoding_queue", []),
        "prior_encoding_items": prior_encoding if compact_count > 1 else [],
        "last_goal_category": wm.get("last_goal_category", ""),
        # --- Full WM snapshot (captures ALL slots including dynamic ones) ---
        "all_slots": slots,
        "slot_meta": wm.get("slot_meta", {}),
        # Top-level WM keys that carry session state
        "goals_completed_this_session": wm.get("goals_completed_this_session", []),
        "aspiration_touched_last": wm.get("aspiration_touched_last", ""),
        # --- Legacy keys (backward compat with existing postcompact-restore) ---
        "active_context": slots.get("active_context"),
        "micro_hypotheses": slots.get("micro_hypotheses", []),
        "knowledge_debt": slots.get("knowledge_debt", []),
        "known_blockers": slots.get("known_blockers", []),
        # Retrieval manifest — survives compaction for Phase 4.26 utilization feedback
        "retrieval_manifest": retrieval_manifest,
        # Blocked-sleep timer — survives compaction for Phase -0.5e recovery
        "blocked_sleep_until": slots.get("blocked_sleep_until"),
        # Pending background agents — count only (file persists on disk)
        "pending_agents_count": pending_agents_count,
    }

    # Atomic write (tmp + rename)
    tmp = CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(checkpoint, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(CHECKPOINT_PATH))

    eq_count = len(checkpoint.get("encoding_queue") or [])
    prior_count = len(prior_encoding) if compact_count > 1 else 0
    total_slots = len(slots)
    non_null_slots = sum(1 for v in slots.values() if v is not None and v != [] and v != {})
    pa_msg = f", {pending_agents_count} agents" if pending_agents_count else ""
    log(f"saved checkpoint #{compact_count}: {eq_count} encoding, {non_null_slots}/{total_slots} slots, {prior_count} prior{pa_msg}")

    # The context-reads clear MOVED OUT of this file 2026-08-22 ().
    # It lived here as an inline `unlink()` of AGENT_DIR/session/context-reads.txt
    # — a hand-rolled second implementation of `context-reads.py clear` that was
    # pointed at the AGENT-WIDE path only. On a worker Body the tracker is
    # sessions/<SID>/body-context-reads.txt, so the unlink found nothing and the
    # manifest survived the compaction asserting in-context for evicted files.
    # It is now ONE implementation called from TWO places, both session-aware:
    #   precompact-checkpoint.sh    (pre-hoc, best-effort — this hook's wrapper,
    #                                which already resolves and exports $SID)
    #   sessionstart-orchestrator.sh source=compact (post-hoc, the guaranteed one)
    # Do not re-add a clear here: the wrapper's call covers this hook, and a
    # THIRD copy is how the first one drifted out of correctness unnoticed.


if __name__ == "__main__":
    main()
