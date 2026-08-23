"""POST /v1/aspirations/add-goal?asp_id=<a>  and  POST /v1/aspirations/update-goal

Writer endpoints over the daemon write infrastructure (file_locks +
history + changelog) PLUS in-process invocation of the extracted gates.

Pipeline (mirrors aspirations.py cmd_add_goal order):

  add-goal:
    Advisories (warn-only, surfaced in `warnings` array on 200 responses):
      1. user_leg_scope         (gates.user_leg_scope.evaluate)
      2. description_length     (gates.description_length.evaluate) +
                                 telemetry to META_DIR
      2b. approval_reference    (gates.approval_reference.evaluate) +
                                 telemetry to META_DIR — warns on the
                                 fabricated-approval shape (g-115-2857)
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
      filing lives in aspirations.py cmd_update_goal (CLI path, which
      wrappers no longer call — daemon-only since 2026-05-14).

Out of scope here — these live only in the CLI path, so daemon-routed
writes (i.e. every wrapper write) do NOT get them:
  - structured-prefix validation, defer_reason_set_at / blocker_ref
    cascades (the rest of cmd_update_goal's mutation pipeline)
  - blocker-create-gate (used by a different code path — CREATE_BLOCKER)
"""
from __future__ import annotations

import json
import re
import uuid
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
from _owncloud_codec import decode_response as _codec_decode_response  # noqa: E402  #  transport seam
from ..agent_paths import (  # noqa: E402
    assert_not_cruft, SESSIONS_DIRNAME, SESSION_DIRNAME,
)
import _gate_log  # noqa: E402
from _cadence_anchor import is_deliberate_raise as _is_deliberate_raise  # noqa: E402
# B9-deep: single-source census math (NOT a 3rd mirror) — folds evicted goals
# back into progress so goal eviction is metric-neutral. _goal_census is a pure
# leaf, resolvable via the same core/scripts sys.path entry file_locks adds.
from _goal_census import effective_counts as _effective_counts, census_completed as _census_completed  # noqa: E402
from _goal_census import all_evicted_ids as _all_evicted_ids  # noqa: E402  #  mint-site tombstone awareness
# : the goal-field allowlist. Imported (never re-typed) so this LIVE
# daemon path and aspirations.py::cmd_update_goal share one list — a hand-copied
# twin here would drift silently while the CLI-side list still looked correct,
# which is the guard-742/547 class this codebase keeps re-learning.
from _goal_fields import (  # noqa: E402
    is_known as _is_known_goal_field,
    unknown_field_error as _unknown_goal_field_error,
)
# : claim()'s terminal-status refusal. REUSED, not redefined —
# _goal_census.TERMINAL_STATUSES is drift-tested against
# aspirations.TERMINAL_GOAL_STATUSES by
# tests/test_goal_eviction_invariance.py::test_abandoned_status_set_no_drift, and
#  /  both record that this definition is already duplicated
# across subsystems. A fresh literal here would make that worse.
from _goal_census import TERMINAL_STATUSES as _TERMINAL_STATUSES  # noqa: E402
import _aspirations_resurrection as _resurrection  # noqa: E402  # archive-sweep resurrection predicate SSOT (2026-08-16)
from gates.origin_signal import evaluate as _origin_signal_eval  # noqa: E402
from gates.goal_duplication import evaluate as _goal_duplication_eval  # noqa: E402
from gates.operator_offload import evaluate as _operator_offload_eval  # noqa: E402
from gates.uncommitted_work import evaluate as _uncommitted_work_eval  # noqa: E402
from gates.capability import evaluate as _capability_eval  # noqa: E402
from gates.completion_artifact import evaluate as _completion_artifact_eval  # noqa: E402
from gates.residual_work import (  # noqa: E402
    evaluate as _residual_work_eval,
    find_existing_successor as _rw_find_existing_successor,
    build_successor_goal as _rw_build_successor,
)
from _override_helpers import audit_bulk_override as _audit_bulk_override  # noqa: E402
# PR 7c additions:
from gates.scaffolded_exploration import evaluate as _scaff_eval  # noqa: E402
from gates.capability_route import evaluate as _cap_route_eval, ACTIVE_AGENTS as _ACTIVE_AGENTS  # noqa: E402
from gates.field_shrink import evaluate as _field_shrink_eval  # noqa: E402
from gates.lane_pin import evaluate as _lane_pin_eval  # noqa: E402
from gates.category_suggest import evaluate as _category_suggest_eval  # noqa: E402
from gates.description_length import evaluate as _desc_len_eval  # noqa: E402
from gates.depends_on_consistency import evaluate as _depends_on_eval  # noqa: E402
from gates.intended_agent_vocab import evaluate as _intended_agent_vocab_eval  # noqa: E402
from gates.approval_reference import evaluate as _approval_ref_eval  # noqa: E402
from gates.prose_verification import evaluate as _prose_verification_eval  # noqa: E402
from gates.check_schema import evaluate as _check_schema_eval  # noqa: E402
from gates.user_leg_scope import evaluate as _user_leg_scope_eval  # noqa: E402
from gates.defer_classifier import (  # noqa: E402
    is_narrative_defer as _is_narrative_defer,
    STRUCTURED_DEFER_PREFIXES as _STRUCTURED_DEFER_PREFIXES,
)
from gates.blocker_ref import (  # noqa: E402
    validate as _validate_blocker_ref,
    log_unstructured_override as _log_unstructured_override,
)
from gates.credential_enum import (  # noqa: E402
    check as _check_credential_enum,
    refusal_message as _credential_enum_refusal,
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


def _routes_away_from(intended_agent, agent_name) -> bool:
    """Daemon-side mirror of `aspirations.routes_away_from` ().

    Duplicated deliberately rather than imported: this module already keeps its
    own `_valid_intended_agents()` for the same layer-separation reason (the
    daemon resolves the roster through gates.capability_route, not through
    core/scripts/_agents). Keep the two in sync.

    Returns False for unset/None, "either", `agent_name` itself, AND any value
    outside the live vocabulary -- a retired agent or an unrecognized sentinel
    names nobody who can honor the routing, so refusing the claim stranded the
    goal permanently (it is invisible to the selector too). Conservative: an
    unresolvable roster ({"either"} alone) skips the vocabulary check.
    """
    # str() before strip(): see the CLI twin's comment -- the replaced
    # predicate compared with != and tolerated any type; a bare .strip() would
    # raise on a malformed value. Keep the two bodies byte-identical (a parity
    # test asserts it).
    ia = str(intended_agent or "").strip()
    if not ia or ia == "either" or ia == agent_name:
        return False
    # See the CLI twin for the full rationale: a roster-resolution failure must
    # never escape (the replaced predicate could not raise), and an unresolvable
    # roster takes the same conservative branch as an empty one. Bodies are kept
    # byte-identical -- a parity test asserts it.
    try:
        valid = _valid_intended_agents()
    except Exception:
        return True
    if len(valid) > 1 and ia not in valid:
        return False  # off-roster -> nobody can honor it -> treat as "either"
    return True

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
_GOAL_ID_RE = re.compile(r"^g-(\d{3}-\d{2,4}(-[a-z])?|xw-\d{8}T\d{6}-\d{2})$")  # 4-digit: asp-115 hit  (2026-05-19); g-xw-<ts>-NN cross-world ids ( made them selector-visible but the update/close path still rejected them -> stuck at 0/1 forever)
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
    """never-success-without-persistence invariant ().

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
    """Shared raw-S3 authoritative-store goal fetch ( / ).

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
        # : decode through the one transport seam — the store may be
        # gzip on the wire (magic-byte authoritative; plain passes through).
        raw = _codec_decode_response(obj, key=key).decode("utf-8", errors="replace")
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
    """never-success-without-persistence invariant for claim() ().

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
# completion) — the  class. Deliberately NOT every field: the
# verification costs one raw-S3 GET per write, acceptable at release/defer/
# complete frequency, not for bulk field edits ( cost note).
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
    co_parent_id checks (those depend on cross-record state). There is no
    fuller path to send callers to — wrappers are daemon-only (2026-05-14)
    — so CLI-only checks that matter at write time are restored one by one
    as shared gates/ modules called from the ADD sites (the _assert_*
    siblings below; guard-547), never from here (see each for the
    update-path blast-radius reasoning).

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
    """Raise ValueError on prose-only verification drift ().

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


def _assert_no_invalid_checks(goal: Dict[str, Any], *, ctx=None) -> None:
    """Raise ValueError on schema-invalid verification.checks ().

    SIBLING OF _assert_no_prose_drift, one layer deeper. That gate asks "does a
    goal advertising verification actually carry structured checks?"; this asks
    "are those checks EVALUABLE?" A goal can satisfy the first and fail this one
    completely -- which is the measured state of the queue: 19 of 29 structured
    checks on open world goals (66%) name a predicate type that does not exist or
    omit a field their type requires, so they can never gate anything in any world
    state. 18 of the 19 are `unknown predicate type`.

    Wired at the ADD sites for the SAME reason _assert_no_prose_drift is, and the
    reason is sharper here: those 19 invalid checks are LIVE. Calling this from
    _validate_goal would run it against the update_goal in-lock candidate too, and
    every status change on those goals -- claim, in-progress, complete -- would
    start failing validation. A filing-time gate that retroactively wedges existing
    work is worse than the drift it prevents.

    Blocks on `invalid` only. A VACUOUS check (well-formed, already satisfied at
    filing time) surfaces on stderr and is allowed through: it is a real signal but
    a weaker one, and refusing it would reject checks that merely need a later
    anchor.
    """
    import sys  # local-import convention (this module imports sys per-function)

    verdict = _check_schema_eval(
        goal,
        meta_dir=(ctx.paths.meta if ctx is not None else None),
        agent_name=((ctx.paths.agent_name or None) if ctx is not None else None),
    )
    if verdict["warning"]:
        print(f"[daemon] {verdict['warning']}", file=sys.stderr)
    if verdict["would_block"]:
        raise ValueError(verdict["message"])


def _assert_depends_on_consistency(goal: Dict[str, Any], *, ctx=None) -> None:
    """Raise ValueError when depends_on is not backed by blocked_by ().

    THIRD SIBLING of _assert_no_prose_drift / _assert_no_invalid_checks, and the
    same orphaning story: aspirations.py::validate_goal has enforced
    goal-schemas.md:636 ("each depends_on.goal_id MUST also appear in
    blocked_by") since the field existed, but the daemon _validate_goal subset
    omits it, and under no-python-cli-fallback the daemon IS the live write path.
    Measured 2026-08-20 over 2771 live goals: 6 non-empty depends_on carriers,
    exactly 1 conforming.

    The failure this prevents is quiet. blocked_by is the only field
    goal-selector.py reads for sequencing, so a goal carrying depends_on alone is
    offered for execution exactly as if it had no prerequisite — it LOOKS
    sequenced and is not. Two instances landed the same day (g-335-1319/20/21 and
    g-326-495/499): in the first, the scorer offered a frontend goal at rank 1
    while a partner held a live claim on the backend goal authoring the very
    route it would POST to. Both halves look correct in isolation, which is what
    makes it a divergence engine rather than a merge conflict.

    ADD SITES ONLY — see the module docstring's wiring note. Five live records
    violate this invariant today; routing it through _validate_goal would wedge
    every status change on them.
    """
    verdict = _depends_on_eval(
        goal,
        meta_dir=(ctx.paths.meta if ctx is not None else None),
        agent_name=((ctx.paths.agent_name or None) if ctx is not None else None),
    )
    if verdict["would_block"]:
        raise ValueError(verdict["message"])


def _assert_intended_agent_vocab(goal: Dict[str, Any], *, ctx=None) -> None:
    """Raise ValueError on an off-vocabulary intended_agent (selection-stack
    review 2026-08-21).

    FOURTH SIBLING of the three parity gates above, same orphaning story: the
    CLI validate_goal has checked intended_agent against the live vocabulary
    since g-282-02, the daemon _validate_goal subset omits it, and under
    no-python-cli-fallback the daemon is the live write path. Measured: 5 live
    goals carried "agent"/"reducer"/"any" — insight-trigger-sweep copied the
    board tag requires_action_by:<x> verbatim (all normalized 2026-08-21; the
    sweep now normalizes off-roster targets to "either" before filing).

    The read side tolerates off-vocab (g-115-3482 falls through to "either"),
    which is exactly why the write must refuse: the stored value misleads every
    reader, the fall-through is roster-dependent (invisible on a box whose
    roster read fails), and a typo of a real agent name silently converts a
    deliberate routing into a broadcast. Fail-open on an unresolvable or empty
    roster (rb-1028) — a fresh install must not refuse its first agent's name.

    ADD SITES ONLY — same _validate_goal blast-radius reasoning as the three
    siblings: legacy off-vocab carriers can arrive via merge from another box,
    and wedging their status changes is worse than the drift.
    """
    verdict = _intended_agent_vocab_eval(
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


def _archived_aspiration_hint(base_dir: Path, asp_id: str) -> str:
    """Error-path-only suffix distinguishing "archived out of the live store"
    from "never existed" (guard-1555: a lookup miss that collapses those two
    cases is indistinguishable from a typo, and an id found in NEITHER store is
    an anomaly to report rather than a silent skip).

    Motivating incident (g-115-5969): the analyze-npc-behavior skill filed every
    auto-generated improvement goal into asp-226 for the life of the skill. Once
    asp-226 was archived, each call returned a bare "not found" — accurate about
    the live store, yet `aspirations-read.sh --source world --id asp-226` STILL
    resolved it from the archive with status=completed. That asymmetry cost a
    two-step misdiagnosis: the refusal reads like a bad id, not a lifecycle event.

    ONE-DIRECTIONAL BY DESIGN. A HIT is trustworthy and upgrades the message; a
    MISS returns "" and changes nothing. The local aspirations-archive.jsonl is
    S3-backed and is never pulled -- every caller reads the LIVE file and appends
    here (g-115-3541) -- so the mirror may be stale and a miss can NEVER support a
    "does not exist anywhere" claim. Fail-open: any read error returns "".
    """
    try:
        for asp in _read_jsonl(base_dir / "aspirations-archive.jsonl"):
            if asp.get("id") == asp_id:
                status = asp.get("status") or "unknown"
                return (f" -- it is ARCHIVED (status={status}) and add-goal "
                        f"resolves targets in the LIVE store only. Retarget a "
                        f"live aspiration, or re-open this one.")
    except (OSError, ValueError):
        pass
    return ""


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
    # Mirrors core/scripts/aspirations.py::_emit_e9_skip_observation — see the
    # long rationale there. Short form: `skip_reason` is a field NO writer sets
    # (measured 2026-08-16, key structurally absent from every goal record), so
    # this chain fell through to the fallback on 100% of skips while
    # `outcome_note` held the actual rationale. outcome_note outranks
    # defer_reason because on a skipped goal the defer is a stale leftover and
    # the note is the skip decision. KEEP THE TWO COPIES IN SYNC.
    # `str(...)` + `.strip()` fix three cases a fresh-eyes probe found in the
    # bare `(x or "")[:300]` form, all introduced by the subscript: a dict
    # raised `KeyError: slice(None, 300, None)` from OUTSIDE the try below (so
    # this fail-open hook failed CLOSED, 500-ing an already-committed write), a
    # list leaked into the f-string as "Reason: ['a', 'b']", and a
    # whitespace-only note is truthy so it short-circuited past defer_reason
    # into "Reason:    .". Keep `outcome_note` INSIDE this chain — the two-copy
    # sync pin asserts on the chain's own text. See the CLI copy for detail.
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
    # counts evicted ids toward max+1 —  tombstone awareness).
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
        # alloc_nonce () — see add_goal() for the full rationale.
        # This lane MUST mint its own: it appends directly and never passes
        # through add_goal()'s setdefault, and it is the lane that produced 2
        # of the 3 observed same-goal splits.
        "alloc_nonce": uuid.uuid4().hex,
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


def _credential_enum_guard(ctx, goal_id: str, raw_ref: Any,
                           context_text: str):
    """Door-B credential-enumeration gate (). Returns a 400 Response
    to refuse, or None to allow.

    `raw_ref` MUST be the un-normalized header payload: `blocker_ref.validate()`
    rebuilds a 5-key envelope and drops `credential_source_enumeration`, so
    passing its output here would refuse every credentials-required blocker.

    Runs the SAME predicate as blocker-create-gate check #5 — the whole point of
    the shared module is that the two doors cannot drift apart. Non-credentials
    blocker types pass through untouched, so every other write is unchanged.

    Escape hatch: `X-Mind-Override-Blocker-Gate: <justification>` bypasses and
    appends to `world/blocker-gate-overrides.jsonl`, matching the CLI's
    `--override-blocker-gate` and the Door-A ledger.
    """
    # Local import — every endpoint in this package does the same; a
    # module-level `from ..server import Response` would close an import cycle
    # at load time. Omitting it raised NameError on the first LIVE refusal while
    # 64 structural/predicate tests stayed green: none executed this function's
    # runtime path. test_daemon_guard_executes_and_refuses now does.
    from ..server import Response

    result = _check_credential_enum(raw_ref)
    if result.get("passed"):
        return None

    override = _header_override(ctx, "X-Mind-Override-Blocker-Gate")
    if override:
        _log_unstructured_override(
            ctx.paths.world,
            goal_id=goal_id,
            defer_reason_text=context_text,
            justification=override,
            agent_name=ctx.paths.agent_name,
            source="daemon:update_goal:credential-enumeration-override",
            which_checks_bypassed=["credential_enumeration"],
        )
        return None

    return Response.json({
        "error": "credential_enumeration_failed",
        # Names the gate that FIRED (Door B). The shared-predicate lineage with
        # blocker-create-gate check #5 is stated in "message" — putting Door A's
        # name here would misroute anyone pivoting on this field.
        "gate": "credential-enumeration-gate",
        "reason": result.get("reason"),
        "message": _credential_enum_refusal(
            goal_id, result.get("reason", ""),
            flag_hint=("pass header X-Mind-Override-Blocker-Gate: "
                       "'<justification>' (audited)"),
        ),
    }, status=400)


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

    # 2b. approval-reference advisory (+ telemetry to META_DIR) — .
    # Warns on the fabricated-approval shape (rb-4517/rb-4513/guard-1328): a
    # high-blast-radius goal that asserts prior approval but carries no
    # verifiable reference. WARN-only — telemetry validates detector precision
    # before any promotion to a hard block (description_length.py precedent).
    ar_result = _approval_ref_eval(
        goal, source=source, meta_dir=ctx.paths.meta,
    )
    if ar_result["warned"]:
        warnings.append(ar_result["message"])

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
            # (): without this, the title-verb classifier can stamp a
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

    # === Phase D.5: forge-curriculum precondition guarantee () ===
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

    # blocker_ref SHAPE REFUSAL (). A blocker_ref must be absent,
    # empty, or a dict -- never a scalar. add-goal already routes its ref
    # through gates.blocker_ref.validate; this generic field-update path wrote
    # whatever the wrapper's parse_value mirror encoded, so
    # `aspirations-update-goal.sh <g> blocker_ref "<anything>"` stored a bare
    # string.
    #
    # THIS IS THE LIVE HALF OF THE FIX, and the CLI twin in
    # core/scripts/aspirations.py::cmd_update_goal carries the same refusal
    # (guard-2323: port a core/scripts fix to its mind_api twin in the SAME
    # change). Per the comment ~30 lines below, the framework is daemon-only, so
    # every real invocation arrives HERE -- a refusal added only to the CLI copy
    # would have been inert on the exact path that admits the defect
    # (guard-742).
    #
    # WHY A REFUSAL rather than an advisory: a bare string is not a judgement
    # call an author might defend, it is unreadable by every consumer. The
    # read-side guards test `isinstance(br, dict) and br.get("type")`, so a
    # scalar is SKIPPED rather than flagged; and no expires_at can be stored on
    # a string, so the TTL that would force a re-probe never arms. The value
    # gates work silently and indefinitely.
    #
    # MEASURED THREE TIMES, WHICH IS THE ACTUAL FINDING. The population is tiny
    # and self-clearing (a ref disappears when its goal unblocks or completes),
    # so every sweep reports "exactly ONE bare string" and looks stable while
    # naming a DIFFERENT record each time:  (2026-07-29), 
    # (2026-08-07),  (2026-08-11, a multi-sentence prose narrative --
    # a third distinct malformed shape). A count that stays arithmetically true
    # while its referent is replaced is invisible to exactly the check a careful
    # reader would run ("is it still 1?"), which is why backfilling the named
    # record was never the fix: the writer is live, so the residue regenerates.
    #
    # Clearing stays open (None / "" / {}) -- that is how the earlier instances
    # were legitimately retired, and refusing it would break the unblock path.
    if field == "blocker_ref" and value not in (None, "", {}):
        if not isinstance(value, dict):
            return Response.json({
                "error": "blocker_ref_shape",
                "gate": "blocker-ref-shape-gate",
                "detail": (
                    f"blocker_ref must be a JSON object, got "
                    f"{type(value).__name__}. A scalar is unreadable by every "
                    f"consumer: the read-side guards require a dict before any "
                    f"branch acts, so it is silently skipped, and no expires_at "
                    f"can be stored on it so the TTL never arms. Pass the "
                    f"canonical shape, e.g. {{\"type\": \"partner-response\", "
                    f"\"external_id\": \"<msg-or-goal-id>\"}}. To CLEAR it, "
                    f"pass null. (g-115-3843)"
                ),
                "received_type": type(value).__name__,
                "received_preview": repr(value)[:200],
            }, status=400), None, None

    if field == "status" and value == "completed":
        unc_result = _uncommitted_work_eval(
            goal_id=goal_id,
            override=_header_override(ctx, "X-Mind-Override-Uncommitted"),
            repo_path=ctx.paths.project_root,
            world_dir=ctx.paths.world,
            agent_name=ctx.paths.agent_name,
            # : WITHOUT THIS THE DELIVERY HALF OF THE GATE IS INERT.
            # The gate blocks a committed-but-unpushed close only for the role
            # contractually responsible for pushing (a worker Body does not
            # push -- ). This framework is daemon-only, so EVERY real
            # `aspirations-update-goal.sh <id> status completed` arrives here;
            # the CLI wrapper's own $BODY_ROLE read is not on the production
            # path. Read inline rather than via _header_override, whose
            # contract is specifically an override JUSTIFICATION -- the
            # empty-to-None coercion is identical, the meaning is not.
            body_role=(ctx.headers.get("x-mind-body-role") or "").strip() or None,
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
            # : THIS IS THE DEFER PATH — say so, or the gate's refusal
            # text recommends a flag this path ignores. gates.capability computes
            # `bypass_flag_hint` from caller_context ( GAP 2): "defer"
            # yields --force-defer (the flag honoured here — note the
            # X-Mind-Force-Defer header read two lines up), anything else yields
            # --override-agent-match, which aspirations-update-goal.sh plumbs ONLY
            # so argparse can redirect and explicitly does NOT honour on this path
            # (). Omitting this defaulted to "create-blocker", so a
            # caller following the message verbatim got override_applied=null and
            # a second refusal with no working escape named anywhere.
            #  fixed exactly this in aspirations.py
            # _run_capability_gate_for_defer (--caller-context defer) but not
            # here — and under daemon-only architecture (35 wrappers, no CLI
            # fallback) THIS is the only path that runs, so the fix landed
            # entirely on dead code. Measured 2026-07-29 on .
            caller_context="defer",
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
            # Door B credential-enumeration check (). Reads the RAW
            # header, NOT `parsed` — validate() rebuilds a 5-key envelope and
            # silently drops credential_source_enumeration, so checking its
            # output would refuse every credentials-required blocker.
            cred_err = _credential_enum_guard(ctx, goal_id, ref_header, str(value))
            if cred_err is not None:
                return cred_err, None, None
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
    """Phase D.5 (): post-decompose Self.md routing audit.

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

    # : honour the aspiration the audit module RESOLVED for this
    # deployment (it routes through _escalation_target), and file into whichever
    # queue actually holds it. Hardcoding "asp-115" + the world store reproduced
    # the original bug in a new costume: on any deployment without asp-115 the
    # id does not resolve, _find_aspiration returns None, and this function
    # returns None — dropping the escalation SILENTLY, with no error anywhere.
    asp_id = str(invest_spec.get("aspiration_id") or "asp-115")
    store_live, store_base = _resolve_paths(ctx, "world")
    try:
        if _find_aspiration(_read_jsonl(store_live), asp_id) is None:
            agent_live, agent_base = _resolve_paths(ctx, "agent")
            if _find_aspiration(_read_jsonl(agent_live), asp_id) is not None:
                store_live, store_base = agent_live, agent_base
    except (OSError, ValueError):
        pass  # fail-open to world — preserves the prior behaviour exactly
    # This probe is deliberately outside the lock: it only SELECTS a store. If
    # the queue changes between probe and lock, the locked _find_aspiration
    # below still returns None and we fall back to the pre-existing behaviour,
    # so the race can only reproduce the old outcome, never a wrong write.
    agent = _agent_name(ctx)
    origin_signal_val = invest_spec.get("origin_signal", "")

    try:
        with file_locks.locked(store_live):
            items = _read_jsonl(store_live)
            found = _find_aspiration(items, asp_id)
            if found is None:
                return None
            _, asp = found

            # Idempotent dedup: skip if a pending/in-progress Investigate
            # with the same origin_signal already exists in that aspiration.
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
                # alloc_nonce () — third mint site; appends directly,
                # so it needs its own stamp. See add_goal() for rationale.
                "alloc_nonce": uuid.uuid4().hex,
                # Roster-aware: this direct append bypasses the add-path vocab
                # gate, and a literal "bravo" is off-vocab on deployments
                # without one (e.g. single-agent prod).
                "intended_agent": (
                    "bravo" if "bravo" in _valid_intended_agents()
                    else "either"),
                "work_class": "framework",
            }
            try:
                _validate_goal(invest_goal)
            except ValueError:
                return None

            asp.setdefault("goals", []).append(invest_goal)
            history.snapshot(
                store_live, store_base, agent,
                summary=f"add-goal {invest_goal['id']} (routing-audit)")
            _atomic_write_jsonl(store_live, items)
            changelog.append(
                store_base, agent, store_live, "edit",
                summary=f"add-goal {invest_goal['id']} (routing-audit)",
                lines_changed=len(items))
            _jsonl_cache().invalidate(store_live)
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

    # UNKNOWN-FIELD GATE, ADD half ( item 3 — selection-stack review
    # 2026-08-21). The update endpoint has refused unknown field names since
    # item 1 (see update_goal), but a brand-new goal could still be BORN with
    # arbitrary keys — the allowlist was enforced everywhere except the moment
    # a record is created. Same SSOT (_goal_fields), same header override, same
    # audit trail. Checked on the CALLER's payload before the daemon stamps its
    # own defaults (all of which are registered names), and before path
    # resolution so a keystroke slip costs nothing. Scoped to THIS endpoint —
    # the bulk aspiration add (_validate_aspiration) is framework-constructed
    # and, on a cross-deployment transplant, may legitimately carry fields a
    # lagging local allowlist has not learned yet; refusing there, where no ctx
    # (and so no override header) exists, would wedge seed plants with no
    # escape hatch.
    _unknown_fields = [k for k in goal if not _is_known_goal_field(k)]
    if _unknown_fields:
        override = _header_override(ctx, "X-Mind-Allow-New-Field")
        if not override:
            return Response.json({
                "error": "unknown_goal_field",
                "gate": "goal-field-allowlist",
                "field": _unknown_fields[0],
                "unknown_fields": _unknown_fields,
                "message": _unknown_goal_field_error(_unknown_fields[0]) + (
                    f" ({len(_unknown_fields)} unknown fields in this add: "
                    f"{_unknown_fields})" if len(_unknown_fields) > 1 else ""),
            }, status=400)
        # Overridden: a genuinely new field is a DELIBERATE act with a
        # justification on the audit ledger rather than a keystroke slip.
        _log_unstructured_override(
            ctx.paths.world,
            goal_id=goal.get("id") or "<auto>",
            defer_reason_text=f"new goal field(s) {_unknown_fields!r} at add",
            justification=override,
            agent_name=ctx.paths.agent_name,
            source="daemon:add_goal:allow-new-field",
            which_checks_bypassed=["goal_field_allowlist"],
        )

    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    # Status/timestamp defaults applied outside the lock — they don't depend
    # on file state. ID allocation MUST happen inside the lock (below) so two
    # concurrent writers can't allocate the same g-NNN-NN sequence.
    goal.setdefault("status", "pending")
    goal.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    # alloc_nonce (): an IMMUTABLE, UNIQUE allocation stamp — the only
    # thing a goal carries that is both. It exists so coordination_merge can tell
    # "the same logical goal, edited on two boxes" from "two distinct goals that
    # collided on an id", without keying on anything a later edit can change.
    #
    # WHY NOTHING ELSE WORKS (the trade  had to decide). `created_at`
    # is immutable but only second-precision, so it is not unique. `id` is unique
    # within a store but MUTABLE — the collision path re-ids goals, which is the
    # whole reason identity could not key on it. `title` is neither: keying on it
    # (the pre-fix behaviour) meant a title edit racing a stale snapshot gave the
    # two copies different identities, so they never collapsed and one was
    # displaced to a fresh id — one goal silently became two (proven live:
    #  carries displaced_from=). Two of the three confirmed
    # instances were `Apply:` -> `Unblock:` retitles, so it is a systematic shape,
    # not a freak race. A random nonce is immutable by construction and unique
    # without needing precision, which is why it is the fix rather than a wider
    # tuple of existing fields.
    #
    # setdefault, matching created_at/filed_by_agent: an explicit caller value is
    # preserved, and goals written before this field simply lack it — merge falls
    # back to the previous (created_at, title) identity for them, so this is a
    # no-op for every existing goal (see coordination_merge._goal_identity).
    goal.setdefault("alloc_nonce", uuid.uuid4().hex)
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
                return Response.error(
                    404, "aspiration_not_found",
                    f"Aspiration {asp_id} not found in {source}"
                    + _archived_aspiration_hint(base_dir, asp_id))
            asp_idx, asp = found

            if "id" not in goal:
                goal["id"] = _allocate_goal_id(asp)
            # Uniqueness guard (): a caller-supplied id can collide
            # with an existing goal in this aspiration — INCLUDING a completed
            # one — the cross-Mind-promotion-injection corruption class that
            # produced two distinct goals both id . Every id-based op
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
                # Prose-verification-drift parity (): the daemon
                # _validate_goal subset omits this check that the CLI runs;
                # without it a daemon-added prose-only goal (markers present,
                # verification.checks empty) slips through. ctx routes the
                # firing telemetry to the calling agent.
                _assert_no_prose_drift(goal, ctx=ctx)
                # Structured-check schema parity (). Prose-drift asks
                # whether checks EXIST; this asks whether they can be EVALUATED.
                # A goal passes the first and fails this one whenever it carries
                # a plausible-looking type name that predicate.py cannot dispatch
                # — the shape of 18 of the 19 invalid checks now in the queue.
                _assert_no_invalid_checks(goal, ctx=ctx)
                # depends_on/blocked_by parity (). Third instance of
                # the same CLI-only-check orphaning: a goal carrying depends_on
                # with no matching blocked_by is invisible to the selector's
                # sequencing predicate and gets offered as if unblocked.
                _assert_depends_on_consistency(goal, ctx=ctx)
                # intended_agent vocabulary parity (selection-stack review
                # 2026-08-21). Fourth instance: an off-vocab routing hint
                # ("any"/"reducer"/a typo) names nobody, misleads readers,
                # and routes nondeterministically per box.
                _assert_intended_agent_vocab(goal, ctx=ctx)
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

    # never-success-without-persistence invariant (): the write above
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

    # Phase D.5 (): Routing-audit after main goal persists.
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

    # UNKNOWN-FIELD GATE (). Direct sibling of the dotted-path check
    # above — both refuse a name that would silently create a key on the shared
    # goal record that no consumer reads. Measured before the gate: 147 distinct
    # top-level keys across 2,791 live goals, 27 of them strays, including a
    # `precondition_unmet` FIELD (that string is a defer_reason PREFIX) on a goal
    # whose author believed it had been deferred. It never was, and nothing said so.
    #
    # Placed AFTER the dotted check and BEFORE source/agent resolution so a bad
    # field name costs nothing: no path resolution, no gates, no lock.
    if not _is_known_goal_field(field):
        override = _header_override(ctx, "X-Mind-Allow-New-Field")
        if not override:
            return Response.json({
                "error": "unknown_goal_field",
                "gate": "goal-field-allowlist",
                "field": field,
                "message": _unknown_goal_field_error(field),
            }, status=400)
        # Overridden: the write proceeds, but a genuinely new field is now a
        # DELIBERATE act with a justification on the audit ledger rather than a
        # keystroke slip nobody ever sees.
        _log_unstructured_override(
            ctx.paths.world,
            goal_id=goal_id,
            defer_reason_text=f"new goal field {field!r}",
            justification=override,
            agent_name=ctx.paths.agent_name,
            source="daemon:update_goal:allow-new-field",
            which_checks_bypassed=["goal_field_allowlist"],
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
    # Superseded direct-set block. THIS is the live path (the running daemon
    # serves it); core/scripts/aspirations.py cmd_update_goal carries the
    # mirror, and the two message texts must be changed TOGETHER — a fix to
    # the CLI copy alone is inert in production (guard-984: the daemon
    # imported its module at startup and does not reload, so a green test
    # suite is not evidence the message a caller sees has changed).
    # Deliberately no line numbers here: the reference this comment used to
    # carry had drifted, and so had the one in 's own description —
    # grep the `value == "superseded"` guard in cmd_update_goal instead.
    # superseded transitions go through the intent-satisfaction evidence gate
    # in aspirations-complete-intent.sh — never via direct update-goal.
    if field == "status" and value == "superseded":
        return Response.error(
            400, "invalid_status_transition",
            f"Cannot set status=superseded directly on {goal_id}. Pick the "
            f"route that matches what is true: (1) THE WHOLE ASPIRATION's "
            f"intent is satisfied -- aspirations-complete-intent.sh <asp-id> "
            f"with intent_satisfaction JSON listing this goal in "
            f"superseded_goal_ids; note its evidence gate requires every "
            f"non-recurring goal in the aspiration to be terminal after the "
            f"supersession, so this route is unavailable for ONE goal in a "
            f"live aspiration. (2) THIS GOAL ALONE is moot because a sibling "
            f"shipped its scope -- write the supersession evidence (the "
            f"sibling's goal id + what it delivered) to outcome_note FIRST, "
            f"then set status=skipped; that is the order and the status "
            f"unblock-parent-status-sweep.py::_mark_skipped already uses for "
            f"the structurally identical case. (3) The work is still WANTED "
            f"and merely waiting on another goal -- use status=blocked, NOT "
            f"skipped: skipped is invisible to the blocked-signal sweeps "
            f"(precheck 0.5b.11/0.5b.12 scan status=blocked), so nothing will "
            f"resurface it when the dependency lands (guard-1690).",
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
            # Door B credential-enumeration check () — RAW header,
            # same reason as the defer site above.
            cred_err = _credential_enum_guard(ctx, goal_id, ref_header,
                                              "status=blocked")
            if cred_err is not None:
                return cred_err
            blocker_ref_for_blocked_status = parsed

    # Warnings collected during the write — surfaced on the 200 response so
    # wrappers can re-emit to stderr (matches add_goal). Pure-logic advisories
    # only here; gates that block live in _run_update_goal_gates above.
    warnings: List[str] = []

    # Capability-absence advisory (). MIRROR of the CLI-side advisory
    # in core/scripts/aspirations.py cmd_update_goal — guard-742: this logic
    # lives on BOTH sides, keep them in sync or it is half-applied.
    #
    # THIS is the live half. aspirations-update-goal.sh is DAEMON-ONLY (no CLI
    # fallback since the 2026-05-14 cutover), so every agent write lands here;
    # the CLI entry serves only the rb-428 sweeps, which invoke aspirations.py
    # directly as a subprocess. Wiring the CLI alone would have produced an
    # advisory that never fires on the path that matters — the same defect this
    # goal exists to fix, since exhaustive-search-gate (5 firings, all noop) and
    # verify-before-assuming-gate (0 firings) are inert for exactly that reason.
    #
    # Appending to `warnings` rather than printing is what makes it REACHABLE:
    # daemon stderr goes to the daemon log, not to the caller, but the wrapper
    # re-emits resp["warnings"] to stderr (aspirations-update-goal.sh:185-186),
    # which does reach the model.
    if field in ("defer_reason", "description", "outcome_note"):
        try:
            from _capability_absence_patterns import advise as _cap_advise
            _cap_msg = _cap_advise(value, field=field, goal_id=goal_id)
            if _cap_msg:
                warnings.append(_cap_msg)
        except Exception:
            pass  # advisory must never break a durable write

    # Defer-target existence advisory (). Same twin shape as the
    # capability-absence advisory directly above, and THIS is the live half for
    # the same reason: a version of this check that lived only in
    # core/scripts/aspirations.py emitted nothing through the wrapper on an
    # end-to-end probe (guard-742/guard-2323 re-derived by measurement).
    #
    # Fires on the FIELD, deliberately NOT on `_is_narrative_defer`. That
    # predicate is False for every STRUCTURED_DEFER_PREFIXES value, and
    # structured defers are where dependency ids live: measured 2026-08-22, of
    # the 79 non-terminal defers citing a goal id, 79 were structured and 0 were
    # narrative. Gating on it would have fired on zero of the real population
    # while reviewing as correct — the guard-1802 class.
    #
    # Advisory, never a refusal: of 51 cited ids resolving nowhere, 41 still
    # have a world-surface footprint and 20 appear in committed framework files,
    # so the citations were mostly RIGHT and the store lost the records.
    # Refusing would block correct writes to punish a defect elsewhere.
    if field == "defer_reason" and value not in (None, ""):
        try:
            from gates.defer_target_existence import (
                evaluate as _defer_target_eval,
                sources_for as _defer_target_sources,
            )
            _dt = _defer_target_eval(
                goal_id, value,
                _defer_target_sources(ctx.paths.world, ctx.paths.agents_root),
            )
            if _dt.get("message"):
                warnings.append(_dt["message"])
        except Exception:
            pass  # advisory must never break a durable write

    # Structured-check schema on the verification-EDIT path — PRE-LOCK by
    # . This ran INSIDE the lock until 2026-08-08, violating this
    # function's own rule at the top ("Gates (run BEFORE the lock — slow I/O
    # must not hold writers)"). _check_schema_eval -> classify() ->
    # predicate.evaluate() reaches FIVE subprocess.run sites in
    # core/scripts/predicate.py: L384 command_succeeds and L521
    # metric_threshold (arbitrary shell, shell=True, up to MAX_COMMAND_TIMEOUT
    # =120s each), plus L277 resolve_after_ref (git show), L668
    # vcs_commits_since (git), and L795 pr_merged (`gh pr view` — a NETWORK
    # round-trip to GitHub). Measured on the two shell handlers alone: a
    # command_succeeds check at timeout_seconds=20 cost 20.0s + 20.0s = 40.0s
    # of lock-held subprocess, and at the 120s ceiling one verification edit
    # held world/aspirations.jsonl for up to 240s — blocking EVERY agent's
    # goal write fleet-wide for that duration. The cost is doubled BY
    # CONSTRUCTION, not incidentally: a no-regression policy must compare
    # before against after, so it pays the command cost twice.
    #
    # THE ASYMMETRY THAT MAKES THIS SAFE. `new` needs NO goal load at all —
    # check_schema.evaluate reads only goal["verification"]["checks"], so a
    # synthetic {"verification": value} is byte-equivalent to the old
    # candidate-overlay and carries ZERO TOCTOU. Only `cur` needs the stored
    # record, and reading it unlocked is a BENIGN race in one direction only:
    # a stale `cur` can list FEWER pre-existing invalid checks than the goal
    # really has, which can only make `introduced` larger — i.e. the gate
    # errs toward refusing, never toward wrongly admitting a regression. It
    # cannot produce a false PASS.
    if field == "verification":
        try:
            _pre_items = _read_jsonl(live_path)
            _pre_found = _find_goal(_pre_items, goal_id)
        except Exception:
            _pre_found = None  # fail-open: the in-lock 404 below is authoritative
        if _pre_found is not None:
            _pre_goal = _pre_found[2]["goals"][_pre_found[1]]
            cur = _check_schema_eval(_pre_goal, meta_dir=ctx.paths.meta,
                                     agent_name=ctx.paths.agent_name or None)
            new = _check_schema_eval({"verification": value},
                                     meta_dir=ctx.paths.meta,
                                     agent_name=ctx.paths.agent_name or None)
            sig = lambda r: {(c["type"], c["reason"]) for c in r["invalid"]}  # noqa: E731
            introduced = sig(new) - sig(cur)
            if introduced:
                return Response.error(
                    400, "validation_failed",
                    new["message"] + "\n  (verification edit refused: it "
                    f"introduces {len(introduced)} NEW schema-invalid check(s). "
                    "Pre-existing invalid checks on this goal are not blocking "
                    "this edit — only the new one(s) are.)")
            if new["warning"]:
                import sys  # local-import convention (see _assert_no_prose_drift)

                print(f"[daemon] {new['warning']}", file=sys.stderr)

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

            # : a session whose claim was taken over used to learn
            # nothing here and keep working. This is the earliest point at
            # which the displaced Body reliably touches the daemon, so it is
            # where it finds out. Appended to `warnings` (not printed) because
            # daemon stderr goes to the daemon log while the wrapper re-emits
            # resp["warnings"] to the caller's stderr -- the reachability the
            # stderr-only take-over log never had.
            _disp = _displaced_claim_warning(ctx, goal, goal_id)
            if _disp:
                warnings.append(_disp)

            # Prose-verification-drift on description / verification edits
            # (): the add path validates via _assert_no_prose_drift,
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

            # Structured-check schema on the verification-EDIT path (),
            # under a NO-REGRESSION policy rather than the ADD path's flat refusal.
            #
            # The difference is forced by the live queue, not by taste. 19 of 29
            # structured checks on open world goals are already schema-invalid. A
            # flat refusal here would mean that editing `verification.outcomes` on
            # any of those 19 goals -- an edit that touches nothing wrong -- fails
            # validation until someone first repairs a check they did not come to
            # touch. That converts a filing-time validator into a blocker on
            # unrelated live work, which is the one outcome that would get this
            # gate turned off.
            #
            # So the test is DIRECTIONAL: block only when the edit makes the
            # invalid set WORSE than what the goal already carried. Fixing checks
            # passes, leaving them alone passes, adding a new broken one does not.
            # Signatures are (type, reason) pairs so a re-ordered checks list is
            # not mistaken for a regression.
            #
            # THE EVALUATION ITSELF NOW RUNS PRE-LOCK () — see the
            # `if field == "verification":` block just above the `try:` that
            # opens this lock. It stays OUT of here permanently: classify()
            # shells out at five sites in predicate.py, one of them a network
            # call to GitHub, and holding this lock across that blocks every
            # agent's goal write fleet-wide. Do NOT move it back in for the
            # convenience of having `goal` already loaded — `new` needs no goal
            # at all, and a pre-lock `cur` can only err toward refusing.

            # === PR 7i in-lock status guards ===
            # Guards that need goal state run AFTER the goal load and BEFORE
            # any mutation. Order mirrors cmd_update_goal: cross-lane TAKEOVER
            # first, recurring-completed second, blocker-ref requirement third.

            # Cross-lane / cross-BODY TAKEOVER guard (). MIRROR of
            # cmd_update_goal in core/scripts/aspirations.py (the guard sitting
            # directly above its recurring-completed block). guard-742: this
            # logic lives on BOTH sides — keep them in sync or it is
            # half-applied.
            #
            # THIS SIDE IS THE LIVE PATH AND HAD NO TAKEOVER GUARD AT ALL until
            # . `aspirations-update-goal.sh` is a daemon-only wrapper
            # (no CLI fallback since the 2026-05-14 cutover), so every wrapper
            # write lands HERE — while the only takeover guard in the system sat
            # in cmd_update_goal, which the wrapper never reaches. The CLI-side
            # comment asserting a mirror "in the PR 7i in-lock block" described
            # a mirror that did not exist; measured 2026-08-06, when
            # `_routes_away_from` had exactly one call site in this file, inside
            # claim(). That asymmetry is the whole mechanism of the 2026-08-05
            # incident: claim() REFUSED the goal and the very next
            # update-goal write LANDED, because the refusal and the write were
            # enforced by different code and only one of them existed on the
            # path the wrapper takes.
            #
            # THREE conditions, any one refuses. The SID condition is PRIMARY:
            # a worker Body and its reducer are BOTH `alpha`, so an agent-name
            # comparison is FALSE for the two-body collision and only the
            # session id separates them (foxtrot, 2026-08-06 09:11).
            #
            # SCOPE IS DELIBERATELY NARROW — takeover only (status->in-progress,
            # claimed_by). Do NOT widen to all status writes: the rb-428 sweeps
            # mutate foreign-lane goals BY DESIGN (skipped / completed /
            # defer_reason / lastAchievedAt), and a blanket cross-lane refusal
            # breaks every one of them (the  over-fix trap).
            if ((field == "status" and value == "in-progress")
                    or field == "claimed_by"):
                _caller = _agent_name(ctx)
                _req_sid = (ctx.query.get("sid") or "").strip() or None
                _held_by = goal.get("claimed_by")
                _held_sid = goal.get("claimed_by_sid")
                _intended = goal.get("intended_agent")
                _xl = (ctx.query.get("cross_lane") or "").strip() or None

                # MISSING-SID SEMANTICS — the two missing-sid cases are NOT
                # symmetric, and collapsing them is how this guard would end up
                # bypassable. Stated explicitly because the fail direction is a
                # real trade, not an oversight to be rediscovered later.
                #
                #   STORED sid absent (`claimed_by_sid` unset) -> ABSTAIN.
                #     Pre- records legitimately carry no claim sid (see
                #     _completed_by_sid). Refusing them would wedge real work to
                #     close a hole, so the sid axis simply does not vote.
                #
                #   REQUEST sid absent while a STORED one exists -> REFUSE.
                #     This is the bypass vector, not an abstention: if the guard
                #     goes quiet whenever the caller omits `sid`, then unsetting
                #     MIND_SID defeats it entirely. claim() reached the same
                #     conclusion the hard way — its case 5b (-b) had
                #     previously ALLOWED a no-sid claim, "which left the guard
                #     bypassable by omitting a param". The holder demonstrably
                #     had a sid, so a caller writing over it should too.
                #
                # Residual cost, named rather than hidden: when NEITHER side has
                # a sid, a same-agent two-body collision is undetectable here and
                # PASSES. Cross-AGENT collisions need no sid and are still caught
                # by _agent_conflict. The refusal is loud and carries the
                # cross_lane override, so a hook-timeout that drops MIND_SID
                # surfaces as a clear message with an escape hatch rather than as
                # silent corruption.
                _sid_conflict = bool(_held_sid and _req_sid
                                     and _held_sid != _req_sid)
                _sid_unprovable = bool(_held_sid and not _req_sid)
                _agent_conflict = bool(_held_by and _held_by != _caller)
                _lane_conflict = _routes_away_from(_intended, _caller)

                if (_sid_conflict or _sid_unprovable
                        or _agent_conflict or _lane_conflict):
                    if not _xl:
                        # REASON ORDER != CHECK ORDER, deliberately. All three
                        # conditions refuse; this picks which one to NAME. The
                        # agent fact is named first because the sid wording
                        # ("two Bodies of X") is only TRUE when the holder and
                        # the caller are the same agent — on a cross-agent
                        # takeover both axes differ, and naming the sid there
                        # would assert a two-body collision that is not
                        # happening, sending the next reader after the wrong
                        # mechanism entirely.
                        if _agent_conflict:
                            _why = (f"claimed by '{_held_by}' but the caller "
                                    f"is '{_caller}'")
                        elif _sid_conflict:
                            _why = (f"held by session '{_held_sid}' but this "
                                    f"request is session '{_req_sid}' — two "
                                    f"Bodies of '{_caller}'")
                        elif _sid_unprovable:
                            _why = (f"held by session '{_held_sid}' but this "
                                    f"request carries NO session id, so it "
                                    f"cannot be shown to be the same Body of "
                                    f"'{_caller}'")
                        else:
                            _why = (f"routed to '{_intended}' but the caller "
                                    f"is '{_caller}'")
                        _at = goal.get("claimed_at") or "an unrecorded time"
                        return Response.error(
                            400, "takeover_refused",
                            f"Goal {goal_id} is {_why} (claimed at {_at}). "
                            f"Refusing the TAKEOVER write (field={field}). "
                            f"Pass cross_lane=<justification> to override "
                            f"(logged to override-bypass-ledger.jsonl). "
                            f"Non-takeover cross-lane writes (skipped / "
                            f"completed / defer_reason) are unaffected.")
                    _audit_cross_lane_claim_inline(
                        ctx, goal_id=goal_id, agent_claiming=_caller,
                        intended_agent=(
                            f"{_held_by or _intended or 'unknown'}@{_held_sid}"
                            if (_sid_conflict or _sid_unprovable)
                            else (_held_by or _intended or "unknown")),
                        justification=_xl,
                        category=goal.get("category"),
                        title=goal.get("title"))
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

            # Layer-B residual-work gate (; Layer A = Step 8.55 in
            # aspirations-state-update + guard-3601, honor-system). Refuses
            # status=completed when the goal's outcome_note names undone work
            # (deliberate scope narrowing — the  class: a spec on a
            # COMPLETED record is invisible to every selector) and no LIVE
            # carrier is cited. Accept paths: a cited pending/in-progress
            # carrier goal id, an explicit owner decline, or the audited
            # X-Mind-Override-Residual header. Runs in-lock because it reads
            # goal.outcome_note; pure regex + in-memory scans, safe here.
            #
            # Layer D (mirror of the defer-gate auto-Unblock above): on block,
            # file the suggested successor UNDER THIS SAME HELD LOCK before
            # returning the 400, so the refusal never strands the agent at
            # the same decision point. The status flip itself is NEVER
            # committed (we are before `goal[field] = value`); only the
            # successor is persisted. The agent's escape is one update: cite
            # the filed id in outcome_note ("residual carried by g-NNN-NN"),
            # then retry the close — accept path 1 then passes against the
            # now-live successor.
            if field == "status" and value == "completed":
                rw_override = _header_override(
                    ctx, "X-Mind-Override-Residual")
                # THE OTHER QUEUE — the one that is NOT the ?source target
                # (). `items` is the target queue, so this must be
                # selected BY SOURCE: reading the agent queue unconditionally
                # made both arguments identical on a `source=agent` close and
                # the world queue was never loaded, so every world carrier
                # reported live:false / status:null and the gate auto-filed
                # duplicates for work already owned. Ported in the same change
                # as the CLI twin in core/scripts/aspirations.py per
                # guard-2323 — the daemon is the LIVE path, so a CLI-only fix
                # would have been inert in production from the moment it
                # landed.
                _rw_other_items = None
                if source == "agent":
                    _rw_other_live = ctx.paths.world / "aspirations.jsonl"
                else:
                    _rw_other_live = (
                        ctx.paths.agent / "aspirations.jsonl"
                        if ctx.paths.agent is not None else None)
                if _rw_other_live is not None and _rw_other_live.exists():
                    try:
                        _rw_other_items = _read_jsonl(_rw_other_live)
                    except Exception:
                        _rw_other_items = None  # fail-open cross-queue
                rw_result = _residual_work_eval(
                    goal_id=goal_id,
                    outcome_note=str(goal.get("outcome_note") or ""),
                    override=rw_override,
                    items=items,
                    other_items=_rw_other_items,
                    world_dir=ctx.paths.world,
                    agent_name=ctx.paths.agent_name or "",
                    goal_priority=goal.get("priority"),
                    goal_category=goal.get("category"),
                )
                if rw_result.get("would_block"):
                    filed_successor_id = None
                    filing_status = "not_attempted"
                    existing = _rw_find_existing_successor(
                        items, goal_id, _rw_other_items)
                    if existing is not None:
                        filing_status = (
                            f"existing successor {existing.get('id')} "
                            f"pending in {existing.get('_aspiration_id')} "
                            f"({existing.get('_source')} queue, strategy="
                            f"{existing.get('_match_strategy')}) — "
                            f"idempotent skip; cite it in outcome_note")
                    else:
                        # Target routing: the original goal's own aspiration
                        # first (a residual continues that aspiration's work
                        # — consolidate-before-expand tail pull), then the
                        # first active aspiration.
                        _rw_target = asp
                        if _rw_target is None or _rw_target.get(
                                "status") not in (None, "active"):
                            _rw_target = next(
                                (a for a in items
                                 if a.get("status") == "active"), None)
                        if _rw_target is None:
                            filing_status = ("no target aspiration "
                                             "available — filing skipped")
                        else:
                            _rw_new_id = _allocate_goal_id(_rw_target)
                            _rw_goal = _rw_build_successor(
                                goal_id, rw_result, _rw_new_id)
                            try:
                                _validate_goal(_rw_goal)
                                _rw_target.setdefault("goals", []).append(
                                    _rw_goal)
                                history.snapshot(
                                    live_path, base_dir, agent,
                                    summary=(f"residual-gate filed successor "
                                             f"{_rw_new_id} for {goal_id}"))
                                _atomic_write_jsonl(live_path, items)
                                changelog.append(
                                    base_dir, agent, live_path, "edit",
                                    summary=(f"residual-gate filed successor "
                                             f"{_rw_new_id} for {goal_id}"),
                                    lines_changed=len(items))
                                _jsonl_cache().invalidate(live_path)
                                filed_successor_id = _rw_new_id
                                filing_status = "filed"
                            except (ValueError, OSError) as exc:
                                # Fail-open: the refusal still stands; the
                                # successor is best-effort.
                                filing_status = f"filing failed: {exc}"
                    return Response.json({
                        "error": "residual_work_blocked",
                        "gate": "residual-work-gate",
                        "gate_output": rw_result,
                        "filed_successor_id": filed_successor_id,
                        "successor_filing_status": filing_status,
                        "detail": (
                            f"outcome_note on {goal_id} names undone work "
                            f"(markers: "
                            f"{', '.join(rw_result['matched_markers'])}) "
                            f"with no live carrier cited. Either cite a "
                            f"live carrier in outcome_note (e.g. 'residual "
                            f"carried by "
                            f"{filed_successor_id or 'g-NNN-NN'}') and "
                            f"retry, record an explicit owner decline, or "
                            f"pass --override-residual \"<justification>\" "
                            f"(audited to residual-work-overrides.jsonl)."),
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

            # : mirror of the cmd_update_goal normalization. A DIRECT
            # `blocker_ref` field write reached this generic assignment with no
            # validation, no alias normalization and no TTL — _validate_blocker_ref
            # was reachable only via the X-Mind-Blocker-Ref HEADER path above.
            # Both writers must normalize or the CLI and daemon disagree on the
            # stored shape (guard-330). DICTS ONLY: a bare-string ref is a live
            # reader-supported shape tracked separately ().
            if field == "blocker_ref" and value not in (None, ""):
                _candidate_ref = value
                if isinstance(_candidate_ref, str):
                    try:
                        _candidate_ref = json.loads(_candidate_ref)
                    except (json.JSONDecodeError, TypeError):
                        _candidate_ref = None      # bare string ref — out of scope
                if isinstance(_candidate_ref, dict):
                    _ok_ref, _norm_ref = _validate_blocker_ref(_candidate_ref)
                    if not _ok_ref:
                        return Response.error(
                            400, "blocker_ref_invalid",
                            f"{_norm_ref} (goal {goal_id}; a direct blocker_ref "
                            f"field write is normalized by the same validator as "
                            f"the X-Mind-Blocker-Ref header — g-115-3532)",
                        )
                    value = _norm_ref

            # === field-shrink guard () — DAEMON HALF ===
            # MUST run here: the predicate needs the OLD value, so it cannot
            # live in the pre-lock _run_update_goal_gates chain (which sees only
            # the incoming write). Byte-parallel with the cmd_update_goal half
            # (guard-547) — both import the same gates.field_shrink predicate
            # rather than re-deriving the thresholds, so CLI and daemon cannot
            # disagree about what a catastrophic shrink is (guard-330).
            #
            # NOT wrapped in try/except, deliberately: the predicate is pure
            # length arithmetic with no dependency to fail, and a fail-open
            # handler here would also cover the refusal-message construction —
            # turning a compose-time bug into a silent approval (guard-3803).
            _shrink = _field_shrink_eval(field, goal.get(field), value)
            if _shrink["blocked"]:
                _shrink_override = _header_override(
                    ctx, "X-Mind-Override-Shrink")
                _gate_log.log(
                    "field-shrink-guard",
                    "override" if _shrink_override else "block",
                    caller="daemon:update_goal",
                    trigger_matched=_shrink["decision_path"],
                    payload=goal_id,
                    override_reason=_shrink_override,
                    extra={"field": field, "old_len": _shrink["old_len"],
                           "new_len": _shrink["new_len"],
                           "ratio": _shrink["ratio"]},
                    meta_dir=ctx.paths.meta,
                    agent_name=ctx.paths.agent_name,
                )
                if not _shrink_override:
                    return Response.json({
                        "error": "field_shrink_blocked",
                        "gate": "field-shrink-guard",
                        "field": field,
                        "old_len": _shrink["old_len"],
                        "new_len": _shrink["new_len"],
                        "ratio": _shrink["ratio"],
                        "detail": f"{_shrink['message']} (goal {goal_id})",
                    }, status=400)
            else:
                _gate_log.log(
                    "field-shrink-guard", "noop",
                    caller="daemon:update_goal",
                    trigger_matched=_shrink["decision_path"],
                    payload=goal_id,
                    extra={"field": field, "old_len": _shrink["old_len"],
                           "new_len": _shrink["new_len"],
                           "ratio": _shrink["ratio"]},
                    meta_dir=ctx.paths.meta,
                    agent_name=ctx.paths.agent_name,
                )

            # Capture old_status BEFORE the mutation — the selection_count
            # cascade compares old vs new to stay idempotent on redundant
            # in-progress writes. Moving this read below `goal[field] = value`
            # would inflate selection_count on every resume/retry of the same
            # in-progress goal (mirror of cmd_update_goal line 2080 invariant).
            old_status = goal.get("status")
            # : capture pre-update interval_hours BEFORE the write so the
            # anchor-persist cascade below records the ORIGINAL cadence, not the
            # incoming (possibly already-extended) value. Mirror of cmd_update_goal.
            _prev_interval_hours = goal.get("interval_hours")

            goal[field] = value
            # asp is items[asp_idx] (same dict reference); mutation above
            # already persists. No rebind needed.

            #  (unbounded interval-ratchet fix — DAEMON MIRROR of the
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

            #  (DAEMON MIRROR of the aspirations.py cmd_update_goal
            # deliberate-raise re-base; guard-742 byte-parallel). THIS is the live
            # batch/manual apply path, so a CLI-only fix here is the half that
            # never runs.
            #
            # The write-once branch above is correct for the CAP consumer
            # (cargo-cult-detector: proposed = min(interval*multiplier,
            # original*cap_ratio)) — a freely-mutable anchor there is the
            #  unbounded ratchet it exists to stop. But the FLOOR consumer
            # reads the SAME field (contract: floor = original*contract_floor_ratio)
            # and goes stale-LOW when a cadence is deliberately raised: a goal
            # widened 24h -> 168h kept floor = 24*0.33 = 7.92h, so a deep-outcome
            # streak could walk a weekly cadence back toward ~8h. One field, two
            # consumers, opposite requirements.
            #
            # Discriminator, needing no new field and no caller flag: an
            # auto-extension is bounded BY CONSTRUCTION at original*cap_ratio, so a
            # write STRICTLY ABOVE that bound provably did not come from one — only
            # a manual or batch cadence edit can land there. The anchor therefore
            # stays immutable for every automatic path and the cap cannot ratchet.
            #
            # Measured 2026-08-13 (zeta, hostname cc-02): of 50 recurring goals
            # carrying both fields, 25 had interval > original and 6 sat ABOVE the
            # cap bound (up to 7.0x). The 4 sitting EXACTLY at 3.0x are
            # auto-extensions at their cap and are left alone by the strict >.
            elif field == "interval_hours":
                _anchor = goal.get("original_interval_hours")
                _new_interval = goal.get("interval_hours")
                if _is_deliberate_raise(_anchor, _new_interval):
                    goal["original_interval_hours"] = _new_interval

            # === Cascade mutations (PR 7e/3) ===
            # All cascades happen INSIDE the lock so a concurrent read sees
            # either pre-write or fully-cascaded state — never a partial
            # mutation. Cascade order is load-bearing: stamp last_modified
            # FIRST so consumers reading any of the auto-managed fields can
            # rely on a single timestamp moment per write.

            # 1. last_modified — stamped on every successful write.
            # A general last-touched timestamp (originally added for the
            # stale-read gate, retired ). Single timestamp per call;
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

            # 6a. completed_date auto-stamp on completion (). This
            # cascade stamped completed_at (6) but NOT completed_date, while
            # the achieve path below (~3588) stamps both — so the field
            # recorded WHICH CLOSE PATH RAN, not whether the goal completed.
            # Measured 2026-08-10 (world+agent): 616/4346 completed goals
            # carried no completed_date, still accruing (471 on 08-05 -> 556
            # on 08-08 -> 616 on 08-10). Read that 616 as TWO populations, not
            # one: 383 carry completed_by, so they are real closes that lost
            # the stamp — the defect this stamp fixes. The other ~233 are
            # mostly the `Maintain:` primitive (252 of the 616 are Maintain-
            # titled, only 54 with completed_by), which CLAUDE.md files with
            # status:completed AT CREATION. Those never transition, so they
            # never reach this cascade and correctly have no completion date.
            # Every window-filtered lane/compliance measurement filters on
            # this field, so the real-close half was invisible to all of them.
            # Scoped to "completed" (not all terminal statuses) — a skipped or
            # expired goal has no completion date. Matches the completed_by
            # scoping in 6b below. Idempotent: only stamps when None, so a
            # back-stamp from aspirations-complete-by survives.
            # DATE shape, not datetime: the canonical iteration-close path
            # writes $TODAY, and 95% of the live store (3557/3743) is
            # date-only. The 5% datetime-shaped minority already breaks a
            # date-string comparison; do not grow it.
            if (field == "status" and value == "completed"
                    and goal.get("completed_date") is None):
                goal["completed_date"] = datetime.now().strftime("%Y-%m-%d")

            # 6b. completed_by auto-stamp on completion (). Daemon
            # mirror of the CLI completed_by stamp (aspirations.py cmd_update_goal,
            # right after its completed_at stamp). The completion chokepoint:
            # every non-recurring status->completed flows here (recurring is
            # blocked above). Pre-fix only ~11% of completed world goals carried
            # completed_by, so agent-attribution audits + the cross_queue
            # graduation count () undercounted. Scoped to
            # value=="completed"; agent from _agent_name(ctx) (the per-request
            # caller — NOT the daemon's env, which the CLI sibling uses).
            # Idempotent: only when unset, preserving complete-by / backfill.
            # : `_stamped_completed_by` carries THIS write's decision
            # down to the completed_by_sid stamp in step 9, so the pair lands
            # together or not at all. Both halves were first-wins on their OWN
            # guard — coherent per-field, wrong for a pair: on a goal that
            # already carries the name and not the sid, this arm skips while the
            # sid arm fires, filling the empty half from whoever issues the next
            # write. Measured 2026-08-09 over the full store: 4239 completed
            # goals in exactly that state, and 6 of 14 completion-SIDs already
            # carrying more than one completed_by. Leaving the sid absent on
            # those is the intended outcome — "an absent sid beats a wrong one"
            # (_completed_by_sid, below).
            _stamped_completed_by = False
            if (field == "status" and value == "completed"
                    and not goal.get("completed_by") and agent):
                goal["completed_by"] = agent
                _stamped_completed_by = True

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
                # : the session stamp is part of the claim and must
                # not outlive it (see release()). THIS is the path every
                # NON-recurring closure takes — iteration-close.sh routes
                # recurring goals to complete-by but everything else to
                # update-goal status=<terminal> — so it is the most-travelled
                # of the four claim-clearing sites, and the one whose omission
                # was caught on a live close rather than by a test.
                #
                #  fix set B part 2: preserve WHICH BODY closed it
                # before the sid is popped. `aspirations-update-goal.sh` now
                # sends `&sid=` (same change), so the helper normally resolves
                # to the REQUEST sid — the body that actually closed the goal.
                # The claim-sid fallback still covers an un-hooked launch with
                # no MIND_SID, and every non-wrapper caller of this endpoint.
                # Order is load-bearing: compute BEFORE the pop.
                #
                # : scope AND idempotency now mirror `completed_by`
                # (step 6b above) exactly. The stamp shipped keyed off the whole
                # terminal set and assigning unconditionally — diverging from the
                # very field it was modelled on, in two directions at once:
                #   * completed -> reopened (stranded-claim-sweep does this BY
                #     DESIGN) -> re-completed by another body left completion #1's
                #     agent beside completion #2's session, so joining the pair
                #     read a combination that never occurred;
                #   * a SKIPPED goal received a field named completed_by_sid with
                #     no completed_by beside it (4 such rows measured live
                #     2026-08-03, of 37 carrying the field at all).
                # completed_at / completed_by / completed_by_sid are now one
                # coherent triple: all first-wins, all scoped to `completed`.
                # WHICH body skipped or expired a claimed goal is deliberately
                # NOT recovered here — that is a different fact and would need its
                # own name, not a widening of this one. The pop stays
                # unconditional: the claim triple clears at EVERY terminal
                # transition (guard-151).
                #
                # : "one coherent triple" was true field-by-field and
                # false as a PAIR — each half guarded itself, so a write could
                # fill one and skip the other. `_stamped_completed_by` is the
                # join: the sid stamps only on the write that also stamped the
                # name. Deliberately stricter than "completed_by was unset" — an
                # unset name with no resolvable `agent` stamps nothing, and that
                # must not license a sid, because a sid with no name beside it is
                # exactly the shape this step was filed to remove.
                if (value == "completed" and _stamped_completed_by
                        and not goal.get("completed_by_sid")):
                    _cbs = _completed_by_sid(ctx, goal)
                    if _cbs:
                        goal["completed_by_sid"] = _cbs
                goal.pop("claimed_by_sid", None)

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

    # never-success-without-persistence for critical transitions (;
    # siblings: add_goal , claim ). Runs BEFORE the E9
    # post-lock hook and the success return — a swallowed status/defer_reason
    # PUT is the  time-travel shape; detect it in THIS writing
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


def _disposition_open_goals_on_retire(asp: Dict[str, Any],
                                      asp_id: str) -> List[str]:
    """Flip open (non-recurring, non-terminal) goals to 'skipped' at the
    retired-archive boundary. Idempotent; returns the dispositioned goal ids.

    g-115-2860: retiring/archiving an aspiration means "abandonment is
    intentional" — but leaving its open goals non-terminal inside the archive
    strands them as invisible-pending, because no read path or the goal
    selector scans aspirations-archive.jsonl. The completed-path already keeps
    completed-with-open-goals aspirations LIVE via its recovery guard; the
    RETIRED path had no equivalent, so its open goals rode into the archive
    still pending/blocked and could never be reached, completed, or surfaced.
    Aligning the stored status with the already-intended abandonment keeps the
    archive honest and prevents the stray-goal class this goal was filed to
    fix. Recurring goals are left untouched (retire/archive_sweep handle those
    via their own recovery paths). Call BEFORE _normalize_terminal_goals_in so
    the newly-skipped goals also get their defer/blocker state cleared.
    """
    dispositioned: List[str] = []
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for g in asp.get("goals", []) or []:
        if g.get("recurring"):
            continue
        if g.get("status") in _TERMINAL_GOAL_STATUSES:
            continue
        g["status"] = "skipped"
        g["skipped_at"] = now
        g["stranded_on_retire"] = True
        g["disposition_reason"] = (
            f"parent aspiration {asp_id} retired/archived with this goal open; "
            f"auto-dispositioned to terminal to prevent invisible-pending "
            f"stranding (g-115-2860)")
        dispositioned.append(g.get("id"))
    return dispositioned


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
    # Cap the ceiling by the QUALIFYING pool — keep byte-identical in intent with
    # aspirations.py::_validate_intent_satisfaction, which carries the full rationale
    # (). Short version: the quality loop below accepts only completed,
    # non-recurring goals carrying verification.outcomes, so demanding
    # ceil(0.5 * ALL non-recurring) is unsatisfiable whenever outcome coverage is
    # under 50%. THIS copy is the one that produced the measured refusals — the
    # daemon serves aspirations-complete-intent.sh, and the incident's quoted error
    # matched this function's wording, not the CLI's.
    qualifying = [
        g for g in non_recurring
        if g.get("status") == "completed"
        and ((g.get("verification") or {}).get("outcomes") or [])
    ]
    required = max(scope_min, min(math.ceil(0.5 * len(non_recurring)), len(qualifying)))
    if len(ev_ids) < required:
        if len(qualifying) < scope_min:
            return False, (
                f"aspiration {asp.get('id')} cannot be intent-closed: only "
                f"{len(qualifying)} of {len(non_recurring)} non-recurring goals are "
                f"completed with verification.outcomes, below the scope={scope} floor of "
                f"{scope_min}. Supplying more evidence_goal_ids cannot satisfy this. "
                f"Reachable exits: retire it (aspirations-retire.sh), or make every "
                f"non-recurring goal terminal and close it normally "
                f"(aspirations-complete.sh)."
            )
        return False, (
            f"evidence_goal_ids has {len(ev_ids)}, scope={scope} requires "
            f">={required} (max of {scope_min}-by-scope and "
            f"min(ceil(0.5 * {len(non_recurring)} non-recurring), {len(qualifying)} qualifying))"
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
    # aspirations-archive.jsonl is S3-backed. Every caller reads the LIVE file
    # and appends here, so the archive itself is never pulled and the append
    # extends a stale mirror (). Fail-open.
    from storage_backend import ensure_local_before_append
    ensure_local_before_append(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=True) + "\n")


def _archive_replace_row(existing: Dict[str, Any],
                         incoming: Dict[str, Any]) -> Dict[str, Any]:
    """The archive row that results from archiving `incoming` when the archive
    ALREADY holds `existing` for the same id. Aspiration-level fields: incoming
    wins (it is the newest state). Goals: UNION by id, incoming wins on a
    same-id clash, and a goal only the EXISTING row holds is KEPT.

    The keep clause is the whole point. An archive row is the LAST home a
    terminal goal record has (eviction re-homes nothing — see
    aspirations-evict-completed.py "WHERE THE RECORDS GO"), and a resurrected
    live copy is a stale SNAPSHOT that can carry FEWER goals than the row it
    replaces. Measured 2026-08-16 on the first live run of the resurrection
    reconcile: asp-240's live copy carried 2 goals, its archive row 7; a
    wholesale replace dropped g-240-102/103/104/105/47 from the archive
    (recovered from the S3 object version). Wholesale replace was
    g-115-2604's shape; the union is what it should have been."""
    out = dict(incoming)
    incoming_goals = [g for g in (incoming.get("goals") or []) if isinstance(g, dict)]
    have = {g.get("id") for g in incoming_goals}
    kept = [g for g in (existing.get("goals") or [])
            if isinstance(g, dict) and g.get("id") not in have]
    if kept:
        out["goals"] = incoming_goals + kept
    return out


def _archive_upsert(archive_path: Path, asp: Dict[str, Any]) -> str:
    """Write `asp` into the archive: REPLACE the row that already carries its
    id (via _archive_replace_row — goals unioned, never dropped), else append.
    Returns "replaced" | "appended".

    g-115-2604 gave archive_sweep replace-by-id because a record can already
    sit in the archive when it is archived again — the live copy was
    RESURRECTED (merge_aspirations is a union by id: an aspiration retired on
    one box is re-added the next time a box that still holds it merges a stale
    live file; a delete has no representation in that union) and then
    re-completed / re-retired. The three single-record boundaries
    (complete, complete-intent, retire) kept a bare append, so exactly the
    aspirations most likely to be archived twice were the ones that doubled
    their archive row (goal-completion audit, 2026-08-16). Same lock, same
    fenced write path as archive_sweep; a miss is a plain append."""
    archive = _read_jsonl(archive_path)
    for i, existing in enumerate(archive):
        if isinstance(existing, dict) and existing.get("id") == asp.get("id"):
            archive[i] = _archive_replace_row(existing, asp)
            _atomic_write_jsonl(archive_path, archive)
            return "replaced"
    _append_jsonl(archive_path, asp)
    return "appended"


def _reconcile_resurrected(items: List[Dict[str, Any]],
                           archive: List[Dict[str, Any]],
                           warnings: List[str]) -> List[str]:
    """Re-apply the archive's disposition to RESURRECTED live copies.

    THE CLASS (goal-completion audit, 2026-08-16 — measured 9 of 29 live
    aspirations also present in the archive; 8 were resurrected retirements,
    7 of them cross-world asp-xw-* stubs the 2026-08-10 sprint had retired as
    duplicates of native goals): merge_aspirations is a UNION by aspiration
    id, so removing a record from the live file (retire / complete /
    archive_sweep) has no representation a peer's merge can see. Any box that
    still holds the pre-retirement copy re-adds it, PRISTINE — goals back to
    pending, no outcome_note, no last_modified — and the stub is selectable,
    digest-emailed, and zombie-flagged again while the archive says it was
    dispositioned. Same shape as write-loss lane (l) in the
    daemon-only-architecture tree node ("a local delete having no
    representation in the sync protocol so a read-through overlay resurrects
    it"), one store up.

    THE PREDICATE lives in core/scripts/_aspirations_resurrection.py — the
    single source of truth shared with the read-only detector behind the
    /verify-learning check (aspirations-resurrection-scan.py), so the sweep
    and the alarm can never disagree about what a resurrection is. This
    function is the APPLY side: for each (live goal, archived goal) pair the
    predicate returns, the archive's disposition comes back — status + the
    outcome fields the archive recorded — and when every remaining live goal
    is terminal and nothing post-dates the archive, the aspiration is
    re-marked with the archive's status so the classification loop below
    archives it (replace-by-id, goals unioned) and drops it from the live
    file. Left alone, on purpose (the predicate's post_archive_work): a live
    goal the archive never saw (asp-328 shape: new goals filed against a
    completed aspiration), a claim, or a last_modified past the stamp — a
    sweep never overrules a live hand. Report-first is unnecessary: the ONLY
    disposition this writes is one the archive already records a human/agent
    having made. Returns the ids of the re-dispositioned goals."""
    by_id = _resurrection.archive_by_id(archive)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    touched: List[str] = []
    for a in items:
        arch = by_id.get(a.get("id"))
        if not arch:
            continue
        pairs, post_archive_work = _resurrection.classify(a, arch)
        if not pairs:
            continue
        stamp = _resurrection.archive_terminal_stamp(arch)
        redispositioned: List[str] = []
        for g, ag in pairs:
            g["status"] = ag["status"]
            for f in ("outcome_note", "key_finding", "completed_date",
                      "completed_by", "outcome_class", "skipped_at",
                      "disposition_reason", "stranded_on_retire"):
                if ag.get(f) is not None and g.get(f) in (None, ""):
                    g[f] = ag[f]
            g["resurrection_reconciled_at"] = now
            redispositioned.append(g["id"])
        _recompute_progress(a)
        touched.extend(redispositioned)
        still_open = [g["id"] for g in a.get("goals") or []
                      if not g.get("recurring")
                      and g.get("status") not in _TERMINAL_GOAL_STATUSES]
        rearchived = False
        if not post_archive_work and not still_open:
            a["status"] = arch["status"]
            a["archived"] = True
            for key in ("retired_at", "completed_at"):
                if arch.get(key) and not a.get(key):
                    a[key] = arch[key]
            rearchived = True
        warnings.append(
            f"RESURRECTION RECONCILE: {a['id']} is archived "
            f"(status={arch.get('status')}, {stamp or 'undated'}) but a live "
            f"copy resurfaced carrying {len(redispositioned)} goal(s) the "
            f"archive had already dispositioned; re-applied the archived "
            f"disposition to {', '.join(redispositioned)}"
            + (" — aspiration re-marked for archival." if rearchived
               else f" — aspiration kept live (post-archive work: "
                    f"{'new/claimed/modified goals' if post_archive_work else 'open goals ' + ', '.join(still_open)})."))
    return touched


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

            # : disposition any open non-terminal goals to 'skipped'
            # BEFORE archiving (mirror retire()/archive_sweep). complete(force=true)
            # falls through the not-force refuse above and would otherwise archive
            # open goals that strand as invisible-pending — no read path or the
            # selector scans aspirations-archive.jsonl (). Idempotent
            # no-op on the normal path where all goals are already terminal.
            _dispositioned = _disposition_open_goals_on_retire(asp, asp_id)
            if _dispositioned:
                warnings.append(
                    f"COMPLETION NOTE: {asp_id} archived with "
                    f"{len(_dispositioned)} open goal(s) auto-dispositioned to "
                    f"'skipped' to prevent invisible-pending stranding "
                    f"(g-115-2882): {', '.join(_dispositioned)}.")

            # Normalize terminal goals before archive (clears stale defer state)
            _normalize_terminal_goals_in(asp)

            # Archive BEFORE removing from live — crash safety:
            # aspiration exists in both (benign) rather than neither (data loss).
            # Upsert, not append: a resurrected copy re-completed here must
            # replace its existing archive row (see _archive_upsert).
            _archive_upsert(archive_path, asp)

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

            # : NO _disposition_open_goals_on_retire call is needed
            # here, unlike complete(force=true)/retire()/archive_sweep(). This
            # path CANNOT strand an open goal: _validate_intent_satisfaction's
            # `remaining_unfinished` guard (above) refuses the completion unless
            # EVERY non-recurring open goal is listed in superseded_goal_ids, and
            # the supersession transition then flips each to status="superseded",
            # which is in _TERMINAL_GOAL_STATUSES. So every non-recurring goal is
            # already terminal by this point (evidence goals were completed;
            # superseded goals are now "superseded"). Adding a disposition call
            # here would be a guaranteed no-op. Analysis recorded so a future
            # fresh-eyes review does not re-flag this as "the uncovered site."

            # Normalize terminal goals before archive (clears stale defer state)
            _normalize_terminal_goals_in(asp)

            # Archive BEFORE removing from live — crash safety (upsert: a
            # resurrected copy re-completed here replaces its archive row).
            _archive_upsert(archive_path, asp)

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
        # streak-breaks.jsonl lives under agents/<a>/session/, which is NOT
        # machine-local: _EXCLUDE_DIRS carries "sessions" (the per-SID dirs), not
        # "session", so this file syncs to S3 (). It has no merge
        # handler either, which makes a stale-base append costlier here than on a
        # registered store. Fail-open — this block is already best-effort.
        from storage_backend import ensure_local_before_append
        ensure_local_before_append(log_path)
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
    # : _resolve_paths branches `if source == "agent" else world` with
    # NO error arm, so any value that is not exactly "agent" — a typo (`agnet`),
    # a case variant (`Agent`), a shell-mangled empty string — silently resolves
    # to the WORLD queue and completes a goal there, reporting success. Every
    # sibling write handler rejects here; this one and `retire` did not.
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", f"source must be 'world' or 'agent', got '{source}'")

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
            # Persist the caller's one-line finding ON THE GOAL RECORD. Until
            # 2026-08-16 () it was read into a local and forwarded
            # only to team-state's 50-row ring buffer, so the durable, shared,
            # queryable artifact — the record every triage/drain lane reads —
            # never carried it, and the same value evaporated from the world
            # after 50 completions. Set for both branches (a recurring cycle's
            # finding is as real as a one-shot's); never cleared on a re-run
            # without a value, so an earlier finding is not blanked by a bare
            # attribution call. Optional by design — see the team-state note
            # below for why the ring-buffer append stays gated on it.
            if key_finding:
                goal["key_finding"] = key_finding

            #  outcome 5 — compute BEFORE either pop path clears the
            # holder fields this reads. Warn-only; see _nonholder_claim_warning
            # for why completing/releasing must not refuse a non-holder.
            nonholder_warning = _nonholder_claim_warning(
                ctx, goal, goal_id, "complete-by")
            if nonholder_warning:
                warnings.append(nonholder_warning)
                print(f"[daemon complete-by] NON-HOLDER: {nonholder_warning}",
                      file=sys.stderr)

            if goal.get("recurring"):
                # Recurring: cycle back to pending, update tracking fields.
                goal.pop("claimed_by", None)
                goal.pop("claimed_at", None)
                #  part 2 — stamp BEFORE the pop. Recurring cycles
                # back to pending rather than a terminal status, but this IS
                # the completion of a cycle: it mirrors `completed_by` above,
                # which is stamped unconditionally for both branches.
                _cbs = _completed_by_sid(ctx, goal)
                if _cbs:
                    goal["completed_by_sid"] = _cbs
                goal.pop("claimed_by_sid", None)  # : see release()

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

                # Streak-break signal (: streak_mult + fire_when
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
                #  part 2 — stamp BEFORE the pop.
                _cbs = _completed_by_sid(ctx, goal)
                if _cbs:
                    goal["completed_by_sid"] = _cbs
                goal.pop("claimed_by_sid", None)  # : see release()

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

    # never-success-without-persistence for complete-by (): a
    # swallowed completion PUT re-serves the goal to the selector later (the
    #  reversion class applied to completions). Verify the final
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
    #
    # BOUND, stated deliberately ( scope item 1). `completed_by` here
    # is the CALLER, exactly as on the goal record — so this surface cannot on
    # its own distinguish the agent that executed a goal from the one that
    # closed it, and nothing pops it, so the contamination persists. That is a
    # KNOWN limit of this store, not an oversight.
    #
    # `executed_by` is deliberately NOT copied in. This record is a derived
    # observability surface; the goal record is the authority and now carries
    # BOTH fields. Duplicating agent attribution into a second store would
    # create a third place to drift, and the sibling writer
    # (iteration-close.sh do_state_update) reaches the value by a different
    # mechanism — so a partial copy would leave rows where the field's absence
    # means "written by the other writer" rather than "no executor", which is
    # precisely the un-auditable ambiguity this goal exists to remove.
    # A reader wanting the executor JOINS on goal_id against the goal record.
    #
    # WHY THIS STAYS GATED ON key_finding (decided 2026-08-16,  Q1,
    # after measuring the reducer's close sequence rather than assuming it):
    # the reducer path already appends this record UNCONDITIONALLY in
    # iteration-close.sh do_state_update (key_finding defaults to "completed"),
    # and it reaches complete-by TWICE per iteration without a key_finding —
    # do_verify's IS_RECURRING branch, then aspirations Phase 5.3 attribution
    # for one-shots — so an unconditional append here would write every reducer
    # close two or three times into a 50-row ring buffer and halve its horizon.
    # The invariant is ONE team-state writer per close path: reducer close ->
    # do_state_update; direct closers (drain / triage / worker retrospective /
    # any caller for whom THIS call is the close of record) -> pass
    # --key-finding and this branch writes the row. Do not "fix" the gate by
    # deduplicating on goal_id instead: recurring goals legitimately recur in
    # the buffer once per cycle, and a goal_id dedup would erase that history.
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
    # : see the identical guard in complete_by. Without it an invalid
    # source resolves to WORLD and archives an aspiration in the wrong queue,
    # reporting success. Retire is the higher-cost half of the pair — it moves a
    # whole aspiration, not one goal.
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source", f"source must be 'world' or 'agent', got '{source}'")

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
                # : disposition open goals to a terminal status BEFORE
                # archiving. Retirement already means "abandonment is
                # intentional" — but leaving these goals non-terminal inside the
                # archive strands them as invisible-pending (no read path or the
                # selector scans aspirations-archive.jsonl), the exact stray
                # class this goal was filed to fix.
                _disposition_open_goals_on_retire(asp, asp_id)
                warnings.append(
                    f"RETIREMENT NOTE: {asp_id} had {len(unfinished)} unfinished "
                    f"goal(s): {uf_summary}. Auto-dispositioned to 'skipped' "
                    f"(abandonment is intentional; prevents invisible-pending "
                    f"stranding in archive — g-115-2860).")

            asp["status"] = "retired"
            asp["completed_at"] = None
            asp["retired_at"] = datetime.now().strftime("%Y-%m-%d")
            asp["archived"] = True

            archived_goal_ids = {g["id"] for g in asp.get("goals", [])}
            _normalize_terminal_goals_in(asp)
            # Upsert: a RESURRECTED copy re-retired here (the sprint-retired
            # asp-xw stubs that came back pristine, 2026-08-16) must replace
            # its archive row, not double it.
            _archive_upsert(archive_path, asp)
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
    # Parity with claim() (). Without this, source=agent, source=bogus
    # and no-source were BYTE-IDENTICAL from outside, which removed the only
    # black-box discriminator for which queue release actually resolved — so a
    # test could not tell a working --source from one the wrapper silently
    # dropped. Newly REFUSED set is exactly the complement of {world, agent}
    # (guard-1562: enumerate what a tightened gate turns away); the wrapper only
    # ever sends those two, so no production caller changes.
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")
    # FW-2: agent-scoped writes MUST carry an explicit X-Mind-Agent header —
    # never silently fall back to the alphabetically-first agent's queue.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)
    had_claim = False
    nonholder_warning = None

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_goal(items, goal_id)
            if found is None:
                # GATED ON SOURCE (), mirroring claim():4559.
                #
                # THIS GATE IS BEHAVIOR-NEUTRAL TODAY — stated plainly because
                #  prescribed it as a fix and it is not one. MEASURED:
                # the new source=agent release test passes against pre-fix HEAD.
                # The mechanism is that `_resolve_paths(ctx, "agent")` returns
                # the SAME file this fallback re-reads, so reaching here under
                # source=agent means the goal was already absent from that exact
                # path; the second read finds nothing and falls to the same 404.
                # It is kept for parity and as a structural invariant — the
                # refusal below tells the caller to "re-issue with &source=agent",
                # advice that would be absurd if it could ever fire under
                # source=agent. The gate makes that impossible by construction
                # rather than by accident. Do NOT cite it as the fix for the
                # agent-queue release path; that path already worked.
                if source == "world":
                    agent_live = ctx.paths.agent / "aspirations.jsonl"
                    if agent_live.exists():
                        try:
                            agent_items = _read_jsonl(agent_live)
                            if _find_goal(agent_items, goal_id) is not None:
                                # STILL A REFUSAL, on a corrected premise. The
                                # old text — "Agent-queue goals do not carry
                                # claims, so there is nothing to release" —
                                # became FALSE when  taught claim() to
                                # accept source=agent. Acting on it strands a
                                # real claim, and the loop digest reads any
                                # error field as journal-abort, so the caller
                                # never retries. What is true is narrower: this
                                # call did not NAME the agent queue.
                                return Response.error(400, "agent_queue_goal",
                                    f"Goal {goal_id} is in the agent queue, but "
                                    f"this release did not request it "
                                    f"(source=world). Re-issue with "
                                    f"&source=agent to release it. Do NOT read "
                                    f"this as 'agent goals carry no claims' — "
                                    f"they have since g-306-238, and a claim "
                                    f"left unreleased outlives the session that "
                                    f"took it.")
                        except Exception:
                            pass
                return Response.error(404, "goal_not_found",
                    f"Goal {goal_id} not found in {source} queue")

            asp_idx, goal_idx, asp = found
            goal = asp["goals"][goal_idx]
            had_claim = "claimed_by" in goal or "claimed_at" in goal
            # DELIBERATELY NARROWER THAN THE POPS BELOW, which include
            # claimed_by_sid (). Do NOT "fix" this to match them on
            # the strength of the clear_stale_claims comment ~1700 lines below
            # — that comment is about a SWEEPER's SELECTION predicate, and this
            # is not a selection predicate at all. release() is handed a goal
            # by id, so it never has to FIND anything; had_claim has exactly
            # two consumers (the touch_peer_signals("goal-claim-released")
            # block and the response field), and its question is "was this
            # claimed in the sense peers can observe?". goal-selector.py keys
            # availability on `not goal.get("claimed_by")` alone (grep that
            # predicate rather than trusting a line number — this goal was
            # filed citing L3517 for the assignment above, which had already
            # drifted to L3812 by the time it ran); claimed_by_sid appears in
            # the selector only for sibling-body discrimination, so a goal
            # carrying ONLY an orphaned sid was already selectable and nothing
            # becomes newly available on release. Widening the predicate would
            # wake every peer for a no-op. The one real cost of the narrowness
            # is cosmetic and accepted: the response can report had_claim
            # false for a call that did pop a field.
            # Compute BEFORE the pops — the warning reads the holder fields.
            nonholder_warning = _nonholder_claim_warning(
                ctx, goal, goal_id, "release")
            goal.pop("claimed_by", None)
            goal.pop("claimed_at", None)
            # : clear the session stamp WITH the claim it belongs to.
            # Leaving it behind outlives its claim, so the next claimer that
            # sends no sid inherits the previous holder's SID label — making a
            # later collision LESS diagnosable than before slice 1 added the
            # field. The stamp must not survive the claim it describes.
            goal.pop("claimed_by_sid", None)

            # : return in-progress work to the pool. goal-selector.py
            # treats `in-progress` as a SKIP status, so dropping the claim
            # WITHOUT this left the goal unclaimed AND unselectable — released
            # from its holder without returning to the pool, invisible to every
            # agent including the one that released it.
            #
            # This does add a lifecycle write to what is otherwise an ownership
            # primitive, and that deserves the justification the filing asked
            # for: release means "this holder is giving up the work", and work
            # nobody holds is not in progress. The two facts are orthogonal in
            # general (a claimed goal sits at `pending` for its whole execution
            # — the normal resting shape), but they are NOT independent at this
            # exact transition. stranded-claim-sweep.py already does both, and a
            # caller hand-repairing state after calling release is the tell.
            #
            # Guarded on the exact status so the other callers of release (stop,
            # retire, take-back) cannot launder a `blocked` or terminal goal
            # into `pending` — only work that was actually advanced is reversed.
            #
            # THE SELECTOR-SIDE ALTERNATIVE WAS MEASURED AND IS WRONG, recorded
            # so it is not re-proposed: making the selector skip `in-progress`
            # only when a claim is present looks more principled (it keeps
            # ownership and lifecycle apart) and fixes every path that can
            # strand a goal, not just this one. But collect_candidates runs over
            # the AGENT queue too (source="agent"), and agent-queue goals never
            # carry claims at all — release() itself returns 400
            # `agent_queue_goal` saying so. Every in-progress agent-queue goal
            # would therefore read as orphaned and be re-offered while it was
            # being worked. Live population when measured was 0, so the defect
            # would have shipped silent.
            if goal.get("status") == "in-progress":
                goal["status"] = "pending"

            if nonholder_warning:
                import sys
                print(f"[daemon release] NON-HOLDER: {nonholder_warning}",
                      file=sys.stderr)

            history.snapshot(live_path, base_dir, agent,
                             summary=f"release {goal_id}")
            _atomic_write_jsonl(live_path, items)
            changelog.append(base_dir, agent, live_path, "edit",
                             summary=f"release {goal_id}",
                             lines_changed=len(items))
            _jsonl_cache().invalidate(live_path)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    # never-success-without-persistence for release (): a swallowed
    # release PUT leaves claimed_by set in the authoritative store — the goal
    # stays claimed on every other box and time-travels back to claimed on
    # THIS one at the next re-sync (the  specimen: an 11:53 release
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

    return Response.json({"ok": True, "goal": goal, "had_claim": had_claim,
                          "warnings": ([nonholder_warning]
                                       if nonholder_warning else None)})


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


_NO_SID_ENV = "MIND_CLAIM_ALLOW_NO_SID"


def _no_sid_bypass() -> Optional[str]:
    """Escape hatch for audited sid-less claim callers (-b).

    Returns the justification when MIND_CLAIM_ALLOW_NO_SID is set non-empty,
    else None (meaning: refuse).

    FAIL-OPEN (guard-142): if reading the environment raises for ANY reason,
    return a sentinel justification so the claim PROCEEDS. A gate that cannot
    read its own dependency must never wedge the world queue — the failure mode
    of a broken gate is "let the work happen and leave a trace", never "freeze
    the fleet". The sentinel is distinguishable in the ledger from a real
    operator justification, so a rash of them is visible rather than silent.
    """
    import os
    try:
        return (os.environ.get(_NO_SID_ENV) or "").strip() or None
    except Exception:
        return f"gate-dependency-error: {_NO_SID_ENV} read failed, failing open (guard-142)"


def _audit_no_sid_claim_inline(ctx, *, goal_id: str, agent_claiming: str,
                               justification: str,
                               title: Optional[str] = None) -> None:
    """Log a sid-less claim bypass to override-bypass-ledger.jsonl.

    Same shape and sink as _audit_cross_lane_claim_inline; distinct `gate` so
    the two bypass classes are separable when auditing the ledger.
    """
    import hashlib
    import sys
    if not justification or not goal_id:
        return
    token = hashlib.sha1(
        justification.encode("utf-8", errors="replace")).hexdigest()[:12]
    context = {"goal_id": goal_id, "agent_claiming": agent_claiming}
    if title:
        context["title"] = title[:200]
    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": token,
        "justification": (justification or "")[:1000],
        "gate": "claim-sid-gate",
        "agent": ctx.paths.agent_name or None,
        "session_id": None,
        "context": context,
    }
    try:
        from _fileops import locked_append_jsonl
        ledger_path = ctx.paths.world / "override-bypass-ledger.jsonl"
        locked_append_jsonl(str(ledger_path), record)
    except Exception as e:
        print(f"[daemon claim] WARN: no-sid ledger write failed: {e}",
              file=sys.stderr)


def _audit_lane_pin_override_inline(ctx, *, goal_id: str, agent_claiming: str,
                                    pin_id: Optional[str],
                                    evidence: Optional[list],
                                    justification: str,
                                    category: Optional[str] = None,
                                    title: Optional[str] = None) -> None:
    """Log a lane-pin override to override-bypass-ledger.jsonl ().

    Same shape and sink as the two siblings above; distinct `gate` so the bypass
    classes stay separable when auditing the ledger. `evidence` carries the
    out-of-lane hits the gate matched on, so a reviewer can judge the override
    without re-running the classifier against a registry that may since have
    been re-worded.
    """
    import hashlib
    import sys
    if not justification or not goal_id:
        return
    token = hashlib.sha1(
        justification.encode("utf-8", errors="replace")).hexdigest()[:12]
    context = {"goal_id": goal_id, "agent_claiming": agent_claiming}
    if pin_id:
        context["pin_id"] = pin_id
    if evidence:
        context["out_of_lane_evidence"] = [str(e)[:80] for e in evidence[:4]]
    if category:
        context["category"] = category
    if title:
        context["title"] = title[:200]
    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": token,
        "justification": (justification or "")[:1000],
        "gate": "lane-pin-gate",
        "agent": ctx.paths.agent_name or None,
        "session_id": None,
        "context": context,
    }
    try:
        from _fileops import locked_append_jsonl
        ledger_path = ctx.paths.world / "override-bypass-ledger.jsonl"
        locked_append_jsonl(str(ledger_path), record)
    except Exception as e:
        print(f"[daemon claim] WARN: lane-pin ledger write failed: {e}",
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


def _audit_cross_session_takeover_inline(ctx, *, goal_id: str,
                                         agent_name: str,
                                         holder_sid: str,
                                         claim_sid: str,
                                         category: Optional[str] = None,
                                         title: Optional[str] = None) -> None:
    """Log a same-agent cross-session take-over to override-bypass-ledger.jsonl.

    g-306-329. The take-over was ALREADY logged before this existed — with
    print(file=sys.stderr), from inside the daemon, which no session can read.
    So two things were true at once: the code's own comment claimed the event
    "must leave a trace", and the trace reached nobody. The displaced Body
    learned nothing (measured 2026-08-19: the loser found out at commit-gate
    time, 34 minutes later), and the take-over rate was unmeasurable, so nobody
    could tell whether g-306-328 had reduced it.

    THIS IS THE AUDIT HALF ONLY, AND SHIPPING IT ALONE IS DELIBERATE. g-306-329
    forbids closing the reader-side question by adding a durable field with no
    consumer. This one has a consumer that already exists: the ledger is
    registered merge-append-only in coordination_merge.py, so records from every
    box accumulate rather than colliding, and the established read is a grep by
    gate tag (the same shape hot-path-size-budget.md documents for its own gate):

        grep -c '"gate": "claim-cross-session-takeover"' \\
            "$WORLD_PATH/override-bypass-ledger.jsonl"

    NOTIFYING the displaced session is a separate and much harder problem — no
    channel exists that a worker polls mid-unit (it reads no board between claim
    and close, and its preamble runs only BETWEEN units). That half is NOT
    solved here and must not be reported as solved.

    A take-over of a DORMANT holder is sanctioned recovery, not a guard bypass,
    so it carries its own gate tag for filtering — same reasoning as
    'claim-staleness-takeback' above. Fail-open: a ledger write failure never
    blocks the claim (warn to stderr only)."""
    import sys
    if not goal_id or not holder_sid or not claim_sid:
        return
    context = {
        "goal_id": goal_id,
        "agent": agent_name,
        "displaced_sid": holder_sid,
        "claiming_sid": claim_sid,
    }
    if category:
        context["category"] = category
    if title:
        context["title"] = title[:200]
    record = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "override_token": None,
        "justification": (
            f"cross-session take-over: dormant sid={holder_sid} -> "
            f"sid={claim_sid} (agent {agent_name})")[:1000],
        "gate": "claim-cross-session-takeover",
        "agent": agent_name or (ctx.paths.agent_name or None),
        "session_id": claim_sid,
        "context": context,
    }
    try:
        from _fileops import locked_append_jsonl
        ledger_path = ctx.paths.world / "override-bypass-ledger.jsonl"
        locked_append_jsonl(str(ledger_path), record)
    except Exception as e:
        print(f"[daemon claim] WARN: cross-session-takeover ledger write "
              f"failed: {e}", file=sys.stderr)


def _cross_box_holder_is_live(ctx, agent_name: str, goal_id: str,
                              stale_minutes: float) -> bool:
    """Is this agent's mind LIVE on ANOTHER box and holding `goal_id`?

    Cross-BOX fallback for `_holder_session_is_live_runner` (g-306-132-a,
    trace T2 of the cross-box-two-bodies design). Only consulted when the
    local session evidence is UNANSWERABLE — see that function's docstring.

    WHY A SEPARATE SIGNAL IS REQUIRED. `running-session-id` is
    `sync_tier: machine_local`, so it is ABSENT on every box except the one
    running the loop. guard-2418: a cross-box condition that reads a
    machine_local file another box owns does not error — it silently
    evaluates False, and the absent-file path is indistinguishable from the
    legitimately-false path. That is exactly how a LIVE reducer's claim on
    box B was being taken over from box A with no signal at all.

    WHY GOAL-SCOPED, NOT JUST "IS THE MIND ALIVE". The shard carries NO
    session id (measured: its keys are beliefs / current_focus / in_flight /
    last_active / live_phase / row_updated / session_ended /
    session_goals_completed — `session_ended` and `session_goals_completed`
    are a timestamp and a count, not identifiers). So it cannot answer the
    session-level question directly, and refusing on bare `last_active`
    freshness would refuse whenever the mind is alive ANYWHERE — including
    while it works a DIFFERENT goal, which would break this fix's own
    requirement that "a genuinely dormant cross-box holder is still taken
    over". `in_flight.goal_id` restores the precision: it is positive
    evidence that the live mind is working THIS goal.

    guard-997 is respected by DIRECTION, not worked around. It says
    `in_flight` showing NONE does not prove a goal is unclaimed elsewhere —
    the ABSENCE direction. This reads the PRESENCE direction (in_flight
    present, matching, and fresh), which is the same fresh-is-evidence /
    stale-is-ambiguous asymmetry the caller is built on. guard-997 also
    predates the authoritative single-shard read used here; the staleness it
    warns about is the LOCAL mirror's (guard-980), which
    `read_shard_authoritative(force_fresh=True)` is what finally answers.

    Returns True ONLY on positive confirmation. Every other path — no
    goal_id, unreadable shard, unparseable or stale `last_active`, absent or
    non-matching `in_flight` — returns False, i.e. permits the claim.
    """
    # This module imports sys PER-FUNCTION (see the convention comments at the
    # other `import sys` sites) — it is NOT module-scope. Relying on a bare
    # `sys` here raises NameError, which the except-clause below would swallow
    # into `return False`, leaving this fallback permanently dead while still
    # compiling and passing every test that does not reach the cross-box
    # branch. That is the same silent-False failure this whole function exists
    # to eliminate, so the local import is load-bearing.
    #
    # HOISTED ABOVE THE TRY (F-003 of ). It used to sit after the
    # first guard clause INSIDE the try, so the except-clause's own diagnostic
    # could not reference `sys` for any exception raised before that line —
    # the logging added to make this path visible would itself have raised
    # NameError and been swallowed, restoring the silence it was added to end.
    import sys
    try:
        if not goal_id or not agent_name:
            return False
        scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from _team_state import read_shard_authoritative
        # Fails open to the LOCAL mirror on any backend error (documented
        # contract). That degradation cannot manufacture a refusal on its
        # own: a stale mirror fails the freshness gate below and permits the
        # claim, which is the direction this function must err in.
        row = read_shard_authoritative(ctx.paths.world, agent_name)
        if not isinstance(row, dict):
            return False
        last_active = row.get("last_active")
        if not last_active:
            return False
        try:
            la = datetime.strptime(str(last_active).strip()[:19],
                                   "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return False  # unparseable -> ambiguous -> never refuse
        age_s = (datetime.now() - la).total_seconds()
        if age_s > (float(stale_minutes) * 60.0):
            return False  # STALE is ambiguous, never grounds to refuse
        in_flight = row.get("in_flight")
        if not isinstance(in_flight, dict):
            return False
        return str(in_flight.get("goal_id") or "") == str(goal_id)
    except Exception as e:  # noqa: BLE001 — fail-open, but never silently
        print(f"[daemon claim] WARN: cross-box holder liveness probe for "
              f"{agent_name!r}/{goal_id!r} failed ({type(e).__name__}: {e}); "
              f"treating as not-live", file=sys.stderr)
        return False


def _same_box_body_is_live(ctx, holder_sid: str,
                           stale_minutes: float,
                           agent_name: str) -> bool:
    """Is `holder_sid` a LIVE non-reducer Body of `agent_name` on THIS box?

    Same-BOX sibling of `_cross_box_holder_is_live` (g-306-140), consulted from
    `_holder_session_is_live_runner` when `running-session-id` is readable and
    names a DIFFERENT session than the holder.

    WHY THAT BRANCH NEEDED A SIGNAL AT ALL. Its original reading — "present and
    != holder_sid -> the holder is positively a dormant PRIOR session" — is
    true under one-Body-per-box and FALSE under the Mind/Body split, because
    `running-session-id` names only the REDUCER (body-manifest.py: "the reducer
    is the worker Body holding running-session-id"). So a LIVE worker Body
    lands in exactly the same branch as a dead prior session, and its claim was
    taken over as benign "dormant session" recovery — the worker-vs-worker
    blindness this function closes. Worker-vs-REDUCER was already guarded; only
    worker-vs-worker fell through.

    WHY A HEARTBEAT AND NOT `body_state`. body-manifest.yaml carries a
    `body_state` field, but nothing clears it when a Body crashes, so refusing
    on state alone would convert a transient crash into a PERMANENT wedge —
    the stale-status-field class (rb-4081: an API reported TERMINATED servers
    as status=active). Freshness decays on its own; a state field does not.
    The writer is `core/scripts/heartbeat-tick.sh`, which is supervisor-emitted
    and unconditional (once per loop iteration, once from /start, and every 60s
    from interruptible-sleep.sh during long waits) rather than piggybacked on a
    discretionary step — rb-4589.

    `agent_name` is REQUIRED and names WHOSE Body this is (g-306-148). It was
    absent until the cross-agent caller was added, and the omission was not
    cosmetic: `ctx.paths.session_dir` roots at `ctx.paths.agent`, the BOUND
    agent's dir, so calling this for a FOREIGN holder without it probes
    `agents/<CALLER>/sessions/<foreign-sid>/body-heartbeat` — a path that
    essentially never exists, so the consult returns False every time and the
    branch it was added to guard stays exactly as silent as before. That is the
    bound-agent-only hazard `_cross_agent_holder_is_live` exists as a separate
    function to avoid, reintroduced one layer down. Making the parameter
    REQUIRED rather than optional-with-default is the same F-002 remedy already
    applied twice in this module: a future call site that forgets it fails with
    a TypeError at import instead of silently disabling a safety path.

    Returns True ONLY on positive confirmation. Every other path — no
    holder_sid, no session dir, absent heartbeat (an older Body predating the
    writer, or a crashed one), unreadable stat, or a STALE mtime — returns
    False, i.e. permits the claim. That preserves the caller's documented
    asymmetry exactly: a wrong False merely permits a claim that is already
    possible today; a wrong True would wedge the goal for every session of the
    agent.
    """
    try:
        if not holder_sid:
            return False
        # Routed through ctx.paths.session_dir / SESSIONS_DIRNAME, never a
        # literal "sessions" segment — CLAUDE.md "Agent-dir Resolution" calls
        # hardcoded copies of that constant out as a class its own audit greps
        # cannot see.
        #
        # The bound-agent arm is kept byte-identical to the pre- path
        # rather than folded into the general form: `ctx.paths.agent` is INJECTED
        # into AgentPaths, not derived from agents_root, so the two are equal by
        # convention and not by construction. Re-deriving it here would silently
        # change the existing caller's resolution wherever a fixture injects a
        # different agent dir.
        # `ctx.paths.agent_name` DIRECTLY, never getattr-with-default. AgentPaths
        # declares agent_name in __slots__ and __init__ always assigns it, so a
        # default is unreachable — and its failure DIRECTION is wrong: an empty
        # default makes this comparison False, which silently routes the
        # BOUND-agent caller down the re-derived agents_root arm, i.e. exactly the
        # substitution the comment above says was deliberately avoided. Reading the
        # attribute directly raises AttributeError instead, which the enclosing
        # except turns into the DOCUMENTED fail-open False. (guard-2601, found by
        # /fresh-eyes-code on this same change.)
        if str(agent_name) == str(ctx.paths.agent_name):
            sess_dir = ctx.paths.session_dir(str(holder_sid))
        else:
            sess_dir = (ctx.paths.agents_root / str(agent_name)
                        / SESSIONS_DIRNAME / str(holder_sid))
        hb_path = sess_dir / "body-heartbeat"
        if not hb_path.exists():
            return False
        import time as _time  # not imported at module scope
        age_s = _time.time() - hb_path.stat().st_mtime
        return age_s <= (float(stale_minutes) * 60.0)
    except Exception:
        return False


# The tri-state core's `None` reasons that mean ABSENCE OF EVIDENCE — the only
# ones `_cross_box_body_is_live` escalates to the heartbeat carrier (,
# re-landed by ). Deliberately NOT every `None`: `shard_unreadable`,
# `no_claimed_at`, `unparseable_claimed_at` and `probe_error:*` are also
# unanswered but were left permitting the claim by the original fix, and
# widening here would change behaviour that fix never touched. `no_ids` is
# excluded because `_body_carrier_is_fresh` cannot act without those ids anyway.
_ABSENT_ROW_REASONS = frozenset({"no_bodies_map", "no_row_for_sid"})


def _body_carrier_is_fresh(ctx, agent_name: str, holder_sid: str,
                           stale_minutes: float) -> bool:
    """Is `holder_sid` emitting a FRESH cross-box body heartbeat? ()

    The SECOND, INDEPENDENT-WRITER signal, consulted ONLY where
    `_cross_box_body_liveness` found no `in_flight_bodies[<sid>]` row at all.

    WHY A SECOND SIGNAL EXISTS HERE. The per-SID row is written FAIL-OPEN
    (`coordination.md`:1035 — "a failed body-row write logs a WARN and does NOT
    fail the claim (visibility is not correctness)"; with no `MIND_SID` it
    writes no row at all). g-306-318 later promoted that same row to the SOLE
    EVIDENCE for a claim REFUSAL without revisiting its writer's posture, so at
    the consumer "the write failed" and "the Body is dormant" became
    byte-identical — and the fall-through asserted the second. Two Bodies of one
    agent then built one goal (2026-08-19T08:20:59 vs 08:23:02; 34 min of work
    discarded). A predicate is only as sound as the write that feeds it.

    THE DOCTRINE IS ALREADY THE FRAMEWORK'S, for the sibling field.
    `.claude/rules/check-team-state-before-silent.md` rules 5-6: a FRESH stamp
    is positive evidence of life, a STALE/ABSENT one is AMBIGUOUS and never
    evidence of death, and the remedy is corroboration from a signal with an
    INDEPENDENT WRITER. This carries that across: absent row => UNANSWERED.

    WHY THIS CARRIER IS GENUINELY INDEPENDENT (the `guard-4390` test, run at
    re-land time rather than assumed). The row is written by
    `team-state-in-flight.sh` at claim time; the carrier by `heartbeat-tick.sh`
    at the top of every worker cycle — verified 2026-08-21 that heartbeat-tick
    never writes `in_flight_bodies`, so this is writer diversity and not merely
    label diversity. Crucially the carrier's writer does NOT survive the failure
    being tested for: if the Body is dead, heartbeat-tick stops, so a FRESH
    carrier discriminates rather than agreeing under both hypotheses.

    READ AUTHORITATIVELY, never off the local mirror (guard-980): under
    own-cloud the local tree is a read-through cache, so `Path.exists()` on a
    peer's carrier proves nothing. `read_authoritative_bytes` is the backend's
    generic primitive for exactly this and is implemented by BOTH backends.

    POSITIVE CONFIRMATION ONLY — this can never manufacture a refusal on its
    own. It is consulted only where the caller already returned False, so every
    failure path here (missing ids, unreadable carrier, malformed doc, absent or
    unparseable `ts`, a stale `ts`, a CLOSED `body_state`) returns False and
    leaves today's take-over behaviour byte-identical. That direction is
    deliberate: refusing a claim because a telemetry read failed is the
    `guard-1562` trade and could wedge every claim on a box whose team-state
    writes are broken. This can only NARROW the take-over window, never widen it.

    `body_state` IS consulted, but only to WITHDRAW liveness: a Body in the
    CLOSED SET is not live however fresh its last stamp, and its claim should
    remain takeable. It is never used to ASSERT liveness — nothing clears that
    field on a crash, so refusing on state alone would convert a transient crash
    into a permanent wedge (the rb-4081 stale-status-field class).
    """
    import sys
    try:
        if not agent_name or not holder_sid:
            return False
        # Same bound-agent-vs-foreign split as `_same_box_body_is_live`, and for
        # the same  reason: `ctx.paths.agent` is INJECTED, not derived
        # from agents_root, so re-deriving it would silently change the bound
        # caller's resolution wherever a fixture injects a different agent dir.
        # `SESSION_DIRNAME` (singular, the agent-wide state dir) is the
        # constant, never a literal "session" segment — CLAUDE.md "Agent-dir
        # Resolution" names hardcoded copies as a class its audit greps miss.
        if str(agent_name) == str(ctx.paths.agent_name):
            state_dir = ctx.paths.state_dir
        else:
            state_dir = (ctx.paths.agents_root / str(agent_name)
                         / SESSION_DIRNAME)
        carrier = state_dir / f"body-heartbeat-{holder_sid}.json"
        doc = json.loads(
            get_backend().read_authoritative_bytes(carrier).decode("utf-8"))
        if not isinstance(doc, dict):
            return False
        # CLOSED SET, never "not active" — `parked` is RESUMABLE and a parked
        # Body is alive (). Testing "not active" here would treat a
        # live parked Body as takeable.
        if str(doc.get("body_state") or "") in (
                "closed-pending-merge", "merged", "closed-stale"):
            return False
        ts = doc.get("ts")
        if not ts:
            return False
        try:
            t = datetime.strptime(str(ts).strip()[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return False  # unparseable -> ambiguous -> never refuse
        return (datetime.now() - t).total_seconds() <= (
            float(stale_minutes) * 60.0)
    except Exception as e:  # noqa: BLE001 — fail-open, but never silently
        print(f"[daemon claim] WARN: cross-box BODY carrier probe for "
              f"{agent_name!r}/{holder_sid!r} failed "
              f"({type(e).__name__}: {e}); treating as not-live",
              file=sys.stderr)
        return False


def _cross_box_body_is_live(ctx, agent_name: str, holder_sid: str,
                            goal_id: str, stale_minutes: float) -> bool:
    """Is `holder_sid` a LIVE worker Body of `agent_name` on ANOTHER box,
    holding `goal_id`?

    Cross-BOX sibling of `_same_box_body_is_live` (g-306-318). This is the last
    cell of the holder-liveness matrix: g-306-140 closed worker-vs-worker on the
    SAME box, g-306-132-a closed the absent-`running-session-id` cross-box case,
    and what remained unguarded was the pairing that matters most under the
    Mind/Body split — the REDUCER versus its own REMOTE workers. Measured
    2026-08-18 07:20: a worker Body claimed a goal and the reducer on another
    box claimed the SAME goal 14 s later, logged as a benign "dormant" takeover.

    WHY NOT REUSE `_cross_box_holder_is_live`, which already reads this shard.
    That helper keys on the AGENT-level `in_flight` row, which carries no
    session id — and `_holder_session_is_live_runner`'s docstring rejects it
    here for exactly that reason: it "would refuse a legitimate same-box
    takeover whenever the mind happened to be alive elsewhere". The SID-keyed
    `in_flight_bodies[<holder_sid>]` row answers the session-level question the
    calling branch actually asks, so the objection does not reach it. That is a
    behavioural difference, not a stylistic one, and it is pinned by
    test_claim_cross_box_body_holder.py case D (a fresh agent-keyed `in_flight`
    naming this very goal must STILL permit the takeover).

    WHY `claimed_at` AND NOT `last_active` (guard-3604). `last_active` has a
    known false-positive generator: `team-state-clear-in-flight.sh` BUMPS a
    peer's `last_active` when policing it, so a dormant peer reads fresh for the
    full window — and here a wrong "fresh" produces a wrong REFUSAL, which
    wedges the goal. `claimed_at` on the per-SID row is written once by the Body
    itself at claim time and no cross-agent maintenance write touches it.

    KNOWN LIMIT, recorded rather than left to be rediscovered: `claimed_at` is
    never refreshed, so a Body legitimately working ONE goal for longer than
    `stale_minutes` ages out of this protection. That lands in the documented
    fail-open direction (a wrong False merely permits a claim that is already
    possible today).

    THAT LIMIT STILL STANDS, and g-306-328 did NOT lift it. The
    continuously-refreshed `session/body-heartbeat-<SID>.json` carrier IS now
    read (`_body_carrier_is_fresh`), but ONLY where the row is ABSENT. A PRESENT
    row with a stale `claimed_at` is evidence and keeps permitting the claim, so
    a Body working one goal past `stale_minutes` still ages out exactly as
    described above. Lifting that is a separate judgement about how long one
    Body may hold one goal, not a plumbing gap — do not read the carrier's
    arrival as having closed it.

    Returns True ONLY on positive confirmation. Every other path — missing
    ids, unreadable shard, absent/!dict `in_flight_bodies`, no row for this sid,
    a row naming a different goal, an unparseable or stale `claimed_at` —
    returns False, i.e. permits the claim.

    THE RETURN IS BOOLEAN; THE EVIDENCE IS TRI-STATE. The real distinction —
    POSITIVE evidence the Body is not on this goal, versus NO EVIDENCE EITHER
    WAY — lives in `_cross_box_body_liveness` below, whose third state (`None`)
    is the `unknown` verdict `guard-2223` requires of any predicate that can
    fail to reach its store.

    THE TWO ABSENT-ROW REASONS ARE NOW ACTED ON (g-306-328, re-landed by
    g-115-6943 inside the tri-state contract). `no_bodies_map` and
    `no_row_for_sid` escalate to `_body_carrier_is_fresh` — a SECOND signal with
    an INDEPENDENT WRITER — and refuse ONLY on a FRESH carrier. This is NOT
    "refusing on unanswered" (the `guard-1562` objection, which correctly still
    stands): a stale, absent or unreadable carrier permits the claim exactly as
    before, so the change can only NARROW the take-over window. The two paths
    that produce a missing row — a body-row write that failed and only WARNed,
    and a Body with no `MIND_SID` that never attempted one — remain
    indistinguishable from here and STILL point at opposite remedies; the
    escalation sidesteps that by not needing to tell them apart. It asks a
    different question (is that session alive?) of a different writer.

    EVERY OTHER `None` REASON STILL COLLAPSES TO `False`: `shard_unreadable`,
    `no_ids`, `no_claimed_at`, `unparseable_claimed_at` and `probe_error:*` are
    unanswered too, but the original fix deliberately left them permitting the
    claim and this re-land does not widen past it. `row_other_goal` and `stale`
    are `False` because they are EVIDENCE, not the absence of it.
    """
    verdict, reason = _cross_box_body_liveness(
        ctx, agent_name, holder_sid, goal_id, stale_minutes)
    # decision is chosen by the CALLER's observable control-flow effect, per
    # guard-1743: True refuses the claim (`block`); every other verdict permits
    # it (`pass`). `fail_open` is reserved for the branch that actually RAISED,
    # because gate-retirement-eval has an investigate-on-fail_open rule and a
    # mislabel manufactures HIGH investigations into events that never
    # happened. The semantic class rides in `extra`, never in `decision`.
    _log_cross_box_body_liveness(ctx, agent_name=agent_name,
                                 holder_sid=holder_sid, goal_id=goal_id,
                                 verdict=verdict, reason=reason)
    if verdict is True:
        return True
    if reason in _ABSENT_ROW_REASONS:
        # THE ABSENT-ROW CASES ARE **UNANSWERED**, NOT DORMANT (,
        # re-landed by  inside the tri-state contract).
        # Reaching one means the FAIL-OPEN writer left no evidence — it never
        # ran, its write only WARNed, or the caller had no `MIND_SID`
        # (`coordination.md`:1035) — which says nothing about the Body. The
        # goal record has ALREADY told us `holder_sid` holds THIS goal, so the
        # only open question is whether that session is alive, and the carrier
        # answers exactly that with an INDEPENDENT WRITER.
        #
        # SCOPED BY REASON, NOT BY BARE `verdict is None`. The core returns
        # `None` for seven reasons; only these two are absence-of-evidence.
        # `shard_unreadable`, `no_claimed_at`, `unparseable_claimed_at` and
        # `probe_error` keep permitting the claim exactly as before — widening
        # to every `None` would change behaviour the reverted commit
        # deliberately left alone. `row_other_goal` and `stale` are already
        # `False`: those are EVIDENCE, not the absence of it, and overriding
        # real evidence with a liveness ping would refuse take-overs that are
        # legitimate today.
        #
        # guard-4390 CHECK, run before trusting this as corroboration: the
        # carrier's writer is `heartbeat-tick.sh` (every cycle), the row's is
        # `team-state-in-flight.sh` (once, at claim). Different writer,
        # different trigger, and heartbeat-tick STOPS when the Body dies — so
        # a FRESH carrier genuinely discriminates the failure being tested for
        # rather than agreeing under both hypotheses.
        #
        # A stale/absent/unreadable carrier still returns False, so take-over
        # behaviour on no-evidence is unchanged and this can only NARROW the
        # window, never widen it (the `guard-1562` fail-open direction).
        return _body_carrier_is_fresh(ctx, agent_name, holder_sid,
                                      stale_minutes)
    return False


def _cross_box_body_liveness(ctx, agent_name: str, holder_sid: str,
                             goal_id: str, stale_minutes: float):
    """Tri-state core of `_cross_box_body_is_live` ().

    Returns `(verdict, reason)`:
      `(True,  "live")`   — positive confirmation: a fresh row for this SID
                            names this goal. The caller REFUSES the claim.
      `(False, reason)`   — POSITIVE evidence this Body is not working this
                            goal: its row names a DIFFERENT goal, or its row
                            for this goal has aged past `stale_minutes`.
      `(None,  reason)`   — UNANSWERED: the store could not be reached, or it
                            was reached and simply has no row for this SID.
                            An absent row is NOT evidence of dormancy — the
                            writer is fail-open by contract
                            (`coordination.md:1035`: "a failed body-row write
                            logs a WARN and does NOT fail the claim").

    The `False`/`None` split is the whole point: only the `False` reasons are
    grounded in something the store actually said.
    """
    # Per-function `import sys`, hoisted ABOVE the try, for the reason spelled
    # out at `_cross_box_holder_is_live`: this module does not import sys at
    # module scope, so a bare `sys` in the except-clause raises NameError and
    # the clause swallows it into `return False` — leaving this fallback
    # permanently dead while still compiling and passing every test that does
    # not reach it. That is the same silent-False class this helper exists to
    # remove (F-003 of ).
    import sys
    try:
        if not goal_id or not agent_name or not holder_sid:
            return None, "no_ids"
        scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from _team_state import read_shard_authoritative
        # Fails open to the LOCAL mirror on any backend error (documented
        # contract). That degradation cannot manufacture a refusal on its own:
        # a stale mirror fails the freshness gate below and permits the claim.
        row = read_shard_authoritative(ctx.paths.world, agent_name)
        if not isinstance(row, dict):
            return None, "shard_unreadable"
        bodies = row.get("in_flight_bodies")
        if not isinstance(bodies, dict):
            return None, "no_bodies_map"
        body = bodies.get(str(holder_sid))
        if not isinstance(body, dict):
            # THE STRUCTURAL CASE. Absent row != dormant Body: it is also what
            # a failed fail-open row write and an absent MIND_SID both look
            # like from here. Counted, not acted on.
            return None, "no_row_for_sid"
        if str(body.get("goal_id") or "") != str(goal_id):
            return False, "row_other_goal"  # store SAID: on a different goal
        claimed_at = body.get("claimed_at")
        if not claimed_at:
            return None, "no_claimed_at"
        try:
            ca = datetime.strptime(str(claimed_at).strip()[:19],
                                   "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return None, "unparseable_claimed_at"
        age_s = (datetime.now() - ca).total_seconds()
        if age_s <= (float(stale_minutes) * 60.0):
            return True, "live"
        return False, "stale"  # store SAID: this claim has aged out
    except Exception as e:  # noqa: BLE001 — fail-open, but never silently
        print(f"[daemon claim] WARN: cross-box BODY liveness probe for "
              f"{agent_name!r}/{holder_sid!r}/{goal_id!r} failed "
              f"({type(e).__name__}: {e}); treating as not-live",
              file=sys.stderr)
        return None, f"probe_error:{type(e).__name__}: {e}"


def _log_cross_box_body_liveness(ctx, *, agent_name: str, holder_sid: str,
                                 goal_id: str, verdict, reason: str) -> None:
    """Emit one `cross-box-body-liveness` firing per probe ().

    Why a durable sink and not another `print` to daemon stderr: the defect
    this instrumentation serves is precisely that a daemon-side observation
    lands where its audience cannot read it (the `guard-2352` class). Firings
    go to the CALLING agent's `meta/gate-firings.jsonl` — same `ctx.paths.meta`
    + `agent_name` override `_gate_log_layer_d` uses, and for the same reason
    (the module-level `META_DIR` is frozen at daemon startup to whichever
    agent's `local-paths.conf` was first found).

    ALL FOUR OUTCOMES ARE LOGGED, not just the refusal. A telemetry surface
    that records only the escape path cannot answer "how often did this
    predicate have no evidence?" — which is the one question the next decision
    on `g-306-328` turns on (`guard-2293`).
    """
    if verdict is True:
        decision, klass = "block", "live"
    elif verdict is False:
        decision, klass = "pass", "dormant"
    elif str(reason).startswith("probe_error"):
        decision, klass = "fail_open", "unanswered"
    else:
        decision, klass = "pass", "unanswered"
    _gate_log.log(
        "cross-box-body-liveness",
        decision,
        trigger_matched=str(reason).split(":", 1)[0],
        payload=goal_id,
        gate_error=(str(reason).split(":", 1)[1].strip()
                    if decision == "fail_open" and ":" in str(reason)
                    else None),
        extra={
            "verdict_class": klass,
            "reason": reason,
            "holder_sid": holder_sid,
            "holder_agent": agent_name,
            "source": "daemon",
        },
        meta_dir=ctx.paths.meta,
        agent_name=ctx.paths.agent_name or None,
    )


def _holder_session_is_live_runner(ctx, agent_name: str,
                                   holder_sid: str,
                                   goal_id: str) -> bool:
    """Is `holder_sid` this agent's CURRENTLY-RUNNING autonomous runner?

    Used by claim() to refuse a same-agent claim from a DIFFERENT session
    (g-115-3176). Returns True ONLY on positive confirmation; every ambiguous
    or error path returns False.

    Why liveness-check.sh is the WRONG instrument here: it answers *agent*
    liveness and cannot distinguish two sessions of ONE agent — which is
    precisely this case. The session-level signals are `running-session-id`
    (which SID owns the loop) plus `runner-heartbeat`, whose liveness model is
    pure mtime (see core/scripts/heartbeat-tick.sh).

    FAIL-OPEN is load-bearing and asymmetric, mirroring
    .claude/rules/check-team-state-before-silent.md: a FRESH heartbeat is
    positive evidence of life, but a STALE one is AMBIGUOUS (an idle session
    and a broken heartbeat writer look identical — observed 2026-07-14, a live
    agent read 59h stale). So we gate the REFUSAL on freshness and never gate
    the ALLOW on staleness. A wrong False merely permits a claim that is
    already possible today; a wrong True would wedge the goal for every
    session of the agent.

    CROSS-BOX (g-306-132-a). `running-session-id` is machine_local, so the
    three cases below are NOT interchangeable and the middle one is the whole
    bug:
      - present and == holder_sid -> THIS box runs the loop and the holder IS
        it. Local heartbeat decides. (unchanged)
      - present and != holder_sid -> the holder is not the REDUCER. This branch
        USED to read that as "positively a dormant PRIOR session" and allow the
        takeover; under the Mind/Body split that is false, because
        `running-session-id` names only the reducer, so a LIVE non-reducer
        worker Body is indistinguishable from a dead prior session here. It now
        consults the per-Body heartbeat via _same_box_body_is_live (g-306-140).
        This branch USED to carry "still deliberately NO cross-box fallback",
        and that was correct only about ONE helper. `_cross_box_holder_is_live`
        is STILL not consulted here, for the reason that sentence gave: it is
        goal-scoped on the AGENT-keyed `in_flight` row, which carries no session
        id, so it would refuse a legitimate same-box takeover whenever the mind
        happened to be alive elsewhere. But "no cross-box signal exists for a
        SESSION" was never true — it just had not been wired. Absent a
        heartbeat on THIS box the holder is not dormant, it is UNANSWERED, and a
        remote worker Body never writes a heartbeat here at all, so the
        same-box probe returns False for a perfectly live Body and the claim
        fell through as a "dormant" takeover (measured 2026-08-18 07:20:
        reducer took a worker Body's 14-second-old claim). So a SID-keyed
        cross-box consult now follows it: `_cross_box_body_is_live` reads
        `in_flight_bodies[holder_sid]` and refuses only on a FRESH row naming
        THIS goal (g-306-318). Both consulted signals are SID-scoped, which is
        what makes them answer the session-level question this branch asks.
      - absent/empty -> this box does not run this agent's loop, so the file
        is not stale, it is UNANSWERABLE (guard-2418). Previously this fell
        straight to False and a LIVE reducer on another box was silently
        taken over. Now it consults an authoritative, goal-scoped signal.
    `goal_id` is REQUIRED (F-002 of g-306-141). It was `Optional[str] = None`,
    an explicit opt-in — but every caller already passed it, so the default was
    reachable only by a NEW call site, and reaching it would silently disable
    the cross-box fallback above while still compiling and passing every test
    that never enters the absent-rsid branch. That is the same silent-False
    class this fallback exists to remove, so the parameter is now required and
    the hazard is a TypeError at import rather than a dead safety path.
    """
    import sys
    try:
        if not holder_sid:
            return False
        # ctx.paths.agent is the BOUND agent's dir. Only trust it when the
        # claimer IS the bound agent; a cross-agent probe would read the wrong
        # session dir and could refuse on a foreign agent's runner.
        if agent_name != _agent_name(ctx):
            return False
        import yaml
        cfg_path = (ctx.paths.project_root / "core" / "config"
                    / "aspirations.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        stale_minutes = (cfg.get("runner_heartbeat") or {}).get("stale_minutes")
        if not stale_minutes:
            return False  # misconfig -> fail open, never refuse
        sess = ctx.paths.agent / "session"
        rsid_path = sess / "running-session-id"
        hb_path = sess / "runner-heartbeat"
        running_sid = ""
        if rsid_path.exists():
            running_sid = rsid_path.read_text(encoding="utf-8").strip()
        if not running_sid:
            # UNANSWERABLE locally -> cross-box fallback (see above).
            return _cross_box_holder_is_live(ctx, agent_name, goal_id or "",
                                             stale_minutes)
        if running_sid != holder_sid:
            # The holder is not the REDUCER. That is NOT the same as dormant:
            # running-session-id names only the reducer, so a live non-reducer
            # worker Body lands here too (). Consult the per-Body
            # heartbeat; absent/stale still permits the takeover. agent_name is
            # provably the bound agent here — the guard clause above returns
            # False unless agent_name == _agent_name(ctx) — so this resolves the
            # same session dir it always did ().
            if _same_box_body_is_live(ctx, holder_sid, stale_minutes,
                                      agent_name):
                return True
            # ...and if the holder has no heartbeat on THIS box, that is not
            # evidence it is dormant — it is the cross-box shape (). A
            # remote worker Body never writes a heartbeat here, so the same-box
            # probe above returns False for a perfectly live Body and the claim
            # used to fall through as a "dormant" takeover. Consult the SID-
            # keyed `in_flight_bodies[holder_sid]` row, which is fleet-shared
            # and answers the session-level question; the AGENT-keyed
            # `in_flight` row is still NOT consulted here (see the docstring).
            return _cross_box_body_is_live(ctx, agent_name, holder_sid,
                                           goal_id or "", stale_minutes)
        if not hb_path.exists():
            return False
        import time as _time  # not imported at module scope
        age_s = _time.time() - hb_path.stat().st_mtime
        return age_s <= (float(stale_minutes) * 60.0)
    except Exception as e:  # noqa: BLE001 — fail-open, but never silently
        print(f"[daemon claim] WARN: same-agent holder liveness probe for "
              f"{agent_name!r}/{goal_id!r} failed ({type(e).__name__}: {e}); "
              f"permitting the claim", file=sys.stderr)
        return False


def _cross_agent_holder_is_live(ctx, agent_name: str,
                                holder_sid: str,
                                goal_id: str) -> bool:
    """Is `holder_sid` the live runner of ANOTHER agent (`agent_name`)?

    Cross-agent sibling of _holder_session_is_live_runner, which is
    deliberately bound-agent-only (its ctx.paths.agent read would resolve the
    CALLER's session dir for a foreign holder). This one roots the probe at
    ctx.paths.agents_root / <holder> so the running-session-id + heartbeat
    checks read the HOLDER's session dir. Same positive-confirmation posture:
    every ambiguous or error path returns False, which here means "stay
    quiet" — a wrong False reproduces today's silence, a wrong True costs one
    spurious warn-only line. (g-115-4232)

    CROSS-BOX (g-306-141). `running-session-id` is `sync_tier: machine_local`
    (core/config/session-manifest.yaml), so for a FOREIGN holder it is absent
    on every box except the one running that agent's loop — which is the
    normal case, not the exception. This function previously folded that
    absence into the same `return False` as a genuine mismatch, so the warning
    was inert for 100% of foreign holders on any box (measured 4-of-4 foreign
    agents inert on cc-05 by the review that filed this, independently
    reproduced 4-of-4 on cc-04 before the fix). That is guard-2418 exactly,
    and it left the warning silent for the very incident its docstring cites:
    foxtrot (LAPTOP-3IOFCNEO) and bravo (cc-05) are DIFFERENT BOXES.

    PRESENT-AND-STALE IS A THIRD STATE (g-306-148). g-306-141 split ABSENT
    (unanswerable) from PRESENT (answered), and case 2 below then read every
    PRESENT-but-mismatched rsid as a positive answer. It is not one. Note the
    asymmetry that made this visible: the MATCH branch demands a FRESH heartbeat
    before concluding "live", while the MISMATCH branch used to trust a file of
    arbitrary age to conclude "dormant". Freshness was required of the positive
    claim and not of the negative one, which is backwards. Two reachable cases
    were silent:
      (a) WORKER BODY, same box — `running-session-id` names only the REDUCER,
          so a live non-reducer Body of the foreign agent lands in the mismatch
          branch (g-306-140, already learned on the sibling).
      (b) STALE LOCAL RSID, cross box — the file is `sync_tier: machine_local`
          and PERSISTS after an agent moves boxes. A foreign agent that ran here
          yesterday leaves its old sid on disk; running elsewhere today under a
          new sid, it lands in the mismatch branch and the cross-box fallback is
          never consulted. That reproduces the exact silence g-306-141 was filed
          to fix, through the "answered" branch instead of the absent one.

    The three cases and what each consults:
      - present and == holder_sid -> the holder's box is THIS box and the
        holder IS its runner. Local heartbeat decides. (unchanged)
      - present and != holder_sid -> the holder is not that agent's REDUCER,
        which is NOT the same as dormant. Consult the holder's per-Body
        heartbeat (case a), then fall through to the goal-scoped shard (case b).
        This is where it diverges from `_holder_session_is_live_runner`, and the
        divergence is deliberate: the sibling stops after the Body probe because
        a wrong True there WEDGES a goal for every session of the agent, so it
        cannot afford a signal that reports "alive somewhere". This path only
        WARNS, so a wrong True costs one line — and `_cross_box_holder_is_live`
        is goal-scoped on `in_flight.goal_id`, not bare liveness, so it fires
        only when the peer's authoritative row says it is working THIS goal.
        A peer alive on unrelated work stays quiet and the recovery sweeps are
        not nagged (test cases 2 and 4).
      - absent/empty -> this box does not run that agent's loop, so the file
        is not false, it is UNANSWERABLE. Consult the authoritative,
        goal-scoped shard signal instead.

    `goal_id` is REQUIRED, not optional, and that is deliberate: the shard
    carries no session id, so `in_flight.goal_id` is the only thing that can
    make the cross-box answer goal-scoped rather than "is this mind alive
    anywhere". An optional-with-default parameter would let a future call site
    silently disable the fallback while still compiling (the F-002 hazard,
    fixed on the sibling in the same change).

    Reading a PEER's shard is the documented purpose of
    `read_shard_authoritative`, not a widening of it — its docstring names
    "a caller that needs exactly one peer's row" and cites `liveness_check`
    as that caller (core/scripts/liveness_check.py:304 does exactly this).
    Confirmed by reading the primitive, not assumed.
    """
    import sys
    try:
        if not holder_sid or not agent_name:
            return False
        import yaml
        cfg_path = (ctx.paths.project_root / "core" / "config"
                    / "aspirations.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        stale_minutes = (cfg.get("runner_heartbeat") or {}).get("stale_minutes")
        if not stale_minutes:
            return False  # misconfig -> fail open, never warn
        sess = ctx.paths.agents_root / agent_name / "session"
        rsid_path = sess / "running-session-id"
        hb_path = sess / "runner-heartbeat"
        running_sid = ""
        if rsid_path.exists():
            running_sid = rsid_path.read_text(encoding="utf-8").strip()
        if not running_sid:
            # UNANSWERABLE locally -> cross-box fallback (see above). Its own
            # fail-open degrades to the local mirror, whose staleness fails the
            # freshness gate and returns False — i.e. today's silence. So the
            # worst case of this branch is exactly the behavior it replaces.
            return _cross_box_holder_is_live(ctx, agent_name, goal_id or "",
                                             stale_minutes)
        if running_sid != holder_sid:
            # PRESENT-AND-STALE is not an answer (). The holder is not
            # this agent's REDUCER; that is not the same as dormant. Case (a):
            # a live non-reducer Body on THIS box — ask the holder's own
            # per-Body heartbeat, rooted at the HOLDER's dir, never the
            # caller's. Case (b): the rsid is a machine_local leftover from
            # when this agent ran here — fall through to the goal-scoped shard,
            # which is the only signal that can see another box at all. Both
            # decline -> False, i.e. today's silence, so the worst case of this
            # branch is exactly the behavior it replaces.
            if _same_box_body_is_live(ctx, holder_sid, stale_minutes,
                                      agent_name):
                return True
            return _cross_box_holder_is_live(ctx, agent_name, goal_id or "",
                                             stale_minutes)
        if not hb_path.exists():
            return False
        import time as _time
        age_s = _time.time() - hb_path.stat().st_mtime
        return age_s <= (float(stale_minutes) * 60.0)
    except Exception as e:  # noqa: BLE001 — fail-open, but never silently
        print(f"[daemon claim] WARN: cross-agent holder liveness probe for "
              f"{agent_name!r}/{goal_id!r} failed ({type(e).__name__}: {e}); "
              f"staying quiet", file=sys.stderr)
        return False


def _completed_by_sid(ctx, goal: dict) -> Optional[str]:
    """Which BODY performed this close ( fix set B part 2).

    Resolves the session id only — WHEN to stamp belongs to each call site, and
    the three sites do not agree by accident (g-306-157): update-goal stamps on
    `completed` alone and only when unset, mirroring `completed_by`; complete-by
    stamps unconditionally on both of its arms, mirroring `completed_by` THERE,
    which is likewise unconditional and likewise covers the recurring arm's
    cycle back to `pending`. Each site matches its own neighbour, so the pair is
    joinable everywhere.

    The claim triple is popped at every terminal transition, so before this
    stamp the completing body was forensically unrecoverable — the g-115-3176
    timeline had to be rebuilt from board posts.

    Prefer the PER-REQUEST sid: the caller that actually performed the
    transition. On a non-holder completion that differs from the holder, and
    that is precisely the interesting case (`_nonholder_claim_warning` exists
    to flag it) — so preferring the request sid records who acted, while the
    warning records who held.

    Fall back to the claim's sid. Both shipped wrappers now send `&sid=`
    (`aspirations-complete-by.sh` already did; `aspirations-update-goal.sh`
    was the asymmetric half and was fixed in this same change — before it,
    the most-travelled terminal door sent none, so a request-sid-only design
    would have silently skipped every non-recurring close while looking
    complete). The fallback is still load-bearing for two live populations:
    an un-hooked launch where MIND_SID is unset, and any non-wrapper caller
    posting to this endpoint directly. Absent both, no field is written —
    an absent sid beats a wrong one.

    NEVER read os.environ["MIND_SID"] here. The daemon is long-lived and
    carries the MIND_SID of whichever session SPAWNED it (measured cc-02:
    pid 3155606 holding zeta's SID), and every agent's writes route through
    that one process — so an env read would stamp EVERY daemon-routed close
    with one arbitrary session's id for the process lifetime. That is
    systematically wrong attribution presented as fact: strictly worse than
    an absent field, and the same defect class this fix set exists to remove.
    The CLI sibling (aspirations.py) DOES use env, correctly — there the
    process IS the session. Same asymmetry as `completed_by` (g-115-1562).
    """
    return (ctx.query.get("sid") or "").strip() or goal.get("claimed_by_sid")


def _displaced_claim_warning(ctx, goal: dict, goal_id: str) -> Optional[str]:
    """Tell a session AT ITS NEXT WRITE that its own claim was taken over.

    g-306-329, the notify half. Its sibling `_nonholder_claim_warning` below
    warns the INTRUDER ("you are acting on a goal someone else holds"). Nothing
    warned the DISPLACED party, so a Body whose claim was taken kept working and
    found out at commit-gate time -- measured 2026-08-19, 34 minutes later.

    WHY THE SIBLING DOES NOT ALREADY COVER THIS, which is the whole reason this
    function exists rather than a flag on that one. Its same-agent branch returns
    None unless `_holder_session_is_live_runner(...)` is True. A worker Body is
    NOT its agent's autonomous runner, so when a worker takes the claim the
    sibling goes quiet for the very session that just lost it. That is the same
    conditional-on-runner logic rb-8513 found in the claim path ("exclusivity is
    CONDITIONAL, the takeover is SANCTIONED"), reaching one layer further than
    anyone had noticed.

    That gate is RIGHT for the sibling and WRONG here, and the asymmetry is the
    design: the sibling stays quiet for a dormant holder because releasing a dead
    session's claim is ordinary cleanup and the recovery sweeps must never be
    nagged. But the fact THIS function reports -- "your claim is gone" -- is true
    regardless of who holds it now or whether they are live, so gating it on the
    new holder's liveness suppresses it exactly when a worker did the taking.

    PURE BY CONTRACT: reads only the goal dict already in hand. No liveness
    probe, no I/O, no network. That is what makes it safe to call on the hot
    update_goal path, where the sibling (which probes) deliberately is not.

    ABSENCE-SAFE (guard-4500): returns None whenever either sid is missing. A
    fail-open writer means `claimed_by_sid` is absent both when there is no
    claiming session AND when a legacy caller sent none, and those are the same
    read here -- so absence must never be reported as displacement. WARN-ONLY,
    never a refusal: it reports a fact the caller needs, and a wrong warning
    costs a confused reader while a wrong refusal would wedge live work.

    THE MESSAGE ADDRESSES TWO AUDIENCES ON PURPOSE, AND THAT IS WHY IT DOES NOT
    LEAD WITH "your claim was taken". This condition -- caller sid != holder sid
    -- is reached by two different callers and the goal record cannot tell them
    apart: a genuinely displaced executor, and a legitimate out-of-band writer
    that never held the claim. stranded-claim-sweep.py POSTs update-goal to set
    status=pending when clearing a dead session's claim (and
    parent-supersession-sweep / routing-audit-target-status-sweep write
    similarly), so an unconditional "YOUR CLAIM IS GONE" would assert
    displacement to a sweep that was never displaced -- a false statement, and
    exactly the nagging the sibling's docstring warns recovery paths must not
    get. So the headline states only the OBSERVED fact (ownership mismatch) and
    the displacement reading is offered conditionally, with the sweep case named
    as expected. If you ever want the unconditional wording, you need the
    displaced identity from a source that records it -- the
    claim-cross-session-takeover ledger rows carry `displaced_sid` -- not from
    this dict.
    """
    try:
        caller_sid = (ctx.query.get("sid") or "").strip() or None
        holder_sid = goal.get("claimed_by_sid")
        holder = goal.get("claimed_by")
        if not caller_sid or not holder_sid or not holder:
            return None
        if holder_sid == caller_sid:
            return None            # caller still holds it -- nothing to say
        if holder != _agent_name(ctx):
            return None            # cross-agent is the sibling's branch
        return (f"CLAIM-OWNERSHIP MISMATCH on {goal_id}: this write came from "
                f"sid={caller_sid}, but the claim is held by sid={holder_sid} "
                f"(same agent {holder}, claimed_at={goal.get('claimed_at')}). "
                f"The write was APPLIED (warn-only). "
                f"IF YOU WERE EXECUTING THIS GOAL you have been DISPLACED by a "
                f"cross-session take-over: another session is working it now, "
                f"so stop executing on the assumption you own it -- duplicated "
                f"side effects are the cost. Confirm with: grep "
                f"'\"gate\": \"claim-cross-session-takeover\"' "
                f"world/override-bypass-ledger.jsonl. "
                f"IF YOU ARE A RECOVERY SWEEP or other out-of-band writer "
                f"(stranded-claim-sweep, parent-supersession-sweep, ...), this "
                f"is EXPECTED and needs no action (g-306-329).")
    except Exception:
        return None


def _nonholder_claim_warning(ctx, goal: dict, goal_id: str,
                             op: str) -> Optional[str]:
    """Warn when `op` is invoked by a session that does NOT hold the claim.

    Outcome 5 of g-115-3176. WARN, never refuse — and that is a deliberate
    asymmetry, not a weaker version of the claim-side refusal:

    `stranded-claim-sweep.py --apply` exists to release claims left behind by
    DEAD sessions, so it necessarily runs from a different session than the
    (dead) holder. Every recovery path has that shape. A refusal here would
    break the sweep fleet-wide and wedge exactly the stranded goals it repairs
    — the failure g-115-3176 exists to prevent, inverted. The claim side can
    afford to refuse because the caller can simply pick another goal; the
    release side cannot, because there is no alternative path to un-wedge a
    goal.

    So the non-holder path stays PERMITTED and merely becomes VISIBLE. Returns
    None (no warning) whenever the situation is routine: no SID on either side,
    same session, or a DORMANT holder — releasing a dormant session's claim is
    ordinary cleanup, not a collision. Fail-open on any error.

    CROSS-AGENT branch (g-115-4232): a holder that is a DIFFERENT agent used
    to early-return None here, deferring to "a separate concern with its own
    handling" — but no such handling existed anywhere on the complete/release
    path (the claim side refuses cross-agent claims; the close side had zero
    coverage). Measured incident: foxtrot's claim was falsely swept mid-
    execution, bravo claimed legitimately, and foxtrot's unaware session then
    completed the goal over bravo's LIVE claim with no signal at all. Now:
    warn when the foreign holder's claim-holding session is that agent's live
    runner (they are likely still working it); stay quiet for dormant/dead
    foreign holders — completing an abandoned goal is ordinary supersession,
    and the recovery sweeps must never be nagged.
    """
    try:
        holder = goal.get("claimed_by")
        holder_sid = goal.get("claimed_by_sid")
        caller_sid = (ctx.query.get("sid") or "").strip() or None
        if not holder:
            return None
        caller_agent = _agent_name(ctx)
        if holder != caller_agent:
            if not _cross_agent_holder_is_live(ctx, holder, holder_sid or "",
                                               goal_id):
                return None
            return (f"{op} of {goal_id} was invoked by {caller_agent} — but "
                    f"the claim is held by a DIFFERENT AGENT's LIVE session "
                    f"({holder}, sid={holder_sid}, claimed_at="
                    f"{goal.get('claimed_at')}). {holder}'s running loop is "
                    f"likely still working this goal. The {op} was applied "
                    f"anyway (warn-only) — coordinate on the board before "
                    f"building on this outcome; legitimate for supersession "
                    f"or hand-off, wrong for a race (g-115-4232).")
        if not holder_sid or not caller_sid:
            return None
        if holder_sid == caller_sid:
            return None
        if not _holder_session_is_live_runner(ctx, holder, holder_sid,
                                              goal_id):
            return None
        return (f"{op} of {goal_id} was invoked by a session that does NOT "
                f"hold the claim: the claim is held by a DIFFERENT LIVE "
                f"session of {holder} (holding sid={holder_sid}, claimed_at="
                f"{goal.get('claimed_at')}; your sid={caller_sid}), which is "
                f"this agent's running autonomous loop. The {op} was applied "
                f"anyway, but that session is likely still working this goal "
                f"— check before proceeding (g-115-3176).")
    except Exception:
        return None


def claim(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/aspirations/claim?id=<goal_id>&agent=<name>
    [&cross_lane=<reason>][&override_lane_pin=<reason>][&sid=<sid>][&source=<q>]
    """
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
    # Lane-pin override (). A pin is a USER directive, so the escape
    # hatch is deliberately a justification string rather than a bare boolean:
    # the value is what lands in override-bypass-ledger.jsonl, and an override
    # with no stated reason is exactly the honor-system bypass the gate removes.
    override_lane_pin = (ctx.query.get("override_lane_pin") or "").strip() or None
    # Claiming SESSION identity ( slice 1, ADDITIVE — recorded only,
    # no refusal behavior depends on it yet). A claim's identity is the AGENT
    # NAME alone, so the `existing != agent_name` test below treats a claim from
    # a DIFFERENT session of the SAME agent as an idempotent no-op: two sessions
    # both "succeed" and neither is warned. Observed live 2026-07-25 (two
    # sessions of one agent held the same world goal 16min apart; the second was
    # one write away from creating duplicate credentials in an external
    # service). Recording the SID
    # makes the collision DIAGNOSABLE now — `claimed_by_sid` names which session
    # holds a claim — and is the prerequisite for the session-scoped refusal
    # (remaining outcomes of ). Optional: callers that do not send it
    # behave exactly as before.
    claim_sid = (ctx.query.get("sid") or "").strip() or None

    # QUEUE SELECTION (). Was hardcoded `"world"`, which is why the
    # agent queue had no claim protocol at all: the world path below carries
    # `same_agent_other_session` (), `_holder_session_is_live_runner`
    # + `_same_box_body_is_live` / `_cross_box_holder_is_live` (), and
    # the SID-less refusal (-b) — every one of them Mind/Body-aware and
    # every one unreachable for source=agent, which returned 400 `agent_queue_goal`
    # ~50 lines down instead. That exemption's stated premise is "SINGLE-AGENT
    # ACCESS"; asp-306 falsified it, because a reducer and N worker Bodies are all
    # executors of ONE agent selecting from one pool. Measured 2026-08-06: worker
    # (cc-08) and reducer both executed  from one cadence fire, producing
    # five hypotheses; neither could see the other.
    #
    # So this is NOT "build claiming for agent goals" — the mechanism was already
    # here. It is "stop exempting one queue from a protocol that already solves
    # this in both shapes" (communication-clarity rule 5: one claim protocol,
    # not two).
    #
    # DEFAULT IS "world", so every existing caller is byte-identical: the wrapper
    # sends no `source` today, and the only production caller is
    # aspirations-claim.sh. The new capability is strictly opt-in via
    # `&source=agent`, which nothing calls yet — the loop digest still guards the
    # claim with `IF source==world`.
    #
    # THAT GUARD MUST BE DROPPED **AFTER** THIS SHIPS, NEVER WITH IT. The digest
    # is LLM-read markdown, so a change there takes effect on every agent's very
    # NEXT iteration, while this module is only picked up when the daemon
    # recycles (no autoreload — verified: PID 325497 had held the old module 10h).
    # Flipping both together therefore opens exactly the window the landmine in
    # aspirations-claim.sh warns about: agent-source iterations calling a daemon
    # that still 400s, which the digest reads as "journal abort + LOOP_CONTINUE"
    # — silently halting the entire recurring cadence (.. all
    # live in the agent queue). Order: land this -> commit (post-commit recycles
    # the daemon) -> verify live -> only then drop the digest guard.
    source = (ctx.query.get("source") or "world").strip()
    if source not in ("world", "agent"):
        return Response.error(400, "invalid_source",
                              "source must be world or agent")
    # FW-2 / guard-620: agent-scoped writes MUST carry an explicit X-Mind-Agent
    # header — never silently fall back to the alphabetically-first agent's
    # queue. release() already does this for its own source param; claim() never
    # needed it while it was world-only.
    agent_guard = _require_explicit_agent(ctx, source)
    if agent_guard is not None:
        return agent_guard
    live_path, base_dir = _resolve_paths(ctx, source)
    agent = _agent_name(ctx)

    try:
        with file_locks.locked(live_path):
            items = _read_jsonl(live_path)
            found = _find_goal(items, goal_id)
            # Detect goal-ID collision across queues.
            # When the same goal_id exists in BOTH world and agent queues,
            # claim previously resolved to the world copy silently regardless
            # of caller intent. Now: if collision detected, refuse with 409
            # so the caller picks a queue explicitly by re-issuing with
            # &source=agent or &source=world. This comment previously carried
            # the same falsified premise as the 409 body below it -- that
            # agent-queue goals need no claim -- which  disproved and
            #  removed from the loop. Both are corrected together
            # (): a stale comment misleads the next EDITOR exactly as
            # the stale string misled the next CALLER.
            #
            # Scoped to source=="world" (): this 409 exists to force the
            # caller to name a queue, so it is exactly redundant once they have.
            # Leaving it unscoped would make an explicit `&source=agent` claim of
            # a colliding id refuse with advice the caller has already followed.
            if found is not None and source == "world":
                agent_live = ctx.paths.agent / "aspirations.jsonl"
                if agent_live.exists():
                    try:
                        agent_items = _read_jsonl(agent_live)
                        if _find_goal(agent_items, goal_id) is not None:
                            return Response.error(409, "goal_id_collision",
                                f"Goal {goal_id} exists in BOTH world and "
                                f"agent queues. Claim is ambiguous — name the "
                                f"queue explicitly and re-issue: &source=agent "
                                f"claims the agent-queue copy under the same "
                                f"session-scoped guards world goals get, "
                                f"&source=world claims the world copy. "
                                f"Renaming one queue's copy also resolves it. "
                                f"Do NOT read this as 'agent goals don't "
                                f"require claims' — they have carried claims "
                                f"since g-306-238, and a reducer and its "
                                f"worker Bodies are separate sessions of one "
                                f"agent that have double-executed the same "
                                f"goal.")
                    except Exception:
                        pass
            if found is None:
                if source == "world":
                    agent_live = ctx.paths.agent / "aspirations.jsonl"
                    if agent_live.exists():
                        try:
                            agent_items = _read_jsonl(agent_live)
                            if _find_goal(agent_items, goal_id) is not None:
                                # STILL A REFUSAL, but no longer on the old
                                # premise (). The prior text said
                                # agent-queue goals "do not require claims
                                # (single-agent access)" and told the caller to
                                # "proceed directly to execution" — advice that
                                # is now measurably wrong under the Mind/Body
                                # split. What is actually true is narrower: this
                                # call did not NAME the agent queue, and claim()
                                # must not silently cross queues. Say that, and
                                # point at the capability that now exists.
                                return Response.error(400, "agent_queue_goal",
                                    f"Goal {goal_id} is in the agent queue, but "
                                    f"this claim did not request it "
                                    f"(source=world). Re-issue with "
                                    f"&source=agent to claim it under the same "
                                    f"session-scoped guards world goals get. Do "
                                    f"NOT read this as 'agent goals need no "
                                    f"claim' — a reducer and its worker Bodies "
                                    f"are separate sessions of one agent and "
                                    f"have double-executed the same goal "
                                    f"(g-306-238).")
                        except Exception:
                            pass
                return Response.error(404, "goal_not_found",
                    f"Goal {goal_id} not found in {source} queue")

            asp_idx, goal_idx, asp = found
            goal = asp["goals"][goal_idx]
            claimed_asp_id = asp.get("id")  # for the post-write persistence read-back

            # Terminal-status refusal (). Claiming an already-closed
            # goal is never correct: the work is done, and the claimer both stamps
            # a claimant onto a closed record and — if it proceeds to execute —
            # risks overwriting the closer's outcome_note (the  clobber
            # class). This is a missing BRANCH, not missing data: the success
            # payload already returns `status`, so the endpoint was handing back
            # the very field it should have refused on.
            #
            # Ordered FIRST among the record-level refusals, ahead of cross_lane
            # and already_claimed, because terminality does not depend on lane or
            # claim state. Ordering it later produces actively misleading advice —
            # a terminal cross-lane goal would return cross_lane_refused, telling
            # the caller to pass cross_lane, which would then let them claim a
            # finished goal.
            #
            # NOT the selector's bug: a selector snapshot is stale by construction
            # (measured twice — bravo/ skipped 31s before the claim,
            # echo/ completed 23s before it), which is exactly why the
            # claim is the right chokepoint. Widening the selector only shrinks
            # the window.
            goal_status = (goal.get("status") or "").strip().lower()
            if goal_status in _TERMINAL_STATUSES:
                return Response.error(409, "goal_terminal",
                    f"Goal {goal_id} is already '{goal_status}' and cannot be "
                    f"claimed (completed_by={goal.get('completed_by')!r}, "
                    f"completed_at={goal.get('completed_at')!r}, "
                    f"outcome_note={goal.get('outcome_note')!r}). The work is "
                    f"done — do NOT execute it and do NOT write an outcome_note "
                    f"onto this record. Re-run goal-selector.sh for a fresh "
                    f"candidate.")

            # SID-less claim refusal (-b). The same-agent/other-session
            # guard below requires BOTH holder_sid AND claim_sid; a claim that
            # sends no `sid` skips it entirely and falls through to the
            # idempotent no-op path. That is the exact collision  was
            # filed to close, reachable by omitting one query param — and it
            # also leaves claimed_by_sid unstamped, so the NEXT session's guard
            # cannot fire either. Refused unconditionally for world goals,
            # regardless of current claim state, because the unstamped record is
            # the durable half of the harm.
            #
            # Reached only when the caller genuinely sent no sid. This comment
            # used to add "so `goal` is a world goal by construction" — that is
            # FALSE as of , because a claim may now name the agent
            # queue explicitly. The refusal itself needs no change and is if
            # anything MORE load-bearing there: session identity is the entire
            # mechanism by which a reducer and its worker Bodies are told apart,
            # so an agent-queue claim without a sid would reinstate exactly the
            # double-execution this change exists to stop.
            #
            # COVERAGE AUDIT — measured 2026-08-03 (alpha, cc-04, Linux
            # 6.8.0-136-generic) BEFORE this refusal was written, per guard-1562.
            # Full detail in the knowledge tree:
            # system/system-constraints-loop/eighth-witness-sid-collision-gate.
            #   * `core/scripts/aspirations-claim.sh` (L290-291) is the ONLY
            #     production caller and appends `&sid=$MIND_SID` whenever the
            #     var is non-empty. `bash-agent-inject.py` (L362-365, L478)
            #     injects MIND_SID into EVERY Bash tool call and its own
            #     comment forbids making it conditional — so every LLM-driven
            #     claim carries one. No executable script calls the wrapper
            #     (grep of core/ + mind_api/ returned comments and docs only),
            #     so no cron/background/CI path reaches it sid-less either.
            #   * LIVE world queue at audit time: 5008 goals, 6 currently
            #     holding a claim, 6/6 (100%) carrying claimed_by_sid, 0
            #     without. That counts CURRENTLY-HELD claims (claimed_by is
            #     cleared at close), which is the right population for "who
            #     would newly be refused" — not an all-time claim census.
            #   * The set this refusal DOES newly reject is 13 direct endpoint
            #     POSTs across 3 TEST files that bypass the wrapper:
            #     core/scripts/tests/test_cross_lane_claim.py and
            #     test_claim_staleness_takeback.py (both claim world goals),
            #     and mind_api/tests/test_runtime_aspirations_retire_release_claim.py
            #     (11 POSTs). Tests can supply a sid trivially, so they are NOT
            #     "callers that cannot" — the hatch below covers them only while
            #     they deliberately exercise the legacy shape, plus any future
            #     genuinely un-hooked context.
            if not claim_sid:
                no_sid_justification = _no_sid_bypass()
                if no_sid_justification is None:
                    return Response.error(400, "missing_claim_sid",
                        f"Claim of {source} goal {goal_id} carries no 'sid' query "
                        f"parameter, so the same-agent/other-session guard "
                        f"cannot run and claimed_by_sid would be left unstamped. "
                        f"Callers reach this endpoint through "
                        f"core/scripts/aspirations-claim.sh, which sends "
                        f"&sid=$MIND_SID automatically — MIND_SID is injected "
                        f"into every Bash tool call by bash-agent-inject.py. If "
                        f"you are seeing this, either the claim bypassed that "
                        f"wrapper or MIND_SID was empty in an un-hooked launch "
                        f"context (background/cron/CI). Fix the caller to send a "
                        f"sid; if it genuinely cannot, set "
                        f"{_NO_SID_ENV}='<why>' (logged to "
                        f"override-bypass-ledger.jsonl under gate "
                        f"'claim-sid-gate').")
                _audit_no_sid_claim_inline(
                    ctx, goal_id=goal_id, agent_claiming=agent_name,
                    justification=no_sid_justification,
                    title=goal.get("title"))

            # LANE-PIN GATE () — Layer B for the Standing Lane Pins
            # registry in world/conventions/capability-routing.md. A pin is a
            # durable USER directive fixing ONE agent's work surface; the gate
            # parses the registry table LIVE, so deleting the row lifts the pin
            # with no code change (outcome 2).
            #
            # Placed BEFORE the cross-lane check deliberately. A pin governs the
            # agent's WHOLE surface, while cross_lane governs one goal's routing
            # preference. If cross_lane refused first, the caller would supply a
            # cross_lane reason, pass, and then hit the pin anyway — a two-step
            # refusal whose first message names the wrong cause. It stays AFTER
            # the terminal-status and SID-less refusals, which are about whether
            # this claim is coherent at all rather than about who may make it.
            #
            # FAIL-OPEN at every path inside the gate (guard-142): unreadable
            # registry, no pin for this agent, goal matching BOTH lanes, or any
            # exception all allow. It refuses only on out-of-lane evidence with
            # NO in-lane evidence, because a false refusal wedges a legitimate
            # claim while a false allow merely leaves the Layer-A honor system
            # where the fleet already was.
            # THE CALL SITE MUST FAIL OPEN TOO, not just evaluate()'s body.
            # Arguments are evaluated BEFORE the gate is entered, so anything
            # raised while building them escapes the gate's own bare-except and
            # 500s the claim — a broken gate wedging the fleet, the one thing
            # requirement (3) forbids. Measured, not hypothetical: this landed
            # as 3 reds in test_verify_goal_persisted, whose fake ctx.paths
            # carries no `meta` (the real PathSet has one; that fake was already
            # missing a slot, and reading a NEW attribute is what surfaced it).
            # Loud on the way out (guard-1977) — a gate that silently declines
            # to run reports success by default, so the WARN is what keeps a
            # permanently-disabled gate distinguishable from a passing one.
            try:
                lane_pin_result = _lane_pin_eval(
                    agent_name, goal,
                    world_dir=getattr(ctx.paths, "world", None),
                    override_lane_pin=override_lane_pin,
                    meta_dir=getattr(ctx.paths, "meta", None),
                )
            except Exception as _lp_err:
                import sys
                print(f"[daemon claim] WARN: lane-pin gate raised, allowing "
                      f"claim of {goal_id} by {agent_name}: "
                      f"{type(_lp_err).__name__}: {_lp_err}", file=sys.stderr)
                lane_pin_result = {}
            if lane_pin_result.get("would_block"):
                return Response.error(400, "lane_pin_refused",
                                      lane_pin_result.get("reason")
                                      or f"Goal {goal_id} is outside the lane "
                                         f"pinned for '{agent_name}'.")
            if lane_pin_result.get("override"):
                _audit_lane_pin_override_inline(
                    ctx, goal_id=goal_id, agent_claiming=agent_name,
                    pin_id=lane_pin_result.get("pin_id"),
                    evidence=lane_pin_result.get("evidence"),
                    justification=lane_pin_result["override"],
                    category=goal.get("category"), title=goal.get("title"))

            intended = goal.get("intended_agent")
            if _routes_away_from(intended, agent_name):
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
                # Staleness take-back (): mirror goal-selector.py's
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
            elif existing == agent_name:
                # SAME-AGENT, possibly DIFFERENT SESSION ().
                # The branch above only fires for a DIFFERENT agent, so this
                # case previously fell straight through as an idempotent no-op:
                # two sessions of one agent both "succeeded" and neither was
                # warned. Refuse ONLY on positive confirmation that the holder
                # is the agent's live autonomous runner — that is the dangerous
                # shape (an observer/assistant session claiming what the running
                # loop already holds). Every other case still falls through
                # unchanged, preserving:
                #   - same-session re-claim  -> idempotent no-op (sids equal)
                #   - legacy claim, no sid   -> no-op (holder_sid falsy)
                #   - caller sent no sid     -> no-op (claim_sid falsy)
                #   - dormant/previous session -> allowed takeover
                holder_sid = goal.get("claimed_by_sid")
                if (holder_sid and claim_sid and holder_sid != claim_sid
                        and _holder_session_is_live_runner(
                            ctx, agent_name, holder_sid, goal_id)):
                    return Response.error(
                        409, "same_agent_other_session",
                        f"Goal {goal_id} is already claimed by a DIFFERENT "
                        f"LIVE session of {agent_name} "
                        f"(holding sid={holder_sid}, claimed_at="
                        f"{goal.get('claimed_at')}; your sid={claim_sid}). "
                        f"That session is this agent's running autonomous "
                        f"loop. Two sessions working one goal duplicates side "
                        f"effects — do NOT proceed. Pick a different goal, or "
                        f"stop the other session first.")
                if holder_sid and claim_sid and holder_sid != claim_sid:
                    # Fell through the refusal above => the holder is DORMANT.
                    # Outcome 4 requires the takeover be logged, not silent:
                    # this is the one path where a claim legitimately changes
                    # hands between two sessions of one agent, so it must leave
                    # a trace for the same reason the collision did not.
                    import sys as _sys
                    print(f"[daemon claim] cross-session take-over of "
                          f"{goal_id}: dormant sid={holder_sid} -> "
                          f"sid={claim_sid} (agent {agent_name})",
                          file=_sys.stderr)
                    # : the line above is written INSIDE the daemon,
                    # so the displaced session cannot read it and the event was
                    # invisible to audit. Emit the same fact durably, to the
                    # ledger four sibling claim-events already use. This makes
                    # take-overs COUNTABLE; it does not notify the loser, which
                    # is the separate half  leaves open.
                    _audit_cross_session_takeover_inline(
                        ctx, goal_id=goal_id, agent_name=agent_name,
                        holder_sid=holder_sid, claim_sid=claim_sid,
                        category=goal.get("category"),
                        title=goal.get("title"))
                    claim_summary = (f"claim {goal_id} (cross-session "
                                     f"take-over from dormant {holder_sid})")

            goal["claimed_by"] = agent_name
            goal["claimed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            #  slice 1: stamp the claiming session so a same-agent
            # cross-session collision is visible after the fact. Only written
            # when the caller supplied one — never clobber a prior SID with a
            # None from a caller that does not send it, since a take-back or a
            # legacy caller would otherwise erase the holding session's identity
            # and make the collision LESS diagnosable than before.
            if claim_sid:
                goal["claimed_by_sid"] = claim_sid
            # Attempt marker (): a CLAIM is the goal's first real
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
            # : executor identity that SURVIVES completion.
            # `completed_by` is derived from the CALLER at close time by all
            # three of its writers, so it records who ISSUED the close, not who
            # did the work — and it is unfalsifiable, because guard-151 pops
            # `claimed_by`/`claimed_at`/`claimed_by_sid` on every terminal
            # transition. Measured: of 4178 completed world goals, 3872 carry
            # completed_by and ZERO also carry claimed_by, so the obvious audit
            # ("flag completed_by != claimed_by") returns 0 forever on every box
            # no matter how wrong the field is. The SID pair is popped
            # identically (633 carry completed_by_sid, 0 carry both), so there
            # is no SID-shaped way out either.
            #
            # The fix is a SIBLING field, never a repair of the pop (rb-2148 —
            # add an optional sibling, never break the locked field). guard-151
            # is a designed, convention-backed, anchor-commented invariant;
            # preserving claimed_by through completion would violate it at two
            # anchored sites. `executed_by` survives BY CONSTRUCTION: every pop
            # in this module and in aspirations.py is explicit-key, so nothing
            # reaches a field they do not name.
            #
            # WRITTEN HERE, inside the claim, under the same lock — never as a
            # caller-side follow-on update-goal (guard-2793 / guard-2309: never
            # chain aspirations-claim.sh with a follow-on goal write).
            #
            # UNCONDITIONAL, mirroring `claimed_by` above — deliberately NOT
            # `setdefault` like `started` on the line before, and this is the
            # one judgment call in the change. The two differ on the take-back
            # path: `started` answers "when was this FIRST attempted", so it
            # must keep the original; `executed_by` answers "who did the work",
            # and on a stale-claim take-back the original holder is by
            # definition the one who went dormant. setdefault would durably
            # record an ABANDONER as the executor, defeating the field's whole
            # purpose. The residual ambiguity is real and is not fixed here: if
            # A claims, executes substantially, dies, and B takes over only to
            # close, this records B. That case is rarer than plain abandonment
            # and no claim-time write can distinguish them.
            #
            # Note this does NOT contradict the goal's "never overwrite it":
            # the evil that spec names is CLOSE-path clobbering (the complete-by
            # endpoint's unconditional completed_by write, which destroys a
            # correct prior value). No non-claim path writes executed_by at all.
            goal["executed_by"] = agent_name
            # The SID half CLEARS on a no-sid claim rather than persisting —
            # and this DELIBERATELY diverges from the `claimed_by_sid` write
            # above, which keeps a prior value. Do not "restore consistency"
            # with that neighbour: its comment's rationale ("never clobber a
            # prior SID with a None") holds only because guard-151 pops the
            # whole claim triple at every terminal transition, so a stale
            # claimed_by/claimed_by_sid pair lives at most until close.
            # executed_by is designed to SURVIVE completion, so the identical
            # code shape here would make the divergence PERMANENT: a reclaim
            # through the `_no_sid_bypass()` hatch would leave the new agent
            # beside the previous holder's sid, forever. A permanently
            # contradictory attribution pair is worse than a missing one — an
            # absent executed_by_sid reads honestly as "not recorded", a stale
            # one reads as a confident wrong answer, which is the exact
            # un-auditability this field exists to remove.
            # (echo-fec-executed-by-sid-can-go-stale-202608081700; guard-3116 —
            # derive the write-guard from the question the field answers, never
            # by mirroring the adjacent field.)
            if claim_sid:
                goal["executed_by_sid"] = claim_sid
            else:
                goal.pop("executed_by_sid", None)

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
    # (; add_goal-pattern ). Two live specimens on cc-05
    # 2026-07-16 ( ~03:29 +  04:34): claim returned a
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

            # Resurrection reconcile FIRST (goal-completion audit 2026-08-16):
            # a live copy of an aspiration the archive already holds as
            # terminal gets the archive's disposition back on the goals the
            # archive dispositioned, and — when nothing post-dates the
            # archive — is re-marked so the classification below archives it
            # (replace-by-id) instead of leaving a pristine pending zombie
            # live forever. Reads the archive under the live lock; the
            # classification loop re-reads it only when there is something
            # to archive, so this is the one archive read on the no-op path.
            resurrected_goal_ids = _reconcile_resurrected(
                items, _read_jsonl(archive_path), warnings)

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
                        # : disposition open goals BEFORE archiving.
                        # The completed-path guard above keeps
                        # completed-with-unfinished aspirations live, but the
                        # RETIRED path has no such guard — its open goals would
                        # ride into the archive still non-terminal and strand as
                        # invisible-pending (no read path / the selector scans
                        # the archive). No-op for completed-with-no-unfinished
                        # (they reach here only after the guard found none).
                        stranded = _disposition_open_goals_on_retire(a, a["id"])
                        if stranded:
                            warnings.append(
                                f"Archived {a['id']} (retired): auto-dispositioned "
                                f"{len(stranded)} open goal(s) to 'skipped' to "
                                f"prevent invisible-pending stranding (g-115-2860): "
                                f"{', '.join(stranded)}.")
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
                if recovered or resurrected_goal_ids:
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
                    "resurrected_reconciled": resurrected_goal_ids,
                    "warnings": warnings if warnings else None,
                })

            archived_goal_ids: set = set()
            for asp in to_archive:
                for g in asp.get("goals", []):
                    archived_goal_ids.add(g["id"])

            # Read existing archive, extend, normalize each, write the whole list back.
            # Replace-by-id on collision (): a record already present in
            # the archive (e.g. resurrected into the live file by an own-cloud
            # partial write, then re-retired) must NOT append a duplicate — the
            # incoming copy carries the newest state, so it replaces the stale
            # archive copy in place. Extend-only here appended a second copy per
            # re-sweep (observed: asp-344 archived as completed, resurrected,
            # retired in live — a plain extend would have doubled it).
            # Replace = aspiration-level fields from the incoming copy, goals
            # UNIONED (_archive_replace_row) — a resurrected snapshot can carry
            # fewer goals than the archive row it supersedes, and the archive
            # is the last home those records have (2026-08-16, asp-240).
            archive = _read_jsonl(archive_path)
            incoming_by_id = {a.get("id"): a for a in to_archive}
            deduped_replaced = 0
            for i, existing in enumerate(archive):
                eid = existing.get("id")
                if eid in incoming_by_id:
                    archive[i] = _archive_replace_row(
                        existing, incoming_by_id.pop(eid))
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
        "resurrected_reconciled": resurrected_goal_ids,
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

    Sweep the live aspirations store and clear the claim TRIPLE (claimed_by,
    claimed_at, claimed_by_sid) from any goal whose status is terminal.
    Idempotent — safe to re-run; zero-effect when there is no residue.
    Self-heal tool if any writer regresses on claim clearing.

    NOTE: this docstring previously read "Mirrors cmd_clear_stale_claims
    exactly." That function does not exist anywhere in the tree (measured
    g-306-145: the only occurrence of the name was this line), so the
    sentence asserted a parity relationship with nothing. It is worth
    recording rather than silently deleting: a stale mirror-claim on a
    self-heal sweeper invites a reader to go fix "the other side" and find
    no other side — the CLI twin that DOES exist is aspirations.py
    cmd_update_goal's terminal-status hook, which is a different function
    with a different name and was itself missing the sid pop.

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
                    # The claim is a TRIPLE (claimed_by, claimed_at,
                    # claimed_by_sid) and all three must clear together —
                    #  added the sid to the four other pop sites in
                    # this file and missed this one ().
                    #
                    # SCOPED TO THIS SWEEPER (): the sid belongs in
                    # the SELECTION predicate of anything whose job is to FIND
                    # residue — it is not a universal rule for every predicate
                    # that reads the triple. Without the disjunct a terminal
                    # goal carrying ONLY an orphaned sid never matches, so it
                    # is permanently invisible to the sweeper that exists to
                    # clean it up — and this sweeper is the self-heal path for
                    # exactly that residue. The disjunct is what makes the fix
                    # retroactive for orphans already on disk, rather than only
                    # stopping new ones.
                    #
                    # The qualifier is load-bearing because release() in this
                    # same file deliberately does NOT carry the disjunct, for a
                    # reason documented at its had_claim assignment: it is
                    # handed a goal by id, so it selects nothing, and its
                    # narrow predicate gates a peer wake-up that an orphaned
                    # sid must not trigger. Read the local context before
                    # propagating this shape (guard-1561).
                    if (goal.get("status") in _TERMINAL_GOAL_STATUSES
                            and ("claimed_by" in goal or "claimed_at" in goal
                                 or "claimed_by_sid" in goal)):
                        cleared.append(goal.get("id"))
                        if not dry_run:
                            goal.pop("claimed_by", None)
                            goal.pop("claimed_at", None)
                            goal.pop("claimed_by_sid", None)

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
        # () — mirrors the CLI validate_aspiration, which runs the
        # check via validate_goal on every embedded goal. No ctx here (pure
        # validator); telemetry falls back to the module-default meta_dir.
        _assert_no_prose_drift(goal)
        _assert_no_invalid_checks(goal)  # , same ADD-path parity
        _assert_depends_on_consistency(goal)  # , same ADD-path parity
        _assert_intended_agent_vocab(goal)  # selection-stack review 2026-08-21, same ADD-path parity


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
