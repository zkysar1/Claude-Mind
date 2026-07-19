"""POST /v1/aspirations/add-goal?asp_id=<a>  and  POST /v1/aspirations/update-goal

Writer endpoints over the daemon write infrastructure (file_locks +
history + changelog) PLUS in-process invocation of the extracted gates.

Pipeline (mirrors aspirations.py cmd_add_goal order):

  add-goal:
    Advisories (warn-only, surfaced in `warnings` array on 200 responses):
      1. user_leg_scope         (gates.user_leg_scope.evaluate)
      2. description_length     (gates.description_length.evaluate) +
                                 telemetry to META_DIR
    Mutators (run before any blocker that might fire on the field):
      3. category-suggest       (gates.category_suggest.evaluate) — sets
                                 goal.category if absent/uncategorized
      4. work_class resolver    (_work_class.resolve) — sets goal.work_class
                                 from goal.category if absent
    Blockers (return 400 on block; ordered to fail-fast before slow I/O):
      5. origin-signal-gate     (gates.origin_signal.evaluate) — blocks when
                                 agent-sourced and missing/invalid signal.
                                 Layer-D auto-derive may patch goal.origin_signal.
                                 Override: X-Mind-Override-Signal.
    Mutator (uses fields the origin-signal gate may have just patched):
      6. capability-route-gate  (gates.capability_route.evaluate) — sets
                                 goal.intended_agent if absent. Override:
                                 X-Mind-Route-To explicit assignment.
    Blockers (continue):
      7. goal-duplication-gate  (gates.goal_duplication.evaluate) — blocks
                                 on peer-work overlap. Override:
                                 X-Mind-Override-Duplication.
      8. scaffolded-exploration (gates.scaffolded_exploration.evaluate) —
                                 only fires on Apply: + product-category +
                                 no discovered_by. Override:
                                 X-Mind-Override-No-Investigate.

  update-goal (unchanged from PR 7b):
    - uncommitted-work-gate (gates.uncommitted_work.evaluate) on status→completed.
      Override: X-Mind-Override-Uncommitted.
    - capability-gate (gates.capability.evaluate) on non-empty defer_reason.
      Override: X-Mind-Force-Defer. Layer-D auto-Unblock filing is NOT
      performed here — daemon returns the suggestion in the payload; auto-
      filing lives in aspirations.py cmd_update_goal (fallback path).

Out of scope here — wrappers must still use the fallback (direct python)
path to get these:
  - structured-prefix validation, defer_reason_set_at / blocker_ref
    cascades (the rest of cmd_update_goal's mutation pipeline)
  - blocker-create-gate (used by a different code path — CREATE_BLOCKER)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..jsonl_cache import cache as _jsonl_cache
from .. import file_locks, history, changelog

# file_locks.py inserts core/scripts onto sys.path at import time, so the
# bare imports below resolve. _fileops gives us the atomic-write-with-retry
# policy (single source of truth for OneDrive contention); the gates
# package gives us the pure evaluate() functions extracted in PR 7a.
from _fileops import _atomic_write_with_fallback  # noqa: E402
from storage_backend import get_backend  # noqa: E402  # s5c: own-cloud read freshness
from ..agent_paths import assert_not_cruft  # noqa: E402
import _gate_log  # noqa: E402
# B9-deep: single-source census math (NOT a 3rd mirror) — folds evicted goals
# back into progress so goal eviction is metric-neutral. _goal_census is a pure
# leaf, resolvable via the same core/scripts sys.path entry file_locks adds.
from _goal_census import effective_counts as _effective_counts, census_completed as _census_completed  # noqa: E402
from _goal_census import all_evicted_ids as _all_evicted_ids  # noqa: E402  # 0 mint-site tombstone awareness
from gates.origin_signal import evaluate as _origin_signal_eval  # noqa: E402
from gates.goal_duplication import evaluate as _goal_duplication_eval  # noqa: E402
from gates.operator_offload import evaluate as _operator_offload_eval  # noqa: E402
from gates.uncommitted_work import evaluate as _uncommitted_work_eval  # noqa: E402
from gates.capability import evaluate as _capability_eval  # noqa: E402
from gates.completion_artifact import evaluate as _completion_artifact_eval  # noqa: E402
from _override_helpers import audit_bulk_override as _audit_bulk_override  # noqa: E402
# PR 7c additions:
from gates.scaffolded_exploration import evaluate as _scaff_eval  # noqa: E402
from gates.capability_route import evaluate as _cap_route_eval, ACTIVE_AGENTS as _ACTIVE_AGENTS  # noqa: E402
from gates.category_suggest import evaluate as _category_suggest_eval  # noqa: E402
from gates.description_length import evaluate as _desc_len_eval  # noqa: E402
from gates.prose_verification import evaluate as _prose_verification_eval  # noqa: E402
from gates.user_leg_scope import evaluate as _user_leg_scope_eval  # noqa: E402
from gates.defer_classifier import (  # noqa: E402
    is_narrative_defer as _is_narrative_defer,
    STRUCTURED_DEFER_PREFIXES as _STRUCTURED_DEFER_PREFIXES,
)
from gates.blocker_ref import (  # noqa: E402
    validate as _validate_blocker_ref,
    log_unstructured_override as _log_unstructured_override,
)
from gates.defer_date import extract as _extract_defer_date  # noqa: E402
from _work_class import resolve as _resolve_work_class  # noqa: E402
from _goal_source import apply_default as _apply_goal_source_default  # noqa: E402

# Allowed intended_agent values — resolved lazily via the gate's
# `_active_agents()` accessor so a long-lived daemon picks up new agents
# added by /start after daemon spawn. Fresh-eyes review HIGH H1 (2026-05-18):
# the prior `_VALID_INTENDED_AGENTS = set(_ACTIVE_AGENTS) | {"either"}` was
# evaluated at import time and froze to `{"either"}` on a fresh install
# (before any /start had populated team-state.yaml), rejecting the new
# agent's own name. _active_agents() is mtime-cached, so the per-call cost
# is one stat() most of the time. "either" is the well-known sentinel for
# "no strong signal — defer to selector".
from gates.capability_route import _active_agents as _gate_active_agents  # noqa: E402

def _valid_intended_agents() -> set:
    return set(_gate_active_agents()) | {"either"}

# Back-compat shim for any unmigrated reader of the module-level constant.
# This snapshot is intentionally lazy-recomputed on first access via property
# isn't possible at module level; the snapshot here is what the gate sees AT
# IMPORT TIME and serves only as a fall-through display value. All real
# validation paths must call `_valid_intended_agents()` instead.
_VALID_INTENDED_AGENTS = _valid_intended_agents()

# Aspiration-level constants — mirrored from aspirations.py (DECISIONS.md #3).
_VALID_ASP_STATUSES = {"active", "paused", "completed", "retired"}
_VALID_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
_VALID_SCOPES = {"sprint", "project", "initiative"}
_VALID_COORDINATION_MODES = ("parallel", "serial", "mixed")


# Duplicated from aspirations.py — see DECISIONS.md #3 for the rationale.
# Mirror upstream when these change; a parity test could enforce.
_VALID_GOAL_STATUSES = {
    "pending", "in-progress", "completed", "blocked",
    "skipped", "expired", "decomposed", "superseded",
}
# Mirror of aspirations.py::TERMINAL_GOAL_STATUSES (line 44). Drives the
# terminal-status cascade (completed_at stamp, _clear_stale_blockers,
# claim clearing) — when this set diverges from upstream, the cascade
# desyncs. Parity is enforced by visual mirror only; no automated test
# yet (could be added — see _VALID_GOAL_STATUSES note above).
_TERMINAL_GOAL_STATUSES = {"completed", "skipped", "expired",
                           "decomposed", "superseded"}
_ASP_ID_RE = re.compile(r"^asp-(\d{3}|xw-\d{8}T\d{6})$")  # asp-xw-<ts> cross-world ids (mirrors aspirations.py::ASP_ID_RE; companion to _GOAL_ID_RE xw branch below)
_GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")  # 4-digit: asp-115 hit  (2026-05-19); g-xw-<ts>-NN cross-world ids (1 made them selector-visible but the update/close path still rejected them -> stuck at 0/1 forever)
# Mirrors aspirations.py::_AGENT_NAME_RE. Catches flag-name leak into the
# agent_name positional/query slot (, 2026-05-14).
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Mirror of aspirations.py::_UNBLOCK_ACTIVE_STATUSES (line 1452). Drives the
# Layer-D dedup scan: only pending/in-progress Unblock goals block a fresh
# filing — completed/blocked/skipped/expired/decomposed Unblocks do NOT
# (the original failure may have recurred and warrant a fresh routing).
_UNBLOCK_ACTIVE_STATUSES = ("pending", "in-progress")


# ---------------------------------------------------------------------------
# JSONL read/write helpers (small, daemon-local — no _fileops globals)
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Plain JSONL read. Used for read-modify-write where the cache's no-
    mutation contract would force a deepcopy anyway."""
    # s5c (own-cloud): force-fresh via the backend before reading. In-lock RMW
    # callers get lost-update prevention + the If-Match fence etag; plain probe
    # reads get the latest remote state. No-op on LocalBackend.
    get_backend().refresh(path)
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    """Write items to `path` via _fileops' atomic-write-with-retry helper.

    Delegates so daemon and fallback-path writes share ONE retry policy.
    The helper retries os.replace on PermissionError/OSError (Windows anti-
    virus + OneDrive reparse points are the dominant offenders) and, after
    exhausting retries, falls back to in-place rewrite. Telemetry sidecars
    in _fileops degrade gracefully when WORLD_DIR/META_DIR are unset.
    """
    assert_not_cruft(path.parent, "mkdir (_atomic_write_jsonl)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_aspirations_write")


def _verify_goal_persisted(live_path: Path, asp_id: str, goal_id: str) -> bool:
    """never-success-without-persistence invariant (8).

    Under own-cloud, a bare-``file_locks.locked()`` write can RETURN without
    the goal reaching S3 — the 2026-07-14 forensic specimen: add-goal returned
    HTTP 200 + a complete success-shaped goal JSON with id g-115-2083 (computed
    from a ~34h-stale local mirror, max=2082 vs true max=2203), ZERO conflict
    logged, and NOTHING persisted (dual-store read-backs on cc-03 + cc-02 show
    no trace). The bare-locked path does not carry ``locked_rmw``'s 412-retry,
    and a *silent* no-conflict loss (stale-mirror fenced PUT that resolves
    away) is invisible to a conflict-only guard anyway.

    Confirm the goal landed in the AUTHORITATIVE store by reading the
    aspiration file's S3 object DIRECTLY (raw ``get_object``) — NOT through the
    local mirror the daemon just wrote (a mirror-routed read, even
    ``force_fresh``, can return the local copy and mask the loss; same raw-S3
    discipline as ``liveness_check.py``, g-115-2149).

    CONSERVATIVE fail-open — this can only ADD an error-return for a
    definitively-lost write, NEVER turn a real fleet-wide success into a false
    failure. Returns True (assume persisted) on the local backend (the local
    write IS authoritative) and on ANY read/parse error or missing-aspiration;
    returns False ONLY when a clean raw-S3 read shows the aspiration PRESENT but
    the goal ABSENT — the specimen's exact signature. Correctness rests on the
    write being S3-synchronous (``atomic_write`` -> backend ``_put`` ->
    ``s3.put_object(IfMatch=...)``; a 412 is an atomic reject) so read-after-PUT
    reflects true persistence under S3 strong consistency.
    """
    status, _goal = _authoritative_goal_lookup(live_path, asp_id, goal_id)
    return status != "goal-absent"


def _authoritative_goal_lookup(live_path: Path, asp_id: str, goal_id: str):
    """Shared raw-S3 authoritative-store goal fetch (8 / 6).

    The read-back core behind _verify_goal_persisted (add_goal) and
    _verify_claim_persisted (claim) — one helper, multiple call sites.
    Returns (status, goal_record):

      ("no-verify", None)   — cannot verify: local backend (the local write IS
                              authoritative), backend unavailable, raw S3 read
                              error, parse anomaly, or aspiration absent from
                              the raw read. Callers MUST fail open (assume
                              persisted) — see _verify_goal_persisted's
                              conservative-fail-open contract.
      ("goal-absent", None) — clean raw-S3 read: aspiration PRESENT, goal id
                              ABSENT (the g-115-2208 loss signature).
      ("found", goal)       — the goal record as stored in the authoritative
                              store (field checks belong to the caller).
    """
    import sys  # local-import convention (this module imports sys per-function)

    try:
        be = get_backend()
    except Exception:  # noqa: BLE001 — backend unavailable -> can't verify -> assume ok
        return "no-verify", None
    # LocalBackend (no S3 surface): the local write IS authoritative.
    if not all(hasattr(be, a) for a in ("s3", "bucket", "_s3_key")):
        return "no-verify", None
    try:
        key = be._s3_key(str(live_path))
        obj = be.s3.get_object(Bucket=be.bucket, Key=key)
        raw = obj["Body"].read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — raw S3 read failed -> fail-open
        print(f"[daemon] persistence read-back unavailable "
              f"({type(e).__name__}); assuming persisted (fail-open, g-115-2208)",
              file=sys.stderr)
        return "no-verify", None
    try:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("id") == asp_id:
                # Aspiration present in the authoritative store — the goal
                # either landed (persisted) or was lost.
                for g in (rec.get("goals") or []):
                    if g.get("id") == goal_id:
                        return "found", g
                return "goal-absent", None
    except Exception:  # noqa: BLE001 — parse anomaly -> fail-open
        return "no-verify", None
    # Raw S3 read succeeded but the aspiration itself is absent — a
    # whole-aspiration anomaly (or sharding) we cannot pin to this goal;
    # fail-open rather than risk a false failure.
    return "no-verify", None


def _verify_claim_persisted(live_path: Path, asp_id: str, goal_id: str,
                            agent_name: str) -> bool:
    """never-success-without-persistence invariant for claim() (6).

    The add_goal guard checks goal EXISTENCE; a lost CLAIM leaves the goal
    present with ``claimed_by`` unset/stale — the two cc-05 specimens
    (2026-07-16): claim returned complete success JSON with claimed_by set,
    raw-store read-back showed claimed_by=None moments later. Silent claim
    loss is a cross-agent DOUBLE-CLAIM hazard (two agents each holding a
    success response for the same goal).

    Same conservative fail-open contract as _verify_goal_persisted: returns
    False ONLY on a clean raw-S3 read where the claim is definitively absent
    — goal present with ``claimed_by != agent_name``, or goal absent entirely
    (a clean read that lacks the goal also lacks the claim). Local backend
    and every read/parse failure assume persisted (True).
    """
    status, g = _authoritative_goal_lookup(live_path, asp_id, goal_id)
    if status == "no-verify":
        return True
    if status == "goal-absent":
        return False
    return (g or {}).get("claimed_by") == agent_name


# Critical update-goal fields: a swallowed PUT on one of these time-travels
# the goal's lifecycle state (re-served claim, un-deferred defer, resurrected
# completion) — the 1 class. Deliberately NOT every field: the
# verification costs one raw-S3 GET per write, acceptable at release/defer/
# complete frequency, not for bulk field edits (9 cost note).
_CRITICAL_TRANSITION_FIELDS = {"status", "defer_reason"}


def _verify_transition_persisted(live_path: Path, asp_id: str, goal_id: str,
                                 expected: Dict[str, Any]) -> bool:
    """never-success-without-persistence invariant for critical goal
    TRANSITIONS — update-goal status/defer_reason, release, complete-by
    (g-115-2429; siblings: add_goal g-115-2208, claim g-115-2306).

    A swallowed transition PUT leaves the goal PRESENT in the authoritative
    store with the OLD field values — the g-115-2351 specimen (2026-07-16):
    a release+defer write was locally applied and local-read-back verified,
    but the store PUT silently resolved away (rb-3636 mechanism B); the
    daemon-restart re-sync (guard-1043) later pulled the stale store copy
    back over local and the goal time-traveled 7h (rb-3744: local read-back
    is necessary, not sufficient).

    ``expected`` maps each just-written critical field to its final
    in-memory value (post in-lock cascades — compare what the endpoint
    actually persisted, not the caller's raw input). Persisted iff a clean
    raw-S3 read shows the goal present with EVERY expected field equal
    (``None`` expects absent-or-null, so cleared fields verify too).

    Same conservative fail-open contract as the sibling verifiers:
    "no-verify" (local backend, backend/read/parse failure, aspiration
    absent) → True; goal ABSENT on a clean read → False (the store lacks
    the goal, so it certainly lacks the transition).
    """
    status, g = _authoritative_goal_lookup(live_path, asp_id, goal_id)
    if status == "no-verify":
        return True
    if status == "goal-absent":
        return False
    g = g or {}
    return all(g.get(f) == v for f, v in expected.items())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_goal(goal: Dict[str, Any], *, require_id: bool = True) -> None:
    """Basic schema check — id format, status enum, type checks.

    Subset of aspirations.py::validate_goal. Skips verification-schema and
    co_parent_id checks (those depend on cross-record state). Callers that
    need the full validation should use the fallback path.

    require_id=False is the g-328-29 auto-allocation path: the add endpoint
    mints goal ids in-lock AFTER validation, so id absence is legal there.
    An id that IS present must match the format either way.
    """
    gid = goal.get("id") or "<auto>"
    if require_id and "id" not in goal:
        raise ValueError("Goal missing 'id' field")
    if "id" in goal and not _GOAL_ID_RE.match(goal["id"]):
        raise ValueError(f"Invalid goal ID format: {goal['id']} (expected g-NNN-NN[N[N]])")
    if "status" not in goal:
        raise ValueError(f"Goal {gid} missing 'status' field")
    if goal["status"] not in _VALID_GOAL_STATUSES:
        raise ValueError(f"Invalid goal status for {gid}: {goal['status']}")
    if "recurring" in goal and not isinstance(goal["recurring"], bool):
        raise ValueError(f"Goal {gid}: recurring must be a boolean")
    if "interval_hours" in goal:
        v = goal["interval_hours"]
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"Goal {gid}: interval_hours must be positive")


def _assert_no_prose_drift(goal: Dict[str, Any], *, ctx=None) -> None:
    """Raise ValueError on prose-only verification drift (4).

    The CLI validate_goal runs this check; the daemon _validate_goal subset
    historically omitted it, so daemon-path goal adds bypassed the gate
    entirely (realized FN: g-315-119, g-316-08). gates.prose_verification is
    the single-source module that restores CLI/daemon parity (guard-547).
    Called explicitly at the goal-ADD sites (add_goal, _validate_aspiration)
    rather than from _validate_goal so the status-update candidate validation
    (update_goal in-lock) does not retroactively block status changes on
    legacy prose-drift goals. ctx threads the calling agent's meta_dir /
    agent_name so the gate-firing record routes correctly (the module-level
    _gate_log.META_DIR is frozen — see _gate_log_layer_d).
    """
    verdict = _prose_verification_eval(
        goal,
        meta_dir=(ctx.paths.meta if ctx is not None else None),
        agent_name=((ctx.paths.agent_name or None) if ctx is not None else None),
    )
    if verdict["would_block"]:
        raise ValueError(verdict["message"])


# ---------------------------------------------------------------------------
# Aspiration lookup
# ---------------------------------------------------------------------------

def _find_aspiration(items: List[Dict[str, Any]], asp_id: str) -> Optional[Tuple[int, Dict]]:
    for i, asp in enumerate(items):
        if asp.get("id") == asp_id:
            return (i, asp)
    return None


def _find_goal(items: List[Dict[str, Any]], goal_id: str) -> Optional[Tuple[int, int, Dict]]:
    """Return (asp_idx, goal_idx, asp_dict) or None."""
    for ai, asp in enumerate(items):
        for gi, goal in enumerate(asp.get("goals", [])):
            if goal.get("id") == goal_id:
                return (ai, gi, asp)
    return None


def _recompute_progress(asp: Dict[str, Any]) -> None:
    """Derive `progress` from goals — recurring goals excluded from completion
    counts. Mirror of aspirations.py::recompute_progress.

    Mutates `asp` in place. Recurring goals run perpetually and never
    "complete", so they must not inflate the total or be counted as
    completed. They are tracked separately under `recurring_goals`.
    """
    goals = asp.get("goals", [])
    recurring_count = sum(1 for g in goals if g.get("recurring"))
    # Census-augmented (B9-deep): "non_recurring" total/completed via the shared
    # helper, which folds archived_census back in so goal eviction leaves
    # progress (and fan_out_ratio) byte-identical.
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


