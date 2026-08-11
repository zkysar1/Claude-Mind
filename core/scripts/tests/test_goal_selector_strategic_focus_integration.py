"""test_goal_selector_strategic_focus_integration.py — .

The INTEGRATION half of test_goal_selector_strategic_focus.py. That file pins the
helper logic and the score_goal call site, but every one of its tests
monkeypatches `_load_team_state_cached`, so the seam between a real
`team-state.yaml` on disk and `load_strategic_focus()` is structurally outside
what any of its fixtures can falsify. One LIVE manual probe confirmed end-to-end
behavior (g-335-155 6.90 -> 8.92); a manual probe is not a repeatable test.

WHAT THE MOCK EXCLUDES (guard-1462 — name the layers the seam places out of reach):
    WORLD_DIR / "team-state.yaml"          <- file existence + YAML parse
      -> _load_team_state_cached()          <- retry loop, fail-open, run cache
        -> _compose_rows()                  <- g-328-27 per-agent shard overlay
          -> load_strategic_focus()         <- isinstance(dict) branch + asp regex
    The mock is injected at the TOP of that chain, so all four layers are
    uncovered. These tests drive the real chain by pointing the module constant
    at a tmp world.

STILL EXCLUDED BY THESE TESTS, deliberately (same guard-1462 discipline applied to
my own seam): how `WORLD_DIR` itself is derived from `agents/<agent>/local-paths.conf`
at import time. These tests monkeypatch that constant, so a regression in path
RESOLUTION would not be caught here — only a regression in what happens once the
directory is known. Covering resolution needs a subprocess run against a seeded
conf, which guard-577 warns is the wrong tool for import-time constants.

WHY monkeypatch(gs.WORLD_DIR) AND NOT an MIND_WORLD env var (guard-577): goal-selector
computes WORLD_DIR at import time from _paths. A runtime env change does not
retroactively move an already-bound module constant, so an env-var-only fixture
would silently keep reading the LIVE world — which is also exactly the
guard-708 hazard (a live-world read makes the test both flaky and capable of
naming real fleet data in an assertion message).

Hermetic: no live world/, no daemon, no network. Safe with a live fleet running.
Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_goal_selector_strategic_focus_integration.py
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Pin MIND_AGENT across import, then restore — goal-selector resolves agent-scoped
# paths at import time. Mirrors test_goal_selector_strategic_focus.py.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")
_team_state = importlib.import_module("_team_state")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


LIVE_PRIMARY = (
    "Product completeness: asp-335 Vinheim+Lodestar parity (user directive "
    "2026-07-04) and asp-334 box live-agent bring-up. Product goals outrank "
    "routine infra sweeps at selection time until asp-335 drains."
)


def _reset_caches():
    """Clear every memo between the tmp world and a fresh read.

    THREE caches, not one. `_TEAM_STATE_CACHE` and `_STRATEGIC_FOCUS` are the
    obvious pair the unit-test file already resets; `_team_state._backend_rows_cache`
    is keyed by rows-dir PATH STRING, so a stale entry from another test's tmp_path
    would not collide — but a repeat call within one test's own world would serve a
    memoized row set and mask a shard change. Clearing all three keeps each test's
    read genuinely fresh.
    """
    gs._TEAM_STATE_CACHE = None
    gs._STRATEGIC_FOCUS = None
    _team_state._backend_rows_cache.clear()


@pytest.fixture
def tmp_world(tmp_path, monkeypatch):
    """A real world dir wired into the REAL reader. No _load_team_state_cached mock.

    That absence is the entire point of this file — if a future edit adds a
    monkeypatch of that function here, this file stops testing anything the
    sibling unit-test file does not already cover.
    """
    world = tmp_path / "world"
    world.mkdir()
    monkeypatch.setattr(gs, "WORLD_DIR", world)
    _reset_caches()
    yield world
    _reset_caches()


def _write_team_state(world: Path, doc: dict) -> Path:
    p = world / "team-state.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return p


def _write_shard(world: Path, name: str, row: dict) -> Path:
    d = world / "team-state" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(yaml.safe_dump(row, sort_keys=False), encoding="utf-8")
    return p


# ── the load-bearing assertion ──────────────────────────────────────────────

def test_real_yaml_on_disk_reaches_the_boost(tmp_world):
    """A real file, the real reader, the real regex — a named aspiration is boosted.

    This is the assertion the mocked suite structurally cannot make.
    """
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": LIVE_PRIMARY, "set_by": "user"}})

    assert gs.strategic_focus_boost("asp-335", 0.5) > 0
    assert gs.strategic_focus_boost("asp-334", 0.5) > 0
    assert gs.strategic_focus_boost("asp-115", 0.5) == 0.0


def test_the_read_actually_traverses_the_file(tmp_world):
    """Positive control for the fixture itself.

    If `WORLD_DIR` were not really redirected, the tests above could pass by
    reading the LIVE world (whose strategic_focus also names asp-335 — so the
    happy-path assertion alone cannot distinguish 'my tmp file was read' from
    'the live file was read'). A sentinel aspiration that exists in NO real
    team-state makes the two cases distinguishable.
    """
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": "Focus asp-99999 only.", "set_by": "test"}})

    assert gs.strategic_focus_boost("asp-99999", 0.0) > 0
    # ...and the live directive's aspiration is NOT boosted, proving the live
    # file was not what got read.
    assert gs.strategic_focus_boost("asp-335", 0.0) == 0.0


def test_drained_aspiration_self_retires_through_the_real_read(tmp_world):
    """completion_ratio >= 1.0 stops the boost — exercised via the real chain."""
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": LIVE_PRIMARY, "set_by": "user"}})

    assert gs.strategic_focus_boost("asp-335", 0.99) > 0
    assert gs.strategic_focus_boost("asp-335", 1.0) == 0.0


# ── the warn branch, reachable here without a mocked read ────────────────────

def test_prose_naming_no_aspiration_warns_on_stderr(tmp_world, capsys):
    """Directive text that parses to zero targets must be loud, not vacuous.

    The `checker-input-assumption-defects` shape: the user renames a lane or
    writes 'asp 335' with a space, the regex matches nothing, and the boost
    silently does nothing while looking honored.
    """
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": "Prioritise the product lane over routine sweeps.",
        "set_by": "user"}})

    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0
    err = capsys.readouterr().err
    assert "strategic_focus is set but names" in err
    assert "set_by='user'" in err


def test_empty_prose_does_not_warn(tmp_world, capsys):
    """The warn must be a signal, not wallpaper — no directive text, no noise."""
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": "", "secondary": None, "set_by": "user"}})

    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0
    assert "strategic_focus is set but names" not in capsys.readouterr().err


def test_secondary_field_is_read_too(tmp_world):
    """`secondary` is optional and present only in some deployments; when it is
    present its aspirations count. Pinned because the join over ('primary',
    'secondary') is easy to narrow to primary-only during a refactor."""
    _write_team_state(tmp_world, {"strategic_focus": {
        "primary": "Focus asp-335.", "secondary": "Then asp-336.",
        "set_by": "user"}})

    assert gs.strategic_focus_boost("asp-335", 0.5) > 0
    assert gs.strategic_focus_boost("asp-336", 0.5) > 0


# ── the  shard overlay, the layer nearest the mock ──────────────────

def test_shard_overlay_does_not_clobber_strategic_focus(tmp_world):
    """_compose_rows overlays per-agent rows; strategic_focus is a CORE key and
    must survive that overlay.

    This is the specific 'moves into a shard' scenario the parent goal names.
    compose_state mutates the core doc in place and lifts last_updated across
    rows, so a future change touching more keys could plausibly disturb it.
    """
    _write_team_state(tmp_world, {
        "strategic_focus": {"primary": LIVE_PRIMARY, "set_by": "user"},
        "agent_status": {"alpha": {"last_active": "2026-07-01T00:00:00"}},
    })
    _write_shard(tmp_world, "zeta", {
        "last_active": "2026-07-27T00:00:00", "in_flight": None})

    state = gs._load_team_state_cached()
    assert "zeta" in (state.get("agent_status") or {}), "shard did not compose"
    assert gs.strategic_focus_boost("asp-335", 0.5) > 0, \
        "shard overlay destroyed the core strategic_focus key"


# ── fail-open paths ─────────────────────────────────────────────────────────

def test_missing_team_state_file_yields_no_boost_and_no_crash(tmp_world):
    """Legitimate pre-coordination state: no file at all."""
    assert not (tmp_world / "team-state.yaml").exists()
    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0


def test_malformed_yaml_fails_open_through_the_real_retry_loop(tmp_world, capsys):
    """A truncated file (the rb-2429 contended-write shape) must not crash
    selection. Exercises the REAL retry loop, which the mocked suite replaces
    wholesale with a raising lambda."""
    (tmp_world / "team-state.yaml").write_text(
        "strategic_focus:\n  primary: \"unterminated\n", encoding="utf-8")

    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0
    assert "team-state.yaml unreadable" in capsys.readouterr().err


# ── characterization: a KNOWN blind spot, pinned rather than asserted-as-good ─

def test_nondict_strategic_focus_is_silently_ignored_KNOWN_GAP(tmp_world, capsys):
    """CHARACTERIZATION — pins CURRENT behavior, which is NOT the desired behavior.

    The parent goal names this exactly: if the schema moves strategic_focus so
    `sf` stops being a dict, the `isinstance(sf, dict)` branch is skipped, `asps`
    stays empty, AND the misparse warn does not fire *because the warn lives
    inside that same branch*. The result is a directive that silently boosts
    nothing with no diagnostic anywhere.

    Asserting the silence is deliberate: it documents the gap at the exact line
    that would have to change, so whoever fixes it sees this test fail and
    updates it on purpose rather than discovering the branch by accident. Do NOT
    read this test as approval of the behavior.
    """
    _write_team_state(tmp_world, {"strategic_focus": "Focus asp-335."})

    assert gs.strategic_focus_boost("asp-335", 0.5) == 0.0
    err = capsys.readouterr().err
    assert "strategic_focus is set but names" not in err, (
        "warn now fires for non-dict strategic_focus — the gap this test "
        "characterizes has been FIXED; update this test to assert the warn")


# ── the meta-guard, and the test that proves the meta-guard fires ───────────

_READER = "_load_team_state_cached"

# Call names that INJECT a replacement. `object` is here for `patch.object(...)`,
# whose func node is the attribute `object` — the raw-line predicate this
# replaced looked for the literal `patch(` and so could never see it.
_INJECTORS = frozenset({"setattr", "patch", "object", "patch_object"})


def _reader_mock_offenders(src: str) -> list[str]:
    """Return a description per call in `src` that injects a replacement for the
    reader. Parses the source instead of scanning raw lines.

    Extracted rather than inlined into the meta-guard below because a guard that
    only ever runs against a clean file proves it is PRESENT, never that it
    FIRES (guard-1638). The two call sites that exist today are the meta-guard
    and `test_reader_mock_guard_fires_on_injection_shapes`, which drives the
    sabotage shapes this file must never contain — so the sabotage COMPILES and
    the RED is this predicate's own verdict.

    Only the TARGET argument is examined, never the whole call subtree. Both
    directions of that distinction were measured on this predicate's first draft
    (fresh-eyes on g-115-3445): searching the subtree flagged
    `setattr(gs, "WORLD_DIR", make_world(_load_team_state_cached))`, which
    patches something else and merely passes the reader along, and it still
    missed `setattr(gs, _READER, ...)` because a bare Name is not a string.

    Module-level `NAME = "literal"` bindings are resolved, so referring to the
    reader through this file's own `_READER` constant is caught. That mattered
    immediately: introducing `_READER` is what made the indirect form the
    natural way to write the injection here.

    A SyntaxError is reported as an offender rather than swallowed: this file
    must parse, and a fail-open here would make the guard silently vacuous.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:  # unparseable file — never silently pass
        return [f"source does not parse: {exc}"]

    # Module-level string constants, so `setattr(gs, _READER, ...)` resolves.
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = node.value.value

    def _names_reader(arg) -> bool:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return _READER in arg.value
        if isinstance(arg, ast.Attribute):
            return arg.attr == _READER
        if isinstance(arg, ast.Name):
            return arg.id == _READER or _READER in consts.get(arg.id, "")
        return False

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name not in _INJECTORS:
            continue
        # Where the replaced symbol is named differs per injector:
        #   setattr(obj, "attr", val) / patch.object(obj, "attr") -> arg 1
        #   patch("dotted.path.to.attr")                          -> arg 0
        if name == "patch":
            targets = node.args[:1]
        else:
            targets = node.args[1:2]
        # A keyword `target=`/`attribute=` names it too.
        targets = list(targets) + [kw.value for kw in node.keywords
                                   if kw.arg in ("target", "attribute", "name")]
        if any(_names_reader(t) for t in targets):
            offenders.append(f"line {node.lineno}: {name}(...) targets {_READER}")
    return offenders


