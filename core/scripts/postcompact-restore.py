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

from _paths import AGENT_DIR, WORLD_DIR, assert_agent_dir, body_state_path

# : fail loud at import time if MIND_AGENT unset; replaces the
# opaque `None / "session"` TypeError class the next line would otherwise raise.
assert_agent_dir("postcompact-restore")

# Body-keyed for symmetry with the writers (). This is a NO-OP today:
# postcompact-restore.sh carries a runner-identity guard (SID must equal
# running-session-id), so this process only ever runs as the reducer, which is
# never bodied and always takes the agent-wide fallback. Routed through the same
# resolver anyway so the reader cannot silently diverge from the writers if that
# guard is ever relaxed.
CHECKPOINT_PATH = body_state_path(AGENT_DIR.name, "compact-checkpoint.yaml")
DIARY_PATH = AGENT_DIR / "session" / "execution-diary.jsonl"
SNAPSHOT_PATH = AGENT_DIR / "session" / "reasoning-snapshot.yaml"
# iteration-checkpoint.json is the skill-level breadcrumb written by
# aspirations-select Phase 2.95. Small (~80B JSON) and durable across
# autocompact — if present, it names the goal the loop picked pre-compact.
# Surfaced prominently so the model resumes the correct goal instead of
# reconstructing a different one from the compact summary (bug traced
# 2026-04-22 alpha session-56).
ITERATION_CKPT_PATH = body_state_path(AGENT_DIR.name, "iteration-checkpoint.json")

# Slots to skip in the "additional slots" section (already shown in dedicated sections)
#  / zeta allowlist audit 8d: DEDICATED_SECTION_SLOTS + SCALAR_SLOTS_FULL
# are a curated DISPLAY-FORMATTING subset of WM state, NOT a mirror of wm.py
# DEFAULT_SLOT_TYPES. They intentionally include blocked_sleep_until -- a
# compact-checkpoint field that is NOT a wm slot -- so a wm-slot parity test would
# false-fail. A new wm slot simply falls through to the generic "additional slots"
# display (graceful, not broken). => document, no test.
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


def _read_iteration_checkpoint():
    """Read the in-flight goal anchor from aspirations-select Phase 2.95.

    Returns dict or None. Robust to missing file and parse errors —
    postcompact-restore must never crash on a broken checkpoint.
    """
    if not ITERATION_CKPT_PATH.exists():
        return None
    try:
        return json.loads(ITERATION_CKPT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"iteration-checkpoint read failed: {e}")
        return None


# Statuses that make an anchored goal impossible to "resume". SSOT is
# coordination_merge._TERMINAL_STATUSES; mirrored here rather than imported
# because this module runs inside a SessionStart hook, where an import failure
# would take out the ENTIRE context restore, not just this check. Pinned equal
# by test_postcompact_restore_terminal_anchor.py.
_TERMINAL_STATUSES = ("completed", "skipped", "expired")


