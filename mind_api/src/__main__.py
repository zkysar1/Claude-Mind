"""Entry point: `python3 -m mind_api.src [--port N] [--foreground]`.

The daemon resolves PROJECT_ROOT as the parent of `mind_api/` containing this
module — same convention the existing shell scripts use. Port defaults to 0
(OS-assigned), discoverable via mind_api/state/daemon.port.

The daemon never daemonises itself. Backgrounding is the wrapper's job
(`_runtime.sh` uses `nohup` on POSIX, `start /B` on Windows). The daemon
just binds, writes PID/port, and serves until the process exits.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from . import lifecycle
from .server import Server

# Stale spawn-lock TTL. If a crashed previous spawn left the lock file behind,
# any spawn attempt older than this is treated as dead and the file is reused.
# 30s is much longer than a healthy spawn (≤2s for bind + ready) but short
# enough that operator-visible delays remain bounded.
_SPAWN_LOCK_STALE_SECONDS = 30

# Self-supersession poll interval (seconds). The daemon's idle main thread
# re-reads mind_api/state/daemon.pid every interval; if it no longer names this
# process a newer daemon has taken over and this one exits (orphan self-reap).
# One tiny file read per interval on an otherwise-blocked thread — far below
# any operator-perceptible orphan-pileup window.
_SUPERSEDE_CHECK_SECONDS = 10

# B15: cadence (seconds) of the own-cloud governed-dir mirror sweep. The sweep is
# incremental (an mtime manifest skips unchanged files), so a tick is an os.walk +
# local stat per governed file plus an S3 HEAD/PUT only for files that changed
# since the last tick. 120s keeps cross-machine staleness of raw-write / LLM-tool
# writes bounded without per-tick S3 cost on a quiescent tree. Override via
# OWNCLOUD_SYNC_INTERVAL to override.
_OWNCLOUD_SYNC_DEFAULT_INTERVAL = 120


def _project_root() -> Path:
    """Resolve PROJECT_ROOT — the directory containing `mind_api/`."""
    return Path(__file__).resolve().parent.parent.parent


# N3 de-overload (2026-06-05, user sign-off): the daemon loads from .env.local
# ONLY this EXACT set of storage-backend config keys, plus the MIND_AWS_* scoped
# credential FAMILY (prefix) and AWS_DEFAULT_REGION. Replaces the old broad
# `startswith("MIND_")` filter — MIND_ now means EXCLUSIVELY the credential family,
# so a stray host STORAGE_*/ENVIRONMENT_* var in .env.local cannot leak into the
# daemon env. Hard cutover, no back-compat: .env.local MUST use the new key names
# (the daemon will not see an old MIND_-prefixed storage key). Adding a NEW daemon
# config var means adding its exact key here (intentional friction — the security
# boundary is explicit).
_N3_ALLOWED_EXACT = frozenset({
    "MACHINE_ID", "MACHINE_MULTI", "RUNTIME_DIR", "STORAGE_BACKEND",
    "STORAGE_S3_BUCKET", "STORAGE_DDB_SESSIONS_TABLE", "STORAGE_DDB_LOCK_TABLE",
    "OWNCLOUD_SYNC_INTERVAL", "OWNCLOUD_CACHE_TTL",
    "ENVIRONMENT_ID",
    # FR-4/FR-5 (BRD Shared-State-API): daemon auth token (a secret,
    # loaded like the MIND_AWS_* scoped-credential family) + the opt-in
    # non-loopback bind interface. Both default-absent → localhost-only no-auth
    # (byte-identical legacy behavior). MIND_API_BIND!=loopback fail-closes in
    # server.start() unless MIND_API_TOKEN is set.
    "MIND_API_TOKEN", "MIND_API_BIND",
})


def _load_env_local(project_root: Path) -> None:
    """Populate ``os.environ`` from ``.env.local`` for the keys the daemon needs
    to select + configure the storage backend (the ``own-cloud`` cutover, s7).

    The daemon does NOT otherwise read ``.env.local`` (it inherits its env from
    the spawning shell — ``WORLD_PATH``/``META_PATH`` arrive that way via
    ``_paths.sh``). Without this, a ``STORAGE_BACKEND=own-cloud`` flip in
    ``.env.local`` would never reach ``get_backend()`` and the daemon would stay
    on the ``local`` default — a silent split-brain.

    Security (N3, 2026-06-05): loads ONLY the EXACT ``_N3_ALLOWED_EXACT`` storage
    config keys + the ``MIND_AWS_*`` scoped-credential family + ``AWS_DEFAULT_REGION``
    (a non-secret region string). It deliberately NEVER loads the root
    ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` (reserved on this deployment
    for unrelated lambda access, NOT the daemon — the ``OwnCloudBackend.from_env``
    fail-closed guard exists for exactly this): they are neither in the exact
    allowlist nor under the ``MIND_AWS_`` prefix, so they are excluded by
    construction. N3 collapsed the old broad ``MIND_`` prefix to EXCLUSIVELY the
    credential family. ``setdefault`` so an explicit launch-env value always wins;
    an already-set var is never clobbered.
    """
    env_local = project_root / ".env.local"
    try:
        text = env_local.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        if not (key in _N3_ALLOWED_EXACT or key.startswith("MIND_AWS_")
                or key == "AWS_DEFAULT_REGION"):
            continue
        # First whitespace token is the value (drops a trailing inline comment).
        tok = val.strip().split()[0] if val.strip() else ""
        # A token that IS a comment means the line had a blank value followed by a
        # `# ...` note — e.g. `.env.example` copied to `.env.local` without editing
        # (`MACHINE_ID=  # UNIQUE per-machine id`). Treat as EMPTY so the
        # required-var / G5 fail-closed checks trip VISIBLY, rather than loading a
        # bogus value like "#" that silently passes them (communication-clarity
        # rule 5: fail visibly over a silent inconsistent value). No legitimate
        # config value begins with '#'.
        if tok.startswith("#"):
            tok = ""
        os.environ.setdefault(key, tok)


