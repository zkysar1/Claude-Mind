#!/usr/bin/env python3
"""Auto-derived skill quality scoring wrapper for Step 8.76 (, rb-428).

Maps execution signals to four of the five quality dimensions mechanically;
the caller (aspirations-state-update/SKILL.md Step 8.76) supplies only
cost_awareness (semantic retrieval-proportionality judgment).

Dimension derivations (see core/config/conventions/skill-quality.md):
  safety          = from --guardrail-violations count
  completeness    = from --outcomes-met / --outcomes-total ratio
  executability   = from --episode-chain-count
  maintainability = auto from forged-skills registry (base → "good",
                    forged → registry.quality_at_forge or "good")
  cost_awareness  = supplied by caller (LLM semantic judgment)

Write path:
  Acquires <meta>/skill-quality.yaml.lock (via _fileops.acquire_lock),
  invokes skill-evaluate.py score with the 5 derived/supplied grades,
  releases lock. The underlying skill-evaluate.py uses tmp+rename, so
  atomicity is preserved; this layer adds cross-process serialization
  against concurrent agent races.

rb-428 lineage: bash-consolidation of LLM-orchestrated step to eliminate
the abbreviation-drift risk (same family as loop-state-save, iteration-close,
compact-cache-size fix). The LLM residue (cost_awareness) is the 1-of-5
judgment that genuinely cannot be mechanically derived.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Add script dir to path for _paths / _fileops imports
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from _paths import META_DIR, WORLD_DIR  # noqa: E402
from _fileops import acquire_lock, release_lock  # noqa: E402

QUALITY_PATH = META_DIR / "skill-quality.yaml"
LOCK_PATH = QUALITY_PATH.with_suffix(".yaml.lock")
FORGED_REGISTRY = WORLD_DIR / "forged-skills.yaml"
SKILL_EVAL = _SCRIPT_DIR / "skill-evaluate.py"
SKILLS_DIR = _SCRIPT_DIR.parent.parent / ".claude" / "skills"


# --- Canonical-name normalization (skill-telemetry-signal-master-plan Layer 3) ---
# The LLM caller (aspirations-state-update Step 8.76) historically passed
# raw skill names — sometimes with leading "/", sometimes with trailing args
# (e.g., "tree maintain"). The result was denormalized keys in
# skill-quality.yaml: both "/replay" and "replay" as separate entries,
# "tree maintain" with a space, etc. Per the master plan: "scripts
# canonicalize, not LLMs."  This function enforces a single canonical key
# at the write boundary so duplicate/garbage keys cannot appear.

_CANONICAL_NAMES = None  # lazy-loaded on first call


def _load_canonical_names():
    """Build the canonical skill-name set from .claude/skills/ + forged registry."""
    names = set()
    if SKILLS_DIR.is_dir():
        for entry in SKILLS_DIR.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
    try:
        import yaml
        if FORGED_REGISTRY.exists():
            with open(FORGED_REGISTRY, encoding="utf-8") as f:
                fd = yaml.safe_load(f) or {}
            forged = fd.get("skills") or {}
            if isinstance(forged, dict):
                names.update(forged.keys())
    except Exception:
        pass  # forged filter is best-effort; never block scoring
    return names


def canonicalize_skill_name(raw: str) -> str:
    """Normalize a skill name to the canonical key.

    Steps: strip leading "/", drop anything after first whitespace,
    validate against the canonical list. Raises ValueError on miss.

    Examples:
      "/replay"        -> "replay"
      "tree maintain"  -> "tree"
      "replay"         -> "replay"
      "nonsense"       -> ValueError
    """
    global _CANONICAL_NAMES
    if _CANONICAL_NAMES is None:
        _CANONICAL_NAMES = _load_canonical_names()
    if not raw or not raw.strip():
        raise ValueError("empty skill name")
    name = raw.lstrip("/").split(None, 1)[0]
    if not name:
        raise ValueError(f"skill name empty after stripping (raw={raw!r})")
    if name not in _CANONICAL_NAMES:
        raise ValueError(
            f"skill name {name!r} (from raw={raw!r}) not in canonical list "
            f"of {len(_CANONICAL_NAMES)} known skills. Check .claude/skills/ "
            f"and world/forged-skills.yaml. If this is a new skill, register "
            f"it before scoring."
        )
    return name


def grade_from_ratio(met: int, total: int) -> str:
    """Map outcomes-met/total ratio to grade."""
    if total <= 0:
        return "good"  # No verification outcomes defined → pass-through
    ratio = met / total
    if ratio >= 1.0:
        return "good"
    if ratio >= 0.5:
        return "average"
    return "poor"


def grade_from_episodes(count: int) -> str:
    """Episode chain count: 0=good, 1=average, 2+=poor."""
    if count <= 0:
        return "good"
    if count == 1:
        return "average"
    return "poor"


def grade_from_violations(count: int) -> str:
    """Guardrail violations: 0=good, 1-2=average (caught), 3+=poor."""
    if count <= 0:
        return "good"
    if count <= 2:
        return "average"
    return "poor"


def derive_maintainability(skill_name: str) -> str:
    """Base skills default 'good'; forged skills look up registry.

    Forged registry supports multiple schemas — check common keys without
    being strict. If the registry is malformed or absent, fail open to 'good'.
    """
    try:
        import yaml
    except ImportError:
        return "good"
    try:
        if not FORGED_REGISTRY.exists():
            return "good"  # No registry → treat as base skill
        with open(FORGED_REGISTRY, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
    except Exception:
        return "good"
    # Try common top-level keys
    forged = None
    for key in ("skills", "forged_skills", "forged"):
        v = reg.get(key)
        if isinstance(v, dict):
            forged = v
            break
        if isinstance(v, list):
            forged = {e.get("name") or e.get("skill"): e for e in v if isinstance(e, dict)}
            break
    if not isinstance(forged, dict):
        return "good"
    entry = forged.get(skill_name)
    if not isinstance(entry, dict):
        return "good"  # Base skill (not in registry)
    for key in ("maintainability", "quality_at_forge", "quality"):
        v = entry.get(key)
        if v in ("good", "average", "poor"):
            return v
    return "good"


def cmd_score(args) -> int:
    """Derive four dimensions, accept cost_awareness, write under lock."""
    # Canonicalize at the write boundary — see canonicalize_skill_name docstring.
    # Fails LOUD on invalid names rather than writing garbage keys.
    try:
        args.skill = canonicalize_skill_name(args.skill)
    except ValueError as e:
        print(f"[skill-quality-score] ERROR: {e}", file=sys.stderr)
        return 1

    safety = args.safety_override or grade_from_violations(args.guardrail_violations)
    completeness = args.completeness_override or grade_from_ratio(args.outcomes_met, args.outcomes_total)
    executability = args.executability_override or grade_from_episodes(args.episode_chain_count)
    maintainability = args.maintainability_override or derive_maintainability(args.skill)

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "skill": args.skill,
            "goal": args.goal,
            "derived": {
                "safety": safety,
                "completeness": completeness,
                "executability": executability,
                "maintainability": maintainability,
            },
            "llm_supplied": {"cost_awareness": args.cost_awareness},
        }, indent=2))
        return 0

    # Lock-protected write (cross-process serialization vs concurrent agents).
    # stale_seconds=60: subprocess inside the lock (skill-evaluate score call) can
    # legitimately hold for many seconds; 30s default risks false-breaking a
    # healthy holder mid-eval. 60s = subprocess upper-bound + buffer.
    acquire_lock(LOCK_PATH, timeout=15, stale_seconds=60)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SKILL_EVAL),
                "score",
                "--skill", args.skill,
                "--goal", args.goal,
                "--safety", safety,
                "--completeness", completeness,
                "--executability", executability,
                "--maintainability", maintainability,
                "--cost-awareness", args.cost_awareness,
            ],
            capture_output=True,
            text=True,
        )
    finally:
        release_lock(LOCK_PATH)

    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        print(f"[skill-quality-score] ERROR: skill-evaluate.py exit {result.returncode}",
              file=sys.stderr)
        return result.returncode
    # Forward skill-evaluate.py stdout (human-readable score line)
    print(result.stdout.strip())
    # Then emit a machine-readable trailer so callers can parse
    print(json.dumps({
        "derived": {
            "safety": safety,
            "completeness": completeness,
            "executability": executability,
            "maintainability": maintainability,
        },
        "llm_supplied": {"cost_awareness": args.cost_awareness},
    }))
    return 0


def cmd_derive(args) -> int:
    """Print auto-derived grades without writing (preview / testing)."""
    try:
        args.skill = canonicalize_skill_name(args.skill)
    except ValueError as e:
        print(f"[skill-quality-score] ERROR: {e}", file=sys.stderr)
        return 1
    out = {
        "safety": grade_from_violations(args.guardrail_violations),
        "completeness": grade_from_ratio(args.outcomes_met, args.outcomes_total),
        "executability": grade_from_episodes(args.episode_chain_count),
        "maintainability": derive_maintainability(args.skill),
        "canonical_skill_name": args.skill,
    }
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="skill-quality-score.py",
        description="Auto-derived skill quality scoring wrapper for Step 8.76",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_signal_args(parser):
        parser.add_argument("--skill", required=True,
                            help="Skill name (no leading slash)")
        parser.add_argument("--goal", required=True,
                            help="Goal id (e.g., g-248-34)")
        parser.add_argument("--outcomes-met", type=int, default=0,
                            help="Count of verification.outcomes met")
        parser.add_argument("--outcomes-total", type=int, default=0,
                            help="Total verification.outcomes for this goal")
        parser.add_argument("--episode-chain-count", type=int, default=0,
                            help="Number of episode-chain retries during execute")
        parser.add_argument("--guardrail-violations", type=int, default=0,
                            help="Guardrail violations caught this iteration")

    p_score = sub.add_parser("score", help="Score + write to meta/skill-quality.yaml under lock")
    add_signal_args(p_score)
    p_score.add_argument("--cost-awareness", required=True,
                         choices=["good", "average", "poor"],
                         help="LLM-supplied retrieval-proportionality grade")
    p_score.add_argument("--safety-override", default=None,
                         choices=["good", "average", "poor"],
                         help="Override auto-derived safety (edge cases only)")
    p_score.add_argument("--completeness-override", default=None,
                         choices=["good", "average", "poor"])
    p_score.add_argument("--executability-override", default=None,
                         choices=["good", "average", "poor"])
    p_score.add_argument("--maintainability-override", default=None,
                         choices=["good", "average", "poor"])
    p_score.add_argument("--dry-run", action="store_true",
                         help="Print derived grades without writing")
    p_score.set_defaults(func=cmd_score)

    p_derive = sub.add_parser("derive", help="Print auto-derived grades (no write, no lock)")
    add_signal_args(p_derive)
    p_derive.set_defaults(func=cmd_derive)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
