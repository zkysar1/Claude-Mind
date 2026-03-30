"""Aspiration Trajectory View — compiles the full learning arc for an aspiration.

Inspired by NVIDIA AVO (arXiv:2603.24517) — gives the agent access to the full
lineage of prior work and scores, enabling trajectory-level reasoning about
progress shape, inflection points, and stagnation.

Usage:
    python aspiration-trajectory.py <asp-id> [asp-id ...]

    Single ID:  outputs a flat JSON trajectory object (backward compatible).
    Multiple IDs: loads shared data once, outputs {"asp-id": trajectory, ...}.

Output: JSON object with trajectory data including:
    - Completed goals in chronological order with learning artifacts
    - Capability level changes over time
    - Inflection points (goals that produced significant learning)
    - Current learning velocity
    - Plateau and diminishing returns detection
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# --- Path setup ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR, CONFIG_DIR

def load_jsonl(path):
    """Load a JSONL file, returning list of dicts."""
    records = []
    p = Path(path)
    if not p.exists():
        return records
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records

def load_yaml(path):
    """Load a YAML file. Crashes if yaml not installed or file is malformed."""
    import yaml
    p = Path(path)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))

def load_config():
    """Load plateau detection config from core/config/aspirations.yaml.

    This is the single source of truth for plateau detection parameters.
    If the config file is missing or malformed, the script crashes — that's
    intentional. Do not add fallback defaults here.
    """
    cfg = load_yaml(CONFIG_DIR / "aspirations.yaml")
    return cfg["plateau_detection"]

def find_aspiration(asp_id, asp_sources):
    """Find aspiration by ID across pre-loaded world and agent sources."""
    for source_records in asp_sources:
        for rec in source_records:
            if rec.get("id") == asp_id:
                return rec
    return None

def get_completed_goals(asp):
    """Extract completed goals sorted by start/completion time."""
    goals = asp.get("goals", [])
    completed = [g for g in goals if g.get("status") == "completed"]
    # Sort by started timestamp if available, else by goal ID sequence number.
    # Two-element tuple: (has_timestamp:0/1, timestamp_or_sequence).
    # Goals with timestamps sort first (by time); goals without sort after (by ID).
    def sort_key(g):
        started = g.get("started")
        if started:
            try:
                return (0, datetime.fromisoformat(started))
            except (ValueError, TypeError):
                pass
        gid = g.get("id", "g-000-99")
        parts = gid.rsplit("-", 1)
        try:
            seq = int(parts[-1])
        except (ValueError, IndexError):
            seq = 99
        return (1, datetime(2000, 1, 1) + timedelta(days=seq))
    completed.sort(key=sort_key)
    return completed

def count_learning_artifacts(goal, reasoning_bank, guardrails, pattern_sigs, tree_data):
    """Count learning artifacts produced by or attributable to a goal."""
    gid = goal.get("id", "")
    cat = goal.get("category", "")
    started = goal.get("started")

    artifacts = {
        "reasoning_bank_entries": 0,
        "guardrails_created": 0,
        "pattern_signatures": 0,
        "tree_nodes_updated": 0,
    }

    # Count reasoning bank entries sourced from this goal
    for rb in reasoning_bank:
        if rb.get("source_goal") == gid:
            artifacts["reasoning_bank_entries"] += 1

    # Count guardrails whose source mentions this goal ID.
    # Only match on goal ID — date-based matching over-counts when
    # multiple goals run the same day (inflates velocity, masks plateaus).
    for g in guardrails:
        source = g.get("source", "")
        if gid and gid in source:
            artifacts["guardrails_created"] += 1

    # Count pattern signatures from this goal
    for ps in pattern_sigs:
        if ps.get("source_goal") == gid:
            artifacts["pattern_signatures"] += 1

    # Approximate tree node updates by category match
    # (exact attribution would require changelog, but category is a good proxy)
    if tree_data and cat:
        node = tree_data.get("nodes", {}).get(cat, {})
        if node:
            last_retrieved = node.get("last_retrieved", "")
            if started and last_retrieved and last_retrieved >= started[:10]:
                artifacts["tree_nodes_updated"] += 1

    return artifacts

def compute_learning_velocity(goal_artifacts, window):
    """Compute learning velocity over the last N goals."""
    if len(goal_artifacts) < window:
        recent = goal_artifacts
    else:
        recent = goal_artifacts[-window:]

    if not recent:
        return 0.0

    total = 0
    for ga in recent:
        a = ga["artifacts"]
        total += (a["reasoning_bank_entries"] + a["guardrails_created"]
                  + a["pattern_signatures"] + a["tree_nodes_updated"])
    return total / len(recent)

def detect_inflection_points(goal_artifacts):
    """Find goals where learning yield jumped significantly."""
    if len(goal_artifacts) < 2:
        return []

    inflections = []
    for i in range(1, len(goal_artifacts)):
        prev = goal_artifacts[i - 1]
        curr = goal_artifacts[i]

        prev_total = sum(prev["artifacts"].values())
        curr_total = sum(curr["artifacts"].values())

        # Inflection = significant jump from low to high
        if curr_total >= 2 and curr_total >= prev_total + 2:
            inflections.append({
                "goal_id": curr["goal_id"],
                "title": curr["title"],
                "index": i,
                "artifacts_before": prev_total,
                "artifacts_at": curr_total,
                "description": f"Learning yield jumped from {prev_total} to {curr_total} artifacts"
            })

    return inflections

def detect_plateau(goal_artifacts, config):
    """Detect if learning velocity has plateaued."""
    window = config.get("velocity_window", 5)
    threshold = config.get("plateau_threshold", 0.2)

    if len(goal_artifacts) < window:
        return False

    velocity = compute_learning_velocity(goal_artifacts, window)
    return velocity < threshold

def detect_diminishing_returns(goal_artifacts, config):
    """Detect if learning yield is declining monotonically."""
    window = config.get("diminishing_returns_window", 5)

    if len(goal_artifacts) < window:
        return False

    recent = goal_artifacts[-window:]
    yields = [sum(ga["artifacts"].values()) for ga in recent]

    # Check monotonic decline (each value <= previous)
    for i in range(1, len(yields)):
        if yields[i] > yields[i - 1]:
            return False
    # Ensure it's actually declining (not just flat at zero)
    return yields[0] > yields[-1]

def load_shared_data():
    """Load data stores shared across all aspirations (load once, use many).

    Returns a dict with config, reasoning_bank, guardrails, pattern_sigs,
    tree_data, and asp_sources (pre-parsed aspiration JSONL records).
    """
    asp_sources = []
    for source_path in [WORLD_DIR / "aspirations.jsonl",
                        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None]:
        if source_path and source_path.exists():
            asp_sources.append(load_jsonl(source_path))
        else:
            asp_sources.append([])
    return {
        "config": load_config(),
        "reasoning_bank": load_jsonl(WORLD_DIR / "reasoning-bank.jsonl"),
        "guardrails": load_jsonl(WORLD_DIR / "guardrails.jsonl"),
        "pattern_sigs": load_jsonl(WORLD_DIR / "pattern-signatures.jsonl"),
        "tree_data": load_yaml(WORLD_DIR / "knowledge" / "tree" / "_tree.yaml"),
        "asp_sources": asp_sources,
    }

def build_trajectory(asp_id, shared=None):
    """Build the full trajectory view for an aspiration.

    Args:
        asp_id: Aspiration ID to build trajectory for.
        shared: Pre-loaded shared data from load_shared_data().
                If None, loads fresh (single-ID backward compat).
    """
    if shared is None:
        shared = load_shared_data()

    asp = find_aspiration(asp_id, asp_sources=shared["asp_sources"])
    if not asp:
        return {"error": f"Aspiration {asp_id} not found"}

    config = shared["config"]
    completed = get_completed_goals(asp)

    reasoning_bank = shared["reasoning_bank"]
    guardrails = shared["guardrails"]
    pattern_sigs = shared["pattern_sigs"]
    tree_data = shared["tree_data"]

    # Build per-goal artifact counts
    goal_artifacts = []
    for g in completed:
        artifacts = count_learning_artifacts(g, reasoning_bank, guardrails,
                                            pattern_sigs, tree_data)
        goal_artifacts.append({
            "goal_id": g.get("id", "unknown"),
            "title": g.get("title", ""),
            "category": g.get("category", ""),
            "started": g.get("started"),
            "priority": g.get("priority", "MEDIUM"),
            "artifacts": artifacts,
            "total_artifacts": sum(artifacts.values()),
        })

    # Compute metrics
    velocity_window = config.get("velocity_window", 5)
    current_velocity = compute_learning_velocity(goal_artifacts, velocity_window)
    inflection_points = detect_inflection_points(goal_artifacts)
    is_plateau = detect_plateau(goal_artifacts, config)
    is_diminishing = detect_diminishing_returns(goal_artifacts, config)

    # Determine primary category (most common across goals)
    cat_counts = {}
    for ga in goal_artifacts:
        c = ga.get("category", "")
        if c:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    primary_category = max(cat_counts, key=cat_counts.get) if cat_counts else ""

    # Goals since last inflection
    if inflection_points:
        last_inflection_idx = inflection_points[-1]["index"]
        goals_since_inflection = len(goal_artifacts) - last_inflection_idx - 1
    else:
        goals_since_inflection = len(goal_artifacts)

    # Build summary
    total_artifacts = sum(ga["total_artifacts"] for ga in goal_artifacts)
    summary = (
        f"{len(completed)} goals completed, {total_artifacts} learning artifacts produced, "
        f"velocity={current_velocity:.2f}/goal over last {velocity_window}"
    )

    return {
        "aspiration_id": asp_id,
        "title": asp.get("title", ""),
        "status": asp.get("status", ""),
        "primary_category": primary_category,
        "completed_goals_count": len(completed),
        "total_goals_count": len(asp.get("goals", [])),
        "summary": summary,
        "goals": goal_artifacts,
        "inflection_points": inflection_points,
        "last_inflection_point": inflection_points[-1] if inflection_points else None,
        "goals_since_inflection": goals_since_inflection,
        "current_velocity": current_velocity,
        "velocity_window": velocity_window,
        "plateau_detected": is_plateau,
        "diminishing_returns": is_diminishing,
        "config": config,
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: aspiration-trajectory.py <asp-id> [asp-id ...]", file=sys.stderr)
        sys.exit(1)

    asp_ids = sys.argv[1:]

    if len(asp_ids) == 1:
        # Single ID — backward-compatible flat JSON object
        result = build_trajectory(asp_ids[0])
        print(json.dumps(result, indent=2, default=str))
    else:
        # Multiple IDs — load shared data once, output keyed object
        shared = load_shared_data()
        results = {}
        for asp_id in asp_ids:
            results[asp_id] = build_trajectory(asp_id, shared=shared)
        print(json.dumps(results, indent=2, default=str))

if __name__ == "__main__":
    main()
