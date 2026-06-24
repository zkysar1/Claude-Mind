# domain-leak-exempt: own-cloud readiness branch -- the S3 head_bucket and
# DynamoDB GetItem calls are functional infrastructure probes against the real
# services OwnCloudBackend uses (mirrors core/scripts/owncloud_backend.py), not a
# domain leak. The local branch and the /health-vs-/ready split stay domain-free;
# only the own-cloud probe names the concrete services it must reach.
"""GET /v1/admin/ready — readiness probe with a store round-trip.

Distinct from /v1/admin/health (pure liveness — always 200 when the daemon is
up). /ready does a cheap authenticated round-trip against the ACTIVE storage
backend so a credential rotation / store outage fails LOUD (503) instead of the
silent multi-minute degradation where /health stayed 200 while every store op
failed (the 2026-06-17 MIND_AWS-rotation outage that motivated g-328-06).

The split is deliberate:
  - /health stays store-independent because the wrapper auto-start logic gates
    "is the daemon accepting requests yet?" on it — making it store-dependent
    would break cold start under a degraded store. DO NOT add store checks to
    /health.
  - /ready answers the orthogonal question "can the daemon actually serve store
    ops right now?" — which is the signal a monitor / health-aggregator needs to
    page on a credential outage.

Backends:
  - local: the store IS local files; daemon-up == store-available. Returns 200
    {ready:true, store_check:"skipped-local"} without any I/O.
  - own-cloud (or any future remote backend): two bounded round-trips on the
    daemon's scoped creds — an S3 head_bucket AND a DynamoDB GetItem on the
    sessions table (the daemon depends on BOTH services; see _probe_store).
    200 {ready:true, store_check:"ok"} when both succeed; 503 {ready:false, ...}
    naming the failing leg on a ClientError (403 / expired token / missing
    bucket-or-table), a backend-construction failure (bad creds/env), an import
    failure, or a probe timeout.

Cheap + non-blocking: the server is ThreadingHTTPServer (each request runs on
its own thread — no shared event loop to stall), and BOTH store calls run
sequentially inside a single worker thread FURTHER bounded by one join-timeout,
so /ready always answers within a few seconds even if the network partitions.
Each call is a single authenticated request (~10-50ms in the common case), so
the probe is far cheaper than any real store op.
"""
from __future__ import annotations

import os
import sys
import threading
import time


# Upper bound on how long /ready waits for the store round-trip before declaring
# a timeout. boto's own client carries retries (max_attempts=3); a hard network
# partition could otherwise stall the head_bucket for tens of seconds, which
# would make /ready itself hang — defeating the point of a readiness probe.
_PROBE_TIMEOUT_S = 5.0


# Partition-key value for the DynamoDB readiness probe. The sessions table is
# keyed by a single "session_key" (S) attribute (see
# OwnCloudBackend.get_runner_state). A GetItem on a key that is never written
# returns an empty Item (no error), so the probe validates creds + table
# access + the dynamodb:GetItem perm the daemon's runner-state reads actually
# use — WITHOUT depending on any item existing (degrade-safe: no sentinel row
# to provision, mirroring head_bucket's no-sentinel-object property).
_DDB_PROBE_SESSION_KEY = "__ready_probe__"


