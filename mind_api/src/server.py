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

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import lifecycle
from . import stats as _stats
from .agent_paths import AgentPathResolver, AgentPaths
from .endpoints import load_all


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

    __slots__ = ("method", "path", "query", "body", "paths", "pid", "port", "headers", "tenant")

    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        body: bytes,
        paths: AgentPaths,
        pid: int,
        port: int,
        headers: Dict[str, str],
        tenant: str = "default",
    ):
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.paths = paths
        self.pid = pid
        self.port = port
        self.headers = headers
        self.tenant = tenant


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

            agent_header = self.headers.get("X-Mind-Agent", "")
            paths = self.resolver.resolve(agent_header)

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
                paths=paths,
                pid=self.pid,
                port=self.port,
                headers={k.lower(): v for k, v in self.headers.items()},
                tenant=tenant_header,
            )

            handler = self.routes.get((method, path))
            if handler is None:
                resp = Response.error(404, "not_found", f"no route for {method} {path}")
            else:
                resp = handler(ctx)

            self._write_response(resp)
        except Exception as e:  # pragma: no cover — defensive
            # : no repr(e) — repr of a UnicodeError embeds the whole
            # failed payload and re-triggers the surrogate cascade (empty body).
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

        # AF_INET ipv4 only — Decision 6 binds to 127.0.0.1.
        try:
            self._http = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
        except OSError as e:
            self._log_lifecycle("bind_failed", port=port, error=repr(e))
            raise

        self.actual_port = self._http.server_address[1]
        # Finalise handler globals once we know our own port/pid.
        handler_cls.pid = os.getpid()
        handler_cls.port = self.actual_port

        lifecycle.write_pid_and_port_atomic(self.project_root, os.getpid(), self.actual_port)
        self._log_lifecycle("started", port=self.actual_port, pid=os.getpid())

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
