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
from storage_backend import get_backend  # noqa: E402  # s5b: own-cloud read freshness
from ..agent_paths import assert_not_cruft  # noqa: E402

# _stamp_now is imported (rather than re-deriving strftime here) so the
# amend_stamp_field value is byte-identical in format to `created` — the merge
# compares these as normalized strings, so a format divergence would order them
# wrongly. Private-by-underscore but same-package; the shared format IS the point.
from ..store_registry import STORE_REGISTRY, apply_defaults, _stamp_now


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
    # s5b (own-cloud): EVERY caller holds file_locks.locked(path) for a
    # read-modify-write (verified: all 5 call sites are inside `with
    # file_locks.locked(path):`). Force-fresh the local cache from the backend
    # before reading — bypasses the cache TTL so the RMW never starts from a
    # stale cached value (lost-update prevention) AND records the If-Match fence
    # etag for the _atomic_write_jsonl that follows. No-op on LocalBackend (zero
    # added I/O); also materializes a remote-only file so the exists() check and
    # read below see it. Mirrors _fileops.locked_modify_jsonl.
    get_backend().refresh(path)
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
    assert_not_cruft(path.parent, "mkdir (store._atomic_write_jsonl)")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _write(handle):
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=True) + "\n")

    _atomic_write_with_fallback(
        path, _write, fallback_counter_key="daemon_store_write")


def _agent_name(ctx) -> str:
    return (ctx.headers.get("x-mind-agent") or "").strip() or "system"


def _require_agent_header(ctx) -> Optional["Response"]:  # type: ignore[name-defined]
    """Reject store writes when X-Mind-Agent header is missing/empty.

    Without this gate, an empty header lets agent_paths.AgentPathResolver
    fall back to `_first_available_agent()` (alphabetically first agent
    with a local-paths.conf — typically "alpha"). The downstream validator
    then surfaces the misleading "Invalid journal_file: bravo/... (expected
    alpha/journal/...)" error even though the caller's environment had
    MIND_AGENT=bravo. The bravo session-77 incident (2026-05-18) was
    exactly this shape: MIND_AGENT was empty at journal-add invocation,
    rt_curl omitted the header, daemon resolved to alpha, validator
    rejected the bravo-shaped body with the wrong-agent error.

    Per-agent store writes require explicit agent identity. Read endpoints
    that legitimately have no agent context (admin, health) bypass this
    gate by not calling it.

    See g-115-957.
    """
    from ..server import Response
    agent = (ctx.headers.get("x-mind-agent") or "").strip()
    if not agent:
        return Response.error(
            400, "missing_agent_header",
            "X-Mind-Agent header required for store writes. Caller "
            "environment likely has MIND_AGENT empty/unset — the wrapper "
            "omits the header when MIND_AGENT is empty, and the daemon "
            "must not silently fall back to the alphabetically first agent "
            "(g-115-957). Set MIND_AGENT explicitly before invoking the "
            "wrapper, or pass --agent to rt_curl.",
        )
    return None


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

    err = _require_agent_header(ctx)
    if err:
        return err
    spec, err = _spec_or_error(ctx)
    if err:
        return err
    rec, err = _parse_body(ctx)
    if err:
        return err

    # 1. Script-owned timestamp (overwrite unconditionally, ignore stdin).
    if spec.created_field:
        rec[spec.created_field] = spec.created_stamp()
    # 1b. Writing-agent provenance (). NEVER-OVERWRITE — a PRESENCE
    # test, not a truthiness test, so an explicit caller value survives even
    # when it is null (the caller-wins contract _rb_inject_source_goal states).
    # Runs BEFORE prepare so a store's prepare hook stays the escape hatch that
    # can override it, and before apply_defaults, which is only-if-absent and so
    # cannot clobber what this sets. _agent_name falls back to "system" on an
    # empty header, but _require_agent_header already rejected that above, so on
    # this path the value is always a real agent name.
    if spec.author_field and spec.author_field not in rec:
        rec[spec.author_field] = _agent_name(ctx)
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
        except (ValueError, TypeError) as e:
            # TypeError too: a list/dict where a scalar field is expected makes
            # a validator's `x not in <set>` raise "unhashable type" — that must
            # be a clean 400, never a 500 that silently drops the write (B10).
            return Response.error(400, "validation_failed", str(e))

    path = spec.path(ctx)
    try:
        # #38: the whole in-lock read-modify-write is the retry unit. On an
        # own-cloud If-Match stale-lock-break ConflictError, locked_rmw re-runs
        # _cycle, which re-reads fresh (via _read_jsonl's backend.refresh),
        # re-runs the dup-check against the peer's landed write, and re-appends.
        def _cycle():
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
                except (ValueError, TypeError) as e:  # B10: TypeError -> 400, not 500
                    return Response.error(400, "validation_failed", str(e))
            items.append(rec)
            _commit(ctx, spec, path, items,
                    f"store-append {ctx.query.get('store')} "
                    f"{rec.get(spec.id_field)}")
            return Response.json({"ok": True, "record": rec})
        return file_locks.locked_rmw(path, _cycle)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))


