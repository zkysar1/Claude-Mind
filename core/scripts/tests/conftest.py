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
_BOOTSTRAP_MIND_BACKEND = os.environ.get("STORAGE_BACKEND", _UNSET)
_BOOTSTRAP_ALLOW_TMP_PUT = os.environ.get("MIND_ALLOW_TMP_OWNCLOUD_PUT", _UNSET)


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
# : phantom team-state shards created by tests that reach a LIVE daemon
# ---------------------------------------------------------------------------
# Several tests bind a FAKE MIND_AGENT ("alpha-test", "ic-recovery-test-agent")
# and shell out to iteration-close.sh / heartbeat-tick.sh. On a box serving a live
# daemon those scripts' EXIT-trap heartbeat writes GO THROUGH —  refuses
# daemon SPAWNS, not calls to an already-running daemon — and the daemon
# materializes world/team-state/agents/<fake>.yaml. _agents.py::_from_team_state
# globs that directory to build ACTIVE_AGENTS, so the shard alone keeps a phantom
# in the fleet roster and turns test_capability_route_gate::test_active_agents_tripwire
# red. Measured 2026-08-07 on cc-07: running one such module advanced the LIVE shard's
# last_active by an hour.
#
# WHY THIS IS NOT SOLVED BY THE OBVIOUS THINGS, each measured rather than assumed:
#   - STORAGE_BACKEND=local (guard-955) is already pinned by these tests and does NOT
#     help. It forces LocalBackend, which stops the S3-KEY collision — that is
#     guard-955's actual scope — but a LocalBackend write still lands in the LIVE
#     world tree. "The guard-955 pin is present" is not evidence a test is
#     world-hermetic, and a reviewer who checks for the pin and stops concludes wrongly.
#   - MIND_WORLD pointed at a tmp dir does NOT redirect it either. Measured: the live
#     shard still advanced and the tmp world stayed empty, because the write is
#     performed by the DAEMON, which resolves its own world path. No env var set in
#     the test process can move it.
#   - A retirement TOMBSTONE applied ONCE does not hold. These shards had been retired
#     by hand on 2026-08-06 and came back, because a heartbeat newer than retired_at
#     auto-un-retires the row. Purging without closing this path is a treadmill.
#     Note what this does and does NOT license: a tombstone written AFTER the run's
#     last write does hold, which is why this teardown writes one (see below).
#
# AND A LOCAL unlink() DOES NOT REMOVE ANYTHING ON THIS BACKEND — measured
# 2026-08-08 (, alpha, cc-04). This fixture originally called
# p.unlink(missing_ok=True), which is precisely the operation  had
# already measured as non-durable a week earlier in _team_state.retire_agent:
# fleet boxes do not hold s3:DeleteObject, so the local mirror goes and the
# backing object survives, and the next read re-materializes the shard
# UN-tombstoned. Proof from live data rather than from re-reading the code:
# test-race-5's S3 object was last written 2026-07-31T17:58:26 and its LOCAL
# mirror carries mtime 2026-08-08T20:15:43 — recreated eight days after the
# remote object's final write, which only read-through can do. Its own
# retirement_reason records that team-state-retire.sh had unlinked it that day.
# So the unlink teardown ran faithfully and cleaned nothing, on every box, for
# as long as it shipped; the tripwire was red again ~23h after the fix landed.
#
# THE MECHANISM THIS STORE ACTUALLY SUPPORTS IS THE TOMBSTONE, written through
# the GOVERNED path (locked_modify_yaml), exactly as _team_state.retire_agent
# does at its tail. compose_agent_status drops a tombstoned row from the roster,
# so "absent from ACTIVE_AGENTS" is reached by marking in place rather than by
# deleting (guard-1072). The unlink is kept AFTER it, best-effort and never
# fatal, matching that function: where delete works the shard is gone, where it
# does not the tombstoned row is what survives, and both converge on absent.
# Ordering is load-bearing — tombstone FIRST, or the unlink drops the local file
# and the governed write re-materializes it without the tombstone.
# Do NOT "simplify" this back to a bare unlink (guard-1493).
#
# NOT A NAME LIST: a curated list of known fake-agent names is an enumeration fix —
# it goes stale the moment someone adds a new fake agent, and it does so silently,
# with no commit for review to catch. The conditions below track the MECHANISM.
#
# THE ORIGINAL "CREATE-SET DIFF" CONDITION IS GONE, AND ITS REMOVAL IS THE OTHER HALF
# OF THIS FIX (, measured 2026-08-08 on cc-04). It required that the shard
# be ABSENT at session start, on the reasoning that this scopes cleanup to the run's
# own residue. On a read-through backend that condition is satisfied almost never,
# because the phantom's S3 object is PERMANENT — fleet boxes hold no s3:DeleteObject,
# so the local mirror re-materializes on any read, and from the next session onward
# the shard is "pre-existing" forever. Measured directly: with the tombstone fix in
# place but this condition retained, one writer module still resurrected alpha-test
# and the teardown skipped it, leaving retired_by from the PRIOR purge rather than
# this teardown. The condition did not make the fixture careful; it made it a no-op.
#
# TWO CONDITIONS REMAIN, and they are the ones that were always doing the protecting.
# The original comment said so itself — a real partner agent survives on 2 and 3 —
# so dropping 1 does not widen the blast radius on real agents:
#   1. no agents/<name>/ directory exists (bravo measured the discriminator: real
#      agents have one, phantoms do not);
#   2. the name is not one of the real fleet agents (belt-and-braces against 1).
# Plus a skip that ALIGNS THIS FIXTURE WITH ITS CONSUMER rather than second-guessing
# it: a row whose tombstone currently holds is already dropped from the composed
# roster, so there is nothing to do and re-stamping it would just churn the store.
# That check imports _team_state._is_retired instead of re-deriving the
# heartbeat-newer-than-retired_at rule, so the two can never drift apart.
#
# RESIDUAL RISK, stated rather than left implicit: a genuinely NEW fleet agent that
# has no agent dir on this box and is not yet in the `real` set would be tombstoned
# here. That is bounded and self-healing — _is_retired treats a heartbeat newer than
# retired_at as revival, so the agent's very next heartbeat re-enters it in the
# roster — whereas the phantom pollution this replaces was permanent and had recurred
# at least four times (2026-08-06, 08-07, 08-08 05:49, 08-08 22:30).
def _retire_phantom_shard(path):
    """Retire a phantom team-state row through the SANCTIONED daemon path.

    THE CLEANUP MUST NOT BE AN IN-PROCESS WRITE, AND THIS IS THE WHOLE FINDING
    OF g-115-5220 (measured 2026-08-08, alpha, cc-04). The pollution and the
    cleanup sit on OPPOSITE SIDES OF THE guard-955 PIN:

      * the phantom row is written by the DAEMON, which runs own-cloud, so it
        lands in the AUTHORITATIVE store (S3);
      * this pytest process is pinned STORAGE_BACKEND=local — mandatory under
        guard-955 and correct for its own purpose — so ANY in-process write,
        even through the governed locked_modify_yaml path, lands on the LOCAL
        MIRROR ONLY;
      * the local mirror is a read-through cache, so the next backend read
        re-materializes the row FROM S3 and discards the local tombstone.

    Measured directly: running the in-process helper under the pin left the S3
    object byte-identical (same LastModified, retired_by unchanged), while the
    same retirement issued through team-state-retire.sh moved it and flipped
    _is_retired to True. So the pin that protects production from the test also
    prevents the test from undoing what the DAEMON did to production — which is
    why four successive purges (2026-08-06, 08-07, 08-08 05:49, 08-08 22:30) all
    came back, and why the unlink-based version of this fixture cleaned nothing.

    Routing through team-state-retire.sh puts the write back on the daemon's own
    lane and gets archive-before-delete plus a .graveyard receipt for free —
    hand-rolling the tombstone here would skip both.

    Fail-open by contract, and the failure mode is ALIGNED rather than merely
    tolerated: g-115-3329 refuses daemon SPAWNS under pytest, so with no live
    daemon this call simply fails and we skip — and with no live daemon no
    phantom was created either, because the EXIT-trap write had nothing to
    reach. Cleanup is available exactly when pollution is possible.
    """
    from pathlib import Path as _Path
    import sys as _sys  # ABOVE the try: the except handler below writes to it too
    try:
        import subprocess as _subp
        from _runtime_bash import BASH as _bash_exe
        _script = _Path(__file__).resolve().parents[1] / "team-state-retire.sh"
        # .as_posix(), never str(): guard-581 — a str(WindowsPath) carries
        # backslashes, which bash reads as escape introducers and strips.
        # Matches the sibling call in test_iteration_close_recovery_probe.py.
        _r = _subp.run(
            [_bash_exe, _script.as_posix(), "--agent", path.stem, "--source",
             "pytest session teardown (g-115-5220): phantom test-agent shard"],
            capture_output=True, timeout=120, check=False,
        )
        # Fail-open, but LOUDLY. A daemon-up-but-retire-FAILED path (write
        # fence, permission error) leaves the phantom AND, if we swallow this,
        # emits zero signal — the same unobservability that let the original
        # defect survive four purge cycles. pytest surfaces teardown stderr.
        if _r.returncode != 0:
            _sys.stderr.write(
                "[conftest] phantom-shard retire FAILED rc=%s for %s: %s\n"
                % (_r.returncode, path.name,
                   (_r.stderr or b"").decode("utf-8", "replace").strip()[:300])
            )
    except Exception as exc:
        _sys.stderr.write(
            "[conftest] phantom-shard retire ERRORED for %s: %r\n" % (path.name, exc)
        )
        return


