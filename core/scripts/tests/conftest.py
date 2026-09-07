"""Pytest configuration for core/scripts/tests/.

Many production modules (wm.py, journal.py, curriculum.py, etc.) compute
module-level paths from AGENT_DIR at import time. AGENT_DIR resolves from
MIND_AGENT in _paths.py. Without an agent bound, these imports fail with
TypeError at `CONST = AGENT_DIR / "..."`.

Production code stays strict: it MUST fail loud at import when AGENT_DIR is
None, because any non-test caller running without an agent has misconfigured
its environment, and a clear TypeError at module load beats an obscure
AttributeError deep in some function call later.

The wrinkle: ~12 test files in this directory call
`os.environ.pop("MIND_AGENT", None)` at MODULE level to bootstrap a clean
WORLD env. Pytest collects test modules alphabetically; one early-alphabetical
test (test_audit_baselines_race.py) imports `_paths` which caches AGENT_DIR
at first-import time. Any pop that fires BEFORE that first import causes the
cache to lock in AGENT_DIR=None for the entire pytest session.

Fix: in this conftest (which pytest loads BEFORE any test module), (1) set
MIND_AGENT to the first available agent, (2) import `_paths` once to lock
in AGENT_DIR. Subsequent test-module pops affect os.environ but cannot
unset the now-cached `_paths.AGENT_DIR`. Tests that override the WM path
must set the BODY_WM_PATH env var (e.g.
`monkeypatch.setenv("BODY_WM_PATH", str(tmp))`) — NOT patch `wm.WM_PATH`:
after g-306-61 WM_PATH is a dynamic `__getattr__` property and read_wm /
write_wm / cmd_init / cmd_reset resolve through `wm_path()` (BODY_WM_PATH
env → else AGENT_DIR/session/working-memory.yaml), so patching the module
attribute is a no-op for I/O and silently targets the live bound-agent WM
(running such a test under MIND_AGENT clobbers live working memory). (g-115-1626)

Selection: honor an externally-set MIND_AGENT if present, else pick the
first directory under PROJECT_ROOT containing a local-paths.conf.

Second wrinkle (added 2026-05-19): pytest collection imports ALL test
modules before any test RUNS. So a module-level pop in test_tree_idf.py
contaminates the env that test_auto_contract.py sees at run time, even
though test_auto_contract.py sorts FIRST alphabetically. ~18 of the 20
baseline failures traced to this: lost MIND_AGENT (Cluster H —
fresh importlib-loaded wm.py re-reads env and fails assert_agent_dir),
and lost MIND_WORLD redirected to a temp dir (Cluster B-G — daemon
fixtures resolve against the wrong world). The `_restore_env_per_test`
autouse fixture below snapshots both vars at conftest-load time (before
any polluter module has imported) and restores them before each test.
"""
import os
import sys
from pathlib import Path

import pytest


_AGENTS_PARENT_DIR = "agents"  # Phase 2.5.C: sync with _paths.py AGENTS_PARENT_DIR


def _set_default_agent():
    if os.environ.get("MIND_AGENT"):
        return
    project_root = Path(__file__).resolve().parents[3]
    agents_root = project_root / _AGENTS_PARENT_DIR if _AGENTS_PARENT_DIR else project_root
    if not agents_root.is_dir():
        return  # pre-init clone: no agents/ yet, nothing to default to ()
    for entry in sorted(agents_root.iterdir()):
        if entry.is_dir() and (entry / "local-paths.conf").is_file():
            os.environ["MIND_AGENT"] = entry.name
            return


_set_default_agent()

# Hermetic storage backend (lodestar-s7 test isolation): tests must NEVER touch
# real S3. After the own-cloud cutover, .env.local carries
# STORAGE_BACKEND=own-cloud, so ANY daemon spawned from this repo —
# including test-spawned subprocess daemons that run mind_api.src.__main__ and
# its _load_env_local — would otherwise inherit own-cloud and either hit real S3
# or 500 in the hermetic test env. Pin local for the whole pytest session
# (subprocesses inherit os.environ). Own-cloud behavior is covered by the
# moto-mocked test_owncloud_backend.py, which constructs the backend directly
# rather than via this env selector, so the pin does not reduce its coverage.
os.environ["STORAGE_BACKEND"] = "local"