# Registry key (in core/config/environments/<env-id>.yaml) -> daemon env var.
# The registry is the single source of truth for storage wiring; the operator
# sets ONE value (ENVIRONMENT_ID) and these are DERIVED from it ().
_REGISTRY_KEY_TO_ENV = {
    "backend": "STORAGE_BACKEND",
    "bucket": "STORAGE_S3_BUCKET",
    "sessions_table": "STORAGE_DDB_SESSIONS_TABLE",
    "lock_table": "STORAGE_DDB_LOCK_TABLE",
    "region": "AWS_DEFAULT_REGION",
}


def _environments_dir(project_root: Path) -> Path:
    """Directory holding the committed per-environment registry files."""
    return project_root / "core" / "config" / "environments"


def _valid_environment_ids(project_root: Path) -> "list[str]":
    """Sorted env-ids for which a registry file exists (the ``*.yaml`` stems)."""
    d = _environments_dir(project_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def _apply_environment_registry(project_root: Path) -> None:
    """Derive the storage-backend wiring from the local environment registry
    (``core/config/environments/<ENVIRONMENT_ID>.yaml``) so the operator sets ONE
    value -- ``ENVIRONMENT_ID`` -- instead of four independently-misconfigurable
    ``STORAGE_*`` / ``AWS_DEFAULT_REGION`` vars that can silently mix state across
    environments (g-328-11, BRD environment-id-as-key).

    Runs in ``main()`` AFTER ``_load_env_local`` (which loads ``ENVIRONMENT_ID``
    from ``.env.local``) and BEFORE ``_ensure_owncloud_roots`` / ``get_backend()``
    (which read the derived ``STORAGE_*``). Semantics:

    * ``ENVIRONMENT_ID`` unset -> NO-OP. Legacy N-var mode is fully preserved (the
      zeta pilot / any env that configures ``STORAGE_*`` directly). This is also
      why the hermetic test suite is unaffected: conftest pins
      ``STORAGE_BACKEND=local`` and sets no ``ENVIRONMENT_ID``.
    * ``ENVIRONMENT_ID`` set but NO registry file -> FAIL LOUD (raise) with the
      list of valid ids. A typo'd env-id must never silently fall back to whatever
      ``STORAGE_*`` happens to be set -- that is the exact state-mixing the
      registry prevents. The registry is committed to ``core/`` so it is always
      locally readable (no chicken-and-egg with the own-cloud store).
    * ``ENVIRONMENT_ID`` set WITH a registry file -> derive each ``STORAGE_*`` via
      ``setdefault`` (an explicit launch-env / ``.env.local`` value still WINS --
      the same "explicit wins" contract as ``_load_env_local``) and emit a single
      DEPRECATION warning naming any legacy storage var that co-exists with the
      registry, so the operator migrates to ENVIRONMENT_ID-only. ``setdefault``
      (not hard override) keeps promotion safe: a downstream env still running on
      legacy vars does not change behavior the moment the registry lands.

    An ``own-cloud`` registry entry MUST carry bucket + both DDB tables + region;
    a half-configured own-cloud entry raises rather than silently 500-ing every
    storage op later (same fail-loud rationale as ``_ensure_owncloud_roots``).
    """
    env_id = os.environ.get("ENVIRONMENT_ID", "").strip()
    if not env_id:
        return  # legacy N-var mode -- registry not in play
    reg_file = _environments_dir(project_root) / f"{env_id}.yaml"
    if not reg_file.is_file():
        valid = _valid_environment_ids(project_root)
        raise RuntimeError(
            f"_apply_environment_registry: ENVIRONMENT_ID={env_id!r} has no "
            f"registry entry at {reg_file}. Valid environment ids: "
            f"{valid or '(none -- core/config/environments/ is empty)'}. "
            "Fix: set ENVIRONMENT_ID to a valid id, or add the registry file. "
            "Refusing to start rather than silently mixing state across "
            "environments."
        )
    import yaml  # lazy -- mirrors _ensure_owncloud_roots' local import
    try:
        data = yaml.safe_load(reg_file.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 -- a malformed registry must fail loud
        raise RuntimeError(
            f"_apply_environment_registry: could not parse {reg_file}: {e}"
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(
            f"_apply_environment_registry: {reg_file} must be a YAML mapping, "
            f"got {type(data).__name__}."
        )

    backend = str(data.get("backend", "")).strip().lower()
    # An own-cloud entry must be fully specified -- fail loud on a half config
    # rather than let get_backend() 500 every op later with a missing bucket/table.
    if backend == "own-cloud":
        missing = [k for k in ("bucket", "sessions_table", "lock_table", "region")
                   if not str(data.get(k, "")).strip()]
        if missing:
            raise RuntimeError(
                f"_apply_environment_registry: {reg_file} sets backend=own-cloud "
                f"but is missing required key(s): {missing}. An own-cloud "
                "environment must specify bucket, sessions_table, lock_table, and "
                "region."
            )

    coexisting = []
    for reg_key, env_key in _REGISTRY_KEY_TO_ENV.items():
        reg_val = data.get(reg_key)
        if reg_val is None or str(reg_val).strip() == "":
            continue  # registry omits this key (e.g. local backend has no bucket)
        reg_val = str(reg_val).strip()
        if os.environ.get(env_key) is not None:
            coexisting.append(env_key)  # legacy explicit value present -> it wins
        os.environ.setdefault(env_key, reg_val)

    if coexisting:
        print(
            f"[environment-registry] DEPRECATION: legacy storage env var(s) "
            f"{sorted(coexisting)} are set alongside ENVIRONMENT_ID={env_id!r}. "
            f"The registry (core/config/environments/{env_id}.yaml) is now the "
            "single source of truth for storage wiring; the explicit var(s) still "
            "take precedence for now, but remove them from .env.local and keep "
            "ONLY ENVIRONMENT_ID.",
            file=sys.stderr,
        )


def _ensure_owncloud_roots(project_root: Path) -> None:
    """When the own-cloud backend is selected, ensure the governed roots
    (``WORLD_PATH``/``META_PATH``) are present in ``os.environ`` so
    ``OwnCloudBackend.from_env`` — called lazily by the first storage request —
    can build its root map.

    The roots live in each agent's ``local-paths.conf`` (NOT ``.env.local``) and
    are normally exported by ``_paths.sh`` in the spawning shell. But the daemon
    is frequently (re)spawned by paths that do NOT export them — notably
    ``mind-api-start.sh --restart`` driven by the watchdog / orphan-sweep. On
    those paths the roots never reach the daemon's env and every storage op 500s
    with ``OwnCloudBackend.from_env: ... cannot map a governed path to a root``.
    Resolving the roots here makes the daemon spawn-path-agnostic — the same
    rationale as ``_load_env_local`` for the backend selector.

    Local mode is untouched (zero added I/O): this runs ONLY when
    ``STORAGE_BACKEND == own-cloud``. Uses ``setdefault`` so an explicit
    shell-exported root always wins, and reuses the daemon's own
    ``AgentPathResolver`` — but ITERATES conf-bearing agents (skipping
    ``_``-prefixed test/throwaway dirs) until one resolves, instead of trusting
    ``resolve(None)``'s single first-agent pick. Fail-LOUD: if own-cloud is
    selected and NO agent resolves the governed roots, raise rather than return.
    A daemon that comes up "healthy" but 500s every storage op (because the
    roots stayed unset) is far worse to diagnose than a startup refusal — the
    g-115-1449 incident, where a ``_``-prefixed test agent whose conf lacked
    ``WORLD_PATH`` sorted first, made ``resolve(None)`` raise, and the prior
    silent fail-open then served own-cloud with no roots (rb-1796).
    """
    if os.environ.get("STORAGE_BACKEND", "").strip().lower() != "own-cloud":
        return
    have_world = os.environ.get("MIND_WORLD") or os.environ.get("WORLD_PATH")
    have_meta = os.environ.get("MIND_META") or os.environ.get("META_PATH")
    if have_world and have_meta:
        return
    from .agent_paths import AgentPathResolver
    resolver = AgentPathResolver(project_root)
    # Iterate conf-bearing agents until one resolves valid WORLD+META, rather
    # than trusting resolve(None)'s single first-agent pick. A `_`-prefixed
    # test/throwaway agent dir (e.g. _gate_test_throwaway_agent_) sorts BEFORE
    # real agents; if its conf lacks WORLD_PATH, resolve(None) raises — and the
    # prior silent fail-open then served own-cloud with NO roots, 500ing every
    # governed-path op (9). Skip `_`-prefixed dirs and try each real
    # agent; the first that resolves wins.
    candidates = [c.parent.name
                  for c in sorted(resolver._agents_root().glob("*/local-paths.conf"))
                  if not c.parent.name.startswith("_")]
    paths = None
    errors = []
    for _name in candidates:
        try:
            paths = resolver.resolve(_name)
            break
        except Exception as e:  # noqa: BLE001 — try the next conf-bearing agent
            errors.append(f"{_name}: {e}")
    if paths is None:
        # FAIL LOUD. own-cloud is selected but no agent resolves the governed
        # roots. A startup refusal is far easier to diagnose than a daemon that
        # comes up healthy and 500s every storage op with unset roots (rb-1796).
        raise RuntimeError(
            "_ensure_owncloud_roots: STORAGE_BACKEND=own-cloud but no "
            f"conf-bearing agent resolved WORLD/META roots (tried "
            f"{len(candidates)}: {errors or 'no agents/*/local-paths.conf found'}). "
            "Refusing to serve own-cloud with unset roots. Fix: ensure an "
            "agents/<name>/local-paths.conf carries WORLD_PATH+META_PATH, or "
            "export MIND_WORLD/MIND_META before launch."
        )
    os.environ.setdefault("WORLD_PATH", str(paths.world))
    os.environ.setdefault("META_PATH", str(paths.meta))
    os.environ.setdefault("AGENTS_ROOT", str(paths.agent.parent))


def _start_owncloud_sync_thread(project_root: Path, shutdown: "threading.Event"):
    """B15: under the own-cloud backend, run a periodic incremental mirror sweep
    so governed-dir files persisted by RAW write paths (and LLM Write/Edit-tool
    writes) that bypass the storage backend still reach S3 — i.e. actually sync
    across machines. Each machine's daemon mirrors ITS machine's writes; the
    daemon already has the scoped creds + governed roots in os.environ (see
    _load_env_local / _ensure_owncloud_roots), so get_backend() works here.

    No-op under the local backend (the local files ARE the store — nothing to
    mirror). Fully defensive: an import failure disables the sweep without
    touching the daemon; every tick is wrapped so a transient S3/credential error
    logs and retries next interval, never killing the thread or the process. The
    thread is daemon=True (dies with the process) and waits on `shutdown` so a
    graceful stop ends it promptly. Tune cadence via OWNCLOUD_SYNC_INTERVAL (clamped to >=30s).
    To pause sync, stop the daemon (mind-api-stop.sh).

    Returns the started Thread, or None when the sweep is not applicable."""
    if os.environ.get("STORAGE_BACKEND", "local").strip().lower() != "own-cloud":
        return None
    try:
        interval = int(os.environ.get("OWNCLOUD_SYNC_INTERVAL",
                                      str(_OWNCLOUD_SYNC_DEFAULT_INTERVAL)))
    except ValueError:
        interval = _OWNCLOUD_SYNC_DEFAULT_INTERVAL
    interval = max(30, interval)

    scripts_dir = str(project_root / "core" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    def _loop():
        try:
            import owncloud_sync
            from storage_backend import get_backend
        except Exception as e:  # noqa: BLE001 — import broke: disable, don't churn
            print(f"[owncloud-sync] import failed; mirror sweep disabled: {e}",
                  file=sys.stderr)
            return
        # Fresh-box firmware materialization (): ONE-TIME pull of the
        # governed non-agent firmware (world/scripts) from S3 so bare-bash
        # world/scripts/*.sh (email-send.sh, output-style-mode-guard.sh) work on
        # day 1 of a fresh own-cloud clone. Runs BEFORE the settle delay so a
        # fresh box materializes ASAP, but AFTER the daemon is already serving
        # (this thread starts post-publish), so it is off the spawn critical path
        # — a slow first pull can never delay daemon publish / trigger a
        # spawn-timeout daemon storm. Marker-gated (one-time per box) + fully
        # fail-open, so warm boots skip at ~1 stat and a bad pull never kills the
        # sweep thread.
        try:
            mstats = owncloud_sync.materialize_firmware(get_backend(), project_root)
            if mstats.get("pulled") or mstats.get("errors"):
                print(f"[owncloud-materialize] roots={mstats.get('materialized_roots')} "
                      f"pulled={mstats.get('pulled', 0)} in_sync={mstats.get('in_sync', 0)} "
                      f"errors={mstats.get('errors', 0)}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — materialization must never kill the sweep
            print(f"[owncloud-materialize] error (non-fatal): {e}", file=sys.stderr)
        # Settle delay: don't fire the periodic PUSH sweep during startup churn.
        if shutdown.wait(timeout=min(interval, 30)):
            return
        while not shutdown.is_set():
            t0 = time.monotonic()
            try:
                stats = owncloud_sync.sweep(
                    get_backend(), only_root=None, dry_run=False,
                    use_manifest=True, full=False)
                if stats.get("pushed") or stats.get("errors") or stats.get("conflicts"):
                    print(f"[owncloud-sync] pushed {stats.get('pushed', 0)}, "
                          f"conflicts {stats.get('conflicts', 0)}, "
                          f"errors {stats.get('errors', 0)} "
                          f"(scanned {stats.get('scanned', 0)})", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — a bad tick never kills the thread
                print(f"[owncloud-sync] sweep error (retry next tick): {e}",
                      file=sys.stderr)
            elapsed = time.monotonic() - t0
            if shutdown.wait(timeout=max(1.0, interval - elapsed)):
                break

    t = threading.Thread(target=_loop, name="owncloud-sync", daemon=True)
    t.start()
    print(f"[runtime] own-cloud mirror sweep thread started (interval {interval}s)",
          file=sys.stderr)
    return t


@contextlib.contextmanager
def _spawn_lock(project_root: Path):
    """Serialize the is_daemon_alive() → write_pid_and_port_atomic() window.

    Closes the TOCTOU race observed in mind_api/state/daemon.log: 14 events
    between 2026-05-12 and 2026-05-14 where two daemons started within 30s
    of each other (closest 2s apart). Two concurrent rt_spawn calls both
    passed is_daemon_alive() before either had written the new PID/port,
    producing orphan daemons.

    Implementation: O_CREAT|O_EXCL on a lock file. If acquisition fails AND
    the existing lock file is older than _SPAWN_LOCK_STALE_SECONDS, the
    previous spawn is presumed dead and we steal the lock by unlinking and
    retrying. Cross-platform — no fcntl, no msvcrt, no third-party.
    """
    lock_path = lifecycle.runtime_dir(project_root) / "daemon.spawn.lock"
    acquired = False
    fd = None
    for attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > _SPAWN_LOCK_STALE_SECONDS:
                with contextlib.suppress(FileNotFoundError):
                    lock_path.unlink()
                continue  # retry the O_EXCL create
            raise RuntimeError(
                f"another daemon spawn is in progress (lock {lock_path.name} "
                f"age {age:.1f}s); refusing to race"
            )
    if not acquired:
        raise RuntimeError(f"could not acquire {lock_path.name}")
    try:
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mind_api.src",
        description="Framework runtime daemon (long-running localhost HTTP).",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="Bind port (default 0 = OS-assigned)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    project_root = _project_root()

    # Load .env.local config — the N3 exact-allowlist storage keys + scoped own-cloud
    # creds — into os.environ before any endpoint calls get_backend(). The daemon
    # does not otherwise read .env.local; without this a STORAGE_BACKEND
    # cutover would never take effect in the daemon. See _load_env_local.
    _load_env_local(project_root)

    # Derive storage wiring (STORAGE_BACKEND / STORAGE_S3_BUCKET / STORAGE_DDB_* /
    # AWS_DEFAULT_REGION) from the local environment registry keyed on
    # ENVIRONMENT_ID, so the operator sets ONE value instead of four
    # independently-misconfigurable vars (). Runs AFTER _load_env_local
    # (loads ENVIRONMENT_ID) and BEFORE _ensure_owncloud_roots / get_backend()
    # (read the derived STORAGE_*). No-op when ENVIRONMENT_ID is unset; fails loud
    # on an unknown ENVIRONMENT_ID (no silent state-mixing). See
    # _apply_environment_registry.
    _apply_environment_registry(project_root)

    # When own-cloud is selected, ensure the governed roots (WORLD_PATH/META_PATH)
    # are in os.environ so OwnCloudBackend.from_env can build its root map. They
    # live in local-paths.conf (not .env.local) and do NOT reliably reach the
    # daemon via the spawning shell on every spawn path (notably the watchdog's
    # mind-api-start.sh --restart). Gated on own-cloud, so local mode is
    # untouched. See _ensure_owncloud_roots.
    _ensure_owncloud_roots(project_root)

    # Acquire spawn lock BEFORE the alive-check so concurrent rt_spawn calls
    # serialize. The second caller sees the live daemon written by the first
    # and exits via the "already running" branch below.
    try:
        with _spawn_lock(project_root):
            if lifecycle.is_daemon_alive(project_root):
                port = lifecycle.read_port(project_root)
                print(f"[runtime] daemon already running on port {port}; refusing to start",
                      file=sys.stderr)
                return 2

            lifecycle.clear_runtime_files(project_root)
            server = Server(project_root=project_root, port=args.port)
            # Server.start() writes PID + port files. The spawn lock is held
            # through that write so a racing spawn sees alive() on retry.
            shutdown = threading.Event()

            # CRITICAL: serve_forever runs in a background thread so signals
            # (delivered to the main thread) can call server.stop() without
            # deadlocking. Calling ThreadingHTTPServer.shutdown() from the
            # same thread as serve_forever() deadlocks: shutdown() waits on
            # an Event that only serve_forever()'s finally clause sets, but
            # serve_forever can never run because the main thread is blocked
            # inside shutdown(). Do not "simplify" this by running
            # server.start() in the main thread.
            def _handle_signal(signum, frame):  # pragma: no cover — signals are runtime
                shutdown.set()

            # SIGHUP added to close the orphan-on-parent-exit path observed
            # in mind_api/state/daemon.log: 80 "started" events vs zero "stopped"
            # events since 2026-05-12. On Git Bash + Windows, `disown` does
            # not fully detach; parent shell exit delivers SIGHUP, which
            # without a handler kills the daemon before its "stopped" log
            # write. A handler turns SIGHUP into a graceful shutdown.
            for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, _handle_signal)
                    except (ValueError, OSError):
                        pass

            server_thread = threading.Thread(target=server.start, daemon=True)
            server_thread.start()

            # Wait for the daemon to be FULLY published — both bind succeeded
            # AND PID/port files written to disk. Using is_daemon_alive (not
            # bare actual_port) closes the narrow race where server.start sets
            # actual_port at server.py:273 but write_pid_and_port_atomic runs at
            # line 278 — a concurrent rt_spawn racing in that 5-line window
            # would see no PID file and spawn a second daemon. Hold the spawn
            # lock through write_pid_and_port_atomic so future spawns see
            # is_daemon_alive() == True and bail via the "already running"
            # branch. DO NOT relax this back to checking actual_port alone.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if lifecycle.is_daemon_alive(project_root) or not server_thread.is_alive():
                    break
                time.sleep(0.01)

            if not lifecycle.is_daemon_alive(project_root):
                print("[runtime] failed to bind (server thread exited)", file=sys.stderr)
                return 1
    except RuntimeError as e:
        print(f"[runtime] {e}", file=sys.stderr)
        return 2

    # B15: start the own-cloud governed-dir mirror sweep (no-op under local).
    # Daemon-owned so it runs regardless of whether any agent loop is active and
    # uses the creds/roots already in os.environ — each machine's daemon mirrors
    # its own writes to S3.
    _start_owncloud_sync_thread(project_root, shutdown)

    # Spawn lock released. Serving happens outside the lock so concurrent
    # alive-check spawns see the new PID/port immediately.
    #
    # Self-supersession (the zero-risk census reaper): instead of an
    # unconditional shutdown.wait(), poll the PID file. If it names a
    # DIFFERENT, still-alive process, a newer daemon has superseded this one
    # (rt_spawn / mind-api-start.sh wrote its PID over ours) — so THIS process
    # is now an orphan and exits itself. A daemon can ONLY ever stop ITSELF
    # here, and only when a live successor positively owns the PID file, so
    # this can never kill the live daemon (its own PID is the one in the file
    # → the branch is never taken). read_pid()==None (file mid-rewrite or
    # absent) is fail-open: keep serving, re-check next tick. Pairs with the
    # clear_runtime_files() ownership guard so the superseded exit does not
    # delete the successor's PID/port files.
    while not shutdown.wait(timeout=_SUPERSEDE_CHECK_SECONDS):
        # serve_forever() died (catastrophic socket/loop failure — NOT the
        # signal/supersession path); its finally already cleared our files.
        # Exit so mind-api-start.sh respawns, instead of lingering as a hung
        # not-serving zombie (itself the orphan class  targets).
        # Symmetric with the startup readiness loop's is_alive() check — keep
        # both: a supervisor that ignores its supervised thread dying is the
        # asymmetry, not the safety.
        if not server_thread.is_alive():
            print("[runtime] server thread exited unexpectedly; shutting down",
                  file=sys.stderr)
            break
        owner = lifecycle.read_pid(project_root)
        if owner is not None and owner != os.getpid() and lifecycle.is_pid_alive(owner):
            print(f"[runtime] superseded by live pid {owner} (mine {os.getpid()}); "
                  f"shutting down", file=sys.stderr)
            break
    server.stop()
    server_thread.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
