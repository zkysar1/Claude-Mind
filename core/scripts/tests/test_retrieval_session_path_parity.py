""" — the retrieval/utilization manifest must resolve Body-aware.

THE DEFECT THIS PINS. The WRITER has routed a worker Body's manifest to
`sessions/<sid>/body-retrieval-session.json` since Phase 1D (g-306-64). Every
CONSUMER composed `AGENT_DIR/"session"/"retrieval-session.json"` by hand, so on
a worker Body they read a file the writer never wrote. Measured on cc-07
2026-08-19 against a live manifest of 48 pending items one directory away:

    utilization-feedback  {"status":"goal_mismatch","session_goal":null}
    phase-4-26-gate       verdict="pass"  reason="empty retrieval population"

The gate is the serious half. Its job is to BLOCK completion until utilization
is attested, and it went silently GREEN on every worker-executed goal — and
workers execute most units. A lost counter announces itself as a zero; a
fail-open gate announces nothing.

Four parts, and B is the one that matters most for THIS defect.

A. Resolver semantics, hermetic. The load-bearing case is the FALLBACK: a
   reducer / unbodied session must resolve byte-identically to the pre-fix
   expression, because that is what makes this safe to ship on a live fleet.

B. WIRING pins. Part A can pass in full while the fix is completely INERT —
   a correct resolver that nothing calls is precisely the shape of the
   original defect (the writer was right for months). Source-level pins are
   the only thing that catch a partial revert, and they assert BOTH halves:
   the new call is present AND the legacy composition is gone. Asserting only
   the former passes a file that calls the resolver and then ignores it.

C. PARITY with the daemon resolver (`mind_api/src/agent_paths.py`). The two
   are a declared mirror pair. If they disagree, the writer and the readers
   part company again and the whole class returns.

D. Non-vacuous control — the assertions must FAIL against the pre-fix
   behaviour, or they are decoration (guard-2435).
"""
import re
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
from _paths import body_state_path, retrieval_session_path  # noqa: E402

AGENT = "testagent"
SID = "sid-abc123"
AGENT_WIDE_NAME = "retrieval-session.json"
BODY_NAME = "body-retrieval-session.json"

# The five consumers that composed the path by hand before this fix.
CONSUMERS = [
    "utilization-feedback.py",
    "phase-4-26-gate.py",
    "exhaustive-search-gate.py",
    "compounding-events.py",
    "pre-apply-consult-gate.py",
]

# The exact code shape every one of them used. Prose mentions of
# `<agent>/session/retrieval-session.json` in docstrings do not match this —
# it is deliberately the quoted-path-join form, not the bare filename.
LEGACY_COMPOSITION = re.compile(r'"session"\s*/\s*"retrieval-session\.json"')

# The shell half of the same defect. These are worker-reachable: a worker calls
# iteration-close.sh at Phase 4a. One of its three sites did ACTIVE harm rather
# than merely going quiet — it overwrote the agent-wide file with a
# no-retrieval STUB, so the learning gate recorded retrieval_performed=false
# for every worker-executed goal.
SHELL_CONSUMERS = ["iteration-close.sh", "utilization-gate.sh"]

# Shell legacy shape: an interpolated dir followed by /session/<the file>.
# Requires the literal `/session/` segment, so prose mentions of the bare
# filename (which both files carry in comments) do not match.
LEGACY_SHELL_COMPOSITION = re.compile(r'\)?/session/retrieval-session\.json')


@pytest.fixture
def hermetic_root(tmp_path, monkeypatch):
    """Point _paths at a tmp PROJECT_ROOT.

    agents_root() reads the PROJECT_ROOT module global at call time, so this
    redirect reaches every helper the resolver composes.
    """
    monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MIND_SID", raising=False)
    return tmp_path


def _adir(root: Path) -> Path:
    """The agent dir every caller passes in — NOT the agent name.

    The resolver is dir-based on purpose: AGENT_DIR honours the
    MIND_AGENT_DIR override seam and agent_dir(name) does not.
    """
    return root / _paths.AGENTS_PARENT_DIR / AGENT


