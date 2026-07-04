"""Skill-analytics read endpoints — parity with core/scripts/skill-analytics.py.

Daemonises all 5 skill-analytics subcommands (Batch 6). ALL are pure reads
(zero writes, no history/changelog/lock):

  GET /v1/skill-analytics/reuse-report      (cmd_reuse_report)   meta
  GET /v1/skill-analytics/co-invocation     (cmd_co_invocation)  world
  GET /v1/skill-analytics/coverage          (cmd_coverage)       agent (HEADER REQ)
  GET /v1/skill-analytics/recommendations   (cmd_recommendations) meta+world+config
  GET /v1/skill-analytics/trend?window=N     (cmd_trend)          meta

SCOPE: mixed. reuse-report/recommendations/trend read META (skill-quality.yaml,
skill-gaps.yaml); co-invocation/recommendations read WORLD (skill-relations.yaml);
recommendations also reads core/config/skill-relations.yaml (base relations);
coverage reads the bound agent's experience.jsonl and REQUIRES X-Mind-Agent
(400 missing_agent_header otherwise). The others need no header.

BYTE-COMPATIBILITY: every subcommand prints
json.dumps(data, indent=2, ensure_ascii=False) + "\n" (skill-analytics.py:151/
162/187/242/337/405). ensure_ascii=False (NOT the _fileops default True) and
indent=2 are both load-bearing — Response.text with the exact string, never
Response.json. Empty/missing inputs: co-invocation/trend emit "[]\n";
reuse-report/coverage/recommendations emit their zero-state objects.

The only CLI sys.exit is the module-level PyYAML import guard (line 31),
unreachable in the daemon. The logic is lifted verbatim; paths route through
ctx.paths (META->meta, WORLD->world, AGENT->agent, CONFIG->project_root/core/config).
"""
from __future__ import annotations

import datetime
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

import yaml


def _read_yaml(path: Path) -> Dict[str, Any]:
    from storage_backend import get_backend
    get_backend().ensure_local(path)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    from storage_backend import get_backend
    get_backend().ensure_local(path)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _quality_path(ctx) -> Path:
    return ctx.paths.meta / "skill-quality.yaml"


def _gaps_path(ctx) -> Path:
    return ctx.paths.meta / "skill-gaps.yaml"


def _world_relations_path(ctx) -> Path:
    return ctx.paths.world / "skill-relations.yaml"


def _base_relations_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "config" / "skill-relations.yaml"


def _load_all_relations(ctx) -> List[Dict[str, Any]]:
    base = _read_yaml(_base_relations_path(ctx)).get("relations", [])
    if not isinstance(base, list):
        base = []
    forged = _read_yaml(_world_relations_path(ctx)).get("forged_relations", [])
    if not isinstance(forged, list):
        forged = []
    return base + forged


def _out(obj: Any) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/reuse-report
# ---------------------------------------------------------------------------

def reuse_report(ctx) -> "Response":  # type: ignore[name-defined]
    quality = _read_yaml(_quality_path(ctx))
    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    result_skills: Dict[str, Any] = {}
    total_evaluated = 0
    quality_sum = 0.0

    for skill_name, skill_info in skills_data.items():
        if not isinstance(skill_info, dict):
            continue
        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue
        total_evals = len(evaluations)
        total_evaluated += 1
        scores = [e.get("overall", 0) for e in evaluations
                  if isinstance(e, dict) and "overall" in e]
        avg_quality = round(sum(scores) / len(scores), 3) if scores else 0.0
        quality_sum += avg_quality
        dates = [e.get("date", "") for e in evaluations
                 if isinstance(e, dict) and "date" in e]
        last_date = max(dates) if dates else None
        result_skills[skill_name] = {
            "total_evaluations": total_evals,
            "avg_quality": avg_quality,
            "min_quality": round(min(scores), 3) if scores else 0.0,
            "max_quality": round(max(scores), 3) if scores else 0.0,
            "last_evaluation_date": last_date,
        }

    avg_overall = round(quality_sum / total_evaluated, 3) if total_evaluated > 0 else 0.0
    return _out({
        "skills": result_skills,
        "summary": {"total_evaluated": total_evaluated, "avg_quality": avg_overall},
    })


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/co-invocation
# ---------------------------------------------------------------------------

def co_invocation(ctx) -> "Response":  # type: ignore[name-defined]
    world_data = _read_yaml(_world_relations_path(ctx))
    log = world_data.get("co_invocation_log", [])
    if not isinstance(log, list):
        log = []
    if not log:
        return _out([])

    pair_counts: Counter = Counter()
    for entry in log:
        skills = entry.get("skills", [])
        if not isinstance(skills, list) or len(skills) < 2:
            continue
        unique_skills = sorted(set(skills))
        for i in range(len(unique_skills)):
            for j in range(i + 1, len(unique_skills)):
                pair_counts[(unique_skills[i], unique_skills[j])] += 1

    total_entries = len(log)
    results = []
    for (skill_a, skill_b), count in pair_counts.most_common(20):
        pct = round(count / total_entries * 100, 1) if total_entries > 0 else 0.0
        results.append({"skill_a": skill_a, "skill_b": skill_b, "count": count, "pct": pct})
    return _out(results)


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/coverage
# ---------------------------------------------------------------------------

