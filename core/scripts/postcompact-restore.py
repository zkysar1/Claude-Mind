#!/usr/bin/env python3
"""PostCompact restore — inject context after compaction.

Called by the SessionStart(compact) hook (via postcompact-restore.sh).
Reads the pre-compact checkpoint and prints a structured restoration message
to stdout. Claude Code injects this stdout into the agent's fresh context.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import AGENT_DIR

CHECKPOINT_PATH = AGENT_DIR / "session" / "compact-checkpoint.yaml"


def log(msg):
    # stderr only — stdout is the agent context injection channel
    print(f"[postcompact] {msg}", file=sys.stderr)


def main():
    if not CHECKPOINT_PATH.exists():
        log("no checkpoint file -- skip")
        return

    checkpoint = yaml.safe_load(CHECKPOINT_PATH.read_text(encoding="utf-8")) or {}

    encoding_queue = checkpoint.get("encoding_queue") or []
    prior_items = checkpoint.get("prior_encoding_items") or []
    all_encoding = encoding_queue + prior_items
    active = checkpoint.get("active_context") or {}
    micro = checkpoint.get("micro_hypotheses") or []
    debt = checkpoint.get("knowledge_debt") or []
    blockers = checkpoint.get("known_blockers") or []
    compact_count = checkpoint.get("compact_count", 1)

    lines = []
    lines.append("=== CONTEXT RESTORED (post-compaction) ===")
    lines.append(f"Compaction #{compact_count} this session. "
                 f"Session: {checkpoint.get('session_id', 'unknown')}")
    lines.append("")

    if active:
        summary = active.get("summary", "")
        if summary:
            lines.append(f"LAST CONTEXT: {summary[:300]}")
            lines.append("")

    if all_encoding:
        lines.append(f"ENCODING QUEUE: {len(all_encoding)} items pending")
        for i, item in enumerate(all_encoding[:5]):
            if isinstance(item, dict):
                obs = str(item.get("observation", ""))[:100]
                target = item.get("target_article", "?")
                score = item.get("encoding_score", 0)
                lines.append(f"  {i+1}. [{score:.1f}] {obs} -> {target}")
            else:
                lines.append(f"  {i+1}. {str(item)[:100]}")
        if len(all_encoding) > 5:
            lines.append(f"  ... and {len(all_encoding) - 5} more")
        lines.append("")

    counts = []
    blocker_details = []
    if micro:
        n = len(micro) if isinstance(micro, list) else 0
        if n:
            counts.append(f"micro-hypotheses: {n}")
    if debt:
        n = len(debt) if isinstance(debt, list) else 0
        if n:
            counts.append(f"knowledge debt: {n}")
    if blockers:
        unresolved = [b for b in blockers if isinstance(b, dict) and not b.get("resolution")]
        if unresolved:
            counts.append(f"blockers: {len(unresolved)}")
            for b in unresolved[:3]:
                blocker_details.append(f"  BLOCKER: {b.get('blocker_id', '?')}: "
                                       f"{str(b.get('reason', ''))[:80]}")
    if counts:
        lines.append(f"STATE: {', '.join(counts)}")
        lines.extend(blocker_details)
        lines.append("")

    # Retrieval manifest restoration
    manifest = checkpoint.get("retrieval_manifest")
    if manifest and isinstance(manifest, dict):
        goal_id = manifest.get("goal_id", "?")
        goal_title = manifest.get("goal_title", "?")
        nodes = manifest.get("tree_nodes_loaded") or []
        delib = manifest.get("deliberation") or {}
        active_items = delib.get("active_items") or []
        skipped_items = delib.get("skipped_items") or []
        pending = manifest.get("utilization_pending", False)

        lines.append(f"RETRIEVAL STATE: {goal_id} ({goal_title})")
        node_str = ", ".join(str(n) for n in nodes[:5])
        if len(nodes) > 5:
            node_str += f" +{len(nodes) - 5} more"
        lines.append(f"  Nodes: {node_str}")
        lines.append(f"  Deliberation: {len(active_items)} active, {len(skipped_items)} skipped")
        if pending:
            lines.append("  *** UTILIZATION FEEDBACK PENDING — Phase 4.26 did not complete before compaction ***")
            for item in active_items[:10]:
                if isinstance(item, dict):
                    lines.append(f"    ACTIVE: {item.get('id', '?')} ({item.get('type', '?')})")
        lines.append("")

    # Pending background agents warning
    pending_count = checkpoint.get("pending_agents_count", 0)
    if pending_count:
        lines.append(f"PENDING AGENTS: {pending_count} background agent(s) were running before compaction.")
        lines.append("  Their completion notifications will re-engage you. Collect results in Phase -0.5a.")
        lines.append("")

    lines.append("IDENTITY: Phase -0.5d will re-read self.md and program.md — identity context lost during compaction.")
    lines.append("")
    lines.append("ACTION: The stop hook will fire next. Re-enter /aspirations loop.")
    lines.append("Phase -0.5c will detect compact-checkpoint.yaml and process encoding queue")
    lines.append("in this fresh context before resuming goal execution.")
    lines.append("")
    lines.append("MANDATORY: Phase 2 requires `goal-selector.sh` — do NOT assume goal availability from memory.")
    lines.append("===========================================")

    parts = []
    if all_encoding:
        parts.append(f"{len(all_encoding)} encoding")
    if micro and isinstance(micro, list) and len(micro):
        parts.append(f"{len(micro)} micro-hyp")
    if debt and isinstance(debt, list) and len(debt):
        parts.append(f"{len(debt)} debt")
    if blockers:
        unresolved = [b for b in blockers if isinstance(b, dict) and not b.get("resolution")]
        if unresolved:
            parts.append(f"{len(unresolved)} blockers")
    if manifest and isinstance(manifest, dict):
        parts.append("retrieval-manifest")
    summary = ", ".join(parts) if parts else "minimal state"
    log(f"restored checkpoint #{compact_count}: {summary}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