def _legacy(root: Path) -> Path:
    """The exact expression all five call sites used before ."""
    return root / _paths.AGENTS_PARENT_DIR / AGENT / "session" / AGENT_WIDE_NAME


def _fork_body_wm(root: Path, sid: str) -> Path:
    """Create the activation signal: a forked per-session working-memory.yaml.

    Only a NON-REDUCER Body ever has one — /start `cp`s the WM for a worker and
    not for the reducer, which is what makes this predicate a role test.
    """
    body_dir = root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / sid
    body_dir.mkdir(parents=True, exist_ok=True)
    (body_dir / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    return body_dir


# ---------------------------------------------------------------- Part A


def test_unbodied_is_byte_identical_to_legacy(hermetic_root):
    """THE acceptance criterion: no worker present => nothing moves.

    Reducers, observers and single-Body sessions all land here. If this ever
    fails, the fix has broken the common case to serve the rare one.
    """
    assert retrieval_session_path(_adir(hermetic_root)) == _legacy(hermetic_root)


def test_bodied_routes_to_the_body_basename(hermetic_root):
    """A worker resolves to sessions/<sid>/body-retrieval-session.json.

    The BASENAME is half the assertion and is the half that made this defect
    survivable-looking: `body_state_path` alone would have returned
    sessions/<sid>/retrieval-session.json — right directory, wrong file, still
    a path the writer never writes.
    """
    _fork_body_wm(hermetic_root, SID)
    got = retrieval_session_path(_adir(hermetic_root), sid=SID)
    assert got.name == BODY_NAME
    assert got.parent.name == SID
    assert got.parent.parent.name == _paths.SESSIONS_DIRNAME


def test_production_call_shape_reads_sid_from_env(hermetic_root, monkeypatch):
    """guard-920: pin the LITERAL production call, not the contract-ideal one.

    Every call site passes ONE positional arg and lets the SID come from
    $MIND_SID (injected into every Bash tool call by bash-agent-inject.py).
    A test that always passes `sid=` explicitly would keep passing if the env
    default broke, which is the only way the real callers get a SID at all.
    """
    _fork_body_wm(hermetic_root, SID)
    monkeypatch.setenv("MIND_SID", SID)
    assert retrieval_session_path(_adir(hermetic_root)).name == BODY_NAME


def test_session_dir_without_forked_wm_falls_back(hermetic_root):
    """The activation signal is the forked WM file, NOT the per-session dir.

    /start creates a per-session dir for EVERY session including the reducer,
    so keying on the dir would route the reducer into a body path and break
    the fallback for the common case.
    """
    body_dir = hermetic_root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / SID
    body_dir.mkdir(parents=True, exist_ok=True)
    assert retrieval_session_path(_adir(hermetic_root), sid=SID) == _legacy(hermetic_root)


def test_foreign_sid_falls_back(hermetic_root):
    """A SID naming somebody else's Body must not resolve into it."""
    _fork_body_wm(hermetic_root, SID)
    assert retrieval_session_path(_adir(hermetic_root), sid="some-other-sid") == _legacy(hermetic_root)


def test_resolver_honours_a_caller_resolved_agent_dir(tmp_path, monkeypatch):
    """REGRESSION PIN — the first version of this fix failed exactly here.

    `MIND_AGENT_DIR` is a documented seam (_paths.py L401-405, _paths.sh L347):
    it points AGENT_DIR at an arbitrary directory that is NOT
    PROJECT_ROOT/agents/<name>. Every consumer test drives the real script
    through it, and production deployments may set it too.

    A NAME-based resolver silently discards that override and rebuilds from
    PROJECT_ROOT — a second way to derive one path, which is the same
    single-source-of-truth violation this whole fix removes, reintroduced one
    layer down. It surfaced as phase-4-26-gate reporting
    "no retrieval-session.json — fail-open" against a manifest the test had
    just written, i.e. the gate failing OPEN again by a different route.

    So: the resolver must use the dir it is handed and never re-derive one.
    """
    override = tmp_path / "somewhere" / "else" / "agentdir"
    (override / "session").mkdir(parents=True)
    monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path / "unrelated-root")
    monkeypatch.delenv("MIND_SID", raising=False)

    got = retrieval_session_path(override)
    assert got == override / "session" / AGENT_WIDE_NAME
    assert "unrelated-root" not in str(got), (
        "resolver rebuilt the agent dir from PROJECT_ROOT instead of using the "
        "dir it was given — MIND_AGENT_DIR-style overrides are discarded."
    )


