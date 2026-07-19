#!/usr/bin/env python3
"""skill-retire-candidates.py — Layer 5e retire-policy auto-flagger ().

Surfaces skills as RETIREMENT-REVIEW candidates by combining three staleness
signals. It NEVER deletes anything — the output is advisory input for a human
`/encode-session` or `/verify-learning` review pass.

A skill is flagged ONLY when ALL of these hold:
  (a) zero Skill-tool invocations in the last N days (default 30), AND
  (b) quality eval is stale (> quality-stale-days, default 7) OR missing, AND
  (c) no SKILL.md edit in the last N days (default 30, via git log), AND
  (d) the skill declares NO companion_scripts.

Criterion (d) is the conservative guard demanded by the goal's precondition
(session-75 evolution finding, g-304-15): the invocation ledgers
(skill-invocations.jsonl, journal, execution-diary) count Skill-TOOL
invocations but do NOT reliably record companion-script *bash* executions,
which are the dominant invocation path for access-* infra/orchestration
forged skills (e.g. access-operator-api's companion operator-api.sh runs
often yet reads as 0-invocation). A companion-script-bearing skill showing
"zero invocations" is EXPECTED, not disused — so those skills are EXCLUDED
from candidacy rather than false-flagged. There is no high-fidelity structured
log of .sh executions to distinguish a truly-dead companion-script skill from
an active-but-untracked one, so the safe rule is: never propose a
companion-script skill for retirement on the zero-invocation signal.
(verify-before-assuming; rb-1361 n=1 caution — do NOT retire the 10 access-*
infra skills flagged by the current invocation signal.)

Protected core/control/orchestrator skills are never flagged regardless of
signals (a second belt-and-suspenders guard; the criteria already spare
actively-run core skills).

Usage:
  py -3 core/scripts/skill-retire-candidates.py [--json|--verbose]
      [--window-days N] [--quality-stale-days N] [--skillmd-stale-days N]

Exit code is always 0 (advisory; a bug here must never block a caller).
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# --- import framework path + skill-md helpers (script lives in core/scripts) ---
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _paths import PROJECT_ROOT, WORLD_DIR, META_DIR, agents_root  # noqa: E402
import _skill_md  # noqa: E402

try:
    import yaml
except Exception:  # pragma: no cover - yaml is always present in this env
    yaml = None

SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"

# Core/control/orchestrator skills that must never be surfaced as retire
# candidates regardless of signals. The invocation/edit criteria already spare
# actively-run core skills; this is a second guard for essential-but-rarely-
# invoked skills (control commands, session-boundary skills, security review).
PROTECTED_EXACT = frozenset({
    # user control commands
    "start", "stop", "open-questions", "tree-reader",
    # session-boundary + core cognition
    "boot", "prime", "respond", "replay", "decompose", "forge-skill",
    "create-aspiration", "review-hypotheses", "research-topic", "tree",
    "curriculum-gates", "verify-learning", "encode-session", "drain-temp",
    "notify-user", "security-review", "init", "review", "seed",
    # hybrid reporting skills
    "agent-completion-report", "backlog-report", "priority-review",
    "felt-sense-checkin",
})
# Prefix-protected families (orchestrator, reflection, fresh-eyes rituals).
PROTECTED_PREFIXES = ("aspirations", "reflect", "fresh-eyes")


def _is_protected(name: str) -> bool:
    if name in PROTECTED_EXACT:
        return True
    return any(name.startswith(p) for p in PROTECTED_PREFIXES)


def _parse_ts(value):
    """Parse an ISO-ish timestamp to a NAIVE-local datetime. None on failure.

    Inputs are mixed: skill-invocations `ts` and skill-quality `date` are naive
    ("2026-07-02T13:19:10"), but `git log %cI` is offset-AWARE
    ("2026-05-17T22:55:24-04:00"). Normalize every aware result to naive-local
    (drop tzinfo after converting to local) so all comparisons with
    datetime.now() (naive local per convention) are type-consistent — the
    g-304-15 offset-aware-vs-naive TypeError guard."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    dt = None
    for candidate in (v, v[:19], v[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except Exception:
            continue
    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def enumerate_skills():
    """Return {name: {"kinds": set(), "companion_scripts": [..]}} across base +
    forged skills."""
    skills = {}
    # base skills = .claude/skills/*/SKILL.md dirs
    if SKILLS_DIR.is_dir():
        for child in sorted(SKILLS_DIR.iterdir()):
            md = child / "SKILL.md"
            if child.is_dir() and md.is_file():
                comp = []
                try:
                    comp = list(_skill_md.get_companion_scripts(md) or [])
                except Exception:
                    comp = []
                entry = skills.setdefault(child.name, {"kinds": set(), "companion_scripts": []})
                entry["kinds"].add("base")
                entry["companion_scripts"].extend(comp)
    # forged skills = $WORLD_DIR/forged-skills.yaml skills: {name: {companion_scripts}}
    forged_path = WORLD_DIR / "forged-skills.yaml"
    if yaml is not None and forged_path.is_file():
        try:
            data = yaml.safe_load(forged_path.read_text(encoding="utf-8")) or {}
            for name, info in (data.get("skills") or {}).items():
                comp = (info or {}).get("companion_scripts") or []
                entry = skills.setdefault(name, {"kinds": set(), "companion_scripts": []})
                entry["kinds"].add("forged")
                entry["companion_scripts"].extend(comp)
        except Exception:
            pass
    # dedupe companion_scripts per skill
    for entry in skills.values():
        entry["companion_scripts"] = sorted(set(entry["companion_scripts"]))
    return skills


def load_invocation_counts(cutoff):
    """Count Skill-tool invocations per skill name at/after cutoff, across all
    agents' skill-invocations.jsonl ledgers. Returns {skill_name: count}."""
    counts = {}
    try:
        ledgers = sorted(agents_root().glob("*/skill-invocations.jsonl"))
    except Exception:
        ledgers = []
    for ledger in ledgers:
        try:
            with ledger.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    name = rec.get("skill")
                    ts = _parse_ts(rec.get("ts") or rec.get("timestamp") or rec.get("date"))
                    if not name or ts is None:
                        continue
                    if ts >= cutoff:
                        counts[name] = counts.get(name, 0) + 1
        except Exception:
            continue
    return counts


def load_last_quality_eval():
    """Return {skill_name: last_eval_datetime} from skill-quality.yaml."""
    out = {}
    qpath = META_DIR / "skill-quality.yaml"
    if yaml is None or not qpath.is_file():
        return out
    try:
        data = yaml.safe_load(qpath.read_text(encoding="utf-8")) or {}
    except Exception:
        return out
    for name, info in (data.get("skills") or {}).items():
        evals = (info or {}).get("evaluations") or []
        dates = [_parse_ts(e.get("date")) for e in evals if isinstance(e, dict)]
        dates = [d for d in dates if d is not None]
        if dates:
            out[name] = max(dates)
    return out


def skill_md_last_edit(name, kinds):
    """Last commit date (datetime) of the skill's SKILL.md via git log. None if
    no SKILL.md / no history."""
    if "base" not in kinds:
        return None  # forged-only skills have no .claude/skills SKILL.md
    md = SKILLS_DIR / name / "SKILL.md"
    if not md.is_file():
        return None
    try:
        rel = md.relative_to(PROJECT_ROOT)
    except Exception:
        rel = md
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(rel)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=15,
        )
        out = (res.stdout or "").strip()
        if out:
            return _parse_ts(out)
    except Exception:
        return None
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flag skills for retirement review (never deletes).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="JSON output (default)")
    mode.add_argument("--verbose", action="store_true", help="Human-readable table")
    ap.add_argument("--window-days", type=int, default=30,
                    help="Invocation + SKILL.md-edit lookback window (default 30)")
    ap.add_argument("--quality-stale-days", type=int, default=7,
                    help="Quality eval older than this (or missing) counts as stale (default 7)")
    ap.add_argument("--skillmd-stale-days", type=int, default=30,
                    help="SKILL.md not edited within this counts as stale (default 30)")
    args = ap.parse_args(argv)

    now = datetime.now()
    inv_cutoff = now - timedelta(days=args.window_days)
    quality_cutoff = now - timedelta(days=args.quality_stale_days)
    skillmd_cutoff = now - timedelta(days=args.skillmd_stale_days)

    skills = enumerate_skills()
    inv_counts = load_invocation_counts(inv_cutoff)
    last_quality = load_last_quality_eval()

    candidates = []
    excluded_untracked = 0
    excluded_protected = 0

    for name in sorted(skills):
        entry = skills[name]
        kinds = entry["kinds"]
        companion = entry["companion_scripts"]
        has_companion = len(companion) > 0

        if _is_protected(name):
            excluded_protected += 1
            continue

        inv_30d = inv_counts.get(name, 0)
        last_eval = last_quality.get(name)
        quality_stale = (last_eval is None) or (last_eval < quality_cutoff)
        md_edit = skill_md_last_edit(name, kinds)
        md_stale = (md_edit is None) or (md_edit < skillmd_cutoff)
        would_flag = (inv_30d == 0 and quality_stale and md_stale)

        # Conservative exclusion (criterion d, precondition guard): a skill
        # whose invocation path is NOT recorded by skill-invocations.jsonl reads
        # zero-invocation regardless of real use, so it must never be flagged.
        # Two such untracked paths:
        #   - companion_scripts: the skill runs primarily via a .sh bash call
        #     (access-* infra skills), which no structured ledger records.
        #   - forged skills: invoked via trigger-resolution / companion scripts,
        #     also unrecorded (skill-invocations.jsonl logs Skill-TOOL calls
        #     only). All 10 precondition-named "do NOT retire" skills are forged,
        #     so excluding forged skills honors that guidance directly.
        # Base, non-forged, non-companion skills ARE invoked via the Skill tool
        # (which the ledger records), so zero-invocation is meaningful for them.
        untracked_invocation = has_companion or ("forged" in kinds)
        if untracked_invocation:
            if would_flag:
                excluded_untracked += 1
            continue

        if would_flag:
            candidates.append({
                "skill": name,
                "kind": "+".join(sorted(kinds)),
                "reasons": {
                    "invocations_%dd" % args.window_days: inv_30d,
                    "last_quality_eval": last_eval.isoformat() if last_eval else None,
                    "quality_stale": quality_stale,
                    "skill_md_last_edit": md_edit.isoformat() if md_edit else None,
                    "skill_md_stale": md_stale,
                    "has_companion_scripts": False,
                },
                "confidence": "high",  # no companion-script masking on these
            })

    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": args.window_days,
        "thresholds": {
            "quality_stale_days": args.quality_stale_days,
            "skillmd_stale_days": args.skillmd_stale_days,
        },
        "total_skills": len(skills),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "excluded_untracked_invocation_skills": excluded_untracked,
        "excluded_protected": excluded_protected,
        "note": (
            "RETIREMENT-REVIEW candidates only — nothing is auto-deleted. "
            "Surface these to /encode-session or /verify-learning for human review. "
            "Skills with an untracked invocation path (forged skills, or any skill "
            "declaring companion_scripts) are excluded: their trigger/bash invocation "
            "is not recorded by skill-invocations.jsonl, so zero-invocation is "
            "uninformative — g-304-15 precondition; do NOT retire access-*/forged infra "
            "skills on the invocation signal. A future 'fix tracking to count "
            "companion-script/forged invocations' goal would make those skills flaggable."
        ),
    }

    if args.verbose:
        print("skill-retire-candidates — %s (window=%dd, quality-stale=%dd, skillmd-stale=%dd)"
              % (result["generated_at"], args.window_days, args.quality_stale_days, args.skillmd_stale_days))
        print("total skills scanned: %d | candidates: %d | excluded(untracked-invocation): %d | excluded(protected): %d"
              % (len(skills), len(candidates), excluded_untracked, excluded_protected))
        if candidates:
            print("\n%-34s %-8s %-6s %-22s %s" % ("SKILL", "KIND", "INV", "LAST_QUALITY_EVAL", "SKILL_MD_EDIT"))
            for c in candidates:
                r = c["reasons"]
                print("%-34s %-8s %-6s %-22s %s" % (
                    c["skill"], c["kind"], r["invocations_%dd" % args.window_days],
                    (r["last_quality_eval"] or "never")[:19], (r["skill_md_last_edit"] or "unknown")[:19]))
        else:
            print("\n(no retirement candidates — all skills show recent use, fresh quality, or companion-script activity)")
        print("\nNOTE:", result["note"])
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
