"""GET /v1/admin/stats — per-endpoint latency distribution.

Returns the current reservoir snapshot for every (method, path) the daemon
has served since startup. Useful for ops dashboards and drift detection
against the bench harness.

Schema:
    {
      "stats": {
        "GET /v1/aspirations/read": {
          "count": 1234,                  # total observed since start
          "samples_in_window": 1024,      # reservoir occupancy
          "min_ms": 1.2,
          "p50_ms": 15.3,
          "p95_ms": 78.1,
          "p99_ms": 134.0,
          "max_ms": 5482.1,
          "avg_ms": 22.4
        },
        ...
      }
    }
"""
from __future__ import annotations

import os
import sys

from ..stats import collector


def stats(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.json({"stats": collector().snapshot()})


def owncloud_flush(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/owncloud-flush — force an immediate own-cloud mirror sweep.

    Pushes every governed-dir file this machine owns (world, meta, owned-agent
    dirs INCLUDING session/ continuity files) to S3 *now*, instead of waiting
    for the periodic sweep thread's next tick (default 120s — see
    `__main__._OWNCLOUD_SYNC_DEFAULT_INTERVAL`). Called by the /stop graceful
    handler (aspirations-graceful-stop D6.7) so a machine-move immediately
    after a clean /stop cannot strand the session's last continuity writes
    (handoff.yaml, working-memory.yaml, execution-diary.jsonl, ...) on the
    local disk where the next machine would never see them.

    SSOT: invokes the SAME `owncloud_sync.sweep()` the periodic thread calls
    (`__main__._start_owncloud_sync_thread`), with identical arguments — there
    is no second sweep code path to drift from. `full=False` keeps the
    manifest mtime-skip, so unchanged files are cheap; the agent's
    just-consolidated session files have fresh mtimes and are pushed.

    No-op under the local backend (the local files ARE the store — nothing to
    mirror). Fully defensive: an import or sweep error returns 500 with the
    reason rather than raising, so the /stop sequence can warn-and-proceed
    (the periodic sweep is still running in this daemon and will retry).
    """
    from ..server import Response
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend != "own-cloud":
        return Response.json({
            "backend": backend, "flushed": False,
            "reason": "non-own-cloud backend — local files are the store",
        })
    # core/scripts must be importable for owncloud_sync + storage_backend. The
    # periodic sweep thread inserts this at daemon start under own-cloud (see
    # _start_owncloud_sync_thread), but a flush could be the first own-cloud
    # caller before the thread's settle delay elapsed — insert defensively.
    # ctx.paths.project_root is the framework repo root (parent of core/).
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import owncloud_sync
        from storage_backend import get_backend
    except Exception as e:  # noqa: BLE001 — import broke: report, don't raise
        return Response.json(
            {"backend": backend, "flushed": False,
             "error": f"import failed: {e}"}, status=500)
    try:
        stats_d = owncloud_sync.sweep(
            get_backend(), only_root=None, dry_run=False,
            use_manifest=True, full=False)
    except Exception as e:  # noqa: BLE001 — a bad sweep must not 500-with-stack
        return Response.json(
            {"backend": backend, "flushed": False,
             "error": f"sweep failed: {e}"}, status=500)
    return Response.json({
        "backend": backend, "flushed": True,
        "pushed": stats_d.get("pushed", 0),
        "scanned": stats_d.get("scanned", 0),
        "in_sync": stats_d.get("in_sync", 0),
        "skipped_unchanged": stats_d.get("skipped_unchanged", 0),
        "conflicts": stats_d.get("conflicts", 0),
        "errors": stats_d.get("errors", 0),
    })


def owncloud_pull(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/owncloud-pull?agent=<name> — pull <name>'s continuity-tier
    session files from S3 to local, freshness-aware.

    The read-side complement of the /stop flush: at /start on a NEW machine the
    local copy of the agent's session/ is stale or absent, while S3 holds the
    last machine's flushed handoff.yaml / working-memory.yaml / execution-diary.jsonl
    / ... Called by the /start IDLE branch (via owncloud-pull.sh) BEFORE boot does
    its raw Read of handoff.yaml, so the resume reads the latest cross-machine
    state instead of a stale local file.

    SSOT: invokes owncloud_sync.pull_continuity(), which NEVER clobbers a local
    file carrying unpushed local writes (the same-machine crash-restart case) —
    the manifest baseline gates every overwrite. No-op under the local backend;
    import/pull errors return 500 with the reason rather than raising, so the
    /start step can warn-and-proceed.
    """
    from ..server import Response
    agent = (ctx.query.get("agent") or "").strip()
    if not agent:
        return Response.json(
            {"ok": False, "error": "agent query param required"}, status=400)
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend != "own-cloud":
        return Response.json({
            "backend": backend, "ok": False,
            "reason": "non-own-cloud backend — local files are the store",
        })
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import owncloud_sync
        from storage_backend import get_backend
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False,
             "error": f"import failed: {e}"}, status=500)
    try:
        stats = owncloud_sync.pull_continuity(get_backend(), agent)
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False,
             "error": f"pull failed: {e}"}, status=500)
    # pull_continuity sets stats["error"] on a fail-closed no-pull (untrustworthy
    # manifest / no agents root) — surface that as ok=False without a 500.
    ok = "error" not in stats
    return Response.json({"backend": backend, "ok": ok, **stats})


def register(routes) -> None:
    routes[("GET", "/v1/admin/stats")] = stats
    routes[("POST", "/v1/admin/owncloud-flush")] = owncloud_flush
    routes[("POST", "/v1/admin/owncloud-pull")] = owncloud_pull