def _probe_store(backend) -> tuple[str, str]:
    """Run cheap authenticated round-trips against a remote backend in a bounded
    worker thread — BOTH service legs the daemon depends on (g-328-06).

    The daemon's store ops split across two AWS services on the same scoped
    creds: S3 (governed-file object read/write) and DynamoDB (the runner-claim
    rows the lodestar single-runner enforcement reads/writes). A credential
    rotation invalidates BOTH at once (the 2026-06-18 rb-2022 outage that
    motivated this probe), but a divergent IAM policy (rb-2026: scoped creds
    activated before the policy was attached) can break ONE leg while the other
    works — so a readiness probe that checked only S3 would stay 200/green on a
    DDB-only outage. Probe both; fail on the first leg that errors.

    Returns ``(status, detail)``:
      ("ok",      "")              — head_bucket AND the DDB GetItem both succeeded
      ("error",   "<leg>: <msg>")  — one leg raised; detail names the leg (s3/ddb)
      ("timeout", "<msg>")         — a round-trip did not finish within _PROBE_TIMEOUT_S

    The worker is a daemon thread: if either call hangs past the join-timeout,
    /ready still answers promptly and the leaked thread is reaped by boto's own
    client timeout. head_bucket is the canonical S3 reachability probe — a 404
    (bucket missing) and a 403 (creds invalid) BOTH surface as ClientError,
    while a 200 proves the creds reach the bucket right now. The DDB GetItem on
    the never-written sentinel key is the canonical DDB read probe — empty Item
    on success (no provisioned row required), ClientError on stale creds /
    missing table / missing perm — exercising the same dynamodb:GetItem path the
    daemon's runner-state reads use.
    """
    result: dict = {}

    def _run() -> None:
        # S3 leg first.
        try:
            backend.s3.head_bucket(Bucket=backend.bucket)
        except Exception as e:  # noqa: BLE001 — any S3 failure means not-ready
            result["status"] = "error"
            result["detail"] = f"s3: {type(e).__name__}: {e}"
            return
        # DDB leg — same scoped creds, the orthogonal service the daemon's
        # runner claims depend on. GetItem on a never-written key returns an
        # empty Item, so success requires no provisioned sentinel row.
        try:
            backend.ddb.get_item(
                TableName=backend.sessions_table,
                Key={"session_key": {"S": _DDB_PROBE_SESSION_KEY}},
            )
        except Exception as e:  # noqa: BLE001 — any DDB failure means not-ready
            result["status"] = "error"
            result["detail"] = f"ddb: {type(e).__name__}: {e}"
            return
        result["status"] = "ok"
        result["detail"] = ""

    t = threading.Thread(target=_run, name="ready-store-probe", daemon=True)
    t.start()
    t.join(_PROBE_TIMEOUT_S)
    if t.is_alive():
        return ("timeout", f"store probe exceeded {_PROBE_TIMEOUT_S}s")
    return (
        result.get("status", "error"),
        result.get("detail", "probe produced no result"),
    )


def ready(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response  # local import — avoids cycle at module load

    backend_name = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend_name in ("", "local", "local-files"):
        # Local store == local files; daemon liveness IS store availability. No
        # remote round-trip exists to fail, so skip the probe entirely.
        return Response.json(
            {
                "ready": True,
                "backend": backend_name or "local",
                "store_check": "skipped-local",
            }
        )

    # Remote backend (own-cloud today) — exercise the store creds.
    # core/scripts must be importable for storage_backend (mirrors admin.py).
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from storage_backend import get_backend
    except Exception as e:  # noqa: BLE001 — import broke: degraded, report don't raise
        return Response.json(
            {
                "ready": False,
                "backend": backend_name,
                "store_check": "import-failed",
                "error": f"{type(e).__name__}: {e}",
            },
            status=503,
        )
    try:
        backend = get_backend()
    except Exception as e:  # noqa: BLE001 — backend construction failed (bad creds/env)
        return Response.json(
            {
                "ready": False,
                "backend": backend_name,
                "store_check": "backend-init-failed",
                "error": f"{type(e).__name__}: {e}",
            },
            status=503,
        )

    t0 = time.monotonic()
    status, detail = _probe_store(backend)
    probe_ms = round((time.monotonic() - t0) * 1000, 1)
    if status == "ok":
        return Response.json(
            {
                "ready": True,
                "backend": backend_name,
                "store_check": "ok",
                "probe_ms": probe_ms,
            }
        )
    return Response.json(
        {
            "ready": False,
            "backend": backend_name,
            "store_check": status,  # "error" | "timeout"
            "error": detail,
            "probe_ms": probe_ms,
        },
        status=503,
    )


def register(routes) -> None:
    routes[("GET", "/v1/admin/ready")] = ready