# : dormant-pin the own-cloud tempdir tripwire OFF for the whole pytest
# session. Under pytest ALL backends are hermetic -- get_backend() returns
# LocalBackend (STORAGE_BACKEND=local above), and the only tests reaching
# OwnCloudBackend._put construct it directly against a moto-mocked S3 (no real
# cloud), so their tmp-path PUTs are safe. The _assert_not_tempdir_put tripwire
# (refuses a PUT under a tempfile/pytest tmp dir) must therefore NOT fire here;
# it exists to catch NON-pytest runners -- main()-style `python3 test_x.py` and
# the bash aggregator (run-asp-257-suite.sh) -- where THIS conftest never loads
# and get_backend() may return a REAL own-cloud backend that would collide on the
# production S3 key (rb-2983/guard-955). The presence of this env var IS the
# "am I inside a hermetic pytest session?" signal the tripwire keys off.
os.environ["MIND_ALLOW_TMP_OWNCLOUD_PUT"] = "1"

# Gate-firings segmentation is a PER-BOX deployment flag (settings.json env,
# fleet-wide since 2026-08-17). Pin it OFF for the session so every lane that
# writes a firing — _gate_log.log()'s direct locked append (which honours the
# flag since 2026-08-18) and gate-firings-flush.py — lands on the legacy
# filename the existing assertions read (test_layer_d_telemetry,
# test_store_dupe_warn, test_phase_4_26_gate, ...). Tests that exercise the
# segment lane opt in with monkeypatch.setenv(SEGMENTED_ENV, "1")
# (test_gate_firings_paths). Same shape as the STORAGE_BACKEND pin above: the
# box's live setting must not decide what a hermetic test observes.
# REACH: pytest-collected files ONLY. test_layer_d_telemetry is a main()-style
# INVISIBLE file, so this pop never reached it and it went red fleet-wide the
# moment settings.json set the flag (2026-08-18); run-invisible-suites.sh
# carries the mirror `unset` for that half, and the test now reads through
# firings_paths() so it is correct under either flag value.
os.environ.pop("GATE_FIRINGS_SEGMENTED", None)

# Hermetic embedding index (). Sibling of the STORAGE_BACKEND pin
# above and for the same reason: a test can redirect the STORE to a tmp path,
# but retrieve.py's `_embedding_blend` calls `cosine_scores(query)` with no
# index_dir, so the widen pass scored tmp-seeded records against the REAL
# per-box index and pulled in any production ID above embedding_min_cosine
# (0.35). Point the default at a nonexistent dir: index_available() is False,
# cosine_scores returns {}, and the blend no-ops — exactly the flag-off path.
#
# This was latent for 17 days and invisible: the index named a model that
# could not load, so cosine_scores already returned {} for the WRONG reason.
# Repairing the index on 2026-07-27 immediately surfaced it as
# test_load_guardrails_filters_by_category getting {guard-001, guard-002}
# where it seeded and asserted only guard-002 — the real guard-001 scored
# 0.588 against "framework-architecture". A hermeticity hole masked by a
# broken dependency is the same shape as the gh-fixture breach ().
#
# Tests that genuinely exercise the index pass index_dir= explicitly
# (test_embedding_retrieval.py) or monkeypatch cosine_scores
# (test_embedding_blend.py, test_embedding_tree_channel.py), so none of them
# lose coverage. Verified: no test relies on the real index.
os.environ["MIND_EMBEDDING_INDEX_DIR"] = str(
    Path(__file__).resolve().parent / "_no_such_embedding_index"
)

