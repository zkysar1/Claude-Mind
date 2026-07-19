"""Skill-relation graph endpoints — parity with core/scripts/skill-relations.py.

Daemonises all 4 skill-relations commands (Batch 6):

  GET  /v1/skill-relations/read       (cmd_read)       ?skill=&type=&composable=&similar=
  POST /v1/skill-relations/add        (cmd_add)        body: JSON relation
  POST /v1/skill-relations/co-invoke  (cmd_co_invoke)  ?goal=&skills=
  GET  /v1/skill-relations/discover   (cmd_discover)

SCOPE: WORLD. Base (immutable) relations live in
core/config/skill-relations.yaml under `relations`; forged relations +
co_invocation_log live in world/skill-relations.yaml. No X-Mind-Agent header.

BYTE-COMPATIBILITY:
  - read/discover (stdout): json.dumps(x, indent=2, ensure_ascii=False) + "\n"
    (skill-relations.py:121/229/273). read merges base_relations + forged_relations
    (BASE FIRST). discover on empty log emits "[]\n" early.
  - add/co-invoke writes: world/skill-relations.yaml via _write_yaml ->
    yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
    sort_keys=False) with the DEFAULT yaml.Dumper (NOT CSafeDumper) and NO
    .history/changelog (skill-relations.py:66-72). Serialization bytes are
    replicated EXACTLY; the WRITE MECHANISM routes through
    _fileops._atomic_write_with_fallback -> get_backend().atomic_write since
    g-001-42 (was raw tmp+os.replace — the last multi-agent RMW daemon writer
    bypassing the backend, g-115-1948), plus file_locks.locked() for
    daemon-process concurrency (byte-neutral — the lock file is transient;
    the wm_write precedent). Success stdout is
    PLAIN TEXT, not JSON: add -> "Added relation: {s} --{t}--> {tgt}\n"
    (line 186); co-invoke -> "Logged co-invocation: {n} skills for goal {g}\n"
    (line 218).

MULTI-TENANT CONFIG: the CLI's _load_config caches the `config` section in a
function-level attribute (skill-relations.py:44). In the long-running daemon
that would pin the FIRST request's project_root config forever, so this module
reads core/config/skill-relations.yaml fresh per request via
ctx.paths.project_root. Byte-neutral (the config VALUES are identical); only the
caching is dropped. co_invocation_log_cap (default 200) and
discover_min_co_occurrences (default 3) come from that `config` section.

sys.exit MAPPING (all bad-input -> 400): add has 6 (stdin-tty/empty-body,
invalid JSON, missing field, invalid type, duplicate); co-invoke has 1 (<2
skills). read/discover have 0. The module-level PyYAML ImportError exit is
irrelevant in the daemon (yaml already imported).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import file_locks
from ..agent_paths import assert_not_cruft

VALID_TYPES = {"similar_to", "compose_with", "belong_to", "depend_on"}


def _world_path(ctx) -> Path:
    return ctx.paths.world / "skill-relations.yaml"


def _config_path(ctx) -> Path:
    return ctx.paths.project_root / "core" / "config" / "skill-relations.yaml"


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Mirror skill-relations.py:read_yaml (line 57). {} when missing."""
    from storage_backend import get_backend
    get_backend().ensure_local(path)  # own-cloud read-path fix: materialize S3-only file before local read; no-op on LocalBackend and out-of-root paths
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _config(ctx) -> Dict[str, Any]:
    """Read the `config` section fresh (no function-level cache — multi-tenant)."""
    return _read_yaml(_config_path(ctx)).get("config", {}) or {}


def _base_relations(ctx) -> List[Dict[str, Any]]:
    base = _read_yaml(_config_path(ctx)).get("relations", [])
    return base if isinstance(base, list) else []


def _forged_relations(ctx) -> List[Dict[str, Any]]:
    forged = _read_yaml(_world_path(ctx)).get("forged_relations", [])
    return forged if isinstance(forged, list) else []


def _load_all_relations(ctx) -> List[Dict[str, Any]]:
    """Mirror skill-relations.py:load_all_relations — base FIRST, then forged."""
    return _base_relations(ctx) + _forged_relations(ctx)


