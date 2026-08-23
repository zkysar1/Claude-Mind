#!/usr/bin/env python3
"""Aspiration lifecycle engine for JSONL-based aspiration management.

All shell scripts are thin wrappers around this. Subcommands managed via argparse.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

# : force utf-8 on stdin/stdout/stderr (covers Windows cp1252 fallback
# when callers bypass the _platform.sh PYTHONIOENCODING=utf-8 shim).
from _stdio import reconfigure_stdio  # noqa: E402
reconfigure_stdio()

from _paths import WORLD_DIR, AGENT_DIR, META_DIR, CORE_ROOT, CONFIG_DIR
from _cadence_anchor import is_deliberate_raise as _is_deliberate_raise
from _gate_log import log as _gate_log
from _goal_census import effective_counts as _effective_counts  # B9-deep census-augmented counts
from _goal_census import all_evicted_ids as _all_evicted_ids  #  mint-site tombstone awareness

# Default paths point to world/ (collective task queue).
# Overridden to agent/ at runtime when --source agent is passed.
LIVE_PATH = WORLD_DIR / "aspirations.jsonl"
ARCHIVE_PATH = WORLD_DIR / "aspirations-archive.jsonl"
META_PATH = WORLD_DIR / "aspirations-meta.json"
EVOLUTION_PATH = META_DIR / "evolution-log.jsonl"


VALID_ASP_STATUSES = {"active", "paused", "completed", "retired"}
VALID_GOAL_STATUSES = {"pending", "in-progress", "completed", "blocked", "skipped", "expired", "decomposed", "superseded"}

# Terminal goal statuses — goals in these states are considered resolved for archival purposes.
# `superseded` means the goal was mooted by aspiration-level intent satisfaction (see cmd_complete --intent-satisfied).
# ⚠ Single source of truth. Consumed by find_unfinished_goals, cmd_update_goal (blocker
# clearing), cmd_archive_sweep, _validate_intent_satisfaction, and downstream scoring.
# Adding/removing a member changes archival eligibility across the engine.
TERMINAL_GOAL_STATUSES = {"completed", "skipped", "expired", "decomposed", "superseded"}

# Inner-refinement (Self-Refine,  / BRD Gap 4) bounded-iteration cap.
# max_iters MUST be <= this; the cap is the structural termination guarantee
# (an inner_refinement loop can never exceed INNER_REFINEMENT_MAX_ITERS_CAP passes).
INNER_REFINEMENT_MAX_ITERS_CAP = 5

def _normalize_terminal_goal(goal):
    """Clear deferral/blocker state when a goal's status is terminal.

    Invariant: a goal in a terminal status cannot also carry active deferral
    state. Active state was meaningful while the goal was being worked; once
    work is done (or abandoned), defer fields are anomalies that distort
    goal-selector scoring and `aspirations-read --blocked` consumers
    (/encode-session Lane 3, etc.).

    Mirrors the field-clearing pattern at cmd_update_goal's defer_reason-
    cleared branch (~line 2182): set defer_reason and defer_reason_set_at to
    None (preserve schema key presence); pop the optional companions.

    g-115-661 Layer 2: ALSO backfills `completed_at` when missing on terminal
    goals. The 533-goal completed_at=null gap (g-115-660 investigation)
    surfaced that no writer ever stamped goal.completed_at — Layer 1 writes
    at the three transition sites (cmd_update_goal, cmd_recover_recurring
    Case 3, cmd_complete_by), and this normalizer is the safety net for any
    legacy goals or paths that bypass those sites. Prefers completed_date
    (parsed) when available — preserves day-level history rather than
    overwriting with current time.

    Preserves `blocked_by` (dependency lineage). Idempotent.
    """
    if goal.get("status") not in TERMINAL_GOAL_STATUSES:
        return
    if goal.get("defer_reason") is not None:
        goal["defer_reason"] = None
    if goal.get("defer_reason_set_at") is not None:
        goal["defer_reason_set_at"] = None
    goal.pop("deferred_until", None)
    goal.pop("blocker_ref", None)
    goal.pop("blocked_since", None)

    #  Layer 2: backfill completed_at when missing. Prefers
    # completed_date (parsed) over now() to preserve history for legacy
    # goals that closed before Layer 1 landed.
    if goal.get("completed_at") is None:
        cd = goal.get("completed_date")
        if isinstance(cd, str) and cd:
            # If completed_date is already a full ISO datetime, use as-is.
            # Otherwise (date-only YYYY-MM-DD), append T00:00:00 to make
            # it ISO-comparable with completed_at consumers.
            if "T" in cd:
                goal["completed_at"] = cd
            else:
                goal["completed_at"] = f"{cd}T00:00:00"
        else:
            goal["completed_at"] = datetime.now().isoformat(timespec="seconds")

def _normalize_terminal_goals_in(asp_or_list):
    """Walk aspiration(s) and normalize each terminal-status goal.

    Accepts a single aspiration dict OR a list of aspiration dicts. Called
    from every disk-write boundary (_write_live_under_lock + the three archive
    write sites in cmd_complete/cmd_retire/cmd_archive_sweep) so the invariant
    is enforced at the disk-write boundary rather than per-setter. Idempotent.
    """
    if isinstance(asp_or_list, dict):
        asps = [asp_or_list]
    else:
        asps = asp_or_list
    for asp in asps:
        for g in asp.get("goals", []) or []:
            _normalize_terminal_goal(g)

VALID_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}

# `user_leg_scope` names what the user would have to approve on a goal whose
# participants include `user`. Closed vocabulary — the selector and guard-349
# compare against this string (not prose) to decide whether a standing grant
# in world/conventions/capability-routing.md covers the goal.
# Extend ONLY when a new standing-grant scope is added to that file; the two
# vocabularies must stay in lockstep.
VALID_USER_LEG_SCOPES = {
    "commit", "push", "deployment-approval",
    "architecture-decision", "credential-grant",
    "data-provision", "new-resource",
}

# `intended_agent` is the optional routing hint produced by
# core/scripts/capability-route-gate.py () suggesting which agent
# should ideally take the goal. Distinct from `participants` which controls
# ELIGIBILITY — intended_agent is a PREFERENCE the goal-selector can use to
# bias scoring without restricting access. Vocabulary is derived dynamically
# from world/team-state.yaml agent_status keys (via _agents.get_active_agents)
# plus the special "either" sentinel — a fresh deployment with one agent named
# "teacher" gets {"teacher", "either"} automatically; no edit needed here.
# When uncertain, leave null (current fall-through behavior).
from _agents import get_active_agents as _get_active_agents  # noqa: E402

def _valid_intended_agents() -> set:
    return set(_get_active_agents()) | {"either"}


def routes_away_from(intended_agent, agent_name) -> bool:
    """True when `intended_agent` routes a goal AWAY from `agent_name`.

    Returns False for the four non-routing cases: unset/None, the "either"
    sentinel, `agent_name` itself, and -- the g-115-3482 fix -- any value
    OUTSIDE the live vocabulary (`_valid_intended_agents()`).

    That last case is the bug this helper exists to kill. An off-roster value
    names nobody who can ever honor the routing: a RETIRED agent ("delta",
    removed from team-state agent_status), or an unrecognized sentinel (the
    cycle-detector writes "any", which obviously MEANS "anyone" and which the
    vocabulary does not contain). Treating such a value as foreign made the
    goal doubly dead -- UNSELECTABLE (goal-selector's collect_candidates drops
    it, while collect_blocked never references intended_agent, so it is absent
    from BOTH outputs -- invisible in both directions) and UNCLAIMABLE (the
    takeover guard and the daemon claim path both refuse it). Measured
    2026-07-28: g-115-913 and g-115-918 sat invisible for 71 days that way.

    Falling through is the SAFE direction, not a loosening: the goal becomes
    visible to everyone, which is exactly what "either" already does and
    exactly what whoever wrote "any" intended.

    Conservative (rb-1028, never-on-absent-evidence): when the vocabulary
    cannot be resolved -- an empty/unreadable roster leaves
    `_valid_intended_agents()` == {"either"} alone -- the roster check is
    SKIPPED and the historical name-mismatch behavior stands. An unreadable
    team-state can therefore never make every routed goal visible fleet-wide,
    which would be a fail-OPEN across the whole fleet.
    """
    # str() before strip(): the predicate this replaced compared with != and so
    # tolerated ANY type. A bare `.strip()` would raise AttributeError on a
    # malformed non-string value, and this runs inside goal-selector's
    # per-goal loop -- an exception there crashes selection, which kills the
    # autonomous loop. No non-string value exists in the live corpus (4019
    # checked, 0 bad), so this is purely about not REGRESSING the old code's
    # type-tolerance. A stringified oddity lands outside the vocabulary and
    # falls through to visible, which is the safe direction.
    ia = str(intended_agent or "").strip()
    if not ia or ia == "either" or ia == agent_name:
        return False
    # NEVER let a roster-resolution failure escape: this runs inside
    # goal-selector's per-goal loop, so an exception here crashes selection and
    # kills the autonomous loop. The predicate this replaced was a pure string
    # comparison and could not raise at all, so the guard is about not
    # REGRESSING that property. The path is real though narrow: _agents
    # ._resolve_world_team_state calls `_agents_root(root).iterdir()` OUTSIDE
    # _from_team_state's try block, so an unreadable agents-root (permission,
    # transient network/own-cloud mount) raises OSError straight through.
    # goal-selector's own _load_team_state_cached docstring already states the
    # invariant this honors: team-state is ADVISORY input and "a partial/
    # unreadable read must NOT crash the whole selector" (rb-2429).
    # Unresolvable roster => same conservative branch as an empty one: skip the
    # vocabulary check and keep the historical name-mismatch behavior.
    try:
        valid = _valid_intended_agents()
    except Exception:
        return True
    if len(valid) > 1 and ia not in valid:
        return False  # off-roster -> nobody can honor it -> treat as "either"
    return True

# Resolved-once snapshot for callers that import as a constant. Refresh
# semantics match capability_route.ACTIVE_AGENTS — module reload required
# if the agent set changes (daemon recycles on post-commit anyway).
VALID_INTENDED_AGENTS = _valid_intended_agents()

# `goal_source` (): inference + apply_default live in _goal_source so
# the daemon (aspirations_write.py) and this fallback path call ONE
# implementation. Convention: core/config/conventions/goal-schemas.md.
from _goal_source import VALID_GOAL_SOURCES, apply_default as _apply_goal_source_default  # noqa: E402

# Re-exported from gates.defer_classifier — that module is the daemon-safe
# single source of truth for the prefix list and the narrative-classifier
# predicate. Local re-export preserves existing import sites
# (`from aspirations import STRUCTURED_DEFER_PREFIXES`) without forcing
# them to switch. When adding a new structured prefix, edit the gates
# module — and also update aspirations-precheck/SKILL.md Phase 0.5b.4 so
# the re-probe sweep skips it too.
# Restored 2026-05-15 (resolves ; Maintain record ):
# 25d6520 over-deleted this re-export during CLI-subcommand cleanup,
# ImportError-crashing goal-selector.py:77 and hard-breaking Phase 2
# selection loop-wide (same over-deletion class as 54529fb tree.py-read
# / 92d9265 pure-CLI wrappers).
from gates.defer_classifier import STRUCTURED_DEFER_PREFIXES  # noqa: E402,F401
# BLOCKER_REF_TYPES is interpolated into the structured-defer refusal
# messages below (cmd_update_goal, ~L1583/L1635). It was referenced without
# an import — the refusal branch crashed with NameError instead of printing
# the educational message + filing the atomic Unblock (refuse-without-queue,
# surfaced by test_defer_to_unblock_integration.py cases 1/5/7; found during
# ). gates.blocker_ref is the single source of truth, same import
# create-blocker.py uses.
from gates.blocker_ref import BLOCKER_REF_TYPES  # noqa: E402
from gates.field_shrink import evaluate as _field_shrink_eval  # noqa: E402


def _warn_missing_user_leg_scope(goal_id, participants, user_leg_scope):
    """Thin wrapper over gates.user_leg_scope.evaluate() — preserved as a
    helper here so existing call sites stay unchanged. Module is the single
    source of truth for the advisory text and the trigger logic.
    """
    from gates.user_leg_scope import evaluate
    result = evaluate(
        goal_id=goal_id,
        participants=participants,
        user_leg_scope=user_leg_scope,
        valid_scopes=VALID_USER_LEG_SCOPES,
    )
    if result["warned"]:
        print(result["message"], file=sys.stderr)

def _emit_description_length_warning(goal, source):
    """Thin wrapper over gates.description_length.evaluate() — preserved
    here so existing call sites stay unchanged. Module is the single source
    of truth for the threshold (80 chars), the warning text, the telemetry
    record shape, and the recurring-goal exemption.
    """
    from gates.description_length import evaluate
    result = evaluate(goal, source=source, meta_dir=META_DIR)
    if result["warned"]:
        print(result["message"], file=sys.stderr)
        if not result["telemetry_written"]:
            # Telemetry append failure is non-fatal — warning still emitted.
            # Mirror the legacy stderr WARN so observers see the same line.
            print(
                "[add-goal] WARN: description-length telemetry append failed "
                "(non-fatal; warning still emitted)",
                file=sys.stderr,
            )

VALID_SCOPES = {"sprint", "project", "initiative"}
ASP_ID_RE = re.compile(r"^asp-(\d{3}|xw-\d{8}T\d{6})$")  # asp-xw-<ts> cross-world ids (companion to GOAL_ID_RE xw branch below)
GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")  # 4-digit:  hit  (2026-05-19); g-xw-<ts>-NN cross-world ids ( made them selector-visible but the update/close path still rejected them -> stuck at 0/1 forever)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Single source of truth for the `add` (aspiration) and `add-goal` schemas —

# ---------------------------------------------------------------------------
# Helpers: file I/O
# ---------------------------------------------------------------------------
# Lock ordering: LIVE_PATH.lock first, ARCHIVE_PATH.lock second.
# Never reverse this or commands that touch both files will deadlock.
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read a JSONL file and return a list of dicts. Returns [] if missing/empty.

    Skips corrupt lines (logged to stderr) so a single partial-write or
    interleaved multi-writer append does not crash all 20+ callers in this
    module. aspirations.jsonl is concurrently appended by alpha + bravo;
    truncation under load is a real failure mode, not theoretical.

    g-001-267 follow-up B: under severe corruption (zero parsed records
    despite non-empty file, or every line failed to parse), restores the
    most recent .history/ snapshot in place. Closes the read-side gap left
    by the OneDrive in-place-rewrite fallback in _write_live_under_lock —
    a process kill mid-fallback produced an unrecoverable file before this
    change. Delegates to _fileops.read_jsonl_with_recovery.
    """
    from _fileops import read_jsonl_with_recovery
    return read_jsonl_with_recovery(path)

def write_jsonl(path, items):
    """Atomically write a list of dicts as JSONL with locking and history."""
    from _fileops import locked_write_jsonl
    locked_write_jsonl(path, items)

def append_jsonl(path, item):
    """Append one JSON line to a JSONL file with locking and history."""
    from _fileops import locked_append_jsonl
    locked_append_jsonl(path, item)

def _write_live_under_lock(items, action_desc, agent_name=None):
    """Write items to LIVE_PATH when the caller already holds its lock.

    Performs: normalize → save_history → atomic write with OneDrive-fallback → changelog.
    MUST only be called while holding LIVE_PATH.with_suffix('.lock').

    Normalization step (added 2026-05-12): clears defer/blocker state on any
    terminal-status goal so anomalies cannot survive a write. See
    _normalize_terminal_goal for the invariant. The disk-write boundary is the
    single point of enforcement — per-setter wiring is not required.

    g-001-267 (2026-05-07): OneDrive Files-On-Demand reparse points block
    os.replace with PermissionError [WinError 5] even when the file is
    pinned. Retry+fallback policy now lives in
    _fileops._atomic_write_with_fallback — single source of truth across
    every locked_write_* path. See its docstring for the full rationale.
    """
    _normalize_terminal_goals_in(items)
    from _fileops import (save_history, append_changelog, resolve_base_dir,
                          _atomic_write_with_fallback)
    base_dir = resolve_base_dir(LIVE_PATH)
    agent = agent_name or (AGENT_DIR.name if AGENT_DIR else "unknown")
    if base_dir:
        save_history(LIVE_PATH, base_dir, agent, action_desc)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")
    _atomic_write_with_fallback(
        LIVE_PATH, _write, fallback_counter_key="aspirations_live")

    if base_dir:
        append_changelog(base_dir, agent, LIVE_PATH, "update", action_desc)

def _check_not_archived(asp_id, *, action="modify"):
    """Refuse if asp_id exists in the archive. Call under lock.

    For 'add': always refuse (never reuse an archived ID).
    For 'modify': refuse only if the ID is EXCLUSIVELY in the archive.
    If the ID exists in both archive and live file (ID collision between
    two different aspirations), allow modification of the live copy.
    """
    archived = read_jsonl(ARCHIVE_PATH)
    if any(a.get("id") == asp_id for a in archived):
        if action == "add":
            print(f"REFUSED: {asp_id} already exists in archive — pick a higher ID.",
                  file=sys.stderr)
            sys.exit(1)
        else:
            # For modifications: only refuse if the aspiration is NOT also in the live file.
            # If it IS in both, that's an ID collision — allow modifying the live copy.
            live = read_jsonl(LIVE_PATH)
            if not any(a.get("id") == asp_id for a in live):
                print(f"REFUSED: {asp_id} is already archived — cannot modify.",
                      file=sys.stderr)
                sys.exit(1)