def _emit_e9_skip_observation(ctx, goal_id: str, new_status: str,
                              goal: Dict[str, Any]) -> None:
    """Append a sensory_buffer observation when a goal flips to skipped/expired.
    Mirror of aspirations.py::_emit_e9_skip_observation.

    Routes the skip rationale through the standard encoding pipeline
    (consolidation Step 2 / state-update Phase 8). Fire-and-forget,
    fail-open: any error logs to stderr but never blocks the status-change
    return path. See encoding-triggers.md E9.

    Daemon-specific: MIND_AGENT is passed via env (the daemon process is
    multi-tenant; wm.py reads _paths.AGENT_DIR which derives from
    MIND_AGENT). The subprocess uses sys.executable + wm.py directly per
    the legacy comment — bash wrappers break Windows paths.
    """
    import os
    import subprocess
    import sys

    title = goal.get("title", "")
    desc = goal.get("description", "")
    skip_reason = (goal.get("skip_reason")
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
    # wm.py is part of the framework — it always lives at PROJECT_ROOT/core/
    # scripts/wm.py relative to this daemon module (mind_api/src/endpoints/
    # aspirations_write.py → parents[3] is PROJECT_ROOT). Using __file__ rather
    # than ctx.paths.project_root keeps the script lookup stable across:
    #   - production (daemon process is in the project, ctx.project_root
    #     equals daemon-module project — both work)
    #   - tests (fixture's tmp project_root has no core/scripts/wm.py, so
    #     using ctx.paths.project_root would point at a non-existent file)
    wm_script = Path(__file__).resolve().parents[3] / "core" / "scripts" / "wm.py"
    sub_env = os.environ.copy()
    # MIND_AGENT drives wm.py's _paths.AGENT_NAME resolver.
    sub_env["MIND_AGENT"] = ctx.paths.agent_name
    # MIND_AGENT_DIR is the test-override hatch on _paths.py line 78. In
    # production this is a no-op (AGENT_DIR falls back to PROJECT_ROOT /
    # AGENT_NAME, which equals ctx.paths.agent anyway). In tests, the
    # fixture's tmp agent dir IS ctx.paths.agent — passing it explicitly
    # decouples wm.py's path resolution from its own __file__-based
    # PROJECT_ROOT detection (which would point at the real repo).
    sub_env["MIND_AGENT_DIR"] = str(ctx.paths.agent)
    try:
        subprocess.run(
            [sys.executable, str(wm_script), "append", "sensory_buffer"],
            input=json.dumps(payload),
            capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
            env=sub_env,
        )
    except Exception as e:
        # Fail-open: never block status change on encoding-hook failure.
        print(f"[daemon update-goal] WARN: E9 skip-encoding failed: {e}",
              file=sys.stderr)


def _clear_stale_blockers_inline(items: List[Dict[str, Any]],
                                 resolved_goal_ids: set) -> None:
    """Remove `blocked_by` references to resolved goals across ALL aspirations.
    Mirror of aspirations.py::_clear_stale_blockers (called from terminal-
    status transitions in cmd_update_goal).

    When a goal hits a terminal status, every other goal that listed it in
    `blocked_by` must drop the reference — otherwise dependent goals stay
    blocked long after the actual blocker resolved. Also nulls
    `blocked_since` when the cleanup leaves `blocked_by` empty.

    Tolerates legacy string-shaped `blocked_by` (auto-wrapped to a list
    for the filter, then written back as a list — same shape promotion the
    legacy CLI does).
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


def _find_existing_unblock_for(items: List[Dict[str, Any]],
                               original_goal_id: str,
                               verb: Optional[str],
                               agent_dir: Optional[Path]
                               ) -> Optional[Dict[str, Any]]:
    """Find an existing pending/in-progress Unblock that already covers
    `original_goal_id`. Mirror of aspirations.py::_find_existing_unblock_for.

    Three OR-ed matching strategies (g-257-04):
      (a) origin_signal == 'unblock:{original_goal_id}' — exact framework match
      (b) title matches 'Unblock:.*for {original_goal_id}\\b' — human convention
      (c) description references both `verb` AND `original_goal_id` within
          80 chars of each other (proximity match) — only when verb provided

    Cross-queue scan: scans the in-memory `items` (the current source —
    typically world) AND the agent queue at `agent_dir/aspirations.jsonl`.
    Fails open on unreadable agent file: skip cross-queue, treat as "no
    existing match" so a missing or corrupt file does not block a legitimate
    Unblock filing.
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
                if g.get("origin_signal") == expected_origin:
                    return {**g, "_aspiration_id": asp_id,
                            "_source": source_label,
                            "_match_strategy": "origin_signal"}
                if title_re.search(g.get("title", "") or ""):
                    return {**g, "_aspiration_id": asp_id,
                            "_source": source_label,
                            "_match_strategy": "title_regex"}
                if verb:
                    desc = (g.get("description", "") or "").lower()
                    verb_lo = verb.lower()
                    gid_lo = original_goal_id.lower()
                    v_idx = desc.find(verb_lo)
                    g_idx = desc.find(gid_lo)
                    if (v_idx >= 0 and g_idx >= 0
                            and abs(v_idx - g_idx) <= 80):
                        return {**g, "_aspiration_id": asp_id,
                                "_source": source_label,
                                "_match_strategy": "description_proximity"}
        return None

    hit = _scan(items, "world")
    if hit is not None:
        return hit

    if agent_dir is not None:
        agent_live = agent_dir / "aspirations.jsonl"
        if agent_live.exists():
            try:
                agent_items = _read_jsonl(agent_live)
                hit = _scan(agent_items, "agent")
                if hit is not None:
                    return hit
            except Exception:
                # Fail-open: unreadable agent file must not block defer-gate.
                pass
    return None


def _file_unblock_inline(items: List[Dict[str, Any]],
                         original_goal_id: str,
                         gate_result: Dict[str, Any],
                         original_asp: Optional[Dict[str, Any]],
                         agent_dir: Optional[Path]
                         ) -> Tuple[Optional[str], str, Optional[str]]:
    """Atomically file an Unblock goal into the caller's `items` list.
    Mirror of aspirations.py::_file_unblock_under_existing_lock.

    Returns (filed_goal_id, status_message, routing_strategy):
      - filed_goal_id is None when no goal was added (existing Unblock
        found by dedup, no target aspiration available, validation error).
      - status_message describes the outcome for logging / response body.
      - routing_strategy is one of {asp-001-current-source,
        original-parent-asp, first-active-asp} or None when no filing.

    Three-strategy target-aspiration fallback (rb-655) — first match wins:
      (a) asp-001 in current source
      (b) original goal's parent aspiration (when (a) fails)
      (c) first active aspiration (last-resort defensive fallback)

    Skips origin-signal-gate / goal-duplication-gate by design: the Unblock's
    origin_signal "unblock:<id>" is structurally valid by construction, and
    the cross-queue dedup scan above replaces the duplication check.
    """
    routing_strategy = None
    target = _find_aspiration(items, "asp-001")
    if target is not None:
        routing_strategy = "asp-001-current-source"
    elif original_asp is not None:
        original_asp_id = original_asp.get("id")
        if original_asp_id:
            target = _find_aspiration(items, original_asp_id)
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
        ), None

    target_idx, target_asp = target
    target_asp_id = target_asp.get("id")

    # Extract verb from gate_result.unblock_title for proximity dedup.
    verb_for_dedup = None
    title_str = gate_result.get("unblock_title") or ""
    if title_str.startswith("Unblock:"):
        rest = title_str[len("Unblock:"):].strip()
        if " for " in rest:
            rest = rest.split(" for ", 1)[0].strip()
        if rest:
            verb_for_dedup = rest

    existing = _find_existing_unblock_for(items, original_goal_id,
                                          verb=verb_for_dedup,
                                          agent_dir=agent_dir)
    if existing is not None:
        existing_id = existing.get("id")
        existing_asp = existing.get("_aspiration_id")
        existing_src = existing.get("_source")
        match_strategy = existing.get("_match_strategy")
        return None, (f"existing Unblock {existing_id} pending in "
                      f"{existing_asp} ({existing_src} queue, "
                      f"strategy={match_strategy}) — idempotent skip"), None

    unblock_title = (gate_result.get("unblock_title")
                     or f"Unblock: capability-routed for {original_goal_id}")
    unblock_description = gate_result.get("unblock_description") or (
        "Defer-gate refused defer_reason — capability-routing matched an "
        f"agent-provisionable action. See capability-gate output for "
        f"{original_goal_id}."
    )

    # Allocate next g-NNN-NN under target_asp via the shared allocator (it
    # counts evicted ids toward max+1 — 0 tombstone awareness).
    new_goal_id = _allocate_goal_id(target_asp)

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
        "origin_signal": f"unblock:{original_goal_id}",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tags": ["unblock", "defer-gate-routed", "framework-maintenance"],
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
    }

    try:
        _validate_goal(unblock_goal)
    except ValueError as exc:
        return None, f"add-goal failed: validation error: {exc}", None

    target_asp.setdefault("goals", []).append(unblock_goal)
    _recompute_progress(target_asp)
    return (new_goal_id,
            f"Filed Unblock goal {new_goal_id} in {target_asp_id}",
            routing_strategy)


def _gate_log_layer_d(ctx, *, filed_id: str, original_goal_id: str,
                      matched_capability: Dict[str, Any],
                      routing_strategy: Optional[str]) -> None:
    """Emit Layer-D telemetry. Mirror of aspirations.py cmd_update_goal line
    1890 _gate_log call. Daemon variant passes ctx.paths.meta + agent_name
    explicitly so the firing record lands in the CALLING agent's
    gate-firings.jsonl (the module-level _gate_log.META_DIR is frozen at
    daemon startup to whichever agent's local-paths.conf was first found —
    wrong for multi-agent requests).

    target_aspiration is derived from filed_id (parents the goal-id's
    middle three digits, e.g. g-001-NN → asp-001) — matches the legacy
    extraction at line 1903.
    """
    target_asp = "asp-" + filed_id.split("-")[1]
    _gate_log.log(
        "capability-gate-layer-d",
        "block",
        trigger_matched=str(matched_capability.get("matched_keyword") or ""),
        payload=original_goal_id,
        extra={
            "filed_unblock_id": filed_id,
            "original_goal_id": original_goal_id,
            "matched_capability": matched_capability,
            "target_aspiration": target_asp,
            "routing_strategy": routing_strategy,
            "source": "daemon",
        },
        meta_dir=ctx.paths.meta,
        agent_name=ctx.paths.agent_name or None,
    )


def _log_defer_date_extraction(ctx, goal_id: str, defer_reason_text: str,
                               extraction: Dict[str, Any]) -> None:
    """Append a defer-date extraction record to
    `world/defer-date-extractions.jsonl`. Mirror of
    aspirations.py::_log_defer_date_extraction.

    Audit trail for the daemon-path narrative→deferred_until auto-pair.
    Without this call the audit goes silent whenever the daemon writes the
    cascade (which is the hot path now). Convention requires it — see
    `core/config/conventions/goal-schemas.md` "Auto-pairing" section.

    Fail-silent: any write error logs to stderr but never blocks the defer
    write. Same contract as the legacy CLI's logger.
    """
    import sys
    from _fileops import locked_append_jsonl
    if ctx.paths.world is None:
        return
    log_path = ctx.paths.world / "defer-date-extractions.jsonl"
    try:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "agent": ctx.paths.agent_name or "unknown",
            "goal_id": goal_id,
            "defer_reason": str(defer_reason_text)[:200],
            "extracted_deferred_until": extraction.get("deferred_until"),
            "pattern": extraction.get("pattern"),
            "match_text": extraction.get("match_text"),
        }
        locked_append_jsonl(str(log_path), record)
    except Exception as e:
        print(f"[daemon defer-date-extractor] WARN: log append failed: {e}",
              file=sys.stderr)


def _allocate_goal_id(asp: Dict[str, Any]) -> str:
    """Allocate next g-NNN-NN id for the aspiration. Mirrors aspirations.py's
    max-seq-plus-one logic; uses two-digit zero-padding (NN). Evicted ids count
    toward max+1 (g-115-2430): re-minting an evicted seq would collide with the
    merge-layer resurrection tombstone, which drops any goal carrying an
    evicted id."""
    asp_id = asp["id"]
    if not _ASP_ID_RE.match(asp_id):
        raise ValueError(f"Invalid aspiration ID format: {asp_id}")
    asp_num = asp_id[len("asp-"):]
    max_seq = 0
    live_ids = [g.get("id", "") for g in asp.get("goals", [])]
    for gid in live_ids + _all_evicted_ids(asp):
        m = re.match(r"^g-\d{3}-(\d{2,4})", gid)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f"g-{asp_num}-{max_seq + 1:02d}"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_paths(ctx, source: str) -> Tuple[Path, Path]:
    """Return (live_path, base_dir) for the requested source.

    INVARIANT: the live_path must equal `aspirations.py:_resolve_paths`'s
    live_path for the same (source, agent). jsonl_cache keys on the Path
    object — divergent construction here vs there silently desyncs the
    invalidate (write) from the get (read), reverting us to the
    eventual-consistency window this PR closed.
    """
    if source == "agent":
        base = ctx.paths.agent
    else:
        base = ctx.paths.world
    return (base / "aspirations.jsonl", base)


def _agent_name(ctx) -> str:
    """Read X-Mind-Agent. Required for write paths — history and changelog
    record which agent made the change."""
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _require_explicit_agent(ctx, source: str) -> Optional["Response"]:  # type: ignore[name-defined]
    """Refuse agent-scoped goal writes when X-Mind-Agent is missing/empty.

    Mirrors store.py:_require_agent_header (g-115-957). When source == "agent",
    the live queue path is ctx.paths.agent, resolved centrally in server.py
    from the X-Mind-Agent header. An empty header makes AgentPathResolver
    fall back to _first_available_agent() (alphabetically-first agent with a
    local-paths.conf — typically "alpha"), so a write meant for the caller's
    OWN agent queue silently targets alpha's queue, while _agent_name() falls
    back to "system" for attribution — the exact mismatch bravo hit on
    2026-05-25 (nearly set alpha's completed g-001-240 -> pending). World-source
    writes target the shared world queue and are agent-agnostic, so they are
    NOT gated here. Read endpoints with no agent context never call this.
    FW-2 (7-agent feedback distillation). See store.py precedent + g-115-957.
    """
    from ..server import Response
    if source != "agent":
        return None
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for agent-scoped goal writes "
            "(source=agent). Caller environment likely has MIND_AGENT "
            "empty/unset — the wrapper omits the header, and the daemon must "
            "not silently fall back to the alphabetically-first agent (would "
            "target the wrong agent's queue; FW-2 / g-115-957). Set "
            "MIND_AGENT explicitly before invoking the wrapper.",
        )
    return None


def _parse_body_json(body: bytes) -> Any:
    if not body:
        raise ValueError("empty body")
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Gate runners
# ---------------------------------------------------------------------------
#
# Each runner returns either None (gate passed; continue) or a Response that
# the handler must return verbatim. Override values come from headers so the
# JSON body stays the same shape callers already use.
#
# The gates are PURE — same evaluate() functions invoked from the CLI
# wrappers. Daemon-vs-CLI equivalence tests in core/tests/gates/ guarantee
# identical block/pass decisions for identical inputs. Do NOT re-implement
# any gate logic here — that's what PR 7a was for.

def _header_override(ctx, header_name: str) -> Optional[str]:
    """Read an override justification from a request header. Empty → None.

    The gates treat empty-string justifications as no-override. Returning
    None lets us pass through the explicit-default semantics of evaluate().
    """
    val = (ctx.headers.get(header_name.lower()) or "").strip()
    return val or None


def _is_forge_goal(goal: Dict[str, Any]) -> bool:
    """True when a goal will invoke /forge-skill — by explicit skill, the
    canonical 'Forge skill:' title prefix, or the 'idea:forge-ready-' origin
    signal the evolve/spark forge-check filing sites stamp. See g-115-2514."""
    if (goal.get("skill") or "") == "/forge-skill":
        return True
    if str(goal.get("title") or "").startswith("Forge skill:"):
        return True
    if str(goal.get("origin_signal") or "").startswith("idea:forge-ready-"):
        return True
    return False


def _ensure_forge_curriculum_precondition(goal: Dict[str, Any]) -> bool:
    """Guarantee a /forge-skill goal carries the pc-curriculum-forge structured
    precondition, attaching the canonical form if absent. Returns True on mutate.

    Why (g-115-2514): forging requires the EXECUTING agent be past the curriculum
    Growth gate (allow_forge_skill). The four forge-filing sites (aspirations-evolve
    Step 9, aspirations-spark Phase 6.5, respond, reflect-on-outcome) route a forge
    goal to a domain-owner intended_agent but historically curriculum-checked only
    the FILING agent, never the TARGET (g-315-383 routed to echo at cur-01). The
    canonical guard is this precondition: goal-selector.py evaluates structured
    preconditions via predicate.command_succeeds, whose subprocess inherits the
    SELECTING agent's MIND_AGENT — so the check runs against the ACTUAL executor
    and a routed forge goal is filtered from a below-Growth target's candidates
    until it can forge. Guaranteeing the precondition here makes the target-agent
    check structural (script-enforced across all filing sites) instead of relying
    on each LLM filing site to add it. Non-refusing (the forge signal is preserved)
    and strictly more correct than a filing-time curriculum snapshot, which would
    go stale as curriculum stages advance between filing and execution."""
    verification = goal.get("verification")
    if not isinstance(verification, dict):
        verification = {}
        goal["verification"] = verification
    pcs = verification.get("preconditions")
    if not isinstance(pcs, list):
        pcs = []
        verification["preconditions"] = pcs
    for p in pcs:
        if isinstance(p, dict) and (
            p.get("id") == "pc-curriculum-forge"
            or "allow_forge_skill" in str(p.get("command") or "")
        ):
            return False  # already gated — respect a caller-supplied precondition
    pcs.append({
        "id": "pc-curriculum-forge",
        "type": "command_succeeds",
        "command": "bash core/scripts/curriculum-contract-check.sh --action allow_forge_skill",
        "timeout_seconds": 30,
        "description": (
            "Curriculum contract permits forge_skill for the EXECUTING agent "
            "(exit 0 = permitted; unlocks at Growth). Auto-attached by the "
            "forge-curriculum gate (g-115-2514) so a forge goal routed to a "
            "below-Growth agent is filtered from that agent's selection "
            "candidates until it can forge."
        ),
    })
    return True


