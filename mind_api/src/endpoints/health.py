"""GET /v1/admin/health — liveness + version probe.

Used by:
  - The wrapper's auto-start logic to decide when the freshly-spawned daemon
    is ready to accept requests.
  - Tests (test_runtime_health.py).
  - Anyone running `curl http://127.0.0.1:<port>/v1/admin/health` from a
    debug shell.

Response body is JSON. Always succeeds when the daemon is up.
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

from .. import __version__, read_git_head_sha


_START_TIME = time.monotonic()
# Snapshot git HEAD at startup. Wrappers compare this against current
# on-disk HEAD to detect "daemon is running stale code" ().
# Resolved once at import-time so the health probe stays ~10ms.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_STARTUP_SHA = read_git_head_sha(_PROJECT_ROOT)


def clock_posture() -> dict:
    """This process's OWN naive-stamp clock, measured against true UTC.

    The daemon is long-lived, so it keeps whatever TZ env it started with
    (CLAUDE.md "Naming Rules"). If it started before the TZ=UTC posture
    landed, every naive stamp it mints is offset — and because the whole
    fleet compares naive stamps (board `--since`, `last_active` staleness,
    LWW merges), a behind-clock writer systematically LOSES last-write-wins
    races and its content is silently discarded. There is no error surface
    on the losing side (g-115-4014).

    WHY THE DAEMON MEASURES ITSELF rather than a caller diffing the reported
    stamp against the caller's clock: two processes skewed by the SAME amount
    cancel to a delta of zero, so a caller-side diff is blind to exactly the
    fleet-wide case that matters most. `now() - utc_now()` is self-contained
    and independent of who is asking.

    Sibling of `git_head_sha` above and the same class as guard-559 /
    rb-2022: a long-lived daemon whose in-process state has diverged from
    disk truth, surfaced as a health field. The remedy is likewise a restart,
    not an edit.

    Computed PER REQUEST, never cached at import — the point is to report the
    live process, and a cached value would answer for the wrong moment.
    """
    now = datetime.datetime.now()
    utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return {
        # Rendered exactly as the framework mints stamps, so this value is
        # directly comparable to what the daemon writes into stores.
        "naive_now": now.isoformat(timespec="seconds"),
        "utc_now": utc.isoformat(timespec="seconds"),
        "tz_offset_s": round((now - utc).total_seconds()),
    }


def health(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response  # local import — avoids cycle at module load
    uptime_s = round(time.monotonic() - _START_TIME, 3)
    # Whether THIS process routes counter increments to the spool ().
    # The flip travels via settings.json env, which the stale-code recycle
    # boundary (mind-api-code-changed.sh: mind_api/src/** + core/scripts/_*.py)
    # deliberately cannot see — so a daemon can sit indefinitely with the flag
    # on disk but not in its env (measured 2026-08-19: 5 active agents, 5.65h,
    # zero adopters). This field is the remote per-box verification for any
    # env-flag cutover: same guard-559/rb-2022 class as git_head_sha above —
    # in-process state diverged from disk truth, remedy is a restart.
    try:
        import _utilization_store as _us
        utilization_spooled = _us.spooled_enabled()
    except Exception:  # never 500 the health probe over a diagnostic field
        utilization_spooled = None
    # Which object store THIS process resolved ( cutover readiness).
    # The endpoint override travels via .env.local and is read ONCE at backend
    # construction, so a flipped box whose daemon predates the flip keeps
    # answering for the old store — the same guard-559/rb-2022 class as
    # git_head_sha above: in-process state diverged from disk truth, remedy
    # is a restart. Reported from the ALREADY-constructed backend only; this
    # never constructs one (a health probe must not raise on a misconfigured
    # box). storage_endpoint: the override URL; "" = the client library's
    # default regional endpoint; None = no backend built yet / not an
    # object-store backend.
    try:
        import storage_backend as _sb
        _be = _sb._ACTIVE_BACKEND
        storage_backend = None if _be is None else type(_be).__name__
        # The backend keeps an unset override as None/"" — both mean "the
        # client library's default regional endpoint", reported as "" so a
        # reader can tell "own-cloud on the default endpoint" from "no
        # object-store backend at all" (None).
        if _be is not None and hasattr(_be, "s3_endpoint_url"):
            storage_endpoint = str(getattr(_be, "s3_endpoint_url") or "")
        else:
            storage_endpoint = None
    except Exception:  # never 500 the health probe over a diagnostic field
        storage_backend = storage_endpoint = None
    # Whether THIS process's WRITE path is wedged (). Measured:
    # /health answered 200 in 0.2ms right through a total box-wide WM-write
    # freeze, so "responsive" was never evidence of "can write" and
    # mind-api-start.sh's idempotent fast path repaired nothing.
    #
    # This does NOT violate the store-independence rule above: it reads
    # file_locks' in-process hold registry — process memory, no filesystem,
    # no store round trip — so cold start under a degraded store is unaffected
    # and rt_ensure_running still makes one store-free request.
    #
    # write_path_wedged is duplicated out to the TOP LEVEL on purpose: the
    # wrapper's consumer is bash and greps for it, and a flat boolean cannot
    # be confused with a same-named key nested under some other object.
    try:
        from .. import file_locks
        write_path = file_locks.write_path_status()
    except Exception:  # never 500 the health probe over a diagnostic field
        write_path = None
    return Response.json(
        {
            "ok": True,
            "write_path_wedged": None if write_path is None else write_path["wedged"],
            "write_path": write_path,
            "version": __version__,
            "uptime_s": uptime_s,
            "pid": ctx.pid,
            "port": ctx.port,
            "git_head_sha": _STARTUP_SHA,
            "utilization_spooled": utilization_spooled,
            "storage_backend": storage_backend,
            "storage_endpoint": storage_endpoint,
            **clock_posture(),
        }
    )


def write_queue(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/admin/write-queue — per-path FIFO contention metrics
    (g-328-28). conflict_rate = contended/enqueued is THE post-sharding
    signal: if it stays high after g-328-27, that justifies the
    remote-lock-table conditional-write escalation the BRD names."""
    from ..server import Response  # local import — avoids cycle at module load
    try:
        from _write_queue import metrics_snapshot
        return Response.json({"ok": True, **metrics_snapshot()})
    except Exception as e:  # pragma: no cover — metrics must never 500 health tooling
        return Response.json({"ok": False,
                              "detail": type(e).__name__ + ": " + str(e)[:200]})


def register(routes) -> None:
    routes[("GET", "/v1/admin/health")] = health
    routes[("GET", "/v1/admin/write-queue")] = write_queue