def _goal_live_status(goal_id, source):
    """Resolve an anchored goal's LIVE status. Returns a dict; NEVER raises.

    Keys: `status` (str|None), `checked` (bool), `ambiguous` (bool), `note`,
    `defer_reason` (str|None).

    `checked` is the load-bearing field, and it is why this returns a dict
    rather than a bare status string. "I read the queue and the goal is live"
    and "I could not read the queue at all" are both a falsy status, and they
    license opposite wordings — the first says resume, the second must admit
    it does not know. Reporting an unreadable queue as a passed check is the
    guard-1760 class (a completeness tool must never report what it declined
    to look at as coverage), so the caller degrades to the original imperative
    ONLY on checked=False and says so.

    `ambiguous` exists because a goal id is NOT unique across queues, which is
    exactly the condition behind this function (g-115-5029). Measured
    2026-08-05: `g-001-01` names "Identify learning domain" in the WORLD queue
    (skipped 2026-07-22, under the RETIRED asp-001 "Explore and Learn") AND
    "Reflect and journal" in EVERY agent queue (pending, under the active
    asp-001 "Maintain Agent Health"). The claim-side anchor in
    aspirations-claim.sh hardcodes source="world" (correctly — claims only ever
    resolve against the world queue), so a checkpoint's `source` cannot be used
    to prove which goal was meant when the id lives in both. When it does, say
    so instead of silently picking one: a reader told "terminal" about the
    wrong copy is worse off than one told the id is ambiguous.

    Reads the JSONL directly rather than via a wrapper: hooks run outside
    bash-agent-inject's PATH shim and outside the daemon's env (guard-1097),
    so shelling out is the fragile path here, not the robust one.
    """
    result = {"status": None, "checked": False, "ambiguous": False, "note": "",
              "defer_reason": None}
    if not goal_id or goal_id == "?":
        result["note"] = "no goal_id on the checkpoint"
        return result

    def _goal_in(path):
        """The goal dict for goal_id in one aspirations.jsonl.

        {} = queue read cleanly, id absent. None = unreadable (distinct, and the
        distinction is the whole reason `checked` exists).
        """
        try:
            if path is None or not path.exists():
                return None
            for line in path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    asp = json.loads(line)
                except Exception:
                    continue
                for goal in (asp.get("goals") or []):
                    if goal.get("id") == goal_id:
                        return goal
            return {}          # queue read cleanly, id absent
        except Exception:
            return None        # unreadable — distinct from absent

    def _status_of(goal):
        if goal is None:
            return None
        return (goal.get("status") or "").strip().lower()

    world_path = (WORLD_DIR / "aspirations.jsonl") if WORLD_DIR else None
    agent_path = AGENT_DIR / "aspirations.jsonl"
    world_goal = _goal_in(world_path)
    agent_goal = _goal_in(agent_path)
    world_status = _status_of(world_goal)
    agent_status = _status_of(agent_goal)

    source = (source or "world").strip().lower()
    primary = world_status if source == "world" else agent_status
    primary_goal = world_goal if source == "world" else agent_goal
    if primary is None:
        result["note"] = f"{source} queue unreadable — status not checked"
        return result

    result["checked"] = True
    result["status"] = primary or None
    # : the field that distinguishes "in flight" from "deliberately
    # parked" WITHOUT the status changing. A deferred goal stays `pending`, so a
    # status-only check reports it resumable — and that is the path that produced
    # this defect's first incident (defer on cc-04, ). The second
    # incident's release-then-skip half was terminal and already caught.
    if primary_goal:
        result["defer_reason"] = (primary_goal.get("defer_reason") or "").strip() or None
    if world_status and agent_status:
        result["ambiguous"] = True
        result["note"] = (f"id exists in BOTH queues "
                          f"(world={world_status}, agent={agent_status})")
    elif not primary:
        result["note"] = f"id not found in the {source} queue"
    return result