@pytest.fixture(autouse=True, scope="session")
def _purge_phantom_team_state_shards():
    from pathlib import Path as _P
    try:
        import sys as _sys
        _sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
        from _paths import WORLD_DIR as _WD, PROJECT_ROOT as _PR
        from _agents import AGENTS_PARENT_DIR as _APD
    except Exception:
        yield
        return
    shards = _P(_WD) / "team-state" / "agents"
    yield
    try:
        import yaml as _yaml
        import storage_backend as _sb
        from _team_state import _is_retired
    except Exception:
        return

    # ENUMERATE FROM THE AUTHORITATIVE STORE, UNION THE LOCAL MIRROR — never the
    # local glob alone (guard-980). Measured 2026-08-08 (, cc-04): a
    # writer module ran, the daemon advanced the phantom's row in the BACKING
    # STORE, and NO local mirror existed on this box at teardown. A local-glob
    # teardown therefore saw nothing to clean and reported success, while the row
    # sat latent in S3 and re-materialized on the next read — the roster showed
    # the phantom again seconds later. The local mirror is a read-through cache,
    # so its contents describe what this box happened to have READ, not what
    # exists; enumerating from it is the same class of error as deciding
    # liveness from a raw local read. Verified both lanes disagree in practice:
    # list_dir returned 12 shards while the local dir held 11.
    backend = None
    names = set()
    try:
        backend = _sb.get_backend()
        names |= {n for n in backend.list_dir(shards) if n.endswith(".yaml")}
    except Exception:
        backend = None
    if shards.is_dir():
        names |= {p.name for p in shards.glob("*.yaml")}

    agents_root = _P(_PR) / _APD if _APD else _P(_PR)
    real = {"alpha", "bravo", "echo", "foxtrot", "zeta"}
    for fname in sorted(names):
        name = fname[:-5]
        # Cheap local discriminators first, so a healthy fleet costs no
        # backend reads for the five real agents.
        if name in real:
            continue
        if (agents_root / name).is_dir():
            continue
        p = shards / fname
        row = None
        if backend is not None:
            try:
                row = _yaml.safe_load(backend.read_text(p)) or {}
            except Exception:
                row = None
        if row is None:
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    row = _yaml.safe_load(fh) or {}
            except (OSError, _yaml.YAMLError):
                continue
        if _is_retired(row):
            continue  # tombstone holds — the roster already drops it
        _retire_phantom_shard(p)