def coverage(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for coverage (reads agent experience.jsonl).")

    records = _read_jsonl(ctx.paths.agent / "experience.jsonl")
    categories = defaultdict(
        lambda: {"skill_counts": Counter(), "skill_successes": Counter(), "total": 0})

    for rec in records:
        if not isinstance(rec, dict):
            continue
        category = rec.get("category", rec.get("goal_category", ""))
        if not category:
            continue
        skill = rec.get("skill", rec.get("primary_skill", ""))
        if not skill:
            skills = rec.get("skills", [])
            if isinstance(skills, list) and skills:
                skill = skills[0]
            else:
                continue
        categories[category]["total"] += 1
        categories[category]["skill_counts"][skill] += 1
        outcome = rec.get("outcome", rec.get("result", ""))
        if isinstance(outcome, str) and outcome.lower() in ("success", "completed", "passed"):
            categories[category]["skill_successes"][skill] += 1
        elif isinstance(outcome, dict) and outcome.get("success"):
            categories[category]["skill_successes"][skill] += 1

    output_categories = {}
    for cat_name, cat_data in sorted(categories.items()):
        skills_list = []
        for skill, count in cat_data["skill_counts"].most_common():
            successes = cat_data["skill_successes"].get(skill, 0)
            success_rate = round(successes / count, 3) if count > 0 else 0.0
            skills_list.append({"skill": skill, "count": count, "success_rate": success_rate})
        output_categories[cat_name] = {"skills": skills_list, "total_goals": cat_data["total"]}

    return _out({"categories": output_categories})


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/recommendations
# ---------------------------------------------------------------------------

def recommendations(ctx) -> "Response":  # type: ignore[name-defined]
    quality = _read_yaml(_quality_path(ctx))
    gaps = _read_yaml(_gaps_path(ctx))
    relations = _load_all_relations(ctx)

    forge: List[Dict[str, Any]] = []
    retire: List[Dict[str, Any]] = []
    improve: List[Dict[str, Any]] = []
    substitute: List[Dict[str, Any]] = []

    gap_list = gaps.get("gaps", [])
    if not isinstance(gap_list, list):
        gap_list = []
    for gap in gap_list:
        if not isinstance(gap, dict):
            continue
        times = gap.get("times_encountered", 0)
        if times >= 3:
            forge.append({
                "skill": gap.get("name", gap.get("skill", "unknown")),
                "reason": "Capability gap encountered {} times".format(times),
                "evidence": gap.get("description", gap.get("evidence", "")),
            })

    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    skill_avg_quality: Dict[str, float] = {}
    for skill_name, skill_info in skills_data.items():
        if not isinstance(skill_info, dict):
            continue
        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue
        scores = [e.get("overall", 0) for e in evaluations
                  if isinstance(e, dict) and "overall" in e]
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        skill_avg_quality[skill_name] = avg
        if avg < 0.30:
            retire.append({
                "skill": skill_name,
                "reason": "Quality below 0.30 threshold (avg {})".format(round(avg, 3)),
                "evidence": "{} evaluations, avg quality {}".format(len(scores), round(avg, 3)),
            })
        elif avg < 0.50:
            improve.append({
                "skill": skill_name,
                "reason": "Quality between 0.30-0.50, improvement needed (avg {})".format(round(avg, 3)),
                "evidence": "{} evaluations, avg quality {}".format(len(scores), round(avg, 3)),
            })

    for rel in relations:
        if not isinstance(rel, dict):
            continue
        if rel.get("type") != "similar_to":
            continue
        source = rel.get("source", "")
        target = rel.get("target", "")
        source_q = skill_avg_quality.get(source)
        target_q = skill_avg_quality.get(target)
        if source_q is not None and target_q is not None:
            if target_q > source_q:
                substitute.append({
                    "skill": source,
                    "reason": "Similar skill '{}' has higher quality ({} vs {})".format(
                        target, round(target_q, 3), round(source_q, 3)),
                    "evidence": "Relation: {} --similar_to--> {}".format(source, target),
                })
            elif source_q > target_q:
                substitute.append({
                    "skill": target,
                    "reason": "Similar skill '{}' has higher quality ({} vs {})".format(
                        source, round(source_q, 3), round(target_q, 3)),
                    "evidence": "Relation: {} --similar_to--> {}".format(source, target),
                })

    return _out({"forge": forge, "retire": retire, "improve": improve, "substitute": substitute})


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/trend?window=N
# ---------------------------------------------------------------------------

def trend(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    window_raw = ctx.query.get("window")
    if window_raw is None or window_raw == "":
        window = 10
    else:
        try:
            window = int(window_raw)
        except ValueError:
            return Response.error(400, "invalid_param", "window must be an integer")

    quality = _read_yaml(_quality_path(ctx))
    skills_data = quality.get("skills", {})
    if not isinstance(skills_data, dict):
        skills_data = {}

    results = []
    for skill_name, skill_info in sorted(skills_data.items()):
        if not isinstance(skill_info, dict):
            continue
        evaluations = skill_info.get("evaluations", [])
        if not isinstance(evaluations, list) or not evaluations:
            continue
        recent = evaluations[-window:]
        scores = [e.get("overall", 0) for e in recent
                  if isinstance(e, dict) and "overall" in e]
        if len(scores) < 2:
            dates = [e.get("date", "") for e in recent
                     if isinstance(e, dict) and "date" in e]
            results.append({
                "skill": skill_name,
                "evaluations": len(scores),
                "oldest_date": min(dates) if dates else None,
                "newest_date": max(dates) if dates else None,
                "trend": "stable",
                "delta": 0.0,
            })
            continue
        mid = len(scores) // 2
        first_half = scores[:mid] if mid > 0 else scores[:1]
        second_half = scores[mid:]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        delta = round(avg_second - avg_first, 3)
        tolerance = 0.05
        if delta > tolerance:
            t = "improving"
        elif delta < -tolerance:
            t = "declining"
        else:
            t = "stable"
        dates = [e.get("date", "") for e in recent
                 if isinstance(e, dict) and "date" in e]
        results.append({
            "skill": skill_name,
            "evaluations": len(scores),
            "oldest_date": min(dates) if dates else None,
            "newest_date": max(dates) if dates else None,
            "trend": t,
            "delta": delta,
        })

    return _out(results)


# ---------------------------------------------------------------------------
# GET /v1/skill-analytics/usage-report?window=N
# ---------------------------------------------------------------------------

def usage_report(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    window_raw = ctx.query.get("window", "")
    try:
        window_days = int(window_raw) if window_raw else 30
    except ValueError:
        return Response.error(400, "invalid_param", "window must be an integer")

    cutoff = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    agents_root = ctx.paths.agents_root

    all_invocations: List[Dict[str, Any]] = []
    for inv_path in sorted(agents_root.glob("*/skill-invocations.jsonl")):
        for rec in _read_jsonl(inv_path):
            if isinstance(rec, dict) and rec.get("ts", "")[:10] >= cutoff:
                all_invocations.append(rec)

    skill_counts: Counter = Counter(r.get("skill", "") for r in all_invocations if r.get("skill"))
    total = sum(skill_counts.values())

    most_used = [
        {"skill": s, "count": c, "pct": round(c / total * 100, 1) if total else 0.0}
        for s, c in skill_counts.most_common(20)
    ]

    # Registered base + forged skills
    registered: set = set()
    skills_dir = ctx.paths.project_root / ".claude" / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                registered.add(skill_dir.name)
    forged_path = ctx.paths.world / "forged-skills.yaml"
    if forged_path.exists():
        fd = _read_yaml(forged_path)
        for sk in fd.get("skills", []):
            if isinstance(sk, dict) and sk.get("name"):
                registered.add(sk["name"])
    never_used = sorted(registered - set(skill_counts.keys()))

    # Per-agent heatmap + model-vs-user split (top 30 skills by volume)
    agents_per_skill: Dict[str, Any] = defaultdict(Counter)
    model_vs_user: Dict[str, Any] = defaultdict(Counter)
    for rec in all_invocations:
        skill = rec.get("skill", "")
        agent = rec.get("agent", "")
        source = rec.get("invocation_source", "model")
        if skill and agent:
            agents_per_skill[skill][agent] += 1
        if skill:
            model_vs_user[skill][source] += 1
    top30 = {s for s, _ in skill_counts.most_common(30)}
    agents_per_skill_out = {s: dict(c) for s, c in agents_per_skill.items() if s in top30}
    model_vs_user_out = {s: dict(c) for s, c in model_vs_user.items() if s in top30}

    # Weekly trend for top 10 skills
    weekly: Dict[str, Counter] = defaultdict(Counter)
    for rec in all_invocations:
        ts = rec.get("ts", "")
        skill = rec.get("skill", "")
        if ts and skill:
            try:
                d = datetime.date.fromisoformat(ts[:10])
                week_label = d.strftime("%Y-W%W")
            except ValueError:
                continue
            weekly[week_label][skill] += 1
    top10_skills = [s for s, _ in skill_counts.most_common(10)]
    weekly_out = {
        week: {s: counts.get(s, 0) for s in top10_skills}
        for week, counts in sorted(weekly.items())
    }

    return _out({
        "window_days": window_days,
        "data_start": cutoff,
        "data_end": datetime.date.today().isoformat(),
        "total_invocations": total,
        "most_used": most_used,
        "never_used": never_used,
        "agents_per_skill": agents_per_skill_out,
        "model_vs_user": model_vs_user_out,
        "weekly_trend": weekly_out,
    })


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/skill-analytics/reuse-report")] = reuse_report
    routes[("GET", "/v1/skill-analytics/co-invocation")] = co_invocation
    routes[("GET", "/v1/skill-analytics/coverage")] = coverage
    routes[("GET", "/v1/skill-analytics/recommendations")] = recommendations
    routes[("GET", "/v1/skill-analytics/trend")] = trend
    routes[("GET", "/v1/skill-analytics/usage-report")] = usage_report
