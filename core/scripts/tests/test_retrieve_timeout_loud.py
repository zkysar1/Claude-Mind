#!/usr/bin/env python3
"""Regression pins for retrieve.sh's loud-failure contract ().

THE DEFECT CLASS: retrieve.sh could exit rc=0 with ZERO bytes on stdout while
the daemon was merely slow — and the caller cannot tell that from a genuine
empty result, so every consult on the slow path silently read as "no
guardrails apply" (a confident wrong answer at rc=0 with empty stderr).
Measured server-side (cc-05 daemon access log): /v1/retrieve 200s at 83-86s
against a 90s client bound; the cost is a ONCE-PER-DAEMON-WARMUP ~73-99s
first call (measured on 3 boxes), after which calls run 2-3s.

THE CONTRACT PINNED HERE (maps to the goal's verification outcomes):
  1. a call that exceeds the client bound exits NON-ZERO with a diagnostic
     naming the endpoint (GET /v1/retrieve) and the elapsed ms;
  2. rc=0 with an EMPTY body is IMPOSSIBLE to mistake for an empty result:
     the wrapper exits 7 (the same distinct code goal-selector.sh uses for
     its g-115-6146 sibling guard) with a FATAL diagnostic — a genuinely
     empty retrieval is a non-empty JSON envelope (measured 93KB for a
     match-nothing category), so zero bytes is always a transport/daemon
     fault;
  3. a normal body passes through byte-identical at rc=0;
  4. the wrapper-local default bound is 240s (above the measured warmup p99)
     and an explicit caller RT_CURL_TIMEOUT is honored unchanged.

Hermetic: a ThreadingHTTPServer plays the daemon on an ephemeral loopback
port via the RT_DIR seam (_runtime.sh reads daemon.port from there);
RT_NO_AUTOSPAWN=1 keeps the wrapper from spawning a real daemon at the fake
runtime dir. Threading matters: a hanging /v1/retrieve handler must not
block the /v1/admin/health probe that rt_no_daemon_error uses to pick its
REACHABLE-vs-unreachable branch (a single-threaded fake wedges health too
and the test asserts the wrong branch — measured while building this file).
"""
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

import sys
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv)

SCRIPTS = _TESTS_DIR.parent
PROJECT_ROOT = SCRIPTS.parent.parent
WRAPPER = SCRIPTS / "retrieve.sh"

NORMAL_BODY = json.dumps({"meta": {"ok": True}, "tree_nodes": []})


