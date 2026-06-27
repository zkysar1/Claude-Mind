"""Strategy-application consumer — parity with core/scripts/strategy-apply.py.

Daemonises the strategy→execution feedback loop (Batch 6). META-scoped: matches
heuristics in goal-selection-strategy.yaml / aspiration-generation-strategy.yaml
against goal keywords, optionally bumps times_applied, and emits a gate-firing
telemetry record.

  POST /v1/meta/strategy-apply/match    {goal_category?,goal_keywords?,phase?,increment?}
  POST /v1/meta/strategy-apply/migrate

FAITHFUL QUIRKS:
  - match output is ALWAYS json.dumps(obj, indent=2) — there is NO --json flag
    (refuted). migrate output is json.dumps(obj) (no indent).
  - match ALWAYS appends a gate-firing record to meta/gate-firings.jsonl via
    `_gate_log.log("strategy-apply", "pass"|"noop", ...)` — a write side-effect
    even on a no-match. The daemon passes meta_dir=ctx.paths.meta + agent_name
    explicitly (the module-level META_DIR / MIND_AGENT in _gate_log is frozen at
    daemon-launch and would route to the wrong agent — exactly the multi-tenant
    case _gate_log.log documents). caller is kept as "strategy-apply.py:main" so
    the gate-firing corpus stays continuous across the cutover.
  - match/migrate write strategy files via locked_write_yaml (CSafeDumper +
    history + changelog) only when a heuristic is dirtied (times_applied added or
    incremented). matched heuristics MISSING times_applied get it backfilled to 0
    even without --increment -> a write occurs.
  - bad --phase -> argparse exit 2 in CLI -> 400 here.

BYTE-COMPATIBILITY:
  - match stdout: json.dumps({"matched","count","keyword_tokens"}, indent=2)+"\n"
    (timestamp-free). migrate stdout: json.dumps({"migrated":n})+"\n".
  - written strategy YAML: CSafeDumper, no timestamps in heuristic data ->
    direct byte-compat.
  - gate-firings.jsonl record carries a `ts` stamp -> the test normalises it;
    agent/session_id are controlled to match the CLI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

import _gate_log  # noqa: E402 — module-level log(); daemon passes meta_dir explicitly

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


# (filename, heuristics_field, phase_name) — keep in sync with CLI STRATEGY_FILES.
STRATEGY_FILES = [
    ("goal-selection-strategy.yaml", "selection_heuristics", "selection"),
    ("aspiration-generation-strategy.yaml", "generation_heuristics", "generation"),
]
_VALID_PHASES = ("selection", "generation", "any")


def _agent_name_or_none(ctx):
    return (ctx.headers.get("x-mind-agent") or "").strip() or None


def _atomic_write_yaml(path: Path, data: Any) -> None:
    assert_not_cruft(path.parent, "mkdir (strategy_apply)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(path, _write, fallback_counter_key="daemon_strategy_apply_write")


def _persist(ctx, path: Path, data: Any) -> None:
    """locked_write_yaml equivalent: CSafeDumper + history + changelog 'edit'."""
    base_dir = ctx.paths.meta
    agent = (ctx.headers.get("x-mind-agent") or "").strip() or "system"
    assert_not_cruft(path.parent, "mkdir (strategy_apply)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_locks.locked(path):
        _validate_no_surrogates(data, path)
        history.snapshot(path, base_dir, agent)
        _atomic_write_yaml(path, data)
        changelog.append(base_dir, agent, path, "edit")


def _load(ctx, file_tuple):
    path = ctx.paths.meta / file_tuple[0]
    if not path.exists():
        return path, None, []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    heuristics = data.get(file_tuple[1], []) or []
    return path, data, heuristics


def _tokenize(text):
    return re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())


def _keyword_match(heuristic, keyword_tokens):
    if not keyword_tokens:
        return False
    desc_tokens = set(_tokenize(heuristic.get("description", "")))
    return any(kw in desc_tokens for kw in keyword_tokens)


def _run_match(ctx, phase_filter, keyword_tokens, increment):
    matched = []
    for file_tuple in STRATEGY_FILES:
        if phase_filter != "any" and phase_filter != file_tuple[2]:
            continue
        path, data, heuristics = _load(ctx, file_tuple)
        if not heuristics:
            continue
        dirty = False
        for h in heuristics:
            if not _keyword_match(h, keyword_tokens):
                continue
            if "times_applied" not in h:
                h["times_applied"] = 0
                dirty = True
            if increment:
                h["times_applied"] = int(h.get("times_applied", 0)) + 1
                dirty = True
            matched.append({
                "id": h.get("id"),
                "description": (h.get("description") or "")[:180],
                "file": file_tuple[0],
                "phase": file_tuple[2],
                "times_applied": h.get("times_applied", 0),
            })
        if dirty:
            data[file_tuple[1]] = heuristics
            _persist(ctx, path, data)
    return matched


def _run_migrate(ctx):
    migrated = 0
    for file_tuple in STRATEGY_FILES:
        path, data, heuristics = _load(ctx, file_tuple)
        if not heuristics:
            continue
        dirty = False
        for h in heuristics:
            if "times_applied" not in h:
                h["times_applied"] = 0
                dirty = True
                migrated += 1
        if dirty:
            data[file_tuple[1]] = heuristics
            _persist(ctx, path, data)
    return migrated


# ---------------------------------------------------------------------------
# POST /v1/meta/strategy-apply/match
# ---------------------------------------------------------------------------

def match(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    goal_category = body.get("goal_category") or ""
    goal_keywords = body.get("goal_keywords") or ""
    phase = body.get("phase") or "any"
    if phase not in _VALID_PHASES:
        return Response.error(400, "invalid_param",
                              "phase must be one of {}".format(list(_VALID_PHASES)))
    increment = bool(body.get("increment"))

    kw_raw = [k.strip() for k in str(goal_keywords).split(",") if k.strip()]
    if goal_category:
        kw_raw.append(goal_category)
    keyword_tokens: List[str] = []
    for kw in kw_raw:
        keyword_tokens.extend(_tokenize(kw))
    keyword_tokens = list(dict.fromkeys(keyword_tokens))  # dedupe, preserve order

    matched = _run_match(ctx, phase, keyword_tokens, increment)

    # gate_id MUST match core/config/gates.yaml id. No block branch — suggests,
    # never refuses. meta_dir + agent_name routed explicitly for multi-tenant.
    _gate_log.log(
        "strategy-apply",
        "pass" if matched else "noop",
        caller="strategy-apply.py:main",
        trigger_matched=(matched[0].get("id") if matched else None),
        payload=",".join(keyword_tokens)[:200],
        extra={"match_count": len(matched), "phase": phase,
               "category": goal_category or None},
        meta_dir=ctx.paths.meta,
        agent_name=_agent_name_or_none(ctx),
    )

    return Response.text(
        json.dumps({"matched": matched, "count": len(matched),
                    "keyword_tokens": keyword_tokens}, indent=2) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/strategy-apply/migrate
# ---------------------------------------------------------------------------

def migrate(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    n = _run_migrate(ctx)
    return Response.text(
        json.dumps({"migrated": n}) + "\n", content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/strategy-apply/match")] = match
    routes[("POST", "/v1/meta/strategy-apply/migrate")] = migrate
