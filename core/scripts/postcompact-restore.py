#!/usr/bin/env python3
"""PostCompact restore — inject context after compaction.

Called by the SessionStart(compact) hook (via postcompact-restore.sh).
Reads the pre-compact checkpoint and prints a structured restoration message
to stdout. Claude Code injects this stdout into the agent's fresh context.

Full-fidelity restore: includes all WM slots, loop state, execution diary,
and reasoning snapshot. No aggressive truncation — fresh context has full budget.
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

from _paths import AGENT_DIR

CHECKPOINT_PATH = AGENT_DIR / "session" / "compact-checkpoint.yaml"
DIARY_PATH = AGENT_DIR / "session" / "execution-diary.jsonl"
SNAPSHOT_PATH = AGENT_DIR / "session" / "reasoning-snapshot.yaml"

# Slots to skip in the "additional slots" section (already shown in dedicated sections)
DEDICATED_SECTION_SLOTS = {
    "active_context", "micro_hypotheses", "knowledge_debt",
    "known_blockers", "blocked_sleep_until",
}

# Scalar slots whose value is worth showing in full
SCALAR_SLOTS_FULL = {
    "active_strategy", "session_goal", "active_hypothesis",
    "active_constraints", "cross_domain_transfer", "pending_resolutions",
}


def log(msg):
    # stderr only — stdout is the agent context injection channel
    print(f"[postcompact] {msg}", file=sys.stderr)


def _truncate(s, maxlen=500):
    """Truncate a string with ellipsis marker if needed."""
    s = str(s)
    return s[:maxlen] + "..." if len(s) > maxlen else s


def _format_slot_value(value):
    """Format a single slot value for the restore message."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        return f"{len(value)} items"
    if isinstance(value, dict):
        if not any(v is not None for v in value.values()):
            return None
        # Show dict keys with non-null values
        keys = [k for k, v in value.items() if v is not None]
        return f"{{{', '.join(keys[:5])}{'...' if len(keys) > 5 else ''}}}"
    return _truncate(str(value), 300)


