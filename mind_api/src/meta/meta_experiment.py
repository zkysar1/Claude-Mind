"""A/B experiment lifecycle endpoints — parity with core/scripts/meta-experiment.py.

Daemonises the 4 meta-experiment subcommands (Batch 6). META-scoped
(meta/experiments/{active,completed}-experiments.yaml). No X-Mind-Agent gate
(agent-agnostic); changelog attribution uses the header defaulting to "system".

  POST /v1/meta/experiment/create   {strategy,field,baseline,variant}   write
  GET  /v1/meta/experiment/status?id=<id>                                read
  POST /v1/meta/experiment/resolve  {id}                                 write
  GET  /v1/meta/experiment/list?completed=1                              read

WRITE mechanism: _fileops.locked_write_yaml (CSafeDumper + history snapshot +
changelog "edit", NO summary). Replicated here via the daemon file_locks/
history/changelog helpers with base_dir = ctx.paths.meta (resolve_base_dir
returns META_DIR for meta/experiments/* in the CLI). create writes ONE file;
resolve writes BOTH active + completed in TWO separate locked cycles (NOT
atomic across files — faithful to CLI lines 156-157).

BYTE-COMPATIBILITY:
  - create stdout: json.dumps({"status":"created","id","strategy","field"})
    (default ensure_ascii=True, no indent) + "\n". Timestamp-free.
  - resolve stdout: json.dumps({"status":"resolved","id","outcome",
    "delta":round(delta,6)}) + "\n". Timestamp-free (delta is deterministic).
  - status/list stdout: json.dumps(obj, ensure_ascii=False, default=str) + "\n"
    (ensure_ascii=False + default=str are both load-bearing).
  - The written YAML carries datetime.now() stamps (created/resolved) -> the
    test normalises them. Missing file -> empty list (status/list NOT 404).

sys.exit MAPPING: max_concurrent exceeded -> 409 (CLI exit 1, msg to stderr);
experiment-not-found (status/resolve) -> 404; bad float baseline/variant -> 400;
missing required create params -> 400. The PyYAML import guard is unreachable.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import file_locks, history, changelog
from ..agent_paths import assert_not_cruft

from _fileops import _atomic_write_with_fallback, _validate_no_surrogates  # noqa: E402


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _active_path(ctx) -> Path:
    return ctx.paths.meta / "experiments" / "active-experiments.yaml"


def _completed_path(ctx) -> Path:
    return ctx.paths.meta / "experiments" / "completed-experiments.yaml"


def _config(ctx) -> Dict[str, Any]:
    return _read_yaml(ctx.paths.project_root / "core" / "config" / "meta.yaml")


def _read_yaml(path: Path, force_fresh: bool = False) -> Dict[str, Any]:
    """force_fresh=True force-pulls the latest remote object AND records its
    ETag as the If-Match fence token, so a locked_rmw retry re-reads the peer's
    landed write and re-fences against the etag the remote actually holds. A
    cache-TTL read inside a retry loop re-fences against an etag the remote no
    longer has, and the 412 then repeats forever against a remote that never
    changes — the per-object stale-IfMatch DEADLOCK (rb-2639). Mirrors
    meta_yaml._read_yaml."""
    from storage_backend import get_backend
    if force_fresh:
        get_backend().refresh(path)  # force-pull latest + set If-Match fence (rb-2639)
    else:
        get_backend().ensure_local(path)  # own-cloud read-path fix 2026-07-02: materialize an S3-only file on a fresh box before the local read; no-op on LocalBackend and for out-of-root/git-shipped paths (keystone in owncloud_backend._refresh)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """Byte-identical to _fileops.locked_write_yaml's inner write (CSafeDumper)."""
    assert_not_cruft(path.parent, "mkdir (meta_experiment)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                  default_flow_style=False, allow_unicode=True, sort_keys=False)

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_meta_experiment_write")


def _persist_unlocked(ctx, path: Path, data: Any) -> None:
    """_persist's body WITHOUT the lock, for callers already inside a
    locked_rmw cycle. file_locks.locked is NOT reentrant (a plain
    threading.Lock), so nesting it inside locked_rmw deadlocks the daemon
    thread. Mirrors meta_yaml._persist_unlocked.

    WHY these two files need the locked_rmw treatment (g-115-3834, measured —
    do not re-derive from shape): coordination_merge.merge_handler_for returns
    None for BOTH active-experiments.yaml and completed-experiments.yaml, so
    both are write-class (b) FENCE-ONLY — nothing reconciles below the write,
    and a stale If-Match fence is a PERMANENT per-object wedge rather than a
    transient miss. Classify by PATH via that one lookup, never by the sibling
    module or the enclosing directory (guard-1733)."""
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)
    assert_not_cruft(path.parent, "mkdir (meta_experiment)")
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_no_surrogates(data, path)
    history.snapshot(path, base_dir, agent)
    _atomic_write_yaml(path, data)
    changelog.append(base_dir, agent, path, "edit")