class _FakeDaemon(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.mode = "normal"
        self.retrieve_sleep = 20
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep pytest output clean
        pass

    def _send(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/admin/health"):
            self._send(b'{"status":"ok"}')
        elif self.path.startswith("/v1/retrieve"):
            mode = self.server.mode
            if mode == "empty":
                self._send(b"")
            elif mode == "slow":
                time.sleep(self.server.retrieve_sleep)
            elif mode == "fastfail":
                # Close without writing any response: curl exits 52 ("empty
                # reply from server"), which rt_curl maps to rc=3 in a few
                # MILLISECONDS. The threading server keeps answering
                # /v1/admin/health, so rt_no_daemon_error still takes its
                # REACHABLE branch — the exact shape that used to be reported
                # as an RT_CURL_TIMEOUT expiry ().
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            else:
                self._send(NORMAL_BODY.encode())
        else:
            self._send(b'{"error":"no route for GET %s"}' % self.path.encode(), 404)


@pytest.fixture()
def fake_daemon(tmp_path):
    srv = _FakeDaemon()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    rt_dir = tmp_path / "rt"
    rt_dir.mkdir()
    (rt_dir / "daemon.port").write_text(str(srv.server_address[1]))
    try:
        yield srv, rt_dir
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _run(rt_dir, timeout_env=None, extra_env=None):
    env = os.environ.copy()
    env.update({
        "RT_DIR": str(rt_dir),
        "RT_NO_AUTOSPAWN": "1",
        "STORAGE_BACKEND": "local",  # guard-955: never let a test touch own-cloud
    })
    # The wrapper distinguishes caller-set from unset (it captures the env
    # BEFORE sourcing _runtime.sh), so deleting the var exercises the default.
    env.pop("RT_CURL_TIMEOUT", None)
    if timeout_env is not None:
        env["RT_CURL_TIMEOUT"] = str(timeout_env)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, WRAPPER.as_posix(), "--category", "test-probe", "--read-only"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def test_empty_body_200_exits_7_with_diagnostic(fake_daemon):
    srv, rt_dir = fake_daemon
    srv.mode = "empty"
    r = _run(rt_dir, timeout_env=10)
    assert r.returncode == 7, (r.returncode, r.stderr[-500:])
    assert r.stdout == "", "an empty-body failure must not emit stdout"
    assert "EMPTY body" in r.stderr
    assert "GET /v1/retrieve" in r.stderr
    assert "g-115-6189" in r.stderr
    assert re.search(r"after \d+ms", r.stderr), r.stderr[-300:]


def test_timeout_exits_nonzero_naming_endpoint_and_elapsed(fake_daemon):
    srv, rt_dir = fake_daemon
    srv.mode = "slow"
    r = _run(rt_dir, timeout_env=2)
    assert r.returncode == 1, (r.returncode, r.stderr[-500:])
    assert r.stdout == ""
    assert "GET /v1/retrieve did not complete" in r.stderr
    assert re.search(r"after \d+ms", r.stderr), r.stderr[-300:]
    # Threading fake keeps /health answering while /v1/retrieve hangs, so the
    # wrapper must land in rt_no_daemon_error's REACHABLE branch — the shape a
    # live slow daemon produces (measured foxtrot 2026-08-14: rc=1 + this text).
    assert "REACHABLE but the request did not complete" in r.stderr
    assert "RT_CURL_TIMEOUT=2" in r.stderr


def test_normal_body_passes_through_rc0(fake_daemon):
    srv, rt_dir = fake_daemon
    srv.mode = "normal"
    r = _run(rt_dir, timeout_env=10)
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert r.stdout == NORMAL_BODY, "body must pass through byte-identical"


def test_default_bound_is_240_when_caller_sets_nothing(fake_daemon):
    """Outcome 4's mechanism, observed cheaply: the empty-body FATAL line
    prints the effective bound, so the default is readable in ~20ms without
    ever waiting on it."""
    srv, rt_dir = fake_daemon
    srv.mode = "empty"
    r = _run(rt_dir, timeout_env=None)
    assert r.returncode == 7
    assert "RT_CURL_TIMEOUT=240s" in r.stderr, r.stderr[-300:]


def test_caller_timeout_override_is_honored(fake_daemon):
    srv, rt_dir = fake_daemon
    srv.mode = "empty"
    r = _run(rt_dir, timeout_env=33)
    assert r.returncode == 7
    assert "RT_CURL_TIMEOUT=33s" in r.stderr, r.stderr[-300:]


def test_fast_failure_is_not_reported_as_a_timeout(fake_daemon):
    """: a fast rc=3 against a REACHABLE daemon must report MEASURED
    elapsed and refuse to name a cause — not assert an RT_CURL_TIMEOUT expiry
    it never observed.

    The defect: rt_no_daemon_error held no measurement at all. RT_CURL_TIMEOUT
    is an env read, so "did not complete within RT_CURL_TIMEOUT=90s" was config
    echoed back as observation. Measured before the fix (foxtrot, 2026-08-26):
    a 97ms failure reported as a 90s timeout.

    Bound is 60s here while the failure lands in single-digit ms, so the two
    are three orders of magnitude apart and no timing flake can blur them.
    """
    srv, rt_dir = fake_daemon
    srv.mode = "fastfail"
    r = _run(rt_dir, timeout_env=60)
    assert r.returncode == 1, (r.returncode, r.stderr[-800:])

    # The fabricated claim must be GONE. This substring is unique to
    # rt_no_daemon_error — retrieve.sh's own line reads "did not complete:
    # transport failure after Nms", which is measured and stays.
    assert "did not complete within RT_CURL_TIMEOUT" not in r.stderr, \
        "SIG-FABRICATED-TIMEOUT-CLAIM: " + r.stderr[-600:]

    m = re.search(r"request FAILED after (\d+)ms against a REACHABLE daemon", r.stderr)
    assert m, "SIG-NO-MEASURED-ELAPSED: " + r.stderr[-600:]

    # Requirement 3: verify the MEASUREMENT is right, not merely that the
    # sentence changed. A wrong measured number is worse than an obviously
    # configured one, because it is credible.
    elapsed = int(m.group(1))
    assert 0 <= elapsed < 6000, f"elapsed {elapsed}ms must be far below the 60000ms bound"

    assert "refusing to name a cause" in r.stderr
    assert "was NOT reached" in r.stderr
    assert "Do NOT raise RT_CURL_TIMEOUT" in r.stderr
    # The slowness causes must NOT be offered for a failure that excluded them.
    assert "one-time daemon warmup" not in r.stderr, \
        "SIG-SLOWNESS-CAUSES-OFFERED: " + r.stderr[-600:]