def _format_iteration_ckpt_block(iter_ckpt):
    """Format the in-flight goal block for the restore output."""
    goal_id = iter_ckpt.get("goal_id", "?")
    phase = iter_ckpt.get("phase", "?")
    selected_at = iter_ckpt.get("selected_at", "?")
    aspiration_id = iter_ckpt.get("aspiration_id", "?")
    score = iter_ckpt.get("selector_score", "?")
    skill = iter_ckpt.get("skill", "")
    cross_owner = iter_ckpt.get("cross_agent_owner", "") or ""
    out = [
        "═══ IN-FLIGHT GOAL (autocompact boundary) ═══",
        f"goal_id:       {goal_id}",
        f"aspiration:    {aspiration_id}",
        f"phase:         {phase}",
        f"selected_at:   {selected_at}",
        f"selector_score: {score}",
    ]
    if skill:
        out.append(f"skill:         {skill}")
    if cross_owner:
        out.append(f"cross_agent_owner: {cross_owner}")
    # Cross-check the anchor against the goal's LIVE status before emitting an
    # imperative about it (). The block is still printed in FULL on
    # every branch — this SURFACES a stale anchor, it never swallows one. A
    # read-side check that quietly dropped the block would leave the writer
    # free to keep producing stale anchors with nothing left to notice them.
    live = _goal_live_status(goal_id, iter_ckpt.get("source"))
    is_terminal = live["checked"] and live["status"] in _TERMINAL_STATUSES

    # : NOT-IN-FLIGHT WITHOUT BEING TERMINAL. The terminal branch below
    # () covers completed/skipped/expired, which caught zeta's
    # release-then-SKIP but missed both of the paths this goal was filed about:
    # a DEFER leaves status `pending` with a defer_reason, and a bare RELEASE
    # leaves status `pending` with claimed_by cleared. A status-only check calls
    # both resumable and emits the full CRITICAL imperative on them.
    #
    # Requires `checked` — an unreadable queue must never manufacture this — and
    # `not ambiguous`, because when the id lives in both queues the fields read
    # here may belong to the other copy, and a confident "not in flight" about
    # the wrong goal is worse than the ambiguity note the reader already gets.
    #
    # FAIL-SAFE DIRECTION: claimed_by is written BY the claim that writes this
    # very checkpoint, so `checked and claimed_by is None` means the claim was
    # given back. The risk of wrongly suppressing a live resume is bounded by
    # that ordering; the risk of NOT suppressing is a confident instruction to
    # redo work that was deliberately routed away.
    #
    # SCOPED TO defer_reason ON PURPOSE. The obvious sibling predicate — "status
    # is live but claimed_by is empty, so the claim was released" — was written,
    # measured against this module's own axis-2 contract test, and REMOVED. It
    # fires on every pending/in-progress/blocked goal whose record simply has no
    # claim field, which is not a stale anchor at all: a worker hands its goal to
    # the reducer at in-progress with the claim released (worker-loop Phase 4),
    # and stranded-claim-sweep strips claim fields by design. An absent field is
    # not evidence of an event. defer_reason IS positive evidence, and the
    # released case does not need a read-side predicate anyway — release is a
    # chokepoint and now clears the anchor itself.
    stale_reason = None
    if (live["checked"] and not is_terminal and not live["ambiguous"]
            and live["defer_reason"]):
        stale_reason = (f"it is DEFERRED (status '{live['status']}', "
                        f"defer_reason: {live['defer_reason']})")

    if is_terminal:
        # The one branch that must NOT tell the model to resume. An in-flight
        # assertion about a closed goal is self-falsifying, and the previous
        # wording forbade the two actions that would have caught it (re-running
        # the selector, and reasoning from context) — so obeying it was the
        # only path left open. guard-2666 is the behavioral rule; this is it
        # enforced at the layer that already has the goal id in hand.
        out.extend([
            "",
            f"STALE ANCHOR — DO NOT RESUME. This checkpoint names {goal_id}, "
            f"whose live status is '{live['status']}'.",
            "A terminal goal cannot be in flight, so this anchor is left over: "
            "no defer, skip, or release path clears iteration-checkpoint.json "
            "(guard-2666), and loop-state-save performs no status validation at "
            "write time.",
            "ACTION: ignore the goal named above, and re-run "
            "/aspirations precheck + select to pick fresh work. Do NOT execute "
            f"{goal_id} and do NOT write an outcome_note onto that record.",
        ])
    elif stale_reason:
        # Deliberately worded differently from the terminal branch. That goal is
        # CLOSED and must not be touched; this one is live work that simply is
        # not in flight here — it may be selected again on its merits, and
        # telling you never to execute it would be its own wrong instruction.
        out.extend([
            "",
            f"STALE ANCHOR — DO NOT RESUME FROM IT. This checkpoint names "
            f"{goal_id}, but {stale_reason}.",
            "No exit path cleared the anchor at the time this one was written "
            "(g-115-4990); aspirations-release.sh now clears it via "
            "`loop-state-save.sh clear --if-goal`, so an anchor still standing "
            "here came from a path that does not route through release — the "
            "skip path is the known one.",
            "ACTION: re-run /aspirations precheck + select to pick fresh work. "
            f"Do NOT resume {goal_id} from this anchor. If the selector offers "
            "it again on its own merits, that is a normal selection and fine — "
            "what is not fine is treating this anchor as evidence it was "
            "in flight.",
        ])
    else:
        out.extend([
            "",
            f"CRITICAL: Your in-flight goal is {goal_id} at phase '{phase}'.",
            "Resume execution on THIS goal. Do NOT re-run goal-selector.sh to",
            "pick a different one. Do NOT substitute a different goal based on",
            "narrative context from the compact summary. If this checkpoint",
            "looks wrong, /aspirations precheck + select will surface the mismatch.",
        ])
        if not live["checked"]:
            # Say that the check did not run rather than letting the imperative
            # above imply it passed (guard-1760 — never report what you declined
            # to look at as coverage).
            out.append(
                f"NOTE: the live-status cross-check did NOT run ({live['note']}), "
                f"so the anchor above is UNVERIFIED — apply guard-2666 by hand "
                f"and read {goal_id}'s record before resuming."
            )

    if live["ambiguous"]:
        # Emitted on BOTH branches: an ambiguous id makes the terminal verdict
        # and the resume imperative equally unsafe to act on unexamined.
        out.append(
            f"AMBIGUOUS ID: {live['note']}. A goal id is not unique across "
            f"queues, and the claim-side anchor hardcodes source=world, so the "
            f"checkpoint cannot say which copy was meant. Read BOTH records "
            f"before acting."
        )
    if cross_owner:
        out.append(
            f"CROSS-AGENT: Goal pulled from sibling '{cross_owner}'. Phase 4 must "
            f"prefix MIND_AGENT={cross_owner} on owner-state writes "
            f"(aspirations-update-goal/release/complete-by, iteration-close, "
            f"recurring-close). See aspirations-execute Phase 4 Setup."
        )
    out.extend([
        "═══════════════════════════════════════════════",
        "",
    ])
    return out


