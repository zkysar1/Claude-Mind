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
from datetime import datetime
from pathlib import Path

# --- Path setup ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR, CONFIG_DIR, PROJECT_ROOT
from _long_path import open_long_path

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


def build_tree_attribution_map(tree_dir):
    """Scan tree .md files, extract goal-id attribution from front matter.

    Returns {goal_id: count_of_nodes_attributed}. A node attributes when either
    (a) front matter has a clean source_goal: 'g-NNN-NN' field, or (b) the free-
    text last_update_trigger.source starts with a goal-id (extract the prefix).

    Both schemas exist in world/knowledge/tree/. Files without a goal-attributable
    source (semantic descriptors like 'DECOMPOSE category', 'tree_growth') are
    skipped — those are tree-internal events, not goal-driven encoding.

    rb-601 fix — replaces the broken count_learning_artifacts() category-key
    proxy that read _tree.yaml node.last_retrieved instead of per-file
    last_updated provenance.
    """
    import re
    import yaml

    GOAL_ID_PFX = re.compile(r'^(g-\d+-\d+)\b')
    attribution = {}

    tree_root = Path(tree_dir)
    if not tree_root.is_dir():
        return attribution

    for md_path in tree_root.rglob("*.md"):
        try:
            # rb-450 / : tree nodes under deeply-nested categories can
            # exceed Windows MAX_PATH (260 chars). Path.read_text() doesn't
            # expose the long-path retry; route through open_long_path so
            # attribution counts don't silently drop those files. POSIX no-op.
            with open_long_path(md_path) as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if not text.startswith("---"):
            continue
        end_idx = text.find("\n---", 4)
        if end_idx < 0:
            continue
        try:
            fm = yaml.safe_load(text[4:end_idx]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue

        # Prefer clean source_goal field if present
        sg = fm.get("source_goal")
        if isinstance(sg, str):
            m = GOAL_ID_PFX.match(sg.strip())
            if m:
                gid = m.group(1)
                attribution[gid] = attribution.get(gid, 0) + 1
                continue

        # Fall back: extract leading goal-id from free-text source
        trigger = fm.get("last_update_trigger") or {}
        if not isinstance(trigger, dict):
            continue
        source = trigger.get("source", "")
        if not isinstance(source, str):
            continue
        m = GOAL_ID_PFX.match(source.strip())
        if m:
            gid = m.group(1)
            attribution[gid] = attribution.get(gid, 0) + 1

    return attribution


def build_script_convention_attribution_map():
    """Scan code artifacts (scripts + conventions) for goal-id mentions
    in header/docstring regions. Returns {goal_id: count} where each file
    that mentions a goal-id in its first 4000 chars contributes +1.

    Closes the gap where development goals whose primary deliverable is
    code or convention text receive zero learning-artifact credit from
    count_learning_artifacts (which only counts rb/guard/pattern/tree
    encodings). Without this, aspirations like asp-282 — which produced
    capability-route-gate.py, agent-lanes.md, and the intended_agent
    schema — show velocity=0 and trip precheck-eval cycles detector's
    zero_learning_velocity false-positive (g-115-595 / rb-803 / g-115-596).

    Scan targets (each fail-open on missing dir):
      - core/scripts/*.py, *.sh         framework scripts
      - core/config/conventions/*.md    framework conventions
      - {WORLD_DIR}/scripts/*.sh, *.py  domain scripts
      - {WORLD_DIR}/conventions/*.md    domain conventions

    Attribution rule: each distinct g-NNN-NN matched in the file's first
    4000 chars yields +1 for that goal-id. Files mentioning multiple
    goal-ids attribute +1 to each (a single file authored under g-282-01
    that also cross-references g-282-02 credits both). Multiple files per
    goal-id accumulate. The 4000-char window targets header/docstring
    regions where authorship signals concentrate and ignores random mid-
    body cross-references that are not authorship.
    """
    import re
    pat = re.compile(r"\bg-\d+-\d+\b")
    attribution = {}

    scan_targets = [
        (PROJECT_ROOT / "core" / "scripts", ("*.py", "*.sh")),
        (CONFIG_DIR / "conventions", ("*.md",)),
        (WORLD_DIR / "scripts", ("*.sh", "*.py")),
        (WORLD_DIR / "conventions", ("*.md",)),
    ]

    for root, patterns in scan_targets:
        if not isinstance(root, Path) or not root.is_dir():
            continue
        for glob_pat in patterns:
            for path in root.glob(glob_pat):
                try:
                    text = path.read_text(encoding="utf-8")[:4000]
                except (OSError, UnicodeDecodeError):
                    continue
                for gid in set(pat.findall(text)):
                    attribution[gid] = attribution.get(gid, 0) + 1

    return attribution


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

# Fields carrying a real completion time, most precise first. `completed_at` is
# a full timestamp; `completed_date` is DATE-ONLY, so it is the fallback rather
# than the primary -- several goals routinely close on the same day and the
# consumers below care about their order. `started` is the last resort because a
# goal's claim time is not its completion time, but it is a real instant and so
# still orders correctly against the others far more often than not.
_COMPLETION_TIME_FIELDS = ("completed_at", "completed_date", "started")


def goal_completion_order_key(g):
    """Total order on real completion time ().

    THE BUG THIS REPLACES WAS A PARTITION WEARING AN ORDERING'S CLOTHES. The old
    key was `(0, started)` when `started` existed and `(1, epoch + seq_days)`
    otherwise, and its comment stated the intent as "goals with timestamps sort
    first; goals without sort after" -- which IS the defect, not a description of
    it. Every timestamped goal preceded every un-timestamped one regardless of
    when either actually completed, and the un-timestamped bucket was ordered by
    a FABRICATED date synthesized from the goal-id sequence number.

    That is fatal here specifically because every consumer of this array takes a
    trailing recency slice or walks it pairwise (see get_completed_goals). Since
    the daemon stamps `started` at CLAIM time, every newly-claimed goal joins
    bucket 0 and bucket 1 can never gain a member -- so the "last N goals" window
    was pinned to a frozen historical set that receded further from the present
    with every goal closed. Measured on ZDS asp-025: 96 completed, 58 timestamped
    and 38 not, so the last-5 window was drawn entirely from the un-timestamped
    tail and reported velocity=0.00 for an aspiration that was producing
    artifacts that week.

    THE RESIDUAL PARTITION HERE IS DELIBERATE, MEASURED, AND POINTS THE OTHER
    WAY. A goal carrying none of the three fields has no time information at all,
    so no key can place it honestly; this returns a sentinel that sorts it LAST
    rather than inventing an instant for it. Two things make that the safe
    direction rather than a smaller copy of the same mistake. It covers 5 of 4487
    live completed goals (0.11%) against the old key's 38-of-96 (40%) in the
    measured case. And those 5 are the NEWEST goals, not ancient ones -- the
    stamp is written by the daemon around close, so the unstamped population is
    whatever just closed. Sending them to the far past, which is the reflexive
    choice, would push the freshest work out of the recency window and reproduce
    the exact defect being fixed. Python's sort is stable, so they hold their
    file order among themselves.
    """
    for field in _COMPLETION_TIME_FIELDS:
        raw = g.get(field)
        if not raw:
            continue
        try:
            return (0, datetime.fromisoformat(str(raw)))
        except (ValueError, TypeError):
            continue
    return (1, datetime.min)


def get_completed_goals(asp):
    """Completed goals in true completion order, oldest first.

    ORDER IS LOAD-BEARING FOR FOUR CONSUMERS, so do not weaken this to a
    partition again: compute_learning_velocity and detect_diminishing_returns
    each take a trailing `[-window:]` slice, detect_plateau delegates to the
    former, and detect_inflection_points walks adjacent pairs (a discontinuity
    in the ordering manufactures a spurious jump at the seam). The commissioning
    goal named only the first three; the fourth is why the enumeration is
    written down here (guard-1737).
    """
    goals = asp.get("goals", [])
    completed = [g for g in goals if g.get("status") == "completed"]
    completed.sort(key=goal_completion_order_key)
    return completed

def count_learning_artifacts(goal, reasoning_bank, guardrails, pattern_sigs,
                             tree_data, tree_attribution=None,
                             script_convention_attribution=None):
    """Count learning artifacts produced by or attributable to a goal."""
    gid = goal.get("id", "")

    artifacts = {
        "reasoning_bank_entries": 0,
        "guardrails_created": 0,
        "pattern_signatures": 0,
        "tree_nodes_updated": 0,
        "scripts_conventions_authored": 0,
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

    # Tree-node attribution by goal_id from per-file front matter (rb-601 fix).
    # Prior implementation used goal.category as a tree-node-key proxy AND read
    # _tree.yaml node.last_retrieved (the field that fires on retrieval, not
    # update) — both incorrect, returning 0 for every goal. tree_attribution
    # is built once in load_shared_data() via build_tree_attribution_map().
    if tree_attribution and gid:
        artifacts["tree_nodes_updated"] = tree_attribution.get(gid, 0)

    # Script + convention attribution ( / rb-803).
    # Counts files in core/scripts, world/scripts, core/config/conventions,
    # and world/conventions whose header region mentions this goal-id.
    # Lifts the zero_learning_velocity false-positive on aspirations whose
    # primary deliverable is code or convention text rather than
    # rb/guard/tree entries —  was the canonical incident.
    if script_convention_attribution and gid:
        artifacts["scripts_conventions_authored"] = \
            script_convention_attribution.get(gid, 0)

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
                  + a["pattern_signatures"] + a["tree_nodes_updated"]
                  + a.get("scripts_conventions_authored", 0))
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
        "tree_attribution": build_tree_attribution_map(WORLD_DIR / "knowledge" / "tree"),
        "script_convention_attribution": build_script_convention_attribution_map(),
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
    tree_attribution = shared.get("tree_attribution", {})
    script_convention_attribution = shared.get("script_convention_attribution", {})

    # Build per-goal artifact counts
    goal_artifacts = []
    for g in completed:
        artifacts = count_learning_artifacts(g, reasoning_bank, guardrails,
                                            pattern_sigs, tree_data,
                                            tree_attribution,
                                            script_convention_attribution)
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
    # Record-level exemption (): maintenance-scope queues (recurring
    # upkeep aspirations) legitimately run at ~0 learning velocity — that is
    # their normal operating point, not a stalled learning direction. An
    # aspiration carrying plateau_exempt: true suppresses BOTH flags so evolve
    # Step 1.5 stops re-making the same skip-judgment every cadence pass.
    # Velocity is still computed and reported (informative); only the flags
    # are suppressed. Set via: aspirations-update.sh <asp-id> plateau_exempt true
    # Strict-boolean contract (fresh-eyes finding 2026-07-16): only a real JSON
    # boolean true exempts. A truthy STRING ("False", "no", string-typed "true")
    # keeps detection ON — fail-safe: malformed values stay visible via the
    # flag rather than silently suppressing detection.
    plateau_exempt = asp.get("plateau_exempt") is True
    is_plateau = (not plateau_exempt) and detect_plateau(goal_artifacts, config)
    is_diminishing = (not plateau_exempt) and detect_diminishing_returns(goal_artifacts, config)

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
        "plateau_exempt": plateau_exempt,
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
