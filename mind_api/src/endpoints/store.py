"""POST /v1/store/{append,replace,merge} — generic JSONL store writer.

ONE implementation of locked append / full-replace / merge over the daemon
write infra (file_locks + history + changelog + jsonl_cache invalidation),
parameterized by store_registry.StoreSpec. Replaces the per-family
add/update/merge CLI subcommands family-by-family.

Same daemon write infrastructure pattern as pipeline_write.py /
aspirations_write.py — history.snapshot -> _atomic_write_with_fallback ->
changelog.append -> cache invalidate, all inside file_locks.locked(path)
(the cross-process .lock the family CLI used, so this serialises correctly
with the other agents' daemons writing the same world/meta files).

Contract (mirrors pipeline_write.py's query+body split):
  ?store=<key>   required for every op (the ONE variable per call)
  ?id=<value>    replace/merge: the record key, coerced per StoreSpec
  body           append/replace: JSON record; merge: JSON patch

Design + rationale: zeta/reports/phase3-h2-wave-plan.md, HARDENING sec17.4.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..jsonl_cache import cache as _jsonl_cache
from .. import file_locks, history, changelog

from _fileops import _atomic_write_with_fallback  # noqa: E402

from ..store_registry import STORE_REGISTRY, apply_defaults


# ---------------------------------------------------------------------------
# Value parsing (verbatim from pipeline_write.py _parse_value)
# ---------------------------------------------------------------------------

def _parse_value(value_str: str):
    """Parse a string value into the appropriate Python type."""
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if value_str == "null":
        return None
    if value_str == "[]":
        return []
    if value_str.startswith("{") or value_str.startswith("["):
        try:
            return json.loads(value_str)
        except json.JSONDecodeError:
            pass
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


# ---------------------------------------------------------------------------
# JSONL read/write helpers (same pattern as pipeline_write.py)
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _atomic_write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_store_write")


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-ayoai-agent") or "").strip() or "system"


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------

def _spec_or_error(ctx) -> Tuple[Optional[Any], Optional["Response"]]:  # type: ignore[name-defined]
    from ..server import Response
    store = (ctx.query.get("store") or "").strip()
    if not store:
        return None, Response.error(400, "missing_param",
                                    "query parameter 'store' required")
    spec = STORE_REGISTRY.get(store)
    if spec is None:
        return None, Response.error(400, "unknown_store",
                                    f"No store registered for '{store}'")
    return spec, None


def _parse_body(ctx) -> Tuple[Optional[dict], Optional["Response"]]:  # type: ignore[name-defined]
    from ..server import Response
    if not ctx.body:
        return None, Response.error(400, "invalid_body", "empty body")
    try:
        obj = json.loads(ctx.body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        return None, Response.error(400, "invalid_body",
                                    f"body must be JSON: {e}")
    if not isinstance(obj, dict):
        return None, Response.error(400, "invalid_body",
                                    "body must be a JSON object")
    return obj, None


def _find(items: List[dict], spec, key) -> Optional[Tuple[int, dict]]:
    for i, rec in enumerate(items):
        if rec.get(spec.id_field) == key:
            return (i, rec)
    return None


def _commit(ctx, spec, path: Path, items: List[dict], summary: str) -> None:
    """history.snapshot -> atomic write -> changelog.append -> cache
    invalidate. Byte-identical sequence to pipeline_write.py (the proven
    daemon write pattern). base resolves via spec.base(ctx) so agent /
    world / meta stores all get the correct history+changelog root."""
    base = spec.base(ctx)
    agent = _agent_name(ctx)
    history.snapshot(path, base, agent, summary=summary)
    _atomic_write_jsonl(path, items)
    changelog.append(base, agent, path, "edit", summary=summary,
                     lines_changed=len(items))
    _jsonl_cache().invalidate(path)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def append(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/append?store=<key>  body: JSON record.

    Mirrors the family CLI cmd_add: script-owned stamp, prepare hook,
    dynamic + static defaults, pre-lock validate (skip_id when the key is
    auto-allocated), then INSIDE the lock allocate-or-dup-check and
    re-validate before append.
    """
    from ..server import Response

    spec, err = _spec_or_error(ctx)
    if err:
        return err
    rec, err = _parse_body(ctx)
    if err:
        return err

    # 1. Script-owned timestamp (overwrite unconditionally, ignore stdin).
    if spec.created_field:
        rec[spec.created_field] = spec.created_stamp()
    # 2. Prepare hook (e.g. rb source_goal injection from team-state).
    if spec.prepare:
        spec.prepare(ctx, rec)
    # 3. Dynamic defaults (only-if-absent), then static defaults.
    for f, fn in spec.defaults_dynamic.items():
        if f not in rec:
            rec[f] = fn()
    rec = apply_defaults(rec, spec.default_fields)
    # 3b. Unconditional recompute on add: the family CLI normalize_record
    # applied defaults THEN recomputed derived fields ("never trust input"
    # for outcome_stats.accuracy / yield_rate / utilization_score) BEFORE
    # validate. Idempotent for journal (no recompute) / rb / guard (fresh
    # all-zero counters -> 0.0 == default); required for pattern-signatures.
    if spec.recompute:
        spec.recompute(rec)

    has_key = spec.id_field in rec and rec[spec.id_field] not in (None, "")

    if spec.validate is not None:
        try:
            spec.validate(ctx, rec, skip_id=not has_key)
        except ValueError as e:
            return Response.error(400, "validation_failed", str(e))

    path = spec.path(ctx)
    try:
        with file_locks.locked(path):
            items = _read_jsonl(path)
            if not has_key:
                if spec.allocate is None:
                    return Response.error(
                        400, "missing_id",
                        f"record must carry '{spec.id_field}'")
                rec[spec.id_field] = spec.allocate(items)
            else:
                if _find(items, spec, rec[spec.id_field]) is not None:
                    return Response.error(
                        409, "duplicate_id",
                        f"Duplicate {spec.id_field}: {rec[spec.id_field]}")
            # Re-validate with the final key present (journal.py cmd_add
            # validates twice — once pre-lock, once in the _build closure).
            if spec.validate is not None:
                try:
                    spec.validate(ctx, rec, skip_id=False)
                except ValueError as e:
                    return Response.error(400, "validation_failed", str(e))
            items.append(rec)
            _commit(ctx, spec, path, items,
                    f"store-append {ctx.query.get('store')} "
                    f"{rec.get(spec.id_field)}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "record": rec})


def replace(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/replace?store=<key>&id=<value>  body: full record.

    Mirrors the family CLI cmd_update: apply defaults, reject a body whose
    key field disagrees with the target (silent-key-mutation guard),
    validate, then locked find-by-key replace-in-place.
    """
    from ..server import Response

    spec, err = _spec_or_error(ctx)
    if err:
        return err
    key_raw = (ctx.query.get("id") or "").strip()
    if not key_raw:
        return Response.error(400, "missing_param",
                              "query parameter 'id' required")
    try:
        key = spec.id_coerce(key_raw)
    except (ValueError, TypeError) as e:
        return Response.error(400, "invalid_id", f"bad id '{key_raw}': {e}")

    rec, err = _parse_body(ctx)
    if err:
        return err
    rec = apply_defaults(rec, spec.default_fields)
    # Mirror the family CLI cmd_update: normalize_record applied defaults
    # THEN recomputed derived fields before validate.
    if spec.recompute:
        spec.recompute(rec)

    if rec.get(spec.id_field) != key:
        return Response.error(
            400, "id_mismatch",
            f"body {spec.id_field}={rec.get(spec.id_field)!r} does not "
            f"match target {key!r}")

    if spec.validate is not None:
        try:
            spec.validate(ctx, rec, skip_id=False)
        except ValueError as e:
            return Response.error(400, "validation_failed", str(e))

    path = spec.path(ctx)
    try:
        with file_locks.locked(path):
            items = _read_jsonl(path)
            found = _find(items, spec, key)
            if found is None:
                return Response.error(
                    404, "not_found", f"{spec.id_field} {key} not found")
            # Preserve the script-owned created stamp from the existing
            # record (mirrors the family CLI cmd_update:
            # rec["created"] = existing["created"]). created is immutable
            # post-add; a full replace must not let the caller reset it.
            if spec.created_field:
                rec[spec.created_field] = found[1].get(spec.created_field)
            items[found[0]] = rec
            _commit(ctx, spec, path, items,
                    f"store-replace {ctx.query.get('store')} {key}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "record": rec})


def merge(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/merge?store=<key>&id=<value>  body: JSON patch.

    Mirrors the family CLI cmd_merge EXACTLY, including that it does NOT
    re-validate the merged record (the family CLI deliberately skips
    validation on merge). Per-field strategy from spec.merge_lists:
    'union' (append if absent), 'append' (extend/append), else scalar
    overwrite.
    """
    from ..server import Response

    spec, err = _spec_or_error(ctx)
    if err:
        return err
    key_raw = (ctx.query.get("id") or "").strip()
    if not key_raw:
        return Response.error(400, "missing_param",
                              "query parameter 'id' required")
    try:
        key = spec.id_coerce(key_raw)
    except (ValueError, TypeError) as e:
        return Response.error(400, "invalid_id", f"bad id '{key_raw}': {e}")

    patch, err = _parse_body(ctx)
    if err:
        return err

    path = spec.path(ctx)
    written: Dict[str, Any] = {}
    try:
        with file_locks.locked(path):
            items = _read_jsonl(path)
            found = _find(items, spec, key)
            if found is None:
                return Response.error(
                    404, "not_found", f"{spec.id_field} {key} not found")
            idx, rec = found

            for k, v in patch.items():
                strat = spec.merge_lists.get(k)
                if strat == "union":
                    existing = rec.get(k, [])
                    if not isinstance(existing, list):
                        existing = []
                    for item in (v if isinstance(v, list) else [v]):
                        if item not in existing:
                            existing.append(item)
                    rec[k] = existing
                elif strat == "append":
                    existing = rec.get(k, [])
                    if not isinstance(existing, list):
                        existing = []
                    if isinstance(v, list):
                        existing.extend(v)
                    else:
                        existing.append(v)
                    rec[k] = existing
                else:
                    rec[k] = v

            items[idx] = rec
            written = rec
            _commit(ctx, spec, path, items,
                    f"store-merge {ctx.query.get('store')} {key}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "record": written})


def set_field(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/set-field?store=&id=&field=&value=

    Mirrors reasoning-bank.py rb_update_field / guard_update_field +
    pipeline_write.py update_field: locked find-by-key, self-heal legacy
    defaults, mutate single field, validate, optional recompute, write.
    """
    from ..server import Response

    spec, err = _spec_or_error(ctx)
    if err:
        return err

    key_raw = (ctx.query.get("id") or "").strip()
    if not key_raw:
        return Response.error(400, "missing_param",
                              "query parameter 'id' required")
    try:
        key = spec.id_coerce(key_raw)
    except (ValueError, TypeError) as e:
        return Response.error(400, "invalid_id", f"bad id '{key_raw}': {e}")

    field_name = (ctx.query.get("field") or "").strip()
    if not field_name:
        return Response.error(400, "missing_param",
                              "query parameter 'field' required")

    value_str = ctx.query.get("value")
    if value_str is None:
        return Response.error(400, "missing_param",
                              "query parameter 'value' required")
    value = _parse_value(value_str)

    # Immutable fields (e.g. "created" for rb/guard).
    if (field_name in spec.immutable_fields
            or field_name.split(".")[0] in spec.immutable_fields):
        return Response.error(
            400, "immutable_field",
            f"'{field_name}' is script-stamped at add time and cannot be updated.")

    # Reject dotted field names (Option A — matches reasoning-bank.py).
    if "." in field_name:
        return Response.error(
            400, "dotted_field_rejected",
            f"Dotted field name '{field_name}' is not supported. "
            f"Write flat top-level keys only.")

    path = spec.path(ctx)
    try:
        with file_locks.locked(path):
            items = _read_jsonl(path)
            found = _find(items, spec, key)
            if found is None:
                return Response.error(
                    404, "not_found", f"{spec.id_field} {key} not found")
            idx, rec = found
            rec = apply_defaults(rec, spec.default_fields)
            rec[field_name] = value
            if spec.validate is not None:
                try:
                    spec.validate(ctx, rec)
                except ValueError as e:
                    return Response.error(400, "validation_failed", str(e))
            if field_name in spec.recompute_on_fields and spec.recompute:
                spec.recompute(rec)
            items[idx] = rec
            _commit(ctx, spec, path, items,
                    f"store-set-field {ctx.query.get('store')} {key} {field_name}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "record": rec})


def increment(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/increment?store=&id=&field=

    Mirrors reasoning-bank.py rb_increment / guard_increment: atomic
    counter increment + utilization_score recompute.
    """
    from ..server import Response

    spec, err = _spec_or_error(ctx)
    if err:
        return err

    key_raw = (ctx.query.get("id") or "").strip()
    if not key_raw:
        return Response.error(400, "missing_param",
                              "query parameter 'id' required")
    try:
        key = spec.id_coerce(key_raw)
    except (ValueError, TypeError) as e:
        return Response.error(400, "invalid_id", f"bad id '{key_raw}': {e}")

    field_name = (ctx.query.get("field") or "").strip()
    if not field_name:
        return Response.error(400, "missing_param",
                              "query parameter 'field' required")

    if not field_name.startswith(spec.increment_prefix):
        return Response.error(
            400, "invalid_field",
            f"Increment only supports {spec.increment_prefix}* fields, "
            f"got: {field_name}")

    counter = field_name.split(".", 1)[1] if "." in field_name else field_name
    if counter not in spec.increment_counters:
        return Response.error(
            400, "invalid_counter",
            f"Invalid counter: {counter} (expected one of: "
            f"{sorted(spec.increment_counters)})")

    parent_key = field_name.split(".", 1)[0]

    path = spec.path(ctx)
    try:
        with file_locks.locked(path):
            items = _read_jsonl(path)
            found = _find(items, spec, key)
            if found is None:
                return Response.error(
                    404, "not_found", f"{spec.id_field} {key} not found")
            idx, rec = found
            rec = apply_defaults(rec, spec.default_fields)
            rec[parent_key][counter] += 1
            if spec.recompute:
                spec.recompute(rec)
            items[idx] = rec
            _commit(ctx, spec, path, items,
                    f"store-increment {ctx.query.get('store')} {key} {field_name}")
    except OSError as e:
        return Response.error(500, "write_failed", str(e))

    return Response.json({"ok": True, "record": rec})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/store/append")] = append
    routes[("POST", "/v1/store/replace")] = replace
    routes[("POST", "/v1/store/merge")] = merge
    routes[("POST", "/v1/store/set-field")] = set_field
    routes[("POST", "/v1/store/increment")] = increment
