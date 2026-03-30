#!/usr/bin/env python3
"""Work alignment check — provides Self-alignment metrics for LLM interpretation.

Reads Self (<agent>/self.md), active aspirations, and goal history to compute
raw metrics. Does NOT make decisions — outputs JSON for the LLM to interpret.

Three core metrics:
  uncovered_priorities: Self priorities not covered by any active aspiration
  hours_since_novel_goal: hours since last first-time goal completion (achievedCount==0→1)
  recurring_ratio: fraction of ranked goals that are recurring
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import WORLD_DIR, AGENT_DIR, CONFIG_DIR

# Per-agent identity
SELF_PATH = AGENT_DIR / "self.md" if AGENT_DIR else None

# Collective domain stores (world/)
ASP_PATH = WORLD_DIR / "aspirations.jsonl"
ASP_ARCHIVE_PATH = WORLD_DIR / "aspirations-archive.jsonl"
CONFIG_PATH = CONFIG_DIR / "aspirations.yaml"


def read_jsonl(path):
    """Read a JSONL file and return a list of dicts."""
    p = Path(path)
    if not p.exists():
        return []
    items = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    items.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
    return items


def read_self():
    """Read <agent>/self.md and extract body content after YAML front matter."""
    if not SELF_PATH.exists():
        return ""
    text = SELF_PATH.read_text(encoding="utf-8")
    # Strip YAML front matter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:].strip()
    return text


def extract_priorities(self_text):
    """Extract priority themes from Self text.

    Looks for numbered lists, section headers, and emphasized phrases
    to identify what Self considers important. Returns a list of
    short priority strings.
    """
    priorities = []

    # Extract numbered items (e.g., "1. **Log analysis across every layer**")
    numbered = re.findall(r'^\d+\.\s+\*\*([^*]+)\*\*', self_text, re.MULTILINE)
    priorities.extend([p.strip() for p in numbered])

    # Extract ## headers as priority themes
    headers = re.findall(r'^##\s+(.+)$', self_text, re.MULTILINE)
    priorities.extend([h.strip() for h in headers])

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in priorities:
        lower = p.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(p)

    return unique


def match_priority(priority, aspirations):
    """Check if a priority is covered by any active aspiration.

    Uses case-insensitive substring matching on aspiration titles,
    descriptions, and goal titles. Returns True if covered.
    """
    priority_lower = priority.lower()
    # Extract key terms (3+ char words)
    terms = [w for w in re.findall(r'\w+', priority_lower) if len(w) >= 3]

    for asp in aspirations:
        asp_text = " ".join([
            asp.get("title", ""),
            asp.get("description", ""),
        ]).lower()
        # Also check goal titles
        for goal in asp.get("goals", []):
            asp_text += " " + goal.get("title", "").lower()

        # Match if at least half of the key terms appear
        if terms:
            matched = sum(1 for t in terms if t in asp_text)
            if matched >= len(terms) * 0.5:
                return True

    return False


def compute_hours_since_novel(aspirations, archive):
    """Find hours since the last first-time goal completion.

    A 'novel' completion is when achievedCount went from 0 to 1
    (non-recurring, or first achievement of a recurring goal).
    """
    latest_novel = None

    for asp in aspirations + archive:
        for goal in asp.get("goals", []):
            achieved = goal.get("achievedCount", 0)
            if achieved == 1 and not goal.get("recurring", False):
                # Non-recurring completed goal — always novel
                completed_at = goal.get("completedAt") or goal.get("lastAchievedAt")
                if completed_at:
                    try:
                        dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                        dt = dt.replace(tzinfo=None)
                        if latest_novel is None or dt > latest_novel:
                            latest_novel = dt
                    except (ValueError, AttributeError):
                        continue
            elif achieved >= 1 and goal.get("recurring", False):
                # Recurring goal — use firstAchievedAt for the novelty timestamp.
                # achievedCount may be >1 now, but the FIRST achievement was the novel event.
                first_at = goal.get("firstAchievedAt") or (goal.get("lastAchievedAt") if achieved == 1 else None)
                if first_at:
                    try:
                        dt = datetime.fromisoformat(first_at.replace("Z", "+00:00"))
                        dt = dt.replace(tzinfo=None)
                        if latest_novel is None or dt > latest_novel:
                            latest_novel = dt
                    except (ValueError, AttributeError):
                        continue

    if latest_novel is None:
        return None  # No novel completions found

    now = datetime.now()
    delta = now - latest_novel
    return round(delta.total_seconds() / 3600, 1)


def compute_category_distribution(aspirations):
    """Count goals per category across active aspirations."""
    dist = {}
    for asp in aspirations:
        for goal in asp.get("goals", []):
            if goal.get("status") in ("completed", "skipped", "expired"):
                continue
            cat = goal.get("category", "uncategorized") or "uncategorized"
            dist[cat] = dist.get(cat, 0) + 1
    return dist


def cmd_check(args):
    """Run alignment check and output JSON metrics."""
    # Read Self
    self_text = read_self()
    if not self_text:
        print(json.dumps({"error": "Self not found at <agent>/self.md"}, indent=2))
        sys.exit(1)

    # Extract priorities from Self
    self_priorities = extract_priorities(self_text)

    # Read active aspirations
    all_aspirations = read_jsonl(ASP_PATH)
    active = [a for a in all_aspirations if a.get("status") == "active"]

    # Read archive for novelty history
    archive = read_jsonl(ASP_ARCHIVE_PATH)

    # Determine covered vs uncovered priorities
    covered = []
    uncovered = []
    for p in self_priorities:
        if match_priority(p, active):
            covered.append(p)
        else:
            uncovered.append(p)

    # Compute hours since novel goal
    hours_since_novel = compute_hours_since_novel(active, archive)

    # Compute recurring ratio from ranked goals (if provided via stdin)
    recurring_ratio = None
    ranked_goals_json = args.ranked_goals
    if ranked_goals_json:
        try:
            ranked = json.loads(ranked_goals_json)
            if ranked:
                recurring_count = sum(1 for g in ranked if g.get("recurring", False))
                recurring_ratio = round(recurring_count / len(ranked), 2)
        except (json.JSONDecodeError, TypeError):
            pass

    # Category distribution
    category_dist = compute_category_distribution(active)

    # Read config thresholds (reference points for LLM)
    thresholds = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        planning = config.get("planning", {})
        thresholds = {
            "novelty_drought_hours": planning.get("novelty_drought_hours", 48),
            "maintenance_threshold": planning.get("maintenance_threshold", 0.70),
            "check_interval_goals": planning.get("check_interval_goals", 3),
        }

    result = {
        "self_priorities": self_priorities,
        "covered_priorities": covered,
        "uncovered_priorities": uncovered,
        "hours_since_novel_goal": hours_since_novel,
        "recurring_ratio": recurring_ratio,
        "active_aspiration_count": len(active),
        "goal_category_distribution": category_dist,
        "config_thresholds": thresholds,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Work alignment check — Self-grounded metrics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_check = subparsers.add_parser("check", help="Run alignment check")
    p_check.add_argument("--ranked-goals", type=str, default=None,
                         help="JSON string of ranked goals from goal-selector")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)


if __name__ == "__main__":
    main()
