"""pytest fixtures: spawn a daemon in a background thread per test.

Every test gets a fresh daemon listening on a free port. PID + port files
go to a per-test runtime dir under tmp_path so we don't collide with the
real `mind_api/state/` at the repo root.

The daemon runs in a thread (same process) rather than as a subprocess —
that's faster for tests AND lets us instrument internal state directly when
needed. The httpd lifecycle is started/stopped explicitly per test.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Generator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# rb-1472: the 21 test_wrapper_*/test_runtime_* helpers resolve bash via
# `shutil.which("bash")`, which on a clean Windows PATH (pytest launched from
# cmd.exe/PowerShell) picks the System32 WSL stub or the raw Git usr/bin/bash.exe
# — the latter does NOT self-configure its PATH, so coreutils go missing and the
# nested SCRIPT_DIR-based bash calls inside the wrappers fail rc=127. Prepend the
# resolved Git bin/bash.exe (login-launcher) dir so every shutil.which("bash")
# and inherited-PATH subprocess in this suite picks the robust launcher. Single
# source of truth: core/scripts/tests/_bash_helpers.resolve_bash(). Fail-open.
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts" / "tests"))
try:
    from _bash_helpers import resolve_bash as _resolve_bash
    _bash_dir = os.path.dirname(_resolve_bash())
    if _bash_dir and _bash_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _bash_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass  # fail-open: tests fall back to the existing shutil.which behavior

# Hermetic storage backend (lodestar-s7 test isolation): tests must NEVER touch
# real S3. After the own-cloud cutover, .env.local carries
# STORAGE_BACKEND=own-cloud; the in-process daemon fixtures here resolve
# get_backend() from os.environ, and any wrapper subprocess they spawn inherits
# this env. Pin local for the whole pytest session so test daemons stay on the
# LocalBackend rather than reaching for real S3 (or 500-ing on from_env).
os.environ["STORAGE_BACKEND"] = "local"


# Suppress the daemon stale-code check for the whole pytest session
# (). The in-process test daemons spawned below report a
# git_head_sha frozen at module-import time; when HEAD advances mid-run (a
# real commit landing during a long suite, the canonical 74-failure incident
# 2026-06-03), rt_check_staleness sees frozen != on-disk and fires a
# disruptive auto-restart mid-wrapper, corrupting the wrapper's output
# (JSONDecodeError). The check is meaningless for an in-process daemon that
# IS the current code, so pre-set the "already warned" sentinel that
# rt_check_staleness short-circuits on (_runtime.sh:169); every wrapper
# subprocess inherits it via os.environ.copy(), giving full isolation from
# both the frozen test-daemon sha AND any stale live daemon, with no
# quiescent window required.
os.environ["RT_STALENESS_WARNED"] = "1"


# Hermetic WORLD/META resolution (). MUST stay ABOVE the
# _BOOTSTRAP_ENV snapshot below — the snapshot captures whatever is live at
# import time, so clearing after it would be a no-op.
#
# MEASURED 2026-08-01 (zeta, cc-02 / Linux 6.8.0-136-generic). This is the
# mechanism the  scope note below records as OPEN, and it is now
# closed. mind_api/src/agent_paths.py::_resolve_src resolves in the order
# 1. MIND_WORLD env -> 2/3. .mind-data/ (when the dir exists) -> 4.
# local-paths.conf. MIND_WORLD is PRECEDENCE 1, so it beats the tmp
# project_root outright. Probed directly against the resolver with a
# conftest-shaped tmp root, varying ONLY this variable:
#     MIND_WORLD unset       -> world=/tmp/.../world          (hermetic)
#     MIND_WORLD=<production> -> world=<repo>/.mind-data/world (LEAK)
# And core/scripts/_paths.sh:262 does
#     export WORLD_DIR WORLD_PATH="$WORLD_DIR" MIND_WORLD="$WORLD_DIR"
# so ANY pytest launched from a shell that sourced _paths.sh inherits a
# production MIND_WORLD. That single fact explains both leaking files at
# once: the in-process running_daemon fixture leaks DESPITE being handed the
# tmp project_root (precedence 1 fires before project_root is consulted), and
# test_wrapper_rbguard.py::_run_wrapper propagates the same var to its
# subprocess via os.environ.copy(). One mechanism, not two.
#
# It also explains why the earlier H-WORLD probe was correctly falsified — it
# ran in a shell with MIND_WORLD unset, so it fell through to the tmp conf and
# the resolver looked innocent. The resolver IS correct; it was being handed a
# production override.
#
# Why POP rather than pin: the per-test tmp world is created by the
# `project_root` fixture and is not knowable at import time. With these unset,
# _resolve_src falls to .mind-data/ (absent under a tmp root) and then to the
# fixture's own local-paths.conf — the hermetic path, as measured above.
for _leaky in ("MIND_WORLD", "MIND_META"):
    os.environ.pop(_leaky, None)


# --- Per-test env restore () ------------------------------------
# The two pins above are MODULE-LEVEL, i.e. applied exactly once at collection
# time. Until this fixture existed, `mind_api/tests` had no autouse fixture at
# ALL (a full-file grep for "autouse" returned nothing), so any test in this
# directory that mutated STORAGE_BACKEND / MIND_WORLD / MIND_META / MIND_AGENT
# without restoring left it mutated for EVERY test collected after it — pytest
# imports all modules into one process, so the leak also reaches tests that sort
# alphabetically earlier. `core/scripts/tests/conftest.py` has carried the
# equivalent fixture since ; this directory was the asymmetric half.
#
# Scope note, stated so nobody reads more into this than was measured: this pins
# the ENV. It does NOT by itself establish the cause of the 2026-07-31 leak of 52
# fixture records into 5 governed world stores. That investigation measured the
# WRITE PATH conclusively (this directory's test_runtime_store_rbguard.py +
# test_wrapper_rbguard.py, three pytest runs, proven by the literal `source`
# values wave-2-test / wave-2-wrapper-test plus production-sequence id allocation
# in world/changelog.jsonl) but did NOT establish the mechanism by which those
# runs' daemon reached the production world — the most natural candidate, "the
# tmp project_root resolves to the production world", was PROBED AND FALSIFIED
# (AgentPathResolver on a conftest-shaped tmp root returns the tmp world). The
# remaining mechanism is open and tracked separately. Do not treat this fixture
# as that fix.
#
# Deliberately NOT pinned here: MIND_ALLOW_TMP_OWNCLOUD_PUT. Its sibling
# conftest sets it to "1" to DISARM the own-cloud tempdir tripwire for hermetic
# pytest sessions; this directory leaves it unset, so the tripwire stays ARMED.
# Adding it would reduce safety in the exact tree where a production leak was
# observed, which is the wrong direction and outside this goal.
_UNSET = object()
_BOOTSTRAP_ENV = {
    key: os.environ.get(key, _UNSET)
    for key in ("STORAGE_BACKEND", "MIND_WORLD", "MIND_META",
                "MIND_AGENT", "RT_STALENESS_WARNED")
}


@pytest.fixture(autouse=True)
def _restore_env_per_test():
    """Re-pin the session env before every test in this directory.

    Runs BEFORE the test body, so a test that legitimately needs a different
    env still overrides it inside the body (and monkeypatch still unwinds its
    own changes afterwards) — this fixture only undoes collection-time and
    prior-test pollution.
    """
    for key, value in _BOOTSTRAP_ENV.items():
        if value is _UNSET:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    yield


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A throwaway PROJECT_ROOT with a minimal layout the daemon needs."""
    pr = tmp_path / "repo"
    pr.mkdir()
    # Minimal agent dir with a local-paths.conf pointing at a tmp world.
    # Phase 2.5.D layout: agent dirs live under agents/ parent. Must match
    # AGENTS_PARENT_DIR in core/scripts/_paths.py / mind_api/src/agent_paths.py.
    (pr / "agents").mkdir()
    agent = pr / "agents" / "alpha"
    agent.mkdir()
    world = pr / "world"
    world.mkdir()
    # Phase 2.5.D + Finding A (no PROJECT_ROOT/world fallback): every agent the
    # test suite references via the agent header MUST have its own
    # local-paths.conf pointing at this same tmp world, otherwise the daemon's
    # agent_paths resolver raises (Plan v1 step 0.1 hard-cut). Cross-agent test
    # references: bravo, charlie, delta, zeta. Cheap to pre-seed all.
    for sibling in ("bravo", "charlie", "delta", "zeta", "echo"):
        (pr / "agents" / sibling).mkdir()
        # local-paths.conf written AFTER world/meta dirs exist, below.
    (world / "aspirations.jsonl").write_text(
        '{"id":"asp-001","title":"Test","status":"active","priority":"LOW",'
        '"archived":false,"goals":[],"progress":{"completed_goals":0,"total_goals":0}}\n',
        encoding="utf-8",
    )
    meta = pr / "meta"
    meta.mkdir()
    conf_body = f"WORLD_PATH={world.as_posix()}\nMETA_PATH={meta.as_posix()}\n"
    (agent / "local-paths.conf").write_text(conf_body, encoding="utf-8")
    # Mirror to pre-seeded siblings (bravo/charlie/delta/zeta/echo).
    for sibling in ("bravo", "charlie", "delta", "zeta", "echo"):
        (pr / "agents" / sibling / "local-paths.conf").write_text(
            conf_body, encoding="utf-8"
        )
    # Agent-local aspirations (so source=agent works)
    (agent / "aspirations.jsonl").write_text(
        '{"id":"asp-100","title":"AgentLocal","status":"active","priority":"LOW",'
        '"archived":false,"goals":[],"progress":{"completed_goals":0,"total_goals":0}}\n',
        encoding="utf-8",
    )

    # Minimal core/config/tree.yaml. The real daemon's project_root ALWAYS
    # carries this (it's part of the framework); the tree-write reparent op's
    # D_max gate reads it via _merged_config (which, mirroring the CLI's
    # no-silent-fallback contract rb-215/rb-275, opens it unconditionally —
    # unlike _load_competence_config, which tolerates a missing file). Seed it
    # with the canonical D_max + competence_mapping so the temp repo matches
    # production shape.
    core_config = pr / "core" / "config"
    core_config.mkdir(parents=True)
    (core_config / "tree.yaml").write_text(
        "config:\n"
        "  D_max: 20\n"
        "domain_health:\n"
        "  competence_mapping:\n"
        "    EXPLORE: 0.25\n"
        "    CALIBRATE: 0.50\n"
        "    EXPLOIT: 0.75\n"
        "    MASTER: 1.00\n",
        encoding="utf-8",
    )

    # Minimal _tree.yaml for tree-find-node tests. parse_front_matter handles
    # missing .md files gracefully (returns {}), so we don't need to write
    # any node .md files — the substring/key match channels still fire.
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(
        "nodes:\n"
        "  alpha-test-node:\n"
        "    summary: 'A test node about alpha matching'\n"
        "    file: world/knowledge/tree/alpha-test-node.md\n"
        "    depth: 1\n"
        "    children: []\n"
        "  beta-other-node:\n"
        "    summary: 'A different node about beta'\n"
        "    file: world/knowledge/tree/beta-other-node.md\n"
        "    depth: 1\n"
        "    children: [alpha-test-node]\n"
        "entity_index: {}\n",
        encoding="utf-8",
    )

    # Minimal working-memory.yaml for wm-read tests.
    session = agent / "session"
    session.mkdir()
    (session / "working-memory.yaml").write_text(
        "slots:\n"
        "  active_context:\n"
        "    summary: 'currently testing the runtime'\n"
        "    experience_refs: []\n"
        "    retrieval_manifest: null\n"
        "  active_strategy: 'depth-first'\n"
        "encoding_queue:\n"
        "  - 'item-1'\n"
        "  - 'item-2'\n"
        "session_id: 'test-sid-001'\n"
        "session_start: '2026-05-12T00:00:00'\n",
        encoding="utf-8",
    )

    # --- PR 4 seeds ---------------------------------------------------------
    # Pipeline: one active + one resolved record, plus a meta file with counts.
    (world / "pipeline.jsonl").write_text(
        '{"id":"2026-05-12_test-active","stage":"active","title":"Test active",'
        '"reflected":false}\n'
        '{"id":"2026-05-12_test-resolved","stage":"resolved","title":"Test resolved",'
        '"outcome":"correct","reflected":true,"replay_metadata":{"next_review_date":"2025-01-01"}}\n',
        encoding="utf-8",
    )
    (world / "pipeline-archive.jsonl").write_text("", encoding="utf-8")
    (world / "pipeline-meta.json").write_text(
        '{"stage_counts":{"discovered":0,"active":1,"resolved":1,"archived":0},'
        '"accuracy":{"correct":1,"total":1}}',
        encoding="utf-8",
    )

    # Reasoning-bank: one active, one universal (applies_to=any), one retired.
    (world / "reasoning-bank.jsonl").write_text(
        '{"id":"rb-001","type":"insight","category":"alpha-cat","title":"T1",'
        '"status":"active","applies_to":"framework","tags":["test","alpha"],'
        '"created":"2026-05-10T10:00:00",'
        '"utilization":{"times_helpful":0,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":0,"utilization_score":0.0}}\n'
        '{"id":"rb-002","type":"lesson","category":"framework-loop","title":"Universal",'
        '"status":"active","applies_to":"any","tags":["uni"],'
        '"created":"2026-05-11T10:00:00",'
        '"utilization":{"times_helpful":3,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":5,"utilization_score":0.5}}\n'
        '{"id":"rb-003","type":"insight","category":"alpha-cat","title":"Retired",'
        '"status":"retired","applies_to":"framework","tags":[],'
        '"created":"2026-05-09T10:00:00",'
        '"utilization":{"times_helpful":0,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":0,"utilization_score":0.0}}\n',
        encoding="utf-8",
    )

    # Guardrails: two active, one retired.
    (world / "guardrails.jsonl").write_text(
        '{"id":"guard-001","category":"infra","rule":"check infra first",'
        '"status":"active","when_to_use":"always",'
        '"utilization":{"times_helpful":0,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":0,"utilization_score":0.0}}\n'
        '{"id":"guard-002","category":"safety","rule":"verify before assuming",'
        '"status":"active","when_to_use":"on negation",'
        '"utilization":{"times_helpful":0,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":0,"utilization_score":0.0}}\n'
        '{"id":"guard-099","category":"infra","rule":"old rule",'
        '"status":"retired","when_to_use":"never",'
        '"utilization":{"times_helpful":0,"times_inferred_helpful":0,"times_cited":0,'
        '"retrieval_count":0,"utilization_score":0.0}}\n',
        encoding="utf-8",
    )

    # Pattern signatures
    (world / "pattern-signatures.jsonl").write_text(
        '{"id":"sig-001","name":"alpha pattern","validation_status":"validated",'
        '"status":"active","outcome_stats":{"accuracy":0.8,"confirmed":4,"total":5}}\n'
        '{"id":"sig-002","name":"beta pattern","validation_status":"pending",'
        '"status":"retired","outcome_stats":{"accuracy":0,"confirmed":0,"total":0}}\n',
        encoding="utf-8",
    )

    # Spark questions (lives under META_DIR)
    (meta / "spark-questions.jsonl").write_text(
        '{"id":"sq-001","type":"question","status":"active","text":"What works?",'
        '"yield_rate":0.5,"times_asked":4,"sparks_generated":2}\n'
        '{"id":"sq-c01","type":"candidate","text":"candidate question text"}\n',
        encoding="utf-8",
    )

    # Experience (agent-local)
    (agent / "experience.jsonl").write_text(
        '{"id":"exp-test-1","type":"insight","category":"alpha-cat","summary":"t1",'
        '"goal_id":"g-001-01","hypothesis_id":"hyp-1","created":"2026-05-10T08:00:00",'
        '"retrieval_stats":{"retrieval_count":10}}\n'
        '{"id":"exp-test-2","type":"lesson","category":"beta-cat","summary":"t2",'
        '"goal_id":"g-001-02","created":"2026-05-12T08:00:00",'
        '"retrieval_stats":{"retrieval_count":2}}\n',
        encoding="utf-8",
    )
    (agent / "experience-archive.jsonl").write_text("", encoding="utf-8")
    (agent / "experience-meta.json").write_text(
        '{"total_records":2,"by_category":{"alpha-cat":1,"beta-cat":1}}',
        encoding="utf-8",
    )

    # Journal (agent-local)
    (agent / "journal.jsonl").write_text(
        '{"session":1,"date":"2026-05-10","goals_completed":["g-001-01"],'
        '"key_events":["start"],"tags":["initial"]}\n'
        '{"session":2,"date":"2026-05-11","goals_completed":["g-001-02","g-001-03"],'
        '"key_events":[],"tags":["routine"]}\n',
        encoding="utf-8",
    )

    # Board: one channel with two messages.
    board = world / "board"
    board.mkdir()
    (board / "general.jsonl").write_text(
        '{"id":"msg-1","author":"alpha","timestamp":"2026-05-12T10:00:00",'
        '"channel":"general","type":"status","text":"hello","reply_to":null,'
        '"tags":["greeting"]}\n'
        '{"id":"msg-2","author":"bravo","timestamp":"2026-05-12T10:01:00",'
        '"channel":"general","type":"claim","text":"working on g-001-99",'
        '"reply_to":null,"tags":["g-001-99"]}\n',
        encoding="utf-8",
    )

    # Team-state
    (world / "team-state.yaml").write_text(
        "last_updated: '2026-05-12T12:00:00'\n"
        "last_updated_by: alpha\n"
        "strategic_focus:\n"
        "  primary: 'building runtime'\n"
        "  rationale: 'speedup'\n"
        "  set_by: alpha\n"
        "  set_at: '2026-05-12T11:00:00'\n"
        "  acknowledged_by: ['bravo']\n"
        "active_blockers: []\n"
        "recent_completions: []\n"
        "agent_status:\n"
        "  alpha:\n"
        "    last_active: '2026-05-12T12:00:00'\n"
        "  bravo:\n"
        "    last_active: '2026-05-12T11:55:00'\n"
        "critical_blockers: []\n",
        encoding="utf-8",
    )
    return pr


@pytest.fixture
def running_daemon(project_root: Path) -> Generator[tuple, None, None]:
    """Start a daemon in a background thread; yield (project_root, port)."""
    from mind_api.src.server import Server

    server = Server(project_root=project_root, port=0)

    # Mirror Server.start() but in a thread we can join cleanly.
    from mind_api.src import lifecycle

    # Build the bound handler ourselves so we can capture the http server
    # before serve_forever blocks the thread.
    from http.server import ThreadingHTTPServer
    from mind_api.src.server import _Handler

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
    handler_cls.pid = 12345

    lifecycle.write_pid_and_port_atomic(project_root, 12345, port)

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # Wait briefly for the listener to come up.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)

    try:
        yield project_root, port
    finally:
        httpd.shutdown()
        httpd.server_close()
        lifecycle.clear_runtime_files(project_root)
