#!/usr/bin/env python3
"""Goal scoring and selection with exploration noise.

Implements the scoring formula from aspirations/SKILL.md Goal Selection Algorithm.
The LLM no longer computes scores — this script handles the arithmetic.
The LLM still handles Phase 2.5 (metacognitive assessment) and can override rankings.

Scoring criteria (16 deterministic + 1 stochastic weighted factors):
  priority × 1.0 + deadline_urgency × 1.0 + agent_executable × 0.8
  + variety_bonus × 0.5 + streak_momentum × 0.5 + novelty_bonus × 0.6
  + recurring_urgency × 0.8 + recurring_saturation × 0.8
  + reward_history × 0.5 + completion_pressure × 0.8 + depth_bonus × 0.6
  + evidence_backing × 0.7 + deferred_readiness × 0.6
  + context_coherence × 1.0 + skill_affinity × 0.4 + directive_boost × 1.5
  + exploration_noise × (epsilon × noise_scale)  [dynamic weight]

  context_coherence: +2.0 if same category as last goal (fresh/normal zone),
    +1.0 if same category (tight zone), 0 otherwise.
    Reads context budget from <agent>/session/context-budget.json (written by status line).

  recurring_urgency: 1.5 base when due + overdue_ratio, capped at 5.0
  recurring_saturation: -(ratio * 4.0) penalty when recurring goals dominate recent selections
  deferred_readiness: +1.5 when a deferred goal's time has arrived
  exploration_noise: random(0,1) scaled by developmental epsilon.
    At exploring stage (~0.85 epsilon): noise can reorder rankings.
    At mastering stage (~0.19 epsilon): noise mostly breaks ties.
"""

import argparse
import json
import random
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml  # Required — tree.py already depends on PyYAML

from _paths import WORLD_DIR, AGENT_DIR, META_DIR, CONFIG_DIR, CORE_ROOT
from wm import read_wm, WM_PATH as WORKING_MEMORY_PATH  # noqa: E402

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
BUDGET_PATH = AGENT_DIR / "session" / "context-budget.json" if AGENT_DIR else None

# Single source of truth for goal scoring weights: meta/goal-selection-strategy.yaml
# Seeded by init-meta.sh, editable by the agent during evolution Step 0.7.
# NOTE: exploration_noise is NOT here — its weight is dynamic (epsilon × noise_scale),
# computed at runtime in score_goal(). Do not add it to this dict.
META_GOAL_SELECTION = META_DIR / "goal-selection-strategy.yaml"


def load_weights():
    """Load goal selection weights from meta/goal-selection-strategy.yaml."""
    with open(META_GOAL_SELECTION, encoding="utf-8") as f:
        meta = yaml.safe_load(f)
    raw = meta["weights"]
    return {k: max(0.0, min(3.0, float(v))) for k, v in raw.items()}


WEIGHTS = load_weights()
# Fallback defaults for agents seeded before consolidate-before-expand
WEIGHTS.setdefault("completion_pressure", 0.8)
WEIGHTS.setdefault("depth_bonus", 0.6)
WEIGHTS.setdefault("directive_boost", 1.5)

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


def read_context_budget():
    """Read context budget from status line output file.

    Returns dict with zone and used_pct. Defaults to normal if unavailable.
    The budget file is written by scripts/context-budget-status.py (status line).
    """
    if not BUDGET_PATH.exists():
        return {"zone": "normal", "used_pct": 50}
    try:
        data = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return {"zone": "normal", "used_pct": 50}


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
                       defer_reason_timeout_hours=None):
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

        # Note: verification.preconditions are natural-language conditions
        # evaluated by the LLM in Phase 2 of SKILL.md, not here.
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
            if source == "world":
                claimed = goal.get("claimed_by")
                if claimed and claimed != AGENT_NAME:
                    if claim_timeout_hours is not None:
                        claim_age = hours_since(goal.get("claimed_at"))
                        if claim_age is not None and claim_age <= claim_timeout_hours:
                            continue  # Valid claim — skip
                        # else: claim expired or no claimed_at — fall through to include
                    else:
                        continue  # No expiry configured — legacy behavior

            # blocked_by check
            if any(b not in done_ids for b in _ensure_list(goal.get("blocked_by"))):
                continue

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
            # Goals WITH deferred_until are governed by the time gate below, not this expiry.
            # If no defer_reason_set_at timestamp (legacy), deferral expires immediately (fail-open).
            if goal.get("defer_reason"):
                if not goal.get("deferred_until") and defer_reason_timeout_hours is not None:
                    defer_age = hours_since(goal.get("defer_reason_set_at"))
                    if defer_age is not None and defer_age <= defer_reason_timeout_hours:
                        continue  # Valid deferral — skip
                    # else: expired or no timestamp — fall through (fail-open)
                else:
                    continue  # Has deferred_until (time-gated below) or no expiry configured

            # Deferred time gate
            deferred = goal.get("deferred_until")
            if deferred:
                try:
                    dt = datetime.fromisoformat(str(deferred))
                    if datetime.now() < dt:
                        continue  # Not yet time
                except (ValueError, TypeError):
                    pass  # Corrupt value — fail open

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

            results.append({"goal": goal, "aspiration": asp, "source": source})

    return results


# ---------------------------------------------------------------------------
# BLOCKED GOAL DIAGNOSTICS
# ---------------------------------------------------------------------------

