#!/usr/bin/env python3
"""Portfolio-shape detectors for /strategic-pulse.

Reads world + agent aspiration state and emits structured "decision
prompts" describing patterns the user should consider acting on.
Distinct from /priority-review (user-pull, dashboard-only) — this
is the proactive surface that says "here's what I'd flag if I were
your strategist."

Detectors (Origin: LifingPolls plan item 10, 2026-05-08):
  1. tail_consolidation     — many aspirations clustered at high completion
  2. work_class_skew        — one work_class >2× its target fraction
  3. aged_aspirations       — active aspirations >60d since last completion
  4. idle_aspirations       — active aspirations with all goals deferred/blocked

Usage:
  py -3 core/scripts/strategic-pulse-detectors.py [--json|--text]

Default: text (terminal-friendly). --json emits structured records for
the SKILL composer to email through /notify-user.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _paths import WORLD_DIR, AGENT_DIR, CONFIG_DIR  # noqa: E402

ABANDONED_STATUSES = {"skipped", "expired", "decomposed", "superseded"}
TERMINAL_STATUSES = ABANDONED_STATUSES | {"completed"}

# Tunables — live in code rather than config because these are the
# detector's signal thresholds (changing them changes what surfaces),
# not user preferences.
TAIL_CONSOLIDATION_RATIO_THRESHOLD = 0.75
TAIL_CONSOLIDATION_MIN_COUNT = 5
WORK_CLASS_SKEW_FACTOR = 2.0
AGED_ASPIRATION_DAYS = 60
IDLE_ASPIRATION_MIN_GOALS = 1


def _read_aspirations() -> list[dict]:
    """Read all live (non-archive) aspirations across world + agent queues."""
    out = []
    for p in [
        WORLD_DIR / "aspirations.jsonl" if WORLD_DIR else None,
        AGENT_DIR / "aspirations.jsonl" if AGENT_DIR else None,
    ]:
        if p is None or not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _read_class_balance_targets() -> dict:
    """Read work-class targets from core/config/aspirations.yaml."""
    path = CONFIG_DIR / "aspirations.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return ((cfg.get("class_balance") or {}).get("targets") or {})


def _completion_ratio(asp: dict) -> tuple[float, int, int]:
    """Return (ratio, completed, total) for non-recurring goals."""
    goals = [g for g in asp.get("goals", [])
             if not g.get("recurring")
             and g.get("status") not in ABANDONED_STATUSES]
    total = len(goals)
    done = sum(1 for g in goals if g.get("status") == "completed")
    return (done / total if total > 0 else 0.0, done, total)


def _last_completion(asp: dict) -> datetime | None:
    """Latest completed_date across this asp's goals."""
    latest = None
    for g in asp.get("goals", []):
        cd = g.get("completed_date")
        if not cd:
            continue
        try:
            dt = datetime.fromisoformat(str(cd))
        except (ValueError, TypeError):
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


def _all_goals_blocked_or_deferred(asp: dict) -> bool:
    """True if every non-terminal non-recurring goal is blocked/deferred.

    Both narrative (defer_reason) AND structured (deferred_until) defers
    count. Pass 1 auto-pairs them, but legacy goals can have deferred_until
    without defer_reason — they ARE deferred and must count toward idle.
    """
    actionable = [g for g in asp.get("goals", [])
                  if not g.get("recurring")
                  and g.get("status") not in TERMINAL_STATUSES]
    if len(actionable) < IDLE_ASPIRATION_MIN_GOALS:
        return False
    for g in actionable:
        if (g.get("status") == "blocked"
                or g.get("defer_reason")
                or g.get("deferred_until")
                or (g.get("blocked_by") or [])):
            continue
        return False  # found at least one actionable goal
    return True


# ---- Detectors -------------------------------------------------------------


def detect_tail_consolidation(asps: list[dict]) -> dict | None:
    active = [a for a in asps if a.get("status") == "active"]
    high = []
    for a in active:
        ratio, done, total = _completion_ratio(a)
        if total > 0 and ratio >= TAIL_CONSOLIDATION_RATIO_THRESHOLD:
            high.append({
                "asp_id": a["id"],
                "title": a.get("title", "")[:80],
                "completion_ratio": round(ratio, 3),
                "remaining": total - done,
            })
    if len(high) < TAIL_CONSOLIDATION_MIN_COUNT:
        return None
    avg = sum(h["completion_ratio"] for h in high) / len(high)
    return {
        "pattern": "tail_consolidation",
        "magnitude": "high" if len(high) >= 10 else "medium",
        "evidence": {
            "high_completion_count": len(high),
            "avg_ratio": round(avg, 3),
            "examples": high[:5],
        },
        "suggestion": (
            f"{len(high)} active aspirations are at {avg:.0%} completion or "
            f"higher. Consolidate before expanding — finishing 3 vs "
            f"starting 3 new produces compounding learning. Review the "
            f"tail and either close (intent-satisfaction) or unblock the "
            f"remaining goals."
        ),
    }


