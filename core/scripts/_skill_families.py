"""Family/purpose-based map for the skills registry (.claude/skills/_tree.yaml).

Single source of truth — consumed by skill-tree-restructure.py (writes the
tree) and skill-tree-sync.py (detects drift). Editing this file is the
authorized way to add/move/group skills in the registry.

Design rationale: skills-registry-family-restructure tree node under
system/system-constraints-loop. Flat parent:null structure cannot answer
purpose-based queries ("what handles planning?", "what's in the loop?").
Family parents give agents and humans a navigable shape that scales as
skills grow. Forged skills stay in world/forged-skills.yaml — not
duplicated here.
"""

# Top-level family definitions.
# Each family has: summary (one-line purpose), children (skills + nested families).
# A skill's parent is inferred by reverse-lookup from this map.
FAMILY_MAP = {
    "control": {
        "summary": "User-facing entry-point skills. Mode control and verification.",
        "children": ["start", "stop", "verify-learning"],
    },
    "aspirations-loop": {
        "summary": "Perpetual goal-execution loop. Contains the orchestrator and its phase + edge-case sub-families.",
        "children": ["aspirations", "aspirations-phases", "aspirations-edge-cases"],
    },
    "aspirations-phases": {
        "summary": "Runs every iteration: precheck → select → execute → verify → state-update → spark → learning-gate → consolidate (session-end).",
        "parent": "aspirations-loop",
        "children": [
            "aspirations-precheck",
            "aspirations-select",
            "aspirations-execute",
            "aspirations-verify",
            "aspirations-state-update",
            "aspirations-spark",
            "aspirations-learning-gate",
            "aspirations-consolidate",
        ],
    },
    "aspirations-edge-cases": {
        "summary": "Conditional/cadence-triggered branches: all-blocked, graceful-stop, complete-review, strategic-scan, curate-memory, evolve.",
        "parent": "aspirations-loop",
        "children": [
            "aspirations-all-blocked",
            "aspirations-graceful-stop",
            "aspirations-complete-review",
            "aspirations-strategic-scan",
            "aspirations-curate-memory",
            "aspirations-evolve",
        ],
    },
    "reflection": {
        "summary": "Learning extraction from outcomes: ABC chains, pattern signatures, self-model updates, memory curation, replay.",
        "children": [
            "reflect",
            "reflect-on-outcome",
            "reflect-on-self",
            "reflect-maintain",
            "reflect-tree-update",
            "replay",
        ],
    },
    "planning": {
        "summary": "Work creation and decomposition. Includes meta-planning (forge-skill creates new skills from gaps; seed transplants the framework itself).",
        "children": ["decompose", "create-aspiration", "forge-skill", "seed"],
    },
    "memory": {
        "summary": "Knowledge tree operations, context priming, session-end encoding.",
        "children": ["tree", "tree-reader", "prime", "encode-session"],
    },
    "review-rituals": {
        "summary": "Periodic self-audits at cadence: felt-sense, fresh-eyes (code/program/review/tree), strategic-pulse.",
        "children": [
            "felt-sense-checkin",
            "fresh-eyes-code",
            "fresh-eyes-program",
            "fresh-eyes-review",
            "fresh-eyes-tree",
            "strategic-pulse",
        ],
    },
    "reporting": {
        "summary": "Dashboards and status outputs: completion reports, backlogs, priority queues, open questions.",
        "children": [
            "agent-completion-report",
            "backlog-report",
            "priority-review",
            "open-questions",
        ],
    },
    "system": {
        "summary": "Runtime infrastructure: boot orchestration, user-message routing, curriculum gates.",
        "children": ["boot", "respond", "curriculum-gates"],
    },
    "research": {
        "summary": "External knowledge acquisition (web research) and hypothesis-pipeline review.",
        "children": ["research-topic", "review-hypotheses"],
    },
    "audit": {
        "summary": "Health and correctness checks on framework artifacts.",
        "children": ["verify-learning-staleness"],
    },
}


# --- Derived helpers ---

def all_families():
    """Return the set of family names (parents that aren't leaf skills)."""
    return set(FAMILY_MAP.keys())


def all_classified_skills():
    """Return the set of skill (leaf) names referenced anywhere in FAMILY_MAP.

    Excludes family names themselves.
    """
    families = all_families()
    out = set()
    for fname, finfo in FAMILY_MAP.items():
        for c in finfo.get("children", []):
            if c not in families:
                out.add(c)
    return out


def parent_of(skill_name):
    """Return the immediate parent (family) of a skill. None if not classified."""
    for fname, finfo in FAMILY_MAP.items():
        if skill_name in finfo.get("children", []):
            return fname
    return None


def family_ancestors(family_name):
    """Return the chain of ancestor families up to a top-level family. Empty if no parent."""
    chain = []
    current = FAMILY_MAP.get(family_name, {}).get("parent")
    while current and current in FAMILY_MAP:
        chain.append(current)
        current = FAMILY_MAP[current].get("parent")
    return chain