def collect_blocked(aspirations, known_blockers=None, global_done_ids=None,
                    defer_reason_timeout_hours=None):
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
            if status in ("completed", "skipped", "expired", "decomposed", "in-progress"):
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
                    blocked.append(entry)
                    continue

            # 3. Dependency (blocked_by with unmet prerequisites)
            unmet = [bid for bid in _ensure_list(goal.get("blocked_by")) if bid not in done_ids]
            if unmet:
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
            if goal.get("defer_reason"):
                if not goal.get("deferred_until") and defer_reason_timeout_hours is not None:
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

def score_goal(cand, wm, resolved, session_completions, epsilon=0.85, noise_scale=3.0,
               budget=None):
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

    # 7. recurring_urgency (1.5 base when due + overdue ratio, capped at 5.0)
    rec = 0
    if goal.get("recurring"):
        interval = get_interval_hours(goal)
        la = hours_since(goal.get("lastAchievedAt"))
        if la is None or (la >= interval and interval > 0):
            # 1.5 base = "this goal is due now" signal
            # + linear overdue growth
            # Cap at 5.0 prevents indefinite starvation of domain work
            overdue_ratio = 0.0
            if la is not None and interval > 0:
                overdue_ratio = (la - interval) / interval
            rec = min(1.5 + overdue_ratio, 5.0)
    raw["recurring_urgency"] = rec

    # 7b. recurring_saturation (penalty when recurring goals dominate recent selections)
    # Uses goals_completed_this_session from working memory. Each entry has an optional
    # "recurring" flag (defaults to False for backward compat with older entries).
    # Penalty scales from 0 (no saturation) to -4.0 (all recent completions were recurring).
    # Truly overdue recurring goals overcome this via high recurring_urgency.
    rec_sat = 0.0
    if goal.get("recurring") and session_completions:
        window = 4
        recent = session_completions[-window:]
        recurring_count = sum(1 for s in recent if s.get("recurring", False))
        ratio = recurring_count / len(recent)
        rec_sat = -(ratio * 4.0)
    raw["recurring_saturation"] = rec_sat

    # 8. reward_history (+1.0 if previous goals in this aspiration had high success)
    completed = sum(1 for g in asp.get("goals", []) if g.get("status") == "completed")
    raw["reward_history"] = 1.0 if completed > 0 else 0

    # 8b. completion_pressure (nonlinear boost for near-complete aspirations)
    # Quadratic: negligible for early aspirations, dominant for near-complete ones
    #   1/15 = 0.01, 7/15 = 0.54, 10/15 = 1.11, 14/15 = 2.18
    active_goals = [g for g in asp.get("goals", [])
                    if g.get("status") not in ("skipped", "expired", "decomposed")]
    total_goals = len(active_goals)
    done_goals = sum(1 for g in active_goals if g.get("status") == "completed")
    completion_ratio = done_goals / total_goals if total_goals > 0 else 0
    raw["completion_pressure"] = (completion_ratio ** 2) * 2.5

    # 8c. depth_bonus (reward continuing in same aspiration — counterbalances variety_bonus)
    raw["depth_bonus"] = 1.0 if asp.get("id") == touched else 0

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

    # 11. context_coherence (same-category bonus modulated by context budget zone)
    budget = budget or {}
    last_cat = wm.get("last_goal_category", "")
    category = _resolve_category(goal, asp)
    if category and last_cat and category == last_cat:
        raw["context_coherence"] = 2.0 if budget.get("zone") != "tight" else 1.0
    else:
        raw["context_coherence"] = 0

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
    sc = wm.get("goals_completed_this_session", [])
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
    except Exception:
        pass

    # Collect candidates from both queues
    candidates = collect_candidates(
        world_aspirations, known_blockers=known_blockers, source="world",
        global_done_ids=global_done_ids, claim_timeout_hours=claim_timeout_hours,
        reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours)
    candidates += collect_candidates(
        agent_aspirations, known_blockers=known_blockers, source="agent",
        global_done_ids=global_done_ids, reallocation_hours=reallocation_hours,
        abstention_timeout_hours=abstention_timeout_hours,
        defer_reason_timeout_hours=defer_reason_timeout_hours)
    if not candidates:
        # Distinguish "no goals exist" from "goals exist but all blocked"
        # (all_aspirations already computed above for global_done_ids)
        blocked = collect_blocked(all_aspirations, known_blockers=known_blockers,
                                  global_done_ids=global_done_ids,
                                  defer_reason_timeout_hours=defer_reason_timeout_hours)
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
    budget = read_context_budget()
    scored = [score_goal(c, wm, resolved, sc, epsilon=epsilon, noise_scale=noise_scale,
                         budget=budget)
              for c in candidates]
    # Sort: highest score first, then lower aspiration number, then lower goal number
    scored.sort(key=lambda x: (-x["score"], x["aspiration_id"], x["goal_id"]))

    print(json.dumps(scored, indent=2, ensure_ascii=False))


def cmd_blocked(args):
    """List all blocked goals with reasons. Output: JSON with blocked_goals and by_reason."""
    empty_reasons = {r: {"count": 0, "goal_ids": []} for r in
                      ["infrastructure", "dependency", "deferred", "hypothesis_gate", "explicit_status"]}
    empty_reasons["dependency"]["head_count"] = 0
    empty_reasons["dependency"]["downstream_count"] = 0

    # Load expiry config (same source as cmd_select)
    defer_reason_timeout_hours = None
    try:
        asp_config = read_yaml_file(CONFIG_DIR / "aspirations.yaml")
        ma = asp_config.get("multi_agent", {})
        if isinstance(ma, dict):
            drth = ma.get("defer_reason_timeout_hours")
            if drth is not None:
                defer_reason_timeout_hours = float(drth)
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
                              defer_reason_timeout_hours=defer_reason_timeout_hours)

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