def replace(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/replace?store=<key>&id=<value>  body: full record.

    Mirrors the family CLI cmd_update: apply defaults, reject a body whose
    key field disagrees with the target (silent-key-mutation guard),
    validate, then locked find-by-key replace-in-place.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
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
        except (ValueError, TypeError) as e:  # B10: TypeError -> 400, not 500
            return Response.error(400, "validation_failed", str(e))

    path = spec.path(ctx)
    try:
        def _cycle():  # #38: retry unit — see append for the rationale.
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
            # Same immutability for the append-time author stamp ():
            # a full replace substitutes the record wholesale, so a caller body
            # that simply omits the field would silently erase — or reassign —
            # who wrote the record. Authorship is a fact about creation, exactly
            # like `created`. Not theoretical: pattern-signatures-update.sh
            # drives this endpoint against one of the three stamped stores.
            # Membership test, not `.get()`: historical rows have no author, and
            # copying an absent key back as null would backfill the very rows
            # this change promises to leave alone. Where the existing record has
            # no author, a caller-supplied value is therefore still allowed to
            # land — deliberate, so a backfill tool remains possible.
            if spec.author_field and spec.author_field in found[1]:
                rec[spec.author_field] = found[1][spec.author_field]
            items[found[0]] = rec
            _commit(ctx, spec, path, items,
                    f"store-replace {ctx.query.get('store')} {key}")
            return Response.json({"ok": True, "record": rec})
        return file_locks.locked_rmw(path, _cycle)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))


def merge(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/merge?store=<key>&id=<value>  body: JSON patch.

    Mirrors the family CLI cmd_merge EXACTLY, including that it does NOT
    re-validate the merged record (the family CLI deliberately skips
    validation on merge). Per-field strategy from spec.merge_lists:
    'union' (append if absent), 'append' (extend/append), else scalar
    overwrite.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
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
    try:
        def _cycle():  # #38: retry unit — see append for the rationale.
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
            _commit(ctx, spec, path, items,
                    f"store-merge {ctx.query.get('store')} {key}")
            return Response.json({"ok": True, "record": rec})
        return file_locks.locked_rmw(path, _cycle)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))


def set_field(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/set-field?store=&id=&field=&value=

    Mirrors reasoning-bank.py rb_update_field / guard_update_field +
    pipeline_write.py update_field: locked find-by-key, self-heal legacy
    defaults, mutate single field, validate, optional recompute, write.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
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
        def _cycle():  # #38: retry unit — see append for the rationale.
            items = _read_jsonl(path)
            found = _find(items, spec, key)
            if found is None:
                return Response.error(
                    404, "not_found", f"{spec.id_field} {key} not found")
            idx, rec = found
            rec = apply_defaults(rec, spec.default_fields)
            rec[field_name] = value
            # Amendment recency stamp ( / guard-1703, redesigned
            # per-field by ). Cross-box merge resolves content fields
            # with a byte-order tiebreak that has no relation to which text is
            # newer, so an in-place amendment can be reverted by the older copy it
            # extends. This stamp is the explicit ordering key
            # _merge_guard_record needs; without a writer it would be a reader
            # with no writer (rb-5493).
            #
            # PER-FIELD, keyed by the field being written — NOT a record-level
            # scalar. The merge handler resolves field by field, so a record-level
            # stamp would order EVERY content field by whichever box wrote last,
            # deterministically discarding a concurrent amendment to a different
            # field of the same record. guard-1153 states the correct shape: LWW
            # on a timestamp written BY THE SAME MUTATION that writes the field.
            # Writing only `field_name`'s key is what makes that true here.
            #
            # Inside locked_rmw, so the stamp is atomic with the field it dates —
            # a separate write could not be. Skipped when the caller is setting
            # the stamp field itself (an explicit backfill/correction stays
            # authoritative rather than being overwritten by now()).
            if spec.amend_stamp_field and field_name != spec.amend_stamp_field:
                stamps = rec.get(spec.amend_stamp_field)
                if not isinstance(stamps, dict):
                    # Absent, or a legacy record-level scalar left by the
                    #  shape. Start a fresh map rather than mutating a
                    # non-dict; the old scalar stays on the record and is read as
                    # a per-field floor by the merge's migration path.
                    stamps = {}
                stamps[field_name] = _stamp_now()
                rec[spec.amend_stamp_field] = stamps
            if spec.validate is not None:
                try:
                    spec.validate(ctx, rec)
                except (ValueError, TypeError) as e:  # B10: TypeError -> 400, not 500
                    return Response.error(400, "validation_failed", str(e))
            if field_name in spec.recompute_on_fields and spec.recompute:
                spec.recompute(rec)
            items[idx] = rec
            _commit(ctx, spec, path, items,
                    f"store-set-field {ctx.query.get('store')} {key} {field_name}")
            return Response.json({"ok": True, "record": rec})
        return file_locks.locked_rmw(path, _cycle)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))


