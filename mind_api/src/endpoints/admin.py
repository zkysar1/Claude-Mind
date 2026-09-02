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
import time

from ..stats import collector


def stats(ctx) -> "Response":  # type: ignore[name-defined]
    from ..server import Response
    return Response.json({"stats": collector().snapshot()})


def owncloud_flush(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/owncloud-flush[?agent=<name>] — force an immediate own-cloud mirror sweep.

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
    # Optional per-agent scope (design §6 /stop flush): ?agent=<name> narrows the
    # sweep to agents/<name>/ with full=True (re-HEAD every file) so the stopping
    # agent's dir is guaranteed complete in S3 BEFORE its claim is released. The
    # ownership filter inside sweep() still prunes the dir if this machine does
    # NOT own it, so the agent param can never push a peer's cache. Absent the
    # param, the flush keeps its full-owned-set behavior (world/meta/all owned
    # agents, manifest mtime-skip) — the periodic-sweep race-closer.
    agent = (ctx.query.get("agent") or "").strip()
    try:
        if agent:
            stats_d = owncloud_sync.sweep(
                get_backend(), only_root="agents", dry_run=False,
                use_manifest=True, full=True, only_agent=agent)
        else:
            stats_d = owncloud_sync.sweep(
                get_backend(), only_root=None, dry_run=False,
                use_manifest=True, full=False)
    except Exception as e:  # noqa: BLE001 — a bad sweep must not 500-with-stack
        return Response.json(
            {"backend": backend, "flushed": False,
             "error": f"sweep failed: {e}"}, status=500)
    return Response.json({
        "backend": backend, "flushed": True,
        "scope": f"agent:{agent}" if agent else "all-owned",
        "pushed": stats_d.get("pushed", 0),
        "scanned": stats_d.get("scanned", 0),
        "in_sync": stats_d.get("in_sync", 0),
        "skipped_unchanged": stats_d.get("skipped_unchanged", 0),
        "conflicts": stats_d.get("conflicts", 0),
        "errors": stats_d.get("errors", 0),
        "pruned_agents": stats_d.get("pruned_agents", 0),
        # WHICH dirs are unpushable from this box, not just how many
        # (guard-1579). Sorted so the wrapper's summary line is stable across
        # runs and diffable; the sweep appends in os.walk order.
        "pruned_agent_names": sorted(stats_d.get("pruned_agent_names", []) or []),
        # Same principle as pruned_agent_names four lines up, applied to the
        # counter that actually signals loss (). guard-1579 got the
        # identity list for PRUNES into this payload; `errors` sat beside it
        # naming nothing, so a sweep reporting errors=8 told an operator
        # nothing about WHICH eight files are unpushable from this box.
        **({"error_paths": stats_d["error_paths"]}
           if stats_d.get("error_paths") else {}),
        **({"error_paths_truncated": stats_d["error_paths_truncated"]}
           if stats_d.get("error_paths_truncated") else {}),
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
    # `only` (): comma-separated continuity filenames to narrow the
    # pull to, skipping the temp/ sweep. Lets a caller that needs ONE file per
    # agent (/open-questions refreshing every peer's pending-questions.yaml)
    # avoid a full ~900-file sweep per agent. Absent/empty -> unchanged full pull.
    only_raw = (ctx.query.get("only") or "").strip()
    only = {p.strip() for p in only_raw.split(",") if p.strip()} or None
    # `with_temp` (): opt INTO the temp/ working-doc sweep, which is
    # OFF by default because temp/ is not continuity-tier. Absent -> continuity
    # set only (17 objects), which is what makes this endpoint's cost bounded by
    # the manifest instead of by scratch population. See pull_continuity's
    # docstring for the measurement and the machine-move trade-off.
    with_temp = (ctx.query.get("with_temp") or "").strip().lower() in (
        "1", "true", "yes")
    # `adopt_store` (): comma-separated continuity filenames whose
    # MANIFEST BASELINE is dropped before the freshness gate runs, so S3 is
    # adopted as canonical. For the three-way-divergence wedge only -- local md5
    # != baseline md5 != S3 md5 -- which the no-clobber gate reads as "unpushed
    # local writes" and skips PERMANENTLY, because nothing below the pull ever
    # re-derives a baseline. Adds no overwrite path: it routes the file into
    # _pull_one's existing no-baseline branch, which snapshots local to .history
    # first. Per-path opt-in BY DESIGN, never a sweep -- `local != baseline`
    # alone does not prove divergence, and adopting a genuinely-unpushed local
    # write would destroy it. Absent/empty -> unchanged no-clobber pull.
    adopt_raw = (ctx.query.get("adopt_store") or "").strip()
    adopt_store = {p.strip() for p in adopt_raw.split(",") if p.strip()} or None
    try:
        stats = owncloud_sync.pull_continuity(get_backend(), agent, only=only,
                                              include_temp=with_temp,
                                              adopt_store=adopt_store)
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False,
             "error": f"pull failed: {e}"}, status=500)
    # pull_continuity sets stats["error"] on a fail-closed no-pull (untrustworthy
    # manifest / no agents root) — surface that as ok=False without a 500.
    ok = "error" not in stats
    return Response.json({"backend": backend, "ok": ok, **stats})


def owncloud_sync_file(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/owncloud-sync-file?path=<file>[&dry_run=1] — push ONE
    governed file to S3, using the daemon's creds.

    The per-file complement of owncloud-flush: the PostToolUse push shim
    (core/scripts/owncloud-push-on-write.sh) calls this so a governed Write/
    Edit propagates in seconds instead of waiting for the ~120s periodic
    sweep — the wait window that breeds both-moved conflict freezes
    (g-115-2447; backlog forensics in g-115-2446). Also the manual push path
    for reconcile-owncloud-conflicts Step 5 on environment-config
    deployments, where the bare-CLI fallback lacks the daemon-only creds.

    SSOT: invokes the SAME `owncloud_sync.sync_file()` the CLI `--file` mode
    runs (multi_machine=False → local IS authoritative → push). It does NOT
    stamp a manifest baseline, and this docstring claimed twice that it did
    until g-115-5356 measured it: sync_file calls _sync_one without
    baseline_md5, discards the md5 it returns, and never opens the manifest.
    Callers must not treat a 200 here as proof a baseline now exists.
    Safe against arbitrary paths: sync_file's own governed-root /
    machine-local / peer-agent filters decide skips; this endpoint reports
    the skip reason rather than second-guessing them. `dry_run=1` maps to
    the shim's OWNCLOUD_PUSH_HOOK_DRYRUN liveness probe. No-op under the
    local backend; import/sync errors return 500-with-reason, never a raise
    (the shim is guard-141 fail-open and must be able to warn-and-proceed).
    """
    from ..server import Response
    from pathlib import Path
    raw_path = (ctx.query.get("path") or "").strip()
    if not raw_path:
        return Response.json(
            {"ok": False, "error": "path query param required"}, status=400)
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend != "own-cloud":
        return Response.json({
            "backend": backend, "ok": True, "pushed": 0,
            "reason": "non-own-cloud backend — local files are the store",
        })
    target = Path(raw_path)
    if not target.is_absolute():
        target = ctx.paths.project_root / target
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
    dry_run = (ctx.query.get("dry_run") or "").strip() in ("1", "true", "yes")
    stats: dict = {}
    try:
        rc = owncloud_sync.sync_file(
            get_backend(), target, dry_run=dry_run, stats_out=stats)
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False, "path": str(target),
             "error": f"sync failed: {e}"}, status=500)
    return Response.json({
        "backend": backend, "ok": rc == 0, "path": str(target),
        "dry_run": dry_run,
        "pushed": stats.get("pushed", 0),
        "would_push": stats.get("would_push", 0),
        "in_sync": stats.get("in_sync", 0),
        "conflicts": stats.get("conflicts", 0),
        "diverged_skipped": stats.get("diverged_skipped", 0),
        "errors": stats.get("errors", 0),
        # WHICH object failed and why, not just how many ().
        # _record_error has stored {path, phase, exc, msg} since 2026-08-11;
        # this payload hand-enumerates counters, so it dropped the one field
        # carrying the identity — and owncloud-push-on-write.sh prints this body
        # VERBATIM in its failure warning, so `errors: 1` reached an operator
        # with no cause attached. Measured
        # 2026-08-22 on a world tree-node body push: three push paths each
        # returned errors:1 with no reason, and the reason existed the whole
        # time. The sibling owncloud_pull endpoint splats **stats and has
        # always carried it; these whitelists were the outliers. Key name is
        # the producer's verbatim (guard-3408).
        **({"error_paths": stats["error_paths"]}
           if stats.get("error_paths") else {}),
        **({"error_paths_truncated": stats["error_paths_truncated"]}
           if stats.get("error_paths_truncated") else {}),
        **({"reason": stats["reason"]} if "reason" in stats else {}),
    })


def _local_claim_store(ctx):
    """Git-ref claim store for a non-own-cloud backend, or None when unusable.

    The local arm of single-runner enforcement (g-306-331). Before this existed,
    every non-own-cloud backend returned a no-op success from the four runner
    endpoints, so a local-backend deployment had NO cross-machine claim at all:
    nothing stopped two boxes running the same agent and neither could see the
    other. git's rejected non-fast-forward push supplies the same
    compare-and-swap the own-cloud record store was providing, with no new
    service.

    Returns None — preserving the historical no-op exactly — when git or a
    remote is missing. Never raises: a claim-store construction failure must
    degrade to today's behaviour, not 500 the endpoint.
    """
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from git_ref_claim import GitRefClaimStore
    except Exception:  # noqa: BLE001 — absent module ⇒ historical no-op
        return None
    root = str(ctx.paths.project_root)
    try:
        # RUNNER_CLAIM_REMOTE → a `claims` remote → origin (see default_remote:
        # a read-only origin is the wrong arbiter for a self-contained box).
        remote = GitRefClaimStore.default_remote(root)
        if not GitRefClaimStore.available(root, remote):
            return None
        return GitRefClaimStore(
            repo_root=root,
            env_id=os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
            remote=remote,
        )
    except Exception:  # noqa: BLE001 — same degrade-to-no-op contract
        return None


def _runner_preamble(ctx, *, need_token: bool = True):
    """Shared front-half for the three runner-claim endpoints (design §4):
    validate query params, short-circuit non-own-cloud backends, ensure
    core/scripts is importable, and return get_backend(). Returns a tuple
    ``(backend_name, get_backend_callable, early_response)`` where exactly one of
    the latter two is non-None: ``early_response`` is set (and must be returned
    as-is) for the bad-request / non-own-cloud / import-error short-circuits;
    otherwise ``get_backend_callable`` is the resolved ``get_backend`` import."""
    from ..server import Response
    agent = (ctx.query.get("agent") or "").strip()
    token = (ctx.query.get("token") or "").strip()
    if not agent or (need_token and not token):
        return (None, None, Response.json(
            {"ok": False, "error": "agent and token query params required"},
            status=400))
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    if backend != "own-cloud":
        # Local arm: a git ref supplies the CAS the own-cloud store does ().
        # Returned as a callable so the four endpoints keep calling
        # `get_backend().<method>(...)` unchanged — the store implements the
        # same runner method names as OwnCloudBackend.
        store = _local_claim_store(ctx)
        if store is not None:
            return (backend, (lambda: store), None)
        # No git/remote — fall back to the historical no-op success, which keeps
        # the caller's gated path uniform across backends.
        return (backend, None, Response.json(
            {"backend": backend, "ok": True, "noop": True, "claim_store": False,
             "reason": "non-own-cloud backend with no usable git remote — "
                       "no cross-machine claim store"}))
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from storage_backend import get_backend
    except Exception as e:  # noqa: BLE001 — import broke: report, don't raise
        return (backend, None, Response.json(
            {"backend": backend, "ok": False, "error": f"import failed: {e}"},
            status=500))
    return (backend, get_backend, None)


def runner_acquire(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/runner-acquire?agent=<name>&token=<uuid> — acquire the DDB
    runner claim (IDLE->RUNNING CAS) for <name> on this machine.

    The cross-machine half of single-runner enforcement (lodestar dynamic-
    ownership design §4): /start calls this alongside the filesystem
    session-state-set RUNNING. On success this machine owns <name> for sync; on
    RunnerHeld (another machine holds a live claim) the caller refuses the
    autonomous start, mirroring the local runner-identity refusal. §5
    stale-lock-break: on RunnerHeld this endpoint first attempts a conditional
    reclaim of a CRASHED peer's frozen claim (heartbeat older than
    runner_stale_seconds) and retries acquire once — so a crash-no-release can
    never PIN ownership; only a genuinely-LIVE peer yields {"held": true} (a
    successful stale reclaim returns acquired=true, reclaimed_stale=true).
    {"held": true} is a NORMAL 200 answer (a live peer owns it), not an error.
    No-op under the local
    backend; import/DDB errors return 500 with the reason (the /start gate decides
    — fail-open so a transient DDB hiccup never blocks a legitimate start)."""
    from ..server import Response
    agent = (ctx.query.get("agent") or "").strip()
    token = (ctx.query.get("token") or "").strip()
    backend, get_backend, early = _runner_preamble(ctx)
    if early is not None:
        return early
    try:
        from owncloud_backend import RunnerHeld
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False, "error": f"import failed: {e}"},
            status=500)
    try:
        from git_ref_claim import GitRefClaimError as _StoreUnwritable
    except Exception:  # noqa: BLE001 — own-cloud arm: no git store in play
        class _StoreUnwritable(Exception):  # type: ignore[no-redef]
            """Placeholder so the except clause below is always well-formed."""
    try:
        get_backend().acquire_runner(agent, token)
    except _StoreUnwritable as e:
        # The claim store could not be WRITTEN — no credential for the remote,
        # an unreachable host, a server-side refusal — which is a plumbing
        # failure, NOT a held claim. Before this clause (2026-08-27, coach-mind
        # on zc-03) the store reported a failed push as a lost CAS race, this
        # endpoint answered {"held": true}, and runner-claim.sh told the operator
        # "another machine owns a live claim for this agent" while the claim
        # namespace was empty. Answer with the remote's own words instead; the
        # wrapper prints FAILED (rc=2), and /start halts on a TRUE diagnosis.
        return Response.json(
            {"backend": backend, "ok": False, "store_error": True,
             "error": f"claim store unwritable: {e}"}, status=500)
    except RunnerHeld:
        # §5 stale-lock-break: a crashed runner never reaches /stop, so its claim
        # sits RUNNING with a frozen heartbeat_at. Without recovery that stale
        # claim PINS ownership forever — no peer could ever acquire <name>.
        # Attempt a conditional reclaim (RUNNING->IDLE iff heartbeat older than
        # runner_stale_seconds); if it fires, the crashed claim is broken and we
        # retry acquire ONCE. A genuinely-live peer (fresh heartbeat) is NOT
        # reclaimed (the conditional check fails) so we still answer held=true.
        # reclaim+re-acquire is the same race-safe conditional-CAS pair a peer
        # /start would run (design §5): two machines cannot both win.
        #
        # Capture the previous holder BEFORE the reclaim so a successful
        # stale-break can report WHO it broke and HOW stale the claim was
        # (prev_machine_id + prev_heartbeat_age_seconds). Without this, the
        # caller cannot distinguish "row was IDLE, clean acquire" from "broke
        # a peer's stale claim" — the 2026-07-07 bravo dual-runner incident
        # was narrated as "no live peer detected" precisely because the
        # stale-break was invisible. Best-effort: a read failure must never
        # abort the reclaim path.
        try:
            prev = get_backend().get_runner_state(agent)
        except Exception:  # noqa: BLE001 — diagnostics only, never fatal
            prev = None
        try:
            reclaimed = get_backend().reclaim_if_stale(agent)
            if reclaimed:
                get_backend().acquire_runner(agent, token)
                resp = {"backend": backend, "ok": True, "acquired": True,
                        "held": False, "reclaimed_stale": True}
                if prev:
                    try:
                        hb = int(prev.get("heartbeat_at") or 0)
                        resp["prev_machine_id"] = prev.get("machine_id")
                        resp["prev_heartbeat_age_seconds"] = max(
                            0, int(time.time()) - hb)
                    except (TypeError, ValueError):
                        pass
                return Response.json(resp)
        except RunnerHeld:
            pass  # raced: another machine acquired between our reclaim and retry
        except Exception as e:  # noqa: BLE001 — reclaim/retry failure is non-fatal
            return Response.json(
                {"backend": backend, "ok": False,
                 "error": f"reclaim-retry failed: {e}"}, status=500)
        # Name the holder on the plain-held answer too, reusing the `prev` read
        # taken above — this path performs NO additional backend call. Measured
        # (-a Q1, live end-to-end against production own-cloud): the
        # held body was exactly {backend, ok, acquired, held} — 4 keys, no
        # machine_id, no heartbeat_at — so a caller could say only that SOMEONE
        # holds the claim. runner-claim.sh printed "another machine owns a live
        # claim", and core/config/start-phase-c.md (the UNINITIALIZED first-boot
        # path) HALTs on that rc=4 with no holder identity anywhere in its text,
        # so that sentence was the entire diagnosis a first-boot user received.
        # Deliberately NOT prev_* : that prefix means "the claim I just broke",
        # and on this path nothing was broken — the holder is CURRENT and live.
        # CONDITIONAL by design, same as the stale-break fields above: omitted
        # when the runner_state row is unreadable, so consumers MUST handle
        # absence rather than assume presence (-a found three response
        # shapes, not two).
        held_resp = {"backend": backend, "ok": True, "acquired": False,
                     "held": True}
        if prev:
            try:
                hb = int(prev.get("heartbeat_at") or 0)
                held_resp["holder_machine_id"] = prev.get("machine_id")
                held_resp["holder_heartbeat_age_seconds"] = max(
                    0, int(time.time()) - hb)
            except (TypeError, ValueError):
                pass
        return Response.json(held_resp)
    except Exception as e:  # noqa: BLE001 — a bad acquire must not 500-with-stack
        return Response.json(
            {"backend": backend, "ok": False, "error": f"acquire failed: {e}"},
            status=500)
    return Response.json(
        {"backend": backend, "ok": True, "acquired": True, "held": False})


