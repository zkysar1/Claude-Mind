"""Skill-evaluate endpoints — parity with core/scripts/skill-evaluate.py.

Daemonises all 4 skill-evaluate subcommands (Batch 6). META-scoped
(meta/skill-quality.yaml). No X-Mind-Agent gate — the file is shared across
agents and the CLI write path has zero agent attribution.

  GET  /v1/skill-evaluate/read?skill=&all=1&summary=1   read
  GET  /v1/skill-evaluate/report                        read
  GET  /v1/skill-evaluate/underperforming?threshold=    read
  POST /v1/skill-evaluate/score                          write

WRITE path (score) — three deviations from the standard _fileops pattern, all
load-bearing for byte-compat (verified against CLI write_yaml lines 62-68):
  1. DEFAULT yaml.Dumper (NOT yaml.CSafeDumper). CSafeDumper differs on some
     types — the #1 byte-compat trap. yaml.dump(...) with no Dumper= arg.
  2. RAW tmp+replace — NO file lock, NO .history snapshot, NO changelog append.
     Adding any would diverge from the CLI and create artefacts it never makes.
  3. tmp suffix is path.with_suffix(".yaml.tmp") = skill-quality.yaml.tmp.

score stdout is human TEXT ("Scored {skill}: overall {o:.2f} (...)"), not JSON.
read/report/underperforming print json.dumps(obj, indent=2, ensure_ascii=False)
+ "\n" — ensure_ascii=False is load-bearing. read's skill-not-found returns an
error JSON object at HTTP 200 (CLI prints it and returns normally, NOT a 404).

Only sys.exit is the PyYAML import guard (unreachable in the daemon). The two
datetime.now() stamps (entry.date, data.last_updated) make the WRITTEN FILE
non-deterministic; the test normalises them. The score STDOUT carries no
timestamp so it is byte-identical without normalisation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROLLING_WINDOW = 20
GRADE_MAP = {"good": 1.0, "average": 0.5, "poor": 0.0}
DIMENSIONS = ["safety", "completeness", "executability", "maintainability", "cost_awareness"]
DEFAULT_WEIGHTS = {
    "safety": 0.30, "completeness": 0.25, "executability": 0.20,
    "maintainability": 0.15, "cost_awareness": 0.10,
}


def _quality_path(ctx) -> Path:
    return ctx.paths.meta / "skill-quality.yaml"


def _strategy_path(ctx) -> Path:
    return ctx.paths.meta / "skill-quality-strategy.yaml"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _write_yaml(path: Path, data) -> None:
    """RAW write, DEFAULT yaml.Dumper, no lock/history/changelog (CLI 62-68)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _load_weights(ctx) -> Dict[str, float]:
    data = _read_yaml(_strategy_path(ctx))
    raw = data.get("dimension_weights")
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    weights = {}
    for dim in DIMENSIONS:
        weights[dim] = float(raw.get(dim, DEFAULT_WEIGHTS.get(dim, 0.0)))
    return weights


def _compute_overall(scores, weights):
    total = 0.0
    for dim in DIMENSIONS:
        total += scores.get(dim, 0.0) * weights.get(dim, 0.0)
    return round(total, 4)


def _compute_aggregate(evaluations, weights):
    if not evaluations:
        return {dim: 0.0 for dim in DIMENSIONS + ["overall"]}
    count = len(evaluations)
    sums = {dim: 0.0 for dim in DIMENSIONS}
    for entry in evaluations:
        for dim in DIMENSIONS:
            sums[dim] += entry.get(dim, 0.0)
    means = {dim: round(sums[dim] / count, 4) for dim in DIMENSIONS}
    means["overall"] = _compute_overall(means, weights)
    return means


def _out(obj: Any) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Read handlers
# ---------------------------------------------------------------------------

def read(ctx) -> "Response":  # type: ignore[name-defined]
    data = _read_yaml(_quality_path(ctx))
    skills = data.get("skills", {})
    skill = ctx.query.get("skill")
    if skill:
        skill_data = skills.get(skill)
        if skill_data is None:
            return _out({"error": "Skill '{}' not found".format(skill)})
        return _out(skill_data)

    def _flag(name):
        v = ctx.query.get(name)
        return v is not None and str(v).lower() not in ("", "0", "false", "no")

    if _flag("all") and _flag("summary"):
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
            })
        return _out(summary)

    return _out(data)