# : CLEAR the world/meta path vars for the whole pytest session, so a
# run cannot inherit them from whatever shell launched it. Unlike the three pins
# above, this is a pop and not an assignment -- the choice is measured, not
# symmetric with STORAGE_BACKEND, and the reasoning is the whole point:
#
#   1. PRODUCTION RUNS WITH THESE UNSET. The PreToolUse[Bash] hook injects only
#      PATH/MIND_AGENT/MIND_SID; it exports neither var. Sourcing _paths.sh
#      DOES export both, so whether a test process has them set is decided by
#      how the launching shell happened to be built. Clearing reproduces the
#      production shape; pinning would manufacture a shape production lacks.
#   2. PINNING BUYS NOTHING OVER THE FALLBACK. Measured on this box: with
#      MIND_WORLD unset, _paths resolves /opt/ayoai-mind/.mind-data/world --
#      byte-identical to what the shell exports. A pin to the real world is a
#      no-op with a second source of truth attached; a pin to a tmp world would
#      break every test that reads real world content.
#   3. A PIN WOULD BE A SECOND RESOLVER. Subprocesses inherit os.environ, so a
#      pinned value hands each child a world computed at THIS import, bypassing
#      the local-paths.conf / .mind-data chain the child would otherwise run.
#      Clearing keeps _paths the single source of truth (guard: SSOT).
#
# What this does NOT buy, stated because the adjacent comment at the S3-key
# note below is the load-bearing limit: it is NOT isolation. Neither clearing
# nor pinning redirects a DAEMON-performed write -- the daemon resolves its own
# world path, so no env var set in the test process can move it. This removes
# launching-shell dependence only.
#
# PLACEMENT IS ABOVE THE _paths PRE-IMPORT, NOT MERELY ABOVE THE SNAPSHOT.
# _paths computes WORLD_DIR/META_DIR as module-level constants, and the
# pre-import below runs before the snapshot -- so a pop placed next to the
# snapshot would clear the env while leaving the shell's value already baked
# into the module cache for the rest of the session.
os.environ.pop("MIND_WORLD", None)
os.environ.pop("MIND_META", None)

# Pre-import _paths to lock AGENT_DIR into the module cache before any test
# module pops MIND_AGENT. Without this, a test that pops the env BEFORE
# _paths is first imported caches AGENT_DIR=None for the whole session.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _paths  # noqa: E402, F401  — side-effect import to lock cache

# Also expose this directory (core/scripts/tests/) on sys.path so test
# modules can `from _bash_helpers import BASH` (, 2026-05-16).
# Six tests previously each duplicated a local `_resolve_bash` to dodge
# WSL bash on Windows; the helper is now a single module here. Pytest
# discovers conftest.py and runs this insert BEFORE any test module
# imports — ad-hoc `py -3 test_foo.py` invocations still need the test
# file to add SCRIPT_DIR to sys.path themselves (the refactored tests
# do this for symmetry with their existing CORE_SCRIPTS insert).
sys.path.insert(0, str(Path(__file__).resolve().parent))


# Snapshot the bootstrap env BEFORE any test module imports — used by the
# autouse fixture below to undo collection-time pollution. Sentinel _UNSET
# distinguishes "var was absent at bootstrap" from "var was empty string".
_UNSET = object()
_BOOTSTRAP_MIND_AGENT = os.environ.get("MIND_AGENT", _UNSET)
_BOOTSTRAP_MIND_WORLD = os.environ.get("MIND_WORLD", _UNSET)
# : MIND_META was absent from this snapshot AND from the restore
# below, so unlike its world sibling it had no per-test isolation at all -- a
# module that set it at import time leaked into every later test. Snapshotted
# here for symmetry of PROTECTION (both vars now restored per test), which is
# a different question from the pin-vs-clear choice made above.
_BOOTSTRAP_MIND_META = os.environ.get("MIND_META", _UNSET)
_BOOTSTRAP_MIND_BACKEND = os.environ.get("STORAGE_BACKEND", _UNSET)
_BOOTSTRAP_ALLOW_TMP_PUT = os.environ.get("MIND_ALLOW_TMP_OWNCLOUD_PUT", _UNSET)


