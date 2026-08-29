"""GET /v1/rb/read and GET /v1/guard/read.

Pre-migration parity with `reasoning-bank.py rb read` and
`reasoning-bank.py guard read`.

RB query params (mutually exclusive — exactly one):
    active=1
    id=<rb-id>
    category=<cat>
    universal=1
    tag=<tag>            (case-insensitive exact match)
    summary=1
    count=1
    recent=<N>           (default 10 when present without value via recent=)

Guard query params:
    active=1
    id=<guard-id>
    category=<cat>
    summary=1
    count=1

Equivalence target: stdout of the corresponding CLI invocation.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..jsonl_cache import cache
from ..endpoints._jsonl_common import (
    displacement_notice, find_by_id, find_displacers, flag,
    json_response_pretty, missing_flag_error, not_found_detail, plain_lines,
)


# Reach into core/scripts for is_universal_rb + sort_universal_rbs. These
# helpers are pure functions of the items list — no _paths globals — so
# safe to import in the daemon process.
#
# CORRECTED 2026-08-16 ( step 4): this comment used to end "_paths gets
# sys.path-injected via _rb_helpers' own imports; that's fine, daemon doesn't
# depend on the bake-in." That is false and was load-bearing in the wrong
# direction — `_rb_helpers.py` contains NO import statements whatsoever, and no
# module under `mind_api/src` imports `_paths` at module level. The sys.path
# insert above is what makes core/scripts importable here; nothing has yet
# imported `_paths` into this process by this point. Read as written, the old
# sentence licensed adding a module-level core/scripts import that resolves
# WORLD_DIR at daemon load — see `_store_paths` for why that import is lazy.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _rb_helpers import is_universal_rb, sort_universal_rbs  # noqa: E402


# ---------------------------------------------------------------------------
# Content-store enumeration ( step 4 — the LIVE read path)
# ---------------------------------------------------------------------------
#
# These two endpoints ARE the fleet's read path for both stores: the wrappers
# `reasoning-bank-read.sh` and `guardrails-read.sh` are daemon-only, so every
# `--recent` / `--summary` / `--id` / `--category` read in every skill arrives
# here. Converting the core/scripts utilities and leaving this file alone would
# have left every real read segment-blind (guard-742/547 — the live path is the
# daemon reimplementation, not the CLI module that looks like the source).
#
# Byte-identical today by construction: no segments exist, so `store_paths`
# returns exactly the legacy file and `_load` degenerates to the single
# `jc.get(path)` this file did before.
#
# `jc.get` is the sanctioned reader under the seam's caller contract — it calls
# `get_backend().ensure_local(path)` before its stat, so a path naming an object
# this box has not materialised yet is pulled rather than read as empty.

def _counters(ctx, kind):
    """Utilization sidecar map for `kind`, or {} when unavailable ().

    Same lazy-import discipline and for the same measured reason as
    `_store_paths` below: a module-level `_utilization_store` import would
    resolve WORLD_DIR at daemon load and freeze a global in a per-request
    `ctx.paths` process. `world` is passed EXPLICITLY so the frozen global is
    never the one consulted.

    {} on any failure is the conservative direction, not a silent empty: an
    empty map makes `utilization_of` fall through to the embedded field, i.e.
    exactly today's ranking. Only the sidecar's ADVISORY counters are at stake
    here — never the records themselves — so degrading to the embedded copy
    cannot drop a guardrail the way a short content read could.
    """
    try:
        from _utilization_store import load_counters
    except ImportError:
        return {}
    try:
        return load_counters(kind, ctx.paths.world)
    except Exception:
        return {}


def _store_paths(ctx, kind):
    """Ordered content-store paths for `kind`, legacy ALWAYS first.

    `store_paths` includes the legacy file only when it can SEE it — locally on
    disk, or in the backend listing. Both can be false at once on a cold box
    whose backend enumeration just failed (a missing ListBucket grant, a network
    fault), and there the seam correctly returns [] because it cannot prove the
    store's contents.

    That is the right answer for an enumerator and the WRONG one here. Today
    `jc.get(legacy)` would materialise that object through `ensure_local`
    regardless of any listing, so adopting the seam unmodified would turn a
    recoverable cold read into zero records — and for guardrails zero records
    reads as "no guardrails apply", the worst-direction failure this whole
    module warns about. So the legacy path is pinned unconditionally and the
    seam contributes the segments. A missing file costs nothing: `jc.get`
    returns [] for it.

    THE IMPORT IS LAZY, and the reason is measured rather than stylistic. This
    module's header comment claims "_paths gets sys.path-injected via
    _rb_helpers' own imports" — that is FALSE: `_rb_helpers.py` has NO imports
    at all, and no module under `mind_api/src` imports `_paths` at module level.
    So a top-level `from _utilization_store import ...` would newly resolve
    WORLD_DIR from `local-paths.conf` at daemon IMPORT time, freezing a global
    in a process whose whole path contract is per-request `ctx.paths`
    (`.claude/rules/path-resolution.md`). Deferring the import keeps that side
    effect out of module load; `sys.modules` makes the repeat cost nil. The
    precedent is `_gate_log`, which daemon modules DO import at module level and
    whose own comments say the daemon must pass `meta_dir` explicitly BECAUSE
    its global is frozen — the same reason `world` is passed explicitly below.

    On ImportError the legacy path alone is returned, which is exactly today's
    behaviour — the conservative direction, not a silent empty.
    """
    world = ctx.paths.world
    legacy = world / ("%s.jsonl" % kind)
    try:
        from _utilization_store import store_paths
    except ImportError:
        return [legacy]
    paths = store_paths(kind, world)
    if legacy not in paths:
        paths.insert(0, legacy)
    return paths


def _load(jc, paths):
    """Records across every path, in the order given (legacy, then segments),
    with ids appearing in more than one path collapsed NEWEST-WINS.

    Returns a FRESH list. Each `jc.get` hands back the shared cache list, so
    concatenating is also what keeps the existing `list(filtered)` copies in
    this file honest — callers may sort this result without touching the cache.

    THE DEDUP IS NOT COSMETIC. `_store_paths` yields legacy first and segments
    ascending, and `find_by_id` (every `?id=` read on both handlers) returns the
    FIRST match — so without this a record mutated into a segment answers with
    its STALE legacy copy, and every list read emits it twice. For guardrails
    that means a retired guardrail still reads `status: active`, on the daemon
    path, which is the fleet read path. Rationale and why the reader reconciles
    rather than constraining the writer: `_utilization_store.dedup_by_id`.

    ImportError degrades to the plain concatenation — today's exact behaviour,
    and the conservative direction, matching `_store_paths` above. It is also
    inert today: no segments exist, so no id can repeat.
    """
    items = []
    for p in paths:
        items.extend(jc.get(p))
    try:
        from _utilization_store import dedup_by_id
    except ImportError:
        return items
    return dedup_by_id(items)


# ---------------------------------------------------------------------------
# RB read
# ---------------------------------------------------------------------------

def rb_read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    paths = _store_paths(ctx, "reasoning-bank")

    if flag(q, "active"):
        items = _load(jc, paths)
        return json_response_pretty([r for r in items if r.get("status") == "active"])

    rec_id = q.get("id")
    if rec_id:
        items = _load(jc, paths)
        result = find_by_id(items, rec_id)
        # Collision-reid awareness (): another record may have been
        # displaced OFF this id, which makes any pre-merge citation of it
        # ambiguous. Annotate rather than redirect; name the successor on a
        # miss rather than 404-ing over a recoverable answer.
        displacers = find_displacers(items, rec_id)
        if result is None:
            return Response.error(404, "not_found",
                                  not_found_detail(rec_id, displacers))
        rec = result[1]
        if displacers:
            # items comes from the SHARED jsonl cache — copy before annotating
            # or the notice leaks into every later reader of this record.
            rec = dict(rec)
            rec["_displacement_notice"] = displacement_notice(rec_id, displacers)
        return json_response_pretty(rec)

    category = q.get("category")
    if category:
        items = _load(jc, paths)
        return json_response_pretty([r for r in items if r.get("category") == category])

    if flag(q, "universal"):
        items = _load(jc, paths)
        filtered = [r for r in items
                    if r.get("status") == "active" and is_universal_rb(r)]
        # sort_universal_rbs mutates the list in place — make a private copy
        # because items came from the shared cache.
        filtered = list(filtered)
        sort_universal_rbs(filtered, _counters(ctx, "reasoning-bank"))
        return json_response_pretty(filtered)

    tag = q.get("tag")
    if tag:
        items = _load(jc, paths)
        tag_lower = tag.lower()
        out = []
        for r in items:
            if r.get("status") != "active":
                continue
            tags = r.get("tags") or []
            if not isinstance(tags, list):
                continue
            if any(tag_lower == str(t).lower() for t in tags):
                out.append(r)
        return json_response_pretty(out)

    if flag(q, "summary"):
        items = _load(jc, paths)
        lines = []
        for rec in items:
            typ = rec.get("type", "?")
            cat = rec.get("category", "?")
            title = rec.get("title", "(untitled)")
            lines.append(f"{rec.get('id', '?')}: [{typ}] {cat} — {title}")
        return plain_lines(lines)

    recent = q.get("recent")
    if recent is not None:
        # recent can be empty string (just `?recent=`) — CLI default 10
        try:
            n = int(recent) if recent != "" else 10
        except ValueError:
            return Response.error(400, "invalid_param", "recent must be integer")
        if n <= 0:
            n = 10
        items = _load(jc, paths)
        active = [r for r in items if r.get("status") == "active"]
        # Make a copy before sort — items is the shared cache list.
        active = sorted(active, key=lambda r: r.get("created", ""), reverse=True)
        return json_response_pretty(active[:n])

    # COUNT — the cheap answer to "how many records are in this store".
    # Added for : agent-completion-report step 7 wanted three integers
    # and paid 2,106,400 bytes of `summary=1` output for them, then MISCOUNTED.
    # Counting summary LINES is not a record count, and it errs in BOTH
    # directions at once: a field carrying an embedded newline emits a
    # continuation line (+3 on guardrails, measured 2026-08-29), while the
    # output has no trailing newline so `wc -l` undercounts by 1. The two
    # partially cancel, which is why the bug read as "+3 on one store only"
    # and stayed invisible on the other two.
    if flag(q, "count"):
        return json_response_pretty({"count": len(_load(jc, paths))})

    return missing_flag_error([
        "active", "id", "category", "universal", "tag", "summary", "recent",
        "count",
    ])


# ---------------------------------------------------------------------------
# Guard read
# ---------------------------------------------------------------------------

def guard_read(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response

    q = ctx.query
    jc = cache()
    paths = _store_paths(ctx, "guardrails")

    if flag(q, "active"):
        items = _load(jc, paths)
        return json_response_pretty([r for r in items if r.get("status") == "active"])

    rec_id = q.get("id")
    if rec_id:
        items = _load(jc, paths)
        result = find_by_id(items, rec_id)
        # Collision-reid awareness (): another record may have been
        # displaced OFF this id, which makes any pre-merge citation of it
        # ambiguous. Annotate rather than redirect; name the successor on a
        # miss rather than 404-ing over a recoverable answer.
        displacers = find_displacers(items, rec_id)
        if result is None:
            return Response.error(404, "not_found",
                                  not_found_detail(rec_id, displacers))
        rec = result[1]
        if displacers:
            # items comes from the SHARED jsonl cache — copy before annotating
            # or the notice leaks into every later reader of this record.
            rec = dict(rec)
            rec["_displacement_notice"] = displacement_notice(rec_id, displacers)
        return json_response_pretty(rec)

    category = q.get("category")
    if category:
        items = _load(jc, paths)
        return json_response_pretty([r for r in items if r.get("category") == category])

    severity = q.get("severity")
    if severity:
        # Full ACTIVE records for one severity tier (). Consumer:
        # guardrail-manifest.sh, which prepends the CRITICAL always-load core
        # (full rule text — their trigger zones are not self-announcing, so
        # the expand-on-demand path structurally cannot cover them; see
        # prime-store-load-budget.md "The CRITICAL admission rule") above the
        # id manifest. Case-insensitive match: the marker's case is canonical
        # UPPER since , but a reader must not silently drop a
        # non-canonical straggler written by an unmigrated box.
        want = str(severity).upper()
        items = _load(jc, paths)
        return json_response_pretty([
            r for r in items
            if r.get("status") == "active"
            and str(r.get("severity") or "").upper() == want])

    if flag(q, "summary"):
        items = _load(jc, paths)
        lines = []
        for rec in items:
            cat = rec.get("category", "?")
            rule = (rec.get("rule") or "(no rule)")[:80]
            lines.append(f"{rec.get('id', '?')}: [{cat}] {rule}")
        return plain_lines(lines)

    if flag(q, "count"):
        return json_response_pretty({"count": len(_load(jc, paths))})

    return missing_flag_error(
        ["active", "id", "category", "summary", "severity", "count"])


def register(routes) -> None:
    routes[("GET", "/v1/rb/read")] = rb_read
    routes[("GET", "/v1/guard/read")] = guard_read
