"""Skill-quality-score endpoints — parity with core/scripts/skill-quality-score.py.

Auto-derived skill quality scoring wrapper (Step 8.76). Derives 4 of the 5
quality dimensions mechanically from execution signals; the caller supplies
cost_awareness. META-scoped write to meta/skill-quality.yaml (shared, no header).

  GET  /v1/skill-quality/derive?skill=&goal=&outcomes_met=&...   read
  POST /v1/skill-quality/score   {skill,goal,outcomes_met,...,cost_awareness}  write
       (?dry_run / body dry_run=true -> derive+preview JSON, NO write, NO lock)

ARCHITECTURE: the CLI's `score` acquires a CUSTOM lock then subprocess-calls
skill-evaluate.py score. The daemon INLINES that by calling the sibling
skill_evaluate._score_write (same default-yaml.Dumper raw write, no history/
changelog) under the same custom lock.

LOCK PATH (#1 deployment trap): the CLI uses QUALITY_PATH.with_suffix(
".yaml.lock") = meta/skill-quality.yaml.lock — NOT the standard
path.with_suffix(".lock") that file_locks.locked() computes. Using the wrong
lock path would race daemon writes against CLI writes during migration. This
module holds the threading lock (file_locks.manager) AND the explicit
.yaml.lock file lock (file_locks.acquire_lock with the custom path). A lock
TimeoutError -> HTTP 503 (the CLI lets it propagate; the daemon catches it).

BYTE-COMPATIBILITY:
  - score success stdout = two lines: skill-evaluate's timestamp-free "Scored
    ..." line, then json.dumps({"derived":{...},"llm_supplied":{...}}) (default
    ensure_ascii=True, NO indent). Each + "\n".
  - dry_run stdout = json.dumps({dry_run,skill,goal,derived,llm_supplied},
    indent=2) (ensure_ascii=True) + "\n". No write.
  - derive stdout = json.dumps({safety,completeness,executability,
    maintainability,canonical_skill_name}, indent=2) (ensure_ascii=True) + "\n".
  - bad skill name -> ValueError -> HTTP 400 (CLI return 1).

canonicalize + derive_maintainability are rebuilt PER-REQUEST from ctx.paths
(.claude/skills via project_root, forged registry via world) — never cached
as a module global (would freeze at first-request state and miss new skills).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from . import skill_evaluate
from .. import file_locks

GRADES = ("good", "average", "poor")


def _skills_dir(ctx) -> Path:
    return ctx.paths.project_root / ".claude" / "skills"


def _forged_registry(ctx) -> Path:
    return ctx.paths.world / "forged-skills.yaml"


def _load_canonical_names(ctx):
    names = set()
    skills_dir = _skills_dir(ctx)
    if skills_dir.is_dir():
        for entry in skills_dir.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
    try:
        reg = _forged_registry(ctx)
        if reg.exists():
            with open(reg, encoding="utf-8") as f:
                fd = yaml.safe_load(f) or {}
            forged = fd.get("skills") or {}
            if isinstance(forged, dict):
                names.update(forged.keys())
    except Exception:
        pass  # forged filter is best-effort; never block scoring
    return names


def _canonicalize_skill_name(ctx, raw: str) -> str:
    """Normalize to canonical key. Raises ValueError on miss (CLI return 1)."""
    canonical = _load_canonical_names(ctx)
    if not raw or not raw.strip():
        raise ValueError("empty skill name")
    name = raw.lstrip("/").split(None, 1)[0]
    if not name:
        raise ValueError("skill name empty after stripping (raw={!r})".format(raw))
    if name not in canonical:
        raise ValueError(
            "skill name {!r} (from raw={!r}) not in canonical list of {} known "
            "skills. Check .claude/skills/ and world/forged-skills.yaml. If this "
            "is a new skill, register it before scoring.".format(name, raw, len(canonical)))
    return name


def _grade_from_ratio(met: int, total: int) -> str:
    if total <= 0:
        return "good"
    ratio = met / total
    if ratio >= 1.0:
        return "good"
    if ratio >= 0.5:
        return "average"
    return "poor"


def _grade_from_episodes(count: int) -> str:
    if count <= 0:
        return "good"
    if count == 1:
        return "average"
    return "poor"


def _grade_from_violations(count: int) -> str:
    if count <= 0:
        return "good"
    if count <= 2:
        return "average"
    return "poor"


def _derive_maintainability(ctx, skill_name: str) -> str:
    try:
        reg_path = _forged_registry(ctx)
        if not reg_path.exists():
            return "good"
        with open(reg_path, encoding="utf-8") as f:
            reg = yaml.safe_load(f) or {}
    except Exception:
        return "good"
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
        return "good"
    for key in ("maintainability", "quality_at_forge", "quality"):
        v = entry.get(key)
        if v in GRADES:
            return v
    return "good"


def _int(val, default=0):
    if val is None or val == "":
        return default
    return int(val)


def _truthy(v) -> bool:
    return v is not None and str(v).lower() not in ("", "0", "false", "no")


# ---------------------------------------------------------------------------
# GET /v1/skill-quality/derive
# ---------------------------------------------------------------------------

def derive(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    skill = ctx.query.get("skill") or ""
    try:
        outcomes_met = _int(ctx.query.get("outcomes_met"))
        outcomes_total = _int(ctx.query.get("outcomes_total"))
        episode_chain_count = _int(ctx.query.get("episode_chain_count"))
        guardrail_violations = _int(ctx.query.get("guardrail_violations"))
    except ValueError:
        return Response.error(400, "invalid_param", "signal counts must be integers")
    try:
        canonical = _canonicalize_skill_name(ctx, skill)
    except ValueError as e:
        return Response.error(400, "invalid_skill", str(e))
    out = {
        "safety": _grade_from_violations(guardrail_violations),
        "completeness": _grade_from_ratio(outcomes_met, outcomes_total),
        "executability": _grade_from_episodes(episode_chain_count),
        "maintainability": _derive_maintainability(ctx, canonical),
        "canonical_skill_name": canonical,
    }
    return Response.text(json.dumps(out, indent=2) + "\n", content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/skill-quality/score
# ---------------------------------------------------------------------------

def score(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    skill_raw = body.get("skill") or ""
    goal = (body.get("goal") or "").strip()
    if not goal:
        return Response.error(400, "missing_param", "goal is required")

    cost_awareness = body.get("cost_awareness")
    if cost_awareness is None:
        cost_awareness = body.get("cost-awareness")
    if cost_awareness not in GRADES:
        return Response.error(
            400, "invalid_grade",
            "cost_awareness must be one of good/average/poor (got {!r})".format(cost_awareness))

    try:
        outcomes_met = _int(body.get("outcomes_met"))
        outcomes_total = _int(body.get("outcomes_total"))
        episode_chain_count = _int(body.get("episode_chain_count"))
        guardrail_violations = _int(body.get("guardrail_violations"))
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "signal counts must be integers")

    # Optional dimension overrides (validated when present).
    overrides = {}
    for dim in ("safety", "completeness", "executability", "maintainability"):
        ov = body.get(dim + "_override")
        if ov is not None:
            if ov not in GRADES:
                return Response.error(
                    400, "invalid_grade",
                    "{}_override must be one of good/average/poor".format(dim))
            overrides[dim] = ov

    try:
        skill = _canonicalize_skill_name(ctx, skill_raw)
    except ValueError as e:
        return Response.error(400, "invalid_skill", str(e))

    safety = overrides.get("safety") or _grade_from_violations(guardrail_violations)
    completeness = overrides.get("completeness") or _grade_from_ratio(outcomes_met, outcomes_total)
    executability = overrides.get("executability") or _grade_from_episodes(episode_chain_count)
    maintainability = overrides.get("maintainability") or _derive_maintainability(ctx, skill)

    derived = {
        "safety": safety,
        "completeness": completeness,
        "executability": executability,
        "maintainability": maintainability,
    }

    if _truthy(body.get("dry_run")):
        preview = {
            "dry_run": True,
            "skill": skill,
            "goal": goal,
            "derived": dict(derived),
            "llm_supplied": {"cost_awareness": cost_awareness},
        }
        return Response.text(json.dumps(preview, indent=2) + "\n",
                             content_type="application/json")

    grades = dict(derived)
    grades["cost_awareness"] = cost_awareness

    # Custom lock path matching the CLI: meta/skill-quality.yaml.lock (NOT the
    # standard .lock). Threading lock + explicit .yaml.lock file lock.
    quality_path = ctx.paths.meta / "skill-quality.yaml"
    lock_path = quality_path.with_suffix(".yaml.lock")
    thread_lock = file_locks.manager().get(quality_path)
    thread_lock.acquire()
    try:
        try:
            file_locks.acquire_lock(lock_path, timeout=15, stale_seconds=60)
        except TimeoutError:
            return Response.error(503, "lock_timeout",
                                  "skill-quality.yaml lock busy; try again")
        try:
            # Judge provenance is forwarded from the caller's body; the writer
            # must not read it from this long-lived process's environment
            # (guard-2480, ).
            scored_line = skill_evaluate._score_write(
                ctx, skill, goal, grades,
                body.get("judge_model"), body.get("harness"))
        finally:
            file_locks.release_lock(lock_path)
    finally:
        thread_lock.release()

    trailer = json.dumps({
        "derived": dict(derived),
        "llm_supplied": {"cost_awareness": cost_awareness},
    })
    return Response.text(scored_line + "\n" + trailer + "\n", content_type="text/plain")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/skill-quality/derive")] = derive
    routes[("POST", "/v1/skill-quality/score")] = score