def main():
    # Read iteration-checkpoint FIRST — surface in-flight goal anchor even
    # when compact-checkpoint.yaml is missing (PreCompact hook failed, or
    # first-iteration session). Separate code path from the full restore.
    iter_ckpt = _read_iteration_checkpoint()

    if not CHECKPOINT_PATH.exists():
        log("no compact-checkpoint.yaml -- degraded restore")
        if iter_ckpt is not None:
            # Emit a minimal restore with just the iteration anchor so the
            # model still knows which goal to resume.
            minimal = [
                "=== CONTEXT RESTORED (post-compaction, degraded) ===",
                "compact-checkpoint.yaml missing — only iteration anchor available.",
                "",
            ]
            minimal.extend(_format_iteration_ckpt_block(iter_ckpt))
            minimal.append("ACTION: Re-enter /aspirations loop. Phase -0.5c will skip")
            minimal.append("(no checkpoint). Phase 0 precheck + Phase 2 select still run.")
            minimal.append("===========================================")
            print("\n".join(minimal))
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

    # --- In-flight goal anchor (from aspirations-select Phase 2.95) ---
    # Highest priority — printed FIRST so the model sees the pre-compact
    # goal selection before any narrative context that could drift it.
    # iter_ckpt was already read at top of main() for degraded-path handling.
    if iter_ckpt is not None:
        lines.extend(_format_iteration_ckpt_block(iter_ckpt))

    # --- Active context (NO truncation — full summary) ---
    if active:
        summary = active.get("summary", "")
        if summary:
            lines.append(f"LAST CONTEXT: {summary}")
            lines.append("")

    # --- Loop state (critical for loop continuity) ---
    loop_state = all_slots.get("loop_state")
    if loop_state and isinstance(loop_state, dict):
        signals = loop_state.get("signals") or {}
        parts = []
        # goals_completed / productive_goals / evolutions live at the top level
        # of loop_state; the three streak/drift counters are nested under
        # loop_state["signals"]. Reading the nested keys at top-level returned
        # None and silently dropped the boredom/streak signal from the
        # post-autocompact display ( / zeta-1385).
        for key in ["goals_completed", "productive_goals", "evolutions"]:
            val = loop_state.get(key)
            if val is not None and val != 0:
                parts.append(f"{key}={val}")
        for key in ["routine_streak_global", "goals_since_last_tree_update",
                     "consecutive_blocked_sleeps"]:
            val = signals.get(key)
            if val is not None and val != 0:
                parts.append(f"{key}={val}")
        if parts:
            lines.append(f"LOOP STATE: {', '.join(parts)}")
            # Show touched aspirations if present. The canonical loop_state key
            # is "touched" (written by the bash loop-state gates); the prior
            # "touched_aspirations" lookup never matched and silently omitted
            # the line (same display-vs-shape defect class, ).
            touched = loop_state.get("touched")
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