def increment(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/store/increment?store=&id=&field=

    Mirrors reasoning-bank.py rb_increment / guard_increment: atomic
    counter increment + utilization_score recompute.
    """
    from ..server import Response

    err = _require_agent_header(ctx)
    if err:
        return err
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

    # --- counter spool lane (). DEFAULT-OFF. -----------------------
    # When this box has been cleared for the cutover, a counter increment
    # appends one line to a machine-local spool instead of read-modify-writing
    # the CONTENT store. That RMW is the whole cost being removed: a 20.46MB /
    # 9.37MB whole-object GET+PUT to change one integer, ~51 GB/day across the
    # two stores at ~93-94% of all writes to them ().
    #
    # DELIBERATELY BEFORE `spec.path(ctx)` AND ANY READ. Reading the store to
    # validate the id would re-introduce the whole-object GET half of the cost,
    # so the spool path does not read at all — which means it cannot 404 on an
    # unknown id the way the legacy path does. That is accepted rather than
    # patched, because the alternative is worse in the direction that matters:
    # an orphan sidecar entry is INERT (utilization_of only ever looks up ids of
    # records that exist, so nothing reads it), whereas having the flusher drop
    # ids it cannot find in the content store would silently discard every
    # first-touch increment during any transient backend read failure. Dead
    # weight beats silent loss.
    #
    # Scoped to the two stores the sidecar seam covers; every other store with
    # an increment_prefix keeps the legacy path untouched.
    if parent_key == "utilization":
        try:
            import _utilization_store as _us
            store_name = (ctx.query.get("store") or "").strip()
            if _us.spooled_enabled() and store_name in _us.KINDS:
                if _us.record_increment(store_name, key, counter,
                                        world_dir=ctx.paths.world):
                    return Response.json({
                        "ok": True,
                        "spooled": True,
                        "record": {spec.id_field: key},
                    })
                # record_increment returns False rather than raising, so a
                # failed spool append falls through to the legacy RMW below and
                # the increment is preserved. Never let the cheap path lose a
                # counter to save a write.
        except Exception:
            pass    # any import/resolution fault -> legacy path, never a 500

    path = spec.path(ctx)
    try:
        # #38: increment is the classic lost-update — a counter += 1 read against
        # a stale value silently drops a peer's concurrent increment. On a
        # ConflictError, _cycle re-reads the peer's landed value and increments
        # on TOP of it, so the count is preserved across the stale-lock-break race.
        def _cycle():
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
            return Response.json({"ok": True, "record": rec})
        return file_locks.locked_rmw(path, _cycle)
    except OSError as e:
        return Response.error(500, "write_failed", str(e))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register(routes) -> None:
    routes[("POST", "/v1/store/append")] = append
    routes[("POST", "/v1/store/replace")] = replace
    routes[("POST", "/v1/store/merge")] = merge
    routes[("POST", "/v1/store/set-field")] = set_field
    routes[("POST", "/v1/store/increment")] = increment
