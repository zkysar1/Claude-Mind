#!/usr/bin/env python3
"""Skill quality evaluation across five dimensions.

Records per-skill quality evaluations (safety, completeness, executability,
maintainability, cost_awareness), maintains rolling aggregates, and reports
on underperforming skills. Dimension weights are agent-tunable via
meta/skill-quality-strategy.yaml.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

# Ensure stdout/stderr handle unicode on all platforms (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from _paths import META_DIR

# : never hardcode the escalation aspiration —  is the UPSTREAM
# deployment's queue and does not exist elsewhere, so a literal files nothing.
try:
    from _paths import AGENT_DIR, CORE_ROOT, WORLD_DIR
    from _escalation_target import resolve as _resolve_asp, source_flag as _asp_source
    ESCALATION_ASP, _ESCALATION_ASP_VIA = _resolve_asp(CORE_ROOT, WORLD_DIR, AGENT_DIR)
    ESCALATION_SOURCE = _asp_source(ESCALATION_ASP, WORLD_DIR, AGENT_DIR)
except Exception:
    ESCALATION_ASP, _ESCALATION_ASP_VIA, ESCALATION_SOURCE = (
        "asp-115", "fallback:import-failed", "world")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUALITY_PATH = META_DIR / "skill-quality.yaml"
STRATEGY_PATH = META_DIR / "skill-quality-strategy.yaml"
ROLLING_WINDOW = 20  # Keep last 20 evaluations per skill
GRADE_MAP = {"good": 1.0, "average": 0.5, "poor": 0.0}
DIMENSIONS = ["safety", "completeness", "executability", "maintainability", "cost_awareness"]

DEFAULT_WEIGHTS = {
    "safety": 0.30,
    "completeness": 0.25,
    "executability": 0.20,
    "maintainability": 0.15,
    "cost_awareness": 0.10,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_yaml(path):
    """Read a YAML file, return parsed dict. Returns {} if missing."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def write_yaml(path, data):
    """Atomically write data as YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def load_weights():
    """Load dimension weights from meta/skill-quality-strategy.yaml.

    Returns dict of dimension -> weight. Falls back to defaults if file
    is missing or dimension_weights key is absent.
    """
    data = read_yaml(STRATEGY_PATH)
    raw = data.get("dimension_weights")
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    weights = {}
    for dim in DIMENSIONS:
        weights[dim] = float(raw.get(dim, DEFAULT_WEIGHTS.get(dim, 0.0)))
    return weights


def compute_overall(scores, weights):
    """Compute weighted overall score from dimension scores and weights.

    Both scores and weights are dicts keyed by dimension name.
    """
    total = 0.0
    for dim in DIMENSIONS:
        total += scores.get(dim, 0.0) * weights.get(dim, 0.0)
    return round(total, 4)


def compute_aggregate(evaluations, weights):
    """Compute aggregate scores from a list of evaluation entries.

    Returns dict with each dimension's mean plus overall.
    """
    if not evaluations:
        return {dim: 0.0 for dim in DIMENSIONS + ["overall"]}

    count = len(evaluations)
    sums = {dim: 0.0 for dim in DIMENSIONS}
    for entry in evaluations:
        for dim in DIMENSIONS:
            sums[dim] += entry.get(dim, 0.0)

    means = {dim: round(sums[dim] / count, 4) for dim in DIMENSIONS}
    means["overall"] = compute_overall(means, weights)
    return means


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

HARNESS_VALUES = ("claude-code", "zakcode", "unknown")


def _judge_provenance(judge_model=None, harness=None):
    """Judge identity for one quality evaluation (, ).

    Returns (judge_model, harness), NORMALIZED from values the CALLER supplied.
    Both fall back to "unknown" rather than guessing, because this field exists
    to make cross-model comparison sound and a wrong value is worse than an
    absent one.

    RESOLVED CALLER-SIDE, NEVER HERE -- this function reads no environment. The
    judge is the process that produced the grades, but on the path that
    actually executes, the WRITER is the long-lived daemon: it inherits the
    environment of whichever session spawned it and holds that for its entire
    lifetime. An environment read here therefore reports one arbitrary
    session's values for every agent's request, on every record, forever --
    not absent, but confidently WRONG and authoritative-looking (guard-2480;
    the guard-1925 hazard). Wrappers resolve via rt_judge_provenance and
    forward judge_model/harness in the request body.

    judge_model is never inferred from CLAUDE_CODE_SUBAGENT_MODEL: that names
    the SUBAGENT model while scoring runs on the MAIN loop, so the two
    genuinely differ (measured 2026-09-01 on cc-04: subagent env read
    claude-opus-4-6 while the scoring session ran claude-opus-5).

    An unrecognised harness normalizes to "unknown" rather than passing
    through: it is a closed vocabulary that aggregate consumers group by, and
    a caller-supplied string is untrusted input.

    Legacy records carry neither key; absent therefore means "unknown" too, so
    no backfill is required (the merge handler unions whole evaluation dicts,
    so records written by older code survive unchanged).
    """
    model = (judge_model or "").strip() or "unknown"
    name = (harness or "").strip()
    return model, (name if name in HARNESS_VALUES else "unknown")


def _judge_from_env(env=None):
    """Resolve the judge's identity from the JUDGE'S OWN environment.

    CLI-LAYER ONLY, and the layer is the whole point. This module runs as a
    fresh subprocess of the session that produced the grades, so its
    environment IS the judge's -- the read is correct here and would be wrong
    one layer down in the writer, which under daemon-only architecture is a
    long-lived process holding a different session's environment entirely
    (guard-2480). The daemon twin deliberately has no counterpart to this
    function; its callers supply the values from the request body instead.

    Returns raw, UNNORMALIZED strings (empty when unresolvable) for
    _judge_provenance to normalize, so both layers share exactly one
    fallback rule.
    """
    env = os.environ if env is None else env
    model = (env.get("MIND_JUDGE_MODEL") or "").strip()
    if (env.get("CLAUDECODE") or "").strip():
        harness = "claude-code"
    elif ((env.get("ZAKCODE_MODEL") or "")
          or (env.get("ZAKCODE_SESSION") or "")).strip():
        harness = "zakcode"
    else:
        harness = ""
    return model, harness


def _judge_summary(evaluations):
    """Composition of the judge population behind an aggregate ().

    Returns a sorted list of {judge_model, harness, n}. Legacy records carry
    neither key and count as unknown/unknown: the field exists to make a
    MIXTURE visible, so dropping un-provenanced records would hide exactly the
    mixture the caller is asking about. A single-element list means the
    aggregate is judge-homogeneous and safe to compare over time; more than one
    means drift in it may be a judge change rather than a skill change.
    """
    counts = {}
    for e in evaluations or []:
        if not isinstance(e, dict):
            continue
        key = (str(e.get("judge_model") or "unknown"),
               str(e.get("harness") or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return [{"judge_model": m, "harness": h, "n": n}
            for (m, h), n in sorted(counts.items())]


def cmd_score(args):
    """Record a quality evaluation for a skill execution."""
    judge_model, harness = _judge_provenance(*_judge_from_env())
    data = read_yaml(QUALITY_PATH)
    weights = load_weights()

    # Map grade strings to numeric values
    scores = {
        "safety": GRADE_MAP[args.safety],
        "completeness": GRADE_MAP[args.completeness],
        "executability": GRADE_MAP[args.executability],
        "maintainability": GRADE_MAP[args.maintainability],
        "cost_awareness": GRADE_MAP[args.cost_awareness],
    }
    overall = compute_overall(scores, weights)

    # Build evaluation entry
    entry = {
        "goal_id": args.goal,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "safety": scores["safety"],
        "completeness": scores["completeness"],
        "executability": scores["executability"],
        "maintainability": scores["maintainability"],
        "cost_awareness": scores["cost_awareness"],
        "overall": overall,
        "judge_model": judge_model,
        "harness": harness,
    }

    # Ensure skills dict exists
    if "skills" not in data:
        data["skills"] = {}

    skill_name = args.skill
    if skill_name not in data["skills"]:
        data["skills"][skill_name] = {
            "evaluations": [],
            "aggregate": {},
            "total_evaluations": 0,
        }

    skill_data = data["skills"][skill_name]

    # Append to evaluations (FIFO, cap at ROLLING_WINDOW)
    evals = skill_data.get("evaluations", [])
    evals.append(entry)
    if len(evals) > ROLLING_WINDOW:
        evals = evals[-ROLLING_WINDOW:]
    skill_data["evaluations"] = evals

    # Recompute aggregate from current evaluations
    skill_data["aggregate"] = compute_aggregate(evals, weights)

    # Increment total_evaluations
    skill_data["total_evaluations"] = skill_data.get("total_evaluations", 0) + 1

    # Update root-level timestamp
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    write_yaml(QUALITY_PATH, data)

    print("Scored {skill}: overall {overall:.2f} (S:{s} C:{c} E:{e} M:{m} $:{ca})".format(
        skill=skill_name,
        overall=overall,
        s=args.safety,
        c=args.completeness,
        e=args.executability,
        m=args.maintainability,
        ca=args.cost_awareness,
    ))


def cmd_read(args):
    """Read quality data."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})

    if args.skill:
        # Specific skill
        skill_data = skills.get(args.skill)
        if skill_data is None:
            print(json.dumps({"error": "Skill '{}' not found".format(args.skill)},
                             indent=2, ensure_ascii=False))
            return
        print(json.dumps(skill_data, indent=2, ensure_ascii=False))
        return

    if args.all and args.summary:
        # Summary table: one entry per skill
        summary = []
        for name, sdata in skills.items():
            agg = sdata.get("aggregate", {})
            summary.append({
                "skill": name,
                "total_evaluations": sdata.get("total_evaluations", 0),
                "safety": agg.get("safety", 0.0),
                "completeness": agg.get("completeness", 0.0),
                "executability": agg.get("executability", 0.0),
                "maintainability": agg.get("maintainability", 0.0),
                "cost_awareness": agg.get("cost_awareness", 0.0),
                "overall": agg.get("overall", 0.0),
                "judges": _judge_summary(sdata.get("evaluations") or []),
            })
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    # Default: entire file
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_report(args):
    """Full quality report across all skills."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})

    if not skills:
        print(json.dumps({
            "skills": {},
            "summary": {
                "total_skills_evaluated": 0,
                "avg_overall": 0.0,
                "min_overall": 0.0,
                "max_overall": 0.0,
            },
            "alerts": [],
        }, indent=2, ensure_ascii=False))
        return

    # Build skills aggregate dict
    skills_agg = {}
    overalls = []
    alerts = []

    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        skills_agg[name] = dict(agg)
        skills_agg[name]["judges"] = _judge_summary(
            sdata.get("evaluations") or [])
        overall = agg.get("overall", 0.0)
        overalls.append(overall)

        # Check for dimensions below 0.30
        low_dims = [dim for dim in DIMENSIONS if agg.get(dim, 0.0) < 0.30]
        if low_dims:
            alerts.append({
                "skill": name,
                "dimensions_below_030": low_dims,
                "values": {dim: agg.get(dim, 0.0) for dim in low_dims},
            })

    result = {
        "skills": skills_agg,
        "summary": {
            "total_skills_evaluated": len(skills),
            "avg_overall": round(sum(overalls) / len(overalls), 4) if overalls else 0.0,
            "min_overall": round(min(overalls), 4) if overalls else 0.0,
            "max_overall": round(max(overalls), 4) if overalls else 0.0,
        },
        "alerts": alerts,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_underperforming(args):
    """Skills below threshold on any dimension."""
    data = read_yaml(QUALITY_PATH)
    skills = data.get("skills", {})
    threshold = args.threshold

    results = []
    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        overall = agg.get("overall", 0.0)

        # Check each dimension against threshold
        dims_below = []
        for dim in DIMENSIONS:
            val = agg.get(dim, 0.0)
            if val < threshold:
                dims_below.append({"dimension": dim, "value": val})

        # Include if overall < threshold OR any individual dimension < threshold
        if overall < threshold or dims_below:
            results.append({
                "skill": name,
                "overall": overall,
                "dimensions_below": dims_below,
                "total_evaluations": sdata.get("total_evaluations", 0),
            })

    # Sort by overall ascending (worst first)
    results.sort(key=lambda x: x["overall"])

    print(json.dumps(results, indent=2, ensure_ascii=False))


def _load_skill_attribution():
    """Load the hyphenated skill-attribution.py as an importable module.

    The filename has a hyphen (not a valid identifier), so a plain `import`
    can't reach it — importlib from the file path is the standard way. Both
    scripts live in the same core/scripts dir.
    """
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill-attribution.py")
    spec = importlib.util.spec_from_file_location("skill_attribution", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_reconsolidation_candidates(join, quality_skills, min_failures, min_fail_rate):
    """Pure: from an invocation->outcome join + quality data, build review candidates.

    A skill with failing invocations at/above BOTH thresholds is a
    reconsolidation-review candidate. reconsolidation_priority weights skills
    that are BOTH failing invocations AND low subjective quality highest
    (failure_rate * (1 - quality_overall); a skill with no quality data is
    treated as neutral 0.5). Sorted worst-first. Extracted for unit testing.
    """
    candidates = []
    for skill, counts in join.get("per_skill", {}).items():
        fails = counts.get("failure", 0)
        classified = counts.get("classified", 0)
        fail_rate = (fails / classified) if classified else 0.0
        if fails < min_failures or fail_rate < min_fail_rate:
            continue
        recent = [f["goal_id"] for f in join.get("failing", []) if f["skill"] == skill][-8:]
        q_overall = quality_skills.get(skill, {}).get("aggregate", {}).get("overall")
        priority = round(fail_rate * (1.0 - (q_overall if q_overall is not None else 0.5)), 4)
        candidates.append({
            "skill": skill,
            "failing_invocations": fails,
            "classified_invocations": classified,
            "failure_rate": round(fail_rate, 4),
            "success_rate": counts.get("success_rate"),
            "recent_failing_goals": recent,
            "current_quality_overall": q_overall,
            "reconsolidation_priority": priority,
        })
    candidates.sort(key=lambda x: -x["reconsolidation_priority"])
    return candidates


def _recon_slug(s):
    """Slug a skill name into a stable per-skill origin_signal dedup key."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48]