def report(ctx) -> "Response":  # type: ignore[name-defined]
    data = _read_yaml(_quality_path(ctx))
    skills = data.get("skills", {})
    if not skills:
        return _out({
            "skills": {},
            "summary": {"total_skills_evaluated": 0, "avg_overall": 0.0,
                        "min_overall": 0.0, "max_overall": 0.0},
            "alerts": [],
        })
    skills_agg = {}
    overalls = []
    alerts = []
    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        skills_agg[name] = agg
        overall = agg.get("overall", 0.0)
        overalls.append(overall)
        low_dims = [dim for dim in DIMENSIONS if agg.get(dim, 0.0) < 0.30]
        if low_dims:
            alerts.append({
                "skill": name,
                "dimensions_below_030": low_dims,
                "values": {dim: agg.get(dim, 0.0) for dim in low_dims},
            })
    return _out({
        "skills": skills_agg,
        "summary": {
            "total_skills_evaluated": len(skills),
            "avg_overall": round(sum(overalls) / len(overalls), 4) if overalls else 0.0,
            "min_overall": round(min(overalls), 4) if overalls else 0.0,
            "max_overall": round(max(overalls), 4) if overalls else 0.0,
        },
        "alerts": alerts,
    })


def underperforming(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    threshold_raw = ctx.query.get("threshold")
    if threshold_raw is None or threshold_raw == "":
        threshold = 0.50
    else:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            return Response.error(400, "invalid_param", "threshold must be a float")
    data = _read_yaml(_quality_path(ctx))
    skills = data.get("skills", {})
    results = []
    for name, sdata in skills.items():
        agg = sdata.get("aggregate", {})
        overall = agg.get("overall", 0.0)
        dims_below = []
        for dim in DIMENSIONS:
            val = agg.get(dim, 0.0)
            if val < threshold:
                dims_below.append({"dimension": dim, "value": val})
        if overall < threshold or dims_below:
            results.append({
                "skill": name,
                "overall": overall,
                "dimensions_below": dims_below,
                "total_evaluations": sdata.get("total_evaluations", 0),
            })
    results.sort(key=lambda x: x["overall"])
    return _out(results)


# ---------------------------------------------------------------------------
# Write handler: score
# ---------------------------------------------------------------------------

def _score_write(ctx, skill, goal, grades):
    """Inlined cmd_score logic. Returns the 'Scored ...' stdout line."""
    data = _read_yaml(_quality_path(ctx))
    weights = _load_weights(ctx)
    scores = {dim: GRADE_MAP[grades[dim]] for dim in DIMENSIONS}
    overall = _compute_overall(scores, weights)
    entry = {
        "goal_id": goal,
        "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "safety": scores["safety"],
        "completeness": scores["completeness"],
        "executability": scores["executability"],
        "maintainability": scores["maintainability"],
        "cost_awareness": scores["cost_awareness"],
        "overall": overall,
    }
    if "skills" not in data:
        data["skills"] = {}
    if skill not in data["skills"]:
        data["skills"][skill] = {"evaluations": [], "aggregate": {}, "total_evaluations": 0}
    skill_data = data["skills"][skill]
    evals = skill_data.get("evaluations", [])
    evals.append(entry)
    if len(evals) > ROLLING_WINDOW:
        evals = evals[-ROLLING_WINDOW:]
    skill_data["evaluations"] = evals
    skill_data["aggregate"] = _compute_aggregate(evals, weights)
    skill_data["total_evaluations"] = skill_data.get("total_evaluations", 0) + 1
    data["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _write_yaml(_quality_path(ctx), data)
    return "Scored {skill}: overall {overall:.2f} (S:{s} C:{c} E:{e} M:{m} $:{ca})".format(
        skill=skill, overall=overall, s=grades["safety"], c=grades["completeness"],
        e=grades["executability"], m=grades["maintainability"], ca=grades["cost_awareness"])


def score(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    skill = (body.get("skill") or "").strip()
    goal = (body.get("goal") or "").strip()
    if not skill or not goal:
        return Response.error(400, "missing_param", "skill and goal are required")

    grades = {}
    for dim in DIMENSIONS:
        # Accept hyphen form for cost-awareness for robustness.
        val = body.get(dim)
        if val is None and dim == "cost_awareness":
            val = body.get("cost-awareness")
        if val not in GRADE_MAP:
            return Response.error(
                400, "invalid_grade",
                "{} must be one of good/average/poor (got {!r})".format(dim, val))
        grades[dim] = val

    line = _score_write(ctx, skill, goal, grades)
    return Response.text(line + "\n", content_type="text/plain")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/skill-evaluate/read")] = read
    routes[("GET", "/v1/skill-evaluate/report")] = report
    routes[("GET", "/v1/skill-evaluate/underperforming")] = underperforming
    routes[("POST", "/v1/skill-evaluate/score")] = score