@pytest.fixture(autouse=True)
def _pin_intended_agent_roster(monkeypatch):
    """Pin gates.intended_agent_vocab's roster to a fixed fixture superset.

    The gate resolves the LIVE deployment roster env-independently
    (_agents._project_root() derives from __file__, so tmp-world fixtures
    cannot redirect it). Daemon-POSTing tests across this tree carry fixture
    agent names (alpha/bravo/echo/foxtrot/zeta/...), which happen to exist on
    dev-fleet rosters and NOT on a single-agent prod deployment — unpinned,
    the same test is green here and 400s after promotion (guard-1038's
    newly-blocked-shape hazard, in its cross-deployment form). The pin makes
    vocabulary verdicts deployment-independent. Tests exercising the gate
    itself (test_intended_agent_vocab_daemon_parity.py) override with their
    own monkeypatch / roster= injection — test-scoped patches stack over this
    autouse pin. Fail-open: if the gates package is unimportable in a
    stripped-down context, skip the pin rather than erroring every test.
    Mirrored in mind_api/tests/conftest.py (g-115-5651: a reset/pin fixture
    that exists in only one test-tree conftest leaves the other tree exposed
    in mixed chunks).
    """
    try:
        from gates import intended_agent_vocab as _iav
    except ImportError:
        yield
        return
    monkeypatch.setattr(
        _iav, "_resolve_roster",
        lambda: ("alpha", "bravo", "charlie", "delta", "echo",
                 "foxtrot", "omni", "zeta"))
    yield


@pytest.fixture(autouse=True)
def _restore_env_per_test():
    """Restore MIND_AGENT and MIND_WORLD before each test.

    Polluter test modules pop or overwrite these at module load. Pytest
    collects all modules before running any test, so the pollution lands
    before the FIRST test runs — even tests that sort earlier
    alphabetically than the polluters. Restoring per-test undoes the
    collection-time damage without forcing 11+ polluter files to grow
    proper setUp/tearDown discipline.

    Tests that legitimately need a different env (e.g.
    test_paths_read_local_paths_fail_loud.py) override inside the test
    body and restore in their own tearDown — this fixture is compatible
    because it runs BEFORE the test starts.
    """
    if _BOOTSTRAP_MIND_AGENT is _UNSET:
        os.environ.pop("MIND_AGENT", None)
    else:
        os.environ["MIND_AGENT"] = _BOOTSTRAP_MIND_AGENT
    if _BOOTSTRAP_MIND_WORLD is _UNSET:
        os.environ.pop("MIND_WORLD", None)
    else:
        os.environ["MIND_WORLD"] = _BOOTSTRAP_MIND_WORLD
    # : MIND_META gets the same per-test restore its world sibling
    # has always had. Both are cleared at module scope above, so on a normal run
    # both branches pop -- the else-branch exists for a caller that deliberately
    # sets them before importing conftest.
    if _BOOTSTRAP_MIND_META is _UNSET:
        os.environ.pop("MIND_META", None)
    else:
        os.environ["MIND_META"] = _BOOTSTRAP_MIND_META
    # Keep the storage backend pinned local across tests that may mutate it
    # (lodestar-s7 test isolation — see the module-level set above).
    if _BOOTSTRAP_MIND_BACKEND is _UNSET:
        os.environ.pop("STORAGE_BACKEND", None)
    else:
        os.environ["STORAGE_BACKEND"] = _BOOTSTRAP_MIND_BACKEND
    # : keep the own-cloud tempdir-tripwire dormant-pin stable across
    # tests that mutate it (the tripwire's own regression test toggles it).
    if _BOOTSTRAP_ALLOW_TMP_PUT is _UNSET:
        os.environ.pop("MIND_ALLOW_TMP_OWNCLOUD_PUT", None)
    else:
        os.environ["MIND_ALLOW_TMP_OWNCLOUD_PUT"] = _BOOTSTRAP_ALLOW_TMP_PUT
    # : restore the DERIVED backend, not just the INPUT env var above.
    # get_backend() memoizes _ACTIVE_BACKEND process-wide AND freezes the
    # governed-root map INTO that instance, so restoring STORAGE_BACKEND=local
    # is not enough -- the next get_backend() returns the cached object and
    # ignores the env entirely. A test that builds an own-cloud backend against
    # its own tmp world therefore leaves every later test in the process holding
    # a backend whose roots point at a tmp dir that no longer exists, surfacing
    # as `ValueError: <tmp>/world/pipeline.lock is not under any configured root`
    # from owncloud_backend._rel -- attributed to the victim file, arbitrarily
    # far from the test that caused it. Reset here so each test derives the
    # backend from the env this fixture just restored.
    #
    # Guarded on sys.modules rather than importing: conftest must not force the
    # storage_backend import (and its side effects) onto runs that never touch
    # it. No try/except -- if the reset helper is missing or raises, that is a
    # real signal and swallowing it would recreate a silent-failure class.
    _sb = sys.modules.get("storage_backend")
    if _sb is not None and hasattr(_sb, "reset_backend_for_tests"):
        _sb.reset_backend_for_tests()
    yield