def test_this_file_never_mocks_the_reader():
    """Meta-guard. The whole value of this file is that it drives the real chain;
    a well-meaning future edit that adds a `_load_team_state_cached` monkeypatch
    would silently reduce it to a duplicate of the sibling unit-test file, and
    nothing else would flag that.

    Matches INJECTION SHAPES, not the bare word "mock". The first draft matched
    "mock" anywhere on a line and immediately failed on this file's own fixture
    docstring ("No _load_team_state_cached mock.") — a checker whose input
    assumption was wrong, flagging prose as code, which is the exact
    `checker-input-assumption-defects` shape this whole file exists to guard
    against. Kept as a comment because a future "simplify" pass would otherwise
    re-widen it.

    The second draft narrowed to raw lines carrying the reader AND `setattr` or
    `patch(`, which fixed the prose hit but left the checker matching TEXT — and
    measurement (g-115-3445) put it wrong on 5 of 9 shapes, in both directions:
      - a `monkeypatch.setattr(` wrapped across lines by black/autopep8 became
        invisible, so a formatter run disarmed the guard with no code change;
      - `patch.object(gs, "_load_team_state_cached")` never matched `patch(`;
      - the reader's name inside a docstring or a comment tripped it — the
        prose-as-code recurrence the paragraph above claims to have closed;
      - collapsing THIS function's own comprehension onto one line made the
        guard flag ITSELF, since that line then carried both tokens.
    Parsing removes the whole class: a docstring is not a Call, and a Call's
    shape does not depend on how it is wrapped.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    offenders = _reader_mock_offenders(src)
    assert not offenders, f"this file must not mock the reader: {offenders}"


def test_reader_mock_guard_fires_on_injection_shapes():
    """Mutation-proof for the meta-guard above (guard-1638): the guard runs
    against a clean file, so on the happy path it is inert and its silence is
    indistinguishable from a guard that can no longer fire at all.

    Each MUST_FLAG case is a real injection shape; each MUST_NOT case is either
    prose or a legitimate patch this file actually performs. All of them parse,
    so a shape that stops being detected fails HERE with this assertion's own
    text rather than somewhere downstream.

    The last two of each set came from a fresh-eyes pass on the AST rewrite
    itself, and both were argument-position blindness in the first draft — it
    asked whether the reader appeared ANYWHERE in the call's subtree, which is
    wrong in both directions at once. The indirect-reference miss is the sharper
    one: hoisting `_READER` to a module constant is what made
    `setattr(gs, _READER, ...)` the natural way to write the injection HERE, so
    the rewrite manufactured its own evasion path.
    """
    must_flag = {
        "one-line setattr":
            f'monkeypatch.setattr(gs, "{_READER}", lambda *a: {{}})',
        # The formatter-wrap case: the tokens land on different lines.
        "wrapped setattr":
            f'monkeypatch.setattr(\n    gs,\n    "{_READER}",\n    lambda *a: {{}},\n)',
        "patch with dotted string target":
            f'patch("core.scripts.goal_selector.{_READER}")',
        # `patch.object(` contains no `patch(` substring.
        "patch.object":
            f'patch.object(gs, "{_READER}")',
        # Named through this module's own constant, as a future edit here would.
        "setattr via module constant":
            f'_READER = "{_READER}"\nmonkeypatch.setattr(gs, _READER, lambda *a: {{}})',
        # Named as a bare reference rather than a string.
        "setattr via bare name":
            f'monkeypatch.setattr(gs, {_READER}, lambda *a: {{}})',
        "patch.object with keyword attribute":
            f'patch.object(gs, attribute="{_READER}")',
    }
    for label, src in must_flag.items():
        assert _reader_mock_offenders(src), f"guard failed to flag: {label}"

    must_not_flag = {
        # This file's real fixture patches a different attribute entirely.
        "legitimate WORLD_DIR patch":
            'monkeypatch.setattr(gs, "WORLD_DIR", world)',
        "reader named in a docstring":
            f'def f():\n    """A {_READER} monkeypatch would be bad; do not setattr it."""',
        "reader named in a comment":
            f'# never setattr the {_READER} reader\nx = 1',
        # Patches something ELSE and merely hands the real reader along; the
        # reader is still the live one, so flagging this would be a false alarm.
        "reader passed to a patch of a different attribute":
            f'monkeypatch.setattr(gs, "WORLD_DIR", make_world({_READER}))',
        # Calling the reader is the whole point of this file.
        "reader merely called":
            f'def f(gs):\n    return gs.{_READER}()',
    }
    for label, src in must_not_flag.items():
        assert not _reader_mock_offenders(src), f"guard false-positived on: {label}"