def _open_origin_signals():
    """Set of origin_signals across all OPEN (pending/in-progress) goals in the
    world+agent queues — the exact-match dedup base for `reconsolidation --apply`
    (a filed candidate carries origin_signal investigate:skill-reconsolidation-<slug>,
    so the next cadence's read finds it and suppresses a re-file; g-115-2196 exact-
    key dedup, NOT a title substring scan). Fail-open: a per-source read error
    skips that source (a missed dedup files a duplicate the daemon's own
    Duplication gate then catches — better than aborting the advisory scan).
    Lazy `import _rt` keeps the daemon client off the module-level import path
    of the read/report/score subcommands (no import-cycle with the daemon endpoint)."""
    import _rt  # noqa: E402 (lazy — only the --apply path needs the daemon client)
    sigs = set()
    for source in ("world", "agent"):
        try:
            out = _rt.aspirations_read(source=source, active=True)
        except Exception as e:  # noqa: BLE001 — fail-open per docstring
            print(f"[skill-evaluate reconsolidation] {source} read failed: {e}", file=sys.stderr)
            continue
        data = _rt.tolerant_decode_aggregate(f"skill-evaluate reconsolidation: {source}", out)
        if data is None:
            continue
        for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
            for g in asp.get("goals", []) or []:
                if g.get("status") in ("pending", "in-progress"):
                    sig = g.get("origin_signal")
                    if sig:
                        sigs.add(sig)
    return sigs