@pytest.fixture(autouse=True)
def _redirect_sweep_stats_sink(tmp_path_factory):
    """Redirect the owncloud_sync sweep-telemetry sink away from the REAL
    core/logs/owncloud-sweep-stats.jsonl for every test (g-115-2468).

    Any test that runs a real sweep()/sync_file() with non-boring stats would
    otherwise append test residue into the production forensic sink (the sink
    is machine-local and gitignored, so the pollution would be invisible to
    git yet corrupt lane-attribution forensics). Same defense shape as the
    STORAGE_BACKEND pin above: protect the shared surface once in conftest
    instead of asking every owncloud test to remember. No-op when
    owncloud_sync was never imported by the test session.
    """
    mod = sys.modules.get("owncloud_sync")
    if mod is None or not hasattr(mod, "_SWEEP_STATS_LOG"):
        yield
        return
    orig = mod._SWEEP_STATS_LOG
    mod._SWEEP_STATS_LOG = (tmp_path_factory.mktemp("sweep-stats")
                            / "owncloud-sweep-stats.jsonl")
    try:
        yield
    finally:
        mod._SWEEP_STATS_LOG = orig


# ── Default subprocess timeout — suite-abort defense () ────────────
# Measured 2026-07-25 on DESKTOP-O91DLK2: spawning bash from Windows Python
# intermittently HANGS AT BASH STARTUP (proven: `bash -x` emits zero trace, so
# the hang precedes the first command). Hung bashes sit at 0 CPU, never exit,
# and accumulate — the more that pile up, the more new spawns hang. The parent
# then blocks forever in communicate(), and pytest's faulthandler bound
# (faulthandler_timeout=600 + exit_on_timeout, pytest.ini) ABORTS THE WHOLE RUN.
# One unlucky spawn therefore destroys a ~90-minute suite and, with it, anyone's
# ability to satisfy .claude/rules/run-full-suite-after-deep-code.md on this box.
#
# 149 of 333 subprocess.run call sites across 149 test files passed no timeout.
# Patching the shared surface ONCE here beats editing 149 sites AND covers every
# test written later — the same reasoning as the STORAGE_BACKEND pin and the
# sweep-stats redirect above.
#
# This does NOT fix the environment (that is  Layer 2, root unknown —
# candidates: MSYS2 fork-emulation contention, AV scanning, handle pressure).
# It converts an unbounded hang into ONE attributable test failure, so the run
# completes and names its victim instead of dying anonymously at 70%.
#
# Default 300s: comfortably above the slowest legitimate test on record (139.6s,
# per run-full-suite-after-deep-code.md) and comfortably BELOW the 600s
# faulthandler abort — the ordering that matters, so our timeout always fires
# first. Explicit caller timeouts are never overridden.
_SUBPROC_TIMEOUT_ENV = "MIND_TEST_SUBPROCESS_TIMEOUT"


def _default_subprocess_timeout():
    """Seconds to inject, or None to disable the guard entirely (set env to 0)."""
    raw = os.environ.get(_SUBPROC_TIMEOUT_ENV, "").strip()
    if not raw:
        return 300.0
    try:
        val = float(raw)
    except ValueError:
        return 300.0
    return val if val > 0 else None