def runner_heartbeat(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/runner-heartbeat?agent=<name>&token=<uuid> — refresh the DDB
    runner claim's heartbeat_at (token-conditional).

    Called from the per-iteration heartbeat tick so the DDB heartbeat advances
    with the local file mtime (design §4). Token-conditional in the backend: a
    reclaimed runner cannot resurrect its heartbeat (mismatch raises). No-op under
    the local backend; errors return 500 (heartbeat-tick.sh fails open on this — a
    DDB hiccup must never block an iteration)."""
    from ..server import Response
    agent = (ctx.query.get("agent") or "").strip()
    token = (ctx.query.get("token") or "").strip()
    backend, get_backend, early = _runner_preamble(ctx)
    if early is not None:
        return early
    try:
        get_backend().heartbeat(agent, token)
    except Exception as e:  # noqa: BLE001 — token-mismatch (reclaimed) or DDB error
        return Response.json(
            {"backend": backend, "ok": False, "error": f"heartbeat failed: {e}"},
            status=500)
    return Response.json({"backend": backend, "ok": True, "beat": True})


def runner_release(ctx) -> "Response":  # type: ignore[name-defined]
    """POST /v1/admin/runner-release?agent=<name>&token=<uuid> — clean RUNNING->IDLE
    release of the DDB runner claim (token-conditional, idempotent).

    Called at /stop AFTER the final S3 flush (design §4/§6). The backend returns
    transitioned=False (NOT an error) when the claim was already reclaimed/idle,
    so /stop always succeeds. No-op under the local backend; errors return 500
    (the /stop sequence warns and proceeds)."""
    from ..server import Response
    agent = (ctx.query.get("agent") or "").strip()
    token = (ctx.query.get("token") or "").strip()
    backend, get_backend, early = _runner_preamble(ctx)
    if early is not None:
        return early
    try:
        transitioned = get_backend().release_runner(agent, token)
    except Exception as e:  # noqa: BLE001
        return Response.json(
            {"backend": backend, "ok": False, "error": f"release failed: {e}"},
            status=500)
    return Response.json(
        {"backend": backend, "ok": True, "released": bool(transitioned)})


def runner_claims(ctx) -> "Response":  # type: ignore[name-defined]
    """GET /v1/admin/runner-claims — list every runner claim under this env-id.

    FR-7 fleet observability: returns all DDB session rows for the current
    ENVIRONMENT_ID — agent name, owning machine_id, agent_state (RUNNING/IDLE),
    heartbeat_at (epoch-sec), and runner_token_fp — so a fleet-health view can
    show which machine owns each agent's RUNNING slot and how fresh its heartbeat
    is. Read-only, no agent/token param (unlike the acquire/heartbeat/release
    trio). Env-scoped in the backend (`list_runner_claims` filters on the
    `<customer><env-id>/` prefix), so a fleet env never sees prod's rows. No-op
    under the local backend (empty claims list); import/DDB errors return 500 with
    the reason (a read-only health probe must degrade to a diagnostic, never a
    stack).

    `runner_token_fp` (g-306-224) is a NON-REVERSIBLE digest of the row's
    `runner_token`, and this endpoint MUST NEVER be extended to return the raw
    token. The token is the `ConditionExpression` bearer credential for
    `heartbeat` and `release_runner`, so publishing it here would let any reader
    forge a heartbeat for another agent (defeating `reclaim_if_stale`) or release
    a live claim out from under a healthy reducer — the exact failures the lease
    exists to prevent. The full argument, and the reason the raw value is not even
    representable on `RunnerClaim`, is in `owncloud_backend.runner_token_fingerprint`.
    The fingerprint carries everything a liveness consumer needs: it CHANGES on a
    same-box reducer restart (new token, unchanged machine_id), which `machine_id`
    alone cannot see, and it authorises nothing."""
    from ..server import Response
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
    local_store = None
    if backend != "own-cloud":
        # Local arm (). Unlike the three mutating endpoints this one is
        # a read, so an empty list here is what `runner-claim.sh status` reads as
        # "no live runner" — hence `claim_store`, which distinguishes "asked a
        # real store and it was empty" from "there was no store to ask".
        local_store = _local_claim_store(ctx)
        if local_store is None:
            return Response.json(
                {"backend": backend, "ok": True, "claims": [],
                 "claim_store": False,
                 "reason": "non-own-cloud backend with no usable git remote — "
                           "no cross-machine claim store"})
    scripts_dir = str(ctx.paths.project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from storage_backend import get_backend
    except Exception as e:  # noqa: BLE001 — import broke: report, don't raise
        return Response.json(
            {"backend": backend, "ok": False, "error": f"import failed: {e}"},
            status=500)
    try:
        be = local_store if local_store is not None else get_backend()
        claims = be.list_runner_claims()
    except Exception as e:  # noqa: BLE001 — DDB Scan error must not 500-with-stack
        msg = str(e)
        payload = {"backend": backend, "ok": False,
                   "error": f"list failed: {msg}"}
        # Actionable hint for the most common operational failure of THIS
        # endpoint: the daemon's scoped creds lack dynamodb:Scan on the sessions
        # table. list_runner_claims Scans; acquire/heartbeat/release do not — so
        # a pre-FR-7 IAM policy (granted only Get/Put/Update) leaves exactly this
        # read denied while the ownership trio still works. Observed 2026-07-01
        # on the ayoai-mind dev creds vs zds-sessions. Fix: grant dynamodb:Scan
        # (mind_api/scripts/provision_aws.py build_policy already does) — see
        # decision-doc §6.
        if "AccessDenied" in msg and "Scan" in msg:
            payload["hint"] = (
                "the daemon's IAM identity lacks dynamodb:Scan on the sessions "
                "table for this ENVIRONMENT_ID — grant dynamodb:Scan (see "
                "mind_api/scripts/provision_aws.py build_policy) or re-run the "
                "provisioner for this env; the acquire/heartbeat/release trio is "
                "unaffected (it does not Scan).")
        return Response.json(payload, status=500)
    return Response.json({
        "backend": backend, "ok": True,
        # TRUE means "a real claim store answered", which is what distinguishes
        # an empty `claims` list from the absence of any store to ask. Callers
        # must treat a MISSING field as the legacy predicate (`backend ==
        # "own-cloud"`), never as false — an older daemon omits it entirely and
        # would otherwise start reporting every own-cloud box as unclaimable.
        "claim_store": True,
        "environment_id": os.environ.get("ENVIRONMENT_ID", "ayoai-mind"),
        # The freshness threshold a caller must apply to heartbeat_at to decide
        # "live" vs "stale". Reported here rather than re-derived caller-side so
        # there is ONE source of truth: this value was recalibrated 900 -> 3900
        # after the 2026-07-07 bravo dual-runner incident, and a caller carrying
        # its own copy would have silently kept stale-breaking live runners.
        # `getattr` default None = "this daemon cannot tell you" — callers must
        # treat that as UNREADABLE (refuse), never as fresh (guard-487).
        "runner_stale_seconds": getattr(be, "runner_stale_seconds", None),
        # Git-ref arm only: the stderr of the most recent FAILED fetch of the
        # claim namespace, else None. When set, `claims` is the LAST-KNOWN LOCAL
        # state, not the remote's — an empty list here must never be read as
        # "no holder" (2026-08-27 coach-mind: `[]` beside failing acquires).
        "store_error": getattr(be, "last_fetch_error", None),
        "claims": [
            {"agent": c.agent, "machine_id": c.machine_id,
             "agent_state": c.agent_state, "heartbeat_at": c.heartbeat_at,
             # Digest, never the raw token — see the docstring above. `None` on a
             # never-claimed IDLE row, which a consumer must read as UNKNOWN
             # (non-discriminating), never as "unchanged".
             "runner_token_fp": getattr(c, "runner_token_fp", None)}
            for c in claims
        ],
    })


def register(routes) -> None:
    routes[("GET", "/v1/admin/stats")] = stats
    routes[("POST", "/v1/admin/owncloud-flush")] = owncloud_flush
    routes[("POST", "/v1/admin/owncloud-pull")] = owncloud_pull
    routes[("POST", "/v1/admin/owncloud-sync-file")] = owncloud_sync_file
    routes[("POST", "/v1/admin/runner-acquire")] = runner_acquire
    routes[("POST", "/v1/admin/runner-heartbeat")] = runner_heartbeat
    routes[("POST", "/v1/admin/runner-release")] = runner_release
    routes[("GET", "/v1/admin/runner-claims")] = runner_claims
