"""test_intended_agent_vocab_daemon_parity.py — selection-stack review 2026-08-21.

`intended_agent` routes a goal: an active agent name, "either" ("no strong
signal — defer to selector"), or null. aspirations.py::validate_goal has
checked the value against the live vocabulary since g-282-02, but the daemon
`_validate_goal` subset omitted it, and under no-python-cli-fallback the daemon
IS the live write path — the fourth instance of the guard-547 orphaning
(prose-verification g-115-1440, check-schema g-115-5170, depends_on
g-115-6979). Measured 2026-08-21: 5 live goals carried "agent" / "reducer" /
"any", all filed by insight-trigger-sweep.py copying the board tag
`requires_action_by:<x>` verbatim.

Off-vocab is not merely cosmetic: the read side treats it as "either" on a
healthy box (g-115-3482) but the conservative unresolvable-roster branch makes
the same goal INVISIBLE on a box whose roster read fails, and a typo of a real
agent name silently converts a deliberate single-agent routing into a
broadcast.

gates.intended_agent_vocab is the single-source module imported by both the
CLI and the daemon, per guard-547.

Contracts pinned here:
  1. add_goal REJECTS an off-vocabulary intended_agent; the goal does not land
     on disk. (X-Mind-Override-All is set by the helper — the _assert_*
     validation block is NOT bypassable by it, which is what makes the sweep's
     writer-side normalization mandatory.)
  2. add_goal ACCEPTS "either", an active roster name, absent, and null.
  3. update_goal does NOT retroactively wedge a status change on a goal that
     already carries an off-vocab value (ADD-sites-only guarantee — legacy
     carriers can arrive via merge from another box).
  4. evaluate() returns the right verdict for noop / pass / block, including
     the two fail-open branches (empty roster, roster resolution raising) and
     the non-string block. Direct unit, roster injected.
  5. CLI aspirations.validate_goal still RAISES (delegation regression — the
     refactor from the inline copy preserved the contract).

Roster determinism: `_agents._project_root()` derives from __file__ (env-
independent), so the live resolver would read THIS deployment's roster —
nondeterministic across boxes and empty on a fresh clone (where the gate
correctly fails open, which would silently skip the reject contracts). Every
test therefore pins the roster by monkeypatching
`gates.intended_agent_vocab._resolve_roster` — the documented test seam — and
the daemon tests work the same way because DaemonFixture's Server runs
IN-PROCESS, sharing the module object.

Pattern: DaemonFixture + direct HTTP POST for (1)-(3); direct import for
(4)-(5). Mirrors test_depends_on_consistency_daemon_parity.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from _daemon_fixture import DaemonFixture  # noqa: E402
import aspirations  # noqa: E402
from gates import intended_agent_vocab  # noqa: E402
from gates.intended_agent_vocab import evaluate as vocab_evaluate  # noqa: E402

ROSTER = ("alpha", "bravo")


@pytest.fixture
def pinned_roster(monkeypatch):
    monkeypatch.setattr(intended_agent_vocab, "_resolve_roster", lambda: ROSTER)


def _make_world(tmp: Path) -> tuple[Path, Path]:
    """Tempdir world with asp-200 holding , which ALREADY carries an
    off-vocab intended_agent — the contract-3 target, seeded directly into the
    file exactly how the five live carriers got there (they predate the gate)."""
    world = tmp / "world"
    world.mkdir()

    legacy_offvocab = {
        "id": "g-200-01",
        "title": "Legacy goal carrying off-vocab intended_agent",
        "description": "Pre-existing carrier; must stay mutable.",
        "status": "pending",
        "priority": "MEDIUM",
        "intended_agent": "any",
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    asp = {
        "id": "asp-200",
        "title": "intended_agent vocabulary daemon parity",
        "motivation": "Test daemon-path intended_agent vocabulary rejection",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-01T00:00:00",
        "goals": [legacy_offvocab],
    }
    with open(world / "aspirations.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(asp, ensure_ascii=False) + "\n")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")

    agent_dir = tmp / "alpha"
    agent_dir.mkdir()
    (agent_dir / "session").mkdir()
    (agent_dir / "aspirations.jsonl").write_text("", encoding="utf-8")
    (agent_dir / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world, agent_dir


def _add_goal(port: int, body: dict, agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-200&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    req.add_header("X-Mind-Override-All", "test-fixture")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _update_goal(port: int, goal_id: str, field: str, value,
                 agent: str = "alpha") -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/update-goal"
           f"?id={goal_id}&field={field}&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(value).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", agent)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _goal_count(world: Path) -> int:
    n = 0
    for line in (world / "aspirations.jsonl").read_text(
            encoding="utf-8").splitlines():
        if line.strip():
            n += len(json.loads(line).get("goals", []))
    return n


def _base_body(**over) -> dict:
    body = {
        "title": "Routed goal",
        "description": "Goal with a routing hint.",
        "priority": "MEDIUM",
        "status": "pending",
        "verification": {"outcomes": [], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
    }
    body.update(over)
    return body


# --- (1) add_goal rejects an off-vocabulary intended_agent ------------------
def test_add_goal_rejects_off_vocab(pinned_roster):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            status, body = _add_goal(df.port, _base_body(intended_agent="reducer"))
            assert status == 400, body
            assert "vocabulary" in body, body
            assert "either" in body, body
            assert _goal_count(world) == before, "rejected goal landed on disk"


def test_add_goal_rejects_typo_of_real_name(pinned_roster):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _add_goal(df.port, _base_body(intended_agent="bravp"))
            assert status == 400, body


# --- (2) add_goal accepts the whole legal vocabulary ------------------------
def test_add_goal_accepts_either_member_absent_and_null(pinned_roster):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            for over in ({"intended_agent": "either"},
                         {"intended_agent": "bravo"},
                         {},
                         {"intended_agent": None}):
                status, body = _add_goal(df.port, _base_body(**over))
                assert status == 200, (over, body)


# --- (3) update_goal does not wedge a legacy off-vocab carrier --------------
def test_update_goal_does_not_wedge_legacy_carrier(pinned_roster):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _update_goal(df.port, "g-200-01", "status", "in-progress")
            assert status == 200, (
                "ADD-sites-only guarantee broken: a status change on a legacy "
                "off-vocab carrier was refused — the gate leaked into "
                "_validate_goal. " + body)


# --- (4) evaluate() unit verdicts -------------------------------------------
def test_evaluate_blocks_off_vocab_with_injected_roster():
    v = vocab_evaluate({"id": "g-1-01", "intended_agent": "any"}, roster=ROSTER)
    assert v["would_block"] is True
    assert v["decision"] == "block"
    assert v["violations"] == ["off_vocabulary"]
    assert "either" in v["message"]


def test_evaluate_passes_member_and_either():
    for val in ("alpha", "bravo", "either"):
        v = vocab_evaluate({"id": "g-1-02", "intended_agent": val}, roster=ROSTER)
        assert v["decision"] == "pass", val


def test_evaluate_noop_on_absent_and_null():
    assert vocab_evaluate({"id": "g-1-03"}, roster=ROSTER)["decision"] == "noop"
    assert vocab_evaluate({"id": "g-1-04", "intended_agent": None},
                          roster=ROSTER)["decision"] == "noop"


def test_evaluate_blocks_non_string():
    v = vocab_evaluate({"id": "g-1-05", "intended_agent": ["alpha"]}, roster=ROSTER)
    assert v["would_block"] is True
    assert v["violations"] == ["not_a_string"]


def test_evaluate_fails_open_on_empty_roster():
    # Fresh install / all-retired: vocabulary collapses to {"either"} alone —
    # cannot distinguish a typo from the first agent's own name (rb-1028).
    v = vocab_evaluate({"id": "g-1-06", "intended_agent": "whoever"}, roster=())
    assert v["decision"] == "pass"


def test_evaluate_fails_open_on_roster_exception(monkeypatch):
    def _boom():
        raise OSError("team-state unreadable")
    monkeypatch.setattr(intended_agent_vocab, "_resolve_roster", _boom)
    v = vocab_evaluate({"id": "g-1-07", "intended_agent": "whoever"})
    assert v["decision"] == "pass"


def test_evaluate_is_strict_on_raw_value():
    # Write-side is byte-strict (CLI parity); READ-side stripping tolerance
    # (routes_away_from) is deliberately not mirrored here.
    v = vocab_evaluate({"id": "g-1-08", "intended_agent": " alpha"}, roster=ROSTER)
    assert v["would_block"] is True


# --- (5) CLI delegation preserved -------------------------------------------
def test_cli_validate_goal_still_raises(monkeypatch):
    monkeypatch.setattr(intended_agent_vocab, "_resolve_roster", lambda: ROSTER)
    goal = {
        "id": "g-100-09",
        "title": "t",
        "status": "pending",
        "priority": "MEDIUM",
        "intended_agent": "reducer",
    }
    with pytest.raises(ValueError, match="vocabulary"):
        aspirations.validate_goal(goal)


def test_cli_validate_goal_accepts_member(monkeypatch):
    monkeypatch.setattr(intended_agent_vocab, "_resolve_roster", lambda: ROSTER)
    goal = {
        "id": "g-100-10",
        "title": "t",
        "status": "pending",
        "priority": "MEDIUM",
        "intended_agent": "bravo",
    }
    aspirations.validate_goal(goal)  # must not raise


# --- (6) ADD-half unknown-field gate ( item 3) --------------------
# The update endpoint has refused unknown field names since item 1; these pin
# the add-path mirror: a goal cannot be BORN carrying keys the allowlist does
# not know. Same harness as (1)-(3); rides in this file because it shares the
# selection-stack-review add-site wiring (and the fixture).

def _add_goal_with_headers(port: int, body: dict, headers: dict) -> tuple[int, str]:
    url = (f"http://127.0.0.1:{port}/v1/aspirations/add-goal"
           "?asp_id=asp-200&source=world")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Mind-Agent", "alpha")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_add_goal_rejects_unknown_field(pinned_roster):
    """An unregistered key on a NEW goal is refused with the same gate name
    and hint machinery as the update path — and X-Mind-Override-All does NOT
    bypass it (only X-Mind-Allow-New-Field does), matching the update gate."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            before = _goal_count(world)
            # _add_goal sends X-Mind-Override-All — proves it is not a bypass.
            status, body = _add_goal(
                df.port, _base_body(outcome_notez="typo of outcome_note"))
            assert status == 400, body
            parsed = json.loads(body)
            assert parsed["gate"] == "goal-field-allowlist", body
            assert parsed["unknown_fields"] == ["outcome_notez"], body
            assert _goal_count(world) == before, "rejected goal landed on disk"


def test_add_goal_unknown_field_override_header_accepts(pinned_roster):
    with tempfile.TemporaryDirectory() as tmpd:
        world, _ = _make_world(Path(tmpd))
        with DaemonFixture(world) as df:
            status, body = _add_goal_with_headers(
                df.port, _base_body(brand_new_field="deliberate"),
                {"X-Mind-Allow-New-Field": "test justification",
                 "X-Mind-Override-All": "test-fixture"})
            assert status == 200, body
