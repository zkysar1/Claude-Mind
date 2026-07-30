"""Shared in-process daemon fixture for daemon-aware script tests.

Background (g-115-887, Cat B+C of g-115-874):
Six tests across five files invoke scripts that call _rt internally,
where _rt's daemon-port resolution reads PROJECT_ROOT/mind_api/state/daemon.port
(the REAL daemon's port) — bypassing the test's temp world. The result
is that Investigate goals filed by the script land in the real
world/aspirations.jsonl, not the test's temp world, and the test
assertion fails (the seeded test aspirations never see the new goal).

The canonical fix pattern was developed in test_window_streak.py
(g-115-756, 2026-05-14 daemon-only cutover). This module extracts the
helpers so the remaining 4-5 tests can adopt the pattern without
duplicating ~80 lines each.

THIS MODULE IS NOW THE CANONICAL COPY -- do not re-fork it, and do not
treat the origin file as the reference. The extraction left the original
behind as a private copy, and it then drifted: this module gained pins for
MIND_WORLD (g-115-2352), STORAGE_BACKEND (g-115-2101) and MIND_META
(guard-652) that the origin never got. The origin sat RED wherever
MIND_WORLD was exported and green everywhere else, which read as a
platform bug for a day (g-115-3947, 2026-07-30). test_window_streak.py now
imports from here; 38 test files use this module and zero fork it.

Usage:
    from _daemon_fixture import DaemonFixture, make_project_root

    def test_thing():
        with tempfile.TemporaryDirectory() as tmpd:
            world = setup_world(Path(tmpd))
            with DaemonFixture(world) as df:
                # df.runtime_dir, df.project_root, df.port available
                # _rt calls now hit the test's daemon
                # subprocess.run invocations need env["RT_DIR"]=str(df.runtime_dir)
                ...

Cross-references:
  - test_window_streak.py — where the pattern originated; its local copy was
    deleted 2026-07-30 (g-115-3947) and it now imports from here
  - mind_api/src/server.py — Server class spawned in-process
  - core/scripts/_rt.py:45-49 — RT_DIR env resolution
  - g-115-874 (zeta investigation) — Cat B + Cat C failure analysis
  - g-115-887 (this Apply) — alpha's migration
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# guard-652 seed-then-pin (): capture the REAL meta dir at import
# time (collection, real env) so DaemonFixture can seed a temp meta with the
# strategy config files BEFORE pinning MIND_META to it in __enter__. Captured
# at module level — NOT inside __enter__ — because __enter__ mutates the env
# (MIND_WORLD/MIND_AGENT), which would drift a re-resolution off the fixture
# world; at collection time the real env still resolves the true META_DIR.
# Best-effort: a _paths import failure leaves _REAL_META_DIR None and the seed
# step no-ops (fixture still works, just without pre-seeded strategy files).
try:
    from _paths import META_DIR as _REAL_META_DIR
except Exception:
    _REAL_META_DIR = None


def make_project_root(tmp: Path, world: Path, agent: str = "alpha",
                      agent_dir: Path | None = None) -> Path:
    """Build a minimal project root with local-paths.conf pointing at world.

    If ``agent_dir`` is provided, also seeds the project's agent directory
    structure so test scripts using MIND_AGENT_DIR env continue to work.
    """
    pr = tmp / "repo"
    pr.mkdir(exist_ok=True)
    # Phase 2.5.D layout: agent dirs live under agents/ parent. Must match
    # AGENTS_PARENT_DIR in core/scripts/_paths.py / mind_api/src/agent_paths.py.
    agents_parent = pr / "agents"
    agents_parent.mkdir(exist_ok=True)
    agent_pr = agents_parent / agent
    agent_pr.mkdir(exist_ok=True)
    (agent_pr / "session").mkdir(exist_ok=True)
    meta = pr / "meta"
    meta.mkdir(exist_ok=True)
    (agent_pr / "local-paths.conf").write_text(
        f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n",
        encoding="utf-8",
    )
    # If caller passed a separate agent_dir (e.g. for MIND_AGENT_DIR
    # env-override scripts), expose its session dir alongside as well so the
    # canonical agent_dir/session/<file>.jsonl probes work.
    if agent_dir is not None:
        (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    return pr


def _start_daemon(project_root: Path) -> tuple:
    """Start an in-process daemon; return (httpd, port).

    Originally mirrored test_window_streak.py:_start_daemon; that copy was
    deleted 2026-07-30 (g-115-3947), so this is now the only implementation.
    """
    from mind_api.src.server import Server, _Handler
    from mind_api.src import lifecycle

    server = Server(project_root=project_root, port=0)
    handler_cls = type(
        "_BoundHandler", (_Handler,), {
            "routes": server.routes,
            "resolver": server.resolver,
            "access_log_path": lifecycle.access_log(project_root),
            "pid": 0,
            "port": 0,
        },
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    handler_cls.port = port
    handler_cls.pid = os.getpid()

    rt_dir = project_root / "mind_api" / "state"
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / "daemon.port").write_text(str(port), encoding="utf-8")

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)
    return httpd, port


class DaemonFixture:
    """Context manager: spins up an in-process daemon for a temp world.

    Exposes ``project_root``, ``runtime_dir``, and ``port`` for callers that
    need to point subprocesses at the test's daemon via env vars
    (typically RT_DIR=str(fixture.runtime_dir) for _rt.py to find it).

    Restores prior env on __exit__ so cross-test isolation holds (mirrors
    test_applies_to_required.py's g-115-888 capture-restore pattern).
    """

    def __init__(self, world: Path, agent: str = "alpha",
                 agent_dir: Path | None = None):
        self.world = world
        self.agent = agent
        self.agent_dir = agent_dir
        self._httpd = None
        self._port = None
        self._pr = None
        self._prev_env = {}

    @property
    def project_root(self) -> Path:
        return self._pr

    @property
    def runtime_dir(self) -> Path:
        return self._pr / "mind_api" / "state"

    @property
    def port(self) -> int:
        return self._port

    def __enter__(self):
        tmp = self.world.parent
        self._pr = make_project_root(tmp, self.world, self.agent,
                                     agent_dir=self.agent_dir)

        self._prev_env = {
            "RT_DIR": os.environ.get("RT_DIR"),
            "RT_PORT_FILE": os.environ.get("RT_PORT_FILE"),
            "MIND_AGENT": os.environ.get("MIND_AGENT"),
            "STORAGE_BACKEND": os.environ.get("STORAGE_BACKEND"),
            "MIND_WORLD": os.environ.get("MIND_WORLD"),
            "MIND_META": os.environ.get("MIND_META"),
        }
        os.environ["RT_DIR"] = str(self.runtime_dir)
        os.environ["MIND_AGENT"] = self.agent
        # : hard-pin STORAGE_BACKEND=local (mirror conftest.py:78) so
        # every in-process DaemonFixture daemon binds to LocalBackend regardless
        # of invocation path. main()-style `python3 test_x.py` and bash
        # aggregators never load conftest's session pin, so an ambient own-cloud
        # env would otherwise leak world-isolated writes onto the production S3
        # key (guard-955/rb-2983). NOT setdefault — the shell may carry own-cloud.
        os.environ["STORAGE_BACKEND"] = "local"
        # : hard-pin MIND_WORLD to the FIXTURE world, BEFORE
        # _start_daemon. get_backend()'s _bootstrap_env_defaults (
        # bare-subprocess self-heal) exports the REAL repo's world as
        # MIND_WORLD when unset — but its pytest guard makes it a no-op under
        # pytest, so ONLY main()-style runs got poisoned: AgentPathResolver's
        # env tier then beat the fixture's local-paths.conf and every daemon
        # request resolved the PRODUCTION world (uniform 404s on fixture
        # goals). Pinning before daemon start means the bootstrap's setdefault
        # no-ops and any init-time resolution caches the fixture world.
        os.environ["MIND_WORLD"] = str(self.world)
        # guard-652 seed-then-pin (): previously MIND_META was left
        # UNPINNED so fixture subprocesses (os.environ.copy()) could resolve
        # strategy files from the real meta (test_cross_aspiration_support's
        # selector) — but that let meta-writers which resolve META_DIR
        # independently of WORLD_DIR (e.g. tree.py _append_l1_pick_log writing
        # META_DIR/l1-pick-log.jsonl) leak writes into the REAL meta. Pinning to
        # an EMPTY pr/meta stopped the leak but broke the strategy-file
        # subprocesses. Seed-then-pin resolves both: copy the real meta's
        # top-level strategy configs (*.yaml/*.md — NOT the large *.jsonl logs)
        # into the fixture meta, THEN pin MIND_META to it. Subprocesses find the
        # strategy files; meta-writers write to the isolated temp dir. _prev_env
        # captured MIND_META above so the pin is restored on exit.
        meta_dir = self._pr / "meta"
        if _REAL_META_DIR is not None:
            try:
                real_meta = Path(_REAL_META_DIR)
                for pat in ("*.yaml", "*.md"):
                    for src in real_meta.glob(pat):
                        if src.is_file():
                            (meta_dir / src.name).write_bytes(src.read_bytes())
            except Exception:
                pass  # best-effort seed; the guard-652 pin below still applies
        os.environ["MIND_META"] = str(meta_dir)

        self._httpd, self._port = _start_daemon(self._pr)
        return self

    def __exit__(self, *exc):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