def test_resolver_honours_the_override_for_a_bodied_session(tmp_path, monkeypatch):
    """The same seam on the worker branch, which is where it actually bites."""
    override = tmp_path / "elsewhere" / "agentdir"
    body = override / _paths.SESSIONS_DIRNAME / SID
    body.mkdir(parents=True)
    (body / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path / "unrelated-root")

    got = retrieval_session_path(override, sid=SID)
    assert got == body / BODY_NAME
    assert "unrelated-root" not in str(got)


def test_body_state_path_default_basename_is_unchanged(hermetic_root):
    """The `body_filename` parameter must be invisible to existing callers.

    Four call sites (precompact-checkpoint, postcompact-restore x2,
    compact-restore-slots, loop-state-save) pass no `body_filename`. If the
    default ever stops meaning "same name on both sides", those files move
    silently and compact recovery reads the wrong checkpoint.
    """
    _fork_body_wm(hermetic_root, SID)
    got = body_state_path(AGENT, "compact-checkpoint.yaml", sid=SID)
    assert got.name == "compact-checkpoint.yaml"
    assert got.parent.name == SID


# ---------------------------------------------------------------- Part B


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_consumer_calls_the_resolver(consumer):
    """Every consumer must ROUTE through the shared resolver."""
    src = (CORE_SCRIPTS / consumer).read_text(encoding="utf-8")
    assert "retrieval_session_path" in src, (
        f"{consumer} no longer calls retrieval_session_path — the fix is inert "
        f"here and this consumer is blind on every worker Body again."
    )


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_consumer_does_not_recompose_the_legacy_path(consumer):
    """...and must not ALSO keep composing it by hand.

    The complement of the test above, and the one that catches a half-revert:
    a file can import the resolver and still read from a hand-built path two
    lines later. Only the pair pins the behaviour.
    """
    src = (CORE_SCRIPTS / consumer).read_text(encoding="utf-8")
    hits = LEGACY_COMPOSITION.findall(src)
    assert not hits, (
        f"{consumer} still composes the agent-wide manifest path by hand "
        f"({len(hits)} site(s)) — on a worker Body that reads a file the "
        f"writer never wrote."
    )


@pytest.mark.parametrize("consumer", SHELL_CONSUMERS)
def test_shell_consumer_calls_the_shell_resolver(consumer):
    """The shell half must route through _paths.sh::retrieval_session_path."""
    src = (CORE_SCRIPTS / consumer).read_text(encoding="utf-8")
    assert "retrieval_session_path" in src, (
        f"{consumer} no longer calls retrieval_session_path — a worker reaches "
        f"this script at Phase 4a, so the utilization lane is blind again."
    )


@pytest.mark.parametrize("consumer", SHELL_CONSUMERS)
def test_shell_consumer_does_not_recompose_the_legacy_path(consumer):
    src = (CORE_SCRIPTS / consumer).read_text(encoding="utf-8")
    hits = LEGACY_SHELL_COMPOSITION.findall(src)
    assert not hits, (
        f"{consumer} still composes the agent-wide manifest path by hand "
        f"({len(hits)} site(s))."
    )


def test_shell_resolver_exists_and_is_body_aware():
    """_paths.sh must carry the mirror, keyed on the same activation signal.

    Without this, the shell consumers above would import a name that does not
    exist and silently expand to the empty string — a path of
    `/retrieval-session.json`, which never exists, so every call fails OPEN and
    looks exactly like 'no retrieval happened'.
    """
    src = (CORE_SCRIPTS / "_paths.sh").read_text(encoding="utf-8")
    assert "retrieval_session_path()" in src
    assert "body-retrieval-session.json" in src
    assert "working-memory.yaml" in src, (
        "_paths.sh resolver no longer keys on the forked-WM activation signal — "
        "it must use the SAME predicate as the Python side or the two disagree "
        "about who is a worker."
    )