def file_reconsolidation_investigate(candidate, target_asp=ESCALATION_ASP):
    """--apply: file ONE reconsolidation candidate as an ADVISORY Investigate via
    the daemon add-goal endpoint (_rt — canonical Python->daemon path, NOT a bash
    subprocess). ADVISORY only: the goal asks the agent to REVIEW the failing
    skill's pseudocode against its failures, NEVER to auto-modify it (advisory-refine
    constraint, g-355-07). Best-effort, fail-open — a filing error is logged and
    returns None without aborting the scan. Returns the new goal id or None."""
    import _rt  # noqa: E402 (lazy — see _open_origin_signals)
    skill = candidate["skill"]
    sig = f"investigate:skill-reconsolidation-{_recon_slug(skill)}"
    record = {
        "title": f"Investigate: reconsolidate failing skill {skill} (failure_rate {candidate.get('failure_rate')})"[:140],
        "description": (
            f"skill-evaluate reconsolidation flagged '{skill}': "
            f"{candidate.get('failing_invocations')} failing / "
            f"{candidate.get('classified_invocations')} classified invocations "
            f"(failure_rate {candidate.get('failure_rate')}, current_quality_overall "
            f"{candidate.get('current_quality_overall')}, reconsolidation_priority "
            f"{candidate.get('reconsolidation_priority')}). Recent failing goals "
            f"(evidence): {candidate.get('recent_failing_goals', [])}. ADVISORY review "
            "of the skill's SKILL.md against these failures — identify the recurring "
            "failure mode and refine the pseudocode. Do NOT auto-modify the skill "
            "without human/verification review (advisory-refine constraint, g-355-07)."
        ),
        "priority": "MEDIUM",
        "participants": ["agent"],
        "category": "skill-quality",
        "intended_agent": "either",
        "origin_signal": sig,
        "tags": ["skill-reconsolidation", "advisory"],
    }
    override = {"Duplication": (
        f"reconsolidation exact-origin_signal dedup confirmed '{sig}' not open "
        "(no pending/in-progress goal carries this key)")}
    try:
        resp = _rt.aspirations_add_goal(target_asp, record, source=ESCALATION_SOURCE,
                                        overrides=override)
    except Exception as e:  # noqa: BLE001 — fail-open per docstring
        print(f"[skill-evaluate reconsolidation] file failed for {skill}: {e}", file=sys.stderr)
        return None
    gid = None
    if isinstance(resp, dict):
        g = resp.get("goal")
        if isinstance(g, dict):
            gid = g.get("id")
        gid = gid or resp.get("id")
    return gid