def detect_work_class_skew(asps: list[dict]) -> dict | None:
    targets = _read_class_balance_targets()
    if not targets:
        return None
    # Count goals by work_class across ACTIVE aspirations
    counter = Counter()
    for a in asps:
        if a.get("status") != "active":
            continue
        for g in a.get("goals", []):
            if g.get("status") in TERMINAL_STATUSES:
                continue
            wc = g.get("work_class") or "unclassified"
            counter[wc] += 1
    total = sum(counter.values())
    if total == 0:
        return None
    actual_fractions = {wc: c / total for wc, c in counter.items()}
    skewed = []
    for wc, target in targets.items():
        actual = actual_fractions.get(wc, 0.0)
        if target > 0 and actual >= target * WORK_CLASS_SKEW_FACTOR:
            skewed.append({
                "work_class": wc,
                "target_fraction": target,
                "actual_fraction": round(actual, 3),
                "skew_factor": round(actual / target, 2),
            })
    if not skewed:
        return None
    skewed.sort(key=lambda x: -x["skew_factor"])
    return {
        "pattern": "work_class_skew",
        "magnitude": "high" if skewed[0]["skew_factor"] >= 3 else "medium",
        "evidence": {
            "skewed_classes": skewed,
            "total_active_goals": total,
            "target_fractions": dict(targets),
        },
        "suggestion": (
            f"Active goals over-weight class '{skewed[0]['work_class']}' at "
            f"{skewed[0]['actual_fraction']:.0%} vs target "
            f"{skewed[0]['target_fraction']:.0%} "
            f"({skewed[0]['skew_factor']:.1f}× ratio). Consider creating "
            f"goals in under-represented classes, or revisiting class "
            f"target fractions in core/config/aspirations.yaml § class_balance."
        ),
    }


def detect_aged_aspirations(asps: list[dict]) -> dict | None:
    cutoff = datetime.now() - timedelta(days=AGED_ASPIRATION_DAYS)
    aged = []
    for a in asps:
        if a.get("status") != "active":
            continue
        last = _last_completion(a)
        # Use created date as fallback if no completions
        if last is None:
            created = a.get("created")
            if not created:
                continue
            try:
                last = datetime.fromisoformat(str(created))
            except (ValueError, TypeError):
                continue
        if last < cutoff:
            days_since = (datetime.now() - last).days
            aged.append({
                "asp_id": a["id"],
                "title": a.get("title", "")[:80],
                "days_since_last_activity": days_since,
            })
    if not aged:
        return None
    aged.sort(key=lambda x: -x["days_since_last_activity"])
    return {
        "pattern": "aged_aspirations",
        "magnitude": "high" if len(aged) >= 5 else "medium",
        "evidence": {
            "aged_count": len(aged),
            "examples": aged[:5],
            "threshold_days": AGED_ASPIRATION_DAYS,
        },
        "suggestion": (
            f"{len(aged)} aspirations have had no goal completions in "
            f"{AGED_ASPIRATION_DAYS}+ days. They may be hopelessly stalled "
            f"or genuinely paused — either way the queue is carrying them. "
            f"Decide retire vs unblock vs deferred-pause for each."
        ),
    }


def detect_idle_aspirations(asps: list[dict]) -> dict | None:
    idle = []
    for a in asps:
        if a.get("status") != "active":
            continue
        if _all_goals_blocked_or_deferred(a):
            idle.append({
                "asp_id": a["id"],
                "title": a.get("title", "")[:80],
            })
    if not idle:
        return None
    return {
        "pattern": "idle_aspirations",
        "magnitude": "high" if len(idle) >= 5 else "medium",
        "evidence": {
            "idle_count": len(idle),
            "examples": idle[:10],
        },
        "suggestion": (
            f"{len(idle)} active aspirations have ALL goals blocked or "
            f"deferred — no actionable work in the queue. Review blockers; "
            f"if the blockers are stale or the work is no longer needed, "
            f"unblock or retire so the queue surfaces real options."
        ),
    }


# ---- Composition ----------------------------------------------------------


def compose_pulse(asps: list[dict]) -> list[dict]:
    out = []
    for fn in (detect_tail_consolidation, detect_work_class_skew,
               detect_aged_aspirations, detect_idle_aspirations):
        result = fn(asps)
        if result is not None:
            out.append(result)
    return out


def render_text(pulses: list[dict]) -> str:
    if not pulses:
        return "[strategic-pulse] No notable patterns detected. Portfolio looks healthy."
    lines = ["═══ STRATEGIC PULSE ═══════════════════════════════"]
    for p in pulses:
        magnitude = p.get("magnitude", "medium").upper()
        lines.append(f"\n[{magnitude}] {p['pattern']}")
        lines.append(f"  {p['suggestion']}")
        ev = p.get("evidence") or {}
        examples = ev.get("examples") or []
        if examples:
            lines.append("  Examples:")
            for e in examples[:3]:
                # Compact one-liner
                bits = []
                for k, v in e.items():
                    if k in ("asp_id", "title", "completion_ratio",
                             "days_since_last_activity"):
                        bits.append(f"{k}={v}")
                lines.append(f"    - {' '.join(bits)}")
    lines.append("\n═══════════════════════════════════════════════════")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="Emit JSON list")
    p.add_argument("--text", action="store_true", help="Emit human-readable text")
    args = p.parse_args()

    asps = _read_aspirations()
    pulses = compose_pulse(asps)

    if args.json:
        print(json.dumps(pulses, indent=2))
    else:
        # Default to text
        print(render_text(pulses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