def _normalize_bash(args):
    """Rewrite a bare `bash` argv[0] to the resolved Git-Bash path ().

    ROOT CAUSE (measured 2026-07-25): subprocess.run(["bash", ...]) goes through
    CreateProcess with lpApplicationName=NULL, and Windows searches **System32
    BEFORE PATH**. On this box C:/Windows/System32/bash.exe exists — it is the
    WSL launcher — and WSL is broken here (Wsl/0x80080005), so the launcher
    blocks forever on the dead LxssManager service. The process never reaches
    bash: `bash -x` emits zero trace, msys-2.0.dll never loads, and its threads
    sit in EventPairLow (LPC) waits at 0 CPU, accumulating until the 600s
    faulthandler bound aborts the whole suite.

    Note shutil.which("bash") does NOT reveal this: which() searches PATH only,
    so it reports Git's bash while CreateProcess picks System32's. Proven by
    controlled comparison — bare "bash" HUNG while the identical binary named
    explicitly succeeded in 0.14s, twice.

    _bash_helpers.BASH already resolves this correctly (g-115-725, 2026-05-16)
    and 38 test files use it — but several do not, and each is a latent
    suite-abort. Normalizing here fixes every current AND future caller instead
    of asking each one to remember, matching this file's existing philosophy.
    Non-win32 platforms and non-bare argv[0] values are passed through untouched.
    """
    if sys.platform != "win32" or not args:
        return args
    seq = args[0] if isinstance(args[0], (list, tuple)) else args
    if not isinstance(seq, (list, tuple)) or not seq:
        return args
    if str(seq[0]).lower() not in ("bash", "bash.exe"):
        return args
    try:
        from _bash_helpers import BASH
    except Exception:
        return args
    if not BASH or str(BASH).lower() in ("bash", "bash.exe"):
        return args
    new = [BASH, *list(seq)[1:]]
    return (new, *args[1:]) if isinstance(args[0], (list, tuple)) else (new,)


def _inject_timeout(kwargs, default):
    """Return kwargs with `timeout` filled in when the caller left it unbounded.

    Injects when `timeout` is ABSENT or explicitly None — both mean "unbounded"
    today, and no test deliberately wants an unbounded hang. An explicit numeric
    timeout is never overridden: the guard fills a gap, it does not impose
    policy on a caller who already chose. Pure and side-effect-free so the
    decision can be tested without spawning anything.
    """
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = default
    return kwargs


@pytest.fixture(autouse=True)
def _default_subprocess_timeout_guard():
    """Inject a default timeout into subprocess.run/call when none was given.

    Injects only when `timeout` is ABSENT or explicitly None — both mean
    "unbounded" today, and no test deliberately wants an unbounded hang. A test
    that passes a real timeout keeps it untouched.

    subprocess.Popen is deliberately NOT wrapped: its timeout lives on
    .wait()/.communicate(), not the constructor, so a correct wrapper would have
    to proxy the object. Only 7 Popen sites exist (4 without a timeout) versus
    333 run sites, so the cost/benefit does not justify the added surface — the
    remaining Popen exposure is documented in g-115-3085 rather than hidden.
    """
    import subprocess as _sp
    default = _default_subprocess_timeout()
    if default is None:
        yield
        return
    _orig_run, _orig_call = _sp.run, _sp.call

    def _run(*args, **kwargs):
        return _orig_run(*_normalize_bash(args), **_inject_timeout(kwargs, default))

    def _call(*args, **kwargs):
        return _orig_call(*_normalize_bash(args), **_inject_timeout(kwargs, default))

    _sp.run, _sp.call = _run, _call
    try:
        yield
    finally:
        _sp.run, _sp.call = _orig_run, _orig_call

# ---------------------------------------------------------------------------
# The phantom team-state shard purge MOVED to the repo-root conftest.py
# (, 2026-09-06). It was here, which meant it guarded only this ONE
# of the three testpaths pytest.ini declares -- while core/tests/gates (six
# files binding a fake MIND_AGENT) and mind_api/tests ran with no cleanup at
# all. Phantom rows rose 8 -> 9 -> 10 across three boxes in the month AFTER
# this fixture shipped, because it was never loaded for the trees polluting.
# Do NOT re-add it here: two copies drift, and a root conftest covers any
# future testpath for free. See conftest.py at the repo root for the full
# incident history that used to live in this block.
# ---------------------------------------------------------------------------
