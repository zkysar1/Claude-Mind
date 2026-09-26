"""The daemon health probes carry the FR-4 bearer ().

Once MIND_API_TOKEN is set on the daemon, FR-4 (mind_api/src/server.py) answers
EVERY request without a matching bearer with 401 -- /v1/admin/health included.
The health probes sent no bearer, so on a token-set box a LIVE daemon read as
down: agent-watchdog --tick respawned it on every iteration close (cc-03: 2,196
daemon_unreachable rows in a row, 2026-09-04 .. 2026-09-25).

Both probe paths are pinned here from ONE table, against a real local HTTP server
that behaves like FR-4:
  - python: agent-watchdog.py daemon_health_probe + daemon_health_json
  - shell:  _runtime.sh rt_is_up + rt_curl_silent_health, run by SOURCING the
    real file (guard-920: the literal production shape, never a re-implementation)

Each path is checked on a token-set box (the probe must send the bearer and read
the daemon as up), with the pre-fix bare request as the positive control (401,
read as down), and on a token-less box (no Authorization header at all, so the
request is exactly what it was before the fix).
"""
from __future__ import annotations

import http.server
import importlib.util
import os
import pathlib
import subprocess
import sys
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
CORE_SCRIPTS = REPO / "core" / "scripts"
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _runtime_bash import BASH  # noqa: E402  guard-580: never a bare "bash" argv[0]


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_bearer", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


class _Fr4Daemon:
    """A local /v1/admin/health that behaves like FR-4: 401 unless the request
    carries `Bearer <required>`; with required=None it answers every request."""

    def __init__(self, required):
        self.required = required
        self.seen = []  # the Authorization header of every request (None = absent)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib name)
                auth = self.headers.get("Authorization")
                outer.seen.append(auth)
                ok = outer.required is None or auth == "Bearer " + outer.required
                body = b'{"ok": true}' if ok else b'{"error": "unauthorized"}'
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# (description, token the daemon requires, token this box resolves, expect up, expected header)
CASES = [
    ("token-set-box-sends-bearer", "tok-A", "tok-A", True, "Bearer tok-A"),
    ("positive-control-bare-request-is-401", "tok-A", "", False, None),
    ("token-less-box-sends-no-header", None, "", True, None),
]
_IDS = [c[0] for c in CASES]


def _port_file(tmp_path, port):
    state = tmp_path / "mind_api" / "state"
    state.mkdir(parents=True, exist_ok=True)
    pf = state / "daemon.port"
    pf.write_text(str(port), encoding="utf-8")
    return pf


@pytest.mark.parametrize("desc,required,token,up,header", CASES, ids=_IDS)
def test_python_probe(tmp_path, monkeypatch, desc, required, token, up, header):
    monkeypatch.delenv("RT_PORT_FILE", raising=False)
    monkeypatch.delenv("RT_DIR", raising=False)
    monkeypatch.setattr(WD, "_rt_api_token", lambda: token)
    with _Fr4Daemon(required) as d:
        _port_file(tmp_path, d.port)
        assert WD.daemon_health_probe(tmp_path, timeout=2.0) is up, desc
        body = WD.daemon_health_json(tmp_path, timeout=2.0)
    assert (body == {"ok": True}) is up, (desc, body)
    assert d.seen == [header, header], (desc, d.seen)


@pytest.mark.parametrize("desc,required,token,up,header", CASES, ids=_IDS)
def test_shell_probe(tmp_path, desc, required, token, up, header):
    if token:
        (tmp_path / ".env.local").write_text("MIND_API_TOKEN=%s\n" % token, encoding="utf-8")
    # PROJECT_ROOT is set AFTER the source: _runtime.sh resolves it itself when
    # sourced, and _rt_api_token reads it at CALL time (same seam as
    # test_rt_token_env_local_fallback.py).
    script = (
        'source "%s/core/scripts/_runtime.sh" >/dev/null 2>&1 || true\n'
        'PROJECT_ROOT="%s"\n'
        "rt_is_up; echo \"is_up=$?\"\n"
        "rt_curl_silent_health; echo \"silent=$?\"\n" % (REPO, tmp_path)
    )
    with _Fr4Daemon(required) as d:
        env = dict(os.environ)
        env.pop("MIND_API_TOKEN", None)
        env["RT_PORT_FILE"] = str(_port_file(tmp_path, d.port))
        out = subprocess.run(
            [BASH, "-c", script], capture_output=True, text=True, env=env,
            cwd=str(REPO), timeout=60,
        )
    # Down is any NONZERO rc: rt_is_up returns curl's own code (22 on a 401),
    # rt_curl_silent_health returns grep's 1.
    rcs = dict(line.split("=", 1) for line in out.stdout.split())
    assert set(rcs) == {"is_up", "silent"}, (desc, out.stdout, out.stderr[-400:])
    assert (rcs["is_up"] == "0") is up, (desc, rcs)
    assert (rcs["silent"] == "0") is up, (desc, rcs)
    assert d.seen == [header, header], (desc, d.seen)
