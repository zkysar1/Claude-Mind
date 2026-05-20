#!/usr/bin/env python3
"""Goal scoring and selection with exploration noise.

Implements the scoring formula from aspirations/SKILL.md Goal Selection Algorithm.
The LLM no longer computes scores — this script handles the arithmetic.
The LLM still handles Phase 2.5 (metacognitive assessment) and can override rankings.

Scoring criteria (21 deterministic + 1 stochastic weighted factors):
  priority × 1.0 + deadline_urgency × 1.0 + agent_executable × 0.8
  + variety_bonus × 0.5 + streak_momentum × 0.5 + novelty_bonus × 0.6
  + recurring_urgency × 0.8 + recurring_saturation × 0.8 + per_goal_saturation × 0.8
  + user_signal_boost × 1.2 + class_balance_bonus × 0.8
  + reward_history × 0.5 + completion_pressure × 0.8 + depth_bonus × 0.6
  + tail_bonus × 0.8
  + evidence_backing × 0.7 + deferred_readiness × 0.6
  + context_coherence × 1.0 + skill_affinity × 0.4 + directive_boost × 1.5
  + co_invest_alignment × 0.5
  + exploration_noise × (epsilon × noise_scale)  [dynamic weight]

  co_invest_alignment: +1.0 raw bonus when this candidate's co_parent_id
    matches a partner's live team-state in_flight.co_parent_id — pair-
    iteration bias. Schema + protocol in core/config/conventions/coordination.md
    Co-Investigation Protocol section. g-115-563.

  context_coherence: +2.0 if same category as last goal, 0 otherwise.
    Context-pressure agnostic — same-category reuse saves tokens regardless of zone.

  recurring_urgency: base + log2(1 + overdue_ratio) * log_scale (logarithmic, no cap)
  recurring_saturation: -(ratio * max_penalty) penalty when recurring goals dominate recent selections (CLASS level)
  per_goal_saturation: flat penalty when the SAME goal_id fires repeatedly in the recent window (GOAL level).
    Config: aspirations.yaml → per_goal_saturation. Tranche B (rb-390).
  user_signal_boost: reads <agent>/session/user-signal-snapshot.yaml (refreshed
    by the signal-refresh hook) and boosts goals listed in the
    pending_questions.silent_48h_goal_ids snapshot field — i.e., goals whose
    user-facing question has gone unanswered for 48h+. Path A (per-goal
    user_signal_kind/user_thread_id) retired 2026-04-24 (g-252-03) after
    fields stayed 0/798 for 6+ days; only the snapshot-level Path B contributes.
    Fail-open: missing snapshot OR empty silent_48h list → zero contribution.
    Config: aspirations.yaml → user_signal_boost. Tranche C (rb-390).
  class_balance_bonus: pulls under-represented work_class values up when the
    last-N session completions distribution drifts from configured targets.
    Goals with no work_class default to "unclassified" and are excluded from
    the balance computation.
    Fail-open: empty distribution or missing targets → zero contribution.
    Config: aspirations.yaml → class_balance (nested: targets, window_size,
    max_boost, max_penalty). Tranche C (rb-390).
  deferred_readiness: +1.5 when a deferred goal's time has arrived
  exploration_noise: random(0,1) scaled by developmental epsilon.
    At exploring stage (~0.85 epsilon): noise can reorder rankings.
    At mastering stage (~0.19 epsilon): noise mostly breaks ties.
"""

import argparse
import json
import math
import os
import random
import subprocess
import sys
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml  # Required — tree.py already depends on PyYAML

from _paths import WORLD_DIR, AGENT_DIR, META_DIR, CONFIG_DIR, CORE_ROOT, agents_root as _agents_root
from _fileops import locked_modify_yaml  # noqa: E402  ( applications_log)
from wm import read_wm, WM_PATH as WORKING_MEMORY_PATH  # noqa: E402
# Single source of truth for terminal goal statuses — see aspirations.py.
# Derived sets below (SKIP_STATUSES, ABANDONED_STATUSES) stay consistent if a new
# status is added to TERMINAL_GOAL_STATUSES.
from aspirations import TERMINAL_GOAL_STATUSES, STRUCTURED_DEFER_PREFIXES  # noqa: E402
SKIP_STATUSES = TERMINAL_GOAL_STATUSES | {"in-progress"}              # not selectable
ABANDONED_STATUSES = TERMINAL_GOAL_STATUSES - {"completed"}            # terminal but not "done"

# Populated by main() before scoring loop fires; consumed by score_goal's
# cross_aspiration_support criterion (LifingPolls item 2). Empty fallback
# means the criterion contributes 0 — never errors when supports[] is unset.
_ASP_COMPLETION_RATIOS: dict = {}
_STRUCTURED_DEFER_PREFIXES_LOWER = tuple(p.lower() for p in STRUCTURED_DEFER_PREFIXES)


def _synth_blocker_ref_from_structured_defer(goal):
    """For quiescence-gate compatibility (): when a goal is structurally
    deferred but lacks an explicit blocker_ref, synthesize one so
    quiescence-gate.py C2 (blocker_ref_required) treats the structural marker as
    structured-enough. Two paths:
      (a) defer_reason has STRUCTURED_DEFER_PREFIXES (precondition_unmet:,
          blocked_on_dependency:, Circuit breaker:). The structured-prefix
          bypass at write-time means cmd_update_goal accepts these defers
          without --blocker-ref; without this synth, quiescence sees
          blocker_ref=None and rejects forever.
      (b) deferred_until is an ISO timestamp in the future. Time-gated defers
          (e.g. "Deferred until 2026-05-15: ..." for telemetry soak windows)
          have a structural expiry but no explicit blocker_ref. Treat the
          time gate as the structural marker; expires_at=deferred_until.
    Returns existing blocker_ref dict if present, else a synthesized one with
    type=resource (catch-all for structural waits — see BLOCKER_REF_TYPES in
    aspirations.py), else None. The synth uses md5(stable-key)[:12] for
    external_id so quiescence C4 hysteresis hash stays stable across iters."""
    existing = goal.get("blocker_ref")
    if isinstance(existing, dict):
        return existing
    defer = goal.get("defer_reason") or ""
    deferred_until = goal.get("deferred_until")

    # Path (a): structured-prefix defer_reason
    if defer and defer.lower().startswith(_STRUCTURED_DEFER_PREFIXES_LOWER):
        set_at = goal.get("defer_reason_set_at")
        try:
            created = datetime.fromisoformat(str(set_at)) if set_at else datetime.now()
        except (ValueError, TypeError):
            created = datetime.now()
        expires = created + timedelta(hours=120)
        h = hashlib.md5(defer.encode("utf-8", errors="replace")).hexdigest()[:12]
        return {
            "type": "resource",
            "external_id": f"structured-defer:{h}",
            "state_hash": None,
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds"),
            "synthesized": True,
        }

    # Path (b): deferred_until time gate
    if deferred_until:
        try:
            expires_dt = datetime.fromisoformat(str(deferred_until))
            if expires_dt > datetime.now():
                set_at = goal.get("defer_reason_set_at")
                try:
                    created = datetime.fromisoformat(str(set_at)) if set_at else datetime.now()
                except (ValueError, TypeError):
                    created = datetime.now()
                key = f"{deferred_until}:{defer[:80]}"
                h = hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]
                return {
                    "type": "resource",
                    "external_id": f"time-gate:{h}",
                    "state_hash": None,
                    "created_at": created.isoformat(timespec="seconds"),
                    "expires_at": expires_dt.isoformat(timespec="seconds"),
                    "synthesized": True,
                }
        except (ValueError, TypeError):
            pass

    # Path (c): hypothesis time gate via resolves_no_earlier_than (date string).
    # Hypothesis-tracked goals carry an ISO date; treat it as the structural expiry.
    rne = goal.get("resolves_no_earlier_than")
    if rne:
        try:
            # rne is a date (YYYY-MM-DD), not full timestamp — promote to midnight
            expires_dt = datetime.fromisoformat(f"{rne}T00:00:00")
            if expires_dt > datetime.now():
                created_str = goal.get("created_at") or goal.get("started")
                try:
                    created = datetime.fromisoformat(str(created_str)) if created_str else datetime.now()
                except (ValueError, TypeError):
                    created = datetime.now()
                key = f"rne:{rne}:{goal.get('id','')}"
                h = hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]
                return {
                    "type": "resource",
                    "external_id": f"hypothesis-gate:{h}",
                    "state_hash": None,
                    "created_at": created.isoformat(timespec="seconds"),
                    "expires_at": expires_dt.isoformat(timespec="seconds"),
                    "synthesized": True,
                }
        except (ValueError, TypeError):
            pass

    return None

# Collective domain stores (world/)
WORLD_ASP_PATH = WORLD_DIR / "aspirations.jsonl"
PIPELINE_PATH = WORLD_DIR / "pipeline.jsonl"
PIPELINE_ARCHIVE_PATH = WORLD_DIR / "pipeline-archive.jsonl"

# Per-agent aspiration queue
AGENT_ASP_PATH = AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None

# Agent identity (used for claim checking AND participant-based goal routing)
AGENT_NAME = AGENT_DIR.name if AGENT_DIR else ""

# Meta-strategies (meta/)
SKILL_QUALITY_PATH = META_DIR / "skill-quality.yaml"

# Per-agent state
DEV_STAGE_PATH = AGENT_DIR / "developmental-stage.yaml" if AGENT_DIR else None
DEV_STAGE_CONFIG_PATH = CONFIG_DIR / "developmental-stage.yaml"

# Single source of truth for goal scoring weights: meta/goal-selection-strategy.yaml
# Seeded by init-meta.sh, editable by the agent during evolution Step 0.7.
# NOTE: exploration_noise is NOT here — its weight is dynamic (epsilon × noise_scale),
# computed at runtime in score_goal(). Do not add it to this dict.
META_GOAL_SELECTION = META_DIR / "goal-selection-strategy.yaml"

# : cap on applications_log entries to prevent unbounded growth.
# Proof-of-concept lane for meta-strategy "when was this applied" telemetry.
_APPLICATIONS_LOG_CAP = 200


def _record_strategy_application(strategy_path, summary):
    """Append a {ts, agent, sid, summary} entry to a meta-strategy's applications_log.

    g-304-09: Surfaces "when this meta-strategy was applied" — a flat strategy
    YAML alone tells you the parameters but not whether anything ever consumed
    them. Log is FIFO-capped at _APPLICATIONS_LOG_CAP to prevent unbounded
    growth. Fail-open: any error logs to stderr but does NOT crash the caller.
    """
    agent = os.environ.get("MIND_AGENT", "unknown")
    sid = os.environ.get("MIND_SID", "unknown")
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "sid": (sid[:8] if isinstance(sid, str) and sid else "unknown"),
        "summary": summary,
    }
    def _mut(data):
        if not isinstance(data, dict):
            data = {}
        log = data.get("applications_log")
        if not isinstance(log, list):
            log = []
        log.append(entry)
        if len(log) > _APPLICATIONS_LOG_CAP:
            log = log[-_APPLICATIONS_LOG_CAP:]
        data["applications_log"] = log
        return data
    try:
        locked_modify_yaml(strategy_path, _mut)
    except Exception as e:
        print(f"[goal-selector] applications_log append failed: {e}",
              file=sys.stderr)


def load_weights():
    """Load goal selection weights from meta/goal-selection-strategy.yaml."""
    with open(META_GOAL_SELECTION, encoding="utf-8") as f:
        meta = yaml.safe_load(f)
    raw = meta["weights"]
    return {k: max(0.0, min(3.0, float(v))) for k, v in raw.items()}