def _write_yaml(path: Path, data: Any) -> None:
    """Byte-identical SERIALIZATION to skill-relations.py:write_yaml (default
    Dumper — NOT CSafeDumper — default_flow_style=False, allow_unicode=True,
    sort_keys=False). The WRITE MECHANISM routes through
    _fileops._atomic_write_with_fallback -> get_backend().atomic_write
    (g-001-42): the previous raw tmp+os.replace was the last multi-agent RMW
    daemon writer bypassing the storage backend (g-115-1948 enumeration), so
    world/skill-relations.yaml reached S3 only at the periodic sweep and the
    stale-baseline no_clobber window applied (loss-lane class). Backend
    routing also stamps the manifest baseline at put (g-115-1946). Caller
    holds file_locks.locked() for daemon concurrency."""
    from _fileops import _atomic_write_with_fallback
    assert_not_cruft(path.parent, "mkdir (skill_relations)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, default_flow_style=False, allow_unicode=True,
                  sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_skill_relations_write")


# ---------------------------------------------------------------------------
# GET /v1/skill-relations/read
# ---------------------------------------------------------------------------

def read(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/skill-relations/read?skill=&type=&composable=&similar= (AND-stacked)."""
    from ..server import Response

    relations = _load_all_relations(ctx)
    q = ctx.query

    skill = q.get("skill")
    if skill:
        relations = [r for r in relations
                     if r.get("source") == skill or r.get("target") == skill]
    rtype = q.get("type")
    if rtype:
        relations = [r for r in relations if r.get("type") == rtype]
    composable = q.get("composable")
    if composable:
        relations = [r for r in relations
                     if r.get("type") == "compose_with" and r.get("source") == composable]
    similar = q.get("similar")
    if similar:
        relations = [r for r in relations
                     if r.get("type") == "similar_to"
                     and (r.get("source") == similar or r.get("target") == similar)]

    return Response.text(
        json.dumps(relations, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/skill-relations/add
# ---------------------------------------------------------------------------

def add(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/skill-relations/add  body: JSON relation {source,target,type,[confidence],[evidence]}."""
    from ..server import Response

    if not ctx.body:
        return Response.error(400, "empty_input", "expected JSON on body")
    try:
        entry = json.loads(ctx.body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        return Response.error(400, "invalid_json", f"invalid JSON: {e}")
    if not isinstance(entry, dict):
        return Response.error(400, "invalid_json", "body must be a JSON object")

    for field in ("source", "target", "type"):
        if field not in entry:
            return Response.error(400, "missing_field", f"missing required field '{field}'")

    rel_type = entry["type"]
    if rel_type not in VALID_TYPES:
        return Response.error(
            400, "invalid_type",
            f"invalid type '{rel_type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}")

    source = entry["source"]
    target = entry["target"]
    world_path = _world_path(ctx)

    try:
        assert_not_cruft(world_path.parent, "mkdir (skill_relations)")
        world_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(world_path):
            world_data = _read_yaml(world_path)
            forged = world_data.get("forged_relations", [])
            if not isinstance(forged, list):
                forged = []
            for existing in forged:
                if (existing.get("source") == source
                        and existing.get("target") == target
                        and existing.get("type") == rel_type):
                    return Response.error(
                        400, "duplicate_relation",
                        f"duplicate relation already exists: {source} --{rel_type}--> {target}")

            relation: Dict[str, Any] = {"source": source, "target": target, "type": rel_type}
            if "confidence" in entry:
                relation["confidence"] = entry["confidence"]
            if "evidence" in entry:
                relation["evidence"] = entry["evidence"]

            forged.append(relation)
            world_data["forged_relations"] = forged
            _write_yaml(world_path, world_data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.text(
        f"Added relation: {source} --{rel_type}--> {target}\n",
        content_type="text/plain")


# ---------------------------------------------------------------------------
# POST /v1/skill-relations/co-invoke
# ---------------------------------------------------------------------------

def co_invoke(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/skill-relations/co-invoke?goal=<id>&skills=<comma-separated>."""
    from ..server import Response

    goal_id = ctx.query.get("goal")
    if not goal_id:
        return Response.error(400, "missing_param", "query parameter 'goal' required")
    skills_raw = ctx.query.get("skills")
    if skills_raw is None:
        return Response.error(400, "missing_param", "query parameter 'skills' required")
    skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
    if len(skills) < 2:
        return Response.error(400, "too_few_skills", "co-invoke requires at least 2 skills")

    world_path = _world_path(ctx)
    cap = _config(ctx).get("co_invocation_log_cap", 200)

    try:
        assert_not_cruft(world_path.parent, "mkdir (skill_relations)")
        world_path.parent.mkdir(parents=True, exist_ok=True)
        with file_locks.locked(world_path):
            world_data = _read_yaml(world_path)
            log = world_data.get("co_invocation_log", [])
            if not isinstance(log, list):
                log = []
            log.append({
                "goal_id": goal_id,
                "skills": skills,
                "date": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })
            if len(log) > cap:
                log = log[-cap:]
            world_data["co_invocation_log"] = log
            _write_yaml(world_path, world_data)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.text(
        f"Logged co-invocation: {len(skills)} skills for goal {goal_id}\n",
        content_type="text/plain")


# ---------------------------------------------------------------------------
# GET /v1/skill-relations/discover
# ---------------------------------------------------------------------------

def discover(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/skill-relations/discover — propose compose_with from co-invocation."""
    from ..server import Response

    log = _read_yaml(_world_path(ctx)).get("co_invocation_log", [])
    if not isinstance(log, list):
        log = []

    if not log:
        return Response.text(
            json.dumps([], indent=2, ensure_ascii=False) + "\n",
            content_type="application/json")

    pair_counts: Counter = Counter()
    for entry in log:
        skills = entry.get("skills", [])
        if not isinstance(skills, list) or len(skills) < 2:
            continue
        for pair in combinations(sorted(set(skills)), 2):
            pair_counts[pair] += 1

    total_invocations = len(log)

    existing_compose = set()
    for r in _load_all_relations(ctx):
        if r.get("type") == "compose_with":
            s, t = r.get("source", ""), r.get("target", "")
            existing_compose.add((min(s, t), max(s, t)))

    min_co = _config(ctx).get("discover_min_co_occurrences", 3)
    proposed: List[Dict[str, Any]] = []
    for (skill_a, skill_b), count in pair_counts.most_common():
        if count < min_co:
            continue
        normalized = (min(skill_a, skill_b), max(skill_a, skill_b))
        if normalized in existing_compose:
            continue
        confidence = round(count / total_invocations, 3) if total_invocations > 0 else 0
        proposed.append({
            "source": skill_a,
            "target": skill_b,
            "type": "compose_with",
            "confidence": confidence,
            "co_invocation_count": count,
            "evidence": f"Co-invoked {count} times across {total_invocations} logged invocations",
        })

    return Response.text(
        json.dumps(proposed, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("GET", "/v1/skill-relations/read")] = read
    routes[("POST", "/v1/skill-relations/add")] = add
    routes[("POST", "/v1/skill-relations/co-invoke")] = co_invoke
    routes[("GET", "/v1/skill-relations/discover")] = discover