# ---------------------------------------------------------------- Part C


def _daemon_resolver(root: Path):
    """Build the daemon-side AgentPaths against the same hermetic root."""
    mind_api_src = CORE_SCRIPTS.parent.parent / "mind_api" / "src"
    if str(mind_api_src) not in sys.path:
        sys.path.insert(0, str(mind_api_src))
    from agent_paths import AgentPaths  # noqa: E402

    agent_dir = root / _paths.AGENTS_PARENT_DIR / AGENT
    return AgentPaths(
        agent_name=AGENT,
        world=root / "world",
        meta=root / "meta",
        agent=agent_dir,
        project_root=root,
    )


def test_parity_with_daemon_resolver_unbodied(hermetic_root):
    """CLI and daemon must agree for a reducer/unbodied session."""
    cli = retrieval_session_path(_adir(hermetic_root))
    daemon = _daemon_resolver(hermetic_root).retrieval_session_path()
    assert cli == daemon


def test_parity_with_daemon_resolver_bodied(hermetic_root):
    """CLI and daemon must agree for a worker Body — the case that broke.

    This is the assertion that keeps writer and readers together. If it ever
    fails, one side has moved and the g-115-6653 class is back.
    """
    _fork_body_wm(hermetic_root, SID)
    cli = retrieval_session_path(_adir(hermetic_root), sid=SID)
    daemon = _daemon_resolver(hermetic_root).retrieval_session_path(SID)
    assert cli == daemon
    assert cli.name == BODY_NAME


# ---------------------------------------------------------------- Part D


def test_the_assertions_are_not_vacuous(hermetic_root):
    """Positive control (guard-2435): the pre-fix behaviour must FAIL these.

    Reconstructs exactly what every consumer did before this goal and asserts
    it differs from the resolver in the bodied case. If this passes trivially,
    the tests above prove nothing.
    """
    _fork_body_wm(hermetic_root, SID)
    pre_fix = _legacy(hermetic_root)          # what the consumers used to build
    post_fix = retrieval_session_path(_adir(hermetic_root), sid=SID)
    assert pre_fix != post_fix, (
        "resolver returns the legacy path for a worker Body — the fix is a "
        "no-op and every assertion above is decoration."
    )
    assert post_fix.name == BODY_NAME and pre_fix.name == AGENT_WIDE_NAME


def test_legacy_pattern_actually_matches_the_pre_fix_source():
    """The Part-B regex must match the real pre-fix expression.

    A wiring pin whose pattern matches nothing passes forever. This asserts
    the detector detects, using the verbatim shape the consumers carried.
    """
    assert LEGACY_COMPOSITION.search('path = AGENT_DIR / "session" / "retrieval-session.json"')
    assert LEGACY_COMPOSITION.search('p = _agent_dir(agent) / "session" / "retrieval-session.json"')
    assert not LEGACY_COMPOSITION.search("path = retrieval_session_path(AGENT_DIR)")


def test_shell_legacy_pattern_actually_matches_the_pre_fix_source():
    """Same control for the shell detector, using the verbatim pre-fix lines.

    The negative cases are the ones that matter here: the shell resolver's own
    body emits `/retrieval-session.json` with no `/session/` segment, and both
    consumers mention the bare filename in comments. If the pattern matched
    either, the wiring pin would fail against a correctly-fixed tree — a
    detector that cries wolf gets deleted, which is how the pin dies.
    """
    assert LEGACY_SHELL_COMPOSITION.search(
        'local ret_file="$AGENT_DIR/session/retrieval-session.json"')
    assert LEGACY_SHELL_COMPOSITION.search(
        'SESSION_FILE="$(agent_dir "$MIND_AGENT")/session/retrieval-session.json"')
    assert not LEGACY_SHELL_COMPOSITION.search(
        'ret_file="$(retrieval_session_path "$AGENT")"')
    assert not LEGACY_SHELL_COMPOSITION.search(
        "printf '%s/retrieval-session.json' \"$(agent_state_dir \"$_agent\")\"")
    assert not LEGACY_SHELL_COMPOSITION.search(
        "# WARN: retrieval-session.json probe failed")