def _read_diary_entries(limit=10):
    """Read the last N entries from the execution diary."""
    if not DIARY_PATH.exists():
        return []
    entries = []
    try:
        with open(DIARY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        return []
    return entries[-limit:]


def _read_reasoning_snapshot():
    """Read the reasoning snapshot if present."""
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return yaml.safe_load(SNAPSHOT_PATH.read_text(encoding="utf-8")) or None
    except Exception:
        return None


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
    all_slots = checkpoint.get("all_slots") or {}

    lines = []
    lines.append("=== CONTEXT RESTORED (post-compaction) ===")
    lines.append(f"Compaction #{compact_count} this session. "
                 f"Session: {checkpoint.get('session_id', 'unknown')}")
    lines.append("")

    # --- Active context (NO truncation — full summary) ---
    if active:
        summary = active.get("summary", "")
        if summary:
            lines.append(f"LAST CONTEXT: {summary}")
            lines.append("")

    # --- Loop state (critical for loop continuity) ---
    loop_state = all_slots.get("loop_state")
    if loop_state and isinstance(loop_state, dict):
        parts = []
        for key in ["goals_completed", "productive_goals", "evolutions",
                     "routine_streak_global", "goals_since_last_tree_update",
                     "consecutive_blocked_sleeps"]:
            val = loop_state.get(key)
            if val is not None and val != 0:
                parts.append(f"{key}={val}")
        if parts:
            lines.append(f"LOOP STATE: {', '.join(parts)}")
            # Show touched aspirations if present
            touched = loop_state.get("touched_aspirations")
            if touched and isinstance(touched, list):
                lines.append(f"  Touched aspirations: {', '.join(str(a) for a in touched[:10])}")
            lines.append("")

    # --- Goals completed this session ---
    goals_done = checkpoint.get("goals_completed_this_session") or []
    if goals_done:
        lines.append(f"GOALS COMPLETED THIS SESSION: {len(goals_done)} — {', '.join(str(g) for g in goals_done[-10:])}")
        asp_last = checkpoint.get("aspiration_touched_last", "")
        if asp_last:
            lines.append(f"  Last aspiration: {asp_last}")
        lines.append("")

    # --- Encoding queue (expanded to 10 items) ---
    if all_encoding:
        lines.append(f"ENCODING QUEUE: {len(all_encoding)} items pending")
        for i, item in enumerate(all_encoding[:10]):
            if isinstance(item, dict):
                obs = str(item.get("observation", ""))[:150]
                target = item.get("target_article", "?")
                score = item.get("encoding_score", 0)
                lines.append(f"  {i+1}. [{score:.1f}] {obs} -> {target}")
            else:
                lines.append(f"  {i+1}. {str(item)[:150]}")
        if len(all_encoding) > 10:
            lines.append(f"  ... and {len(all_encoding) - 10} more")
        lines.append("")

    # --- State: micro-hypotheses, knowledge debt, blockers (ALL shown) ---
    counts = []
    if micro and isinstance(micro, list) and len(micro):
        counts.append(f"micro-hypotheses: {len(micro)}")
    if debt and isinstance(debt, list) and len(debt):
        counts.append(f"knowledge debt: {len(debt)}")
    if counts:
        lines.append(f"STATE: {', '.join(counts)}")
        lines.append("")

    # --- Blockers (ALL unresolved, not just first 3) ---
    if blockers:
        unresolved = [b for b in blockers if isinstance(b, dict) and not b.get("resolution")]
        if unresolved:
            lines.append(f"BLOCKERS: {len(unresolved)} unresolved")
            for b in unresolved:
                bid = b.get("blocker_id", "?")
                reason = str(b.get("reason", ""))[:120]
                affected = b.get("affected_skills") or b.get("affected_categories") or []
                lines.append(f"  {bid}: {reason}")
                if affected:
                    lines.append(f"    affects: {', '.join(str(a) for a in affected[:5])}")
            lines.append("")

    # --- Additional slots from full snapshot ---
    if all_slots:
        additional_lines = []
        for slot_name in SCALAR_SLOTS_FULL:
            if slot_name in all_slots and slot_name not in DEDICATED_SECTION_SLOTS:
                val = all_slots[slot_name]
                formatted = _format_slot_value(val)
                if formatted:
                    additional_lines.append(f"  {slot_name}: {formatted}")

        # Conclusions (high-value — judgment calls with evidence)
        conclusions = all_slots.get("conclusions") or []
        if conclusions and isinstance(conclusions, list) and len(conclusions):
            pending = [c for c in conclusions if isinstance(c, dict) and c.get("status") != "verified"]
            additional_lines.append(f"  conclusions: {len(conclusions)} total, {len(pending)} pending verification")

        # Sensory buffer
        sensory = all_slots.get("sensory_buffer") or []
        if sensory and isinstance(sensory, list) and len(sensory):
            additional_lines.append(f"  sensory_buffer: {len(sensory)} observations pending encoding")

        # Episode chain
        episode = all_slots.get("episode_chain")
        if episode and isinstance(episode, dict):
            goal_id = episode.get("goal_id", "?")
            episodes = episode.get("episodes") or []
            additional_lines.append(f"  episode_chain: {goal_id}, {len(episodes)} episodes")
            if episodes:
                last = episodes[-1] if isinstance(episodes[-1], dict) else {}
                approach = str(last.get("approach", ""))[:100]
                outcome = str(last.get("outcome", ""))[:100]
                if approach:
                    additional_lines.append(f"    last approach: {approach}")
                if outcome:
                    additional_lines.append(f"    last outcome: {outcome}")

        # Domain data
        domain = all_slots.get("domain_data")
        if domain and isinstance(domain, dict):
            cat = domain.get("category", "?")
            additional_lines.append(f"  domain_data: loaded for {cat}")

        # Recent violations
        violations = all_slots.get("recent_violations") or []
        if violations and isinstance(violations, list) and len(violations):
            additional_lines.append(f"  recent_violations: {len(violations)} tracked")

        if additional_lines:
            lines.append("ADDITIONAL STATE:")
            lines.extend(additional_lines)
            lines.append("")

    # --- Retrieval manifest ---
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
        node_str = ", ".join(str(n) for n in nodes[:8])
        if len(nodes) > 8:
            node_str += f" +{len(nodes) - 8} more"
        lines.append(f"  Nodes: {node_str}")
        lines.append(f"  Deliberation: {len(active_items)} active, {len(skipped_items)} skipped")
        if pending:
            lines.append("  *** UTILIZATION FEEDBACK PENDING — Phase 4.26 did not complete before compaction ***")
            for item in active_items[:10]:
                if isinstance(item, dict):
                    lines.append(f"    ACTIVE: {item.get('id', '?')} ({item.get('type', '?')})")
        lines.append("")

    # --- Execution diary (last 10 entries) ---
    diary_entries = _read_diary_entries(limit=10)
    if diary_entries:
        lines.append(f"EXECUTION DIARY (last {len(diary_entries)} entries):")
        for entry in diary_entries:
            ts = str(entry.get("timestamp", ""))
            # Extract just HH:MM from ISO timestamp
            time_part = ts[11:16] if len(ts) >= 16 else ts
            goal = entry.get("goal_id", "")
            etype = entry.get("entry_type", "")
            content = str(entry.get("content", ""))[:200]
            lines.append(f"  [{time_part}] {goal} {etype}: {content}")
        lines.append("")

    # --- Reasoning snapshot ---
    snapshot = _read_reasoning_snapshot()
    if snapshot and isinstance(snapshot, dict):
        lines.append("REASONING SNAPSHOT (pre-compaction synthesis):")
        current = snapshot.get("current_reasoning") or {}
        if current:
            if current.get("goal"):
                lines.append(f"  Goal: {current['goal']}")
            if current.get("approach"):
                lines.append(f"  Approach: {_truncate(str(current['approach']), 200)}")
            tried = current.get("tried_and_failed") or []
            for t in tried[:5]:
                lines.append(f"  TRIED & FAILED: {_truncate(str(t), 150)}")
            if current.get("current_theory"):
                lines.append(f"  Theory: {_truncate(str(current['current_theory']), 200)}")
            if current.get("next_step"):
                lines.append(f"  Next step: {_truncate(str(current['next_step']), 200)}")
        decisions = snapshot.get("key_decisions_this_session") or []
        if decisions:
            lines.append("  Key decisions:")
            for d in decisions[:5]:
                lines.append(f"    - {_truncate(str(d), 150)}")
        patterns = snapshot.get("emerging_patterns") or []
        if patterns:
            lines.append("  Emerging patterns:")
            for p in patterns[:5]:
                lines.append(f"    - {_truncate(str(p), 150)}")
        lines.append("")

    # --- Pending background agents ---
    pending_count = checkpoint.get("pending_agents_count", 0)
    if pending_count:
        lines.append(f"PENDING AGENTS: {pending_count} background agent(s) were running before compaction.")
        lines.append("  Their completion notifications will re-engage you. Collect results in Phase -0.5a.")
        lines.append("")

    # --- Blocked-sleep state ---
    blocked_sleep = checkpoint.get("blocked_sleep_until")
    if blocked_sleep:
        lines.append(f"BLOCKED-SLEEP ACTIVE: Agent was sleeping until {blocked_sleep}.")
        lines.append("  Phase -0.5e will resume or expire this — do NOT re-run B1-B7.")
        lines.append("")

    lines.append("IDENTITY: Phase -0.5d will re-read self.md and program.md — identity context lost during compaction.")
    lines.append("")
    lines.append("ACTION: The stop hook will fire next. Re-enter /aspirations loop.")
    lines.append("Phase -0.5c will detect compact-checkpoint.yaml, run compact-restore-slots.sh to")
    lines.append("restore all WM slots, and process encoding queue in this fresh context.")
    lines.append("")
    lines.append("MANDATORY: Phase 2 requires `goal-selector.sh` — do NOT assume goal availability from memory.")
    lines.append("===========================================")

    # --- stderr summary ---
    parts = []
    if all_encoding:
        parts.append(f"{len(all_encoding)} encoding")
    total_slots = len(all_slots)
    non_null = sum(1 for v in all_slots.values() if v is not None and v != [] and v != {})
    if non_null:
        parts.append(f"{non_null}/{total_slots} slots")
    if diary_entries:
        parts.append(f"{len(diary_entries)} diary")
    if snapshot:
        parts.append("snapshot")
    if manifest and isinstance(manifest, dict):
        parts.append("retrieval-manifest")
    summary = ", ".join(parts) if parts else "minimal state"
    log(f"restored checkpoint #{compact_count}: {summary}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
