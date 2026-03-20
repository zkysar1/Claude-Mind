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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import MIND_DIR
from wm import read_wm, WM_PATH  # noqa: E402

CHECKPOINT_PATH = MIND_DIR / "session" / "compact-checkpoint.yaml"


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

    checkpoint = {
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "compact_count": compact_count,
        "session_id": wm.get("session_id"),
        "trigger": hook_input.get("trigger", "auto"),
        # encoding_queue is TOP-LEVEL in working-memory.yaml (not inside slots)
        "encoding_queue": wm.get("encoding_queue", []),
        "prior_encoding_items": prior_encoding if compact_count > 1 else [],
        "last_goal_category": wm.get("last_goal_category", ""),
        # Slots that may be lost during compaction
        "active_context": slots.get("active_context"),
        "micro_hypotheses": slots.get("micro_hypotheses", []),
        "knowledge_debt": slots.get("knowledge_debt", []),
        "known_blockers": slots.get("known_blockers", []),
        # Retrieval manifest — survives compaction for Phase 4.26 utilization feedback
        "retrieval_manifest": retrieval_manifest,
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
    slot_count = sum(1 for v in [
        checkpoint["active_context"],
        checkpoint["micro_hypotheses"],
        checkpoint["knowledge_debt"],
        checkpoint["known_blockers"],
        checkpoint["retrieval_manifest"],
    ] if v)
    log(f"saved checkpoint #{compact_count}: {eq_count} encoding, {slot_count} slots, {prior_count} prior")

    # Clear context reads tracker — post-compaction context may not retain file contents
    context_reads_path = MIND_DIR / "session" / "context-reads.txt"
    if context_reads_path.exists():
        context_reads_path.unlink()
        log("cleared context-reads tracker")


if __name__ == "__main__":
    main()