def _run_add_goal_pipeline(ctx, goal: Dict[str, Any], source: str
                           ) -> Tuple[Optional["Response"], List[str], Optional[Tuple[str, List[str]]]]:  # type: ignore[name-defined]
    """Run the full add-goal pipeline: advisories → mutators → blockers.

    Mirrors aspirations.py cmd_add_goal order exactly. Mutates `goal` in
    place for category/work_class/intended_agent stamps and origin-signal
    auto-derive.

    Returns (Response if blocked else None, warning messages, bulk_audit).
    bulk_audit is (justification, slots_filled) when X-Mind-Override-All
    fanned into at least one unset per-gate slot, else None — the caller
    audits it to override-bypass-ledger.jsonl AFTER the write lands (mirror
    of add(ctx) and legacy cmd_add_goal's post-write audit ordering).
    """
    from ..server import Response
    warnings: List[str] = []

    # Bulk override fan-out (mirrors _override_helpers.apply_override_all and
    # the add(ctx) sibling endpoint). Legacy cmd_add_goal fanned --override-all
    # into exactly these four slots; per-gate headers ALWAYS win — bulk only
    # fills slots the caller left unset. _header_override normalizes empty →
    # None, so `is None` and `or` agree (no empty-string ambiguity here,
    # unlike argparse's apply_override_all).
    bulk_override = _header_override(ctx, "X-Mind-Override-All")
    raw_sig = _header_override(ctx, "X-Mind-Override-Signal")
    raw_dup = _header_override(ctx, "X-Mind-Override-Duplication")
    raw_ni = _header_override(ctx, "X-Mind-Override-No-Investigate")
    raw_off = _header_override(ctx, "X-Mind-Override-Offload")
    bulk_slots_filled: List[str] = []
    if bulk_override:
        if raw_sig is None:
            bulk_slots_filled.append("override_signal")
        if raw_dup is None:
            bulk_slots_filled.append("override_duplication")
        if raw_ni is None:
            bulk_slots_filled.append("override_no_investigate")
        if raw_off is None:
            bulk_slots_filled.append("override_offload")
    eff_sig = raw_sig or bulk_override
    eff_dup = raw_dup or bulk_override
    eff_ni = raw_ni or bulk_override
    eff_off = raw_off or bulk_override
    bulk_audit: Optional[Tuple[str, List[str]]] = (
        (bulk_override, bulk_slots_filled)
        if (bulk_override and bulk_slots_filled) else None
    )

    # === Phase A: advisories (warn-only) ===

    # 1. user_leg_scope advisory
    uls_result = _user_leg_scope_eval(
        goal_id=goal.get("id") or "<unassigned>",
        participants=goal.get("participants"),
        user_leg_scope=goal.get("user_leg_scope"),
    )
    if uls_result["warned"]:
        warnings.append(uls_result["message"])

    # 2. description-length advisory (+ telemetry to META_DIR)
    dl_result = _desc_len_eval(
        goal, source=source, meta_dir=ctx.paths.meta,
    )
    if dl_result["warned"]:
        warnings.append(dl_result["message"])

    # === Phase B: mutators (run BEFORE blockers that read these fields) ===

    # 3. category-suggest — only when caller didn't pick one or marked
    # "uncategorized". Matches cmd_add_goal's behavior.
    if not goal.get("category") or goal.get("category") == "uncategorized":
        text = f"{goal.get('title', '')}. {goal.get('description', '')}"
        matches = _category_suggest_eval(text, top_n=1, world_dir=ctx.paths.world)
        if matches and matches[0].get("score", 0) > 0:
            goal["category"] = matches[0]["key"]
        if not goal.get("category"):
            goal["category"] = "uncategorized"

    # 4. work_class resolver — only when caller didn't pick one. Pure
    # in-process call; no I/O beyond _work_class's own lru_cache.
    if not goal.get("work_class"):
        goal["work_class"] = _resolve_work_class(goal.get("category"))

    # === Phase C: blocker — origin-signal (run first; failing fast saves
    # the duplication gate's network calls) ===
    sig_payload = {
        "title": goal.get("title", ""),
        "description": goal.get("description", ""),
        "origin_signal": goal.get("origin_signal"),
        "source": source,
    }
    sig_result = _origin_signal_eval(
        sig_payload,
        override_signal=eff_sig,
        agent_name=ctx.paths.agent_name,
        world_dir=ctx.paths.world,
    )
    if sig_result.get("would_block"):
        return Response.json({
            "error": "origin_signal_blocked",
            "gate": "origin-signal-gate",
            "gate_output": sig_result,
        }, status=400), warnings, None
    # Layer-D auto-derive (): patch goal.origin_signal so the
    # stored signal matches what the gate accepted.
    if sig_result.get("auto_derived") and sig_result.get("origin_signal"):
        goal["origin_signal"] = sig_result["origin_signal"]

    # === Phase C.5: goal-source default (, applied 2026-05-19) ===
    # The asp-creation pipeline at cmd_add() calls _apply_goal_source_default
    # for every goal (line ~3012); the add-goal hot path here did NOT, so
    # goals filed via /v1/aspirations/add-goal landed with goal_source=null
    # until a subsequent backfill run. Wire the same SSOT helper here so
    # both paths produce the same metadata. In-process pure helper, no I/O.
    _apply_goal_source_default(goal)

    # === Phase D: capability-route mutator (sets intended_agent) ===
    # Skipped when caller explicitly set intended_agent — caller's choice
    # wins; gate never overrides. Mirrors cmd_add_goal lines ~3086-3111.
    if not goal.get("intended_agent"):
        route_to = _header_override(ctx, "X-Mind-Route-To")
        handoff_to = goal.get("handoff_to")
        if (not route_to) and isinstance(handoff_to, str) \
                and handoff_to in _valid_intended_agents():
            # An explicit handoff_to is ALSO the caller's routing choice
            # (7): without this, the title-verb classifier can stamp a
            # THIRD agent and the selector's intended_agent filter then hides
            # the goal from the very agent the handoff named — handoff_bonus
            # unreachable (observed: "Apply:" slices with handoff_to=zeta
            # stamped intended_agent=alpha, invisible to zeta across 3
            # consecutive selector runs). The X-Mind-Route-To header remains
            # the stronger per-call override when present.
            goal["intended_agent"] = handoff_to
        else:
            route_result = _cap_route_eval(
                goal.get("title", "") or "",
                category=goal.get("category", "") or "",
                description=goal.get("description", "") or "",
                route_to=route_to,
            )
            ia = route_result.get("intended_agent")
            if ia in _valid_intended_agents():
                goal["intended_agent"] = ia

    # === Phase D.5: forge-curriculum precondition guarantee (4) ===
    # A /forge-skill goal is executable only by an agent past the curriculum
    # Growth gate. Guarantee the pc-curriculum-forge precondition so the
    # goal-selector's per-executor check (predicate.command_succeeds inherits the
    # selecting agent's MIND_AGENT) gates the TARGET agent — closing the
    # filing-site gap where only the FILING agent's curriculum was checked
    # ( was routed to echo at cur-01, which cannot forge). Pure
    # in-process mutation; no I/O. Non-refusing — preserves the forge signal.
    if _is_forge_goal(goal):
        if _ensure_forge_curriculum_precondition(goal):
            warnings.append(
                "forge-curriculum-gate: attached pc-curriculum-forge precondition "
                "(g-115-2514) — this forge goal is selectable only by an agent whose "
                "curriculum permits allow_forge_skill (Growth+)."
            )

    # === Phase E: goal-duplication blocker ===
    dup_result = _goal_duplication_eval(
        goal,
        override_duplication=eff_dup,
        agent_name=ctx.paths.agent_name,
        world_dir=ctx.paths.world,
        project_root=ctx.paths.project_root,
    )
    if dup_result.get("would_block"):
        return Response.json({
            "error": "goal_duplication_blocked",
            "gate": "goal-duplication-gate",
            "gate_output": dup_result,
        }, status=400), warnings, None

    # === Phase E.5: operator-offload blocker — only when recurring ===
    # Layer-B backstop for gh-005 (meta/aspiration-generation-strategy.yaml):
    # a recurring goal must carry an `offload_decision` explaining why the
    # work stays on the LLM loop instead of becoming an operator job.
    # Pure no-op for non-recurring goals. user-directed 2026-07-13 (mc-066).
    off_result = _operator_offload_eval(
        goal,
        override_offload=eff_off,
        meta_dir=ctx.paths.meta,
        agent_name=ctx.paths.agent_name,
    )
    if off_result.get("would_block"):
        return Response.json({
            "error": "operator_offload_blocked",
            "gate": "operator-offload-gate",
            "gate_output": off_result,
        }, status=400), warnings, None

    # === Phase G: scaffolded-exploration blocker (Apply: + product cat) ===
    scaff_result = _scaff_eval(
        goal,
        override_no_investigate=eff_ni,
    )
    if scaff_result.get("would_block"):
        return Response.json({
            "error": "scaffolded_exploration_blocked",
            "gate": "scaffolded-exploration-gate",
            "gate_output": scaff_result,
        }, status=400), warnings, None

    return None, warnings, bulk_audit