# The bare-lock `_persist` that used to live here is DELETED, not retained
# (). Both paths this module writes — active-experiments.yaml and
# completed-experiments.yaml — are class (b) fence-only, so a bare-lock persist
# here is not merely unused, it is ALWAYS the wrong call, and leaving it in
# place arms the next editor to reach for the shorter name. `_persist_unlocked`
# + `file_locks.locked_rmw` is the only correct pair in this module.


def _next_id(experiments) -> str:
    max_num = 0
    for e in experiments:
        eid = e.get("id", "")
        if eid.startswith("exp-meta-"):
            try:
                max_num = max(max_num, int(eid.split("-")[-1]))
            except ValueError:
                pass
    return "exp-meta-{:03d}".format(max_num + 1)


# ---------------------------------------------------------------------------
def _resolve_dotpath(data, dotpath):
    """Read-only dotpath resolution — an EXISTENCE check, never a mutation.

    Returns (found, deepest_container, resolved_prefix). On a miss the container
    is the last level successfully navigated, so the caller can name the keys
    that ARE available there.

    Deliberately not the meta-yaml navigator, which "creates intermediate dicts
    as needed for set operations" and therefore can never report a missing
    segment — it materialises one. A setter's navigator is the wrong instrument
    for a validity check (g-115-5154). Parity twin:
    core/scripts/meta-experiment.py::resolve_dotpath.
    """
    parts = re.sub(r"\[(\d+)\]", r".\1", str(dotpath)).lstrip(".").split(".")
    current = data
    prefix: List[str] = []
    for part in parts:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return False, current, ".".join(prefix)
        elif isinstance(current, dict):
            if part not in current:
                return False, current, ".".join(prefix)
            current = current[part]
        else:
            return False, current, ".".join(prefix)
        prefix.append(part)
    return True, None, str(dotpath)


# POST /v1/meta/experiment/create
# ---------------------------------------------------------------------------

