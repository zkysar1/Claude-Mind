"""HTTP request router and Response shape.

stdlib http.server.ThreadingHTTPServer is the transport. Concurrent requests
are handled on a thread pool managed by the stdlib. Endpoints are pure
functions; thread safety is the cache + jsonl_cache's job.

Why ThreadingHTTPServer over asyncio: stdlib + zero learning curve + every
endpoint we have is millisecond-scale I/O. If benchmarks ever show thread
contention dominating (Decision 1, Q1), we swap in aiohttp here without
touching endpoints.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import lifecycle
from . import stats as _stats
from .agent_paths import AgentPathResolver, AgentPaths
from .endpoints import load_all


# --- Request body normalization --------------------------------------------

def _normalize_request_body(raw: bytes) -> bytes:
    """Normalize a POST body to valid UTF-8 bytes at the single door.

    Some clients (Windows shells using the cp1252 codepage) encode the body
    in CP1252 rather than UTF-8 — an em-dash arrives as the lone byte 0x97
    instead of the UTF-8 sequence 0xE2 0x80 0x94. Every endpoint then does a
    strict ``body.decode("utf-8")``; that raises UnicodeDecodeError and
    surfaces as a 500, silently dropping the agent's write (board posts,
    reasoning-bank lessons, working-memory, experience — learning/coordination
    DATA LOSS). Normalizing once HERE means all ~20 endpoint decode sites see
    clean UTF-8 with zero per-endpoint changes (single source of truth).

    We re-decode invalid bytes as cp1252 (which maps 0x97 -> U+2014 em-dash)
    then re-encode UTF-8, RECOVERING the intended character rather than
    blanking it to U+FFFD. cp1252 has 5 undefined byte positions; errors=
    "replace" covers only those rare cases. Symmetric with the response-side
    surrogatepass in Response.text/json. (Lodestar B5; reinforced by rb-739:
    the cp1252<->utf-8 transform is the canonical mojibake recovery.)
    """
    if not raw:
        return raw
    try:
        raw.decode("utf-8")
        return raw  # already valid UTF-8 — the overwhelmingly common case
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace").encode("utf-8")


# --- Response shape --------------------------------------------------------

class Response:
    """Endpoint return value.

    Body is bytes on the wire. `text()` and `json()` encode for you; the
    raw `__init__` form is for endpoints that need exact byte control.
    """

    __slots__ = ("status", "body", "content_type")

    def __init__(self, status: int, body: bytes, content_type: str = "application/octet-stream"):
        self.status = status
        self.body = body
        self.content_type = content_type

    @classmethod
    def text(cls, s: str, status: int = 200, content_type: str = "text/plain") -> "Response":
        #  FIX A: surrogatepass — the aspirations/read endpoint emits
        # JSON via Response.text(json.dumps(..., ensure_ascii=False)); a
        # rehydrated lone surrogate (persisted U+DC9D) must not nuke the whole
        # response with a strict-utf-8 UnicodeEncodeError. Mirrors line 54.
        return cls(status, s.encode("utf-8", "surrogatepass"), content_type)

    @classmethod
    def json(cls, obj: Any, status: int = 200) -> "Response":
        return cls(
            status,
            json.dumps(obj, ensure_ascii=False).encode("utf-8", "surrogatepass"),
            "application/json",
        )

    @classmethod
    def error(cls, status: int, code: str, detail: str = "") -> "Response":
        return cls.json({"error": code, "detail": detail}, status=status)


# --- Request context --------------------------------------------------------

class RequestContext:
    """Per-request bag passed to every handler.

    Holds the parsed query, the resolved agent paths, the daemon's own PID
    and port, and the request body bytes for POST endpoints.
    """

    __slots__ = ("method", "path", "query", "body", "_paths", "_paths_factory",
                 "pid", "port", "headers", "tenant")

    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        body: bytes,
        paths: Optional[AgentPaths],
        pid: int,
        port: int,
        headers: Dict[str, str],
        tenant: str = "default",
        paths_factory: Optional[Callable[[], AgentPaths]] = None,
    ):
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self._paths = paths
        self._paths_factory = paths_factory
        self.pid = pid
        self.port = port
        self.headers = headers
        self.tenant = tenant

    @property
    def paths(self) -> AgentPaths:
        """Resolved agent paths — resolved LAZILY on first access (g-367-03).

        On a pre-init deployment (no world configured yet, no local-paths.conf,
        no .mind-data/) AgentPathResolver.resolve() raises RuntimeError. Eager
        per-request resolution 500'd EVERY route including /v1/admin/health,
        whose contract is "always succeeds when the daemon is up" — breaking
        the spawn-confirmation probe in mind-api-start.sh:66. Deferring to
        first ACCESS lets path-free admin endpoints serve pre-init while
        endpoints that touch paths keep the identical loud RuntimeError
        (mapped to 500 by _serve's generic handler). Loud-at-use preserved.
        """
        if self._paths is None and self._paths_factory is not None:
            self._paths = self._paths_factory()
        return self._paths


# --- HTTP handler -----------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """Dispatches HTTP requests to registered endpoints.

    The handler is intentionally thin: parse method + path + query + body,
    look up the route, call the endpoint, write the Response. Endpoints
    should hold no daemon-wide state — they reach into shared caches via
    module singletons (e.g. jsonl_cache.cache()) so concurrency is local
    to the cache, not the handler.
    """

    # Owned by Server (set after construction).
    routes: Dict
    resolver: AgentPathResolver
    pid: int
    port: int
    access_log_path: Path

    # Quiet the default per-request stderr log. We write our own JSON
    # access log instead.
    def log_message(self, format, *args):  # noqa: A003 - stdlib API
        return

    def do_GET(self):  # noqa: N802 - stdlib API
        self._serve("GET")

    def do_POST(self):  # noqa: N802 - stdlib API
        self._serve("POST")

    def _serve(self, method: str) -> None:
        started = time.monotonic()
        # Pre-bind so the access_log in the finally clause has something to
        # read even if an exception fires before they're set in the try.
        resp: Optional[Response] = None
        agent_header = ""
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = self._flatten_qs(parse_qs(parts.query, keep_blank_values=True))

            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            # B5: recover a cp1252-encoded body (Windows em-dash 0x97 etc.) to
            # valid UTF-8 once, so every endpoint's strict body.decode("utf-8")
            # sees clean bytes instead of 500-ing and dropping the write.
            body = _normalize_request_body(body)

            agent_header = self.headers.get("X-Mind-Agent", "")
            # g-367-03: do NOT resolve here. Resolution happens lazily at the
            # first ctx.paths access (see RequestContext.paths) so a pre-init
            # deployment can still serve /v1/admin/health and the other
            # path-free admin routes.

            # X-Mind-Tenant: H9-light tenant seam (Phase 5). Today the
            # value is propagated through ctx for future use but does NOT
            # select a non-default (world, meta) pair — the default tenant
            # is `paths.tenant` (Tenant(world=paths.world, meta=paths.meta)
            # from agent_paths.py). H5 (multi-tenant runtime, post-stop)
            # will extend AgentPathResolver to map the header value to a
            # non-default Tenant. HTTP headers are case-insensitive
            # (RFC 7230); .get() on http.client.HTTPMessage is already
            # case-insensitive.
            tenant_header = self.headers.get("X-Mind-Tenant", "") or "default"

            ctx = RequestContext(
                method=method,
                path=path,
                query=query,
                body=body,
                paths=None,
                pid=self.pid,
                port=self.port,
                headers={k.lower(): v for k, v in self.headers.items()},
                tenant=tenant_header,
                paths_factory=lambda: self.resolver.resolve(agent_header),
            )

            # FR-4 (daemon auth, BRD Shared-State-API): bearer-token
            # AUTHENTICATION for remote fleet clients, GATED behind MIND_API_TOKEN
            # (unset = today's localhost-only no-auth default → byte-identical
            # legacy behavior, NFR-1 back-compat). When set, EVERY request MUST
            # present a matching `Authorization: Bearer <token>`; a missing or
            # wrong token is refused 401 BEFORE any handler, tenant check, or
            # backend call. This is authentication (401 — who are you), distinct
            # from the tenant authz below (403 — you may not touch this customer).
            # Constant-time compare (hmac.compare_digest) so a wrong token cannot
            # be timing-probed. Paired with the FR-5 fail-closed bind guard in
            # start(): a non-loopback bind REQUIRES this token to be set, so the
            # daemon can never expose a routable interface without authentication.
            _api_token = os.environ.get("MIND_API_TOKEN", "").strip()
            if _api_token:
                _auth = ctx.headers.get("authorization", "")
                _presented = (_auth[7:].strip()
                              if _auth[:7].lower() == "bearer " else "")
                if not _presented or not hmac.compare_digest(_presented, _api_token):
                    resp = Response.error(
                        401, "unauthorized",
                        "missing or invalid bearer token")
                    self._write_response(resp)
                    return

            # T-c (): multi-tenant customer activation + app-authz,
            # GATED behind MIND_MULTI_TENANT (default OFF = today's single-tenant
            # deployment). OFF: the tenant header is still propagated through
            # ctx.tenant (the R4 seam, unchanged) but does NOT scope storage keys
            # and is NOT authz-checked — byte-identical legacy behavior. ON: the
            # daemon is authenticated for exactly one customer (MIND_CUSTOMER,
            # default "default"); a mismatched X-Mind-Tenant is rejected 403
            # BEFORE any handler/backend call (defense in depth behind the IAM
            # prefix-conditions), and the matching customer is activated so the
            # OwnCloudBackend key builders carry the <customer>/ prefix. The
            # set/reset live in storage_backend (the cloud-SDK-free seam) so this
            # path never imports the cloud backend (LocalBackend hosts stay clean).
            _cust_token = None
            if os.environ.get("MIND_MULTI_TENANT", "").strip().lower() in (
                    "1", "true", "yes"):
                from storage_backend import set_customer, reset_customer
                authed_customer = (os.environ.get("MIND_CUSTOMER", "")
                                   or "default").strip().strip("/") or "default"
                if ctx.tenant != authed_customer:
                    resp = Response.error(
                        403, "tenant_forbidden",
                        f"X-Mind-Tenant {ctx.tenant!r} not authorized for this "
                        f"daemon (serves {authed_customer!r})")
                    self._write_response(resp)
                    return
                _cust_token = set_customer(ctx.tenant)

            try:
                handler = self.routes.get((method, path))
                if handler is None:
                    resp = Response.error(404, "not_found", f"no route for {method} {path}")
                else:
                    resp = handler(ctx)
            finally:
                # Reset within the request so the customer never leaks to the next
                # request on a reused thread (defensive — ThreadingHTTPServer
                # spawns per-request threads today, but a future pool must be safe).
                if _cust_token is not None:
                    reset_customer(_cust_token)

            self._write_response(resp)
        except Exception as e:  # pragma: no cover — defensive
            # own-cloud RMW conflict (#38): an If-Match stale-lock-break
            # ConflictError — the remote object moved between a handler's
            # in-lock fresh read and its PUT (the DDB lock was force-broken by
            # a crashed/reclaimed holder on another machine) — is a TRANSIENT
            # optimistic-concurrency conflict, not an internal fault. Map it to
            # a 409 the caller can safely retry, distinct from a 500. The
            # high-value shared-store handlers retry in-process
            # (file_locks.locked_rmw) so most conflicts never reach here; this
            # is the universal floor for every other handler. isinstance against
            # the backend's conflict_error is () on LocalBackend (matches
            # nothing — zero behavior change off own-cloud) and ConflictError on
            # OwnCloudBackend. Lazy import keeps server.py importable on a
            # LocalBackend-only host without the cloud-backend dependencies.
            _is_conflict = False
            try:
                from storage_backend import get_backend
                _is_conflict = isinstance(e, get_backend().conflict_error)
            except Exception:
                _is_conflict = False
            # : per-path FIFO backpressure (queue full / wait
            # timeout) is orderly load-shedding, not an internal fault —
            # map to 429 with the distinct error code the BRD requires
            # (callers must never see a raw "Could not acquire lock" for
            # daemon-queued paths). Lazy import, same pattern as above.
            _is_backpressure = False
            try:
                from _write_queue import WriteQueueBackpressure
                _is_backpressure = isinstance(e, WriteQueueBackpressure)
            except Exception:
                _is_backpressure = False
            if _is_backpressure:
                resp = Response.error(
                    429, "write_queue_backpressure",
                    type(e).__name__ + ": " + str(e)[:300])
            elif _is_conflict:
                # . Report only what the exception LICENSES. The
                # previous text asserted "remote changed ... safe to retry" —
                # both derived from the exception TYPE, neither observed. This
                # handler never reads remote state, so "remote changed" is a
                # claim about something it did not look at, and "safe to retry"
                # is advice built on that claim.
                #
                # Measured counter-case (, 2026-07-29): the remote was
                # STATIC — double-HEAD returned the same version 15s apart —
                # while the local mirror sat 11,057 bytes behind. 8 attempts over
                # ~2.5min of escalating backoff all conflicted identically,
                # because retrying cannot fix a stale local read. Worse, the
                # message asserted the OPPOSITE of persistence, so guard-908's
                # retry-playbook trigger could not fire. A self-declared-retryable
                # error that is not retryable is worse than no advice: it is
                # confident, specific, and wrong, so nothing prompts a re-look.
                #
                # What IS known here: the write was rejected on a version
                # precondition and did not land. The CAUSE is not measured, and
                # the two candidates need OPPOSITE responses — so name both and
                # hand over the discriminator instead of picking one.
                resp = Response.error(
                    409, "write_conflict",
                    "optimistic-concurrency conflict: the write was rejected on "
                    "a version precondition and did NOT land. WHY it failed is "
                    "NOT measured here — a live remote change and a STALE LOCAL "
                    "MIRROR raise this same error and need opposite responses "
                    "(retry vs. refresh first). Discriminate before retrying: "
                    "HEAD the remote twice; an UNCHANGED version means local is "
                    "behind and every retry will conflict identically (g-115-3782)")
            else:
                # : no repr(e) — repr of a UnicodeError embeds the
                # whole failed payload and re-triggers the surrogate cascade
                # (empty body).
                detail = type(e).__name__ + ": " + str(e)[:200]
                resp = Response.error(500, "internal_error", detail)
            try:
                self._write_response(resp)
            except Exception:
                pass
        finally:
            dur_ms = (time.monotonic() - started) * 1000.0
            self._access_log(method, dur_ms, resp, agent_header)

    @staticmethod
    def _flatten_qs(qs: Dict[str, list]) -> Dict[str, str]:
        # parse_qs gives lists per key; flatten to last-value for caller ergonomics.
        return {k: v[-1] for k, v in qs.items() if v}

    def _write_response(self, resp: Response) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("X-Runtime-Version", __version__)
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    def _access_log(self, method: str, dur_ms: float, resp: Optional[Response], agent: Optional[str]) -> None:
        # Record into reservoir-sampled stats (for /v1/admin/stats). Key by
        # method + path-WITHOUT-query so /v1/aspirations/read?summary=1 and
        # /v1/aspirations/read?active=1 group as one endpoint.
        try:
            path_only = urlsplit(self.path).path
            _stats.collector().record(f"{method} {path_only}", dur_ms)
        except Exception:
            pass

        # Extract error code + detail from the JSON body for non-2xx
        # responses. Cheap (response body is already JSON-encoded), and the
        # whole point of access-log is post-hoc diagnosis — bare status code
        # is useless for distinguishing "lock timeout" from "validation
        # failed" from "internal error". Body parse failure → skip silently.
        err_code = None
        err_detail = None
        if resp is not None and resp.status >= 400 and resp.body:
            try:
                err = json.loads(resp.body.decode("utf-8"))
                if isinstance(err, dict):
                    err_code = err.get("error")
                    err_detail = err.get("detail")
                    if isinstance(err_detail, str) and len(err_detail) > 200:
                        err_detail = err_detail[:200]
            except Exception:
                pass

        try:
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "method": method,
                "path": self.path,
                "agent": agent or "",
                "status": resp.status if resp else 0,
                "duration_ms": round(dur_ms, 3),
            }
            if err_code is not None:
                record["error"] = err_code
            if err_detail is not None:
                record["detail"] = err_detail
            line = json.dumps(record, ensure_ascii=False)
            with self.access_log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# --- Server -----------------------------------------------------------------


class _BackloggedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a listen backlog sized for a shared daemon.

    request_queue_size (the socket listen backlog passed to socket.listen())
    defaults to 5 in the stdlib. When many agents share one daemon, a burst
    of >5 simultaneous wrapper connects overflows the backlog and the kernel
    refuses the excess connects. A refused connect surfaces to the wrapper as
    connection-refused (rc=3), which spuriously respawns a busy-but-alive
    daemon and orphans it — the orphan-accumulation source observed under a
    multi-agent fleet (~1 orphan every ~90s under a 6-agent fleet). 128 covers
    the fleet's burst with wide margin; the OS clamps the value to the
    platform backlog ceiling, so a too-large value is harmless.
    """

    request_queue_size = 128


class Server:
    """The daemon. Holds state, starts the HTTP listener, manages PID/port files."""

    def __init__(self, project_root: Path, port: int = 0):
        self.project_root = project_root
        self.requested_port = port
        self.actual_port: Optional[int] = None
        self.resolver = AgentPathResolver(project_root)
        self.routes = load_all()
        self._http: Optional[ThreadingHTTPServer] = None

    # --- lifecycle ---

    def start(self) -> int:
        """Bind to 127.0.0.1:<port>, write PID/port files, start serving.

        Returns the bound port. Blocks the caller — Server.serve_forever()
        runs until SIGTERM or Server.stop().
        """
        access_log_path = lifecycle.access_log(self.project_root)

        # Capture closure state for the handler subclass.
        handler_cls = type(
            "_BoundHandler", (_Handler,), {
                "routes": self.routes,
                "resolver": self.resolver,
                "access_log_path": access_log_path,
            },
        )

        port = self.requested_port if self.requested_port > 0 else 0

        # AF_INET ipv4 only. FR-5 (guarded remote bind, BRD
        # Shared-State-API): the daemon binds 127.0.0.1 by DEFAULT
        # (Decision 6 — unchanged, back-compat). A non-loopback bind (e.g. a
        # Tailscale IP so remote fleet clients can reach ONE canonical daemon,
        # fleet-BRD §10a / Option B) is opt-in via MIND_API_BIND and is
        # FAIL-CLOSED: refused unless FR-4 auth (MIND_API_TOKEN) is enabled, so
        # the daemon can never expose a routable interface without authentication.
        bind_addr = os.environ.get("MIND_API_BIND", "").strip() or "127.0.0.1"
        if bind_addr not in {"127.0.0.1", "::1", "localhost"} and not \
                os.environ.get("MIND_API_TOKEN", "").strip():
            raise RuntimeError(
                f"FATAL: MIND_API_BIND={bind_addr!r} is a non-loopback interface "
                "but MIND_API_TOKEN is not set — refusing to bind a routable "
                "interface without authentication (FR-5 fail-closed).")
        try:
            self._http = _BackloggedHTTPServer((bind_addr, port), handler_cls)
        except OSError as e:
            self._log_lifecycle("bind_failed", port=port, error=repr(e))
            raise

        self.actual_port = self._http.server_address[1]
        # Finalise handler globals once we know our own port/pid.
        handler_cls.pid = os.getpid()
        handler_cls.port = self.actual_port

        #  v3 fix: capture the launcher (py.exe on Windows, shell on
        # POSIX) parent PID at spawn time. The kill path reads this file so
        # it can force-kill the parent WITHOUT a Win32 .ParentProcessId
        # lookup that silently no-ops once the child has gracefully exited
        # from SIGTERM. Behavior when getppid() returns the test harness or
        # debugger PID (not a launcher) is safe: the kill path does a
        # CommandLine sanity check before Stop-Process, so a non-py.exe parent
        # never gets killed by accident.
        parent_pid = os.getppid()
        lifecycle.write_pid_and_port_atomic(
            self.project_root, os.getpid(), self.actual_port,
            parent_pid=parent_pid,
        )
        self._log_lifecycle("started", port=self.actual_port, pid=os.getpid(),
                            parent_pid=parent_pid)

        try:
            self._http.serve_forever()
        finally:
            lifecycle.clear_runtime_files(self.project_root)
            self._log_lifecycle("stopped")
        return self.actual_port

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None

    # --- logging ---

    def _log_lifecycle(self, event: str, **extra: Any) -> None:
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "event": event,
            "version": __version__,
            **extra,
        }, ensure_ascii=False)
        try:
            with lifecycle.daemon_log(self.project_root).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        # Echo to stderr so a foreground `python -m mind_api.src` shows progress.
        print(f"[runtime] {line}", file=sys.stderr)