def _run_update_goal_gates(ctx, goal_id: str, field: str, value
                           ) -> Tuple[Optional["Response"], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:  # type: ignore[name-defined]
    """Run pre-lock gates: uncommitted-work (status→completed), capability
    (narrative defer_reason), blocker_ref requirement (narrative defer
    that passed the capability gate).

    Returns (response_or_none, normalized_blocker_ref_or_none,
             cap_block_for_layer_d_or_none).

    - response_or_none: 400 Response when any gate blocks; caller propagates.
      For the capability-blocked-with-suggestion case the response is built
      here as a partial — the caller updates the body with filed_unblock_id
      after running Layer-D filing.
    - normalized_blocker_ref_or_none: validated blocker_ref from the
      X-Mind-Blocker-Ref header (Layer 2). None when no narrative defer or
      no ref supplied.
    - cap_block_for_layer_d_or_none: carries the cap_result when the
      capability gate blocked AND suggested an Unblock. The caller uses this
      to drive Layer-D auto-Unblock filing (PR 7j) — atomically file the
      Unblock under the live aspirations.jsonl lock, then return 400 with
      `filed_unblock_id` in the response body. None when Layer-D filing is
      not warranted (gate didn't block, or blocked without a suggestion).
    """
    from ..server import Response

    if field == "status" and value == "completed":
        unc_result = _uncommitted_work_eval(
            goal_id=goal_id,
            override=_header_override(ctx, "X-Mind-Override-Uncommitted"),
            repo_path=ctx.paths.project_root,
            world_dir=ctx.paths.world,
            agent_name=ctx.paths.agent_name,
        )
        if unc_result.get("would_block"):
            return Response.json({
                "error": "uncommitted_work_blocked",
                "gate": "uncommitted-work-gate",
                "gate_output": unc_result,
            }, status=400), None, None

    # Capability gate fires ONLY on NARRATIVE defer_reason writes. Structured
    # internal markers (precondition_unmet:, blocked_on_dependency, Circuit
    # breaker:) bypass the gate — they're machine-written state, not claims
    # about external signals, and the keyword scan would collide with forged
    # skills. is_narrative_defer is the single source of truth for this
    # predicate (gates.defer_classifier).
    normalized_ref: Optional[Dict[str, Any]] = None
    if _is_narrative_defer(field, value):
        # ORDER IS LOAD-BEARING: Layer 1 (capability) runs BEFORE Layer 2
        # (blocker_ref). X-Mind-Force-Defer bypasses ONLY Layer 1, not
        # Layer 2 — same two-flag contract as the legacy CLI's
        # --force-defer + --force-unstructured-defer. Do NOT reorder for
        # readability; a reorder silently changes which gate
        # X-Mind-Force-Defer bypasses.
        # Layer 1: capability gate. suggest_unblock=True populates Unblock
        # title/description fields in cap_result — when this gate blocks
        # AND emits a suggestion, the caller runs Layer-D auto-Unblock
        # filing (PR 7j) inside the live aspirations.jsonl lock so the
        # action the agent should perform is queued atomically with the
        # defer refusal. No wrapper fallback is needed.
        cap_result = _capability_eval(
            str(value),
            intended_participants="user",
            override_agent_match=_header_override(ctx, "X-Mind-Force-Defer"),
            for_goal_id=goal_id,
            suggest_unblock=True,
            agent_name=ctx.paths.agent_name,
            world_dir=ctx.paths.world,
        )
        if cap_result.get("would_block"):
            cap_block_for_layer_d = (
                cap_result if cap_result.get("unblock_suggested") else None
            )
            return Response.json({
                "error": "capability_blocked",
                "gate": "capability-gate",
                "gate_output": cap_result,
            }, status=400), None, cap_block_for_layer_d

        # Layer 2: blocker_ref requirement ( + Change 1). Every
        # narrative defer MUST cite a structured blocker_ref so the
        # quiescence gate can distinguish genuine external gating from
        # narrative laundering. Three accept paths:
        #   (a) X-Mind-Blocker-Ref header with valid JSON payload
        #   (b) X-Mind-Force-Unstructured-Defer override (audited)
        #   (c) [CLI-only — daemon has no third path here yet]
        ref_header = _header_override(ctx, "X-Mind-Blocker-Ref")
        force_unstructured = _header_override(
            ctx, "X-Mind-Force-Unstructured-Defer")
        if ref_header:
            ok, parsed = _validate_blocker_ref(ref_header)
            if not ok:
                return Response.json({
                    "error": "blocker_ref_invalid",
                    "gate": "blocker-ref-gate",
                    "reason": parsed,
                }, status=400), None, None
            normalized_ref = parsed
        elif force_unstructured:
            # Override granted — log to audit ledger (best-effort).
            # log_unstructured_override returns None on failure; we don't
            # surface that to the client (override was already granted).
            _log_unstructured_override(
                ctx.paths.world,
                goal_id=goal_id,
                defer_reason_text=str(value),
                justification=force_unstructured,
                agent_name=ctx.paths.agent_name,
                source="daemon:update_goal:unstructured-defer",
            )
        else:
            return Response.json({
                "error": "blocker_ref_required",
                "gate": "blocker-ref-gate",
                "reason": (
                    f"defer_reason on {goal_id} requires a structured "
                    f"blocker_ref. Pass X-Mind-Blocker-Ref with a JSON "
                    f"payload, use a structured defer prefix "
                    f"({' / '.join(_STRUCTURED_DEFER_PREFIXES)}), or pass "
                    f"X-Mind-Force-Unstructured-Defer for an audited "
                    f"override (disqualifies the goal from quiescence)."
                ),
            }, status=400), None, None

    return None, normalized_ref, None


def _file_routing_audit_investigate(ctx, goal: Dict[str, Any]) -> Optional[str]:
    """Phase D.5 (5): post-decompose Self.md routing audit.

    After Phase D stamps intended_agent and the main goal persists, this
    helper runs the audit (`core/scripts/post-decompose-routing-audit.py`).
    When a significant mismatch is detected, it files a single Investigate
    goal into asp-115 (world source) with idempotent dedup against existing
    routing-mismatch:<id> origin_signals.

    Returns the filed Investigate goal id, or None when:
      - audit module can't be imported (fail-open),
      - audit decision is no_file,
      - dedup found an existing pending/in-progress Investigate,
      - the file write failed.

    Side-effect-free on errors. Never raises. Bypasses _run_add_goal_pipeline
    intentionally — the Investigate is system-generated, well-formed, and
    routing it back through goal-duplication-gate would falsely block on
    "Investigate:" verb overlap.

    Recursion guard: the audit module bails on origin_signal starting with
    "routing-mismatch:", so the Investigate filed here won't re-trigger.
    """
    try:
        import importlib.util
        from pathlib import Path as _Path
        core_scripts = _Path(ctx.paths.project_root) / "core" / "scripts"
        audit_path = core_scripts / "post-decompose-routing-audit.py"
        spec = importlib.util.spec_from_file_location(
            "post_decompose_routing_audit_loaded", str(audit_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        audit_fn = mod.audit
    except Exception:  # pylint: disable=broad-except
        return None

    try:
        result = audit_fn(goal, project_root=_Path(ctx.paths.project_root))
    except Exception:  # pylint: disable=broad-except
        return None

    if result.get("decision") != "file":
        return None
    invest_spec = result.get("investigate_spec")
    if not invest_spec or not isinstance(invest_spec, dict):
        return None

    asp_id = "asp-115"
    world_live, world_base = _resolve_paths(ctx, "world")
    agent = _agent_name(ctx)
    origin_signal_val = invest_spec.get("origin_signal", "")

    try:
        with file_locks.locked(world_live):
            items = _read_jsonl(world_live)
            found = _find_aspiration(items, asp_id)
            if found is None:
                return None
            _, asp = found

            # Idempotent dedup: skip if a pending/in-progress Investigate
            # with the same origin_signal already exists in asp-115.
            for existing in asp.get("goals", []) or []:
                if (existing.get("origin_signal") == origin_signal_val
                        and existing.get("status") in (
                            "pending", "in-progress")):
                    return None

            invest_goal: Dict[str, Any] = {
                "id": _allocate_goal_id(asp),
                "title": invest_spec.get("title", ""),
                "description": invest_spec.get("description", ""),
                "priority": invest_spec.get("priority", "MEDIUM"),
                "status": "pending",
                "participants": invest_spec.get("participants", ["agent"]),
                "origin_signal": origin_signal_val,
                "category": invest_spec.get(
                    "category", "framework-decomposition"),
                "discovered_by": invest_spec.get(
                    "discovered_by", "post-decompose-routing-audit"),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "intended_agent": "bravo",
                "work_class": "framework",
            }
            try:
                _validate_goal(invest_goal)
            except ValueError:
                return None

            asp.setdefault("goals", []).append(invest_goal)
            history.snapshot(
                world_live, world_base, agent,
                summary=f"add-goal {invest_goal['id']} (routing-audit)")
            _atomic_write_jsonl(world_live, items)
            changelog.append(
                world_base, agent, world_live, "edit",
                summary=f"add-goal {invest_goal['id']} (routing-audit)",
                lines_changed=len(items))
            _jsonl_cache().invalidate(world_live)
        return invest_goal["id"]
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def add_goal(ctx) -> "Response":  # type: ignore[name-defined]
    import sys  # local-import convention (this module imports sys per-function)
    from ..server import Response

    asp_id = (ctx.query.get("asp_id") or "").strip()
    if not asp_id:
        return Response.error(400, "missing_param", "query parameter 'asp_id' required")
    if not _ASP_ID_RE.match(asp_id):
        return Response.error(400, "invalid_asp_id", f"expected asp-NNN, got {asp_id!r}")

    source = (ctx.query.get("source") or "world").lower()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", "source must be world or agent")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    try:
        goal = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON goal object: {e}")
    if not isinstance(goal, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    # Status/timestamp defaults applied outside the lock — they don't depend
    # on file state. ID allocation MUST happen inside the lock (below) so two
    # concurrent writers can't allocate the same g-NNN-NN sequence.
    goal.setdefault("status", "pending")
    goal.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    # filed_by_agent (): stamp the filing agent at add time so the
    # per-agent contribution-vs-harm scorecard can attribute churn ("who filed
    # the goal that later expired?") without git-blame / which-queue heuristics.
    # setdefault preserves an explicit caller-supplied value (e.g. a goal filed
    # on behalf of another agent). Backward-compat: goals written before this
    # field default to "unknown" at read time (no field requirement). Validated
    # (null-or-string) in aspirations.py::validate_goal. `agent` resolved above
    # via _agent_name(ctx); guard against an empty resolution (leave unset →
    # read-time "unknown") rather than stamping a blank attribution.
    if agent:
        goal.setdefault("filed_by_agent", agent)
    # blocked_since auto-stamp on add: parity with add(ctx) (full-aspiration
    # add, ~L3186) and cmd_update_goal's blocked_by cascade. A goal added WITH
    # blocked_by MUST carry blocked_since — otherwise goal-selector's
    # dependency-timeout check reads the null timestamp as an EXPIRED
    # dependency (fail-open) and the blocked goal leaks into the executable
    # ranked list, where another agent may be handed a goal whose
    # prerequisites have not run. Single-goal add was the one blocked_by-write
    # path missing this stamp (observed: HTN decomposition children added with
    # inline blocked_by surfaced as top-ranked executable while genuinely
    # blocked). Applied outside the lock with the other no-file-state defaults.
    if goal.get("blocked_by") and not goal.get("blocked_since"):
        goal["blocked_since"] = datetime.now().isoformat(timespec="seconds")

    # --- Full pipeline (advisories + mutators + blockers) runs BEFORE the
    # lock so slow I/O (git log, target-state probe, tree YAML read) does
    # not hold other writers. Override headers carry caller-supplied
    # justifications; gates audit them to world/*-overrides.jsonl when they
    # would have blocked. Mirror of cmd_add_goal's --override-* flags.
    # DO NOT move this call inside the `file_locks.locked` block below —
    # gates do slow external I/O (git log, target-state file probe, tree
    # YAML read, override-ledger writes). Serializing all writers behind
    # gate evaluation would collapse daemon write throughput. The TOCTOU
    # window between gate evaluation and the write is accepted (legacy CLI
    # has the same window). ---
    gate_resp, warnings, bulk_audit = _run_add_goal_pipeline(ctx, goal, source)
    if gate_resp is not None:
        return gate_resp

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_aspiration(items, asp_id)
            if found is None:
                return Response.error(404, "aspiration_not_found",
                                      f"Aspiration {asp_id} not found in {source}")
            asp_idx, asp = found

            if "id" not in goal:
                goal["id"] = _allocate_goal_id(asp)
            # Uniqueness guard (4): a caller-supplied id can collide
            # with an existing goal in this aspiration — INCLUDING a completed
            # one — the cross-Mind-promotion-injection corruption class that
            # produced two distinct goals both id 9. Every id-based op
            # (claim / update / complete-by) then targets the FIRST match and
            # silently corrupts the wrong record. _allocate_goal_id returns
            # max-seq+1 (strictly greater than every existing seq), so
            # reassigning through it yields a fresh unique id. Reassign + warn
            # rather than overwrite the colliding record or reject the add.
            existing_ids = {g.get("id") for g in asp.get("goals", []) if g.get("id")}
            if goal["id"] in existing_ids:
                _collided_id = goal["id"]
                goal["id"] = _allocate_goal_id(asp)
                print(
                    f"[daemon add_goal] WARN: goal id {_collided_id!r} already "
                    f"exists in {asp_id}; reassigned to {goal['id']!r} "
                    f"(uniqueness guard, g-115-1544)",
                    file=sys.stderr,
                )

            try:
                _validate_goal(goal)
                # Prose-verification-drift parity (4): the daemon
                # _validate_goal subset omits this check that the CLI runs;
                # without it a daemon-added prose-only goal (markers present,
                # verification.checks empty) slips through. ctx routes the
                # firing telemetry to the calling agent.
                _assert_no_prose_drift(goal, ctx=ctx)
            except ValueError as e:
                return Response.error(400, "validation_failed", str(e))

            # asp is the same dict object as items[asp_idx] (see _find_aspiration);
            # mutating asp's goals list is sufficient — no rebind needed.
            asp.setdefault("goals", []).append(goal)

            # History snapshot BEFORE write so a daemon crash leaves a
            # recoverable copy.
            history.snapshot(live_path, base_dir, agent, summary=f"add-goal {goal['id']}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"add-goal {goal['id']}",
                             lines_changed=len(items))
            # Invalidate while the lock is held — keeps the cache flip inside
            # the same critical section as the write. Out-of-lock invalidate
            # leaves a narrow window where a reader can stat the new mtime,
            # miss the size-or-mtime check on a pathological tick collision,
            # and serve stale data. See jsonl-read-modify-write-race.
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence invariant (8): the write above
    # returned without raising, but under own-cloud a bare-locked() write can
    # report success while the fenced PUT never reached S3 (stale-mirror /
    # silent no-conflict loss — the 2026-07-14 forensic specimen: HTTP 200,
    # id from a 34h-stale mirror, zero trace in S3). Confirm the goal is in the
    # authoritative store BEFORE the post-write audits (which record blast
    # radius only for goals that persisted) and BEFORE returning success;
    # refuse the false 200 otherwise so the caller retries instead of silently
    # losing the goal. Conservative fail-open (see _verify_goal_persisted): a
    # real success can never become a false failure.
    if not _verify_goal_persisted(live_path, asp_id, goal["id"]):
        print(f"[daemon add_goal] WRITE-LOSS DETECTED: {goal['id']} in {asp_id} "
              f"returned success-shaped but is ABSENT from the authoritative "
              f"store after write (own-cloud silent write-loss, g-115-2208)",
              file=sys.stderr)
        return Response.error(
            500, "write_not_persisted",
            f"add-goal for {goal['id']} did not persist to the authoritative "
            f"store (own-cloud write-loss, g-115-2208); retry the add")

    # Audit bulk override AFTER the goal lands (mirror of add(ctx) and legacy
    # cmd_add_goal's post-write audit ordering — records blast radius only for
    # goals that actually persisted). Best-effort: helper never raises.
    if bulk_audit is not None:
        bulk_just, bulk_slots = bulk_audit
        import hashlib as _hashlib
        bulk_token = _hashlib.sha1(
            bulk_just.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        _audit_bulk_override(
            bulk_token, bulk_just, bulk_slots,
            context={"caller": "aspirations_write.py:add_goal",
                     "goal_id": goal["id"],
                     "asp_id": asp_id,
                     "source": source},
            world_dir=ctx.paths.world)

    # Phase D.5 (5): Routing-audit after main goal persists.
    # Outside the original lock — the audit reads each agent's Self.md and
    # files a separate Investigate goal in asp-115 if a significant mismatch
    # is detected. Fail-open: returns None on any error.
    routing_audit_investigate_id = _file_routing_audit_investigate(ctx, goal)

    response_body: Dict[str, Any] = {
        "ok": True,
        "goal_id": goal["id"],
        "aspiration_id": asp_id,
        "source": source,
        # Full persisted goal — wrappers print this to stdout to match the
        # legacy CLI's `json.dumps(goal, indent=2)` output. Includes daemon-
        # side mutations (category, work_class, intended_agent, origin_signal
        # auto-derive) so the caller sees what actually landed on disk.
        "goal": goal,
    }
    if routing_audit_investigate_id:
        response_body["routing_audit_investigate_id"] = routing_audit_investigate_id
    # Surface advisory warnings on 200 — wrappers (when migrated) can
    # re-emit them to stderr to match the legacy CLI experience.
    if warnings:
        response_body["warnings"] = warnings
    return Response.json(response_body)


def update_goal(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    goal_id = (ctx.query.get("id") or "").strip()
    if not goal_id:
        return Response.error(400, "missing_param", "query parameter 'id' required")
    if not _GOAL_ID_RE.match(goal_id):
        return Response.error(400, "invalid_goal_id", f"expected g-NNN-NN[N[N]], got {goal_id!r}")

    field = (ctx.query.get("field") or "").strip()
    if not field:
        return Response.error(400, "missing_param", "query parameter 'field' required")
    # `field` is treated as a flat top-level key — dotted paths are REJECTED.
    # Mirror of cmd_update_goal's same check (legacy CLI line ~2064). Pre-fix,
    # a dotted name silently created a LITERAL "verification.outcomes" string
    # key on the goal dict instead of nesting into goal["verification"]
    # ["outcomes"]. For nested writes pass the parent field with full nested
    # JSON; for dotted-path navigation use team-state.py.
    if "." in field:
        return Response.error(
            400, "dotted_field_rejected",
            f"dotted field name {field!r} is not supported by update_goal. "
            f"This endpoint writes flat top-level keys only. To write a "
            f"nested value, pass the parent field with full nested JSON "
            f"(e.g., field=verification value={{\"outcomes\":[...]}}). "
            f"For dotted-path navigation, use team-state.py.",
        )

    source = (ctx.query.get("source") or "world").lower()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", "source must be world or agent")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    try:
        value = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON value: {e}")

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    # --- Gates (run BEFORE the lock — slow I/O must not hold writers).
    # uncommitted-work fires on status→completed; capability + blocker_ref
    # fire on NARRATIVE defer_reason writes (gates.defer_classifier filters
    # structured prefixes). normalized_ref carries a validated blocker_ref
    # payload from X-Mind-Blocker-Ref so the in-lock cascade can persist
    # it without re-parsing the header. cap_block_for_layer_d carries the
    # capability-gate result when the gate blocked AND suggested an Unblock
    # — the Layer-D filing below runs under a brief, dedicated lock around
    # the aspirations.jsonl write so the suggested action lands atomically
    # with the defer refusal.
    gate_resp, normalized_ref, cap_block_for_layer_d = (
        _run_update_goal_gates(ctx, goal_id, field, value)
    )
    if gate_resp is not None:
        # PR 7j Layer-D auto-Unblock filing — mirror of aspirations.py
        # cmd_update_goal lines 1865-1934. Fires ONLY when the capability
        # gate blocked AND emitted a suggestion; other refusals (uncommitted,
        # blocker_ref_invalid, blocker_ref_required) propagate the 400 as-is.
        # Takes the aspirations.jsonl lock for the duration of dedup + write
        # only — the gate evaluation itself already ran pre-lock, so the
        # critical section here is short. The original defer_reason write is
        # NEVER committed (the original goal stays pending); only the new
        # Unblock goal is persisted, queueing the action the agent should
        # perform instead. The response body then carries filed_unblock_id
        # so the wrapper / caller can surface it without a second read.
        if cap_block_for_layer_d is not None:
            try:
                with file_locks.locked(live_path):
                    items = _read_jsonl(live_path)
                    original_found = _find_goal(items, goal_id)
                    original_asp = (
                        original_found[2] if original_found is not None
                        else None
                    )
                    filed_id, filing_status, routing_strategy = (
                        _file_unblock_inline(
                            items,
                            original_goal_id=goal_id,
                            gate_result=cap_block_for_layer_d,
                            original_asp=original_asp,
                            agent_dir=ctx.paths.agent,
                        )
                    )
                    if filed_id is not None:
                        history.snapshot(
                            live_path, base_dir, agent,
                            summary=(f"defer-gate filed Unblock {filed_id} "
                                     f"for {goal_id}"),
                        )
                        _atomic_write_jsonl(live_path, items)
                        changelog.append(
                            base_dir, agent, live_path, "edit",
                            summary=(f"defer-gate filed Unblock {filed_id} "
                                     f"for {goal_id}: {filing_status}"),
                            lines_changed=len(items),
                        )
                        _jsonl_cache().invalidate(live_path)
            except OSError as e:
                # Fail-open: filing the audit goal is best-effort. The
                # original refusal still stands — the user gets the 400.
                filed_id = None
                filing_status = f"add-goal write failed: {e}"
                routing_strategy = None

            # Telemetry: Layer-D firing (mirror of aspirations.py line 1890).
            # Only the success case emits "block" — when filing was skipped
            # (existing Unblock found, no target asp, validation error), the
            # gate still refused but the routing decision is informational.
            matches = cap_block_for_layer_d.get("matches") or []
            first = matches[0] if matches else {}
            if filed_id is not None:
                _gate_log_layer_d(
                    ctx,
                    filed_id=filed_id,
                    original_goal_id=goal_id,
                    matched_capability={
                        "skill": first.get("skill"),
                        "matched_keyword": first.get("matched_keyword"),
                        "row": first.get("row"),
                    },
                    routing_strategy=routing_strategy,
                )

            # Mutate the response body in place so the wrapper sees
            # filing_status without an extra round-trip. The 400 is preserved.
            try:
                body = json.loads(gate_resp.body.decode("utf-8"))
            except Exception:
                body = {}
            body["filed_unblock_id"] = filed_id
            body["unblock_filing_status"] = filing_status
            body["unblock_routing_strategy"] = routing_strategy
            gate_resp = Response.json(body, status=400)
        return gate_resp

    # === PR 7i pre-lock status guards ===
    # Pure-on-input checks (no goal state needed) fail fast before the lock.
    # Guards that need goal.recurring / goal.status remain inside the lock.
    #
    # Superseded direct-set block (mirror of cmd_update_goal lines 1835-1840).
    # superseded transitions go through the intent-satisfaction evidence gate
    # in aspirations-complete-intent.sh — never via direct update-goal.
    if field == "status" and value == "superseded":
        return Response.error(
            400, "invalid_status_transition",
            f"Cannot set status=superseded directly on {goal_id}. Use "
            f"aspirations-complete-intent.sh <asp-id> with intent_satisfaction "
            f"JSON listing this goal in superseded_goal_ids.",
        )

    # X-Mind-Blocker-Ref parsing for status=blocked writes. The header is
    # request-level (not goal-state), so we parse + validate it pre-lock and
    # carry the result into the lock for the requirement check and the
    # persist cascade. blocker_ref_for_blocked_status is None when no header
    # was supplied; the in-lock requirement check then looks for alternative
    # evidence (existing blocker_ref or non-empty blocked_by).
    blocker_ref_for_blocked_status: Optional[Dict[str, Any]] = None
    if field == "status" and value == "blocked":
        ref_header = _header_override(ctx, "X-Mind-Blocker-Ref")
        if ref_header:
            ok, parsed = _validate_blocker_ref(ref_header)
            if not ok:
                return Response.error(
                    400, "blocker_ref_invalid",
                    f"--blocker-ref validation failed on {goal_id}: {parsed}",
                )
            blocker_ref_for_blocked_status = parsed

    # Warnings collected during the write — surfaced on the 200 response so
    # wrappers can re-emit to stderr (matches add_goal). Pure-logic advisories
    # only here; gates that block live in _run_update_goal_gates above.
    warnings: List[str] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_goal(items, goal_id)
            if found is None:
                return Response.error(404, "goal_not_found",
                                      f"Goal {goal_id} not found in any aspiration ({source})")
            asp_idx, goal_idx, asp = found

            # Validate against a candidate state BEFORE mutating the canonical
            # record. _read_jsonl currently returns a fresh list, so a mutate-
            # then-error-return is safe — items goes out of scope undisturbed.
            # If _read_jsonl ever switches to returning the cached reference
            # (jsonl_cache.get explicitly returns the shared copy by contract),
            # the old mutate-first form would leak partial state into the
            # cache on the early-return path. Validating first removes that
            # entire failure mode regardless of which read shape is in use.
            if field == "status":
                candidate = dict(asp["goals"][goal_idx])
                candidate[field] = value
                try:
                    _validate_goal(candidate)
                except ValueError as e:
                    return Response.error(400, "validation_failed", str(e))

            goal = asp["goals"][goal_idx]

            # Prose-verification-drift on description / verification edits
            # (4): the add path validates via _assert_no_prose_drift,
            # but the description / verification edit path does not — catch
            # post-add prose injection here. Build the post-write candidate
            # (new value overlaid on the current goal) and re-use the shared
            # gate so a goal whose description gains the markers without a
            # structured verification.checks is rejected. Scoped to these two
            # fields so status / other edits on a legacy drift goal are not
            # retroactively blocked.
            if field in ("description", "verification"):
                candidate = dict(goal)
                candidate[field] = value
                pv = _prose_verification_eval(
                    candidate, meta_dir=ctx.paths.meta,
                    agent_name=ctx.paths.agent_name or None,
                )
                if pv["would_block"]:
                    return Response.error(400, "validation_failed", pv["message"])

            # === PR 7i in-lock status guards ===
            # Guards that need goal state run AFTER the goal load and BEFORE
            # any mutation. Order mirrors cmd_update_goal: recurring-completed
            # first (cheapest check), blocker-ref requirement second.
            #
            # Recurring + completed block (mirror of cmd_update_goal lines
            # 1779-1784). Recurring goals run perpetually — closing one as
            # completed is LLM drift. Use complete-by for cycle tracking, or
            # set recurring=false first to permanently retire it.
            if (field == "status" and value == "completed"
                    and goal.get("recurring")):
                return Response.error(
                    400, "invalid_status_transition",
                    f"Cannot set status=completed on recurring goal {goal_id}. "
                    f"Recurring goals stay 'pending'. Use complete-by for cycle "
                    f"tracking, or set recurring=false first to permanently "
                    f"stop it.",
                )

            # status=blocked blocker_ref requirement (mirror of cmd_update_goal
            # lines 2018-2050). Fires only on TRANSITION into blocked (current
            # status != blocked) — idempotent re-writes are no-op. Accepted
            # evidence (any one passes):
            #   1. blocker_ref_for_blocked_status from X-Mind-Blocker-Ref
            #      header (parsed + validated pre-lock above)
            #   2. existing goal.blocker_ref from a prior defer_reason write
            #   3. non-empty goal.blocked_by (goal-chain dependencies are
            #      their own evidence)
            #
            # Without this, status=blocked writes bypass the schema entirely
            # and dependent goals stay blocked long after their actual blocker
            # resolves ( /  found 2026-04-24).
            if (field == "status" and value == "blocked"
                    and goal.get("status") != "blocked"):
                has_existing = goal.get("blocker_ref") is not None
                has_blocked_by = bool(goal.get("blocked_by"))
                if (blocker_ref_for_blocked_status is None
                        and not (has_existing or has_blocked_by)):
                    return Response.error(
                        400, "blocker_ref_required_for_blocked_status",
                        f"status=blocked on {goal_id} requires blocker evidence. "
                        f"This goal has no existing blocker_ref and no "
                        f"blocked_by entries. Pass X-Mind-Blocker-Ref with a "
                        f"JSON payload, set blocked_by first (non-empty list), "
                        f"or ensure blocker_ref is already populated via a "
                        f"prior defer_reason write.",
                    )

            # Pre-completion artifact-existence gate (, 2026-05-14).
            # Mirror of aspirations.py cmd_update_goal lines 1849-1907.
            # Action-prefix goals that reference concrete file paths in
            # title/description must have those files on disk at close
            # time. Fires BEFORE the (pre-lock) uncommitted-work gate by
            # virtue of running in-lock; in practice the two are mutually
            # exclusive (missing artifact ≠ uncommitted artifact). Override
            # header X-Mind-Override-Missing-Artifact logs to
            # world/missing-artifact-overrides.jsonl. In-process call
            # (pure regex + file-existence checks, no subprocess) so safe
            # inside the lock.
            if field == "status" and value == "completed":
                ca_override = _header_override(
                    ctx, "X-Mind-Override-Missing-Artifact")
                ca_result = _completion_artifact_eval(
                    goal_id=goal_id,
                    goal_title=goal.get("title", "") or "",
                    goal_description=goal.get("description", "") or "",
                    override=ca_override,
                    project_root=ctx.paths.project_root,
                    world_dir=ctx.paths.world,
                    meta_dir=ctx.paths.meta,
                    agent_name=ctx.paths.agent_name or "",
                )
                if ca_result.get("would_block"):
                    return Response.json({
                        "error": "missing_artifact_blocked",
                        "gate": "completion-artifact-gate",
                        "gate_output": ca_result,
                    }, status=400)

            # === Pre-mutation advisories (PR 7h) ===
            # Read pre-update state for advisories whose result depends on
            # the existing record (e.g., user_leg_scope on the current goal,
            # not the incoming write). Must run BEFORE goal[field] = value so
            # the gate sees the "did this write leave user_leg_scope unset?"
            # question with the correct pre-update reference.
            #
            # field=participants → user_leg_scope advisory.
            # Mirror of cmd_update_goal lines 1775-1776. Pure evaluate() with
            # no I/O — safe inside the lock; runtime is microseconds. Warning
            # text is identical to legacy CLI (gate is single source of truth).
            if field == "participants":
                uls_result = _user_leg_scope_eval(
                    goal_id=goal_id,
                    participants=value,
                    user_leg_scope=goal.get("user_leg_scope"),
                )
                if uls_result["warned"]:
                    warnings.append(uls_result["message"])

            # Capture old_status BEFORE the mutation — the selection_count
            # cascade compares old vs new to stay idempotent on redundant
            # in-progress writes. Moving this read below `goal[field] = value`
            # would inflate selection_count on every resume/retry of the same
            # in-progress goal (mirror of cmd_update_goal line 2080 invariant).
            old_status = goal.get("status")
            # 9: capture pre-update interval_hours BEFORE the write so the
            # anchor-persist cascade below records the ORIGINAL cadence, not the
            # incoming (possibly already-extended) value. Mirror of cmd_update_goal.
            _prev_interval_hours = goal.get("interval_hours")

            goal[field] = value
            # asp is items[asp_idx] (same dict reference); mutation above
            # already persists. No rebind needed.

            # 9 (unbounded interval-ratchet fix — DAEMON MIRROR of the
            # aspirations.py cmd_update_goal anchor-persist; guard-742 byte-parallel).
            # The LIVE batch-calibrate and manual apply paths flow through THIS daemon
            # endpoint and never persisted original_interval_hours, so cargo-cult-
            # detector's 3x cap read orig=None, treated the already-extended value as
            # "original", and ratcheted UNBOUNDED ( root-cause, zeta 2026-07-12).
            # Persist the anchor here (the single daemon write site) to the PRE-update
            # cadence when absent; skip a fresh goal's first interval set (_prev None/0);
            # no-op when the anchor already exists (update_interval_hours wrote it first).
            if (
                field == "interval_hours"
                and goal.get("original_interval_hours") in (None, "")
                and isinstance(_prev_interval_hours, (int, float))
                and not isinstance(_prev_interval_hours, bool)
                and _prev_interval_hours > 0
            ):
                goal["original_interval_hours"] = _prev_interval_hours

            # === Cascade mutations (PR 7e/3) ===
            # All cascades happen INSIDE the lock so a concurrent read sees
            # either pre-write or fully-cascaded state — never a partial
            # mutation. Cascade order is load-bearing: stamp last_modified
            # FIRST so consumers reading any of the auto-managed fields can
            # rely on a single timestamp moment per write.

            # 1. last_modified — stamped on every successful write.
            # A general last-touched timestamp (originally added for the
            # stale-read gate, retired 7). Single timestamp per call;
            # auto-cascades below share this moment (defer_reason_set_at,
            # blocker_ref, etc.).
            goal["last_modified"] = datetime.now().isoformat(timespec="seconds")

            # 2. defer_reason cascade.
            # SET path: stamp defer_reason_set_at, auto-pair deferred_until
            #           from narrative date extraction, persist validated
            #           blocker_ref from header.
            # CLEAR path: clear defer_reason_set_at, drop blocker_ref so
            #             quiescence-gate never sees an orphan ref on an
            #             un-deferred goal.
            if field == "defer_reason":
                if value not in (None, ""):
                    goal["defer_reason_set_at"] = (
                        datetime.now().isoformat(timespec="seconds"))
                    # Auto-pair narrative defer_reason with structured
                    # deferred_until. Skipped when caller-supplied
                    # deferred_until is already set (caller wins).
                    if not goal.get("deferred_until"):
                        extracted = _extract_defer_date(str(value))
                        if extracted.get("matched"):
                            goal["deferred_until"] = extracted["deferred_until"]
                            # Audit trail (PR 7j housekeeping). Mirror of
                            # aspirations.py line 2166 — every narrative→
                            # deferred_until conversion writes one record to
                            # world/defer-date-extractions.jsonl so a reviewer
                            # can spot mis-extractions. Without this the
                            # audit was silent on the daemon hot path. Best-
                            # effort: never blocks the defer write.
                            _log_defer_date_extraction(
                                ctx, goal_id, value, extracted)
                    # Persist validated blocker_ref alongside defer_reason.
                    # normalized_ref is None when the caller used a
                    # structured-prefix defer (gate didn't fire) or used
                    # the unstructured-defer override.
                    if normalized_ref is not None:
                        goal["blocker_ref"] = normalized_ref
                else:
                    goal["defer_reason_set_at"] = None
                    # Clearing defer_reason drops its structured companion.
                    # Keep the pair consistent.
                    goal.pop("blocker_ref", None)

            # 3. recurring=false cascade (PR 7g).
            # Mirror of cmd_update_goal lines 2183-2185. When recurring flips
            # to falsy, interval_hours and lastAchievedAt MUST drop here so
            # goal-selector's `hours_since(lastAchievedAt) < interval_hours`
            # filter doesn't treat the dead goal as "not yet due" until the
            # next archive-sweep safety-net catches it. History fields
            # (achievedCount, currentStreak, longestStreak) are preserved as
            # factual record.
            if field == "recurring" and not value:
                goal.pop("interval_hours", None)
                goal.pop("lastAchievedAt", None)

            # 4. blocked_by → blocked_since auto-management (PR 7g).
            # Mirror of cmd_update_goal lines 2188-2193. blocked_since is the
            # "how long has this been blocked?" signal consumed by proactive
            # escalation and defer-recheck. Without this, blocked_by writes
            # leave blocked_since null and age-based sweeps miss the goal.
            # parse_value at the daemon contract already converted "[]" → [],
            # so `if value` (truthy = non-empty list) suffices.
            if field == "blocked_by":
                if value:
                    if not goal.get("blocked_since"):
                        goal["blocked_since"] = (
                            datetime.now().isoformat(timespec="seconds"))
                else:
                    goal["blocked_since"] = None

            # === PR 7i status cascades ===
            # All five fire on field == "status". Order mirrors
            # cmd_update_goal lines 2100-2200 so anyone reading both files
            # sees them line up.

            # 5. selection_count + last_selected bump (mirror of lines
            # 2100-2106). Fires on the TRANSITION into in-progress so
            # selection_count surfaces "which aspirations have actually been
            # worked on" without inflation from resume/retry writes. The
            # idempotency guard (old_status != "in-progress") is load-bearing.
            if (field == "status" and value == "in-progress"
                    and old_status != "in-progress"):
                asp["selection_count"] = int(
                    asp.get("selection_count", 0) or 0) + 1
                asp["last_selected"] = datetime.now().isoformat(timespec="seconds")

            # 6. completed_at auto-stamp on terminal status (mirror of lines
            # 2113-2118). Idempotent: only stamps when None — legitimate
            # back-stamps from aspirations-complete-by or external backfill
            # are preserved.
            if (field == "status" and value in _TERMINAL_GOAL_STATUSES
                    and goal.get("completed_at") is None):
                goal["completed_at"] = datetime.now().isoformat(timespec="seconds")

            # 6b. completed_by auto-stamp on completion (2). Daemon
            # mirror of the CLI completed_by stamp (aspirations.py cmd_update_goal,
            # right after its completed_at stamp). The completion chokepoint:
            # every non-recurring status->completed flows here (recurring is
            # blocked above). Pre-fix only ~11% of completed world goals carried
            # completed_by, so agent-attribution audits + the cross_queue
            # graduation count (0) undercounted. Scoped to
            # value=="completed"; agent from _agent_name(ctx) (the per-request
            # caller — NOT the daemon's env, which the CLI sibling uses).
            # Idempotent: only when unset, preserving complete-by / backfill.
            if (field == "status" and value == "completed"
                    and not goal.get("completed_by") and agent):
                goal["completed_by"] = agent

            # 7. blocker_ref persist for status=blocked (mirror of lines
            # 2131-2136). The pre-lock header parse staged the validated ref
            # in blocker_ref_for_blocked_status; persist it under the canonical
            # key so goal-selector, quiescence-gate, and aspirations-precheck
            # Phase 0.5b all read consistent payload regardless of which
            # entry point transitioned the goal to blocked.
            if (field == "status" and value == "blocked"
                    and blocker_ref_for_blocked_status is not None):
                goal["blocker_ref"] = blocker_ref_for_blocked_status

            # 8. blocked_since auto-stamp on status=blocked (mirror of lines
            # 2144-2145). Matches the blocked_by → blocked_since cascade
            # pattern: stamp only when previously unset, never overwrite.
            if (field == "status" and value == "blocked"
                    and not goal.get("blocked_since")):
                goal["blocked_since"] = datetime.now().isoformat(timespec="seconds")

            # 9. Terminal-status cleanup (mirror of lines 2197-2200).
            # When a goal hits a terminal status: (a) every other goal
            # listing it in blocked_by must drop the reference (otherwise
            # dependents stay blocked forever); (b) the claim — if any —
            # is cleared per convention Rule 3.
            if field == "status" and value in _TERMINAL_GOAL_STATUSES:
                _clear_stale_blockers_inline(items, {goal_id})
                goal.pop("claimed_by", None)
                goal.pop("claimed_at", None)

            # 10. recompute_progress (mirror of line 2201). Fires on EVERY
            # successful write, not just status. For non-status writes this
            # is a no-op recompute (same goals[] in, same progress out) but
            # is cheap (linear over goal count) so the parity with legacy is
            # worth more than the micro-opt. asp is items[asp_idx] (same
            # dict reference) so the mutation persists into items.
            _recompute_progress(asp)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"update-goal {goal_id} {field}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"update-goal {goal_id} {field}",
                             lines_changed=len(items))
            # Invalidate while the lock is held — same rationale as add_goal:
            # close the eventual-consistency window on the write-then-cache-
            # flip sequence. See jsonl-read-modify-write-race.
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence for critical transitions (9;
    # siblings: add_goal 8, claim 6). Runs BEFORE the E9
    # post-lock hook and the success return — a swallowed status/defer_reason
    # PUT is the 1 time-travel shape; detect it in THIS writing
    # iteration, not 7h later at the next re-sync. Conservative fail-open
    # (see _verify_transition_persisted): a real success can never become a
    # false failure.
    if field in _CRITICAL_TRANSITION_FIELDS:
        expected = {field: goal.get(field)}
        if field == "status" and value in _TERMINAL_GOAL_STATUSES:
            expected["claimed_by"] = None  # step 9 popped the claim in-lock
        if not _verify_transition_persisted(live_path, asp["id"], goal_id,
                                            expected):
            import sys
            print(f"[daemon update_goal] WRITE-LOSS DETECTED: {field} "
                  f"transition on {goal_id} returned success-shaped but the "
                  f"authoritative store does not carry it (own-cloud silent "
                  f"write-loss, g-115-2429)", file=sys.stderr)
            return Response.error(
                500, "update_not_persisted",
                f"update-goal {goal_id} {field} did not persist to the "
                f"authoritative store (own-cloud write-loss, g-115-2429); "
                f"retry the update")

    # === PR 7i post-lock E9 skip observation ===
    # Fires AFTER the lock releases so the wm-append doesn't hold
    # aspirations.jsonl. Mirror of cmd_update_goal lines 2210-2211 + the
    # _emit_e9_skip_observation hook. Skip rationale is sometimes tree-worthy
    # ("X became moot because Y replaced it"); without this hook the rationale
    # dies on the goal record alone. Fail-open: any subprocess error logs to
    # stderr but never affects the 200 response.
    if field == "status" and value in ("skipped", "expired"):
        _emit_e9_skip_observation(ctx, goal_id, value, goal)

    response_body: Dict[str, Any] = {
        "ok": True,
        "goal_id": goal_id,
        "aspiration_id": asp["id"],
        "field": field,
        "source": source,
        # Full persisted goal — wrappers print this to stdout to match the
        # legacy CLI's `json.dumps(goal, indent=2)` output. Includes the
        # in-lock cascades (last_modified, defer_reason_set_at, deferred_until
        # auto-pair, blocker_ref persist/drop) so the caller sees what
        # actually landed on disk. Mirrors add_goal's response shape.
        "goal": goal,
    }
    # Surface advisory warnings on 200 — wrappers re-emit to stderr to match
    # the legacy CLI experience. Same shape as add_goal's response.
    if warnings:
        response_body["warnings"] = warnings
    return Response.json(response_body)


# ---------------------------------------------------------------------------
# PR 9a — `complete` endpoint (POST /v1/aspirations/complete)
# ---------------------------------------------------------------------------
#
# Mirror of aspirations.py::cmd_complete. The helpers below are
# `complete`-specific; they DO NOT duplicate `_clear_stale_blockers_inline`
# or `_recompute_progress` defined earlier in this module — both are
# reused as the single source of truth for that behavior.


def _find_recurring_goals(asp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return goals with recurring=true in an aspiration."""
    return [g for g in asp.get("goals", []) if g.get("recurring")]


def _find_unfinished_goals(asp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return non-recurring goals not in a terminal status."""
    return [g for g in asp.get("goals", [])
            if not g.get("recurring") and g.get("status") not in _TERMINAL_GOAL_STATUSES]


def _find_shape_recurring_corrupted(asp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return goals with recurring-shape fields (interval_hours +
    lastAchievedAt) but recurring=false AND status=completed.

    Mirrors aspirations.py — these goals would otherwise drift into archive
    via the completed-status path while still carrying recurring metadata.
    """
    return [g for g in asp.get("goals", [])
            if g.get("status") == "completed"
            and not g.get("recurring")
            and g.get("interval_hours")
            and g.get("lastAchievedAt")]


def _normalize_terminal_goals_in(asp: Dict[str, Any]) -> None:
    """Clear defer/blocker state on terminal-status goals. Idempotent.

    Mirrors aspirations.py _normalize_terminal_goals_in for a single asp.
    """
    for g in asp.get("goals", []) or []:
        if g.get("status") in _TERMINAL_GOAL_STATUSES:
            # completed_at stamp
            if g.get("status") == "completed" and "completed_at" not in g:
                g["completed_at"] = datetime.now().isoformat(timespec="seconds")
            # Clear defer/blocker state
            for key in ("defer_reason", "defer_reason_set_at", "deferred_until",
                        "blocked_by", "blocked_since", "blocker_ref"):
                if key in g:
                    g[key] = None


def _motivation_tokens(text: str) -> set:
    """Lowercase alphanumeric tokens of >=4 chars from a string."""
    if not text:
        return set()
    return {t for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if len(t) >= 4}


def _load_intent_satisfaction_config(project_root: Path) -> Dict[str, Any]:
    """Load intent_satisfaction config from core/config/aspirations.yaml."""
    import yaml
    cfg_path = project_root / "core" / "config" / "aspirations.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg["intent_satisfaction"]


def _load_streak_mult_config(project_root: Path) -> float:
    """Load recurring.streak_mult from core/config/aspirations.yaml.

    Single source of truth for the "streak reset" multiplier — the same
    constant that defines (a) the elapsed window past which a recurring
    goal's currentStreak resets to 1 (this function's reader, line ~2206),
    AND (b) the "recovered cadence" envelope used by
    core/scripts/streak-break-reflector.py _auto_resolve_recovered_canaries
    to close transient session-gap canaries. Sync is now structural —
    both readers pull from this key, so a drift between them is
    impossible (g-115-929, 2026-05-18).

    Fail-open: any read/parse error falls back to 2.0 (the historical
    literal). Bounded by `modifiable.recurring.streak_mult: {min: 1.5,
    max: 5.0}` in aspirations.yaml.
    """
    import yaml
    cfg_path = project_root / "core" / "config" / "aspirations.yaml"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        v = (cfg.get("recurring") or {}).get("streak_mult")
        if v is None:
            return 2.0
        return float(v)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return 2.0


def _validate_intent_satisfaction(
    asp: Dict[str, Any],
    intent_block: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """Validate an intent_satisfaction block against an aspiration.

    Returns (ok, error_message). Does NOT mutate the aspiration.
    Mirrors aspirations.py _validate_intent_satisfaction.
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

    # Build goal lookup
    goals_by_id = {g.get("id"): g for g in asp.get("goals", [])}
    non_recurring = [g for g in asp.get("goals", []) if not g.get("recurring")]

    # Evidence cardinality
    scope = asp.get("scope", "project")
    scope_min = config["min_evidence_by_scope"].get(scope, 3)
    required = max(scope_min, math.ceil(0.5 * len(non_recurring)))
    if len(ev_ids) < required:
        return False, (
            f"evidence_goal_ids has {len(ev_ids)}, scope={scope} requires "
            f">={required} (max of {scope_min}-by-scope and "
            f"ceil(0.5 * {len(non_recurring)} non-recurring))"
        )

    # Evidence quality
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
            return False, f"evidence goal {gid} has no verification.outcomes"

    # Superseded goals validation
    for gid in sup_ids:
        g = goals_by_id.get(gid)
        if g is None:
            return False, f"superseded goal {gid} not in aspiration {asp.get('id')}"
        if g.get("recurring"):
            return False, f"superseded goal {gid} is recurring; recurring goals cannot be superseded"
        if g.get("status") in _TERMINAL_GOAL_STATUSES:
            return False, f"superseded goal {gid} already terminal (status={g.get('status')})"

    # After supersession completeness
    sup_set = set(sup_ids)
    remaining_unfinished = [
        g for g in non_recurring
        if g.get("status") not in _TERMINAL_GOAL_STATUSES
        and g.get("id") not in sup_set
    ]
    if remaining_unfinished:
        ids = ", ".join(g.get("id", "?") for g in remaining_unfinished)
        return False, (
            f"after supersession, these non-recurring goals would still be unfinished: {ids}. "
            f"Add them to superseded_goal_ids or complete them first."
        )

    # Rationale length
    if len(rationale) < 40:
        return False, f"rationale too short ({len(rationale)} chars); need >=40"

    # Motivation overlap
    motivation = asp.get("motivation") or ""
    mtokens = _motivation_tokens(motivation)
    if not mtokens:
        return False, (
            f"aspiration {asp.get('id')} has no motivation; "
            f"intent-satisfaction requires a non-empty motivation"
        )
    rtokens = _motivation_tokens(rationale)
    if not (mtokens & rtokens):
        return False, (
            f"rationale shares no tokens with motivation; "
            f"quote the motivation text explicitly. "
            f"Motivation tokens: {sorted(mtokens)[:10]}..."
        )

    return True, None


def _append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    """Append a single record to a JSONL file (for archive writes)."""
    assert_not_cruft(path.parent, "mkdir (_append_jsonl)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


def complete(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/complete?asp_id=<a>&source=<world|agent>

    Mirrors aspirations.py cmd_complete:
      1. Parse params (asp_id, source, force, intent_satisfied + body)
      2. Lock → read → guards → archive → remove → write → unlock
      3. Return completed aspiration JSON

    Query params:
      asp_id   (required) — aspiration ID (asp-NNN)
      source   (optional, default "world") — "world" or "agent"
      force    (optional, default "false") — skip recurring/unfinished guards

    Headers:
      X-Mind-Agent — agent name for history/changelog

    Body (JSON, optional):
      When intent_satisfied=true in query, body is the intent_satisfaction
      block: {evidence_goal_ids, rationale, superseded_goal_ids}
    """
    from ..server import Response

    # --- Parse query params ---
    asp_id = (ctx.query.get("asp_id") or "").strip()
    if not asp_id:
        return Response.error(400, "missing_asp_id", "asp_id query param is required")
    if not _ASP_ID_RE.match(asp_id):
        return Response.error(400, "invalid_asp_id", f"Invalid aspiration ID: {asp_id}")

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", f"source must be 'world' or 'agent', got '{source}'")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    force = (ctx.query.get("force") or "").strip().lower() in ("true", "1", "yes")
    intent_satisfied = (ctx.query.get("intent_satisfied") or "").strip().lower() in ("true", "1", "yes")

    agent = _agent_name(ctx)
    live_path, base_dir = _resolve_paths(ctx, source)
    archive_path = base_dir / "aspirations-archive.jsonl"

    # Parse intent_satisfaction body BEFORE lock (mirrors CLI stdin read)
    intent_block = None
    if intent_satisfied:
        try:
            intent_block = _parse_body_json(ctx.body)
        except (ValueError, json.JSONDecodeError) as e:
            return Response.error(400, "invalid_body",
                                  f"intent_satisfied requires JSON body: {e}")
        if not isinstance(intent_block, dict):
            return Response.error(400, "invalid_body",
                                  "intent_satisfaction body must be a JSON object")

    warnings: List[str] = []

    # --- Lock + read-modify-write ---
    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            result = _find_aspiration(items, asp_id)
            if result is None:
                return Response.error(404, "aspiration_not_found",
                                      f"Aspiration {asp_id} not found")
            idx, asp = result

            # Guard: refuse recurring goals (unless force)
            recurring = _find_recurring_goals(asp)
            if recurring and not force:
                rg_ids = ", ".join(g["id"] for g in recurring)
                return Response.error(400, "recurring_goals_present",
                    f"{asp_id} contains {len(recurring)} recurring goal(s): {rg_ids}. "
                    f"Recurring goals run perpetually and must not be archived. "
                    f"Set recurring=false on goals to stop, or use force=true.")

            # Intent-satisfied pathway
            if intent_block is not None:
                config = _load_intent_satisfaction_config(ctx.paths.project_root)
                ok, err = _validate_intent_satisfaction(asp, intent_block, config)
                if not ok:
                    return Response.error(400, "intent_validation_failed", err)
                # Stamp and persist
                intent_block = dict(intent_block)
                intent_block["claimed_at"] = datetime.now().isoformat(timespec="seconds")
                asp["intent_satisfaction"] = intent_block
                # Transition superseded goals
                sup_set = set(intent_block["superseded_goal_ids"])
                for g in asp.get("goals", []):
                    if g.get("id") in sup_set:
                        g["status"] = "superseded"
                        g["superseded_by_aspiration"] = asp["id"]
                _recompute_progress(asp)

            # Guard: refuse unfinished goals (unless force)
            unfinished = _find_unfinished_goals(asp)
            if unfinished and not force:
                uf_summary = "; ".join(
                    f"{g['id']} ({g.get('status', '?')})" for g in unfinished)
                return Response.error(400, "unfinished_goals_present",
                    f"{asp_id} has {len(unfinished)} unfinished goal(s): {uf_summary}. "
                    f"All non-recurring goals must be terminal before archival. "
                    f"Use force=true to override, or intent_satisfied=true.")

            # Maturity warning (advisory, not blocking)
            scope = asp.get("scope", "project")
            sessions_active = asp.get("sessions_active", 0)
            min_sessions_map = {"sprint": 1, "project": 2, "initiative": 4}
            min_sessions = min_sessions_map.get(scope, 2)
            if sessions_active < min_sessions and scope != "sprint":
                warnings.append(
                    f"MATURITY WARNING: {asp['id']} completing after {sessions_active} "
                    f"session(s) but scope={scope} expects {min_sessions}. "
                    f"Consider adding depth goals.")

            # Mark completed + archived
            asp["status"] = "completed"
            asp["completed_at"] = datetime.now().strftime("%Y-%m-%d")
            asp["archived"] = True

            archived_goal_ids = {g["id"] for g in asp.get("goals", [])}

            # Normalize terminal goals before archive (clears stale defer state)
            _normalize_terminal_goals_in(asp)

            # Archive BEFORE removing from live — crash safety:
            # aspiration exists in both (benign) rather than neither (data loss).
            _append_jsonl(archive_path, asp)

            # Remove from live
            items.pop(idx)

            # Clean up blocked_by references to archived goals — reuses the
            # SSoT helper defined alongside update_goal (single definition,
            # both endpoints call it).
            _clear_stale_blockers_inline(items, archived_goal_ids)

            # Write live file
            history.snapshot(live_path, base_dir, agent,
                             summary=f"complete {asp_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"complete {asp_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "aspiration": asp,
        "warnings": warnings if warnings else None,
    })


_MAX_RECENT_COMPLETIONS = 50


def complete_intent(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/complete-intent?asp_id=<a>&source=<world|agent>

    Dedicated intent-satisfaction completion endpoint. Always requires the
    intent_satisfaction JSON body — unlike ``complete`` which accepts it as
    an optional ``intent_satisfied=true`` query param.

    Mirrors the intent-satisfied branch of ``complete`` but:
      - No ``force`` flag (intent pathway handles its own guards)
      - No ``intent_satisfied`` query param (always on)
      - Body is REQUIRED (400 on empty/missing)

    Query params:
      asp_id   (required) — aspiration ID (asp-NNN)
      source   (optional, default "world") — "world" or "agent"

    Headers:
      X-Mind-Agent — agent name for history/changelog

    Body (JSON, required):
      {
        "evidence_goal_ids":   ["g-XXX-NN", ...],
        "rationale":           ">=40 chars, must overlap motivation text",
        "superseded_goal_ids": ["g-XXX-NN", ...]
      }
    """
    from ..server import Response

    # --- Parse query params ---
    asp_id = (ctx.query.get("asp_id") or "").strip()
    if not asp_id:
        return Response.error(400, "missing_asp_id", "asp_id query param is required")
    if not _ASP_ID_RE.match(asp_id):
        return Response.error(400, "invalid_asp_id", f"Invalid aspiration ID: {asp_id}")

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", f"source must be 'world' or 'agent', got '{source}'")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    agent = _agent_name(ctx)
    live_path, base_dir = _resolve_paths(ctx, source)
    archive_path = base_dir / "aspirations-archive.jsonl"

    # Parse intent_satisfaction body (REQUIRED for this endpoint)
    try:
        intent_block = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body",
                              f"JSON body required: {e}")
    if not isinstance(intent_block, dict):
        return Response.error(400, "invalid_body",
                              "body must be a JSON object")

    warnings: List[str] = []

    # --- Lock + read-modify-write ---
    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            result = _find_aspiration(items, asp_id)
            if result is None:
                return Response.error(404, "aspiration_not_found",
                                      f"Aspiration {asp_id} not found")
            idx, asp = result

            # Guard: refuse recurring goals
            recurring = _find_recurring_goals(asp)
            if recurring:
                rg_ids = ", ".join(g["id"] for g in recurring)
                return Response.error(400, "recurring_goals_present",
                    f"{asp_id} contains {len(recurring)} recurring goal(s): {rg_ids}. "
                    f"Recurring goals run perpetually and must not be archived.")

            # Intent-satisfied validation
            config = _load_intent_satisfaction_config(ctx.paths.project_root)
            ok, err = _validate_intent_satisfaction(asp, intent_block, config)
            if not ok:
                return Response.error(400, "intent_validation_failed", err)

            # Stamp and persist
            intent_block = dict(intent_block)
            intent_block["claimed_at"] = datetime.now().isoformat(timespec="seconds")
            asp["intent_satisfaction"] = intent_block

            # Transition superseded goals
            sup_set = set(intent_block["superseded_goal_ids"])
            for g in asp.get("goals", []):
                if g.get("id") in sup_set:
                    g["status"] = "superseded"
                    g["superseded_by_aspiration"] = asp["id"]
            _recompute_progress(asp)

            # Maturity warning (advisory, not blocking)
            scope = asp.get("scope", "project")
            sessions_active = asp.get("sessions_active", 0)
            min_sessions_map = {"sprint": 1, "project": 2, "initiative": 4}
            min_sessions = min_sessions_map.get(scope, 2)
            if sessions_active < min_sessions and scope != "sprint":
                warnings.append(
                    f"MATURITY WARNING: {asp['id']} completing after {sessions_active} "
                    f"session(s) but scope={scope} expects {min_sessions}. "
                    f"Consider adding depth goals.")

            # Mark completed + archived
            asp["status"] = "completed"
            asp["completed_at"] = datetime.now().strftime("%Y-%m-%d")
            asp["archived"] = True

            archived_goal_ids = {g["id"] for g in asp.get("goals", [])}

            # Normalize terminal goals before archive (clears stale defer state)
            _normalize_terminal_goals_in(asp)

            # Archive BEFORE removing from live — crash safety
            _append_jsonl(archive_path, asp)

            # Remove from live
            items.pop(idx)

            # Clean up blocked_by references to archived goals
            _clear_stale_blockers_inline(items, archived_goal_ids)

            # Write live file
            history.snapshot(live_path, base_dir, agent,
                             summary=f"complete-intent {asp_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"complete-intent {asp_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "aspiration": asp,
        "warnings": warnings if warnings else None,
    })


def _team_state_append_completion(world_dir: Path, record: dict,
                                  agent: str) -> Optional[str]:
    """Append a completion record to team-state.yaml recent_completions[].

    Uses locked_modify_yaml directly in-process (saves ~400ms subprocess
    overhead vs shelling out to team-state.py). Returns None on success,
    error string on failure. Ring buffer enforced at MAX_RECENT_COMPLETIONS.
    """
    from _fileops import locked_modify_yaml
    import yaml  # noqa: F811 — local to this rarely-called path

    ts_path = world_dir / "team-state.yaml"
    try:
        def _modifier(state):
            if "recent_completions" not in state:
                state["recent_completions"] = []
            state["recent_completions"].append(record)
            state["recent_completions"] = \
                state["recent_completions"][-_MAX_RECENT_COMPLETIONS:]
            state["last_updated"] = datetime.now().isoformat(timespec="seconds")
            state["last_updated_by"] = agent
            return state

        locked_modify_yaml(ts_path, _modifier, initial={})
        return None
    except Exception as e:
        return str(e)


def _recent_break_actuals(log_path: Path, goal_id: str,
                          window: int = 5) -> List[float]:
    """Last `window` actual_elapsed_hours for goal_id from streak-breaks.jsonl.

    Read-side twin of cargo-cult-detector.py::_recent_actual_cadence (same
    file, same field, same skip-corrupt-lines posture). Fail-open: a missing
    or unreadable file returns [] — the canary basis falls back to the raw
    interval, so a broken read can only make the canary fire MORE, never
    silently suppress it.
    """
    vals: List[float] = []
    try:
        if not log_path.exists():
            return []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("goal_id") != goal_id:
                    continue
                v = rec.get("actual_elapsed_hours")
                if isinstance(v, (int, float)) and v > 0:
                    vals.append(float(v))
    except OSError:
        return []
    return vals[-window:]


def _streak_break_canary_fields(interval: float, elapsed: float,
                                streak_mult: float,
                                recent_actuals: List[float],
                                signal_gated: bool,
                                min_samples: int = 3) -> Dict[str, Any]:
    """Classify a streak-break emission as canary-worthy vs informational.

    g-115-2310 (g-115-2300 item b): the break RECORD is still emitted
    unconditionally on the raw interval basis — cargo-cult-detector's
    contract-suppression predicate reads actual_elapsed_hours from every
    record and must never starve. These fields only tell
    streak-break-reflector.py which records deserve an Investigate canary:

      canary=False — signal-gated source (fire_when set: interval_hours is
        vestigial, "late" has no meaning), or the recent ACTUAL cadence
        (p50 of the last N same-goal breaks) explains the elapsed window —
        the rb-1391 chronic-late class where a low-priority selection-gated
        goal's interval_hours << its selector-driven effective cadence, so
        a break records on nearly EVERY fire (g-001-01: 25/25).
      canary=True — elapsed exceeds streak_mult x max(interval, recent p50):
        late even by the goal's own demonstrated cadence. Real drift signal.

    Pure function — no I/O; unit-tested in
    core/scripts/tests/test_streak_break_canary_basis.py.
    """
    import statistics
    if signal_gated:
        return {"canary": False,
                "canary_basis_hours": None,
                "basis_reason": "signal_gated"}
    basis = interval
    reason = "interval"
    vals = [float(v) for v in (recent_actuals or [])
            if isinstance(v, (int, float)) and v > 0]
    if len(vals) >= min_samples:
        p50 = statistics.median(vals)
        if p50 > basis:
            basis = p50
            reason = "recent_actual_p50"
    return {"canary": bool(elapsed > streak_mult * basis),
            "canary_basis_hours": round(basis, 2),
            "basis_reason": reason}


def _emit_streak_break_signal_daemon(ctx, goal_id: str,
                                     interval: float,
                                     elapsed: float,
                                     aspiration_id: Optional[str],
                                     streak_mult: float = 2.0,
                                     signal_gated: bool = False) -> None:
    """Append a streak-break event to <agent>/session/streak-breaks.jsonl.

    Daemon-side mirror of aspirations.py::_emit_streak_break_signal (the CLI
    twin has no live caller since the daemon-only cutover; this is the sole
    emission site). Fail-silent so the recurring close path is never blocked.

    g-115-2310: emission is UNCONDITIONAL on the raw-interval break test the
    caller already ran — the record additionally carries canary/basis fields
    (see _streak_break_canary_fields) so the reflector can skip filing
    Investigates for informational breaks without starving the cargo-cult
    contract-suppression predicate that reads actual_elapsed_hours here.

    KNOWN RACE (accept-with-doc, g-115-1595): this append shares NO lock with
    the reflector's full-file rewrite (streak-break-reflector.py _write_signals,
    os.replace). An interleave can leave a signal `processed: false` whose
    canary was already filed. Impact is COSMETIC (48h dedup prevents duplicate
    canaries); a lock was deliberately NOT added. Full analysis + revisit
    trigger live in streak-break-reflector.py::_write_signals.
    """
    import sys
    session_dir = ctx.paths.agent / "session"
    assert_not_cruft(session_dir, "mkdir (streak-break session dir)")
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "streak-breaks.jsonl"
    canary_fields = _streak_break_canary_fields(
        interval, elapsed, streak_mult,
        _recent_break_actuals(log_path, goal_id), signal_gated)
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "goal_id": goal_id,
        "aspiration_id": aspiration_id,
        "expected_interval_hours": interval,
        "actual_elapsed_hours": round(elapsed, 2),
        "lateness_ratio": round(elapsed / interval, 2)
                          if interval > 0 else None,
        **canary_fields,
        "processed": False,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[daemon complete-by] WARN: streak-break signal failed: {e}",
              file=sys.stderr)


def complete_by(ctx):
    """POST /v1/aspirations/complete-by?goal_id=<g>&source=<s>[&agent_name=<a>][&key_finding=<k>]

    Mark a goal completed with agent attribution. Mirrors
    aspirations.py::cmd_complete_by exactly.
    """
    import sys
    from ..server import Response

    params = ctx.query
    goal_id = params.get("goal_id", "")
    if not goal_id:
        return Response.error(400, "missing_goal_id",
                              "goal_id query parameter is required")

    source = params.get("source", "world")
    agent = params.get("agent_name", "").strip() or _agent_name(ctx)
    # Reject flag-shaped or otherwise malformed agent names. Catches the
    # 2026-05-14 failure mode (.completed_by = '--completed-by'
    # literal). Symmetric with cmd_complete_by's _agent_name_type validator.
    if not _AGENT_NAME_RE.match(agent):
        return Response.error(400, "invalid_agent_name",
                              f"agent_name {agent!r} must match "
                              f"[a-z][a-z0-9_-]* (looks like a flag or empty)")
    key_finding = params.get("key_finding", "").strip() or None

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    live_path, base_dir = _resolve_paths(ctx, source)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    warnings: List[str] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            result = _find_goal(items, goal_id)
            if result is None:
                return Response.error(404, "goal_not_found",
                                      f"Goal {goal_id} not found")

            asp_idx, goal_idx, asp = result
            goal = asp["goals"][goal_idx]
            goal["completed_by"] = agent

            if goal.get("recurring"):
                # Recurring: cycle back to pending, update tracking fields.
                goal.pop("claimed_by", None)
                goal.pop("claimed_at", None)

                # Compute elapsed BEFORE updating lastAchievedAt
                interval = goal.get("interval_hours", 24)
                if "remind_days" in goal and "interval_hours" not in goal:
                    interval = goal["remind_days"] * 24
                elapsed = None
                la_str = goal.get("lastAchievedAt")
                if la_str:
                    try:
                        past = datetime.fromisoformat(str(la_str))
                        elapsed = (datetime.now() - past).total_seconds() / 3600.0
                    except (ValueError, TypeError):
                        pass

                goal["lastAchievedAt"] = now
                goal["achievedCount"] = goal.get("achievedCount", 0) + 1

                # Streak reset: missed interval (>streak_mult x) resets to 1.
                # streak_mult lives in core/config/aspirations.yaml
                # recurring.streak_mult — single source of truth shared with
                # core/scripts/streak-break-reflector.py
                # _auto_resolve_recovered_canaries (, 2026-05-18).
                streak_mult = _load_streak_mult_config(ctx.paths.project_root)
                streak_broken = (elapsed is not None
                                 and elapsed > streak_mult * interval)
                if streak_broken:
                    goal["currentStreak"] = 1
                else:
                    goal["currentStreak"] = goal.get("currentStreak", 0) + 1
                goal["longestStreak"] = max(
                    goal.get("longestStreak", 0), goal["currentStreak"])

                # Window streak
                window_mult = goal.get("windowStreakMultiplier", 7)
                window_broken = (elapsed is not None
                                 and elapsed > window_mult * interval)
                if window_broken:
                    goal["windowStreak"] = 1
                else:
                    goal["windowStreak"] = goal.get("windowStreak", 0) + 1
                goal["longestWindowStreak"] = max(
                    goal.get("longestWindowStreak", 0), goal["windowStreak"])

                # Streak-break signal (0: streak_mult + fire_when
                # pass through so the record's canary classification uses
                # the same knob as the break test above)
                if streak_broken:
                    _emit_streak_break_signal_daemon(
                        ctx, goal_id, interval, elapsed, asp.get("id"),
                        streak_mult=streak_mult,
                        signal_gated=("fire_when" in goal))

                goal["status"] = "pending"
            else:
                goal["status"] = "completed"
                goal["completed_date"] = now
                goal["completed_at"] = now
                goal.pop("claimed_by", None)
                goal.pop("claimed_at", None)

            _recompute_progress(asp)
            items[asp_idx] = asp
            _clear_stale_blockers_inline(items, {goal_id})

            history.snapshot(live_path, base_dir, agent,
                             summary=f"complete-by {goal_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"complete-by {goal_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence for complete-by (9): a
    # swallowed completion PUT re-serves the goal to the selector later (the
    # 1 reversion class applied to completions). Verify the final
    # in-memory state landed — status for one-shot goals, lastAchievedAt for
    # recurring (whose status stays pending by design). Runs BEFORE the
    # team-state cross-write so downstream records only persisted completions
    # (mirror of add_goal's audit ordering). Conservative fail-open.
    _cb_expected: Dict[str, Any] = {"status": goal.get("status")}
    if goal.get("lastAchievedAt") is not None:
        _cb_expected["lastAchievedAt"] = goal.get("lastAchievedAt")
    if not _verify_transition_persisted(live_path, asp["id"], goal_id,
                                        _cb_expected):
        import sys
        print(f"[daemon complete_by] WRITE-LOSS DETECTED: completion of "
              f"{goal_id} returned success-shaped but the authoritative "
              f"store does not carry it (own-cloud silent write-loss, "
              f"g-115-2429)", file=sys.stderr)
        return Response.error(
            500, "complete_not_persisted",
            f"complete-by for {goal_id} did not persist to the "
            f"authoritative store (own-cloud write-loss, g-115-2429); "
            f"retry the completion")

    # Team-state cross-write AFTER aspirations lock released.
    if key_finding:
        completion_record = {
            "goal_id": goal_id,
            "completed_by": agent,
            "completed_at": now,
            "key_finding": key_finding,
        }
        err = _team_state_append_completion(
            ctx.paths.world, completion_record, agent)
        if err:
            warnings.append(f"team-state append failed: {err}")

    return Response.json({
        "ok": True,
        "goal": goal,
        "warnings": warnings if warnings else None,
    })


def retire(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/retire?asp_id=<a>&source=<world|agent>[&force=true]"""
    from ..server import Response

    asp_id = (ctx.query.get("asp_id") or "").strip()
    if not asp_id:
        return Response.error(400, "missing_asp_id", "query parameter 'asp_id' required")

    source = (ctx.query.get("source") or "world").strip()
    force = (ctx.query.get("force") or "").lower() == "true"
    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)
    archive_path = base_dir / "aspirations-archive.jsonl"
    agent = _agent_name(ctx)
    warnings: List[str] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_aspiration(items, asp_id)
            if found is None:
                return Response.error(404, "aspiration_not_found",
                                      f"Aspiration {asp_id} not found")

            idx, asp = found

            recurring = _find_recurring_goals(asp)
            if recurring and not force:
                rg_ids = ", ".join(g["id"] for g in recurring)
                return Response.error(400, "recurring_goals_present",
                    f"{asp_id} contains {len(recurring)} recurring goal(s): {rg_ids}. "
                    f"Recurring goals run perpetually and must not be archived. "
                    f"Set recurring=false on goals to stop, or use force=true.")

            unfinished = _find_unfinished_goals(asp)
            if unfinished:
                uf_summary = "; ".join(
                    f"{g['id']} ({g.get('status', '?')})" for g in unfinished)
                warnings.append(
                    f"RETIREMENT NOTE: {asp_id} has {len(unfinished)} unfinished "
                    f"goal(s): {uf_summary}. Proceeding with retirement "
                    f"(abandonment is intentional).")

            asp["status"] = "retired"
            asp["completed_at"] = None
            asp["retired_at"] = datetime.now().strftime("%Y-%m-%d")
            asp["archived"] = True

            archived_goal_ids = {g["id"] for g in asp.get("goals", [])}
            _normalize_terminal_goals_in(asp)
            _append_jsonl(archive_path, asp)
            items.pop(idx)
            _clear_stale_blockers_inline(items, archived_goal_ids)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"retire {asp_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"retire {asp_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "aspiration": asp,
        "warnings": warnings if warnings else None,
    })


def release(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/release?id=<goal_id>&source=<world|agent>"""
    from ..server import Response

    goal_id = (ctx.query.get("id") or "").strip()
    if not goal_id:
        return Response.error(400, "missing_goal_id",
                              "query parameter 'id' required")

    source = (ctx.query.get("source") or "world").strip()
    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)
    had_claim = False

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_goal(items, goal_id)
            if found is None:
                agent_live = ctx.paths.agent / "aspirations.jsonl"
                if agent_live.exists():
                    try:
                        agent_items = _read_jsonl(agent_live)
                        if _find_goal(agent_items, goal_id) is not None:
                            return Response.error(400, "agent_queue_goal",
                                f"Goal {goal_id} is in the agent queue. "
                                f"Agent-queue goals do not carry claims, "
                                f"so there is nothing to release.")
                    except Exception:
                        pass
                return Response.error(404, "goal_not_found",
                    f"Goal {goal_id} not found in world queue")

            asp_idx, goal_idx, asp = found
            goal = asp["goals"][goal_idx]
            had_claim = "claimed_by" in goal or "claimed_at" in goal
            goal.pop("claimed_by", None)
            goal.pop("claimed_at", None)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"release {goal_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"release {goal_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence for release (9): a swallowed
    # release PUT leaves claimed_by set in the authoritative store — the goal
    # stays claimed on every other box and time-travels back to claimed on
    # THIS one at the next re-sync (the 1 specimen: an 11:53 release
    # verified against the local mirror, reverted by the 18:21 restart
    # re-sync). Verified unconditionally (not just had_claim): the store copy
    # may carry a claim the stale local mirror lacked. Conservative fail-open
    # (see _verify_transition_persisted).
    if not _verify_transition_persisted(live_path, asp["id"], goal_id,
                                        {"claimed_by": None,
                                         "claimed_at": None}):
        import sys
        print(f"[daemon release] WRITE-LOSS DETECTED: release of {goal_id} "
              f"returned success-shaped but a claim persists in the "
              f"authoritative store (own-cloud silent write-loss, "
              f"g-115-2429)", file=sys.stderr)
        return Response.error(
            500, "release_not_persisted",
            f"release of {goal_id} did not persist to the authoritative "
            f"store (own-cloud write-loss, g-115-2429); retry the release")

    if had_claim:
        try:
            from _wake_signals import touch_peer_signals
            touch_peer_signals("goal-claim-released")
        except Exception:
            pass

    return Response.json({"ok": True, "goal": goal, "had_claim": had_claim})


def _audit_cross_lane_claim_inline(ctx, *, goal_id: str,
                                   agent_claiming: str,
                                   intended_agent: str,
                                   justification: str,
                                   category: Optional[str] = None,
                                   title: Optional[str] = None) -> None:
    """Log cross-lane claim override to override-bypass-ledger.jsonl.

    Daemon-specific variant of _override_helpers.audit_cross_lane_claim
    that uses ctx.paths.world instead of module-level WORLD_DIR.
    """
    import hashlib
    import sys
    if not justification or not goal_id:
        return
    token = hashlib.sha1(
        justification.encode("utf-8", errors="replace")).hexdigest()[:12]
    merged_context = {
        "goal_id": goal_id,
        "agent_claiming": agent_claiming,
        "intended_agent": intended_agent,
    }
    if category:
        merged_context["category"] = category
    if title:
        merged_context["title"] = title[:200]
    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": token,
        "justification": (justification or "")[:1000],
        "gate": "capability-route-gate",
        "agent": ctx.paths.agent_name or None,
        "session_id": None,
        "context": merged_context,
    }
    try:
        from _fileops import locked_append_jsonl
        ledger_path = ctx.paths.world / "override-bypass-ledger.jsonl"
        locked_append_jsonl(str(ledger_path), record)
    except Exception as e:
        print(f"[daemon claim] WARN: cross-lane ledger write failed: {e}",
              file=sys.stderr)


def _load_claim_timeout_hours(project_root: Path) -> Optional[float]:
    """Load multi_agent.claim_timeout_hours from core/config/aspirations.yaml.

    Single source of truth SHARED with goal-selector.py's claim-visibility
    contract: the selector makes a stale-claimed world goal visible again once
    claim_age exceeds this value, and claim() must mirror it (else the two
    disagree and the world queue livelocks — g-115-1841). Fail-open to the
    historical literal 4.0 on any read/parse error OR a missing key; returns
    None ONLY when the key is explicitly null (parity with the selector's
    `claim_timeout_hours is None` legacy branch — no expiry configured -> no
    take-back). Mirrors the fail-open shape of _load_streak_mult_config.
    """
    import yaml
    cfg_path = project_root / "core" / "config" / "aspirations.yaml"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ma = cfg.get("multi_agent") or {}
        if "claim_timeout_hours" not in ma:
            return 4.0
        v = ma["claim_timeout_hours"]
        return None if v is None else float(v)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return 4.0


def _hours_since(iso_ts) -> Optional[float]:
    """Hours between now and an ISO-8601 LOCAL timestamp; None on missing or
    unparseable input. Mirrors goal-selector.py hours_since — callers treat a
    None result as 'age unknown'."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts))
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).total_seconds() / 3600.0


def _audit_stale_claim_takeback_inline(ctx, *, goal_id: str,
                                       agent_claiming: str,
                                       prior_claimer: str,
                                       claim_age_hours: Optional[float],
                                       effective_timeout_hours: float,
                                       category: Optional[str] = None,
                                       title: Optional[str] = None) -> None:
    """Log a stale-claim take-back to override-bypass-ledger.jsonl.

    When claim() re-assigns a world goal whose prior claim has expired
    (claim_age > effective_timeout, mirroring goal-selector.py's visibility
    contract — g-115-1841), record the steal to the shared audit ledger so
    claim-contention is debuggable (parity with _audit_cross_lane_claim_inline).
    A take-back is sanctioned auto-recovery, NOT a guard bypass, so it carries a
    distinct gate tag ('claim-staleness-takeback') for filtering. Fail-open:
    a ledger write failure never blocks the claim (warn to stderr only)."""
    import sys
    if not goal_id or not prior_claimer:
        return
    age_str = "unknown" if claim_age_hours is None else round(claim_age_hours, 2)
    context = {
        "goal_id": goal_id,
        "agent_claiming": agent_claiming,
        "prior_claimer": prior_claimer,
        "claim_age_hours": (None if claim_age_hours is None
                            else round(claim_age_hours, 2)),
        "effective_timeout_hours": round(effective_timeout_hours, 2),
    }
    if category:
        context["category"] = category
    if title:
        context["title"] = title[:200]
    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": None,
        "justification": (
            f"stale-claim take-back: prior claim by {prior_claimer} aged "
            f"{age_str}h > {round(effective_timeout_hours, 2)}h timeout")[:1000],
        "gate": "claim-staleness-takeback",
        "agent": agent_claiming or (ctx.paths.agent_name or None),
        "session_id": None,
        "context": context,
    }
    try:
        from _fileops import locked_append_jsonl
        ledger_path = ctx.paths.world / "override-bypass-ledger.jsonl"
        locked_append_jsonl(str(ledger_path), record)
    except Exception as e:
        print(f"[daemon claim] WARN: stale-claim-takeback ledger write "
              f"failed: {e}", file=sys.stderr)


def claim(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/claim?id=<goal_id>&agent=<name>[&cross_lane=<reason>]"""
    from ..server import Response

    goal_id = (ctx.query.get("id") or "").strip()
    if not goal_id:
        return Response.error(400, "missing_goal_id",
                              "query parameter 'id' required")

    agent_name = (ctx.query.get("agent") or "").strip()
    if not agent_name:
        return Response.error(400, "missing_agent",
                              "query parameter 'agent' required")

    cross_lane = (ctx.query.get("cross_lane") or "").strip() or None
    live_path, base_dir = _resolve_paths(ctx, "world")
    agent = _agent_name(ctx)

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_goal(items, goal_id)
            # Detect goal-ID collision across queues.
            # When the same goal_id exists in BOTH world and agent queues,
            # claim previously resolved to the world copy silently regardless
            # of caller intent. Now: if collision detected, refuse with 409
            # so the caller picks a queue explicitly (agent-queue goals don't
            # need claim; use recurring-close.sh or
            # aspirations-update-goal.sh --source agent directly for those).
            if found is not None:
                agent_live = ctx.paths.agent / "aspirations.jsonl"
                if agent_live.exists():
                    try:
                        agent_items = _read_jsonl(agent_live)
                        if _find_goal(agent_items, goal_id) is not None:
                            return Response.error(409, "goal_id_collision",
                                f"Goal {goal_id} exists in BOTH world and "
                                f"agent queues. Claim is ambiguous. Rename "
                                f"one queue's copy, OR for the agent-queue "
                                f"goal call recurring-close.sh / "
                                f"aspirations-update-goal.sh --source agent "
                                f"directly (agent goals don't require claims).")
                    except Exception:
                        pass
            if found is None:
                agent_live = ctx.paths.agent / "aspirations.jsonl"
                if agent_live.exists():
                    try:
                        agent_items = _read_jsonl(agent_live)
                        if _find_goal(agent_items, goal_id) is not None:
                            return Response.error(400, "agent_queue_goal",
                                f"Goal {goal_id} is in the agent queue. "
                                f"Agent-queue goals do not require claims "
                                f"(single-agent access per conventions/"
                                f"aspirations.md Source Routing Protocol). "
                                f"Proceed directly to execution.")
                    except Exception:
                        pass
                return Response.error(404, "goal_not_found",
                    f"Goal {goal_id} not found in world queue")

            asp_idx, goal_idx, asp = found
            goal = asp["goals"][goal_idx]
            claimed_asp_id = asp.get("id")  # for the post-write persistence read-back

            intended = goal.get("intended_agent")
            if (intended and intended != agent_name
                    and intended != "either"):
                if not cross_lane:
                    return Response.error(400, "cross_lane_refused",
                        f"Goal {goal_id} routed to '{intended}' but claimer "
                        f"is '{agent_name}'. Pass cross_lane query param to "
                        f"override (justification logged to "
                        f"override-bypass-ledger.jsonl).")
                _audit_cross_lane_claim_inline(
                    ctx, goal_id=goal_id, agent_claiming=agent_name,
                    intended_agent=intended, justification=cross_lane,
                    category=goal.get("category"), title=goal.get("title"))

            existing = goal.get("claimed_by")
            claim_summary = f"claim {goal_id}"
            if existing and existing != agent_name:
                # Staleness take-back (1): mirror goal-selector.py's
                # claim-visibility contract (L1415-1428). The selector makes a
                # stale-claimed world goal VISIBLE again once its claim expires;
                # if claim() keeps hard-409ing, selector and endpoint DISAGREE
                # and the world queue LIVELOCKS (goal offered to the agent, which
                # then cannot claim it). A claim is stale when
                # claim_age > effective_timeout, where effective_timeout is
                # claim_timeout_hours capped at 2x interval_hours for recurring
                # goals (a short-interval goal must not stay claimed for the full
                # window). Selector-parity on the edge cases:
                #   - claim_timeout_hours is None (no expiry configured) -> NEVER
                #     take back (selector's legacy `continue`).
                #   - claimed_at missing/unparseable (claim_age is None) -> treat
                #     as expired and take back (selector falls through to include).
                #     Safe: claim() ALWAYS stamps claimed_at under this same lock
                #     (below), so a live in-flight claim always carries a
                #     parseable timestamp; a timestamp-less claim is legacy/manual
                #     residue and stealing it is the correct recovery.
                claim_timeout_hours = _load_claim_timeout_hours(
                    ctx.paths.project_root)
                claim_age = None
                effective_timeout = None
                if claim_timeout_hours is not None:
                    effective_timeout = claim_timeout_hours
                    if goal.get("recurring"):
                        interval = goal.get("interval_hours")
                        if not interval:
                            rd = goal.get("remind_days")
                            interval = (rd * 24) if rd else 24
                        effective_timeout = min(claim_timeout_hours, 2 * interval)
                    claim_age = _hours_since(goal.get("claimed_at"))
                    stale = (claim_age is None) or (claim_age > effective_timeout)
                else:
                    stale = False  # no expiry configured -> keep the claim
                if not stale:
                    return Response.error(409, "already_claimed",
                        f"Goal {goal_id} already claimed by {existing}")
                # Expired claim -> take it back. Audit the steal to the bypass
                # ledger (like cross_lane) so claim contention is debuggable.
                _audit_stale_claim_takeback_inline(
                    ctx, goal_id=goal_id, agent_claiming=agent_name,
                    prior_claimer=existing, claim_age_hours=claim_age,
                    effective_timeout_hours=effective_timeout,
                    category=goal.get("category"), title=goal.get("title"))
                claim_summary = f"claim {goal_id} (take-back from {existing})"

            goal["claimed_by"] = agent_name
            goal["claimed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            # Attempt marker (5): a CLAIM is the goal's first real
            # attempt signal. `started` already rides _COMPACT_GOAL_KEEP but had
            # NO writer on the goal path, so precheck-eval cmd_cycles could not
            # tell a WITHDRAWN skip (never attempted — 96.5% of skips, e.g. a
            # dedup sweep skipping a pending duplicate straight from pending)
            # from genuine attempted-then-failed work, and a dedup sweep could
            # trip a phantom repeated_failure Investigate (it hit asp-335, the
            # team's primary strategic aspiration). setdefault → idempotent
            # (never overwrite an existing marker; a stale-claim take-back keeps
            # the original attempt time). Zero hot-path tax: `started` is already
            # in the compact projection both sides read.
            goal.setdefault("started", goal["claimed_at"])

            history.snapshot(live_path, base_dir, agent,
                             summary=claim_summary)
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=claim_summary,
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence invariant extended to claim()
    # (6; add_goal-pattern 8). Two live specimens on cc-05
    # 2026-07-16 (6 ~03:29 + 1 04:34): claim returned a
    # complete success JSON with claimed_by set, raw-store read-back showed
    # claimed_by=None moments later — the bare-locked write's fenced PUT
    # resolved away against stale mirror state. A silent claim loss is a
    # cross-agent DOUBLE-CLAIM hazard (two agents both believing they own the
    # goal). Verify claimed_by landed in the authoritative store BEFORE
    # returning success; refuse the false 200 on definitive absence so the
    # wrapper/caller retries. Conservative fail-open (see
    # _verify_claim_persisted): a real success can never become a false failure.
    if not _verify_claim_persisted(live_path, claimed_asp_id, goal_id,
                                   agent_name):
        import sys
        print(f"[daemon claim] WRITE-LOSS DETECTED: claim of {goal_id} by "
              f"{agent_name} returned success-shaped but claimed_by is absent "
              f"from the authoritative store after write (own-cloud silent "
              f"claim-loss, g-115-2306)", file=sys.stderr)
        return Response.error(
            500, "claim_not_persisted",
            f"claim of {goal_id} by {agent_name} did not persist to the "
            f"authoritative store (own-cloud claim-loss, g-115-2306); retry "
            f"the claim")

    return Response.json({"ok": True, "goal": goal})


def archive_sweep(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/archive-sweep?source=<world|agent>

    Batch operation: classify all aspirations, archive completed/retired,
    recover corrupted recurring goals. Mirrors cmd_archive_sweep exactly.
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    agent = _agent_name(ctx)
    live_path, base_dir = _resolve_paths(ctx, source)
    archive_path = base_dir / "aspirations-archive.jsonl"
    warnings: List[str] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            to_archive: List[Dict[str, Any]] = []
            remaining: List[Dict[str, Any]] = []
            recovered = 0

            for a in items:
                if a.get("status") in ("completed", "retired"):
                    recurring = _find_recurring_goals(a)
                    if recurring:
                        rg_ids = ", ".join(g["id"] for g in recurring)
                        warnings.append(
                            f"Recovering {a['id']} -- has {len(recurring)} recurring "
                            f"goal(s): {rg_ids}. Resetting to active.")
                        a["status"] = "active"
                        a["archived"] = False
                        a.pop("completed_at", None)
                        for g in recurring:
                            if g.get("status") == "completed":
                                g["status"] = "pending"
                        _recompute_progress(a)
                        remaining.append(a)
                        recovered += 1
                    else:
                        if a.get("status") == "completed":
                            unfinished = _find_unfinished_goals(a)
                            if unfinished:
                                uf_ids = ", ".join(g["id"] for g in unfinished)
                                warnings.append(
                                    f"Recovering {a['id']} -- has {len(unfinished)} "
                                    f"unfinished goal(s): {uf_ids}. Resetting to active.")
                                a["status"] = "active"
                                a["archived"] = False
                                a.pop("completed_at", None)
                                _recompute_progress(a)
                                remaining.append(a)
                                recovered += 1
                                continue
                        to_archive.append(a)
                else:
                    recurring = _find_recurring_goals(a)
                    if recurring:
                        corrupted = [g for g in recurring
                                     if g.get("status") == "completed"]
                        if corrupted:
                            c_ids = ", ".join(g["id"] for g in corrupted)
                            warnings.append(
                                f"Recovering {len(corrupted)} corrupted recurring "
                                f"goal(s) in {a['id']}: {c_ids}. Resetting to pending.")
                            for g in corrupted:
                                g["status"] = "pending"
                            _recompute_progress(a)
                            recovered += 1
                    shape_corrupted = _find_shape_recurring_corrupted(a)
                    if shape_corrupted:
                        s_ids = ", ".join(g["id"] for g in shape_corrupted)
                        warnings.append(
                            f"Recovering {len(shape_corrupted)} shape-recurring "
                            f"goal(s) in {a['id']} (recurring=false but "
                            f"interval_hours+lastAchievedAt set): {s_ids}. "
                            f"Resetting to pending.")
                        for g in shape_corrupted:
                            g["status"] = "pending"
                        _recompute_progress(a)
                        recovered += 1
                    remaining.append(a)

            if not to_archive:
                if recovered:
                    history.snapshot(live_path, base_dir, agent,
                                    summary="archive-sweep (recovery only)")
                    _atomic_write_jsonl(live_path, remaining)
                    changelog.append(base_dir, agent, live_path, "edit",
                                     summary="archive-sweep (recovery only)",
                                     lines_changed=len(remaining))
                    _jsonl_cache().invalidate(live_path)
                return Response.json({
                    "ok": True,
                    "archived_count": 0,
                    "recovered": recovered,
                    "warnings": warnings if warnings else None,
                })

            archived_goal_ids: set = set()
            for asp in to_archive:
                for g in asp.get("goals", []):
                    archived_goal_ids.add(g["id"])

            # Read existing archive, extend, normalize each, write the whole list back.
            # Replace-by-id on collision (4): a record already present in
            # the archive (e.g. resurrected into the live file by an own-cloud
            # partial write, then re-retired) must NOT append a duplicate — the
            # incoming copy carries the newest state, so it replaces the stale
            # archive copy in place. Extend-only here appended a second copy per
            # re-sweep (observed: asp-344 archived as completed, resurrected,
            # retired in live — a plain extend would have doubled it).
            archive = _read_jsonl(archive_path)
            incoming_by_id = {a.get("id"): a for a in to_archive}
            deduped_replaced = 0
            for i, existing in enumerate(archive):
                eid = existing.get("id")
                if eid in incoming_by_id:
                    archive[i] = incoming_by_id.pop(eid)
                    deduped_replaced += 1
            archive.extend(incoming_by_id.values())
            for asp in archive:
                _normalize_terminal_goals_in(asp)
            _atomic_write_jsonl(archive_path, archive)

            _clear_stale_blockers_inline(remaining, archived_goal_ids)

            history.snapshot(live_path, base_dir, agent,
                            summary=f"archive-sweep ({len(to_archive)} archived)")
            _atomic_write_jsonl(live_path, remaining)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"archive-sweep ({len(to_archive)} archived)",
                             lines_changed=len(remaining))
            _jsonl_cache().invalidate(live_path)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "archived_count": len(to_archive),
        "deduped_replaced": deduped_replaced,
        "recovered": recovered,
        "warnings": warnings if warnings else None,
    })


def meta_update(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/meta-update?source=<world|agent>

    Body: JSON object of {field: value, ...} pairs to set on aspirations-meta.json.
    Mirrors aspirations.py cmd_meta_update: locked read of the JSON file, apply
    field updates, atomic write back. Dotted field names are rejected (flat
    top-level keys only — use nested JSON values for sub-objects).

    The meta file is plain JSON (not JSONL), so we use json.load / json.dump
    with the same indent/ensure_ascii as _fileops.locked_write_json.
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")

    agent = _agent_name(ctx)

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    # Resolve meta-json path from the same base_dir as aspirations.jsonl.
    _, base_dir = _resolve_paths(ctx, source)
    meta_path = base_dir / "aspirations-meta.json"

    # Parse body — expects {field: value, ...} pairs.
    try:
        updates = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", str(e))

    if not isinstance(updates, dict):
        return Response.error(400, "invalid_body",
                              "body must be a JSON object of {field: value} pairs")
    if not updates:
        return Response.error(400, "empty_body",
                              "body must contain at least one field to update")

    # Reject dotted field names — symmetric with cmd_meta_update's Option A
    # reject ().
    for field in updates:
        if "." in field:
            return Response.error(400, "dotted_field_rejected",
                f"Dotted field name '{field}' is not supported. "
                f"This endpoint writes flat top-level keys only. To write a "
                f"nested value, pass the parent field with a full nested JSON "
                f"object as the value.")

    _DEFAULT_META = {
        "last_updated": None,
        "last_evolution": None,
        "session_count": 0,
        "readiness_gates": {},
    }

    try:
        with file_locks.locked(meta_path):
            # #38 own-cloud: force-fresh BEFORE the raw read so the read sees a
            # peer's committed meta and the backend records the If-Match fence
            # etag — without it the _atomic_write below issues an UNCONDITIONAL
            # PUT (fence=None) that silently clobbers a concurrent peer's meta
            # update on a stale-lock-break race. No-op on LocalBackend.
            get_backend().refresh(meta_path)
            # Read existing or create default.
            if meta_path.exists():
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            else:
                data = dict(_DEFAULT_META)

            # Apply updates.
            for field, value in updates.items():
                data[field] = value

            # Atomic write via _fileops helper (same retry policy as
            # locked_write_json but we already hold the lock).
            assert_not_cruft(meta_path.parent, "mkdir (meta_update)")
            meta_path.parent.mkdir(parents=True, exist_ok=True)

            def _write(handle):
                json.dump(data, handle, indent=2, ensure_ascii=True)
                handle.write("\n")

            _atomic_write_with_fallback(
                meta_path, _write,
                fallback_counter_key="daemon_aspirations_meta_update")

            # History + changelog — meta.json is structural data worth versioning.
            history.snapshot(meta_path, base_dir, agent,
                             summary="meta-update")
            changelog.append(base_dir, agent, meta_path, "edit",
                             summary="meta-update",
                             lines_changed=1)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "data": data})


def clear_stale_claims(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/clear-stale-claims?source=<world|agent>[&dry_run=true]

    Sweep the live aspirations store and clear claimed_by/claimed_at from any
    goal whose status is terminal. Idempotent — safe to re-run; zero-effect
    when there is no residue. Self-heal tool if any writer regresses on claim
    clearing. Mirrors cmd_clear_stale_claims exactly.

    Query params:
      source   (optional, default "world") — "world" or "agent"
      dry_run  (optional, default "false") — "true" to report without writing

    Headers:
      X-Mind-Agent — agent name for history/changelog
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")

    dry_run = (ctx.query.get("dry_run") or "").strip().lower() == "true"
    agent = _agent_name(ctx)
    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)
    cleared: List[str] = []

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)

            for asp in items:
                for goal in asp.get("goals", []):
                    if (goal.get("status") in _TERMINAL_GOAL_STATUSES
                            and ("claimed_by" in goal or "claimed_at" in goal)):
                        cleared.append(goal.get("id"))
                        if not dry_run:
                            goal.pop("claimed_by", None)
                            goal.pop("claimed_at", None)

            if cleared and not dry_run:
                history.snapshot(live_path, base_dir, agent,
                                summary=f"clear-stale-claims ({len(cleared)} goals)")
                _atomic_write_jsonl(live_path, items)
                changelog.append(base_dir, agent, live_path, "edit",
                                 summary=f"clear-stale-claims ({len(cleared)} goals)",
                                 lines_changed=len(items))
                _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({
        "ok": True,
        "cleared_count": len(cleared),
        "cleared_ids": cleared,
        "dry_run": dry_run,
    })


# ---------------------------------------------------------------------------
# Aspiration-level validation (mirrors aspirations.py::validate_aspiration)
# ---------------------------------------------------------------------------

def _validate_aspiration(asp: Dict[str, Any], *, auto_id: bool = False) -> None:
    """Validate an aspiration dict. Raises ValueError on invalid.

    Mirror of aspirations.py::validate_aspiration — duplicated per
    DECISIONS.md #3 (no cross-import from script module).

    auto_id=True (g-328-29): the add endpoint mints asp + goal ids in-lock
    AFTER this runs, so the id field is legally absent here and embedded
    goals are validated with require_id=False.
    """
    required = {"id", "title", "status", "goals", "priority", "archived"}
    if auto_id:
        required = required - {"id"}
    missing = required - set(asp.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if "id" in asp and not _ASP_ID_RE.match(asp["id"]):
        raise ValueError(f"Invalid aspiration ID format: {asp['id']} (expected asp-NNN)")

    if asp["status"] not in _VALID_ASP_STATUSES:
        raise ValueError(f"Invalid aspiration status: {asp['status']}")

    if asp["priority"] not in _VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {asp['priority']}")

    if not isinstance(asp["goals"], list):
        raise ValueError("goals must be a list")

    if not isinstance(asp["archived"], bool):
        raise ValueError("archived must be a boolean")

    if "scope" in asp and asp["scope"] not in _VALID_SCOPES:
        raise ValueError(f"Invalid scope: {asp['scope']} (expected one of {_VALID_SCOPES})")

    if "coordination_mode" in asp and asp["coordination_mode"] not in _VALID_COORDINATION_MODES:
        raise ValueError(
            f"Invalid coordination_mode: {asp['coordination_mode']} "
            f"(expected one of {_VALID_COORDINATION_MODES})")

    if "sessions_active" in asp and not isinstance(asp["sessions_active"], (int, float)):
        raise ValueError("sessions_active must be a number")

    if "co_investigators" in asp:
        if not isinstance(asp["co_investigators"], list):
            raise ValueError("co_investigators must be a list")
        for name in asp["co_investigators"]:
            if not isinstance(name, str):
                raise ValueError("co_investigators entries must be strings")

    for goal in asp["goals"]:
        _validate_goal(goal, require_id=not auto_id)
        # Prose-verification-drift parity on the bulk aspiration-add path
        # (4) — mirrors the CLI validate_aspiration, which runs the
        # check via validate_goal on every embedded goal. No ctx here (pure
        # validator); telemetry falls back to the module-default meta_dir.
        _assert_no_prose_drift(goal)


# ---------------------------------------------------------------------------
# POST /v1/aspirations/add — add a full aspiration record
# ---------------------------------------------------------------------------

def add(ctx) -> "Response":  # type: ignore[name-defined]
    """Create a new aspiration (with embedded goals).

    Mirrors aspirations.py::cmd_add flow:
      1. Parse body JSON (the aspiration dict)
      2. Apply defaults (archived, blocked_since)
      3. Validate aspiration + embedded goals
      4. Run origin-signal gate per goal (batch)
      5. Apply goal-source auto-derive per goal
      6. Run goal-duplication gate per goal
      7. Lock: mint asp/goal ids if omitted (g-328-29) + archive-check +
         dup-check + recompute_progress + write

    Query params:
      source  — "world" (default) or "agent"

    Override headers:
      X-Mind-Override-Signal       — origin-signal gate bypass (per-gate)
      X-Mind-Override-Duplication  — goal-duplication gate bypass (per-gate)
      X-Mind-Override-All          — bulk bypass (fans into any unset
                                       per-gate slot; per-gate ALWAYS wins;
                                       audited to world/override-bypass-ledger.jsonl)
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").lower()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", "source must be world or agent")

    try:
        asp = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON aspiration object: {e}")
    if not isinstance(asp, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    # --- : server-side ID allocation ---------------------------------
    # The id field is OPTIONAL. When absent (or "auto"/""), the daemon mints
    # the next asp-NNN INSIDE the write lock below — max+1 across live ∪
    # archive of the target queue. Client-side minting (SKILL-layer max+1
    # computed outside any lock) was the asp-334/asp-335 double-mint race.
    # Explicit ids remain supported (transplant/migration callers).
    auto_id = str(asp.get("id") or "").strip().lower() in ("", "auto")
    if auto_id:
        asp.pop("id", None)
        conflicted = [g.get("id") for g in asp.get("goals", []) if g.get("id")]
        if conflicted:
            return Response.error(
                400, "auto_id_goal_conflict",
                "aspiration id omitted (auto allocation) but embedded goals "
                f"carry ids ({', '.join(map(str, conflicted))}) — the caller "
                "cannot know the asp number yet. Omit goal ids too; they are "
                "minted server-side as g-NNN-01.. in array order.")

    # --- Defaults (outside lock — no file-state dependency) ---
    asp.setdefault("archived", False)
    # initial_goal_count: non-recurring goal count at creation, stamped
    # once. Idempotent (never overwritten on a re-add) so fan_out_ratio
    # in _recompute_progress can express growth from the seed. Non-recurring
    # to stay consistent with progress.total_goals.
    if "initial_goal_count" not in asp:
        asp["initial_goal_count"] = sum(
            1 for g in asp.get("goals", []) if not g.get("recurring")
        )
    now_ts = datetime.now().isoformat(timespec="seconds")
    for g in asp.get("goals", []):
        if g.get("blocked_by") and not g.get("blocked_since"):
            g["blocked_since"] = now_ts

    # --- Validate ---
    try:
        _validate_aspiration(asp, auto_id=auto_id)
    except ValueError as e:
        return Response.error(400, "validation_failed", str(e))

    # --- Gates (outside lock — slow I/O) ---
    warnings: List[str] = []

    # user_leg_scope advisory per goal (mirrors cmd_add's inline call)
    for g in asp.get("goals", []):
        uls_result = _user_leg_scope_eval(
            goal_id=g.get("id") or "<unassigned>",
            participants=g.get("participants"),
            user_leg_scope=g.get("user_leg_scope"),
        )
        if uls_result["warned"]:
            warnings.append(uls_result["message"])

    # Bulk override fan-out (mirrors _override_helpers.apply_override_all).
    # Per-gate headers ALWAYS win — X-Mind-Override-All only fills slots
    # the caller did not explicitly set. Track which slots received the
    # bulk so audit_bulk_override below can record the blast radius.
    bulk_override = _header_override(ctx, "X-Mind-Override-All")
    raw_override_signal = _header_override(ctx, "X-Mind-Override-Signal")
    raw_override_dup = _header_override(ctx, "X-Mind-Override-Duplication")
    raw_override_off = _header_override(ctx, "X-Mind-Override-Offload")
    bulk_slots_filled: List[str] = []
    if bulk_override:
        if raw_override_signal is None:
            bulk_slots_filled.append("override_signal")
        if raw_override_dup is None:
            bulk_slots_filled.append("override_duplication")
        if raw_override_off is None:
            bulk_slots_filled.append("override_offload")
    override_signal = raw_override_signal or bulk_override
    override_dup = raw_override_dup or bulk_override
    override_off = raw_override_off or bulk_override

    # Origin-signal gate per goal (batch — any block rejects the whole asp)
    for g in asp.get("goals", []):
        sig_payload = {
            "title": g.get("title", ""),
            "description": g.get("description", ""),
            "origin_signal": g.get("origin_signal"),
            "source": source,
        }
        sig_result = _origin_signal_eval(
            sig_payload,
            override_signal=override_signal,
            agent_name=ctx.paths.agent_name,
            world_dir=ctx.paths.world,
        )
        if sig_result.get("would_block"):
            return Response.json({
                "error": "origin_signal_blocked",
                "gate": "origin-signal-gate",
                "blocked_goal": g.get("id"),
                "gate_output": sig_result,
            }, status=400)
        if sig_result.get("auto_derived") and sig_result.get("origin_signal"):
            g["origin_signal"] = sig_result["origin_signal"]

    # Goal-source auto-derive (AFTER origin-signal gate — order matters)
    for g in asp.get("goals", []):
        _apply_goal_source_default(g)

    # Goal-duplication gate per goal (any block rejects the whole asp)
    # override_dup was set above with bulk-fan-out (per-gate wins over bulk).
    for g in asp.get("goals", []):
        dup_result = _goal_duplication_eval(
            g,
            override_duplication=override_dup,
            agent_name=ctx.paths.agent_name,
            world_dir=ctx.paths.world,
            project_root=ctx.paths.project_root,
        )
        if dup_result.get("would_block"):
            return Response.json({
                "error": "goal_duplication_blocked",
                "gate": "goal-duplication-gate",
                "blocked_goal": g.get("id"),
                "gate_output": dup_result,
            }, status=400)

    # Operator-offload gate per goal (any block rejects the whole asp) —
    # Layer-B backstop for gh-005: recurring goals must carry an
    # offload_decision. Pure no-op for non-recurring goals (the common case).
    # override_off was set above with bulk-fan-out (per-gate wins over bulk).
    for g in asp.get("goals", []):
        off_result = _operator_offload_eval(
            g,
            override_offload=override_off,
            meta_dir=ctx.paths.meta,
            agent_name=ctx.paths.agent_name,
        )
        if off_result.get("would_block"):
            return Response.json({
                "error": "operator_offload_blocked",
                "gate": "operator-offload-gate",
                "blocked_goal": g.get("id"),
                "gate_output": off_result,
            }, status=400)

    # --- Lock: archive-check + dup-check + recompute + write ---
    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)

            # Archive check — refuse reused archived IDs.
            archive_path = live_path.parent / "aspirations-archive.jsonl"
            archived = _read_jsonl(archive_path)

            #  in-lock mint: max+1 across live ∪ archive, BOTH read
            # under THIS lock — two concurrent auto adds serialize here and
            # get distinct sequential ids. \d{3,} so a future 4-digit id
            # still counts toward max (the minted format grows past 999
            # naturally via :03d). Embedded goal ids are minted g-NNN-01..
            # in array order (their absence was enforced pre-lock).
            if auto_id:
                max_n = 0
                for rec in items:
                    m = re.match(r"^asp-(\d{3,})$", str(rec.get("id") or ""))
                    if m:
                        max_n = max(max_n, int(m.group(1)))
                for rec in archived:
                    m = re.match(r"^asp-(\d{3,})$", str(rec.get("id") or ""))
                    if m:
                        max_n = max(max_n, int(m.group(1)))
                asp["id"] = f"asp-{max_n + 1:03d}"
                asp_num = asp["id"][len("asp-"):]
                for seq, g in enumerate(asp.get("goals", []), start=1):
                    g["id"] = f"g-{asp_num}-{seq:02d}"

            if any(a.get("id") == asp["id"] for a in archived):
                return Response.error(
                    400, "archived_id_reuse",
                    f"{asp['id']} already exists in archive — pick a higher ID")

            # Duplicate check — refuse IDs already in the live file.
            if _find_aspiration(items, asp["id"]) is not None:
                return Response.error(
                    400, "duplicate_id",
                    f"Aspiration {asp['id']} already exists in {source}")

            _recompute_progress(asp)
            items.append(asp)

            history.snapshot(live_path, base_dir, agent,
                            summary=f"add {asp['id']}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"add {asp['id']}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # Audit bulk override (mirror of aspirations.py cmd_add lines 1169-1174).
    # Records the blast radius (which gates were silenced) so retrospective
    # review can spot vague justifications suppressing too many checks.
    # Best-effort: helper has internal try/except — never raises.
    if bulk_override and bulk_slots_filled:
        import hashlib as _hashlib
        bulk_token = _hashlib.sha1(
            bulk_override.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        _audit_bulk_override(
            bulk_token, bulk_override, bulk_slots_filled,
            context={"caller": "aspirations_write.py:add",
                     "asp_id": asp.get("id"),
                     "goals_count": len(asp.get("goals", [])),
                     "source": source},
            world_dir=ctx.paths.world)

    response_body: Dict[str, Any] = {
        "ok": True,
        "aspiration_id": asp["id"],
        "source": source,
        "aspiration": asp,
    }
    if auto_id:
        # : tell the caller the id was minted server-side — the
        # SKILL layer reads aspiration_id back instead of pre-computing it.
        response_body["id_allocated"] = True
    if warnings:
        response_body["warnings"] = warnings
    return Response.json(response_body)


def recover_recurring(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/recover-recurring?source=<world|agent>

    One-off recovery sweep for corrupted recurring goals.  Mirrors
    cmd_recover_recurring exactly — three cases, no archival:
      Case 1: recurring=true + status=completed → reset to pending
      Case 2: shape-recurring corrupted (recurring=false + status=completed
              + interval_hours + lastAchievedAt) → reset to pending
      Case 3: recurring goal pointing to an archived hypothesis → retire
              (recurring=false, status=completed, outcome_note)
    Skips terminal aspirations (completed/retired).
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")

    agent = _agent_name(ctx)
    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)

    # --- Pipeline reads (Case 3): outside lock, read-only, fail-open ---
    world_dir = ctx.paths.world
    archived_hypothesis_ids: set = set()
    for pname in ("pipeline.jsonl", "pipeline-archive.jsonl"):
        ppath = world_dir / pname
        if not ppath.exists():
            continue
        try:
            for h in _read_jsonl(ppath):
                if h.get("stage") == "archived" and h.get("id"):
                    archived_hypothesis_ids.add(h["id"])
        except Exception:
            pass  # fail-open: missing/corrupt pipeline disables Case 3

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            recovered_goals: List[Dict[str, Any]] = []
            changed = False
            today_iso = datetime.now().strftime("%Y-%m-%d")

            for a in items:
                if a.get("status") in ("completed", "retired"):
                    continue
                asp_touched = False

                # Case 1: recurring=true + status=completed → reset to pending
                recurring = _find_recurring_goals(a)
                if recurring:
                    for g in recurring:
                        if g.get("status") == "completed":
                            g["status"] = "pending"
                            recovered_goals.append({
                                "asp": a["id"], "goal": g["id"],
                                "pattern": "recurring-completed"})
                            changed = True
                            asp_touched = True

                # Case 2: shape-recurring corrupted → reset to pending
                shape_corrupted = _find_shape_recurring_corrupted(a)
                for g in shape_corrupted:
                    g["status"] = "pending"
                    recovered_goals.append({
                        "asp": a["id"], "goal": g["id"],
                        "pattern": "shape-recurring"})
                    changed = True
                    asp_touched = True

                # Case 3: recurring goal → archived hypothesis → retire
                if archived_hypothesis_ids:
                    for g in a.get("goals", []):
                        if not g.get("recurring"):
                            continue
                        hyp_id = g.get("hypothesis_id")
                        if not hyp_id or hyp_id not in archived_hypothesis_ids:
                            continue
                        if g.get("status") not in ("pending", "completed"):
                            continue
                        g["recurring"] = False
                        g["status"] = "completed"
                        g["completed_date"] = today_iso
                        g["completed_at"] = datetime.now().isoformat(
                            timespec="seconds")
                        g["outcome_note"] = (
                            f"hypothesis {hyp_id} stage=archived (auto-retired "
                            "by recover-recurring Case 3 — g-115-236)")
                        recovered_goals.append({
                            "asp": a["id"], "goal": g["id"],
                            "pattern": "hypothesis-archived",
                            "hypothesis_id": hyp_id})
                        changed = True
                        asp_touched = True

                if asp_touched:
                    _recompute_progress(a)

            if changed:
                history.snapshot(live_path, base_dir, agent,
                                summary="recover-recurring")
                _atomic_write_jsonl(live_path, items)
                changelog.append(base_dir, agent, live_path, "edit",
                                 summary="recover-recurring",
                                 lines_changed=len(items))
                _jsonl_cache().invalidate(live_path)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # R2 (BRD 2026-05-14): response shape is server's responsibility.
    # No "ok" wrapper — caller reads recovered + goals directly.
    return Response.json({
        "recovered": len(recovered_goals),
        "goals": recovered_goals,
    })


# NOTE (2026-05-14 cutover): this endpoint is STRICTER than the legacy
# cmd_update. Specifically: (1) dotted field paths are rejected with 400
# (cmd_update accepted full-record stdin JSON with no per-field validation);
# (2) known-enum fields (status, priority, scope, coordination_mode,
# archived) are validated individually against their enums — cmd_update
# deferred to validate_aspiration() which was looser on partial updates;
# (3) body must be a JSON object mapping field->value, not a full aspiration
# record (cmd_update replaced the entire record). Callers may see 400 where
# the CLI would have silently accepted. Safe direction — see BRD section 9 T2.3.
def update_aspiration(ctx) -> "Response":  # type: ignore[name-defined]
    """Update one or more fields on an aspiration record.

    Mirrors aspirations.py::cmd_update_asp_field — lightweight single-field
    (or multi-field via JSON body) update without full aspiration revalidation.
    Used for additive metadata fields like chronic_friction.

    Query params:
        asp_id  — required, asp-NNN format
        source  — "world" (default) or "agent"

    Body: JSON object mapping field→value. Each field is set on the matched
    aspiration. Validation fires for known-enum fields (status, priority,
    scope, coordination_mode, archived).
    """
    from ..server import Response

    asp_id = (ctx.query.get("asp_id") or "").strip()
    source = (ctx.query.get("source") or "world").strip()

    if not asp_id:
        return Response.error(400, "missing_asp_id",
                              "Query parameter 'asp_id' is required")
    if not _ASP_ID_RE.match(asp_id):
        return Response.error(400, "invalid_asp_id",
                              f"Invalid asp_id format: {asp_id} (expected asp-NNN)")
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              f"Invalid source: {source!r}")

    # Parse body
    try:
        body = _parse_body_json(ctx.body)
    except Exception:
        return Response.error(400, "invalid_body", "Body must be a JSON object")

    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "Body must be a JSON object")
    if not body:
        return Response.error(400, "empty_body", "Body must contain at least one field")

    # Validate known-enum fields
    for field, value in body.items():
        if "." in field:
            return Response.error(400, "dotted_field_rejected",
                                  f"Dotted field paths not supported: {field}")
        if field == "status" and value not in _VALID_ASP_STATUSES:
            return Response.error(400, "invalid_status",
                                  f"Invalid aspiration status: {value!r}. "
                                  f"Valid: {sorted(_VALID_ASP_STATUSES)}")
        if field == "priority" and value not in _VALID_PRIORITIES:
            return Response.error(400, "invalid_priority",
                                  f"Invalid priority: {value!r}. "
                                  f"Valid: {sorted(_VALID_PRIORITIES)}")
        if field == "scope" and value not in _VALID_SCOPES:
            return Response.error(400, "invalid_scope",
                                  f"Invalid scope: {value!r}. "
                                  f"Valid: {sorted(_VALID_SCOPES)}")
        if field == "coordination_mode" and value not in _VALID_COORDINATION_MODES:
            return Response.error(400, "invalid_coordination_mode",
                                  f"Invalid coordination_mode: {value!r}. "
                                  f"Valid: {sorted(_VALID_COORDINATION_MODES)}")
        if field == "archived" and not isinstance(value, bool):
            return Response.error(400, "invalid_archived",
                                  f"'archived' must be a boolean, got {type(value).__name__}")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_aspiration(items, asp_id)
            if found is None:
                return Response.error(404, "aspiration_not_found",
                                      f"Aspiration {asp_id} not found")

            idx, asp = found

            # Apply each field
            for field, value in body.items():
                asp[field] = value

            items[idx] = asp

            summary = (f"update-aspiration {asp_id} "
                       f"{','.join(body.keys())}")
            history.snapshot(live_path, base_dir, agent, summary=summary)
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=summary, lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)

    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "aspiration": asp})


# ---------------------------------------------------------------------------
# Bulk maintenance + evolution log (Batch 4)
# ---------------------------------------------------------------------------

# Mirror of aspirations.py::DATE_RE (line 198), used by evolution-append's
# validate_evolution_event replica.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def recompute_all_progress(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/recompute-all-progress?source=world|agent

    Mirrors aspirations.py cmd_recompute_all_progress: recompute progress for
    EVERY aspiration in the source's aspirations.jsonl, then full-rewrite the
    file. recompute_progress excludes recurring goals from completion counts;
    _recompute_progress (line 236) is the verbatim mirror used here. The
    full rewrite is byte-identical to write_jsonl -> locked_write_jsonl
    (`json.dumps(item, ensure_ascii=True) + "\\n"`).
    """
    from ..server import Response

    source = (ctx.query.get("source") or "world").lower()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", "source must be world or agent")

    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    if not live_path.exists():
        return Response.error(404, "file_not_found",
                              f"{source} aspirations.jsonl not found at {live_path}")

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            for asp in items:
                _recompute_progress(asp)
            history.snapshot(live_path, base_dir, agent,
                             summary="recompute-all-progress")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary="recompute-all-progress",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "source": source,
                          "aspirations_recomputed": len(items)})


def evolution_append(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/evolution-append   body: JSON evolution event.

    Mirrors aspirations.py cmd_evolution_append: validate {date,event,details},
    then single-line append to META_DIR/evolution-log.jsonl. The append is
    byte-identical to _fileops.locked_append_jsonl (validate -> history snapshot
    -> append-only `json.dumps(evt, ensure_ascii=True) + "\\n"` -> changelog;
    _fileops.py:1372-1383). base_dir = ctx.paths.meta (evolution-log.jsonl
    lives under META_DIR).
    """
    from ..server import Response
    from _fileops import _validate_no_surrogates

    try:
        evt = _parse_body_json(ctx.body)
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_body", f"body must be JSON event: {e}")
    if not isinstance(evt, dict):
        return Response.error(400, "invalid_body", "body must be a JSON object")

    # validate_evolution_event (aspirations.py:525) — required fields + date.
    missing = {"date", "event", "details"} - set(evt.keys())
    if missing:
        return Response.error(400, "validation_failed",
                              f"Missing required evolution event fields: {missing}")
    if not _DATE_RE.match(str(evt["date"])):
        return Response.error(400, "validation_failed",
                              f"Invalid date format: {evt['date']} (expected YYYY-MM-DD)")

    log_path = ctx.paths.meta / "evolution-log.jsonl"
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    try:
        _validate_no_surrogates(evt, log_path)
        assert_not_cruft(log_path.parent, "mkdir (evolution-append)")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(log_path):
            history.snapshot(log_path, base_dir, agent, summary="evolution-append")
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=True) + "\n")
            changelog.append(base_dir, agent, log_path, "edit",
                             summary="evolution-append", lines_changed=1)
            _jsonl_cache().invalidate(log_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "event": evt})


def register(routes) -> None:
    routes[("POST", "/v1/aspirations/add-goal")] = add_goal
    routes[("POST", "/v1/aspirations/update-goal")] = update_goal
    routes[("POST", "/v1/aspirations/complete")] = complete
    routes[("POST", "/v1/aspirations/complete-intent")] = complete_intent
    routes[("POST", "/v1/aspirations/complete-by")] = complete_by
    routes[("POST", "/v1/aspirations/retire")] = retire
    routes[("POST", "/v1/aspirations/release")] = release
    routes[("POST", "/v1/aspirations/claim")] = claim
    routes[("POST", "/v1/aspirations/archive-sweep")] = archive_sweep
    routes[("POST", "/v1/aspirations/meta-update")] = meta_update
    routes[("POST", "/v1/aspirations/clear-stale-claims")] = clear_stale_claims
    routes[("POST", "/v1/aspirations/add")] = add
    routes[("POST", "/v1/aspirations/recover-recurring")] = recover_recurring
    routes[("POST", "/v1/aspirations/update")] = update_aspiration
    routes[("POST", "/v1/aspirations/recompute-all-progress")] = recompute_all_progress
    routes[("POST", "/v1/aspirations/evolution-append")] = evolution_append