def cmd_reconsolidation(args):
    """Surface skills whose invocations are FAILING as reconsolidation-review candidates.

    Continual-Harness lifecycle (g-355-06): a skill is not just five-dimension
    quality-scored, it is success/failure-scored per invocation (via the
    skill-attribution invocation->outcome join). Any skill with failing
    invocations above the threshold is attached to a reconsolidation review,
    cross-referenced against its current subjective quality.
    """
    sa = _load_skill_attribution()
    agents = [args.agent] if args.agent else sa.find_agent_dirs()
    since_dt = sa.parse_since(args.since) if args.since else None
    join = sa.compute_join(agents, since_dt=since_dt)

    quality_skills = read_yaml(QUALITY_PATH).get("skills", {})
    candidates = build_reconsolidation_candidates(
        join, quality_skills, args.min_failures, args.min_fail_rate)

    result = {
        "reconsolidation_candidates": candidates,
        "candidate_count": len(candidates),
        "threshold": {"min_failures": args.min_failures, "min_fail_rate": args.min_fail_rate},
        "agents_scanned": agents,
        "window": args.since or "all_time",
    }

    # --apply: route each candidate into an ADVISORY Investigate goal, deduped
    # against open goals by exact origin_signal ( — exact key, never a
    # title substring). Mirrors silent-gap-audit's self-filing so the cadence
    # surface (strategic-scan S4.5) actually turns failing-invocation skills into
    # reviewable work instead of a report nobody reads. Advisory only: the filed
    # goal REVIEWS the skill against its failures, never auto-modifies it ().
    if getattr(args, "apply", False):
        target_asp = getattr(args, "target_asp", ESCALATION_ASP)
        open_sigs = _open_origin_signals()
        filed, suppressed_dedup = [], []
        for c in candidates:
            sig = f"investigate:skill-reconsolidation-{_recon_slug(c['skill'])}"
            if sig in open_sigs:
                suppressed_dedup.append({"skill": c["skill"], "origin_signal": sig})
                continue
            gid = file_reconsolidation_investigate(c, target_asp=target_asp)
            if gid:
                filed.append({"skill": c["skill"], "goal_id": gid, "origin_signal": sig})
        result["filed"] = filed
        result["suppressed_dedup"] = suppressed_dedup
        result["target_asp"] = target_asp

    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Skill quality evaluation (five dimensions)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_score = sub.add_parser("score", help="Record skill quality evaluation")
    p_score.add_argument("--skill", required=True)
    p_score.add_argument("--goal", required=True)
    p_score.add_argument("--safety", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--completeness", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--executability", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--maintainability", required=True, choices=["good", "average", "poor"])
    p_score.add_argument("--cost-awareness", required=True, choices=["good", "average", "poor"])

    p_read = sub.add_parser("read", help="Read quality data")
    p_read.add_argument("--skill")
    p_read.add_argument("--all", action="store_true")
    p_read.add_argument("--summary", action="store_true")

    sub.add_parser("report", help="Full quality report")

    p_under = sub.add_parser("underperforming", help="Skills below quality threshold")
    p_under.add_argument("--threshold", type=float, default=0.50)

    p_recon = sub.add_parser("reconsolidation",
                             help="Surface failing-invocation skills for reconsolidation review "
                                  "(invocation->outcome join from skill-attribution)")
    p_recon.add_argument("--agent", help="Limit to one agent (else all with a ledger)")
    p_recon.add_argument("--since", default="", help="Time window: 7d, 24h, 30m, or ISO date")
    p_recon.add_argument("--min-failures", type=int, default=2,
                         help="Minimum failing invocations to flag a skill (default 2)")
    p_recon.add_argument("--min-fail-rate", type=float, default=0.20,
                         help="Minimum failure rate to flag a skill (default 0.20)")
    p_recon.add_argument("--apply", action="store_true",
                         help="File each candidate as an advisory Investigate goal "
                              "(exact-origin_signal dedup; advisory-refine only, g-355-07)")
    p_recon.add_argument("--target-asp", default=ESCALATION_ASP,
                         help="Aspiration to file reconsolidation Investigate goals into "
                              f"(default {ESCALATION_ASP}, resolved per deployment)")

    args = parser.parse_args()
    cmds = {"score": cmd_score, "read": cmd_read, "report": cmd_report,
            "underperforming": cmd_underperforming, "reconsolidation": cmd_reconsolidation}
    cmds[args.command](args)


if __name__ == "__main__":
    main()