def read_json(path):
    """Read a JSON file and return a dict."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    """Atomically write a dict as pretty-printed JSON with locking and history."""
    from _fileops import locked_write_json
    locked_write_json(path, data)

# ---------------------------------------------------------------------------
# Helpers: validation
# ---------------------------------------------------------------------------

def validate_aspiration(asp):
    """Validate an aspiration dict. Raises ValueError on invalid."""
    required = {"id", "title", "status", "goals", "priority", "archived"}
    missing = required - set(asp.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not ASP_ID_RE.match(asp["id"]):
        raise ValueError(f"Invalid aspiration ID format: {asp['id']} (expected asp-NNN)")

    if asp["status"] not in VALID_ASP_STATUSES:
        raise ValueError(f"Invalid aspiration status: {asp['status']}")

    if asp["priority"] not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {asp['priority']}")

    if not isinstance(asp["goals"], list):
        raise ValueError("goals must be a list")

    if not isinstance(asp["archived"], bool):
        raise ValueError("archived must be a boolean")

    if "scope" in asp and asp["scope"] not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {asp['scope']} (expected one of {VALID_SCOPES})")

    # Parallelizability classification (multi-agent coordination)
    valid_coordination_modes = ("parallel", "serial", "mixed")
    if "coordination_mode" in asp and asp["coordination_mode"] not in valid_coordination_modes:
        raise ValueError(
            f"Invalid coordination_mode: {asp['coordination_mode']} "
            f"(expected one of {valid_coordination_modes})")

    if "sessions_active" in asp and not isinstance(asp["sessions_active"], (int, float)):
        raise ValueError("sessions_active must be a number")

    # Co-investigation primitive (): co_investigators names the
    # agents committed to co-iterate on this aspiration. Pass-through at
    # the schema layer; goal-selector reads goal-level co_parent_id, not
    # aspiration-level co_investigators (the latter is documentation /
    # board-display surface). Schema + protocol: coordination.md
    # → Co-Investigation Protocol.
    if "co_investigators" in asp:
        if not isinstance(asp["co_investigators"], list):
            raise ValueError("co_investigators must be a list")
        for name in asp["co_investigators"]:
            if not isinstance(name, str):
                raise ValueError("co_investigators entries must be strings")

    for goal in asp["goals"]:
        validate_goal(goal)
        # Structured-check schema (). Deliberately HERE and not in
        # validate_goal: validate_goal also validates the update_goal candidate,
        # so running it there would make every status change on the 19 live
        # goals that already carry schema-invalid checks start failing. This is
        # the ADD-shaped seam, matching where the daemon calls its own copy.
        _assert_no_invalid_checks(goal)

def validate_verification(verification, goal_id):
    """Validate the unified verification field on a goal."""
    if not isinstance(verification, dict):
        raise ValueError(f"Goal {goal_id}: verification must be a dict")
    # outcomes: list of strings (human-readable success criteria)
    outcomes = verification.get("outcomes")
    if outcomes is not None and not isinstance(outcomes, list):
        raise ValueError(f"Goal {goal_id}: verification.outcomes must be a list")
    # outcomes_agent_leg: list of strings -- the agent-side subset of success
    # criteria on a participants:[agent,user] collaborative goal, separate from
    # outcomes (which span both legs). Lets Phase 5 verify close "agent leg
    # complete, user leg pending" as a valid terminal state without inventing
    # closure justification (US-04 / ). Validated like outcomes.
    outcomes_agent_leg = verification.get("outcomes_agent_leg")
    if outcomes_agent_leg is not None and not isinstance(outcomes_agent_leg, list):
        raise ValueError(f"Goal {goal_id}: verification.outcomes_agent_leg must be a list")
    # checks: list of dicts (machine-verifiable conditions)
    checks = verification.get("checks")
    if checks is not None and not isinstance(checks, list):
        raise ValueError(f"Goal {goal_id}: verification.checks must be a list")
    # preconditions: list of strings (what must be true before execution)
    preconditions = verification.get("preconditions")
    if preconditions is not None and not isinstance(preconditions, list):
        raise ValueError(f"Goal {goal_id}: verification.preconditions must be a list")
    # FIX 2 ( / rb-4371): a structured goal_completed_after precondition
    # MISSING after_ref silently perma-blocks the goal — predicate.py returns False
    # forever, goal-selector classifies precondition_unmet and EXCLUDES the goal
    # from selection while status stays `pending` (invisible except via
    # `goal-selector blocked`). Refuse it at filing time — fail LOUD, not silent.
    for pc in (preconditions or []):
        if isinstance(pc, dict) and pc.get("type") == "goal_completed_after":
            if not pc.get("goal_id") or not pc.get("after_ref"):
                raise ValueError(
                    f"Goal {goal_id}: a goal_completed_after precondition requires "
                    f"both 'goal_id' and 'after_ref' (got {pc!r}). A missing after_ref "
                    f"silently perma-blocks the goal (rb-4371)."
                )

def validate_goal(goal):
    """Validate a goal dict within an aspiration.

    Accepts both new unified 'verification' field and legacy 'desiredEndState' +
    'completion_check' fields. Both formats are valid for backward compatibility.
    """
    if "id" not in goal:
        raise ValueError("Goal missing 'id' field")
    if not GOAL_ID_RE.match(goal["id"]):
        raise ValueError(f"Invalid goal ID format: {goal['id']} (expected g-NNN-NN[N[N]] or g-NNN-NN-a)")
    if "status" not in goal:
        raise ValueError(f"Goal {goal['id']} missing 'status' field")
    if goal["status"] not in VALID_GOAL_STATUSES:
        raise ValueError(f"Invalid goal status for {goal['id']}: {goal['status']}")
    # Validate unified verification field if present
    if "verification" in goal:
        validate_verification(goal["verification"], goal["id"])
    # Validate recurring goal fields if present
    if "interval_hours" in goal:
        if not isinstance(goal["interval_hours"], (int, float)) or goal["interval_hours"] <= 0:
            raise ValueError(f"Goal {goal['id']}: interval_hours must be a positive number")
    if "recurring" in goal:
        if not isinstance(goal["recurring"], bool):
            raise ValueError(f"Goal {goal['id']}: recurring must be a boolean")
    # Validate deferred goal fields if present
    if "deferred_until" in goal:
        val = goal["deferred_until"]
        if val is not None:
            try:
                datetime.fromisoformat(str(val))
            except (ValueError, TypeError):
                raise ValueError(f"Goal {goal['id']}: deferred_until must be a valid ISO 8601 timestamp or null")
    if "defer_reason" in goal:
        val = goal["defer_reason"]
        if val is not None and not isinstance(val, str):
            raise ValueError(f"Goal {goal['id']}: defer_reason must be a string or null")
    # Co-investigation primitive (): co_parent_id names the parent
    # goal both agents are iterating on. When non-null, must be a valid
    # goal-id; goal-selector.py reads it for the co_invest_alignment bonus.
    # Schema + protocol: core/config/conventions/coordination.md
    # → Co-Investigation Protocol.
    if "co_parent_id" in goal:
        val = goal["co_parent_id"]
        if val is not None and not isinstance(val, str):
            raise ValueError(f"Goal {goal['id']}: co_parent_id must be a string or null")
        if isinstance(val, str) and not GOAL_ID_RE.match(val):
            raise ValueError(
                f"Goal {goal['id']}: co_parent_id must be a valid goal-id (got {val!r})"
            )
    # user_leg_scope: when set, names what the user must approve. Matched by
    # guard-349 against standing grants in capability-routing.md. Legacy goals
    # with participants:[agent,user] but no user_leg_scope still work — the
    # stderr WARN in cmd_add_goal surfaces them for backfill without blocking.
    if "user_leg_scope" in goal:
        val = goal["user_leg_scope"]
        if val is not None and val not in VALID_USER_LEG_SCOPES:
            raise ValueError(
                f"Goal {goal['id']}: user_leg_scope must be null or one of "
                f"{sorted(VALID_USER_LEG_SCOPES)}, got {val!r}"
            )
    # Validate reallocatable field (multi-agent straggler mitigation)
    if "reallocatable" in goal:
        if not isinstance(goal["reallocatable"], bool):
            raise ValueError(f"Goal {goal['id']}: reallocatable must be a boolean")
    # Validate depends_on field (output-passing dependencies, arXiv 2603.28990).
    # Delegates to the shared gates.depends_on_consistency module ()
    # for the same guard-547 reason as the two gates below it: this check lived
    # here alone for the life of the field, and the daemon _validate_goal subset
    # omits it — so under no-python-cli-fallback it was inert on every real
    # filing. 5 of 6 live carriers violate the invariant it "enforced".
    _check_depends_on_consistency(goal)
    # Validate abstained_by field (self-abstention, arXiv 2603.28990)
    if "abstained_by" in goal:
        val = goal["abstained_by"]
        if val is not None and not isinstance(val, str):
            raise ValueError(f"Goal {goal['id']}: abstained_by must be a string or null")
    # Validate intended_agent routing hint (; pairs with capability-route-gate.py)
    # Delegates to gates.intended_agent_vocab (selection-stack review
    # 2026-08-21) — fourth guard-547 extraction alongside the three below: the
    # daemon _validate_goal subset omitted this check, so under
    # no-python-cli-fallback it was inert on every real filing (5 live goals
    # carried "agent"/"reducer"/"any" from verbatim board-tag copies). The
    # gate resolves the roster lazily per call (mtime-cached), preserving the
    # H1 2026-05-18 staleness fix the inline copy carried.
    _check_intended_agent_vocab(goal)
    # Validate goal_source attribution field ()
    if "goal_source" in goal:
        val = goal["goal_source"]
        if val is not None and val not in VALID_GOAL_SOURCES:
            raise ValueError(
                f"Goal {goal['id']}: goal_source must be null or one of "
                f"{sorted(VALID_GOAL_SOURCES)}, got {val!r}"
            )
    # Validate filed_by_agent attribution field (). Stamped at add time
    # by the daemon add-goal endpoint (the filing agent) for the per-agent
    # contribution-vs-harm scorecard. Kept a free string (like abstained_by),
    # NOT constrained to the active-agent set, so a goal filed by an agent later
    # removed from the roster still validates. Backward-compat: missing field =
    # unknown (no requirement clause — never raise on absence).
    if "filed_by_agent" in goal:
        val = goal["filed_by_agent"]
        if val is not None and not isinstance(val, str):
            raise ValueError(
                f"Goal {goal['id']}: filed_by_agent must be a string or null, got {val!r}"
            )
    # Validate inner_refinement field (optional Self-Refine inner loop,  / BRD Gap 4).
    # Absent or null = OFF (default); goals without it behave exactly as before.
    # When set: {max_iters: int in [1, INNER_REFINEMENT_MAX_ITERS_CAP], satisficed_when: non-empty str}.
    # The max_iters cap is the structural termination guarantee. CLI-only by design
    # (matches the reallocatable/abstained_by/intended_agent optional-field pattern;
    # the daemon _validate_goal deliberately validates only id/status/recurring/interval).
    # The execution-side clamp-to-CAP in aspirations-execute is the termination
    # guarantee that holds regardless of which write path fired (guard-547 split).
    if "inner_refinement" in goal:
        val = goal["inner_refinement"]
        if val is not None:
            if not isinstance(val, dict):
                raise ValueError(
                    f"Goal {goal['id']}: inner_refinement must be a dict or null, got {val!r}"
                )
            mi = val.get("max_iters")
            if not isinstance(mi, int) or isinstance(mi, bool) or mi < 1 or mi > INNER_REFINEMENT_MAX_ITERS_CAP:
                raise ValueError(
                    f"Goal {goal['id']}: inner_refinement.max_iters must be an int in "
                    f"[1, {INNER_REFINEMENT_MAX_ITERS_CAP}], got {mi!r}"
                )
            sw = val.get("satisficed_when")
            if not isinstance(sw, str) or not sw.strip():
                raise ValueError(
                    f"Goal {goal['id']}: inner_refinement.satisficed_when must be a non-empty string"
                )
    # Prose-verification drift check (, rb-329 schema-drift family):
    # descriptions that advertise "Verification outcomes:" / "Verification checks:"
    # without a corresponding structured verification.checks entry silently slip
    # past /verify-learning S49.7 post-write. Catch them pre-write so the drift
    # can't enter the file in the first place.
    _check_prose_verification_drift(goal)

# Prose-verification-drift markers + check live in the shared
# gates.prose_verification module () so the CLI validate_goal path
# and the daemon aspirations_write.py paths run IDENTICAL logic (guard-547
# anti-drift — duplication is exactly the CLI/daemon split that produced the
# original FN gap). Re-exported here for any caller that imported the constant
# from this module.
from gates.prose_verification import (  # noqa: E402
    PROSE_VERIFICATION_MARKERS,  # noqa: F401
    evaluate as _prose_verification_evaluate,
)
from gates.check_schema import evaluate as _check_schema_evaluate  # noqa: E402
from gates.depends_on_consistency import (  # noqa: E402
    evaluate as _depends_on_consistency_evaluate,
)
from gates.intended_agent_vocab import (  # noqa: E402
    evaluate as _intended_agent_vocab_evaluate,
)


def _check_depends_on_consistency(goal):
    # Delegates to the shared gate, raising ValueError so validate_goal's
    # existing contract (raise → caller surfaces) is preserved. The daemon
    # calls the SAME evaluate() from _assert_depends_on_consistency, so the two
    # write paths can no longer drift apart (guard-547).
    result = _depends_on_consistency_evaluate(goal)
    if result["would_block"]:
        raise ValueError(result["message"])


def _check_intended_agent_vocab(goal):
    # Delegates to the shared gate, raising ValueError so validate_goal's
    # existing contract (raise → caller surfaces) is preserved. The daemon
    # calls the SAME evaluate() from _assert_intended_agent_vocab (guard-547).
    result = _intended_agent_vocab_evaluate(goal)
    if result["would_block"]:
        raise ValueError(result["message"])


def _check_prose_verification_drift(goal):
    # Delegates to the shared gate, raising ValueError on prose-only drift so
    # validate_goal's existing contract (raise → caller surfaces) is preserved.
    result = _prose_verification_evaluate(goal)
    if result["would_block"]:
        raise ValueError(result["message"])


def _assert_no_invalid_checks(goal):
    """Refuse schema-invalid verification.checks ().

    Same single-module / both-sides shape as the prose gate above, for the same
    guard-547 reason: goal filing is daemon-routed, so a validator that existed
    only here would be inert on every real filing while its tests stayed green.
    gates.check_schema is the shared implementation; this is the CLI half.
    """
    result = _check_schema_evaluate(goal)
    if result["warning"]:
        print(result["warning"], file=sys.stderr)
    if result["would_block"]:
        raise ValueError(result["message"])

def validate_evolution_event(evt):
    """Validate an evolution event dict. Raises ValueError on invalid."""
    required = {"date", "event", "details"}
    missing = required - set(evt.keys())
    if missing:
        raise ValueError(f"Missing required evolution event fields: {missing}")
    if not DATE_RE.match(str(evt["date"])):
        raise ValueError(f"Invalid date format: {evt['date']} (expected YYYY-MM-DD)")

# ---------------------------------------------------------------------------
# Helpers: search
# ---------------------------------------------------------------------------

def find_aspiration_by_id(items, asp_id):
    """Find an aspiration by ID. Returns (index, aspiration) or None."""
    for i, asp in enumerate(items):
        if asp.get("id") == asp_id:
            return (i, asp)
    return None

def find_goal_in_aspirations(items, goal_id):
    """Find a goal across all aspirations. Returns (asp_index, goal_index, aspiration) or None."""
    for ai, asp in enumerate(items):
        for gi, goal in enumerate(asp.get("goals", [])):
            if goal.get("id") == goal_id:
                return (ai, gi, asp)
    return None

def find_recurring_goals(asp):
    """Return list of goals with recurring: true in an aspiration."""
    return [g for g in asp.get("goals", []) if g.get("recurring")]

def find_shape_recurring_corrupted(asp):
    """Return goals with recurring-shape fields but recurring=false AND status=completed.
    Pattern observed in alpha/aspirations.jsonl g-001-05 (iter 100-101 session-50,
    rb-295): goals that were once recurring got their flag flipped without their
    interval_hours/lastAchievedAt cleared, then landed at status=completed where
    the standard safety net (which checks recurring=true) couldn't reach them.
    Bash-gated counterpart to the aspirations-precheck Recurring Goal Safety Net
    — see g-001-138 / rb-295."""
    return [g for g in asp.get("goals", [])
            if g.get("status") == "completed"
            and not g.get("recurring")
            and g.get("interval_hours")
            and g.get("lastAchievedAt")]

def find_unfinished_goals(asp):
    """Return non-recurring goals not in a terminal status."""
    return [g for g in asp.get("goals", [])
            if not g.get("recurring") and g.get("status") not in TERMINAL_GOAL_STATUSES]

def parse_value(value_str):
    """Parse a string value into the appropriate Python type."""
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if value_str == "null":
        return None
    if value_str == "[]":
        return []
    # Try JSON parse for complex values (objects, arrays)
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    # Try int
    try:
        return int(value_str)
    except ValueError:
        pass
    # Try float
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str

# ---------------------------------------------------------------------------
# Helpers: duplicate check
# ---------------------------------------------------------------------------

def check_no_duplicate_id(items, asp_id):
    """Raise ValueError if asp_id already exists in items."""
    for item in items:
        if item.get("id") == asp_id:
            raise ValueError(f"Duplicate aspiration ID: {asp_id}")

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

# The compact goal projection lives in the daemon:
# mind_api/src/endpoints/aspirations.py `_COMPACT_GOAL_KEEP` (SSOT). The CLI
# `COMPACT_GOAL_KEEP` copy that lived here was retired in  — it had
# zero consumers since the daemon-only cutover removed the CLI read path
# (`--active-compact` routes to the daemon via aspirations-read.sh).

def recompute_progress(asp):
    """Derive progress from goals — recurring goals excluded from completion counts.

    Recurring goals run perpetually and never "complete", so they must not inflate
    the total or be counted as completed. They are tracked separately.
    """
    goals = asp.get("goals", [])
    recurring_count = sum(1 for g in goals if g.get("recurring"))
    # Census-augmented (B9-deep): "non_recurring" = all non-recurring goals
    # (abandoned included). effective_counts folds every archived status back in,
    # so eviction leaves total/completed/fan_out_ratio byte-identical.
    total, completed_goals = _effective_counts(asp, include_recurring=False)
    # fan_out_ratio: growth from the creation-time seed. None when
    # initial_goal_count is absent (predates the metric — no inferred
    # backfill) or 0 (ratio from an empty seed is undefined).
    igc = asp.get("initial_goal_count")
    fan_out_ratio = (round(total / igc, 2)
                     if isinstance(igc, int) and igc > 0 else None)
    asp["progress"] = {
        "completed_goals": completed_goals,
        "total_goals": total,
        "recurring_goals": recurring_count,
        "fan_out_ratio": fan_out_ratio,
    }

def _validate_blocker_ref(raw):
    """Thin wrapper — delegates to gates.blocker_ref.validate."""
    from gates.blocker_ref import validate
    return validate(raw)

def _credential_enum_guard(goal_id, raw_ref, context_text, args):
    """Door-B credential-enumeration gate, CLI lane ().

    Refuses (sys.exit(1)) when a credentials-required blocker_ref carries no
    proof that the grant is genuinely unavailable. Returns None to allow.

    `raw_ref` MUST be the un-normalized --blocker-ref argument: validate()
    rebuilds a 5-key envelope and drops credential_source_enumeration, so
    checking its output would refuse every credentials-required blocker.

    Same predicate as blocker-create-gate check #5 — see gates/credential_enum.py
    for why the two doors share one implementation. NOTE this is the CLI/import
    lane; `aspirations-update-goal.sh` is daemon-only, so the enforcing copy for
    live traffic is aspirations_write.py::_credential_enum_guard. Both call the
    same predicate; this one exists so the lanes cannot diverge.
    """
    from gates.credential_enum import check, refusal_message
    result = check(raw_ref)
    if result.get("passed"):
        return None

    override = getattr(args, "override_blocker_gate", None)
    if override:
        from gates.blocker_ref import log_unstructured_override
        if WORLD_DIR is None:
            # Mirror _log_unstructured_defer_override's surface: an override
            # granted WITHOUT an audit record must say so. Silence here would
            # make an unaudited bypass indistinguishable from a logged one.
            print("[credential-enum-gate] WARN: override granted but not logged "
                  "(no MIND_AGENT binding -> cannot resolve WORLD_PATH).",
                  file=sys.stderr)
        else:
            log_unstructured_override(
                WORLD_DIR,
                goal_id=goal_id,
                defer_reason_text=context_text,
                justification=override,
                agent_name=os.environ.get("MIND_AGENT", "") or "unknown",
                source="aspirations.py:cmd_update_goal:credential-enumeration-override",
                which_checks_bypassed=["credential_enumeration"],
            )
        print(f"[credential-enum-gate] --override-blocker-gate on {goal_id}: "
              f"{override}", file=sys.stderr)
        return None

    print(refusal_message(
        goal_id, result.get("reason", ""),
        flag_hint='pass --override-blocker-gate "<justification>" (audited)',
    ), file=sys.stderr)
    sys.exit(1)


def _log_unstructured_defer_override(goal_id, defer_reason_text, justification):
    """Thin wrapper — delegates to gates.blocker_ref.log_unstructured_override.

    Preserves the legacy CLI's stderr surface: WARN on skipped-because-no-
    WORLD_DIR, WARN on write failure. The daemon path is silent (no stderr
    to write to); module-level helper returns the same path-or-None result.
    """
    from gates.blocker_ref import log_unstructured_override
    if WORLD_DIR is None:
        print("[defer-gate] WARN: override granted but not logged "
              "(no MIND_AGENT binding -> cannot resolve WORLD_PATH).",
              file=sys.stderr)
        return None
    result = log_unstructured_override(
        WORLD_DIR,
        goal_id=goal_id,
        defer_reason_text=defer_reason_text,
        justification=justification,
        agent_name=os.environ.get("MIND_AGENT", "") or "unknown",
        source="aspirations.py:cmd_update_goal:unstructured-defer",
    )
    if result is None:
        # log_unstructured_override returned None — either world_dir was
        # None (already handled above) or the locked_append_jsonl raised.
        # Surface the second case to stderr to match legacy WARN behavior.
        print("[defer-gate] WARN: override-log append failed",
              file=sys.stderr)
    return result

def _extract_defer_date(defer_reason_text: str) -> dict:
    """Extract structured deferred_until from a defer_reason narrative.

    Delegates to gates.defer_date.extract — pure regex, never raises.
    Origin: LifingPolls plan item 5 (2026-05-08).
    """
    from gates.defer_date import extract
    return extract(defer_reason_text)

def _emit_streak_break_signal(goal_id: str, expected_interval: float,
                               actual_elapsed: float,
                               aspiration_id: str | None) -> None:
    """Append a streak-break event to <agent>/session/streak-breaks.jsonl.

    Read by streak-break-reflector.py post-close to file an Investigate
    goal asking what disrupted the cadence. Origin: LifingPolls plan
    item 1 (2026-05-08). Fail-silent caller: any IO error is swallowed
    so the recurring close never fails on signal emission.
    """
    if AGENT_DIR is None:
        return
    session_dir = AGENT_DIR / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "streak-breaks.jsonl"
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "goal_id": goal_id,
        "aspiration_id": aspiration_id,
        "expected_interval_hours": expected_interval,
        "actual_elapsed_hours": round(actual_elapsed, 2),
        "lateness_ratio": round(actual_elapsed / expected_interval, 2)
                          if expected_interval > 0 else None,
        "processed": False,
    }
    # Append directly — this is per-agent session state, not shared world data,
    # so locked_append_jsonl's contention model is overkill. Plain append-only
    # write works because only complete_by writes here and reflector only reads
    # + back-patches processed=true under its own lock.
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def _log_defer_date_extraction(goal_id: str, defer_reason_text: str,
                               extraction: dict) -> None:
    """Append extraction record to world/defer-date-extractions.jsonl.

    Audit trail: which prose phrase was converted into which structured
    deferred_until, when, by whom. Lets a reviewer spot mis-extractions
    (e.g., "in 7 years" reading as a real defer rather than a figure of
    speech). Fail-silent on log error.
    """
    from _fileops import locked_append_jsonl
    if WORLD_DIR is None:
        return
    log_path = WORLD_DIR / "defer-date-extractions.jsonl"
    try:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "agent": os.environ.get("MIND_AGENT", "") or "unknown",
            "goal_id": goal_id,
            "defer_reason": str(defer_reason_text)[:200],
            "extracted_deferred_until": extraction.get("deferred_until"),
            "pattern": extraction.get("pattern"),
            "match_text": extraction.get("match_text"),
        }
        locked_append_jsonl(str(log_path), record)
    except Exception as e:
        print(f"[defer-date-extractor] WARN: log append failed: {e}",
              file=sys.stderr)

def _run_capability_gate_for_defer(defer_reason_text: str,
                                   goal_id: str | None = None) -> dict:
    """Invoke capability-gate.py against a defer_reason string.

    Parallel to blocker-recheck.py's _run_gate — same invocation shape,
    same fail-open semantics (a broken gate must not block legitimate
    defer writes). Returns the parsed JSON result, or a match_count=0 /
    would_block=False fallback on subprocess failure.

    We pass intended-participants=user because a defer implicitly hands
    the goal off — the goal is frozen until an external signal clears
    it. Semantically that's the same routing decision as participants:
    [user], so the gate applies with the same rules.

    g-257-03: passes --suggest-unblock + --for-goal-id (when goal_id is
    provided) so the gate emits unblock_title/unblock_description/
    matched_capability fields. The caller (cmd_update_goal defer-time
    branch) consumes these to atomically file an Unblock goal before
    refusing the defer write.

    g-115-277 / msg-bravo-456: timeout=15 mirrors the timeout pattern of
    sibling subprocess.run calls in this file (line 854/2091). Without it,
    a hang in capability-gate.py (e.g., yaml.safe_load on a malformed
    capability-routing.md) would freeze every defer_reason write
    indefinitely. 15s matches the goal-duplication-gate timeout.
    """
    gate_path = CORE_ROOT / "scripts" / "capability-gate.py"
    cmd = [sys.executable, str(gate_path),
           "--failure-reason", defer_reason_text,
           "--intended-participants", "user",
           "--output", "json",
           # : tell the gate this is the DEFER path so its `reason`
           # text recommends --force-defer (the flag this path honours) rather
           # than --override-agent-match (the CREATE_BLOCKER bypass, explicitly
           # NOT honoured here — ). Following the old text verbatim
           # failed quietly: override_applied stayed null while the gate
           # re-blocked on different keywords.
           "--caller-context", "defer",
           "--suggest-unblock"]
    if goal_id:
        cmd.extend(["--for-goal-id", goal_id])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("[defer-gate] capability-gate timeout (15s) — fail-open",
              file=sys.stderr)
        return {"match_count": 0, "would_block": False,
                "error": "gate timeout"}
    except Exception as exc:
        print(f"[defer-gate] capability-gate invocation error: {exc}",
              file=sys.stderr)
        return {"match_count": 0, "would_block": False,
                "error": "gate invocation failed"}
    try:
        return json.loads(result.stdout)
    except Exception:
        print(f"[defer-gate] capability-gate output unparseable "
              f"(rc={result.returncode}): "
              f"{(result.stderr or result.stdout or '').strip()[:200]}",
              file=sys.stderr)
        return {"match_count": 0, "would_block": False,
                "error": "gate output unparseable"}

def _run_uncommitted_work_gate(goal_id: str,
                                override: str | None = None) -> dict:
    """Invoke uncommitted-work-gate.py against the framework repo.

    Pattern mirror of _run_capability_gate_for_defer. Returns the parsed
    JSON {would_block, dirty_framework_files, repo_path, override_applied}.
    Fail-open on subprocess errors — a broken gate must not block legitimate
    goal closes (the orphan-code class is bad, but freezing every close
    on a gate bug is worse).

    Override semantics: when override is non-None, the gate writes an
    audit record to world/uncommitted-work-overrides.jsonl and returns
    would_block=False. The caller still surfaces the override on stderr
    for the operator-visible audit trail.
    """
    gate_path = CORE_ROOT / "scripts" / "uncommitted-work-gate.py"
    cmd = [sys.executable, str(gate_path),
           "--goal-id", goal_id,
           "--output", "json"]
    if override is not None:
        cmd.extend(["--override", override])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("[uncommitted-gate] timeout (20s) — fail-open", file=sys.stderr)
        return {"would_block": False, "dirty_framework_files": [],
                "error": "gate timeout"}
    except Exception as exc:
        print(f"[uncommitted-gate] invocation error: {exc} — fail-open",
              file=sys.stderr)
        return {"would_block": False, "dirty_framework_files": [],
                "error": "gate invocation failed"}
    try:
        return json.loads(result.stdout)
    except Exception:
        print(f"[uncommitted-gate] output unparseable (rc={result.returncode}): "
              f"{(result.stderr or result.stdout or '').strip()[:200]}",
              file=sys.stderr)
        return {"would_block": False, "dirty_framework_files": [],
                "error": "gate output unparseable"}

def _run_completion_artifact_gate(goal_id: str, goal_title: str,
                                  goal_description: str,
                                  override: "str | None" = None) -> dict:
    """Invoke goal-completion-artifact-gate.py against a goal record.

    Pattern mirror of _run_uncommitted_work_gate. Returns the parsed JSON
    payload from gates/completion_artifact.evaluate(). Fail-open on
    subprocess errors — a broken gate must not block legitimate goal
    closes (canonical incident g-115-724 is bad, but freezing every
    close on a gate bug is worse).
    """
    gate_path = CORE_ROOT / "scripts" / "goal-completion-artifact-gate.py"
    cmd = [sys.executable, str(gate_path),
           "--goal-id", goal_id,
           "--goal-title", goal_title,
           "--goal-description", goal_description,
           "--output", "json"]
    if override is not None:
        cmd.extend(["--override", override])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("[completion-artifact-gate] timeout (15s) — fail-open",
              file=sys.stderr)
        return {"would_block": False, "missing_artifacts": [],
                "error": "gate timeout"}
    except Exception as exc:
        print(f"[completion-artifact-gate] invocation error: {exc} — "
              "fail-open", file=sys.stderr)
        return {"would_block": False, "missing_artifacts": [],
                "error": "gate invocation failed"}
    try:
        return json.loads(result.stdout)
    except Exception:
        print(f"[completion-artifact-gate] output unparseable "
              f"(rc={result.returncode}): "
              f"{(result.stderr or result.stdout or '').strip()[:200]}",
              file=sys.stderr)
        return {"would_block": False, "missing_artifacts": [],
                "error": "gate output unparseable"}

_UNBLOCK_ACTIVE_STATUSES = ("pending", "in-progress")

# Circuit breaker (). _find_existing_unblock_for dedups only ACTIVE
# Unblocks — a RESOLVED one never suppresses a re-file (guard-487-aligned: a
# genuine recurrence must re-route). Under a STANDING outage that produces one
# Unblock per defer attempt, each resolving before the next fires. After N such
# same-parent Unblocks have RESOLVED while the parent is STILL being
# defer-attempted, the block is standing (not recurring): escalate the PARENT to
# a tracked blocker (blocker_ref + structured "Circuit breaker:" defer) so the
# churn stops at the root. FAIL-OPEN / non-suppressing: the Nth+1 Unblock is
# STILL filed — this only ADDS tracking, never removes routing. Mirrors the
# consecutive_goal_failures circuit breaker (aspirations loop Phase 5.5).
_UNBLOCK_RESOLVED_STATUSES = ("completed", "skipped")
_UNBLOCK_CIRCUIT_BREAKER_THRESHOLD = 3

def _find_existing_unblock_for(items: list, original_goal_id: str,
                               verb: str | None = None,
                               also_scan_agent: bool = True) -> dict | None:
    """Find an existing pending/in-progress Unblock goal that already covers
    `original_goal_id`. Returns the matching goal record (with `_aspiration_id`
    and `_source` keys added for caller diagnostics) or None.

    g-257-04: refines g-257-03's inline origin_signal scan with three
    complementary matching strategies that catch BOTH defer-gate-filed
    Unblocks (via origin_signal) AND human-filed Unblocks (via title or
    description text). Cross-queue scan covers the case where bravo files
    in world while alpha files in agent (or vice versa).

    Three OR-ed strategies:
      (a) origin_signal == 'unblock:{original_goal_id}' — exact framework match
      (b) title matches 'Unblock:.*for {original_goal_id}\\b' — standard human convention
      (c) description references both `verb` AND `original_goal_id` within
          80 chars of each other (proximity match) — only when verb provided

    Skipped statuses: completed/blocked/skipped/expired/decomposed/superseded.
    A previously-resolved Unblock does NOT block re-filing — the original
    failure may have recurred and warrant a fresh routing attempt.

    The (a) strategy is exact and always applies. (b) catches "Unblock:
    fix-deploy for g-115-149" or similar manual filings. (c) catches the
    rare case of free-form titles (e.g., "Investigate deploy issue
    blocking g-115-149") — requires verb to disambiguate from unrelated
    mentions of the goal-id.

    Cross-queue scan reads AGENT_DIR/aspirations.jsonl when also_scan_agent
    is True. Fails open: if the agent file is missing or unreadable, skip
    that scan (return-as-if-not-found) — matches the contract of
    _goal_in_agent_queue helper elsewhere in this file.
    """
    expected_origin = f"unblock:{original_goal_id}"
    title_re = re.compile(
        r"Unblock:.*\bfor\s+" + re.escape(original_goal_id) + r"\b",
        re.IGNORECASE,
    )

    def _scan(asp_iter, source_label):
        for asp in asp_iter:
            asp_id = asp.get("id", "")
            for g in asp.get("goals", []):
                if g.get("status") not in _UNBLOCK_ACTIVE_STATUSES:
                    continue
                # Strategy (a): origin_signal exact match
                if g.get("origin_signal") == expected_origin:
                    return {**g, "_aspiration_id": asp_id,
                            "_source": source_label, "_match_strategy": "origin_signal"}
                # Strategy (b): title regex
                if title_re.search(g.get("title", "") or ""):
                    return {**g, "_aspiration_id": asp_id,
                            "_source": source_label, "_match_strategy": "title_regex"}
                # Strategy (c): description proximity (verb + goal-id within 80 chars)
                if verb:
                    desc = (g.get("description", "") or "").lower()
                    verb_lo = verb.lower()
                    gid_lo = original_goal_id.lower()
                    v_idx = desc.find(verb_lo)
                    g_idx = desc.find(gid_lo)
                    if v_idx >= 0 and g_idx >= 0 and abs(v_idx - g_idx) <= 80:
                        return {**g, "_aspiration_id": asp_id,
                                "_source": source_label,
                                "_match_strategy": "description_proximity"}
        return None

    hit = _scan(items, "world")
    if hit is not None:
        return hit

    if also_scan_agent and AGENT_DIR is not None:
        agent_live = AGENT_DIR / "aspirations.jsonl"
        if agent_live.exists():
            try:
                agent_items = read_jsonl(agent_live)
                hit = _scan(agent_items, "agent")
                if hit is not None:
                    return hit
            except Exception:
                # Fail-open: an unreadable agent file should not block defer-gate
                # work. Same contract as _goal_in_agent_queue (line 2169 region).
                pass

    return None

def _count_resolved_unblocks_for(items: list, original_goal_id: str,
                                 also_scan_agent: bool = True) -> int:
    """Count RESOLVED (completed/skipped) Unblock goals for original_goal_id.

    Companion to _find_existing_unblock_for (which finds ACTIVE Unblocks for
    dedup). This counts the RESOLVED ones — the churn signal the g-115-2772
    circuit breaker reads: N resolved same-parent Unblocks means the
    defer->Unblock loop has already cycled N times on a STANDING block.

    Matches ONLY strategy (a) — origin_signal == 'unblock:{id}' — the exact
    framework marker every defer-gate-filed Unblock carries. The title /
    description strategies of _find_existing_unblock_for are DELIBERATELY
    excluded: a false-positive count would ESCALATE spuriously, and
    origin_signal is unambiguous for the gate-filed Unblocks this breaker
    targets.

    Fail-open: an unreadable agent queue is skipped (as-if-empty), same
    contract as _find_existing_unblock_for. A count of 0 on any read error
    means NO escalation (the Unblock still files normally) — the fail-OPEN
    direction for an ESCALATION gate: never a false suppression (guard-487 is
    about suppression gates; this one only adds tracking, so its safe-default
    is "do not escalate").
    """
    expected_origin = f"unblock:{original_goal_id}"

    def _count(asp_iter) -> int:
        n = 0
        for asp in asp_iter:
            for g in asp.get("goals", []):
                if (g.get("origin_signal") == expected_origin
                        and g.get("status") in _UNBLOCK_RESOLVED_STATUSES):
                    n += 1
        return n

    total = _count(items)

    if also_scan_agent and AGENT_DIR is not None:
        agent_live = AGENT_DIR / "aspirations.jsonl"
        if agent_live.exists():
            try:
                total += _count(read_jsonl(agent_live))
            except Exception:
                # Fail-open: unreadable agent queue -> skip (no false escalation).
                pass

    return total

def _escalate_standing_blocker(items: list, original_goal_id: str,
                               resolved_count: int) -> str | None:
    """Promote a churning parent goal to a tracked standing blocker ().

    Called by _file_unblock_under_existing_lock when
    _count_resolved_unblocks_for >= _UNBLOCK_CIRCUIT_BREAKER_THRESHOLD. Sets a
    structured blocker_ref + a 'Circuit breaker:' structured defer_reason on the
    PARENT goal so it stops being defer-attempted (the churn root) and becomes a
    properly-tracked blocked goal (quiescence-legitimate). Mirrors the
    consecutive_goal_failures circuit breaker (aspirations loop Phase 5.5); the
    'Circuit breaker:' prefix is a STRUCTURED_DEFER_PREFIX so the escalated defer
    bypasses the capability gate and is not swept by defer-recheck.

    FAIL-OPEN / non-suppressing (guard-487): the CALLER still files the Unblock
    after this returns — this ONLY adds tracking to the parent, never removes
    routing.

    Idempotent: if the parent already carries a blocker_ref, do nothing (the
    standing block is already tracked) — returns None so the caller does not
    double-log.

    Returns the blocker_ref.external_id on a fresh escalation, else None
    (parent not found, already tracked, or validator rejected).
    """
    # Locate the parent in the world items (cmd_update_goal found it there; the
    # narrative defer that triggered this path is on it, never yet written).
    parent = None
    for asp in items:
        for g in asp.get("goals", []):
            if g.get("id") == original_goal_id:
                parent = g
                break
        if parent is not None:
            break
    if parent is None:
        return None  # fail-open: nothing to escalate

    if parent.get("blocker_ref"):
        return None  # idempotent: standing block already tracked

    from gates.blocker_ref import validate as _validate_blocker_ref
    external_id = f"standing-unblock-churn:{original_goal_id}"
    ok, ref = _validate_blocker_ref({
        "type": "infrastructure",
        "external_id": external_id,
    })
    if not ok:
        # Validator rejected — fail-open: skip escalation, let the Unblock file.
        print(f"[unblock-circuit-breaker] blocker_ref build failed for "
              f"{original_goal_id}: {ref}", file=sys.stderr)
        return None

    now_iso = datetime.now().isoformat(timespec="seconds")
    parent["blocker_ref"] = ref
    parent["defer_reason"] = (
        f"Circuit breaker: standing blocker after {resolved_count} resolved "
        f"same-parent Unblocks — escalated to tracked blocker "
        f"({external_id}); churn root-fix g-115-2772"
    )
    parent["defer_reason_set_at"] = now_iso
    print(f"[unblock-circuit-breaker] ESCALATED {original_goal_id} to standing "
          f"blocker after {resolved_count} resolved Unblocks "
          f"(>= {_UNBLOCK_CIRCUIT_BREAKER_THRESHOLD}); set blocker_ref + "
          f"structured defer. Unblock STILL filed (fail-open, guard-487).",
          file=sys.stderr)
    return external_id

def _find_asp_in_items(items: list, asp_id: str) -> tuple[int, dict] | None:
    """Locate an aspiration by id within the in-memory items list.

    Returns (idx, asp) or None when not found. Helper for the
    three-strategy target-aspiration fallback in
    `_file_unblock_under_existing_lock` (rb-655) — same lookup
    primitive used three times for asp-001, the original goal's parent
    asp, and the active-aspiration last-resort.
    """
    for idx, asp in enumerate(items):
        if asp.get("id") == asp_id:
            return (idx, asp)
    return None

def _file_unblock_under_existing_lock(items: list, original_goal_id: str,
                                      gate_result: dict,
                                      original_asp: dict | None = None
                                      ) -> tuple[str | None, str]:
    """Atomically file an Unblock goal within the existing lock.

    Called from cmd_update_goal's defer-gate would_block branch when the
    gate returns unblock_suggested=true. Operates on the in-memory `items`
    list (caller writes it via _write_live_under_lock before sys.exit).

    Returns (filed_goal_id, status_message). filed_goal_id is None when no
    goal was added (existing pending Unblock found, or filing skipped).

    Three-strategy target-aspiration fallback (rb-655):
      a) asp-001 in current source — preserves backward compat for the
         agent queue, which is seeded by init-agent.sh with an asp-001
         per-agent recurring template (alpha → asp-001 "Maintain Agent
         Health"; bravo same).
      b) original goal's parent aspiration — when the current source
         has no asp-001 (the world queue holds work aspirations only,
         no asp-001), the Unblock lands visibly on the same aspiration
         as the goal that triggered it. cmd_update_goal passes the
         parent via `original_asp=asp` at the call site.
      c) first active aspiration in items — last-resort defensive
         fallback when (a) and (b) both fail. Currently unreachable
         because the call site always passes original_asp, but kept
         so a future refactor that drops the kwarg cannot reintroduce
         the silent failure mode.

    asp-001 was historically hardcoded as the universal target on the
    assumption that both queues had it. They do not: only the agent
    queue is seeded with asp-001. Cross-source defer-gate firings
    against the world queue refused the defer correctly but failed to
    file the Unblock — silent data loss, the agent had no surfaced
    action item to drive resolution. The three-strategy fallback fixes
    that without changing same-source behavior.

    Idempotency: delegated to _find_existing_unblock_for (g-257-04). Three
    matching strategies (origin_signal, title-regex, description-proximity)
    across both world and agent queues. Bounds the duplicate-Unblock
    surface from defer-retry loops AND from human-filed Unblocks that use
    the standard naming convention.

    Skips origin-signal-gate / goal-duplication-gate. Rationale: the
    origin_signal "unblock:<id>" is structurally valid by construction;
    the duplication check is replaced by the dedup scan above.
    """
    routing_strategy = None
    target = _find_asp_in_items(items, "asp-001")
    if target is not None:
        routing_strategy = "asp-001-current-source"
    elif original_asp is not None:
        original_asp_id = original_asp.get("id")
        if original_asp_id:
            target = _find_asp_in_items(items, original_asp_id)
            if target is not None:
                routing_strategy = "original-parent-asp"
    if target is None:
        for idx, asp in enumerate(items):
            if asp.get("status") == "active":
                target = (idx, asp)
                routing_strategy = "first-active-asp"
                break
    if target is None:
        return None, (
            "add-goal failed: no target aspiration available "
            "(asp-001 missing in current source, original_asp not "
            "provided or not found, no active aspirations in items)"
        )

    target_idx, target_asp = target
    target_asp_id = target_asp.get("id")

    expected_origin = f"unblock:{original_goal_id}"

    # Extract verb from gate_result.unblock_title for proximity strategy (c).
    # Title format is "Unblock: <verb>" or "Unblock: <verb> for <goal-id>".
    verb_for_dedup = None
    title_str = gate_result.get("unblock_title") or ""
    if title_str.startswith("Unblock:"):
        rest = title_str[len("Unblock:"):].strip()
        # Strip the optional " for <goal-id>" suffix
        if " for " in rest:
            rest = rest.split(" for ", 1)[0].strip()
        if rest:
            verb_for_dedup = rest

    existing = _find_existing_unblock_for(items, original_goal_id,
                                          verb=verb_for_dedup)
    if existing is not None:
        existing_id = existing.get("id")
        existing_asp = existing.get("_aspiration_id")
        existing_src = existing.get("_source")
        match_strategy = existing.get("_match_strategy")
        verb_label = verb_for_dedup or "?"
        #  stderr message format
        print(
            f"[defer-gate] dedup: existing Unblock {existing_id} already "
            f"covers verb={verb_label!r} for original={original_goal_id} "
            f"(source={existing_src}, asp={existing_asp}, "
            f"strategy={match_strategy})",
            file=sys.stderr,
        )
        return None, (f"existing Unblock {existing_id} pending in "
                      f"{existing_asp} ({existing_src} queue, "
                      f"strategy={match_strategy}) — idempotent skip")

    # Circuit breaker (): no ACTIVE Unblock exists but we are about to
    # file a FRESH one — the defer->Unblock loop is cycling. If >= N same-parent
    # Unblocks have already RESOLVED, the block is STANDING (not recurring):
    # escalate the parent to a tracked blocker so the churn stops at the root.
    # FAIL-OPEN — we STILL file the Unblock below (never suppress; guard-487).
    resolved_unblocks = _count_resolved_unblocks_for(items, original_goal_id)
    if resolved_unblocks >= _UNBLOCK_CIRCUIT_BREAKER_THRESHOLD:
        _escalate_standing_blocker(items, original_goal_id, resolved_unblocks)

    unblock_title = gate_result.get("unblock_title") or f"Unblock: capability-routed for {original_goal_id}"
    unblock_description = gate_result.get("unblock_description") or (
        "Defer-gate refused defer_reason — capability-routing matched an "
        f"agent-provisionable action. See capability-gate output for {original_goal_id}."
    )

    asp_num = target_asp_id.replace("asp-", "")
    max_seq = 0
    # Evicted ids count toward max+1 (): re-minting an evicted seq
    # would collide with the merge-layer resurrection tombstone, which drops
    # any goal carrying an evicted id.
    live_ids = [g.get("id", "") for g in target_asp.get("goals", [])]
    for gid in live_ids + _all_evicted_ids(target_asp):
        match = re.match(r"^g-\d{3}-(\d{2,4})", gid)
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    new_goal_id = f"g-{asp_num}-{max_seq + 1:02d}"

    unblock_goal = {
        "id": new_goal_id,
        "title": unblock_title,
        "description": unblock_description,
        "type": "idea",
        "category": "framework-maintenance",
        "priority": "HIGH",
        "participants": ["agent"],
        "status": "pending",
        "blocked_by": [],
        "origin_signal": expected_origin,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        # : parity with the add-goal chokepoint
        # (mind_api/src/endpoints/aspirations_write.py). This site mints goals
        # directly rather than routing through it, so without this the
        # defer->Unblock auto-conversion — the exact lane whose `Apply:` ->
        # `Unblock:` retitles produced 2 of the 3 confirmed splits — would keep
        # emitting nonce-less goals that fall back to the mutable-title identity.
        "alloc_nonce": uuid.uuid4().hex,
        "tags": ["unblock", "defer-gate-routed", "framework-maintenance"],
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
    }

    try:
        validate_goal(unblock_goal)
    except ValueError as exc:
        return None, f"add-goal failed: validation error: {exc}"

    target_asp.setdefault("goals", []).append(unblock_goal)
    recompute_progress(target_asp)
    items[target_idx] = target_asp
    print(
        f"[defer-gate] routing Unblock for {original_goal_id} to "
        f"{target_asp_id} (strategy={routing_strategy})",
        file=sys.stderr,
    )
    return new_goal_id, f"Filed Unblock goal {new_goal_id} in {target_asp_id}"

def _is_narrative_defer(field, value):
    """Thin wrapper — delegates to gates.defer_classifier.is_narrative_defer.

    Single source of truth lives in gates.defer_classifier (daemon-safe).
    Local wrapper preserved so existing call sites stay unchanged.
    """
    from gates.defer_classifier import is_narrative_defer
    return is_narrative_defer(field, value)

def _warn_unresolvable_defer_targets(goal_id, text):
    """ADVISORY: warn when a defer_reason names a DEPENDENCY goal id that
    resolves in no queue and no archive (g-115-7282).

    CLI half of a twin (guard-2323 — port a core/scripts fix to its mind_api
    twin in the SAME change). The framework is daemon-only for this wrapper, so
    the LIVE half is `aspirations_write.py::update_goal`, which appends the same
    message to its `warnings[]` array; a check added only here would be inert on
    the exact path every production caller takes (guard-742). Verified by
    end-to-end probe, not by review: the first version of this fix lived only in
    this function and emitted nothing through the wrapper.

    All logic — including which ids count as dependencies and the empty-universe
    positive control — lives in `gates.defer_target_existence`. Prints to stderr
    and returns; it NEVER refuses the write, and every failure path is silent.
    """
    try:
        from gates.defer_target_existence import evaluate, sources_for
        from _paths import agents_root
        result = evaluate(goal_id, text, sources_for(WORLD_DIR, agents_root()))
    except Exception:
        return
    if result.get("message"):
        print(result["message"], file=sys.stderr)

def cmd_update_asp_field(args):
    """Update a single field on an aspiration in place.

    Mirror of cmd_update_goal but operates at the aspiration level. Used for
    additive metadata fields like chronic_friction (LifingPolls plan item 8)
    that don't need full aspiration revalidation. Skips the origin-signal
    batch gate that cmd_update would trigger.
    """
    from _fileops import acquire_lock, release_lock
    asp_id = args.asp_id
    field = args.field
    value = parse_value(args.value)
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        result = find_aspiration_by_id(items, asp_id)
        if result is None:
            print(f"Aspiration {asp_id} not found", file=sys.stderr)
            sys.exit(1)
        idx = result[0]
        asp = items[idx]
        asp[field] = value
        items[idx] = asp
        _write_live_under_lock(items, f"update-asp-field {asp_id} {field}")
    finally:
        release_lock(lock_path)
    print(json.dumps(asp, indent=2, ensure_ascii=False))

def cmd_update_goal(args):
    # No re-validation — use "null" (not "") to clear date fields like deferred_until.
    goal_id = args.goal_id
    field = args.field
    value = parse_value(args.value)

    # UNKNOWN-FIELD GATE (). Twin of the daemon check in
    # mind_api/src/endpoints/aspirations_write.py::update_goal — BOTH import the
    # same list from _goal_fields, so this is a second CALL SITE and never a
    # second copy of the policy. Runs before the lock: a bad field name should
    # cost no I/O.
    #
    # DOTTED NAMES ARE DELIBERATELY NOT MINE. A dotted path is also an
    # unregistered name, so this gate would happily answer first — with the
    # wrong error. The dedicated dotted-path check further down owns that case
    # and emits the `BLOCKED:` contract message that
    # tests/test_dotted_path_rejection.sh () pins, and the daemon twin
    # gets this ordering for free by running its dotted check FIRST. Skipping
    # dotted names here restores that same precedence on the CLI side.
    # Caught by the full suite, not by review: both refuse, both exit 1, and no
    # stray key is ever created — so the defect was invisible except to the one
    # fixture that asserts WHICH refusal the caller is told about.
    from _goal_fields import is_known as _is_known_goal_field, \
        unknown_field_error as _unknown_goal_field_error
    if "." not in field and not _is_known_goal_field(field):
        justification = getattr(args, "allow_new_field", None)
        if not justification:
            print(_unknown_goal_field_error(field), file=sys.stderr)
            sys.exit(1)
        print(f"[update-goal] --allow-new-field: writing unregistered field "
              f"{field!r} on {goal_id} — {justification}", file=sys.stderr)

    # Full-cycle lock: read + archive cross-check + write are atomic
    from _fileops import acquire_lock, release_lock
    lock_path = LIVE_PATH.with_suffix(".lock")
    try:
        acquire_lock(lock_path)
        items = read_jsonl(LIVE_PATH)
        result = find_goal_in_aspirations(items, goal_id)
        if result is None:
            print(f"Goal {goal_id} not found in any aspiration", file=sys.stderr)
            sys.exit(1)

        asp_idx, goal_idx, asp = result
        _check_not_archived(asp["id"])
        goal = asp["goals"][goal_idx]

        # user_leg_scope advisory on participants updates — see
        # _warn_missing_user_leg_scope. Uses the INCOMING participants value
        # (post-update state) plus the goal's current user_leg_scope.
        if field == "participants":
            _warn_missing_user_leg_scope(goal_id, value, goal.get("user_leg_scope"))

        # blocker_ref SHAPE REFUSAL (). A blocker_ref must be absent,
        # empty, or a dict -- never a scalar. This is the ONLY generic write path
        # for the field: cmd_add_goal already routes through
        # gates.blocker_ref.validate (and so does the unblock circuit breaker),
        # but this function writes whatever parse_value() returns, and
        # parse_value has no per-field type enforcement -- so
        # `aspirations-update-goal.sh <g> blocker_ref "<anything>"` stored a bare
        # string.
        #
        # WHY A REFUSAL AND NOT AN ADVISORY, which is the opposite of the two
        # advisories bracketing it: a bare string is not a judgement call the
        # author might defend, it is unreadable by every consumer. The read-side
        # guards test `isinstance(br, dict) and br.get("type")`, so a scalar is
        # SKIPPED rather than flagged; and an expires_at cannot be stored on a
        # string, so the TTL that would force a re-probe never arms. The value
        # therefore gates work silently and forever.
        #
        # MEASURED THREE TIMES, AND THAT IS THE ACTUAL FINDING. The population is
        # tiny and self-clearing (a blocker_ref disappears when its goal unblocks
        # or completes), so every sweep reports "exactly ONE bare string" and
        # looks stable -- while naming a DIFFERENT record each time: 
        # (2026-07-29),  (2026-08-07),  (2026-08-11, a
        # multi-sentence prose narrative -- a third distinct malformed shape).
        # A count that stays arithmetically true while its referent is replaced
        # is invisible to exactly the check a careful reader would run, and it
        # is why backfilling the named record was never the fix: the writer is
        # live, so the residue regenerates. Refuse at the write, and the backfill
        # becomes a consequence rather than the remedy.
        #
        # Clearing stays open (None / "" / {}) -- that is how the earlier
        # instances were legitimately retired, and refusing it would break the
        # normal unblock path.
        if field == "blocker_ref" and value not in (None, "", {}):
            if not isinstance(value, dict):
                print(
                    f"REFUSED: blocker_ref must be a JSON object, got "
                    f"{type(value).__name__} ({value!r:.120}). A scalar is "
                    f"unreadable by every consumer -- the read-side guards "
                    f"require a dict before any branch acts, so it is silently "
                    f"skipped, and no expires_at can be stored on it so the TTL "
                    f"never arms. Pass the canonical shape, e.g. "
                    f"'{{\"type\": \"partner-response\", \"external_id\": "
                    f"\"<msg-or-goal-id>\"}}' (valid types: "
                    f"{', '.join(BLOCKER_REF_TYPES)}). To CLEAR it, pass null. "
                    f"(g-115-3843)",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Capability-absence advisory (). Deliberately mirrors the
        # user_leg_scope advisory directly above — same chokepoint, same stderr
        # channel, same never-refuse contract. It fires on the DURABLE PROSE
        # fields, because that is where a capability-absence claim actually gets
        # authored; `capability-gate` already hard-blocks the defer_reason subset
        # it can prove, and this covers the phrasing that gate's keyword match
        # misses.
        #
        # WHY AN ADVISORY HERE RATHER THAN A THIRD GATE (measured ,
        # meta/gate-firings.jsonl, 120,219 rows): the framework ALREADY has two
        # capability-absence gates — exhaustive-search-gate (5 firings ever, all
        # noop) and verify-before-assuming-gate (0 firings) — and both DO log to
        # the ledger, so those are real zeros. They never fire because their only
        # call site is an LLM-discretionary step in aspirations-verify Q2, while
        # capability-gate, invoked from code chokepoints like this one, fired
        # 8,530 times over the same corpus. The gap was never detection; it was
        # the absence of a chokepoint call site.
        #
        # stderr is the correct channel HERE specifically because this runs in a
        # Bash-invoked script, whose stderr reaches the model in tool output — a
        # non-blocking PreToolUse hook's stderr does not (guard-1680).
        if field in ("defer_reason", "description", "outcome_note"):
            try:
                from _capability_absence_patterns import advise
                _cap_banner = advise(value, field=field, goal_id=goal_id)
                if _cap_banner:
                    print(_cap_banner, file=sys.stderr)
            except Exception:
                pass  # advisory must never break a durable write

        # Cross-lane / cross-BODY TAKEOVER guard (, ) —
        # MIRROR of the daemon guard in
        # mind_api/src/endpoints/aspirations_write.py update_goal() (first guard
        # in the `=== PR 7i in-lock status guards ===` block). guard-742: this
        # logic lives on BOTH sides. The daemon is the LIVE path for
        # aspirations-update-goal.sh (daemon-only wrapper), but this CLI entry
        # is NOT dead code — the rb-428 sweeps (precondition-defer-recheck,
        # credential-defer-recheck) invoke `aspirations.py update-goal`
        # DIRECTLY as a Python subprocess, bypassing the wrapper. Keep both in
        # sync or the guard is half-applied.
        #
        # THE MIRROR ABOVE ONLY BECAME TRUE ON 2026-08-06 (). From
        #  until then this comment asserted a daemon mirror that did
        # not exist: `_routes_away_from` had exactly one call site in that file,
        # inside claim(). So the ONLY takeover guard in the system sat on the
        # path production never takes, and the wrapper's writes were entirely
        # unguarded. Do not read a "MIRROR of" comment as evidence the mirror is
        # there — grep the other side. That is the whole mechanism of the
        # 2026-08-05 incident: claim() refused a goal and the next update-goal
        # write landed, because only one of the two enforcers existed.
        #
        # claim()/release() enforce intended_agent ownership; update_goal did
        # not — so claiming a foreign goal was refused while
        # `update-goal <foreign> status in-progress` silently took it over.
        #
        # THREE conditions, any one refuses. The SID condition is PRIMARY: a
        # worker Body and its reducer are BOTH `alpha`, so an agent-name
        # comparison is FALSE for the two-body collision and only the session id
        # separates them (foxtrot, 2026-08-06 09:11).
        #
        # SCOPE IS DELIBERATELY NARROW — takeover only (status->in-progress,
        # claimed_by). Do NOT widen to all status writes: the rb-428 sweeps
        # mutate foreign-lane goals BY DESIGN, writing skipped / completed /
        # defer_reason / lastAchievedAt. A blanket cross-lane refusal breaks
        # every one of them (the  over-fix trap). Verified 2026-07-14:
        # the only writer of status->in-progress is aspirations-execute Phase 4
        # (on an already-claimed goal, so already past claim()'s same check),
        # and nothing writes claimed_by through update-goal at all.
        if ((field == "status" and value == "in-progress")
                or field == "claimed_by"):
            _intended = goal.get("intended_agent")
            _caller = os.environ.get("MIND_AGENT", "").strip() or "unknown"
            _req_sid = os.environ.get("MIND_SID", "").strip() or None
            _held_by = goal.get("claimed_by")
            _held_sid = goal.get("claimed_by_sid")

            # MISSING-SID SEMANTICS — the two missing-sid cases are NOT
            # symmetric, and collapsing them is how this guard would end up
            # bypassable. Stated explicitly because the fail direction is a real
            # trade, not an oversight to be rediscovered later.
            #
            #   STORED sid absent (`claimed_by_sid` unset) -> ABSTAIN.
            #     Pre- records legitimately carry no claim sid.
            #     Refusing them would wedge real work to close a hole, so the
            #     sid axis simply does not vote.
            #
            #   REQUEST sid absent while a STORED one exists -> REFUSE.
            #     This is the bypass vector, not an abstention: if the guard
            #     goes quiet whenever the caller omits the sid, then unsetting
            #     MIND_SID defeats it entirely. claim() reached the same
            #     conclusion the hard way — its case 5b (-b) had
            #     previously ALLOWED a no-sid claim, "which left the guard
            #     bypassable by omitting a param".
            #
            # Residual cost, named rather than hidden: when NEITHER side has a
            # sid, a same-agent two-body collision is undetectable here and
            # PASSES. Cross-AGENT collisions need no sid and are still caught by
            # _agent_conflict. The refusal is loud and carries --cross-lane, so
            # a hook-timeout that drops MIND_SID surfaces as a clear message
            # with an escape hatch rather than as silent corruption.
            _sid_conflict = bool(_held_sid and _req_sid
                                 and _held_sid != _req_sid)
            _sid_unprovable = bool(_held_sid and not _req_sid)
            _agent_conflict = bool(_held_by and _held_by != _caller)
            _lane_conflict = routes_away_from(_intended, _caller)

            if (_sid_conflict or _sid_unprovable
                    or _agent_conflict or _lane_conflict):
                _xl = (getattr(args, "cross_lane", None) or "").strip() or None
                if not _xl:
                    # REASON ORDER != CHECK ORDER, deliberately. All three
                    # conditions refuse; this picks which one to NAME. The agent
                    # fact is named first because the sid wording ("two Bodies
                    # of X") is only TRUE when the holder and the caller are the
                    # same agent — on a cross-agent takeover both axes differ,
                    # and naming the sid there would assert a two-body collision
                    # that is not happening, sending the next reader after the
                    # wrong mechanism entirely.
                    if _agent_conflict:
                        _why = (f"claimed by '{_held_by}' but the caller is "
                                f"'{_caller}'")
                    elif _sid_conflict:
                        _why = (f"held by session '{_held_sid}' but this "
                                f"request is session '{_req_sid}' — two "
                                f"Bodies of '{_caller}'")
                    elif _sid_unprovable:
                        _why = (f"held by session '{_held_sid}' but this "
                                f"request carries NO session id, so it cannot "
                                f"be shown to be the same Body of '{_caller}'")
                    else:
                        _why = (f"routed to '{_intended}' but the caller is "
                                f"'{_caller}'")
                    _at = goal.get("claimed_at") or "an unrecorded time"
                    print(f"BLOCKED: Goal {goal_id} is {_why} (claimed at "
                          f"{_at}). "
                          f"Refusing the TAKEOVER write (field={field}). Pass "
                          f"--cross-lane <justification> to override (logged "
                          f"to override-bypass-ledger.jsonl). Non-takeover "
                          f"cross-lane writes (skipped / completed / "
                          f"defer_reason) are unaffected.",
                          file=sys.stderr)
                    sys.exit(1)
                try:
                    from _override_helpers import audit_cross_lane_claim
                    audit_cross_lane_claim(
                        goal_id, _caller,
                        (f"{_held_by or _intended or 'unknown'}@{_held_sid}"
                         if (_sid_conflict or _sid_unprovable)
                         else (_held_by or _intended or "unknown")),
                        _xl,
                        category=goal.get("category"),
                        title=goal.get("title"))
                except Exception as _ae:  # ledger failure must not lose the write
                    print(f"WARN: cross-lane override audit failed: {_ae!r}",
                          file=sys.stderr)

        # Guard: recurring goals must never reach status=completed (LLM drift protection)
        if field == "status" and value == "completed" and goal.get("recurring"):
            print(f"BLOCKED: Cannot set status=completed on recurring goal {goal_id}. "
                  f"Recurring goals stay 'pending'. Use complete-by for cycle tracking, "
                  f"or set recurring=false first to permanently stop it.",
                  file=sys.stderr)
            sys.exit(1)

        # Pre-completion artifact-existence gate ( class, 2026-05-14).
        # Goals titled with action prefixes (Apply/Create/Implement/Add/
        # Build/Wire/Land) that reference concrete file paths in their
        # description must have those files on disk at close time. Catches
        # the failure mode where a goal is marked completed but the artifact
        # was never produced. Canonical incident:  marked complete
        # 2026-05-14 but core/scripts/post-state-update-metric-gate.sh was
        # never committed — 17 stderr failures from iteration-close.sh
        # accumulated before discovery.
        #
        # Override --override-missing-artifact "<reason>" exists for
        # legitimate cases (path was renamed, typo in description, gate
        # regex false-positive) and logs to
        # world/missing-artifact-overrides.jsonl for audit.
        #
        # Runs BEFORE the uncommitted-work gate — order matters: you can't
        # commit what doesn't exist, so detect the missing-artifact case
        # first so the operator fixes the right thing.
        if field == "status" and value == "completed":
            ca_override = getattr(args, "override_missing_artifact", None)
            ca_result = _run_completion_artifact_gate(
                goal_id,
                goal.get("title", ""),
                goal.get("description", ""),
                override=ca_override,
            )
            if ca_result.get("would_block"):
                missing = ca_result.get("missing_artifacts") or []
                near = ca_result.get("near_misses") or {}
                lines = []
                for vp in missing[:10]:
                    hint = near.get(vp)
                    if hint:
                        lines.append(f"  {vp}  (did you mean {hint}?)")
                    else:
                        lines.append(f"  {vp}")
                preview = "\n".join(lines)
                more = (f"\n  ... and {len(missing) - 10} more"
                        if len(missing) > 10 else "")
                print(
                    f"BLOCKED: closing {goal_id} as 'completed' but "
                    f"{len(missing)} artifact path(s) referenced in the "
                    f"description don't exist on disk:\n{preview}{more}\n\n"
                    f"Either:\n"
                    f"  1. Create the missing artifact(s) and retry the close\n"
                    f"  2. Fix a typo in the description (e.g., .json vs "
                    f".jsonl) and retry\n"
                    f"  3. Pass --override-missing-artifact \"<justification>\" "
                    f"if the path was renamed / removed / never required "
                    f"(logged to world/missing-artifact-overrides.jsonl)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if ca_override and (ca_result.get("missing_artifacts") or []):
                print(
                    f"[completion-artifact-gate] "
                    f"--override-missing-artifact on {goal_id}: {ca_override}",
                    file=sys.stderr,
                )

        # Layer-B residual-work gate (; Layer A = Step 8.55 in
        # aspirations-state-update + guard-3601). Refuses status=completed when
        # outcome_note names undone work (the  class — a spec on a
        # COMPLETED record is invisible to every selector) and no LIVE carrier
        # is cited. On block, Layer-D files the suggested successor into the
        # in-memory items and commits it via _write_live_under_lock before
        # exit 1 — mirror of the defer-gate auto-Unblock below. guard-2323 /
        # guard-742: the LIVE path is the daemon mirror in
        # mind_api/src/endpoints/aspirations_write.py (update_goal, in-lock
        # completed branch) — keep both sides in sync.
        if field == "status" and value == "completed":
            from gates.residual_work import (
                evaluate as _residual_work_eval,
                find_existing_successor as _rw_find_existing_successor,
                build_successor_goal as _rw_build_successor,
            )
            rw_override = getattr(args, "override_residual", None)
            # THE OTHER QUEUE — the one that is NOT the --source target
            # (). `items` is whatever LIVE_PATH points at, so this
            # must be selected BY SOURCE: reading the agent queue
            # unconditionally made both arguments identical on a
            # `--source agent` close and the world queue was never loaded,
            # so every world carrier reported live:false / status:null and
            # the gate auto-filed duplicates for work already owned.
            _rw_other_items = None
            if getattr(args, "source", "world") == "agent":
                _rw_other_live = WORLD_DIR / "aspirations.jsonl"
            else:
                _rw_other_live = (
                    AGENT_DIR / "aspirations.jsonl"
                    if AGENT_DIR is not None else None)
            if _rw_other_live is not None and _rw_other_live.exists():
                try:
                    _rw_other_items = read_jsonl(_rw_other_live)
                except Exception:
                    _rw_other_items = None  # fail-open cross-queue
            rw_result = _residual_work_eval(
                goal_id=goal_id,
                outcome_note=str(goal.get("outcome_note") or ""),
                override=rw_override,
                items=items,
                other_items=_rw_other_items,
                world_dir=WORLD_DIR,
                agent_name=(AGENT_DIR.name if AGENT_DIR else ""),
                goal_priority=goal.get("priority"),
                goal_category=goal.get("category"),
            )
            if rw_result.get("would_block"):
                filed_successor_id = None
                rw_filing_status = "not_attempted"
                existing = _rw_find_existing_successor(
                    items, goal_id, _rw_other_items)
                if existing is not None:
                    rw_filing_status = (
                        f"existing successor {existing.get('id')} pending in "
                        f"{existing.get('_aspiration_id')} "
                        f"({existing.get('_source')} queue, strategy="
                        f"{existing.get('_match_strategy')}) — idempotent "
                        f"skip; cite it in outcome_note")
                else:
                    # Target: the original goal's own aspiration (a residual
                    # continues that aspiration's work), else first active.
                    _rw_target = asp
                    if _rw_target is None or _rw_target.get(
                            "status") not in (None, "active"):
                        _rw_target = next(
                            (a for a in items
                             if a.get("status") == "active"), None)
                    if _rw_target is None:
                        rw_filing_status = ("no target aspiration available "
                                            "— filing skipped")
                    else:
                        # Allocate g-NNN-NN under the target — same live +
                        # evicted-id max as _file_unblock_under_existing_lock
                        # ( tombstone awareness).
                        _rw_asp_num = (_rw_target.get("id", "")
                                       .replace("asp-", ""))
                        _rw_max_seq = 0
                        _rw_live_ids = [g.get("id", "")
                                        for g in _rw_target.get("goals", [])]
                        for gid in _rw_live_ids + _all_evicted_ids(_rw_target):
                            m = re.match(r"^g-\d{3}-(\d{2,4})", gid)
                            if m:
                                _rw_max_seq = max(_rw_max_seq,
                                                  int(m.group(1)))
                        _rw_new_id = f"g-{_rw_asp_num}-{_rw_max_seq + 1:02d}"
                        _rw_goal = _rw_build_successor(
                            goal_id, rw_result, _rw_new_id)
                        try:
                            validate_goal(_rw_goal)
                            _rw_target.setdefault("goals", []).append(
                                _rw_goal)
                            recompute_progress(_rw_target)
                            _write_live_under_lock(
                                items,
                                f"residual-gate filed successor "
                                f"{_rw_new_id} for {goal_id}")
                            filed_successor_id = _rw_new_id
                            rw_filing_status = (f"Filed successor "
                                                f"{_rw_new_id} in "
                                                f"{_rw_target.get('id')}")
                        except (ValueError, OSError) as exc:
                            rw_filing_status = f"filing failed: {exc}"
                print(
                    f"BLOCKED: closing {goal_id} as 'completed' but its "
                    f"outcome_note names undone work (markers: "
                    f"{', '.join(rw_result['matched_markers'])}) with no "
                    f"live carrier cited. Residual clause: "
                    f"\"{(rw_result.get('residual_clause') or '')[:160]}\"\n\n"
                    f"Either:\n"
                    f"  1. Cite a live carrier in outcome_note (e.g. "
                    f"'residual carried by "
                    f"{filed_successor_id or 'g-NNN-NN'}') and retry\n"
                    f"  2. Record an explicit owner decline in outcome_note "
                    f"and retry\n"
                    f"  3. Pass --override-residual \"<justification>\" "
                    f"(audited to world/residual-work-overrides.jsonl)\n\n"
                    f"Residual-gate successor routing: {rw_filing_status}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if rw_override and rw_result.get("matched_markers"):
                print(
                    f"[residual-work-gate] --override-residual on "
                    f"{goal_id}: {rw_override}",
                    file=sys.stderr,
                )

        # Pre-completion uncommitted-work gate (, 2026-05-07).
        # Goals close as 'completed' when the file edit lands on disk, but
        # commit/push is described in world/conventions/post-execution.md as
        # a separate step with no chokepoint. Result: framework code accumulates
        # uncommitted in the working tree and the user discovers it days later
        # (5d-stale Processor llm_service.py orphan, 2026-05-07 audit found
        # ~20 dirty framework files in this repo alone).
        #
        # This gate refuses status=completed when framework-code patterns are
        # dirty (regex filters out agent state churn — jsonl/journal/session/
        # COMPLETION-REPORT). Override --override-uncommitted "<reason>" exists
        # for legitimate cases (partner mid-flight on adjacent file, etc.) and
        # logs to world/uncommitted-work-overrides.jsonl for audit.
        #
        # Mirrors the capability-gate.py defer-time gate pattern. See
        # core/scripts/uncommitted-work-gate.py for the gate body.
        if field == "status" and value == "completed":
            override = getattr(args, "override_uncommitted", None)
            gate_result = _run_uncommitted_work_gate(goal_id, override=override)
            if gate_result.get("would_block"):
                dirty = gate_result.get("dirty_framework_files") or []
                preview = "\n".join(f"  {f}" for f in dirty[:10])
                more = (f"\n  ... and {len(dirty) - 10} more"
                        if len(dirty) > 10 else "")
                print(
                    f"BLOCKED: closing {goal_id} as 'completed' would leave "
                    f"{len(dirty)} uncommitted framework code file(s) in the "
                    f"working tree:\n{preview}{more}\n\n"
                    f"Either:\n"
                    f"  1. Commit + push the changes (preferred — see "
                    f"world/conventions/post-execution.md) and retry the close\n"
                    f"  2. Revert the changes if they were accidental, then retry\n"
                    f"  3. Pass --override-uncommitted \"<justification>\" if "
                    f"the dirty files belong to a different goal or partner "
                    f"agent (logged to world/uncommitted-work-overrides.jsonl)",
                    file=sys.stderr,
                )
                sys.exit(1)
            if override:
                # Echo override to stderr for audit trail (mirrors --force-defer
                # and capability-gate's --override-agent-match conventions).
                print(
                    f"[uncommitted-gate] --override-uncommitted on {goal_id}: "
                    f"{override}",
                    file=sys.stderr,
                )

        # Guard: superseded can only be set via `complete --intent-satisfied` evidence gate,
        # never by direct update-goal. This keeps the evidence requirement enforceable.
        if field == "status" and value == "superseded":
            print(f"BLOCKED: Cannot set status=superseded directly on {goal_id}. "
                  f"Pick the route that matches what is true: "
                  f"(1) THE WHOLE ASPIRATION's intent is satisfied -- "
                  f"`aspirations-complete-intent.sh <asp-id>` with intent_satisfaction JSON "
                  f"listing this goal in superseded_goal_ids; note its evidence gate requires "
                  f"every non-recurring goal in the aspiration to be terminal after the "
                  f"supersession, so this route is unavailable for ONE goal in a live "
                  f"aspiration. "
                  f"(2) THIS GOAL ALONE is moot because a sibling shipped its scope -- write "
                  f"the supersession evidence (the sibling's goal id + what it delivered) to "
                  f"outcome_note FIRST, then set status=skipped; that is the order and the "
                  f"status unblock-parent-status-sweep.py::_mark_skipped already uses for the "
                  f"structurally identical case. "
                  f"(3) The work is still WANTED and merely waiting on another goal -- use "
                  f"status=blocked, NOT skipped: skipped is invisible to the blocked-signal "
                  f"sweeps (precheck 0.5b.11/0.5b.12 scan status=blocked), so nothing will "
                  f"resurface it when the dependency lands (guard-1690).",
                  file=sys.stderr)
            sys.exit(1)

        # Defer-time capability gate (rb/probe-before-defer.md).
        # A defer_reason that names an agent-provisionable capability freezes
        # real work for up to defer_reason_timeout_hours (default 120h = ~5 days)
        # on a wrong premise. Check with capability-gate.py before writing.
        # Only fires when setting a non-empty value — clearing (value=None/"")
        # is always allowed so unblock paths don't need the override flag.
        #
        # Structured internal defers (STRUCTURED_DEFER_PREFIXES) bypass the gate:
        # they're machine-written state markers (Circuit breaker / precondition_unmet /
        # blocked_on_dependency), not narrative claims about external signals. Running
        # the gate on them would keyword-collide with forged skills (e.g., "circuit" in
        # "Circuit breaker: …" matches run-test-circuit) and block the framework's own
        # protective mechanisms. DO NOT remove this check without a matching change in
        # aspirations-precheck/SKILL.md Phase 0.5b.4 — the two sites must stay in sync.
        if _is_narrative_defer(field, value):
            override = getattr(args, "force_defer", None)
            # : --override-agent-match is capability-gate's CREATE_BLOCKER
            # bypass, NOT the defer-path one. A user reaching for it here (wrong
            # context, from CREATE_BLOCKER muscle memory) must NOT get the bypass —
            # --force-defer stays the single canonical defer flag. We capture it only
            # to surface an actionable redirect in the BLOCKED message below.
            wrong_defer_flag = getattr(args, "override_agent_match", None)
            gate_result = _run_capability_gate_for_defer(str(value), goal_id)
            if gate_result.get("would_block") and not override:
                matches = gate_result.get("matches") or []
                first = matches[0] if matches else {}
                matched_skill = first.get("skill") or (first.get("row") or "")[:60] or "(unnamed)"
                matched_kw = first.get("matched_keyword", "")

                # : file Unblock goal atomically before refusing the defer.
                # The gate's --suggest-unblock fields carry the title/description.
                # Filing happens INSIDE the existing lock — items is mutated and
                # _write_live_under_lock is called below to commit it. Order is
                # load-bearing: file Unblock FIRST (success or skip), then exit 1.
                # The original goal's defer_reason is NEVER written either way.
                add_goal_status = ""
                items_modified = False
                if gate_result.get("unblock_suggested"):
                    # Pass original_asp so cross-source defers (e.g. world
                    # queue with no ) route the Unblock to the
                    # original goal's parent aspiration instead of failing
                    # silently. Three-strategy fallback in
                    # _file_unblock_under_existing_lock (rb-655).
                    filed_id, add_goal_status = _file_unblock_under_existing_lock(
                        items, goal_id, gate_result, original_asp=asp
                    )
                    if filed_id is not None:
                        items_modified = True
                        # Layer-D telemetry (). Captures defer-time
                        # auto-Unblock filings so the Layer-B (create_blocker
                        # write-time block) vs Layer-D (defer-time auto-route)
                        # split can be quantified from gate-firings.jsonl.
                        # gate_id mirrors the 4-Layer enforcement-pattern
                        # naming under capability-routing-enforcement.
                        _gate_log(
                            "capability-gate-layer-d",
                            "block",
                            trigger_matched=str(first.get("matched_keyword") or ""),
                            payload=goal_id,
                            extra={
                                "filed_unblock_id": filed_id,
                                "original_goal_id": goal_id,
                                "matched_capability": {
                                    "skill": first.get("skill"),
                                    "matched_keyword": first.get("matched_keyword"),
                                    "row": first.get("row"),
                                },
                                "target_aspiration": "asp-" + filed_id.split("-")[1],
                            },
                        )
                else:
                    add_goal_status = "no unblock suggested by gate"

                # Commit the new Unblock goal under the existing lock. Skipped
                # when no goal was filed (idempotent retry, missing target asp,
                # validation error). The defer write itself is never committed
                # — we sys.exit(1) below.
                if items_modified:
                    try:
                        _write_live_under_lock(
                            items,
                            f"defer-gate filed Unblock for {goal_id}: {add_goal_status}"
                        )
                    except Exception as exc:
                        add_goal_status = f"add-goal write failed: {exc}"

                # : if the user reached for --override-agent-match (the
                # CREATE_BLOCKER-context bypass) on this defer, redirect them to the
                # correct defer-path flag instead of leaving them staring at option 3.
                wrong_flag_redirect = ""
                if wrong_defer_flag:
                    wrong_flag_redirect = (
                        f"\nNOTE: you passed --override-agent-match — that flag is the "
                        f"CREATE_BLOCKER-context bypass and does NOT apply to defers. "
                        f"The defer-path bypass is --force-defer (option 3 above); "
                        f"re-run with --force-defer \"<justification>\".\n"
                    )
                print(
                    f"BLOCKED: defer_reason on {goal_id} names an agent-provisionable "
                    f"capability ({matched_skill!r}, keyword {matched_kw!r}). Deferring "
                    f"would freeze the goal for up to defer_reason_timeout_hours on a "
                    f"premise the agent can resolve itself. Either:\n"
                    f"  1. Actually invoke the capability (probe-before-defer.md rule 1), or\n"
                    f"  2. Rewrite the defer_reason to name the genuine external signal, or\n"
                    f"  3. Pass --force-defer \"<justification>\" if this is a real false positive.\n"
                    f"{wrong_flag_redirect}"
                    f"\n"
                    f"Defer-gate Unblock-routing: {add_goal_status}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if override:
                # Echo override to stderr for audit trail (mirrors
                # capability-gate.py and blocker-create-gate.py conventions).
                print(
                    f"[defer-gate] --force-defer override on {goal_id}: {override}",
                    file=sys.stderr,
                )

        # Blocker-ref requirement for narrative defers (Change 1).
        # The capability-gate check above catches defers that name
        # agent-provisionable capabilities. This check catches the second
        # attack surface: narrative laundering where the LLM writes
        # defer_reason="awaiting user feedback" on deep goals to qualify for
        # quiescence. A valid narrative defer MUST cite a structured
        # blocker_ref with a type from BLOCKER_REF_TYPES and an observable
        # external_id the next wake-cycle can probe. Without this, the
        # quiescence gate (Change 2) is trivially gameable.
        #
        # Bypass: --force-unstructured-defer "<justification>" appends to
        # world/blocker-gate-overrides.jsonl for audit (same mechanism as
        # capability-gate's --override-agent-match). An override disqualifies
        # the goal from quiescence eligibility — the quiescence gate rejects
        # any blocked goal without a valid blocker_ref, regardless of how it
        # was deferred.
        #
        # Same null / structured-prefix gating as the capability-gate check
        # above so clears and internal machine-writes pass unchanged. Both
        # gates use _is_narrative_defer() — keeping the predicate in one
        # place prevents drift between them.
        blocker_ref_normalized = None
        if _is_narrative_defer(field, value):
            ref_raw = getattr(args, "blocker_ref", None)
            force_unstructured = getattr(args, "force_unstructured_defer", None)
            if ref_raw:
                ok, parsed = _validate_blocker_ref(ref_raw)
                if not ok:
                    print(
                        f"BLOCKED: --blocker-ref validation failed on {goal_id}: {parsed}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # Door B credential-enumeration check () — RAW arg,
                # not `parsed` (validate() drops the enumeration field).
                _credential_enum_guard(goal_id, ref_raw, value, args)
                blocker_ref_normalized = parsed
            elif force_unstructured:
                _log_unstructured_defer_override(goal_id, value, force_unstructured)
                print(
                    f"[defer-gate] --force-unstructured-defer override on {goal_id}: "
                    f"{force_unstructured}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"BLOCKED: defer_reason on {goal_id} requires a structured "
                    f"blocker_ref. Narrative defers without a typed external-signal "
                    f"reference are the escape hatch the quiescence gate must reject. "
                    f"Either:\n"
                    f"  1. Pass --blocker-ref '{{\"type\":\"<one of {list(BLOCKER_REF_TYPES)}>\","
                    f"\"external_id\":\"<observable id>\"}}', or\n"
                    f"  2. Use a structured defer prefix (precondition_unmet: / "
                    f"blocked_on_dependency / Circuit breaker:), or\n"
                    f"  3. Pass --force-unstructured-defer \"<justification>\" if the "
                    f"external signal genuinely cannot be referenced. Overrides are "
                    f"logged to world/blocker-gate-overrides.jsonl and disqualify the "
                    f"goal from quiescence.",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Defer-target existence advisory (). Runs LAST among the
        # defer gates — after every refusing gate has passed — so a defer that
        # is about to be REFUSED never also emits a warning about its target.
        # Advisory only: it prints and returns, never exits.
        #
        # DELIBERATELY NOT GATED ON _is_narrative_defer, and this is the whole
        # correctness of the check. That predicate returns False for every
        # STRUCTURED_DEFER_PREFIXES defer — including `blocked_on_dependency`
        # and `precondition_unmet:`, which are precisely the defers that carry
        # dependency goal ids. Measured on the live corpus 2026-08-22: of the 79
        # non-terminal defers citing a goal id, **79 were structured and 0 were
        # narrative**, so reusing the narrative predicate here would have fired
        # on zero of the real population while looking correct in review — the
        # guard-1802 class (a predicate narrower than the population it audits
        # reports clean forever). The structured bypass exists so the capability
        # gate's forged-skill KEYWORD scan cannot collide with machine-written
        # markers; this check does no keyword matching, so that rationale does
        # not transfer. Trigger on the field itself.
        if field == "defer_reason" and value not in (None, ""):
            _warn_unresolvable_defer_targets(goal_id, value)

        # Blocker-ref requirement for direct status=blocked writes ().
        # Parallel to the defer_reason gate above. Without this check, the
        # status=blocked path bypasses the blocker_ref requirement entirely —
        # goals enter blocked state with no structural evidence, the quiescence
        # gate can't detect drift, and dependent goals stay blocked long after
        # their actual blocker resolves. Found by felt-sense sweep 2026-04-24:
        #  and  were both status=blocked with blocked_by=[]
        # and no blocker_ref, violating the Blocker Reference Schema.
        #
        # Accepted evidence forms (any one passes the gate):
        #   1. --blocker-ref flag with valid structured payload
        #   2. Existing blocker_ref already on the goal (from prior defer_reason write)
        #   3. blocked_by non-empty (goal-chain dependencies are their own evidence)
        #
        # Only fires when transitioning INTO blocked (current status != blocked) —
        # idempotent re-writes are no-op from the gate's perspective.
        blocker_ref_for_blocked_status = None
        if (
            field == "status"
            and value == "blocked"
            and goal.get("status") != "blocked"
        ):
            has_existing_blocker_ref = goal.get("blocker_ref") is not None
            has_blocked_by = bool(goal.get("blocked_by", []))
            ref_raw = getattr(args, "blocker_ref", None)

            if ref_raw:
                ok, parsed = _validate_blocker_ref(ref_raw)
                if not ok:
                    print(
                        f"BLOCKED: --blocker-ref validation failed on {goal_id}: {parsed}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # Door B credential-enumeration check () — RAW arg.
                _credential_enum_guard(goal_id, ref_raw, "status=blocked", args)
                blocker_ref_for_blocked_status = parsed
            elif not (has_existing_blocker_ref or has_blocked_by):
                print(
                    f"BLOCKED: status=blocked on {goal_id} requires blocker evidence. "
                    f"This goal has no existing blocker_ref and no blocked_by entries. "
                    f"Either:\n"
                    f"  1. Pass --blocker-ref '{{\"type\":\"<one of {list(BLOCKER_REF_TYPES)}>\","
                    f"\"external_id\":\"<observable id>\"}}', or\n"
                    f"  2. Set blocked_by first (non-empty list of blocking goal IDs), or\n"
                    f"  3. Ensure blocker_ref is already populated via a prior defer_reason write.\n"
                    f"Floating-blocked goals violate goal-schemas.md §Blocker Reference Schema "
                    f"and are invisible to the quiescence gate.",
                    file=sys.stderr,
                )
                sys.exit(1)

        # CRITICAL: `field` is treated as a flat top-level key — dotted paths
        # are REJECTED below. Per zeta/reports/-dotted-path-corruption-
        # decision.md (Option A), this guard is applied symmetrically across the
        # six field=value scripts: aspirations.py (here + cmd_meta_update),
        # pipeline.py, reasoning-bank.py, experience.py, spark-questions.py,
        # pattern-signatures.py. Pre-2026-05-10 this site silently created a
        # LITERAL "verification.outcomes" string-key on the goal dict instead
        # of nesting into goal["verification"]["outcomes"], corrupting ,
        # , . The contract is "field is flat; callers pass full
        # nested JSON for nested writes (e.g., field=verification value={"outcomes":
        # [...], "checks": [], "preconditions": []})". For dotted-path navigation,
        # use team-state.py (which has _set_nested + _validate_field_path).
        if "." in field:
            print(
                f"BLOCKED: dotted field name '{field}' is not supported by "
                f"aspirations.py update-goal. This script writes flat top-level "
                f"keys only. To write a nested value, pass the parent field with "
                f"a full nested JSON: --field verification "
                f"--value '{{\"outcomes\": [...], \"checks\": [...]}}'. "
                f"For dotted-path navigation, use team-state.py "
                f"(which has _set_nested + _validate_field_path).",
                file=sys.stderr,
            )
            sys.exit(1)
        # : a DIRECT `blocker_ref` field write used to fall straight
        # through to the generic `goal[field] = value` below with NO validation,
        # NO alias normalization and NO TTL — validate() was reachable only via
        # the --blocker-ref FLAG paired with a defer_reason / status=blocked
        # write. Measured consequence (2026-07-27): of 11 live dict refs, ONE
        # matched validate()'s shape; 7 carried no expires_at at all, so the
        # TTL that exists to force a Phase 0.5b re-probe never armed and those
        # blocks could not age out. Route the direct write through the same
        # validator (guard-330: every write path calls its full-record validator).
        #
        # DICTS ONLY, deliberately. A bare-STRING blocker_ref is a live,
        # reader-supported shape (blocked-signal-resolution-check's `kind ==
        # "str"` branch) and normalizing it is a separate tracked concern
        # (); this goal's contract is that an un-normalized DICT
        # cannot land. A str that decodes to a dict IS in scope — that is just
        # a dict arriving over the CLI's JSON-encoded path.
        if field == "blocker_ref" and value not in (None, ""):
            _candidate = value
            if isinstance(_candidate, str):
                try:
                    _candidate = json.loads(_candidate)
                except (json.JSONDecodeError, TypeError):
                    _candidate = None          # bare string ref — out of scope
            if isinstance(_candidate, dict):
                from gates.blocker_ref import validate as _validate_ref
                _ok, _normalized = _validate_ref(_candidate)
                if not _ok:
                    print(
                        f"BLOCKED: {_normalized}\n"
                        f"Goal: {goal_id}. A direct `blocker_ref` field write is "
                        f"normalized by the same validator as --blocker-ref "
                        f"(g-115-3532) — fix the payload rather than routing "
                        f"around the gate.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                value = _normalized

        # === field-shrink guard () — CLI HALF ===
        # Byte-parallel with the daemon half in
        # mind_api/src/endpoints/aspirations_write.py (guard-547/2323: a
        # normalize/validate change on one writer must land on the other in the
        # SAME change, or the two disagree about what a legal write is). Both
        # import the same gates.field_shrink predicate — the thresholds live in
        # exactly one place.
        #
        # Placed HERE and not in an earlier gate chain because the predicate
        # needs the pre-mutation value, which only exists once `goal` is loaded.
        # Not wrapped in try/except: pure arithmetic, no dependency to fail, and
        # a fail-open handler would also swallow refusal-message construction
        # errors and convert a block into a silent pass (guard-3803).
        _shrink = _field_shrink_eval(field, goal.get(field), value)
        if _shrink["blocked"]:
            _shrink_override = getattr(args, "override_shrink", None)
            _gate_log(
                "field-shrink-guard",
                "override" if _shrink_override else "block",
                caller="cli:cmd_update_goal",
                trigger_matched=_shrink["decision_path"],
                payload=goal_id,
                override_reason=_shrink_override,
                extra={"field": field, "old_len": _shrink["old_len"],
                       "new_len": _shrink["new_len"],
                       "ratio": _shrink["ratio"]},
            )
            if not _shrink_override:
                print(
                    f"BLOCKED: {_shrink['message']}\nGoal: {goal_id}.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            _gate_log(
                "field-shrink-guard", "noop",
                caller="cli:cmd_update_goal",
                trigger_matched=_shrink["decision_path"],
                payload=goal_id,
                extra={"field": field, "old_len": _shrink["old_len"],
                       "new_len": _shrink["new_len"],
                       "ratio": _shrink["ratio"]},
            )

        # CRITICAL: capture BEFORE the mutation below — the  selection_count
        # bump compares old_status vs the new value to stay idempotent on redundant
        # in-progress writes. Moving this read below `goal[field] = value` would
        # break the guard and inflate selection_count on every resume/retry.
        old_status = goal.get("status")
        # : capture pre-update interval_hours BEFORE the write so the
        # anchor-persist cascade below records the ORIGINAL cadence, not the
        # incoming (possibly already-extended) value.
        _prev_interval_hours = goal.get("interval_hours")
        goal[field] = value

        #  (unbounded interval-ratchet fix): persist the cap anchor here,
        # at the single write site EVERY interval_hours path funnels through. The
        # per-goal path (cargo-cult-detector.update_interval_hours) writes the anchor
        # itself, but the BATCH-CALIBRATE and MANUAL apply paths reach interval_hours
        # through this generic chokepoint and never persisted original_interval_hours —
        # so every later auto-extension read orig=None, treated the already-extended
        # value as "original", and the 3x cap ratcheted UNBOUNDED ( root-cause,
        # zeta 2026-07-12). Anchor to the PRE-update cadence; skip a fresh goal's first
        # interval set (_prev None/0) so no spurious anchor is created. When the anchor
        # already exists (e.g. update_interval_hours wrote it first) this is a no-op.
        if (
            field == "interval_hours"
            and goal.get("original_interval_hours") in (None, "")
            and isinstance(_prev_interval_hours, (int, float))
            and not isinstance(_prev_interval_hours, bool)
            and _prev_interval_hours > 0
        ):
            goal["original_interval_hours"] = _prev_interval_hours

        # : RE-BASE the anchor on a DELIBERATE cadence raise.
        #
        # The write-once branch above is correct for the CAP consumer
        # (cargo-cult-detector: proposed = min(interval*multiplier,
        # original*cap_ratio)) — a freely-mutable anchor there is exactly the
        #  unbounded ratchet it was written to stop. But the FLOOR
        # consumer reads the SAME field (contract: floor =
        # original*contract_floor_ratio) and goes stale-LOW when a cadence is
        # deliberately raised: a goal widened 24h -> 168h kept floor =
        # 24*0.33 = 7.92h, so a deep-outcome streak could walk a weekly cadence
        # back toward ~8h. One field, two consumers, opposite requirements.
        #
        # The discriminator needs no new field and no caller flag. An
        # auto-extension is bounded BY CONSTRUCTION at original*cap_ratio, so a
        # write STRICTLY ABOVE that bound provably did not come from one — only
        # a manual or batch cadence edit can land there. Re-basing on exactly
        # those writes keeps the anchor immutable for every automatic path, so
        # the cap cannot ratchet.
        #
        # Measured 2026-08-13 (zeta, hostname cc-02, uname -r 6.8.0-137-generic)
        # over the live world store: of 50 recurring goals carrying both fields,
        # 25 had interval > original and 6 sat ABOVE the cap bound (up to 7.0x
        # —  at 168h vs anchor 24h). Those 6 are unreachable by
        # auto-extension and are the manual-raise population this branch fixes.
        # The 4 sitting EXACTLY at 3.0x are auto-extensions at their cap and are
        # correctly left alone by the strict inequality.
        elif field == "interval_hours":
            _anchor = goal.get("original_interval_hours")
            _new_interval = goal.get("interval_hours")
            if _is_deliberate_raise(_anchor, _new_interval):
                goal["original_interval_hours"] = _new_interval

        # Stamp last_modified on every successful field write. A general
        # last-touched timestamp (originally -a for the stale-read gate,
        # retired ). Single timestamp per cmd_update_goal call
        # — auto-managed cascades below (defer_reason_set_at, blocked_since, blocker_ref,
        # deferred_until) are side-effects of THIS write and share its modification moment.
        # Local system time, no microseconds, matching blocked_since/defer_reason_set_at
        # convention. Backward compat: legacy goals without the field still read fine
        # (consumers must treat absent field as 'unknown', not stale — fail-open).
        goal["last_modified"] = datetime.now().isoformat(timespec="seconds")

        # : bump aspiration-level selection_count + last_selected when a goal
        # transitions INTO in-progress from a non-in-progress state. Surfaces "which
        # aspirations have actually been worked on" — flat aspirations.jsonl alone
        # tells you priority + completion %, but not whether the aspiration has
        # ever been selected since creation. The idempotency guard (old vs new
        # status) prevents inflation on resume/retry writes where in-progress is
        # written redundantly.
        if (
            field == "status"
            and value == "in-progress"
            and old_status != "in-progress"
        ):
            asp["selection_count"] = int(asp.get("selection_count", 0) or 0) + 1
            asp["last_selected"] = datetime.now().isoformat(timespec="seconds")

        #  Layer 1: stamp completed_at when transitioning to a terminal status.
        # The 533-goal completed_at=null gap ( investigation) traces to three
        # writer sites that set status without paired completed_at. This is one of them.
        # Idempotent: only stamps when the field is currently None (preserves legitimate
        # back-stamps from /aspirations-complete-by below or from external backfill).
        if (
            field == "status"
            and value in TERMINAL_GOAL_STATUSES
            and goal.get("completed_at") is None
        ):
            goal["completed_at"] = datetime.now().isoformat(timespec="seconds")

        # : stamp completed_date on the completion transition. The
        # daemon twin (aspirations_write.py cascade 6a) carries the identical
        # logic — port both or the fix is inert under daemon-only (guard-2323).
        # This cascade stamped completed_at above but NOT completed_date, so
        # the field recorded WHICH CLOSE PATH RAN rather than whether the goal
        # completed. 616/4346 completed goals lacked it on 2026-08-10, of which
        # 383 carry completed_by and are the real closes this stamp fixes; most
        # of the remainder are `Maintain:` goals, filed status:completed at
        # creation, which never transition and correctly have no completion
        # date. Every window-filtered lane/compliance metric filters on this
        # field. Scoped to "completed" (a skipped/expired goal has no
        # completion date), idempotent, and DATE-shaped to match the canonical
        # iteration-close stamp and the 95% date-only live majority.
        if (
            field == "status"
            and value == "completed"
            and goal.get("completed_date") is None
        ):
            goal["completed_date"] = datetime.now().strftime("%Y-%m-%d")

        # : stamp completed_by on the completion transition — the
        # completion chokepoint every non-recurring status->completed flows
        # through (recurring is blocked above). Pre-fix only ~11% (174/1609) of
        # completed world goals carried completed_by (the rest closed via this
        # path, which never stamped it), so agent-attribution audits and the
        # cross_queue graduation count () undercounted real output.
        # Scoped to value=="completed" (attribution = who completed it); agent
        # from MIND_AGENT. Idempotent: only when unset, preserving explicit
        # /aspirations-complete-by attribution and external backfill.
        # : `_stamped_completed_by` carries THIS write's decision down to
        # the completed_by_sid stamp below, so the pair lands together or not at
        # all. Both halves were first-wins on their OWN guard, which is coherent
        # per-field and wrong for a pair: on a goal that already carries the name
        # and not the sid, this arm skips while the sid arm fires, filling the
        # empty half from whoever happens to issue the next write. Measured
        # 2026-08-09 over the full store: 4239 completed goals in exactly that
        # state, and 6 of 14 completion-SIDs already carrying more than one
        # completed_by. Leaving the sid absent on those is the intended outcome —
        # "an absent sid beats a wrong one" (aspirations_write.py _completed_by_sid).
        _stamped_completed_by = False
        if field == "status" and value == "completed" and not goal.get("completed_by"):
            _completed_by_agent = os.environ.get("MIND_AGENT", "").strip()
            if _completed_by_agent:
                goal["completed_by"] = _completed_by_agent
                _stamped_completed_by = True

        # Persist blocker_ref alongside defer_reason when one was validated.
        # Store under the canonical key so goal-selector, quiescence-gate, and
        # aspirations-precheck Phase 0.5b all read the same structured payload.
        if field == "defer_reason" and blocker_ref_normalized is not None:
            goal["blocker_ref"] = blocker_ref_normalized

        # Parallel persistence for the status=blocked write path ().
        # When the direct-status gate above validated a new blocker_ref, store
        # it here so the same three readers (goal-selector, quiescence-gate,
        # aspirations-precheck) see consistent payload regardless of which
        # entry point transitioned the goal to blocked.
        if (
            field == "status"
            and value == "blocked"
            and blocker_ref_for_blocked_status is not None
        ):
            goal["blocker_ref"] = blocker_ref_for_blocked_status

        # Auto-manage blocked_since when transitioning to status=blocked.
        # Matches the blocked_by auto-management pattern below — blocked_since
        # is the "how long has this been blocked?" signal that proactive
        # escalation and defer-recheck both consume. Without this, direct
        # status=blocked writes leave blocked_since null, and the age-based
        # sweeps miss the goal entirely.
        if field == "status" and value == "blocked" and not goal.get("blocked_since"):
            goal["blocked_since"] = datetime.now().isoformat(timespec="seconds")

        # Auto-manage defer_reason_set_at timestamp alongside defer_reason.
        # Matches the blocked_since auto-management pattern below. The timestamp
        # is what defer_reason_timeout_hours measures against — required for the
        # fail-open expiry in goal-selector.py (see goal-schemas.md:286).
        # Null check MUST match the gate's null check above (value not in (None, ""))
        # so empty-string clears treat both sites consistently.
        if field == "defer_reason":
            if value not in (None, ""):
                goal["defer_reason_set_at"] = datetime.now().isoformat(timespec="seconds")
                # Auto-pair narrative defer_reason with structured deferred_until.
                # Closes the gap where callers wrote "Not before 2026-07-14"
                # into prose without setting the structured time gate, leaving
                # the goal-selector and aspirations-precheck re-probe sweep
                # unable to act on the implied date mechanically. Skips when
                # deferred_until is already set (caller-supplied wins).
                if not goal.get("deferred_until"):
                    extracted = _extract_defer_date(str(value))
                    if extracted.get("matched"):
                        goal["deferred_until"] = extracted["deferred_until"]
                        _log_defer_date_extraction(goal_id, value, extracted)
            else:
                goal["defer_reason_set_at"] = None
                # Clearing defer_reason drops its structured companion.
                # Keep the pair consistent so goal-selector and quiescence-gate
                # never see an orphan blocker_ref on an un-deferred goal.
                goal.pop("blocker_ref", None)
        # CRITICAL — root-cause fix for the recurring-shape-leak bug. Do NOT remove this
        # cascade or move it to a caller. When recurring flips to falsy, interval_hours
        # and lastAchievedAt MUST drop here, at the data primitive, so any future caller
        # of update-goal recurring=false gets the right behavior automatically. The
        # archive-sweep at line ~1057 is a SAFETY NET, not the fix — it un-sticks the
        # goal but cannot retroactively prevent the goal-selector
        # (`hours_since(lastAchievedAt) < interval_hours` filter at goal-selector.py
        # line ~432) from treating the dead goal as "not yet due" between sweeps.
        # History fields (achievedCount, currentStreak, longestStreak) are preserved as
        # factual record. See plan improve-recurring-goals-kind-yao.md.
        if field == "recurring" and not value:
            goal.pop("interval_hours", None)
            goal.pop("lastAchievedAt", None)
        # Auto-manage blocked_since timestamp alongside blocked_by.
        # parse_value() already converted "[]" → [] so `if value` is sufficient.
        if field == "blocked_by":
            if value:
                if not goal.get("blocked_since"):
                    goal["blocked_since"] = datetime.now().isoformat(timespec="seconds")
            else:
                goal["blocked_since"] = None
        # Clear blocked_by refs when goal reaches a terminal status.
        # Claim-clearing invariant (convention Rule 3): terminal transition clears claim.
        # Keyed off TERMINAL_GOAL_STATUSES so any future terminal status auto-enrolls.
        if field == "status" and value in TERMINAL_GOAL_STATUSES:
            _clear_stale_blockers(items, {goal_id})
            goal.pop("claimed_by", None)
            goal.pop("claimed_at", None)
            # The claim is a TRIPLE — the sid clears with the pair ().
            # claimed_by_sid postdates this code () and was never
            # propagated here: before this line the file contained ZERO
            # occurrences of the field, so a terminal transition through THIS
            # door left an orphaned sid on an unclaimed goal. The daemon
            # endpoint is the other door; a fix wired into only one door is
            # inert on the other (the shape test_credential_enum_both_doors.py
            # exists to police).
            #
            # This sentence read "already paired it at four of its five sites"
            # until 2026-08-22. Present-tense, that says one daemon site is
            # UNPAIRED, and it sent a  investigation to hunt it.
            # Re-measured that day (zeta, cc-02, uname -r 6.8.0-137-generic) by
            # mapping every claimed_by pop in aspirations_write.py to its
            # enclosing def and its sid partner: ALL FIVE ARE PAIRED —
            # L3126 update_goal/L3176, L4239 complete_by/L4248,
            # L4306 complete_by/L4312, L4599 release/L4606,
            # L6892 clear_stale_claims/L6894. "Four of five" was a true
            # description of a state that has since been fixed; left in the
            # present tense it reads as a live defect report. Do not re-derive
            # the hunt from this comment — verify against the file.
            #
            #  fix set B part 2: preserve WHICH BODY closed it before
            # the sid is popped, so the completing body stays forensically
            # recoverable. Prefer this process's own MIND_SID — here env IS
            # correct, because the CLI process IS the session (the daemon
            # sibling must NOT read env: it is long-lived and carries its
            # SPAWNER's sid, see _completed_by_sid there). Same env-vs-ctx
            # asymmetry as `completed_by` (). Fall back to the
            # claim's sid for an un-hooked launch with no MIND_SID.
            # Order is load-bearing: compute BEFORE the pop.
            #
            # : scope AND idempotency mirror the `completed_by` stamp
            # above exactly — value=="completed", assign only when unset. The
            # stamp shipped keyed off the whole terminal set and assigning
            # unconditionally, so a reopened-and-re-completed goal carried one
            # completion's agent beside another's session, and a SKIPPED goal
            # carried a field named completed_by_sid with no completed_by beside
            # it. completed_at / completed_by / completed_by_sid are now one
            # coherent first-wins triple. The pop stays unconditional: the claim
            # triple clears at EVERY terminal transition (guard-151).
            #
            # : "one coherent first-wins triple" was true field-by-field
            # and false as a PAIR — each half guarded itself, so a write could
            # fill one and skip the other. `_stamped_completed_by` is the join:
            # the sid stamps only on the write that also stamped the name. Note
            # this is deliberately stricter than "completed_by was unset" — an
            # unset name with no resolvable agent stamps nothing, and that must
            # not license a sid, because a sid with no name beside it is exactly
            # the shape  was filed to remove.
            if (value == "completed" and _stamped_completed_by
                    and not goal.get("completed_by_sid")):
                _cbs = (os.environ.get("MIND_SID", "").strip()
                        or goal.get("claimed_by_sid"))
                if _cbs:
                    goal["completed_by_sid"] = _cbs
            goal.pop("claimed_by_sid", None)
        recompute_progress(asp)
        items[asp_idx] = asp
        _write_live_under_lock(items, f"update-goal {goal_id} {field}")
    finally:
        release_lock(lock_path)
    # E9: goal-skipped/expired encoding. Fires AFTER the lock releases so the
    # WM write doesn't hold aspirations.jsonl. Skip rationale is sometimes
    # tree-worthy ("X became moot because Y replaced it"); without this hook
    # the rationale dies on the goal record alone.
    if field == "status" and value in ("skipped", "expired"):
        _emit_e9_skip_observation(goal_id, value, goal)
    # Print the updated goal, not its containing aspiration.
    # Matches cmd_add_goal and cmd_claim. Do NOT change to `asp` — see rb-336.
    print(json.dumps(goal, indent=2, ensure_ascii=False))

def _emit_e9_skip_observation(goal_id, new_status, goal):
    """Append a sensory_buffer observation when a goal flips to skipped/expired.

    Routes the skip rationale through the standard encoding pipeline
    (consolidation Step 2 / state-update Phase 8). Fail-open: any error logs
    to stderr but never blocks the status-change return path. See encoding-
    triggers.md E9.
    """
    title = goal.get("title", "")
    desc = goal.get("description", "")
    # `outcome_note` is IN this chain because it is where the skip rationale
    # actually lands. `skip_reason` is not a field any writer sets — measured
    # 2026-08-16 (bravo, cc-05, ): the key is structurally ABSENT from
    # every goal record, so with only skip_reason+defer_reason the fallback
    # fired on 100% of skips and the buffer read "Reason: no reason given" for
    # five goals carrying 85-3316 chars of recorded rationale each. That is a
    # manufactured false alarm: it reads as a hygiene problem ("5 goals skipped
    # blind") when the true count is zero, which trains the reader to discount
    # the buffer. rb-245 class — a zero produced by reading a field the store
    # does not carry. `pending-questions-sweep.py` already reads
    # ("outcome_note", "completion_note", "skip_reason") for the same question,
    # so the correct precedence was already established one file over.
    # ORDER: outcome_note outranks defer_reason because on a SKIPPED goal the
    # defer is a stale leftover from before the skip decision, while
    # outcome_note IS the skip decision. Reachable, not inert: this function
    # fires on the STATUS write, and the framework's own documented order is
    # "outcome_note FIRST, then set status=skipped" (see the superseded-status
    # guard above, ~L2017; agent-watchdog.py writes the pair in exactly that
    # order), so the note is on the record by the time we read it here.
    # `str(...)` and `.strip()` are both load-bearing, and a fresh-eyes probe
    # (, same goal that added this line) found all three cases the
    # bare `(x or "")[:300]` form got wrong — every one INTRODUCED by the
    # subscript, since the pre-fix chain had none:
    #   dict  -> `KeyError: slice(None, 300, None)`. Raised OUTSIDE the try
    #            below and from an unguarded call site, so a hook documented
    #            "fail-open, never blocks the status-change return path" failed
    #            CLOSED, after the status write had already committed.
    #   list  -> returned a LIST, which the f-string then embedded as
    #            "Reason: ['a', 'b']".
    #   "  \n" -> whitespace is truthy, so it short-circuited PAST
    #            defer_reason and the fallback: "Reason:    ." — strictly worse
    #            than the "no reason given" it was meant to replace.
    # Keep `outcome_note` INSIDE this parenthesized chain: the two-copy sync
    # pin in test_e9_skip_observation_reason.py asserts on the chain's own
    # text, so hoisting the lookup to a local would silently defeat it.
    skip_reason = (goal.get("skip_reason")
                   or str(goal.get("outcome_note") or "").strip()[:300]
                   or goal.get("defer_reason")
                   or "no reason given")
    # Skip trivial / mechanical goals — no encoding value.
    if len(desc) < 40 and len(title) < 30:
        return
    observation = (
        f"Goal {new_status}: {title}. Reason: {skip_reason}. "
        f"Description: {desc[:400]}"
    )
    payload = {
        "source_goal": goal_id,
        "observation": observation,
        "encoding_score": 0.0,
        "scores": {
            "novelty": 0.3,
            "outcome_impact": 0.2,
            "surprise": 0.4,
            "goal_relevance": 0.5,
            "repetition_strength": 0.1,
        },
        "target_article": None,
        "replay_priority": "routine_observations",
    }
    try:
        # DO NOT switch this to `bash core/scripts/wm-append.sh`. The bash-
        # wrapper invocation breaks on Windows when Python subprocess passes
        # a Windows-style path through bash — backslashes get eaten and the
        # script-not-found error swallows the write silently. Use sys.executable
        # + wm.py directly. Same pattern as the goal-duplication-gate and
        # capability-gate subprocess calls elsewhere in this file.
        subprocess.run(
            [sys.executable, str(CORE_ROOT / "scripts" / "wm.py"),
             "append", "sensory_buffer"],
            input=json.dumps(payload),
            capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        # Fail-open: never block status change on encoding-hook failure.
        print(f"[aspirations update-goal] WARN: E9 skip-encoding failed: {e}",
              file=sys.stderr)

def _clear_stale_blockers(items, resolved_goal_ids):
    """Remove blocked_by references to goals that are resolved (completed/archived/terminal).

    Called from: cmd_complete/cmd_retire/cmd_archive_sweep (archival),
    cmd_complete_by (goal completion), cmd_update_goal (terminal status).
    """
    for asp in items:
        for goal in asp.get("goals", []):
            bb = goal.get("blocked_by", [])
            if isinstance(bb, str):
                bb = [bb]
            if bb:
                cleaned = [b for b in bb if b not in resolved_goal_ids]
                if len(cleaned) != len(bb):
                    goal["blocked_by"] = cleaned
                    if not cleaned:
                        goal["blocked_since"] = None

def _load_intent_satisfaction_config():
    """Load intent_satisfaction config block from core/config/aspirations.yaml.

    Single source of truth — fails loud if yaml missing, unparseable, or
    lacking the intent_satisfaction block. No hidden defaults.
    """
    import yaml
    cfg_path = CONFIG_DIR / "aspirations.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg["intent_satisfaction"]

def _motivation_tokens(text):
    """Lowercase alphanumeric tokens of >=4 chars from a string. Used for rationale overlap gate."""
    if not text:
        return set()
    return {t for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if len(t) >= 4}

def _validate_intent_satisfaction(asp, intent_block, config):
    """Validate an intent_satisfaction block against an aspiration.

    Returns (ok, error_message). Does NOT mutate the aspiration.

    Enforces:
      1. Evidence cardinality: len >= max(scope_min, min(ceil(0.5 * total_non_recurring),
         qualifying)) where qualifying = the goals rule 2 below can actually accept
      2. Evidence quality: all evidence goals exist in this asp, are status=completed,
         non-recurring, and have non-empty verification.outcomes
      3. Superseded goals exist in this asp, are non-recurring, and are not already terminal
      4. No goal appears in both evidence and superseded lists
      5. Rationale is >=40 chars and shares >=1 token (>=4 chars) with motivation
      6. After supersession, every non-recurring goal would be terminal
    """
    import math
    required_keys = {"evidence_goal_ids", "rationale", "superseded_goal_ids"}
    missing = required_keys - set(intent_block.keys())
    if missing:
        return False, f"intent_satisfaction missing required keys: {sorted(missing)}"

    ev_ids = intent_block.get("evidence_goal_ids") or []
    sup_ids = intent_block.get("superseded_goal_ids") or []
    rationale = intent_block.get("rationale") or ""

    if not isinstance(ev_ids, list) or not isinstance(sup_ids, list):
        return False, "evidence_goal_ids and superseded_goal_ids must be lists"

    # Cross-contamination check
    overlap = set(ev_ids) & set(sup_ids)
    if overlap:
        return False, f"goals cannot be both evidence and superseded: {sorted(overlap)}"

    # Build goal lookup for this aspiration only
    goals_by_id = {g.get("id"): g for g in asp.get("goals", [])}
    non_recurring = [g for g in asp.get("goals", []) if not g.get("recurring")]

    # Evidence cardinality — scope-aware floor (direct access; missing scope = broken config, fail loud)
    scope = asp.get("scope", "project")
    scope_min = config["min_evidence_by_scope"][scope]
    # The ceiling is capped by the QUALIFYING pool, not the raw non-recurring count
    # (). The quality loop below accepts ONLY completed, non-recurring goals
    # carrying verification.outcomes, so demanding ceil(0.5 * ALL non-recurring) made
    # the gate mathematically unsatisfiable whenever outcome coverage fell below 50%:
    # no evidence set could satisfy both halves, and the caller bounced between
    # "requires >=37" and "goal X has no verification.outcomes" indefinitely. Measured
    # on ZDS  — 73 non-recurring, threshold 37, only 30 goals carrying outcomes,
    # refused twice for real before the arithmetic was identified as the cause.
    #
    # min() is strictly STRONGER than relaxing to half the qualifying pool: it demands
    # EVERY available piece of honest evidence (30 of 30 on ), and where
    # coverage is healthy (qualifying >= ceil) it is a no-op. So the anti-thin-evidence
    # intent is preserved rather than traded away. Sparse outcome coverage is scattered
    # across an aspiration's whole life, not a legacy era — a date-based grandfather
    # clause would not have worked.
    qualifying = [g for g in non_recurring
                  if g.get("status") == "completed"
                  and ((g.get("verification") or {}).get("outcomes") or [])]
    required = max(scope_min, min(math.ceil(0.5 * len(non_recurring)), len(qualifying)))
    if len(ev_ids) < required:
        if len(qualifying) < scope_min:
            # Still unsatisfiable — but HONESTLY so, and scope_min stays a hard floor
            # on purpose: an aspiration with fewer qualifying goals than the floor has
            # genuinely thin evidence and should not be intent-closed. Say so outright
            # instead of letting the caller discover it by bouncing off the quality
            # loop one id at a time, which is the dead end this whole fix is about.
            return False, (f"aspiration {asp.get('id')} cannot be intent-closed: only "
                           f"{len(qualifying)} of {len(non_recurring)} non-recurring goals are "
                           f"completed with verification.outcomes, below the scope={scope} floor of "
                           f"{scope_min}. Supplying more evidence_goal_ids cannot satisfy this. "
                           f"Reachable exits: retire it (aspirations-retire.sh), or make every "
                           f"non-recurring goal terminal and close it normally "
                           f"(aspirations-complete.sh).")
        return False, (f"evidence_goal_ids has {len(ev_ids)}, scope={scope} requires "
                       f">={required} (max of {scope_min}-by-scope and min(ceil(0.5 * "
                       f"{len(non_recurring)} non-recurring), {len(qualifying)} qualifying))")

    # Evidence quality — each evidence goal must be in-asp, non-recurring, completed, with outcomes
    for gid in ev_ids:
        g = goals_by_id.get(gid)
        if g is None:
            return False, f"evidence goal {gid} not in aspiration {asp.get('id')}"
        if g.get("recurring"):
            return False, f"evidence goal {gid} is recurring; cannot count toward intent satisfaction"
        if g.get("status") != "completed":
            return False, f"evidence goal {gid} has status={g.get('status')}; must be completed"
        outcomes = (g.get("verification") or {}).get("outcomes") or []
        if not outcomes:
            return False, f"evidence goal {gid} has no verification.outcomes; cannot serve as intent evidence"

    # Superseded goals — must exist, be non-recurring, be not-yet-terminal
    for gid in sup_ids:
        g = goals_by_id.get(gid)
        if g is None:
            return False, f"superseded goal {gid} not in aspiration {asp.get('id')}"
        if g.get("recurring"):
            return False, f"superseded goal {gid} is recurring; recurring goals cannot be superseded"
        if g.get("status") in TERMINAL_GOAL_STATUSES:
            return False, f"superseded goal {gid} already terminal (status={g.get('status')})"

    # After supersession, every non-recurring goal must be in terminal status
    sup_set = set(sup_ids)
    remaining_unfinished = [g for g in non_recurring
                            if g.get("status") not in TERMINAL_GOAL_STATUSES
                            and g.get("id") not in sup_set]
    if remaining_unfinished:
        ids = ", ".join(g.get("id", "?") for g in remaining_unfinished)
        return False, (f"after supersession, these non-recurring goals would still be unfinished: {ids}. "
                       f"Add them to superseded_goal_ids or complete them first.")

    # Rationale length
    if len(rationale) < 40:
        return False, f"rationale too short ({len(rationale)} chars); need >=40 explaining how motivation was met"

    # Motivation must exist — the entire intent-satisfaction pathway is anchored to
    # `motivation`. An aspiration without one cannot be intent-closed; retire or complete normally.
    motivation = asp.get("motivation") or ""
    mtokens = _motivation_tokens(motivation)
    if not mtokens:
        return False, (f"aspiration {asp.get('id')} has no motivation (or motivation has no >=4-char tokens); "
                       f"intent-satisfaction requires a non-empty motivation to verify rationale against. "
                       f"Use --force or aspirations-retire.sh instead.")

    # Rationale-motivation token overlap
    rtokens = _motivation_tokens(rationale)
    if not (mtokens & rtokens):
        return False, (f"rationale shares no tokens with motivation; quote the motivation text explicitly. "
                       f"Motivation tokens: {sorted(mtokens)[:10]}...")

    return True, None

def cmd_recompute_all_progress(args):
    """Recompute progress.total_goals for every aspiration in a JSONL file."""
    path = Path(args.path)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    items = read_jsonl(path)
    for asp in items:
        recompute_progress(asp)
    write_jsonl(path, items)
    print(f"Recomputed progress for {len(items)} aspiration(s)")

def cmd_evolution_append(args):
    if sys.stdin.isatty():
        print("Error: expected JSON on stdin (not a terminal)", file=sys.stderr)
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        print("No input provided on stdin", file=sys.stderr)
        sys.exit(1)
    try:
        evt = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_evolution_event(evt)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)

    append_jsonl(EVOLUTION_PATH, evt)
    print(json.dumps(evt, indent=2, ensure_ascii=False))

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aspiration lifecycle engine")
    # WORLD_AGENT_ONLY: cross-agent execution routes via the MIND_AGENT env
    # override ( Option 3), never by widening this enum.
    parser.add_argument("--source", choices=["world", "agent"], default="world",
                        help="Which aspiration queue to operate on (default: world)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # update-asp-field — single-field aspiration update without full re-validation.
    # Used for additive metadata (e.g. chronic_friction). LifingPolls item 8.
    p_uaf = subparsers.add_parser("update-asp-field",
                                   help="Update a single field on an aspiration")
    p_uaf.add_argument("asp_id", type=str, help="Aspiration ID")
    p_uaf.add_argument("field", type=str, help="Field to update")
    p_uaf.add_argument("value", type=str, help="New value (JSON-parseable)")

    # update-goal
    p_ug = subparsers.add_parser("update-goal", help="Update a single goal field")
    p_ug.add_argument("goal_id", type=str, help="Goal ID")
    p_ug.add_argument("field", type=str, help="Field to update")
    p_ug.add_argument("value", type=str, help="New value")
    # : the goal-field allowlist bypass. Deliberately shaped like
    # --force-defer above (justification-bearing, echoed for audit) rather than a
    # bare boolean: a genuinely new field should be a decision someone can read
    # back later, not a flag someone reaches for to make an error message stop.
    p_ug.add_argument("--allow-new-field", default=None,
                      help="Justification for writing a goal field that is not in "
                           "_goal_fields.GOAL_KNOWN_FIELDS. Register the field in "
                           "core/scripts/_goal_fields.py in the same change that "
                           "ships its writer. Echoed to stderr for auditability.")
    # --force-defer is the defer-time analogue of blocker-create-gate's
    # --override-blocker-gate and capability-gate's --override-agent-match.
    # Required to bypass the capability-gate check when field == defer_reason.
    # The justification is echoed to stderr (audit trail); no ledger file yet
    # — if override usage grows, wire one up mirroring blocker-gate-overrides.jsonl.
    p_ug.add_argument("--force-defer", default=None,
                      help="Justification for deferring a goal whose defer_reason "
                           "names an agent-provisionable capability. Required to "
                           "bypass the defer-time capability gate. Echoed to stderr "
                           "for auditability. See .claude/rules/probe-before-defer.md.")
    # --override-agent-match is capability-gate.py's CREATE_BLOCKER-context bypass
    # (participants:[user] routing), NOT the defer-path bypass — the defer analogue
    # is --force-defer above (deliberate one-flag-per-context design; the two are
    # documented as analogues in each other's help). It is accepted HERE only so a
    # user reaching for it on a defer (from CREATE_BLOCKER muscle memory) gets a
    # clear redirect to --force-defer in the BLOCKED message instead of a cryptic
    # argparse "unrecognized arguments" error (). It does NOT honor the
    # bypass — --force-defer stays the single canonical defer flag.
    p_ug.add_argument("--override-agent-match", dest="override_agent_match",
                      default=None,
                      help="Wrong-context flag on the defer path: this is the "
                           "CREATE_BLOCKER bypass. Recognized here only to redirect "
                           "you to --force-defer (the defer-path bypass). Does NOT "
                           "apply a defer override. See g-115-2814.")
    # --cross-lane is the takeover-time analogue of --force-defer ().
    # Required to bypass the cross-lane TAKEOVER guard when the write is
    # status->in-progress or claimed_by on a goal whose intended_agent names a
    # DIFFERENT agent. Logged to world/override-bypass-ledger.jsonl via
    # _override_helpers.audit_cross_lane_claim — the same ledger + schema
    # claim() writes, so cross-lane takeovers and cross-lane claims are one
    # analyzable stream. Non-takeover cross-lane writes (skipped / completed /
    # defer_reason) are NOT gated and need no flag — the rb-428 maintenance
    # sweeps depend on that.
    p_ug.add_argument("--cross-lane", dest="cross_lane", default=None,
                      help="Justification for taking over a goal routed to a "
                           "different agent (status->in-progress or "
                           "claimed_by). Required to bypass the cross-lane "
                           "takeover guard. Logged to override-bypass-ledger"
                           ".jsonl.")
    # --override-uncommitted is the close-time analogue of --force-defer.
    # Required to bypass the pre-completion uncommitted-work gate when
    # framework-code files are dirty in the working tree at status=completed.
    # Logged to world/uncommitted-work-overrides.jsonl for audit. See
    # core/scripts/uncommitted-work-gate.py and .
    p_ug.add_argument("--override-uncommitted", dest="override_uncommitted",
                      default=None,
                      help="Justification for closing a goal as 'completed' "
                           "when framework-code files are uncommitted in the "
                           "working tree. Required to bypass the pre-completion "
                           "uncommitted-work gate. Use only when the dirty files "
                           "belong to a different goal or partner agent that the "
                           "current agent is not authorized to commit. Logged "
                           "to world/uncommitted-work-overrides.jsonl.")
    # --override-missing-artifact is the close-time analogue to --override-uncommitted
    # for the artifact-existence gate. Required when an action-prefix goal
    # references file paths in its description that don't exist on disk
    # (canonical incident: , 2026-05-14 — script declared in
    # description but never committed). See goal-completion-artifact-gate.py.
    p_ug.add_argument("--override-missing-artifact",
                      dest="override_missing_artifact",
                      default=None,
                      help="Justification for closing a goal as 'completed' "
                           "when artifact paths referenced in the description "
                           "don't exist on disk. Required to bypass the pre-"
                           "completion artifact-existence gate. Use only when "
                           "the path was renamed / removed / a typo in the "
                           "description (e.g., .json vs .jsonl). Logged to "
                           "world/missing-artifact-overrides.jsonl.")
    # --override-residual is the close-time bypass for the Layer-B
    # residual-work gate (): outcome_note names undone work but no
    # live carrier is cited. Use only for genuine marker false-positives
    # (e.g. the note QUOTES residual vocabulary while all work was done).
    p_ug.add_argument("--override-residual", dest="override_residual",
                      default=None,
                      help="Justification for closing a goal as 'completed' "
                           "when its outcome_note matches residual-work "
                           "markers with no live carrier cited. Bypasses the "
                           "residual-work gate; logged to "
                           "world/residual-work-overrides.jsonl.")
    # --override-shrink is the bypass for the field-shrink guard ():
    # a write that would shrink `description` / `outcome_note` to under 25% of
    # its current size, when it currently exceeds 2000 chars. Use only for a
    # DELIBERATE condense — the common cause is a read-modify-write against a
    # truncated read, or a wholesale REPLACE where an append was intended.
    p_ug.add_argument("--override-shrink", dest="override_shrink",
                      default=None,
                      help="Justification for a write that shrinks a long "
                           "description/outcome_note to under a quarter of its "
                           "current length. Bypasses the field-shrink guard; "
                           "logged to meta/gate-firings.jsonl as an override.")
    # --blocker-ref is the structured companion to a narrative defer_reason.
    # Required for any non-null, non-structured-prefix defer_reason write so
    # the quiescence gate can distinguish genuine external gating from
    # narrative laundering. Schema: {type, external_id, [state_hash],
    # [created_at], [expires_at]}. See core/config/conventions/goal-schemas.md
    # "Blocker Reference Schema" and BLOCKER_REF_TYPES in this file.
    p_ug.add_argument("--blocker-ref", dest="blocker_ref", default=None,
                      help="JSON object identifying the external signal blocking this "
                           "goal. Required whenever setting a narrative defer_reason. "
                           "Minimum shape: "
                           "'{\"type\":\"<one of BLOCKER_REF_TYPES>\","
                           "\"external_id\":\"<observable id>\"}'. created_at and "
                           "expires_at auto-populate from BLOCKER_REF_TTL_HOURS if "
                           "omitted. See core/config/conventions/goal-schemas.md.")
    # --force-unstructured-defer is the blocker-ref override. Logs to
    # world/blocker-gate-overrides.jsonl (same ledger as --override-blocker-gate
    # and --override-agent-match). Overrides disqualify the goal from quiescence
    # — the quiescence gate rejects any blocked goal without a valid blocker_ref.
    p_ug.add_argument("--force-unstructured-defer",
                      dest="force_unstructured_defer", default=None,
                      help="Justification for writing a narrative defer_reason without "
                           "a structured blocker_ref. Appends to "
                           "world/blocker-gate-overrides.jsonl. Use only when the "
                           "external signal genuinely cannot be referenced by ID. "
                           "Overrides disqualify the goal from quiescence eligibility.")
    # --override-blocker-gate bypasses the credential-enumeration check on a
    # credentials-required blocker_ref (). Same flag name and same
    # ledger as blocker-create-gate.py's override — one vocabulary, both doors.
    p_ug.add_argument("--override-blocker-gate",
                      dest="override_blocker_gate", default=None,
                      help="Justification for writing a credentials-required "
                           "blocker_ref that fails the credential-enumeration "
                           "check. Appends to world/blocker-gate-overrides.jsonl. "
                           "Use only for a genuine false positive — the check "
                           "exists because g-335-210 sat 90h on an unproven "
                           "human-credential assertion.")

    # recompute-all-progress
    p_recompute = subparsers.add_parser("recompute-all-progress", help="Recompute progress for all aspirations in a JSONL file")
    p_recompute.add_argument("path", type=str, help="Path to JSONL file")

    # evolution-append
    subparsers.add_parser("evolution-append", help="Append evolution event from stdin JSON")

    args = parser.parse_args()

    # Override paths for agent source
    global LIVE_PATH, ARCHIVE_PATH, META_PATH
    if args.source == "agent":
        if AGENT_DIR is None:
            print("Error: MIND_AGENT not set — cannot use --source agent", file=sys.stderr)
            sys.exit(1)
        LIVE_PATH = AGENT_DIR / "aspirations.jsonl"
        ARCHIVE_PATH = AGENT_DIR / "aspirations-archive.jsonl"
        META_PATH = AGENT_DIR / "aspirations-meta.json"

    dispatch = {
        "update-asp-field": cmd_update_asp_field,
        "update-goal": cmd_update_goal,
        "recompute-all-progress": cmd_recompute_all_progress,
        "evolution-append": cmd_evolution_append,
    }

    try:
        dispatch[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
