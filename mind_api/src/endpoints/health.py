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

import time
from pathlib import Path

from .. import __version__, read_git_head_sha


_START_TIME = time.monotonic()
# Snapshot git HEAD at startup. Wrappers compare this against current
# on-disk HEAD to detect "daemon is running stale code" ().
# Resolved once at import-time so the health probe stays ~10ms.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_STARTUP_SHA = read_git_head_sha(_PROJECT_ROOT)


def health(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response  # local import — avoids cycle at module load
    uptime_s = round(time.monotonic() - _START_TIME, 3)
    return Response.json(
        {
            "ok": True,
            "version": __version__,
            "uptime_s": uptime_s,
            "pid": ctx.pid,
            "port": ctx.port,
            "git_head_sha": _STARTUP_SHA,
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
