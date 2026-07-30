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

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

# core/scripts imports MUST follow the relative imports above — file_locks
# installs core/scripts on sys.path at module load (sibling-module pattern,
# e.g. meta_backpressure). _gate_log ahead of them ModuleNotFoundError'd on
# any isolated import (tests); the real daemon only worked because another
# module happened to load first ().
import _gate_log  # noqa: E402 — module-level log(); daemon passes meta_dir explicitly
from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402
from coordination_merge import merge_handler_for  # noqa: E402 — write-class classifier (guard-1733)


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


def _persist_unlocked(ctx, path: Path, data: Any) -> None:
    """_persist's body WITHOUT the lock, for callers already inside a
    locked_rmw cycle. file_locks.locked is NOT reentrant (a plain
    threading.Lock), so nesting it inside locked_rmw deadlocks the daemon
    thread. Mirrors meta_yaml._persist_unlocked."""
    base_dir = ctx.paths.meta
    agent = (ctx.headers.get("x-mind-agent") or "").strip() or "system"
    assert_not_cruft(path.parent, "mkdir (strategy_apply)")
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_surrogates(data, path)
    history.snapshot(path, base_dir, agent)
    _atomic_write_yaml(path, data)
    changelog.append(base_dir, agent, path, "edit")


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


def _load(ctx, file_tuple, force_fresh: bool = False):
    path = ctx.paths.meta / file_tuple[0]
    from storage_backend import get_backend
    if force_fresh:
        # Force-pull the latest remote object AND record its ETag as the
        # If-Match fence, so a locked_rmw retry re-reads the peer's landed write
        # and re-fences against the etag the remote actually holds. A cache-TTL
        # read inside a retry loop re-fences against an etag the remote no
        # longer has and the 412 repeats forever (rb-2639).
        get_backend().refresh(path)
    else:
        get_backend().ensure_local(path)  # own-cloud read-path fix 2026-07-02: materialize an S3-only file on a fresh box before the local read; no-op on LocalBackend and for out-of-root/git-shipped paths (keystone in owncloud_backend._refresh)
    if not path.exists():
        return path, None, []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    heuristics = data.get(file_tuple[1], []) or []
    return path, data, heuristics


def _apply_to_strategy_file(ctx, file_tuple, mutate):
    """Read→mutate→write ONE strategy file under the locking discipline that
    file's write class actually requires, and return whatever `mutate` reports.

    STRATEGY_FILES is MIXED-CLASS (g-115-3834, measured — do not re-derive from
    shape). Both entries are `*-strategy.yaml` in the same directory, driven by
    the same loop, through the same helper, and they need OPPOSITE treatment:

        goal-selection-strategy.yaml         (a) MERGE-protected -> the bare
            lock is correct; merge_goal_selection_strategy reconciles below the
            write, so a concurrent writer is merged rather than refused.
        aspiration-generation-strategy.yaml  (b) fence-only -> needs locked_rmw
            with a fresh in-cycle read; nothing reconciles below the write, an
            unlocked read + bare-locked write silently drops the peer's
            increment, and a stale If-Match fence is a PERMANENT per-object
            wedge rather than a transient miss (rb-2639).

    That is why the class is looked up per BASENAME here at run time via the
    registry, rather than inferred from the loop, the module, the directory, or
    a sibling file — the inference guard-1733 exists to forbid. Deriving it from
    merge_handler_for also means a future registry change re-routes this code
    automatically instead of silently invalidating a hardcoded assumption.

    `mutate(heuristics) -> (dirty, result)` mutates the list IN PLACE and
    reports whether a write is needed plus whatever the caller accumulates. It
    is re-invoked from scratch on every locked_rmw retry against a freshly-read
    list, so it MUST NOT append into an enclosing collection — return it.
    """
    path = ctx.paths.meta / file_tuple[0]

    if merge_handler_for(file_tuple[0]) is not None:
        # Class (a) — deliberately left on the bare lock. See docstring.
        _p, data, heuristics = _load(ctx, file_tuple)
        if not heuristics:
            return None
        dirty, result = mutate(heuristics)
        if dirty:
            data[file_tuple[1]] = heuristics
            _persist(ctx, path, data)
        return result

    # Class (b) — cured: the read moves INSIDE the lock and the whole cycle
    # re-runs on a conflict, re-applying the mutation over the peer's write.
    def _cycle():
        _p, data, heuristics = _load(ctx, file_tuple, force_fresh=True)
        if not heuristics:
            return None
        dirty, result = mutate(heuristics)
        if dirty:
            data[file_tuple[1]] = heuristics
            _persist_unlocked(ctx, path, data)
        return result

    return file_locks.locked_rmw(path, _cycle)


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

        def _mutate(heuristics, _ft=file_tuple):
            # Built per attempt and RETURNED, never appended into `matched`
            # directly: on a locked_rmw retry this runs again against a freshly
            # read list, and an enclosing append would duplicate every match.
            dirty = False
            local = []
            for h in heuristics:
                if not _keyword_match(h, keyword_tokens):
                    continue
                if "times_applied" not in h:
                    h["times_applied"] = 0
                    dirty = True
                if increment:
                    # Reads from the freshly-read record on a retry, so a peer's
                    # concurrent increment is added to rather than overwritten.
                    h["times_applied"] = int(h.get("times_applied", 0)) + 1
                    dirty = True
                local.append({
                    "id": h.get("id"),
                    "description": (h.get("description") or "")[:180],
                    "file": _ft[0],
                    "phase": _ft[2],
                    "times_applied": h.get("times_applied", 0),
                })
            return dirty, local

        result = _apply_to_strategy_file(ctx, file_tuple, _mutate)
        if result:
            matched.extend(result)
    return matched


def _run_migrate(ctx):
    migrated = 0
    for file_tuple in STRATEGY_FILES:

        def _mutate(heuristics):
            dirty = False
            count = 0
            for h in heuristics:
                if "times_applied" not in h:
                    h["times_applied"] = 0
                    dirty = True
                    count += 1
            return dirty, count

        result = _apply_to_strategy_file(ctx, file_tuple, _mutate)
        migrated += result or 0
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
