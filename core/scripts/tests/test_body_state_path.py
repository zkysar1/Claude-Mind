""" — body-keyed session-state paths (iteration-checkpoint.json,
compact-checkpoint.yaml) with agent-wide fallback.

Three parts. B is the one that catches a partial revert; C is the one that
catches a resolver and a call site that are each correct and do not MEET.

A. `_paths.body_state_path` semantics — hermetic, against a tmp PROJECT_ROOT.
   The load-bearing case is the FALLBACK: a reducer (or any unbodied session)
   must resolve byte-identically to the pre-body layout, because that is what
   makes this change safe to ship on a live fleet.

B. WIRING pins — the five call sites and the two launcher SID exports.
   Part A can pass in full while the fix is completely inert: the helper is
   correct, and nothing calls it. That is not hypothetical for this defect —
   the hook processes inherit NO env vars, so without `export MIND_SID` in
   the launcher, `body_state_path` silently takes the fallback on every
   PreCompact and the clobber this goal fixes still happens. A source-level
   pin is the only thing that catches a partial revert.

C. The COMPOSED path (g-306-139) — the real writer/reader driven THROUGH the
   real resolver. A and B can both pass while the two never meet: A never calls
   a caller, and B asserts that source TEXT is present, not that the runtime
   destination moved. See the Part C header for the four-surface measurement of
   why this was uncovered.
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
from _paths import body_state_path  # noqa: E402

AGENT = "testagent"
SID = "sid-abc123"
CKPT = "iteration-checkpoint.json"
COMPACT = "compact-checkpoint.yaml"


@pytest.fixture
def hermetic_root(tmp_path, monkeypatch):
    """Point _paths at a tmp PROJECT_ROOT.

    agents_root() reads the PROJECT_ROOT module global at call time, so this
    redirect reaches every helper body_state_path composes.
    """
    monkeypatch.setattr(_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("MIND_SID", raising=False)
    return tmp_path


def _legacy(root: Path, filename: str) -> Path:
    """The exact expression every call site used before ."""
    return root / _paths.AGENTS_PARENT_DIR / AGENT / "session" / filename


def _fork_body_wm(root: Path, sid: str) -> Path:
    body_dir = root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / sid
    body_dir.mkdir(parents=True, exist_ok=True)
    (body_dir / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    return body_dir


# ---------------------------------------------------------------- Part A


@pytest.mark.parametrize("filename", [CKPT, COMPACT])
def test_unbodied_is_byte_identical_to_legacy(hermetic_root, filename):
    """THE acceptance criterion: no worker present => nothing moves."""
    assert body_state_path(AGENT, filename) == _legacy(hermetic_root, filename)


@pytest.mark.parametrize("filename", [CKPT, COMPACT])
def test_bodied_routes_under_sessions_unitkey(hermetic_root, filename):
    _fork_body_wm(hermetic_root, SID)
    got = body_state_path(AGENT, filename, sid=SID)
    assert got.parent.name == SID
    assert got.parent.parent.name == _paths.SESSIONS_DIRNAME
    assert got.name == filename


def test_session_dir_without_forked_wm_falls_back(hermetic_root):
    """The activation signal is the forked working-memory.yaml, NOT the dir.

    /start creates a per-session dir for EVERY session, so keying on the dir
    would route every session — including the reducer — into a body path and
    break the fallback for the common case.
    """
    body_dir = hermetic_root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / SID
    body_dir.mkdir(parents=True, exist_ok=True)
    assert body_state_path(AGENT, CKPT, sid=SID) == _legacy(hermetic_root, CKPT)


def test_unknown_sid_falls_back(hermetic_root):
    _fork_body_wm(hermetic_root, SID)
    assert body_state_path(AGENT, CKPT, sid="some-other-sid") == _legacy(hermetic_root, CKPT)


def test_sid_defaults_to_env(hermetic_root, monkeypatch):
    _fork_body_wm(hermetic_root, SID)
    monkeypatch.setenv("MIND_SID", SID)
    assert body_state_path(AGENT, CKPT).parent.name == SID


def test_blank_and_whitespace_env_sid_falls_back(hermetic_root, monkeypatch):
    _fork_body_wm(hermetic_root, SID)
    for blank in ("", "   "):
        monkeypatch.setenv("MIND_SID", blank)
        assert body_state_path(AGENT, CKPT) == _legacy(hermetic_root, CKPT)


def test_returns_absolute_path(hermetic_root):
    """guard-552 — a path resolver in core/scripts must return an absolute Path."""
    _fork_body_wm(hermetic_root, SID)
    assert body_state_path(AGENT, CKPT).is_absolute()
    assert body_state_path(AGENT, CKPT, sid=SID).is_absolute()


def test_reducer_and_body_paths_are_distinct(hermetic_root):
    """The whole point: a worker's write must not land on the reducer's file."""
    _fork_body_wm(hermetic_root, SID)
    assert body_state_path(AGENT, CKPT, sid=SID) != body_state_path(AGENT, CKPT)


# ---------------------------------------------------------------- Part B

# (module filename, [state files it must no longer resolve the legacy way])
WIRED_SITES = [
    ("loop-state-save.py", [CKPT]),
    ("precompact-checkpoint.py", [COMPACT]),
    ("postcompact-restore.py", [COMPACT, CKPT]),
    ("compact-restore-slots.py", [COMPACT]),
]


@pytest.mark.parametrize("module_name,state_files", WIRED_SITES)
def test_call_site_routes_through_helper(module_name, state_files):
    src = (CORE_SCRIPTS / module_name).read_text(encoding="utf-8")
    assert "body_state_path" in src, (
        f"{module_name} no longer calls body_state_path — a revert here silently "
        f"restores the cross-body clobber"
    )
    for filename in state_files:
        legacy = f'"session" / "{filename}"'
        assert legacy not in src, (
            f'{module_name} still contains the legacy literal {legacy} — '
            f"body-keying is bypassed for {filename}"
        )


@pytest.mark.parametrize(
    "launcher", ["precompact-checkpoint.sh", "postcompact-restore.sh"]
)
def test_launcher_exports_sid(launcher):
    """Hook processes inherit no env vars.

    Without this export the python side has no unitKey, body_state_path takes
    the fallback on every invocation, and the fix is inert in production while
    every hand-run test still passes.
    """
    src = (CORE_SCRIPTS / launcher).read_text(encoding="utf-8")
    assert 'export MIND_SID="$SID"' in src, (
        f"{launcher} does not export MIND_SID — the python side cannot body-key"
    )
    sid_assign = src.index("SID=$(")
    sid_export = src.index('export MIND_SID="$SID"')
    assert sid_assign < sid_export, f"{launcher} exports MIND_SID before resolving it"


def test_execute_skill_reads_checkpoint_through_wrapper():
    """CLAIM and EXECUTE are both WORKER_PHASES.

    A hardcoded agents/<a>/session/ read here would miss the body-keyed file
    the claim just wrote, and the surrounding `|| echo ""` fallback renders
    that miss as "not a cross-agent execution" — so the goal would be written
    back to the wrong queue, silently. Pin the wrapper call.
    """
    skill = CORE_SCRIPTS.parent.parent / ".claude" / "skills" / "aspirations-execute" / "SKILL.md"
    src = skill.read_text(encoding="utf-8")
    assert "loop-state-save.sh read" in src, (
        "aspirations-execute no longer reads the checkpoint through the wrapper"
    )
    # Quote-agnostic ON PURPOSE (fresh-eyes-code finding, ). The first
    # version of this pin asserted absence of the single-quoted literal only, so
    # a rewrite using double quotes passed it — measured caught=False. That is
    # guard-1802's shape (a predicate narrower than the population it must
    # cover) occurring INSIDE the guard written to prevent the regression.
    # Verified safe: the bare substring occurs 0 times in the skill today, so
    # this cannot false-RED on the wrapper-call line or its comment.
    assert "session/iteration-checkpoint.json" not in src, (
        "aspirations-execute still hardcodes the agent-wide checkpoint path "
        "(any quoting style)"
    )


def test_worker_phases_still_include_claim_and_execute():
    """The premise of this whole change.

    If CLAIM ever leaves WORKER_PHASES the defect disappears; if EXECUTE ever
    leaves it, the wrapper pin above stops being load-bearing. Either way the
    reasoning recorded here needs re-deriving rather than inheriting.
    """
    import worker_execute as we

    assert "claim" in we.WORKER_PHASES
    assert "execute" in we.WORKER_PHASES


def test_helper_is_exported_from_paths():
    assert callable(getattr(_paths, "body_state_path", None))


# ---------------------------------------------------------------- Part C

# THE COMPOSED PATH — a real caller writing and reading THROUGH the resolver.
#
# Parts A and B each stop one step short of the behaviour this change exists to
# produce, and so does every other surface that touches this mechanism. All four
# were measured on 2026-08-03 () and each excludes the composed seam a
# DIFFERENT way, which is why the gap survived four passing test files:
#
#   Part A (above)                    the resolver alone — right path, no caller,
#                                     no write.
#   Part B (above)                    source-grep — text presence, not runtime
#                                     behaviour.
#   test_claim_checkpoint_anchor.py   runs the REAL claim wrapper, but STUBS
#                                     loop-state-save.sh and pops MIND_SID from
#                                     the child env — so it asserts the PAYLOAD
#                                     and never the DESTINATION. Deliberate: its
#                                     docstring explains why it is a stubbed
#                                     harness, so this assertion does not belong
#                                     there.
#   test_loop_state_save_cross_agent_owner.py
#                                     runs the REAL write, but monkeypatches
#                                     `_checkpoint_path` — the composed resolver
#                                     is replaced by the fixture.
#
# Every layer is covered and the JOIN between them is not: guard-1462's shape, a
# fixture seam that excludes the layer under test. These tests drive the REAL
# loop-state-save `cmd_init` / `cmd_read` through the REAL `_checkpoint_path`, so
# a revert in either the helper or the call site fails HERE even while Parts A
# and B still pass.
#
# Single-box and hermetic by construction — the activation signal is a file on
# disk, so no second machine is involved. (That is why this is not an assertion
# in , whose outcomes are all cross-MACHINE and explicitly
# "structurally unsatisfiable on a single box".)

_ANCHOR = {
    "goal_id": "g-306-139",
    "aspiration_id": "asp-306",
    "source": "world",
    "phase": "selected",
    "selected_at": "2026-08-03T21:00:00",
}


def _load_loop_state_save():
    """Import the hyphenated module fresh.

    Mirrors test_loop_state_save_cross_agent_owner.py's importlib pattern.
    Fresh per test so module state cannot leak between cases.
    """
    spec = importlib.util.spec_from_file_location(
        "loop_state_save_composed", str(CORE_SCRIPTS / "loop-state-save.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bodied(hermetic_root, monkeypatch, *, sid=SID):
    """Make this session look like a non-reducer Body and load the real writer.

    The PROJECT_ROOT redirect reaches the writer because `body_state_path`
    composes `agent_dir` -> `agents_root`, which reads the module global at CALL
    time — the same property the `hermetic_root` fixture docstring relies on.
    Verified end to end by the tests below, not assumed.
    """
    monkeypatch.setenv("MIND_AGENT", AGENT)
    if sid is None:
        monkeypatch.delenv("MIND_SID", raising=False)
    else:
        monkeypatch.setenv("MIND_SID", sid)
        _fork_body_wm(hermetic_root, sid)
    return _load_loop_state_save()


def _body_ckpt(root: Path, sid: str = SID) -> Path:
    return (
        root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / sid / CKPT
    )


def test_composed_body_write_lands_under_sessions_and_spares_the_reducer(
    hermetic_root, monkeypatch
):
    """CLAIM is a WORKER_PHASE: a worker's anchor must not reach the reducer.

    The reducer's agent-wide anchor is pre-seeded with a DIFFERENT goal, because
    that is the actual drift — postcompact-restore reads that file, so a worker
    write landing there re-points the reducer at the worker's goal, silently.
    Asserting mere absence of the reducer file would pass even if the writer
    truncated it.
    """
    lss = _bodied(hermetic_root, monkeypatch)

    reducer = _legacy(hermetic_root, CKPT)
    reducer.parent.mkdir(parents=True, exist_ok=True)
    reducer.write_text(
        json.dumps({**_ANCHOR, "goal_id": "g-306-000"}), encoding="utf-8"
    )
    before = reducer.read_bytes()

    assert lss.cmd_init(argparse.Namespace(json=json.dumps(_ANCHOR))) == 0

    landed = _body_ckpt(hermetic_root)
    assert landed.exists(), "worker anchor did not land under sessions/<unitKey>"
    assert json.loads(landed.read_text(encoding="utf-8"))["goal_id"] == "g-306-139"
    assert reducer.read_bytes() == before, (
        "the worker's claim rewrote the reducer's anchor — this is the goal "
        "substitution the body-keying exists to prevent"
    )


def test_composed_body_read_sees_the_body_anchor_not_the_reducers(
    hermetic_root, monkeypatch, capsys
):
    """EXECUTE is a WORKER_PHASE too, and the read half fails silently.

    aspirations-execute reads this checkpoint to recover `cross_agent_owner`,
    behind a `|| echo ""` fallback. A read that missed the body file would fall
    back to the reducer's anchor and render the miss as "not a cross-agent
    execution" — writing the goal back to the WRONG QUEUE with nothing raised.
    """
    lss = _bodied(hermetic_root, monkeypatch)

    reducer = _legacy(hermetic_root, CKPT)
    reducer.parent.mkdir(parents=True, exist_ok=True)
    reducer.write_text(
        json.dumps({**_ANCHOR, "goal_id": "g-306-000"}), encoding="utf-8"
    )

    assert lss.cmd_init(argparse.Namespace(json=json.dumps(_ANCHOR))) == 0
    capsys.readouterr()

    assert lss.cmd_read(argparse.Namespace()) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["goal_id"] == "g-306-139", (
        "the worker read the REDUCER's anchor — the write/read pair disagree, "
        "which is exactly the case the resolver-only tests cannot see"
    )


def test_composed_unbodied_write_still_lands_agent_wide(hermetic_root, monkeypatch):
    """The fallback, COMPOSED.

    Part A proves the resolver returns the legacy path when unbodied; this
    proves the real writer actually uses it. That is the acceptance criterion
    for shipping on a live fleet, so it needs the composed form too.
    """
    lss = _bodied(hermetic_root, monkeypatch, sid=None)

    assert lss.cmd_init(argparse.Namespace(json=json.dumps(_ANCHOR))) == 0

    reducer = _legacy(hermetic_root, CKPT)
    assert reducer.exists(), "unbodied write did not land on the agent-wide path"
    assert json.loads(reducer.read_text(encoding="utf-8"))["goal_id"] == "g-306-139"
    assert not _body_ckpt(hermetic_root).exists(), (
        "an unbodied session wrote into a per-session dir"
    )


def test_live_session_is_unbodied_or_consistent():
    """Smoke check against the real tree: whatever this session is, the helper
    agrees with the forked-WM signal rather than with the env var alone."""
    agent = os.environ.get("MIND_AGENT")
    sid = os.environ.get("MIND_SID")
    if not agent or not sid:
        pytest.skip("not running under a bound agent session")
    got = body_state_path(agent, CKPT)
    forked = _paths.agent_session_dir(agent, sid) / "working-memory.yaml"
    if forked.exists():
        assert got.parent.name == sid
    else:
        assert got == _paths.agent_state_dir(agent) / CKPT