def create(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")

    strategy = body.get("strategy")
    field = body.get("field")
    baseline = body.get("baseline")
    variant = body.get("variant")
    if strategy is None or field is None or baseline is None or variant is None:
        return Response.error(400, "missing_param",
                              "strategy, field, baseline, variant are required")
    try:
        baseline_value = float(baseline)
        variant_value = float(variant)
    except (ValueError, TypeError):
        return Response.error(400, "invalid_param", "baseline and variant must be floats")

    max_concurrent = _config(ctx).get("experiments", {}).get("max_concurrent", 1)
    active_path = _active_path(ctx)
    base_dir = ctx.paths.meta
    agent = _agent_name(ctx)

    assert_not_cruft(active_path.parent, "mkdir (meta_experiment)")
    active_path.parent.mkdir(parents=True, exist_ok=True)

    def _cycle():
        # force_fresh matters most HERE: both the max_concurrent check and
        # _next_id are derived from the list this read returns. A cache-TTL read
        # lets two boxes mint the same exp-NNN and each overwrite the other's,
        # silently — the retry re-derives both against the peer's landed write.
        active = _read_yaml(active_path, force_fresh=True)
        experiments = active.get("experiments", [])
        if len(experiments) >= max_concurrent:
            # No write — locked_rmw makes exactly one pass and returns this.
            return Response.error(409, "max_concurrent",
                                  "Max {} concurrent experiments".format(max_concurrent))
        # : refuse a field that does not resolve in the target strategy
        # file. An experiment on a nonexistent field can never accumulate a
        # sample, so aspirations-evolve Step 0.7's "resolve only past
        # min_duration_goals" gate can never fire — the record sits `active`
        # forever, and that same step gates NEW experiments on `IF no active
        # experiment`, so ONE typo silently blocks all A/B experimentation from
        # then on. Nothing errors and nothing reports. Measured: exp-meta-001
        # (field `weights.x`, absent from goal-selection-strategy.yaml's 26
        # weight keys) held the single slot 6 days and would have held it
        # permanently.
        #
        # Fail-closed, and the guard-1562 enumeration is small: the only
        # programmatic caller is aspirations-evolve/SKILL.md:251, the active slot
        # is empty, and the only historical field value across every stored
        # experiment is `weights.x` — the defect itself.
        #
        # ORDER IS DELIBERATE: after the max_concurrent early-return, matching
        # core/scripts/meta-experiment.py::cmd_create exactly. The parity twins
        # must refuse in the same order or a caller sees a different error code
        # for the same input depending on which path served it. Cost is a
        # strategy-file re-read per locked_rmw retry, which is rare and cheap.
        # ctx.paths.meta, never a module constant (path-resolution.md).
        strategy_path = ctx.paths.meta / str(strategy)
        if not strategy_path.exists():
            return Response.error(400, "strategy_not_found",
                                  "strategy file not found: {}".format(strategy))
        _found, _container, _prefix = _resolve_dotpath(
            _read_yaml(strategy_path), field)
        if not _found:
            _avail = sorted(_container.keys()) if isinstance(_container, dict) else []
            return Response.error(
                400, "field_unresolved",
                "field '{}' does not resolve in {} (resolved as far as '{}'; keys there: {})".format(
                    field, strategy, _prefix or "(root)",
                    ", ".join(_avail) if _avail else "(none)"))
        exp_id = _next_id(experiments)
        experiment = {
            "id": exp_id,
            "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "strategy_file": strategy,
            "field": field,
            "baseline_value": baseline_value,
            "variant_value": variant_value,
            "status": "active",
            "phase": "baseline",
            "total_goals": 0,
            "metrics": {"baseline": [], "variant": []},
        }
        experiments.append(experiment)
        active["experiments"] = experiments
        _validate_no_surrogates(active, active_path)
        history.snapshot(active_path, base_dir, agent)
        _atomic_write_yaml(active_path, active)
        changelog.append(base_dir, agent, active_path, "edit")

        return Response.text(
            json.dumps({"status": "created", "id": exp_id,
                        "strategy": strategy, "field": field}) + "\n",
            content_type="application/json")

    return file_locks.locked_rmw(active_path, _cycle)


# ---------------------------------------------------------------------------
# GET /v1/meta/experiment/status
# ---------------------------------------------------------------------------

def status(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    active = _read_yaml(_active_path(ctx))
    experiments = active.get("experiments", [])
    exp_id = ctx.query.get("id")
    if exp_id:
        for exp in experiments:
            if exp.get("id") == exp_id:
                return Response.text(
                    json.dumps(exp, ensure_ascii=False, default=str) + "\n",
                    content_type="application/json")
        return Response.error(404, "not_found", "Experiment {} not found".format(exp_id))
    return Response.text(
        json.dumps({"active_experiments": len(experiments), "experiments": experiments},
                   ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/meta/experiment/resolve
# ---------------------------------------------------------------------------

def resolve(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    try:
        body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
    except (ValueError, AttributeError):
        return Response.error(400, "invalid_body", "request body must be JSON")
    if not isinstance(body, dict):
        return Response.error(400, "invalid_body", "request body must be a JSON object")
    exp_id = (body.get("id") or "").strip()
    if not exp_id:
        return Response.error(400, "missing_param", "id is required")

    active_path = _active_path(ctx)
    completed_path = _completed_path(ctx)
    threshold = _config(ctx).get("experiments", {}).get("significance_threshold", 0.05)
    out: Dict[str, Any] = {}

    # Both files are write-class (b), and before  BOTH reads happened
    # entirely outside any lock — the widest RMW window of the five cured sites.
    # Each file now gets its own locked_rmw with a fresh in-cycle read. The two
    # writes remain NON-atomic across files (faithful to CLI, unchanged here):
    # locked_rmw fences one object, so a cross-file transaction is out of its
    # scope and out of this fix's.
    def _cycle_active():
        active = _read_yaml(active_path, force_fresh=True)
        experiments = active.get("experiments", [])

        target = None
        remaining = []
        for exp in experiments:
            if exp.get("id") == exp_id:
                target = exp
            else:
                remaining.append(exp)
        if not target:
            # No write — locked_rmw makes exactly one pass and returns this.
            return Response.error(404, "not_found",
                                  "Experiment {} not found".format(exp_id))

        baseline_metrics = target.get("metrics", {}).get("baseline", [])
        variant_metrics = target.get("metrics", {}).get("variant", [])
        if baseline_metrics and variant_metrics:
            delta = (sum(variant_metrics) / len(variant_metrics)
                     - sum(baseline_metrics) / len(baseline_metrics))
        else:
            delta = 0.0

        if delta > threshold:
            outcome = "adopted"
        elif delta < -threshold:
            outcome = "reverted"
        else:
            outcome = "inconclusive"

        target["resolved"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        target["outcome"] = outcome
        target["delta"] = round(delta, 6)
        target["status"] = "resolved"

        active["experiments"] = remaining
        _persist_unlocked(ctx, active_path, active)
        out["target"] = target
        out["outcome"] = outcome
        out["delta"] = delta
        return None

    not_found = file_locks.locked_rmw(active_path, _cycle_active)
    if not_found is not None:
        return not_found

    def _cycle_completed():
        completed = _read_yaml(completed_path, force_fresh=True)
        completed_list = completed.get("experiments", [])
        completed_list.append(out["target"])
        completed["experiments"] = completed_list
        _persist_unlocked(ctx, completed_path, completed)

    file_locks.locked_rmw(completed_path, _cycle_completed)

    return Response.text(
        json.dumps({"status": "resolved", "id": exp_id,
                    "outcome": out["outcome"], "delta": round(out["delta"], 6)}) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# GET /v1/meta/experiment/list
# ---------------------------------------------------------------------------

def list_experiments(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    completed_flag = ctx.query.get("completed")
    use_completed = completed_flag is not None and str(completed_flag).lower() not in ("", "0", "false", "no")
    path = _completed_path(ctx) if use_completed else _active_path(ctx)
    data = _read_yaml(path)
    experiments = data.get("experiments", [])

    # : an ACTIVE experiment holding zero samples cannot reach
    # min_duration_goals, so aspirations-evolve Step 0.7's resolution gate never
    # fires and the single slot stays occupied in silence. Surface it HERE —
    # this is the endpoint evolve reads (`meta-experiment.sh list --active`)
    # immediately before deciding `IF no active experiment`, so a stuck record
    # becomes visible at the exact moment it would otherwise block a new one.
    #
    # Report-only by design: never resolves, never mutates. An experiment can be
    # legitimately young, and auto-resolving on a wall clock would discard real
    # baselines — a human or the evolve step decides.
    #
    # THIS IS THE PATH THAT RUNS. The create-side validation was first written
    # into core/scripts/meta-experiment.py alone and was inert: the wrapper is
    # daemon-only (no CLI fallback since 2026-05-29), so the bogus field was
    # still accepted end-to-end. Parity twin: that file's cmd_list.
    stuck: List[Dict[str, Any]] = []
    if not use_completed:
        after_hours = _config(ctx).get("experiments", {}).get("stuck_after_hours", 48)
        now = datetime.now()
        for exp in experiments:
            if exp.get("total_goals", 0):
                continue
            created = exp.get("created")
            if not created:
                continue
            try:
                age_h = (now - datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")).total_seconds() / 3600.0
            except (TypeError, ValueError):
                continue
            if age_h >= after_hours:
                stuck.append({
                    "id": exp.get("id"),
                    "field": exp.get("field"),
                    "strategy_file": exp.get("strategy_file"),
                    "age_hours": round(age_h, 1),
                    "total_goals": 0,
                    "why": ("zero samples past {}h — cannot reach min_duration_goals, so "
                            "Step 0.7 will never resolve it; it is occupying the single "
                            "slot. Verify the field resolves, then "
                            "`meta-experiment.sh resolve --id <id>`.".format(after_hours)),
                })

    out: Dict[str, Any] = {"count": len(experiments), "experiments": experiments}
    if stuck:
        out["stuck"] = stuck
        out["stuck_count"] = len(stuck)
    return Response.text(
        json.dumps(out, ensure_ascii=False, default=str) + "\n",
        content_type="application/json")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/meta/experiment/create")] = create
    routes[("GET", "/v1/meta/experiment/status")] = status
    routes[("POST", "/v1/meta/experiment/resolve")] = resolve
    routes[("GET", "/v1/meta/experiment/list")] = list_experiments