WEIGHTS = load_weights()
# handoff_bonus: raw value IS the bonus (not scaled). Configured in
# meta/goal-selection-strategy.yaml like every other weight — no setdefault
# fallback (, rb-215 single-source-of-truth).

# Override-key prefix for aspirations.yaml — see world/conventions/capability-routing.md
# "Currently wired readers" list. Keep in sync with _OVERRIDE_FILE_PREFIX in tree.py.
_OVERRIDE_FILE_PREFIX = "aspirations."


def load_recurring_config():
    """Load recurring goal scoring params from core/config/aspirations.yaml.

    Routes through _config_overlay.merged_config so meta/config-overrides.yaml
    entries keyed `aspirations.recurring.*` take effect. Wires 7 params:
    urgency_base, urgency_log_scale, saturation_window, saturation_max_penalty,
    debt_threshold, debt_bonus, streak_mult (g-115-123, rb-335 — streak_mult
    renamed from streak_reset_multiplier 2026-05-18 / g-115-929 to consolidate
    with the literal variable name used at the two readers in
    aspirations_write.py cmd_complete_by and streak-break-reflector.py).
    """
    defaults = {
        "urgency_base": 1.5, "urgency_log_scale": 1.5,
        "saturation_window": 4, "saturation_max_penalty": 4.0,
        "debt_threshold": 0.80, "debt_bonus": 3.0,
        "streak_mult": 2.0,
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        rc = asp_config.get("recurring", {})
        if isinstance(rc, dict):
            for k, default in defaults.items():
                v = rc.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


RECURRING_CONFIG = load_recurring_config()


def load_per_goal_saturation_config():
    """Load per-goal rapid-repeat suppression config from aspirations.yaml.

    Tranche B 2026-04-20 (rb-390): suppresses the SAME goal_id firing
    multiple times within the recent session-completions window.
    Distinct from RECURRING_CONFIG['saturation_*'] which is class-level.
    """
    defaults = {
        "window_size": 8,
        "consecutive_threshold": 1,
        "suppress_penalty": -5.0,
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        pgs = asp_config.get("per_goal_saturation", {})
        if isinstance(pgs, dict):
            for k, default in defaults.items():
                v = pgs.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


PER_GOAL_SATURATION_CONFIG = load_per_goal_saturation_config()


def load_user_signal_boost_config():
    """Load user-signal boost params from core/config/aspirations.yaml.

    Tranche C 2026-04-20 (rb-390): reweights goals based on fresh user-signal
    evidence collected by the signal-refresh hook
    (<agent>/session/user-signal-snapshot.yaml).

    Natural gate: snapshot existence. No separate `enabled` flag — if the
    snapshot is missing or empty, every boost path evaluates to zero.
    """
    defaults = {
        "reply_boost": 2.5,           # user_signal_kind == "reply" on this thread
        "directive_boost": 3.0,       # user_signal_kind == "directive"
        "silence_48h_boost": 1.5,     # pending-question silent ≥48h — surface the ask
        "override_penalty": -2.0,     # user override superseded this thread
        "thread_active_boost": 1.0,   # user_thread_id appears in any recent-activity list
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        # Block name matches the scorer criterion (user_signal_boost) —
        # keep these two in sync. DO NOT rename without matching score_goal().
        usb = asp_config.get("user_signal_boost", {})
        if isinstance(usb, dict):
            for k, default in defaults.items():
                v = usb.get(k)
                if v is not None:
                    defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


USER_SIGNAL_BOOST_CONFIG = load_user_signal_boost_config()


def load_user_signal_snapshot():
    """Read <agent>/session/user-signal-snapshot.yaml if present.

    Fail-open: missing file, parse error, or wrong shape → empty dict.
    Called once per goal-selector invocation (one-shot script; the file is
    refreshed per iteration by the signal-refresh hook in aspirations-precheck).
    """
    try:
        snap_path = AGENT_DIR / "session" / "user-signal-snapshot.yaml"
        if not snap_path.exists():
            return {}
        with open(snap_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


USER_SIGNAL_SNAPSHOT = load_user_signal_snapshot()


def load_class_balance_config():
    """Load work-class balance params from core/config/aspirations.yaml.

    Tranche C 2026-04-20 (rb-390): pulls under-represented work_class values
    up when the last-N session completions distribution drifts from the
    configured targets. Addresses the work-mix skew failure mode (framework
    maintenance accumulating gravity unopposed).

    Natural gate: `targets` non-empty. No separate `enabled` flag — an empty
    targets map means the criterion contributes zero.
    """
    defaults = {
        "window_size": 20,
        "max_boost": 2.0,             # cap for under-represented class
        "max_penalty": -2.0,          # cap for over-represented class
        "targets": {},                # {work_class: fraction}; empty → disabled
    }
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", Path(__file__).parent / "_config_overlay.py"
        )
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp_config = overlay.merged_config("aspirations.yaml")
        cb = asp_config.get("class_balance", {})
        if isinstance(cb, dict):
            for k, default in defaults.items():
                v = cb.get(k)
                if v is not None:
                    if isinstance(default, dict):
                        defaults[k] = v if isinstance(v, dict) else {}
                    else:
                        defaults[k] = type(default)(v)
    except Exception:
        pass
    return defaults


CLASS_BALANCE_CONFIG = load_class_balance_config()


def load_agent_role_multipliers():
    """Load per-agent work_class multipliers from meta/goal-selection-strategy.yaml.

    Magic Wand 4 (bravo session-61, 2026-05-07): encodes agent role into goal
    scoring without per-iteration LLM metacognition. Format:
    {agent_name: {work_class: multiplier_float}}. Empty dict on missing key,
    parse error, or wrong shape — fail-open (criterion contributes zero,
    identical to today's scoring).

    Multiplier interpretation: positive = score boost, zero = no effect.
    Values are NOT clamped to [0, 3] like the weights table — they are
    per-class scaling factors meant to live in the [0.0, 2.0] range
    (e.g., 0.3 dampens by 70%, 1.5 boosts by 50%).
    """
    try:
        with open(META_GOAL_SELECTION, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        v = meta.get("agent_role_multipliers", {})
        if not isinstance(v, dict):
            return {}
        return v
    except Exception:
        return {}


AGENT_ROLE_MULTIPLIERS = load_agent_role_multipliers()


def compute_role_affinity(agent_name, goal_class, multipliers):
    """Return the role_affinity raw value for (agent, goal_class).

    Pure function — no module state. Extracted to top level so tests can
    exercise the decision rule without setting up a full score_goal call.

    Returns 0.0 (zero contribution) when:
    - agent_name is empty/None
    - goal_class is None or "unclassified"
    - agent has no entry in multipliers
    - agent's entry is not a dict (corrupt config)
    - goal_class has no entry under that agent
    - the looked-up value cannot be coerced to float
    """
    # "unclassified" is excluded by design — same precedent as criterion
    # 7e (class_balance_bonus, line ~1473): goals with no work_class tag
    # don't participate in class-based scoring at all. Removing this
    # exclusion would attribute role_affinity to backfill-pending goals.
    if not agent_name or not goal_class or goal_class == "unclassified":
        return 0.0
    if not isinstance(multipliers, dict):
        return 0.0
    agent_mults = multipliers.get(agent_name, {})
    if not isinstance(agent_mults, dict):
        return 0.0
    try:
        return float(agent_mults.get(goal_class, 0.0))
    except (TypeError, ValueError):
        return 0.0


def load_handoff_config():
    """Load cross-agent handoff scoring params from core/config/aspirations.yaml.

    handoff_bonus: scoring bonus when goal.handoff_to == current MIND_AGENT.
    handoff_sender_penalty: penalty applied to goals.handoff_to == OTHER agent
      (prevents the sender from taking back their own routed work).
    handoff_aging:
      warn_hours: receiver-side escalating-bonus onset
      escalate_hours: aspirations-precheck board-post + notify age
      sender_decay_hours: sender-side silence-gated decay window
      partner_active_threshold_min: below this (minutes of silence),
        penalty stays full regardless of handoff age
    """
    defaults = {
        "handoff_bonus": 0.30,
        "handoff_sender_penalty": -2.5,
        "warn_hours": 48,
        "escalate_hours": 72,
        "sender_decay_hours": 4,
        "partner_active_threshold_min": 30,
    }
    try:
        with open(CONFIG_DIR / "aspirations.yaml", encoding="utf-8") as f:
            asp_config = yaml.safe_load(f)
        scoring = asp_config.get("scoring", {}) or {}
        if "handoff_bonus" in scoring:
            defaults["handoff_bonus"] = float(scoring["handoff_bonus"])
        if "handoff_sender_penalty" in scoring:
            defaults["handoff_sender_penalty"] = float(scoring["handoff_sender_penalty"])
        aging = asp_config.get("handoff_aging", {}) or {}
        for k in ("warn_hours", "escalate_hours", "sender_decay_hours"):
            if k in aging:
                defaults[k] = int(aging[k])
        if "partner_active_threshold_min" in aging:
            defaults["partner_active_threshold_min"] = int(aging["partner_active_threshold_min"])
    except Exception:
        pass
    return defaults


HANDOFF_CONFIG = load_handoff_config()


# Sentinel for the team-state read cache. None = not yet read this run.
# Any dict (including {}) means the read has already happened — a repeat
# call returns the cached value. Declared BEFORE the function that uses it
# so the module's read order matches its runtime order.
_TEAM_STATE_CACHE = None


def _load_team_state_cached():
    """Read world/team-state.yaml once and cache for the selector run.

    The sender-side handoff penalty decay is gated by partner liveness
    (agent_status.<partner>.last_active). We read the file exactly once per
    selector invocation — not once per scored goal — to avoid per-goal I/O.
    Missing file → {} (legitimate pre-coordination state). Corrupt yaml
    raises — the selector should fail visibly on a malformed team-state,
    not silently score every handoff with the penalty disengaged.
    """
    global _TEAM_STATE_CACHE
    if _TEAM_STATE_CACHE is not None:
        return _TEAM_STATE_CACHE
    path = WORLD_DIR / "team-state.yaml"
    if not path.exists():
        _TEAM_STATE_CACHE = {}
        return _TEAM_STATE_CACHE
    with open(path, "r", encoding="utf-8") as f:
        _TEAM_STATE_CACHE = yaml.safe_load(f) or {}
    return _TEAM_STATE_CACHE


PRIORITY_MAP = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _ensure_list(val, default=None):
    """Normalize a JSONL field that should be a list. Strings become [string].

    Use this for every list-typed JSONL field (blocked_by, participants, tags).
    Raw .get() on these fields will silently iterate characters if the data is
    a string, producing wrong results without any error.
    """
    if val is None:
        return default if default is not None else []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return default if default is not None else []


def _is_agent_eligible(participants, agent_name):
    """Check if current agent can execute a goal based on participants.

    - ["agent"]: any agent (backward compatible wildcard)
    - ["user"]: not eligible
    - ["alpha"]: only alpha
    - ["alpha", "user"]: alpha + user collaborative
    - Empty/default: treated as ["agent"]
    """
    if not participants:
        return True
    if participants == ["user"]:
        return False
    if "agent" in participants:
        return True
    if agent_name and agent_name in participants:
        return True
    # Only specific agent names remain, and we're not one of them
    non_user = [p for p in participants if p != "user"]
    return not non_user  # True only if nothing but "user" entries


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_jsonl(path):
    """Read a JSONL file and return a list of dicts."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                items.append(json.loads(s))
    return items


def read_yaml_file(path):
    """Read a YAML file via PyYAML."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Cross-session class completions ( /  drift fix)
# ---------------------------------------------------------------------------

def load_recent_class_completions(window_size=20):
    """Cross-session sampling window for goal-selector criteria.

    Replaces in-session-only `wm.goals_completed_this_session` (which resets
    every /stop and was structurally blind to cross-session drift) with a
    rolling window drawn from <agent>/journal.jsonl recent completions, cross-
    referenced against world+agent aspirations.jsonl for `work_class` lookup.

    Returns a list shaped like `goals_completed_this_session` entries:
        [{goal_id, aspiration_id, recurring, work_class}, ...]
    in chronological order (oldest first), so consumers can keep using
    `recent[-window:]` slicing semantics unchanged.

    Falls back to the in-session list when:
    - No AGENT_DIR (fresh install)
    - journal.jsonl missing
    - journal yielded zero entries with mappable work_class (fresh agent)

    See alpha/reports/framework-vs-product-drift-2026-05-09.md for motivation
    (g-115-508 finding: 53% framework dominance over 60 sessions vs 25%
    target was invisible to class_balance_bonus because the window reset
    every session).
    """
    if not AGENT_DIR:
        return []

    journal_path = AGENT_DIR / "journal.jsonl"

    def _in_session_fallback():
        try:
            wm = read_wm()
            sc = wm.get("goals_completed_this_session", [])
            return sc if isinstance(sc, list) else []
        except Exception:
            return []

    if not journal_path.exists():
        return _in_session_fallback()

    # Build goal_id → {aspiration_id, recurring, work_class} index from
    # world + agent aspirations. Empty work_class is preserved so callers
    # can distinguish "no entry" from "no work_class tag" (the existing
    # class_balance check filters missing work_class out of the denominator).
    index = {}
    try:
        for src_path in (WORLD_ASP_PATH, AGENT_ASP_PATH):
            if not src_path:
                continue
            for asp in read_jsonl(src_path):
                asp_id = asp.get("id")
                for g in asp.get("goals", []):
                    gid = g.get("id")
                    if not gid:
                        continue
                    index[gid] = {
                        "goal_id": gid,
                        "aspiration_id": asp_id,
                        "recurring": bool(g.get("recurring", False)),
                        "work_class": g.get("work_class") or "",
                    }
    except Exception:
        return _in_session_fallback()

    # Tail-read journal: collect goals_completed entries from latest entries
    # backwards until we have window_size with non-empty work_class.
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return _in_session_fallback()

    completions = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        gids = entry.get("goals_completed") or []
        if not isinstance(gids, list):
            continue
        # Process in reverse (last-completed-first within this entry)
        for gid in reversed(gids):
            if not isinstance(gid, str):
                continue
            info = index.get(gid)
            if not info or not info.get("work_class"):
                # Skip goals without a current work_class lookup —
                # archived/orphaned IDs would dilute the denominator
                continue
            completions.append(info)
            if len(completions) >= window_size:
                break
        if len(completions) >= window_size:
            break

    if not completions:
        return _in_session_fallback()

    # Reverse to chronological (oldest first) so [-N:] slicing semantics match
    return list(reversed(completions))


# ---------------------------------------------------------------------------
# Exploration params
# ---------------------------------------------------------------------------

def load_exploration_params():
    """Load epsilon and noise_scale from developmental stage + config.

    Returns (epsilon, noise_scale) tuple.
    Epsilon from <agent>/developmental-stage.yaml -> exploration.epsilon
    noise_scale from core/config/developmental-stage.yaml -> exploration.noise_scale
    """
    # Read epsilon from mutable state
    dev_state = read_yaml_file(DEV_STAGE_PATH)
    epsilon = 0.85  # default for uninitialized (exploring stage)
    exploration = dev_state.get("exploration", {})
    if isinstance(exploration, dict):
        epsilon = exploration.get("epsilon", 0.85)

    # Read noise_scale from framework config
    dev_config = read_yaml_file(DEV_STAGE_CONFIG_PATH)
    noise_scale = 3.0  # default
    config_exploration = dev_config.get("exploration", {})
    if isinstance(config_exploration, dict):
        noise_scale = config_exploration.get("noise_scale", 3.0)

    return (float(epsilon), float(noise_scale))


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def days_until(date_str):
    """Days until a future date. Negative if past."""
    if not date_str:
        return None
    try:
        return (date.fromisoformat(str(date_str)) - date.today()).days
    except (ValueError, TypeError):
        return None


def days_since(date_str):
    """Days since a past date. Negative if future."""
    if not date_str:
        return None
    try:
        return (date.today() - date.fromisoformat(str(date_str))).days
    except (ValueError, TypeError):
        return None


def hours_since(timestamp_str):
    """Hours since a past timestamp. Handles both YYYY-MM-DD and YYYY-MM-DDTHH:MM:SS.

    For date-only strings (legacy), assumes start of day (00:00:00).
    Returns float hours, or None if unparseable/corrupt.
    Timestamps must be local system time — see core/config/conventions/goal-schemas.md.
    """
    if not timestamp_str:
        return None
    s = str(timestamp_str)
    try:
        if "T" in s:
            past = datetime.fromisoformat(s)
        else:
            past = datetime.combine(date.fromisoformat(s), datetime.min.time())
        delta = datetime.now() - past
        hours = delta.total_seconds() / 3600.0
        # Negative = corrupt timestamp. Return None so callers treat goal as due (fail open).
        if hours < 0:
            return None
        return hours
    except (ValueError, TypeError):
        return None


def get_interval_hours(goal):
    """Get the recurring interval in hours for a goal.

    Reads interval_hours first, falls back to remind_days * 24, defaults to 24.
    """
    if "interval_hours" in goal:
        return goal["interval_hours"]
    if "remind_days" in goal:
        return goal["remind_days"] * 24
    return 24


# ---------------------------------------------------------------------------
# FILTER + COLLECT
# ---------------------------------------------------------------------------

def collect_candidates(aspirations, known_blockers=None, source="world",
                       global_done_ids=None, claim_timeout_hours=None,
                       reallocation_hours=None,
                       abstention_timeout_hours=None,
                       defer_reason_timeout_hours=None,
                       dependency_timeout_hours=None):
    """Return unblocked pending goals from active aspirations (Phase 2 FILTER + COLLECT).

    Args:
        aspirations: list of aspiration dicts
        known_blockers: list of blocker dicts from working memory
        source: "world" or "agent" — tags each candidate with its origin queue
        global_done_ids: set of completed/decomposed goal IDs across ALL aspirations
            (both world and agent). Enables cross-aspiration blocked_by enforcement.
            If None, falls back to per-aspiration done_ids (legacy behavior).
        claim_timeout_hours: hours after which a stale claim is treated as expired.
            If None, claims persist indefinitely (legacy behavior).
        reallocation_hours: hours after which an unclaimed goal with reallocatable=true
            targeted at another agent becomes eligible for any agent.
            If None, reallocation is disabled (legacy behavior).
    """
    today = date.today()
    results = []

    # Build set of skills blocked by infrastructure blockers
    blocked_skills = set()
    blocked_categories = set()
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocked_skills.add(skill)
                for cat in b.get("affected_categories", []):
                    blocked_categories.add(cat)

    for asp in aspirations:
        if asp.get("status") != "active":
            continue

        # Cooldown check
        cooldown = asp.get("cooldown_days", 0)
        if cooldown > 0:
            lw = days_since(asp.get("last_worked"))
            if lw is not None and lw < cooldown:
                continue

        # Use global done_ids if provided (cross-aspiration dependency enforcement),
        # otherwise fall back to per-aspiration scope (legacy behavior).
        if global_done_ids is not None:
            done_ids = global_done_ids
        else:
            done_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") in ("completed", "decomposed")}

        # verification.preconditions come in two forms:
        #   - strings → LLM-evaluated in aspirations-select Phase 2.2
        #   - dicts with "type" → structured predicates, filtered below via
        #     predicate.evaluate_all (fail-fast; selector_skip=True is honored)
        for goal in asp.get("goals", []):
            if goal.get("status") != "pending":
                continue

            # Self-abstention check: skip goals this agent previously abstained from.
            # The other agent sees them normally. (arXiv 2603.28990: voluntary abstention)
            # Expiry: abstentions older than abstention_timeout_hours are ignored (fail-open).
            # If no abstained_at timestamp exists (legacy), abstention expires immediately.
            if goal.get("abstained_by") == AGENT_NAME:
                if abstention_timeout_hours is not None:
                    abstain_age = hours_since(goal.get("abstained_at"))
                    if abstain_age is not None and abstain_age <= abstention_timeout_hours:
                        continue  # Valid abstention — skip
                    # else: expired or no timestamp — fall through (fail-open)
                else:
                    continue  # No expiry configured — legacy behavior

            # Claim check (world goals only): skip goals claimed by another agent.
            # Expiry makes stale claims (older than claim_timeout_hours) fall through
            # so other agents can pick up abandoned work. The actual re-claim is still
            # atomic via aspirations-claim.sh — this only controls VISIBILITY.
            # For recurring goals, claim timeout is capped at 2x interval_hours so that
            # short-interval goals (e.g. 1h email check) don't stay claimed for 4h.
            if source == "world":
                claimed = goal.get("claimed_by")
                if claimed and claimed != AGENT_NAME:
                    if claim_timeout_hours is not None:
                        effective_timeout = claim_timeout_hours
                        if goal.get("recurring"):
                            interval = get_interval_hours(goal)
                            effective_timeout = min(claim_timeout_hours, 2 * interval)
                        claim_age = hours_since(goal.get("claimed_at"))
                        if claim_age is not None and claim_age <= effective_timeout:
                            continue  # Valid claim — skip
                        # else: claim expired or no claimed_at — fall through to include
                    else:
                        continue  # No expiry configured — legacy behavior

            # blocked_by check (dependency timeout: expired blocks fall through)
            unmet_deps = [b for b in _ensure_list(goal.get("blocked_by"))
                          if b not in done_ids]
            if unmet_deps:
                if dependency_timeout_hours is not None:
                    dep_age = hours_since(goal.get("blocked_since"))
                    if dep_age is not None and dep_age <= dependency_timeout_hours:
                        continue  # Valid dependency block — skip
                    # else: expired or no blocked_since — fall through (fail-open)
                else:
                    continue  # No expiry configured — legacy behavior

            # Infrastructure blocker check (skill-based, primary)
            goal_skill = goal.get("skill", "")
            if goal_skill and blocked_skills and goal_skill in blocked_skills:
                continue
            # Category fallback: when skill is null/empty, check goal.category
            if not goal_skill and blocked_categories:
                goal_cat = goal.get("category", "")
                if goal_cat and goal_cat in blocked_categories:
                    continue

            # Recurring time gate (hour-level precision)
            if goal.get("recurring"):
                interval = get_interval_hours(goal)
                la = hours_since(goal.get("lastAchievedAt"))
                if la is not None and la < interval:
                    continue

            # Hypothesis time gate
            rne = goal.get("resolves_no_earlier_than")
            if rne:
                try:
                    if today < date.fromisoformat(str(rne)):
                        continue
                except (ValueError, TypeError):
                    pass

            # Defer reason: textual deferral blocks the goal.
            # Expiry: defer_reason without deferred_until expires after defer_reason_timeout_hours.
            # Goals WITH deferred_until are governed by the time gate below, not this expiry —
            # the structured field is authoritative when present, and falls through here
            # to the time gate which checks the date and clears past-dated entries naturally.
            # If no defer_reason_set_at timestamp (legacy), deferral expires immediately (fail-open).
            # (: prior `else: continue` unconditionally blocked goals with both fields,
            # leaving 5 goals with deferred_until in the past permanently blocked because they
            # never reached the time gate at L678-685.)
            if goal.get("defer_reason"):
                if not goal.get("deferred_until"):
                    # No structural gate — apply expiry logic
                    if defer_reason_timeout_hours is not None:
                        defer_age = hours_since(goal.get("defer_reason_set_at"))
                        if defer_age is not None and defer_age <= defer_reason_timeout_hours:
                            continue  # Valid deferral — skip
                        # else: expired or no timestamp — fall through (fail-open)
                    else:
                        continue  # No expiry configured — defer indefinitely
                # else: has deferred_until — let the time gate below handle it

            # Deferred time gate
            deferred = goal.get("deferred_until")
            if deferred:
                try:
                    dt = datetime.fromisoformat(str(deferred))
                    if datetime.now() < dt:
                        continue  # Not yet time
                except (ValueError, TypeError):
                    pass  # Corrupt value — fail open

            # Structured preconditions (cheap filter; strings stay on the LLM path).
            # SYMMETRY: must be the logical complement of the struct_pc check in
            # collect_blocked. If you change one, change the other.
            # No try/except on the import — predicate is a required sibling module;
            # a broken import is a real bug we want to surface, not silence.
            # NOTE: `goal.get("verification") or {}` (not `goal.get("verification", {})`)
            # because some goals carry an explicit `verification: null` from filings
            # that didn't structure verification — dict.get returns the stored None
            # in that case, not the default. AttributeError on the chained `.get`
            # was the crash observed during selector runs.
            struct_pcs = [p for p in (goal.get("verification") or {}).get("preconditions") or []
                          if isinstance(p, dict) and "type" in p]
            # Magic Wand #4 (alpha session-60, 2026-05-07): fire_when sugar.
            # Recurring goals can carry a single structured fire_when gate
            # that's evaluated alongside preconditions. Same predicate
            # registry — any predicate type works (command_succeeds for
            # infrastructure probes, file_check for data-pending gates,
            # metric_threshold for traffic-driven recurring fires, etc).
            # When fire_when fails, recurring-precondition-sweep.py advances
            # lastAchievedAt so overdue_ratio doesn't run away while the
            # upstream signal is absent.
            fw = goal.get("fire_when")
            if isinstance(fw, dict) and "type" in fw:
                struct_pcs.append(fw)
            if struct_pcs:
                from predicate import evaluate_all as _eval_preconditions
                # NOTE: do NOT name this `results` — outer `results = []` (L644)
                # is the candidate accumulator and L820 appends to it. Function
                # scope means a `results = ...` assignment here would clobber
                # the accumulator and L822 would return the wrong object type.
                # Filed as fresh-eyes-code F-001 (alpha-fec, msg-715), fixed
                # in  — the regression came in with commit 0f42275.
                pc_results = _eval_preconditions(struct_pcs, mode="fail_fast",
                                                 include_skippable=False)
                if any(not r.passed for r in pc_results):
                    continue

            # Agent eligibility check (filters user-only AND other-agent goals)
            participants = _ensure_list(goal.get("participants"), ["agent"])
            if not _is_agent_eligible(participants, AGENT_NAME):
                # Straggler-aware reallocation: if the goal is marked reallocatable
                # and hasn't been claimed by the targeted agent within reallocation_hours,
                # any agent can pick it up. (Distributed Systems Finding 5: dynamic realloc.)
                if (reallocation_hours is not None
                        and goal.get("reallocatable")
                        and not goal.get("claimed_by")):
                    # Check if enough time has passed since goal creation/last status change
                    created = goal.get("created") or asp.get("created")
                    age = hours_since(created)
                    if age is not None and age >= reallocation_hours:
                        pass  # Fall through — goal is reallocatable and overdue
                    else:
                        continue  # Not yet eligible for reallocation
                else:
                    continue  # Not eligible and not reallocatable

            # Intended-agent routing filter (): drop goals routed to a
            # different agent. Routing is a HINT, not a hard refusal — the
            # `intended_agent` field can be null/unset (no preference),
            # "either" (no preference), this agent's own name (route to me),
            # or another agent's name (route away from me). Only the last
            # case drops; the first three pass through. Mirror of the
            # partner-claim filter pattern in aspirations-select.
            #
            # Pairs with the creation-side wire-up in aspirations.py
            # cmd_add_goal () that stamps intended_agent via
            # capability-route-gate.py. The gate suggests "either" on
            # uncertainty, so any genuinely cross-lane goal stays visible
            # to both agents — only goals confidently routed to a specific
            # peer get filtered out here.
            intended_agent = goal.get("intended_agent")
            if (intended_agent
                    and intended_agent != AGENT_NAME
                    and intended_agent != "either"):
                continue

            results.append({"goal": goal, "aspiration": asp, "source": source})

    return results


def collect_cross_agent_candidates(project_root, agent_dir, agent_name,
                                   known_blockers=None,
                                   global_done_ids=None,
                                   claim_timeout_hours=None,
                                   reallocation_hours=None,
                                   abstention_timeout_hours=None,
                                   defer_reason_timeout_hours=None,
                                   dependency_timeout_hours=None):
    """Pull pending goals from sibling agent queues where intended_agent == agent_name.

    Closes the cross-agent stranding gap (g-115-946): the capability-route gate
    stamps intended_agent on newly-filed goals; when that lands in the FILER's
    private queue, the routed-to TARGET never sees the goal because the
    selector previously read only world + own-agent queues. This helper adds
    a third read pass over sibling agent dirs at selection time. Symmetric
    with the cross-agent awareness already present in directive_boost and
    handoff_bonus scoring criteria — preserves "each goal lives in one queue"
    invariant (no migration / copy / push).

    Returns list of candidate dicts shaped exactly like collect_candidates,
    with source="cross-agent:<owner_dir_name>" so downstream completion
    attribution can write back to the owning sibling's queue.

    Strict-match contract: ONLY goals where goal["intended_agent"] == agent_name
    are returned. Goals with intended_agent unset, "either", or another agent's
    name are NOT included (they're either visible via the agent's own queue
    or correctly routed elsewhere).

    Fail-open per sibling: an unreadable aspirations.jsonl, missing dir, or
    permission error on a single sibling skips THAT sibling without aborting
    the sweep. iterdir errors at the project_root level return empty list.
    """
    if project_root is None or agent_dir is None or not agent_name:
        return []
    results = []
    # Phase 2.5.D: agent dirs live under PROJECT_ROOT/agents/. Walk that
    # parent — fall back to no-op if the parent is missing (fresh repo).
    agents_parent = project_root / "agents"
    if not agents_parent.is_dir():
        return []
    try:
        siblings = list(agents_parent.iterdir())
    except Exception:
        return []  # fail-open at parent
    for sib_dir in siblings:
        if not sib_dir.is_dir() or sib_dir == agent_dir:
            continue
        sib_q = sib_dir / "aspirations.jsonl"
        if not sib_q.exists():
            continue  # non-agent dir (no aspirations.jsonl)
        try:
            sib_aspirations = read_jsonl(sib_q)
        except Exception:
            continue  # fail-open per sibling
        # Reuse the full eligibility filter from collect_candidates (status,
        # claims, defers, blocked_by, structured preconditions, participants,
        # intended_agent routing). The intended_agent filter at the end of
        # collect_candidates already drops goals routed to OTHER agents; we
        # post-filter to require exact match (drop "either" and None too,
        # those are not stranded — they're visible via world/own queue or
        # explicitly meant to stay in the filer's hands).
        sib_candidates = collect_candidates(
            sib_aspirations, known_blockers=known_blockers,
            source=f"cross-agent:{sib_dir.name}",
            global_done_ids=global_done_ids,
            claim_timeout_hours=claim_timeout_hours,
            reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours)
        for c in sib_candidates:
            if c.get("goal", {}).get("intended_agent") == agent_name:
                results.append(c)
    return results


# ---------------------------------------------------------------------------
# BLOCKED GOAL DIAGNOSTICS
# ---------------------------------------------------------------------------

def collect_blocked(aspirations, known_blockers=None, global_done_ids=None,
                    defer_reason_timeout_hours=None,
                    dependency_timeout_hours=None):
    """Return blocked goals with reasons (inverse of collect_candidates).

    Checks blocking conditions in priority order (first match = primary reason):
      explicit_status  — goal.status == "blocked"
      infrastructure   — goal.skill in known_blockers affected_skills
      dependency       — blocked_by contains unmet prerequisite IDs
      deferred         — deferred_until is in the future
      hypothesis_gate  — resolves_no_earlier_than is in the future

    Excludes: recurring cooldown (not a real block), user-only goals,
    completed/skipped/expired/decomposed/in-progress goals.
    """
    today = date.today()
    blocked = []

    # Map skill -> blocker info for infrastructure blocks
    blocker_by_skill = {}
    blocker_by_category = {}
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocker_by_skill[skill] = b
                for cat in b.get("affected_categories", []):
                    blocker_by_category[cat] = b

    for asp in aspirations:
        if asp.get("status") != "active":
            continue

        asp_id = asp.get("id", "")
        # Use global done_ids for cross-aspiration dependency resolution (must match
        # collect_candidates — otherwise a goal can appear "unblocked" in selection
        # but "blocked" in diagnostics for the same cross-aspiration dependency).
        if global_done_ids is not None:
            done_ids = global_done_ids
        else:
            done_ids = {g["id"] for g in asp.get("goals", [])
                        if g.get("status") in ("completed", "decomposed")}

        for goal in asp.get("goals", []):
            status = goal.get("status", "")
            goal_id = goal.get("id", "")

            # Skip terminal and in-progress statuses
            if status in SKIP_STATUSES:
                continue

            # Skip ineligible goals (user-only or other-agent)
            if not _is_agent_eligible(_ensure_list(goal.get("participants"), ["agent"]), AGENT_NAME):
                continue

            entry = {
                "goal_id": goal_id,
                "aspiration_id": asp_id,
                "title": goal.get("title", ""),
                "skill": goal.get("skill"),
                "priority": goal.get("priority", asp.get("priority", "MEDIUM")),
                "chain_position": None,
                # blocker_ref (Change 1: typed blocker reference). Present
                # uniformly so the quiescence gate (Change 2) has one read
                # path regardless of block_reason. For deferred blocks this
                # comes from goal.blocker_ref; for infrastructure blocks the
                # known_blockers augmentation below overwrites it with the
                # infra blocker's own blocker_ref. Absence (None) is the
                # signal that disqualifies quiescence.
                # : synth blocker_ref from structured-prefix defer
                # so quiescence accepts what the write-side already accepts.
                "blocker_ref": _synth_blocker_ref_from_structured_defer(goal),
            }

            # Checks 1-5: first match wins. Order matters — higher-level blocks
            # (infrastructure) must precede lower-level (dependency) so chain
            # compression classifies downstream goals correctly.
            # 1. Explicit "blocked" status
            if status == "blocked":
                entry["block_reason"] = "explicit_status"
                entry["block_detail"] = goal.get("block_reason", "No reason given")
                blocked.append(entry)
                continue

            # Only pending goals from here
            if status != "pending":
                continue

            # 2. Infrastructure blocker (skill-based, primary)
            goal_skill = goal.get("skill", "")
            if goal_skill and goal_skill in blocker_by_skill:
                b = blocker_by_skill[goal_skill]
                entry["block_reason"] = "infrastructure"
                entry["block_detail"] = "{skill} blocked: {reason}".format(
                    skill=goal_skill, reason=b.get("reason", "unknown"))
                entry["blocker_id"] = b.get("blocker_id", "")
                # Prefer the known_blockers blocker_ref over the goal's own
                # — infrastructure blocks are owned by the blocker record,
                # not the individual goal. create-blocker.py writes this.
                if b.get("blocker_ref"):
                    entry["blocker_ref"] = b["blocker_ref"]
                blocked.append(entry)
                continue

            # 2b. Infrastructure blocker (category fallback for skill=null goals)
            if not goal_skill:
                goal_cat = goal.get("category", "")
                if goal_cat and goal_cat in blocker_by_category:
                    b = blocker_by_category[goal_cat]
                    entry["block_reason"] = "infrastructure"
                    entry["block_detail"] = "{cat} category blocked: {reason}".format(
                        cat=goal_cat, reason=b.get("reason", "unknown"))
                    entry["blocker_id"] = b.get("blocker_id", "")
                    if b.get("blocker_ref"):
                        entry["blocker_ref"] = b["blocker_ref"]
                    blocked.append(entry)
                    continue

            # 3. Dependency (blocked_by with unmet prerequisites, timeout-aware).
            # SYMMETRY: must be the logical complement of the blocked_by check in
            # collect_candidates. If you change one, change the other.
            unmet = [bid for bid in _ensure_list(goal.get("blocked_by")) if bid not in done_ids]
            if unmet:
                if dependency_timeout_hours is not None:
                    dep_age = hours_since(goal.get("blocked_since"))
                    if dep_age is None or dep_age > dependency_timeout_hours:
                        pass  # Expired — not blocked (fail-open)
                    else:
                        entry["block_reason"] = "dependency"
                        entry["block_detail"] = "Waiting on: {deps}".format(deps=", ".join(unmet))
                        entry["unmet_deps"] = unmet
                        blocked.append(entry)
                        continue
                else:
                    entry["block_reason"] = "dependency"
                    entry["block_detail"] = "Waiting on: {deps}".format(deps=", ".join(unmet))
                    entry["unmet_deps"] = unmet
                    blocked.append(entry)
                    continue

            # 4. Deferred time gate
            deferred = goal.get("deferred_until")
            if deferred:
                try:
                    dt = datetime.fromisoformat(str(deferred))
                    if datetime.now() < dt:
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = "Deferred until {until}: {reason}".format(
                            until=deferred,
                            reason=goal.get("defer_reason", ""))
                        entry["deferred_until"] = str(deferred)
                        blocked.append(entry)
                        continue
                except (ValueError, TypeError):
                    pass

            # 4b. Defer reason (textual — blocks unless expired)
            # Symmetric to collect_eligible's defer_reason check (L664-680).
            # Goals WITH deferred_until are governed by the time gate at L864-877
            # above; this block only handles goals WITHOUT a structural gate.
            # (: prior `else: append blocked + continue` marked goals
            # with both fields as "deferred" even when deferred_until had passed.)
            if goal.get("defer_reason"):
                if not goal.get("deferred_until"):
                    # No structural gate — apply expiry logic
                    if defer_reason_timeout_hours is not None:
                        defer_age = hours_since(goal.get("defer_reason_set_at"))
                        if defer_age is None or defer_age > defer_reason_timeout_hours:
                            pass  # Expired — fall through to candidate pool
                        else:
                            entry["block_reason"] = "deferred"
                            entry["block_detail"] = "Deferred: {reason}".format(
                                reason=goal.get("defer_reason", ""))
                            blocked.append(entry)
                            continue
                    else:
                        entry["block_reason"] = "deferred"
                        entry["block_detail"] = "Deferred: {reason}".format(
                            reason=goal.get("defer_reason", ""))
                        blocked.append(entry)
                        continue
                # else: has deferred_until — handled by time gate at L864-877

            # 5. Hypothesis time gate
            rne = goal.get("resolves_no_earlier_than")
            if rne:
                try:
                    if today < date.fromisoformat(str(rne)):
                        entry["block_reason"] = "hypothesis_gate"
                        entry["block_detail"] = "Not before {date}".format(date=rne)
                        blocked.append(entry)
                        continue
                except (ValueError, TypeError):
                    pass

            # 6. Structured preconditions unmet (SYMMETRY with collect_candidates).
            # NOTE: `goal.get("verification") or {}` defends against goals with
            # explicit `verification: null` — same fix as in collect_candidates.
            struct_pcs = [p for p in (goal.get("verification") or {}).get("preconditions") or []
                          if isinstance(p, dict) and "type" in p]
            # Magic Wand #4 (alpha session-60): fire_when sugar in collect_blocked
            # too — must mirror collect_candidates for the SYMMETRY invariant.
            fw = goal.get("fire_when")
            if isinstance(fw, dict) and "type" in fw:
                struct_pcs.append(fw)
            if struct_pcs:
                from predicate import evaluate_all as _eval_preconditions
                results = _eval_preconditions(struct_pcs, mode="fail_fast",
                                              include_skippable=False)
                failed = [r for r in results if not r.passed]
                if failed:
                    failed_ids = [r.predicate_id or r.type for r in failed]
                    entry["block_reason"] = "precondition_unmet"
                    entry["block_detail"] = "Preconditions unmet: " + ", ".join(failed_ids)
                    entry["precondition_unmet"] = failed_ids
                    blocked.append(entry)
                    continue

            # Recurring cooldown is NOT a block — goal is just "not yet due"

    # Dependency chain compression: mark head vs downstream
    dep_blocked_ids = {e["goal_id"] for e in blocked if e["block_reason"] == "dependency"}
    for entry in blocked:
        if entry["block_reason"] == "dependency":
            # Head = none of its unmet deps are themselves dependency-blocked
            unmet = entry.get("unmet_deps", [])
            if any(u in dep_blocked_ids for u in unmet):
                entry["chain_position"] = "downstream"
            else:
                entry["chain_position"] = "head"

    return blocked


def trace_root_bottleneck(goal_id, goal_map, done_ids, blocker_by_skill, blocker_by_category=None, visited=None):
    """Walk dependency chains to find the ultimate root blocker.

    Returns (root_goal_id, cause_label) tuple.
    Follows blocked_by references recursively until hitting a terminal condition.
    """
    if visited is None:
        visited = set()
    if goal_id in visited:
        return (goal_id, "CYCLE")
    visited.add(goal_id)

    goal = goal_map.get(goal_id)
    if not goal:
        return (goal_id, "UNKNOWN (missing goal)")

    status = goal.get("status", "")

    # Terminal statuses
    if status == "in-progress":
        return (goal_id, "IN PROGRESS")
    if status == "blocked":
        return (goal_id, "BLOCKED (status)")
    if status in ("skipped", "expired"):
        return (goal_id, "DEAD END: prereq {id} {status}".format(id=goal_id, status=status))

    # For pending goals: check unsatisfied deps
    if status == "pending":
        unsatisfied = [b for b in _ensure_list(goal.get("blocked_by")) if b not in done_ids]
        if unsatisfied:
            # Follow first dep only — preserves 1:1 goal→bottleneck invariant
            return trace_root_bottleneck(unsatisfied[0], goal_map, done_ids,
                                         blocker_by_skill, blocker_by_category, visited)

        # No unsatisfied deps — this IS the root. Classify it.
        deferred = goal.get("deferred_until")
        if deferred:
            try:
                dt = datetime.fromisoformat(str(deferred))
                if datetime.now() < dt:
                    return (goal_id, "DEFERRED until {t}".format(t=deferred))
            except (ValueError, TypeError):
                pass

        goal_skill = goal.get("skill", "")
        if goal_skill and goal_skill in blocker_by_skill:
            reason = blocker_by_skill[goal_skill].get("reason", "unknown")
            return (goal_id, "INFRA: {r}".format(r=reason))
        if not goal_skill and blocker_by_category:
            goal_cat = goal.get("category", "")
            if goal_cat and goal_cat in blocker_by_category:
                reason = blocker_by_category[goal_cat].get("reason", "unknown")
                return (goal_id, "INFRA: {cat} — {r}".format(cat=goal_cat, r=reason))

        participants = _ensure_list(goal.get("participants"), ["agent"])
        if not _is_agent_eligible(participants, AGENT_NAME):
            if participants == ["user"]:
                return (goal_id, "NEEDS USER")
            return (goal_id, "OTHER AGENT ({})".format(", ".join(p for p in participants if p != "user")))

        return (goal_id, "READY")

    # Completed/decomposed shouldn't reach here (in done_ids), but handle gracefully
    return (goal_id, "READY")


# ---------------------------------------------------------------------------
# Evidence backing
# ---------------------------------------------------------------------------

def evidence_score(asp, resolved):
    """Compute evidence_backing for an aspiration from resolved hypotheses.

    For each resolved hypothesis relevant to this goal's aspiration:
      earned_confirmed: +2.0, unlucky_corrected: +1.0
      lucky_confirmed: +0.5, deserved_corrected: -1.0
    Normalize by count. 0 if no relevant hypotheses.
    """
    tags = set(_ensure_list(asp.get("tags")))
    hyp_ids = {g.get("hypothesis_id") for g in asp.get("goals", [])
               if g.get("hypothesis_id")}

    relevant = [h for h in resolved
                if h.get("category") in tags or h.get("id") in hyp_ids]
    if not relevant:
        return 0.0

    dual_scores = {
        "earned_confirmed": 2.0, "unlucky_corrected": 1.0,
        "lucky_confirmed": 0.5, "deserved_corrected": -1.0,
    }
    total = 0.0
    for h in relevant:
        ps = h.get("process_score") or {}
        dc = ps.get("dual_classification") if isinstance(ps, dict) else None
        if dc and dc in dual_scores:
            total += dual_scores[dc]
        else:
            # Fallback: use outcome directly
            outcome = h.get("outcome")
            total += 1.0 if outcome == "CONFIRMED" else (-0.5 if outcome == "CORRECTED" else 0)

    return total / len(relevant)


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------

def _resolve_category(goal, asp):
    """Resolve goal category: direct field > suggest from text > aspiration tag.

    Falls back through three strategies:
    1. goal.category if set and not "uncategorized"
    2. category-suggest.py on title+description
    3. First aspiration tag, then "uncategorized"
    """
    cat = goal.get("category")
    if cat and cat != "uncategorized":
        return cat

    # Derive from title+description via category-suggest
    text = "{title}. {desc}".format(
        title=goal.get("title", ""),
        desc=goal.get("description", ""),
    )
    try:
        result = subprocess.run(
            [sys.executable, str(CORE_ROOT / "scripts" / "category-suggest.py"),
             "--text", text, "--top", "1"],
            capture_output=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            matches = json.loads(result.stdout)
            if matches and matches[0].get("score", 0) > 0:
                return matches[0]["key"]
    except Exception:
        pass

    tags = _ensure_list(asp.get("tags"))
    return tags[0] if tags else "uncategorized"


# ---------------------------------------------------------------------------
# Directive Boost (cross-agent priority influence)
# ---------------------------------------------------------------------------

BOARD_COORD_PATH = WORLD_DIR / "board" / "coordination.jsonl"


def load_active_directives():
    """Load active (non-expired) directive messages from the coordination board.

    Returns a list of dicts: [{target_goals: [...], target_categories: [...], weight: float}]
    Parses structured tags from directive messages (see board.md Directive Payload Schema).
    """
    if not BOARD_COORD_PATH.exists():
        return []
    directives = []
    now = datetime.now()
    for msg in read_jsonl(BOARD_COORD_PATH):
        if msg.get("type") != "directive":
            continue
        tags = _ensure_list(msg.get("tags"))
        # Parse expiry
        expires = None
        for tag in tags:
            if tag.startswith("expires:"):
                try:
                    expires = datetime.fromisoformat(tag[8:])
                except (ValueError, TypeError):
                    pass
        if expires and now > expires:
            continue  # Expired
        # Parse weight modifier
        weight = 0.0
        for tag in tags:
            if tag.startswith("weight:"):
                try:
                    weight = float(tag[7:])
                except (ValueError, TypeError):
                    pass
        if weight == 0.0:
            continue  # No weight = no effect
        # Parse targets
        target_goals = []
        target_categories = []
        for tag in tags:
            if tag.startswith("target:"):
                target_goals.append(tag[7:])
            elif tag.startswith("category:"):
                target_categories.append(tag[9:])
        if not target_goals and not target_categories:
            continue  # No targets = no effect
        directives.append({
            "target_goals": target_goals,
            "target_categories": target_categories,
            "weight": weight,
        })
    return directives


# Cache directives for the duration of a single selector run.
# Safe without cleanup: each goal-selector.py invocation is a separate process.
_ACTIVE_DIRECTIVES = None


def _get_directives():
    global _ACTIVE_DIRECTIVES
    if _ACTIVE_DIRECTIVES is None:
        _ACTIVE_DIRECTIVES = load_active_directives()
    return _ACTIVE_DIRECTIVES


def directive_boost_score(goal_id, category):
    """Compute directive boost for a goal based on active directives."""
    boost = 0.0
    for d in _get_directives():
        if goal_id in d["target_goals"]:
            boost += d["weight"]
        elif category in d["target_categories"]:
            boost += d["weight"]
    return boost


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_goal(cand, wm, resolved, session_completions, epsilon=0.85, noise_scale=3.0):
    """Score a single goal using the 15-criteria weighted formula."""
    goal, asp, source = cand["goal"], cand["aspiration"], cand.get("source", "world")
    raw = {}

    # 1. priority (HIGH=3, MEDIUM=2, LOW=1)
    raw["priority"] = PRIORITY_MAP.get(
        goal.get("priority", asp.get("priority", "MEDIUM")), 2)

    # 2. deadline_urgency (+3 ≤1d, +2 ≤3d, +1 ≤7d)
    deadline = goal.get("resolves_by") or goal.get("deadline")
    remaining = days_until(deadline)
    raw["deadline_urgency"] = (
        3 if remaining is not None and remaining <= 1 else
        2 if remaining is not None and remaining <= 3 else
        1 if remaining is not None and remaining <= 7 else 0)

    # 3. agent_executable (+2 if current agent is eligible)
    participants = _ensure_list(goal.get("participants"), ["agent"])
    raw["agent_executable"] = 2 if _is_agent_eligible(participants, AGENT_NAME) else 0

    # 4. variety_bonus (+1.5 if different aspiration than last touched)
    touched = wm.get("aspiration_touched_last", "")
    raw["variety_bonus"] = 1.5 if asp.get("id") != touched else 0

    # 5. streak_momentum (+0.5 if same aspiration had a goal completed this session)
    # Each entry written by aspirations-state-update Step 3: {"goal_id", "aspiration_id", "recurring", "_item_ts"}
    asp_id = asp.get("id", "")
    raw["streak_momentum"] = (
        0.5 if any(s.get("aspiration_id") == asp_id for s in session_completions) else 0)

    # 6. novelty_bonus (+1.0 if never done before)
    raw["novelty_bonus"] = 1.0 if goal.get("achievedCount", 0) == 0 else 0

    # 7. recurring_urgency (log-scaled: base + log2(1 + overdue_ratio) * scale)
    # Logarithmic scaling preserves differentiation among overdue goals — a 72x-overdue
    # goal scores higher than a 4x-overdue one, unlike the old linear cap at 5.0.
    rec = 0
    if goal.get("recurring"):
        interval = get_interval_hours(goal)
        la = hours_since(goal.get("lastAchievedAt"))
        if la is None or (la >= interval and interval > 0):
            overdue_ratio = 0.0
            if la is not None and interval > 0:
                overdue_ratio = max((la - interval) / interval, 0.0)
            rec = RECURRING_CONFIG["urgency_base"] + math.log2(1 + overdue_ratio) * RECURRING_CONFIG["urgency_log_scale"]
    raw["recurring_urgency"] = rec

    # 7b. recurring_saturation (penalty when recurring goals dominate recent selections)
    # Uses goals_completed_this_session from working memory. Each entry has an optional
    # "recurring" flag (defaults to False for backward compat with older entries).
    # Truly overdue recurring goals overcome this via high recurring_urgency.
    rec_sat = 0.0
    if goal.get("recurring") and session_completions:
        window = int(RECURRING_CONFIG["saturation_window"])
        recent = session_completions[-window:]
        recurring_count = sum(1 for s in recent if s.get("recurring", False))
        ratio = recurring_count / len(recent)
        rec_sat = -(ratio * RECURRING_CONFIG["saturation_max_penalty"])
    raw["recurring_saturation"] = rec_sat

    # 7c. per_goal_saturation (penalty when the SAME goal_id fires rapidly)
    # Tranche B 2026-04-20 (rb-390). Addresses the " fired 4× in 2 min"
    # failure mode the recurring.saturation_* block above cannot see — that one
    # measures the CLASS ratio (any recurring goal in window), not the specific
    # goal_id. This criterion suppresses rapid re-selection of the exact same
    # goal by counting matches in the recent completions window and applying
    # a flat penalty once the threshold is reached. Count-based rather than
    # wall-clock because session_completions entries carry no timestamp; in
    # practice 4 back-to-back fires == 4 consecutive window entries regardless
    # of elapsed seconds, which is exactly the signal we want.
    pgs = 0.0
    if session_completions:
        window = int(PER_GOAL_SATURATION_CONFIG["window_size"])
        threshold = int(PER_GOAL_SATURATION_CONFIG["consecutive_threshold"])
        recent = session_completions[-window:]
        same_id = sum(1 for s in recent if s.get("goal_id") == goal.get("id"))
        if same_id >= threshold:
            pgs = float(PER_GOAL_SATURATION_CONFIG["suppress_penalty"])
    raw["per_goal_saturation"] = pgs

    # 7d. user_signal_boost (Path B: snapshot-level silent_48h detection only)
    # Path A (per-goal user_signal_kind/user_thread_id) retired 2026-04-24
    # after : fields stayed 0/798 for 6+ days — writer never landed.
    # Re-audit on 2026-05-07 will confirm Path B still contributes. To revert:
    # restore signal_kind/thread_id reads AND ship a scanner that populates
    # them in the same change — reader-without-writer is the failure mode
    # this retire removed. See bravo/reports/-user-signal-boost-decision.md.
    usb_total = 0.0
    if USER_SIGNAL_SNAPSHOT:
        sources = USER_SIGNAL_SNAPSHOT.get("sources", {}) or {}
        goal_id = goal.get("id")
        pq = sources.get("pending_questions", {}) or {}
        silent_ids = pq.get("silent_48h_goal_ids", []) or []
        if goal_id and isinstance(silent_ids, list) and goal_id in silent_ids:
            usb_total += float(USER_SIGNAL_BOOST_CONFIG["silence_48h_boost"])
    raw["user_signal_boost"] = usb_total

    # 7e. class_balance_bonus (pull under-represented work_class up)
    # Tranche C 2026-04-20 (rb-390). Computes the last-N distribution of
    # work_class among session completions; if this goal's class is below
    # its configured target fraction, boost proportionally (capped by
    # max_boost); if above, penalize (capped by max_penalty). Goals with
    # no work_class tag default to "unclassified" and are excluded from
    # both the distribution and the bonus computation.
    # Fail-open: empty distribution, no targets, missing work_class,
    # or disabled config → 0.0.
    cbb = 0.0
    targets = CLASS_BALANCE_CONFIG["targets"] or {}
    goal_class = goal.get("work_class")
    if (
        targets
        and goal_class
        and goal_class != "unclassified"
        and goal_class in targets
        and session_completions
    ):
        window = int(CLASS_BALANCE_CONFIG["window_size"])
        recent = session_completions[-window:]
        # work_class in session_completions entries is optional; missing →
        # excluded from the denominator so classes that aren't being tracked
        # don't dilute the fractions.
        classed = [s.get("work_class") for s in recent if s.get("work_class")]
        if classed:
            count = sum(1 for c in classed if c == goal_class)
            observed_fraction = count / len(classed)
            target_fraction = float(targets.get(goal_class, 0.0))
            # deficit > 0 = under-represented; < 0 = over-represented
            deficit = target_fraction - observed_fraction
            max_boost = float(CLASS_BALANCE_CONFIG["max_boost"])
            max_penalty = float(CLASS_BALANCE_CONFIG["max_penalty"])
            # Linear proportional to deficit, clamped to [max_penalty, max_boost].
            # Deficit range is [-1, +1]; scale = cap at boundary.
            if deficit >= 0:
                cbb = min(max_boost, deficit * max_boost * 2)  # ×2 so deficit=0.5 saturates
            else:
                cbb = max(max_penalty, deficit * abs(max_penalty) * 2)
    raw["class_balance_bonus"] = cbb

    # 7f. role_affinity (per-agent work_class preference)
    # Magic Wand 4 (bravo session-61, 2026-05-07): encodes the agent's role
    # into scoring so PM-shaped goals naturally rank higher for PM agents
    # (bravo) and code-shaped goals naturally rank higher for code agents
    # (alpha). Replaces reliance on per-iteration LLM metacognition to
    # enforce role. Stacks additively with handoff_bonus — a bravo-created
    # goal with handoff_to=alpha gets BOTH bonuses on alpha's selector,
    # reinforcing routing intent. Backward compat: missing AGENT_NAME,
    # missing multipliers config, or missing/unclassified work_class all
    # resolve to multiplier 0.0 → zero contribution → identical to today.
    # Decision rule lives in compute_role_affinity (top-level, testable).
    raw["role_affinity"] = compute_role_affinity(
        AGENT_NAME, goal_class, AGENT_ROLE_MULTIPLIERS
    )

    # 8. reward_history (+1.0 if previous goals in this aspiration had high success)
    completed = sum(1 for g in asp.get("goals", []) if g.get("status") == "completed")
    raw["reward_history"] = 1.0 if completed > 0 else 0

    # 8b. completion_pressure (nonlinear boost for near-complete aspirations)
    # Quadratic: negligible for early aspirations, dominant for near-complete ones
    #   1/15 = 0.01, 7/15 = 0.54, 10/15 = 1.11, 14/15 = 2.18
    active_goals = [g for g in asp.get("goals", [])
                    if g.get("status") not in ABANDONED_STATUSES]
    total_goals = len(active_goals)
    done_goals = sum(1 for g in active_goals if g.get("status") == "completed")
    completion_ratio = done_goals / total_goals if total_goals > 0 else 0
    raw["completion_pressure"] = (completion_ratio ** 2) * 2.5

    # 8c. tail_bonus (surface frontier goals as an aspiration nears completion)
    # Zero below 50% completion. As the tail shrinks, each remaining goal gets
    # a larger share of the pull — final straggler in 14/15 scores ~1.30 raw
    # under the 0.50 threshold. Natural ceiling is ~1.50 (asymptote at
    # remaining=1, ratio→1); do not add a cap, it would mask deliberate future
    # tuning of the 3.0 factor. Threshold lowered from 0.70 to 0.50 to extend
    # consolidation pressure into the mid-tail range (50-70% aspirations).
    if completion_ratio >= 0.50 and total_goals > done_goals:
        remaining = total_goals - done_goals  # guaranteed >= 1 by the guard above
        raw["tail_bonus"] = (completion_ratio - 0.50) / remaining * 3.0
    else:
        raw["tail_bonus"] = 0.0

    # 8d. depth_bonus (reward continuing in same aspiration — counterbalances variety_bonus)
    raw["depth_bonus"] = 1.0 if asp.get("id") == touched else 0

    # 8e. cross_aspiration_support (LifingPolls plan item 2 — 2026-05-08).
    # When a goal declares supports: [asp-id, ...], it advances the named
    # aspirations in addition to its own parent. Boost = sum of supported
    # aspirations' completion_pressure × per_support_weight, capped at +2.0.
    # Soft attribution only — completion still ticks one parent.
    supports = goal.get("supports") or []
    if isinstance(supports, list) and supports:
        per_weight = 0.3
        cap = 2.0
        bonus = 0.0
        for sup_id in supports:
            sup_ratio = _ASP_COMPLETION_RATIOS.get(sup_id, 0.0)
            # Mirror completion_pressure shape: quadratic × 2.5 weight.
            bonus += (sup_ratio ** 2) * 2.5 * per_weight
        raw["cross_aspiration_support"] = round(min(bonus, cap), 3)
    else:
        raw["cross_aspiration_support"] = 0.0

    # 9. evidence_backing (resolved hypothesis support score)
    raw["evidence_backing"] = evidence_score(asp, resolved)

    # 10. deferred_readiness (+1.5 when a deferred goal becomes due)
    dr = 0
    deferred = goal.get("deferred_until")
    if deferred:
        try:
            dt = datetime.fromisoformat(str(deferred))
            if datetime.now() >= dt:
                dr = 1.5
        except (ValueError, TypeError):
            pass
    raw["deferred_readiness"] = dr

    # 11. context_coherence — same-category bonus. Context-pressure agnostic:
    # reusing already-primed category knowledge is always cheaper, regardless
    # of zone. Do not re-introduce zone modulation without profiling evidence.
    last_cat = wm.get("last_goal_category", "")
    category = _resolve_category(goal, asp)
    raw["context_coherence"] = 2.0 if category and last_cat and category == last_cat else 0

    # 12. skill_affinity (quality-weighted skill preference)
    # Reads meta/skill-quality.yaml for aggregate quality of the goal's linked skill.
    # High-quality skills get a boost; low-quality skills get a penalty.
    # Goals with no skill or unevaluated skills get neutral 0.
    skill = goal.get("skill", "")
    skill_name = skill.strip("/").split()[0] if skill else ""
    skill_quality_data = read_yaml_file(SKILL_QUALITY_PATH)
    sq_skills = skill_quality_data.get("skills", {})
    sq_entry = sq_skills.get(skill_name, {})
    sq_aggregate = sq_entry.get("aggregate", {})
    sq_overall = sq_aggregate.get("overall", 0.5)  # default neutral
    raw["skill_affinity"] = (sq_overall - 0.5) * 2  # maps [0,1] to [-1, +1]

    # 13b. directive_boost (cross-agent priority influence from board directives)
    raw["directive_boost"] = directive_boost_score(
        goal.get("id", ""), category)

    # 13c. handoff_bonus (cross-agent handoff routing).
    # A planning/reviewer agent files implementer-targeted goals via
    # handoff_to="<target-agent>". The target's selector applies a positive
    # bonus; the sender's selector applies a penalty so those goals don't
    # loop back. Aging boost fires after warn_hours so stale handoffs
    # surface naturally.
    # NOTE: handoff_to is a ROUTING hint, not a visibility gate. Participants
    # still controls who CAN see the goal (see goal-schemas.md). raw value IS
    # the bonus (WEIGHTS["handoff_bonus"] = 1.0 — no scaling).
    raw["handoff_bonus"] = 0.0
    ht = goal.get("handoff_to")
    if ht:
        if ht == AGENT_NAME:
            raw["handoff_bonus"] = HANDOFF_CONFIG["handoff_bonus"]
            ch = goal.get("handoff_created_at")
            age = hours_since(ch) if ch else None
            if age is not None and age > HANDOFF_CONFIG["warn_hours"]:
                # Escalating bonus: +0.10 per 48h of age, capped at 2x (0.20 total addition)
                raw["handoff_bonus"] += min(age / 48, 2.0) * 0.10
        else:
            # Other agent's handoff — partner-liveness-gated penalty.
            # Heavy (handoff_sender_penalty, default -2.5) when the routed
            # partner is demonstrably alive. Decays linearly to 0 over
            # sender_decay_hours (default 4h) of measured PARTNER SILENCE —
            # NOT of elapsed wall-clock handoff age.
            #
            # Why the decay clock is silence, not handoff-age: a 3-day-old
            # handoff where bravo is still actively running means bravo is
            # working on it. The old pure-age decay let alpha take back
            # bravo's fresh work after 72h just because time passed. This
            # rebase makes the penalty derivable from observable state —
            # rb-324 (brittle tuned weight → derivable state, 2026-04-19).
            #
            # History: see rb-284 +  for why fresh penalty had to
            # be ≤-2.5 in the first place (lower values let priority +
            # recurring_urgency still rank other-agent handoffs #1).
            base = HANDOFF_CONFIG["handoff_sender_penalty"]
            decay_h = HANDOFF_CONFIG["sender_decay_hours"]
            active_floor_min = HANDOFF_CONFIG["partner_active_threshold_min"]

            team = _load_team_state_cached()
            partner_status = (team.get("agent_status") or {}).get(ht) or {}
            silence_h = hours_since(partner_status.get("last_active"))
            handoff_age_h = hours_since(goal.get("handoff_created_at"))

            # FAIL-OPEN on missing inputs: the sender penalty is a behavioral
            # control that requires BOTH partner silence AND handoff age to
            # compute. If either input is missing/corrupt, we can't derive
            # the decision from observable state — so the control does not
            # engage (penalty = 0). No fake-protection, no protective guess.
            # The active-partner branch below is NOT a fallback — it is the
            # feature working correctly when both inputs are valid.
            if silence_h is None or silence_h < 0 or handoff_age_h is None or handoff_age_h < 0:
                # INVARIANT — DO NOT REPLACE WITH `base`.
                # Fail-open is the spec: without both inputs we cannot
                # derive the penalty from observed state, so the control
                # disengages. Restoring the full penalty here would bring
                # back the brittle tuned-weight behavior rb-324 retired.
                raw["handoff_bonus"] = 0.0
            elif silence_h * 60 < active_floor_min:
                # Partner wrote to team-state within the active floor — alive.
                # Full base penalty protects them from take-back.
                raw["handoff_bonus"] = base
            else:
                # INVARIANT — DO NOT REMOVE THE min() CLAMP.
                # Decay cannot exceed handoff age. Without this clamp, a
                # long-silent partner sending a fresh handoff would let the
                # sender immediately take it back (silence_h >> age_h).
                # The clamp encodes "decay only counts silence that overlaps
                # with the handoff's own lifetime."
                effective_h = min(silence_h, handoff_age_h)

                if decay_h <= 0 or effective_h >= decay_h:
                    raw["handoff_bonus"] = 0.0
                else:
                    raw["handoff_bonus"] = base * (1.0 - effective_h / decay_h)

    # 13d. co_invest_alignment (): pair-iteration bias.
    # Bonus when this candidate's co_parent_id matches a partner's live
    # team-state in_flight.co_parent_id — biases the selector toward "pair
    # on the same parent right now." Schema: see core/config/conventions/
    # coordination.md → Co-Investigation Protocol. Reads use the cached
    # team-state; missing/empty fields produce 0.0 (no bonus).
    raw["co_invest_alignment"] = 0.0
    candidate_cpi = goal.get("co_parent_id")
    if candidate_cpi:
        team = _load_team_state_cached()
        agent_status = team.get("agent_status", {}) or {}
        for other_name, other_state in agent_status.items():
            if other_name == AGENT_NAME:
                continue
            in_flight = (other_state or {}).get("in_flight") or {}
            if in_flight.get("co_parent_id") == candidate_cpi:
                raw["co_invest_alignment"] = 1.0
                break

    # 14. exploration_noise (random value scaled by developmental epsilon)
    raw["exploration_noise"] = random.random()

    # Weighted total — static criteria + dynamic exploration noise
    total = sum(raw[k] * WEIGHTS[k] for k in WEIGHTS)
    noise_weight = epsilon * noise_scale
    total += raw["exploration_noise"] * noise_weight

    return {
        "goal_id": goal.get("id"),
        "aspiration_id": asp_id,
        "source": source,
        "title": goal.get("title", ""),
        "skill": goal.get("skill"),
        "category": category,
        "recurring": bool(goal.get("recurring")),
        "score": round(total, 2),
        "breakdown": {
            **{k: round(raw[k] * WEIGHTS[k], 2) for k in WEIGHTS},
            "exploration_noise": round(raw["exploration_noise"] * noise_weight, 2),
        },
        "raw": {k: round(v, 2) if isinstance(v, float) else v for k, v in raw.items()},
        "exploration_params": {
            "epsilon": epsilon,
            "noise_scale": noise_scale,
            "noise_weight": round(noise_weight, 2),
        },
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_select(args):
    """Score and rank all unblocked goals from both world and agent queues.

    Output: JSON array sorted by score desc, each entry tagged with source.
    """
    # Read from both aspiration queues
    world_aspirations = read_jsonl(WORLD_ASP_PATH)
    agent_aspirations = read_jsonl(AGENT_ASP_PATH) if AGENT_ASP_PATH else []

    if not world_aspirations and not agent_aspirations:
        print("[]")
        return

    # Load resolved hypotheses for evidence_backing
    pipeline = read_jsonl(PIPELINE_PATH)
    archive = read_jsonl(PIPELINE_ARCHIVE_PATH)
    resolved = [r for r in pipeline + archive
                if r.get("outcome") in ("CONFIRMED", "CORRECTED")]

    # Load working memory for variety/streak context
    wm = read_wm()
    # Cross-session sampling window ( /  drift fix).
    # Replaces in-session-only `wm.goals_completed_this_session` (which reset
    # every /stop) with a rolling window from journal + aspirations.
    # See alpha/reports/framework-vs-product-drift-2026-05-09.md.
    cb_window = int(CLASS_BALANCE_CONFIG.get("window_size", 20)) if CLASS_BALANCE_CONFIG else 20
    sc = load_recent_class_completions(window_size=cb_window)
    if not isinstance(sc, list):
        sc = []

    known_blockers = wm.get("slots", {}).get("known_blockers", [])
    if not isinstance(known_blockers, list):
        known_blockers = []

    # Build global done_ids across ALL aspirations for cross-aspiration dependency enforcement.
    # Without this, blocked_by references to goals in other aspirations are silently ignored.
    # (Mirrors the global goal_map approach already used by collect_blocked/trace_root_bottleneck.)
    all_aspirations = world_aspirations + agent_aspirations
    global_done_ids = set()
    for asp in all_aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") in ("completed", "decomposed"):
                global_done_ids.add(g["id"])

    # Load multi-agent coordination config from aspirations.yaml
    claim_timeout_hours = None
    reallocation_hours = None
    abstention_timeout_hours = None
    defer_reason_timeout_hours = None
    dependency_timeout_hours = None
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        ma = asp_config.get("multi_agent", {})
        if isinstance(ma, dict):
            cth = ma.get("claim_timeout_hours")
            if cth is not None:
                claim_timeout_hours = float(cth)
            rh = ma.get("reallocation_hours")
            if rh is not None:
                reallocation_hours = float(rh)
            ath = ma.get("abstention_timeout_hours")
            if ath is not None:
                abstention_timeout_hours = float(ath)
            drth = ma.get("defer_reason_timeout_hours")
            if drth is not None:
                defer_reason_timeout_hours = float(drth)
            dth = ma.get("dependency_timeout_hours")
            if dth is not None:
                dependency_timeout_hours = float(dth)
    except Exception:
        pass

    # Collect candidates from both queues
    candidates = collect_candidates(
        world_aspirations, known_blockers=known_blockers, source="world",
        global_done_ids=global_done_ids, claim_timeout_hours=claim_timeout_hours,
        reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours,
        dependency_timeout_hours=dependency_timeout_hours)
    candidates += collect_candidates(
        agent_aspirations, known_blockers=known_blockers, source="agent",
        global_done_ids=global_done_ids, reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours,
        dependency_timeout_hours=dependency_timeout_hours)
    #  — cross-agent stranding fix. Pull goals from sibling agent
    # queues where intended_agent matches AGENT_NAME. Catches goals filed by
    # another agent (capability-route gate stamps intended_agent on add) that
    # landed in the FILER's private queue and were invisible to the TARGET.
    # Strict-match contract: only intended_agent == AGENT_NAME pulls; "either"
    # and unset stay in their owner's queue. Fail-open per sibling.
    if AGENT_DIR is not None:
        candidates += collect_cross_agent_candidates(
            AGENT_DIR.parent, AGENT_DIR, AGENT_NAME,
            known_blockers=known_blockers,
            global_done_ids=global_done_ids,
            claim_timeout_hours=claim_timeout_hours,
            reallocation_hours=reallocation_hours,
            abstention_timeout_hours=abstention_timeout_hours,
            defer_reason_timeout_hours=defer_reason_timeout_hours,
            dependency_timeout_hours=dependency_timeout_hours)
    if not candidates:
        # Distinguish "no goals exist" from "goals exist but all blocked"
        # (all_aspirations already computed above for global_done_ids)
        blocked = collect_blocked(all_aspirations, known_blockers=known_blockers,
                                  global_done_ids=global_done_ids,
                                  defer_reason_timeout_hours=defer_reason_timeout_hours,
                                  dependency_timeout_hours=dependency_timeout_hours)
        if blocked:
            summary = {}
            for b in blocked:
                reason = b["block_reason"]
                summary[reason] = summary.get(reason, 0) + 1
            print(json.dumps({
                "candidates": [],
                "all_blocked": True,
                "blocked_count": len(blocked),
                "by_reason": summary,
                "blocked_goals": [
                    {"goal_id": b["goal_id"], "title": b["title"],
                     "reason": b["block_reason"],
                     "detail": b.get("block_detail", "")}
                    for b in blocked[:10]
                ]
            }, indent=2))
        else:
            print("[]")
        return

    epsilon, noise_scale = load_exploration_params()
    # Precompute completion ratios for cross_aspiration_support criterion.
    # Origin: LifingPolls plan item 2 (2026-05-08). Builds a dict of
    # {asp_id: completion_ratio} so score_goal can read it without
    # re-walking aspirations per candidate.
    global _ASP_COMPLETION_RATIOS
    _ASP_COMPLETION_RATIOS = {}
    for asp in all_aspirations:
        active_g = [g for g in asp.get("goals", [])
                    if g.get("status") not in ABANDONED_STATUSES]
        total = len(active_g)
        done = sum(1 for g in active_g if g.get("status") == "completed")
        _ASP_COMPLETION_RATIOS[asp.get("id", "")] = (
            done / total if total > 0 else 0.0)
    scored = [score_goal(c, wm, resolved, sc, epsilon=epsilon, noise_scale=noise_scale)
              for c in candidates]

    # Recurring debt recovery: when most candidates are recurring (agent recovering from
    # a gap), boost non-recurring goals so real work gets oxygen during catch-up.
    recurring_count = sum(1 for s in scored if s.get("recurring"))
    debt_threshold = RECURRING_CONFIG["debt_threshold"]
    if len(scored) >= 5 and recurring_count / len(scored) > debt_threshold:
        bonus = RECURRING_CONFIG["debt_bonus"]
        for s in scored:
            if not s.get("recurring"):
                s["score"] += bonus
                s["breakdown"]["recurring_debt_bonus"] = bonus
                s["raw"]["recurring_debt_bonus"] = bonus

    # Sort: highest score first, then lower aspiration number, then lower goal number
    scored.sort(key=lambda x: (-x["score"], x["aspiration_id"], x["goal_id"]))

    # : log meta-strategy application. Proof-of-concept that the
    # goal-selection-strategy.yaml WAS consulted during this iteration —
    # surfaces "which meta-strategies actually fire" telemetry that's
    # invisible from the strategy file alone. Other meta-strategy consumers
    # (reflection-strategy, encoding-strategy, etc.) can follow the same
    # pattern: import _record_strategy_application + call near decision point.
    _record_strategy_application(META_GOAL_SELECTION, {
        "skill": "goal-selector.cmd_select",
        "candidates_scored": len(scored),
        "top_score": round(float(scored[0]["score"]), 2) if scored else None,
        "top_goal_id": scored[0]["goal_id"] if scored else None,
    })

    print(json.dumps(scored, indent=2, ensure_ascii=False))


def cmd_blocked(args):
    """List all blocked goals with reasons. Output: JSON with blocked_goals and by_reason."""
    empty_reasons = {r: {"count": 0, "goal_ids": []} for r in
                      ["infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status"]}
    empty_reasons["dependency"]["head_count"] = 0
    empty_reasons["dependency"]["downstream_count"] = 0

    # Load expiry config (same source as cmd_select)
    defer_reason_timeout_hours = None
    dependency_timeout_hours = None
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        ma = asp_config.get("multi_agent", {})
        if isinstance(ma, dict):
            drth = ma.get("defer_reason_timeout_hours")
            if drth is not None:
                defer_reason_timeout_hours = float(drth)
            dth = ma.get("dependency_timeout_hours")
            if dth is not None:
                dependency_timeout_hours = float(dth)
    except Exception:
        pass

    # Read from both aspiration queues
    world_aspirations = read_jsonl(WORLD_ASP_PATH)
    agent_aspirations = read_jsonl(AGENT_ASP_PATH) if AGENT_ASP_PATH else []
    aspirations = world_aspirations + agent_aspirations
    if not aspirations:
        print(json.dumps({"blocked_goals": [], "by_reason": empty_reasons,
            "bottlenecks": [], "summary": {
            "total_blocked": 0, "total_active_goals": 0,
            "bottleneck_count": 0}}, indent=2))
        return

    wm = read_wm()
    known_blockers = wm.get("slots", {}).get("known_blockers", [])
    if not isinstance(known_blockers, list):
        known_blockers = []

    # Build global done_ids for cross-aspiration dependency resolution
    global_done_ids = set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") in ("completed", "decomposed"):
                global_done_ids.add(g["id"])

    blocked = collect_blocked(aspirations, known_blockers=known_blockers,
                              global_done_ids=global_done_ids,
                              defer_reason_timeout_hours=defer_reason_timeout_hours,
                              dependency_timeout_hours=dependency_timeout_hours)

    # Count total non-terminal goals across active aspirations
    total_active = 0
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            if g.get("status") not in ("completed", "skipped", "expired", "decomposed"):
                total_active += 1

    # Group by reason
    reasons = ["infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status"]
    by_reason = {}
    for reason in reasons:
        matches = [e for e in blocked if e["block_reason"] == reason]
        entry = {"count": len(matches), "goal_ids": [e["goal_id"] for e in matches]}
        if reason == "dependency":
            entry["head_count"] = sum(1 for e in matches if e.get("chain_position") == "head")
            entry["downstream_count"] = sum(1 for e in matches if e.get("chain_position") == "downstream")
        by_reason[reason] = entry

    # --- Root bottleneck tracing ---
    # Global goal map + done_ids (NOT per-aspiration) — chains cross aspirations
    goal_map = {}
    all_done_ids = set()
    for asp in aspirations:
        if asp.get("status") != "active":
            continue
        for g in asp.get("goals", []):
            gid = g.get("id", "")
            goal_map[gid] = {
                "status": g.get("status", ""),
                "blocked_by": g.get("blocked_by", []),
                "skill": g.get("skill"),
                "deferred_until": g.get("deferred_until"),
                "participants": g.get("participants"),
                "title": g.get("title", ""),
                "aspiration_id": asp.get("id", ""),
            }
            if g.get("status") in ("completed", "decomposed"):
                all_done_ids.add(gid)

    # Build blocker_by_skill and blocker_by_category for INFRA classification
    blocker_by_skill = {}
    blocker_by_category = {}
    if known_blockers:
        for b in known_blockers:
            if b.get("resolution") is None:
                for skill in b.get("affected_skills", []):
                    blocker_by_skill[skill] = b
                for cat in b.get("affected_categories", []):
                    blocker_by_category[cat] = b

    # Trace root bottleneck for each blocked goal
    for entry in blocked:
        gid = entry["goal_id"]
        if entry["block_reason"] == "dependency":
            # Follow the chain to its root
            root_id, cause = trace_root_bottleneck(
                gid, goal_map, all_done_ids, blocker_by_skill, blocker_by_category)
            entry["root_bottleneck"] = {"goal_id": root_id, "cause": cause}
        else:
            # Non-dependency blocks: root is self, cause from block_detail
            cause_map = {
                "infrastructure": entry.get("block_detail", "INFRA"),
                "deferred": "DEFERRED until {t}".format(
                    t=entry.get("deferred_until", "?")),
                "hypothesis_gate": entry.get("block_detail", "hypothesis gate"),
                "explicit_status": entry.get("block_detail", "explicit block"),
            }
            entry["root_bottleneck"] = {
                "goal_id": gid,
                "cause": cause_map.get(entry["block_reason"], entry["block_reason"]),
            }

    # Group by root bottleneck → build bottlenecks array
    root_groups = {}
    for entry in blocked:
        root_id = entry["root_bottleneck"]["goal_id"]
        if root_id not in root_groups:
            root_info = goal_map.get(root_id, {})
            root_groups[root_id] = {
                "goal_id": root_id,
                "title": root_info.get("title", entry.get("title", "")),
                "aspiration_id": root_info.get("aspiration_id",
                                               entry.get("aspiration_id", "")),
                "cause": entry["root_bottleneck"]["cause"],
                "downstream_ids": [],
                "affected_aspirations": set(),
            }
        group = root_groups[root_id]
        if entry["goal_id"] != root_id:
            group["downstream_ids"].append(entry["goal_id"])
        group["affected_aspirations"].add(entry["aspiration_id"])

    bottlenecks = []
    for root_id, group in root_groups.items():
        bottlenecks.append({
            "goal_id": group["goal_id"],
            "title": group["title"],
            "aspiration_id": group["aspiration_id"],
            "cause": group["cause"],
            "downstream_count": len(group["downstream_ids"]),
            "downstream_ids": group["downstream_ids"],
            "affected_aspirations": sorted(group["affected_aspirations"]),
        })
    bottlenecks.sort(key=lambda b: -b["downstream_count"])

    result = {
        "blocked_goals": blocked,
        "by_reason": by_reason,
        "bottlenecks": bottlenecks,
        "summary": {
            "total_blocked": len(blocked),
            "total_active_goals": total_active,
            "bottleneck_count": len(bottlenecks),
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Goal scoring with exploration noise")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select", help="Score and rank all unblocked goals")
    sub.add_parser("blocked", help="List all blocked goals with reasons")
    args = parser.parse_args()
    {"select": cmd_select, "blocked": cmd_blocked}[args.command](args)


if __name__ == "__main__":
    main()
