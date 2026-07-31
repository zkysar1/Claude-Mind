""": enforce the cross-deployment addressing rule in
insight-trigger-sweep.py — bare requires_action_by in the collision set
must FAIL LOUD.

The rule (core/config/conventions/cross-deployment-channel.md "Addressing an
agent", decided g-115-3929):

  1. `<agent>@<env-id>` is EXACT — @self-env resolves local (qualifier
     stripped); @peer-env belongs to the peer's queue, not ours;
     @unregistered-env cannot be vouched for.
  2. A bare name NOT in the collision set means the LOCAL agent — the 87%
     installed base, preserved byte-for-byte.
  3. A bare name IN the collision set (local roster ∩ peer agents) is
     AMBIGUOUS and refuses to route. Silent local-default is the defect.

What these pins hold, and why each is here rather than implied:

  1. bare non-collision converts     — the installed base. Any enforcement
                                       that breaks clause 2 fails here first.
  2. bare collision REFUSES          — the defect class itself, stated as
                                       behavior. Never filed, verdict named.
  3. explicit @self-env resolves     — the RECOVERY path: refusal is only
                                       recoverable because qualification
                                       works. Target is stripped to bare.
  4. explicit @peer-env refuses      — a peer's agent must not convert into
                                       THIS deployment's queue.
  5. explicit @unregistered refuses  — cannot vouch for a deployment nobody
                                       registered (peer_surface posture).
  6. observed-evidence collision     — a peer nobody declared in the registry
                                       still collides when its agents post
                                       under <agent>@<peer-env> form.
  7. summary visibility              — refusals appear in the JSON output
                                       (count + details + collision_set); a
                                       silent skip is the banned class.

Run: python3 -m pytest core/scripts/tests/test_insight_trigger_sweep_addressing.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_addressing_under_test", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_addressing_under_test"] = its
_spec.loader.exec_module(its)

SELF_ENV = "test-self-env"
PEER_ENV = "test-peer-env"


def _msg(msg_id, *, author="localbot", target="alpha", action="revisit",
         severity="constrains", hours_ago=3.0):
    ts = (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "type": "handoff",
        "text": f"test trigger {msg_id}",
        "tags": [
            f"requires_action_by:{target}",
            f"action_type:{action}",
            f"severity:{severity}",
        ],
        "timestamp": ts,
    }) + "\n"


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    """Sandbox board + registry + roster + self-env, and stub the filing side.

    Registry fixture declares one peer env whose known_agents overlaps the
    local roster on exactly one name ('zeta') — the measured live shape.
    """
    board_dir = tmp_path / "world" / "board"
    board_dir.mkdir(parents=True)
    asp_jsonl = tmp_path / "world" / "aspirations.jsonl"
    asp_jsonl.write_text("", encoding="utf-8")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    registry_dir = tmp_path / "environments"
    registry_dir.mkdir()
    (registry_dir / "self.yaml").write_text(
        f"environment_id: {SELF_ENV}\nbackend: local\n", encoding="utf-8")
    (registry_dir / "peer.yaml").write_text(
        f"environment_id: {PEER_ENV}\nbackend: local\n"
        "known_agents:\n  - omni\n  - zeta\n", encoding="utf-8")

    monkeypatch.setattr(its, "BOARD_DIR", board_dir)
    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", registry_dir)
    monkeypatch.setattr(its, "_self_env", lambda: SELF_ENV)
    monkeypatch.setattr(
        its, "_local_roster",
        lambda: {"alpha", "bravo", "echo", "foxtrot", "zeta"})

    filed = []

    def fake_file_goal(trigger, *, dry_run=False):
        filed.append(trigger)
        return {"would_file": dry_run, "rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(its, "file_goal", fake_file_goal)
    return {"dir": board_dir, "filed": filed, "registry_dir": registry_dir}


def _write(env, channel, *rows):
    (env["dir"] / f"{channel}.jsonl").write_text("".join(rows), encoding="utf-8")


def _run_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["insight-trigger-sweep.py", "--dry-run", "--json"])
    assert its.main() == 0
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# 1 — clause 2: the installed base survives enforcement
# ---------------------------------------------------------------------------


def test_bare_non_collision_name_still_converts(env):
    """'alpha' is local-only (peers have no alpha) — routes local, unchanged."""
    _write(env, "findings", _msg("msg-local-1", target="alpha"))
    resolved, refused, collision = its.resolve_addressing(its.load_triggers())

    assert [t["msg_id"] for t in resolved] == ["msg-local-1"]
    assert refused == []
    assert resolved[0]["target"] == "alpha"
    assert collision == ["zeta"], collision


# ---------------------------------------------------------------------------
# 2 — clause 3: the defect class, stated as behavior
# ---------------------------------------------------------------------------


def test_bare_collision_name_refuses_loud_and_never_files(env, monkeypatch, capsys):
    """Bare 'zeta' exists in BOTH rosters — ambiguous, refused, NOT filed.

    Modeled on the live msg-20260727-011523-omni-4540 (omni ->
    requires_action_by:zeta), the one post targeting the measured collision
    set. Silent local-default here is the exact defect the rule bans.
    """
    _write(env, "findings", _msg("msg-ambig-1", author="omni", target="zeta"))
    summary = _run_json(monkeypatch, capsys)

    assert summary["addressing_refused"] == 1
    detail = summary["addressing_refused_details"][0]
    assert detail["msg_id"] == "msg-ambig-1"
    assert detail["verdict"] == "ambiguous_collision"
    assert "zeta" in detail["reason"]
    assert summary["filed"] == 0
    assert env["filed"] == []
    # Refused items must not linger in pending either.
    assert summary["pending"] == []


# ---------------------------------------------------------------------------
# 3 — clause 1, recovery path: explicit @self-env resolves local
# ---------------------------------------------------------------------------


def test_explicit_self_env_qualifier_resolves_local(env):
    """`zeta@<self-env>` is EXACT — resolves to the local zeta, qualifier
    stripped, so intended_agent lands as the bare local name. This is what
    makes the clause-3 refusal recoverable."""
    _write(env, "findings", _msg("msg-exact-1", target=f"zeta@{SELF_ENV}"))
    resolved, refused, _ = its.resolve_addressing(its.load_triggers())

    assert refused == []
    assert [t["msg_id"] for t in resolved] == ["msg-exact-1"]
    assert resolved[0]["target"] == "zeta"

    payload = its._build_goal_payload(resolved[0])
    assert payload["intended_agent"] == "zeta"


# ---------------------------------------------------------------------------
# 4 — clause 1: @peer-env is the peer's work, not ours
# ---------------------------------------------------------------------------


def test_explicit_peer_env_qualifier_refuses(env):
    _write(env, "findings", _msg("msg-peer-1", target=f"zeta@{PEER_ENV}"))
    resolved, refused, _ = its.resolve_addressing(its.load_triggers())

    assert resolved == []
    assert len(refused) == 1
    assert refused[0]["verdict"] == "peer_addressed"
    assert refused[0]["msg_id"] == "msg-peer-1"


# ---------------------------------------------------------------------------
# 5 — clause 1: unregistered env cannot be vouched for
# ---------------------------------------------------------------------------


def test_explicit_unregistered_env_refuses(env):
    _write(env, "findings", _msg("msg-unreg-1", target="alpha@nobody-registered-this"))
    resolved, refused, _ = its.resolve_addressing(its.load_triggers())

    assert resolved == []
    assert len(refused) == 1
    assert refused[0]["verdict"] == "unknown_env"


# ---------------------------------------------------------------------------
# 6 — evidence pass: an undeclared peer still collides via @-form observation
# ---------------------------------------------------------------------------


def test_observed_peer_author_widens_collision_set(env, monkeypatch):
    """'echo' is NOT in any registry known_agents, but an author posting as
    `echo@<peer-env>` proves a peer echo exists — so a bare 'echo' target in
    the same window is ambiguous. Registry is the durable source; observation
    is the net for peers nobody declared."""
    _write(env, "findings",
           _msg("msg-evidence-1", author=f"echo@{PEER_ENV}", target="alpha"),
           _msg("msg-evidence-2", author="localbot", target="echo"))
    resolved, refused, collision = its.resolve_addressing(its.load_triggers())

    assert "echo" in collision
    assert [t["msg_id"] for t in resolved] == ["msg-evidence-1"]
    assert len(refused) == 1
    assert refused[0]["msg_id"] == "msg-evidence-2"
    assert refused[0]["verdict"] == "ambiguous_collision"


# ---------------------------------------------------------------------------
# 7 — visibility: refusals appear in the machine-readable output
# ---------------------------------------------------------------------------


def test_summary_reports_refusals_and_collision_set(env, monkeypatch, capsys):
    """A silently-narrow route is the defect class — scope must be visible.
    `scanned` stays the FULL pre-resolution count (guard-1616: account for
    what an aggregate excluded)."""
    _write(env, "findings",
           _msg("msg-ok-1", target="alpha"),
           _msg("msg-ambig-2", target="zeta"))
    summary = _run_json(monkeypatch, capsys)

    assert summary["scanned"] == 2
    assert summary["addressing_refused"] == 1
    assert summary["collision_set"] == ["zeta"]
    assert summary["filed"] == 1
    assert {d["msg_id"] for d in summary["addressing_refused_details"]} == {"msg-ambig-2"}


# ---------------------------------------------------------------------------
# fail-open posture: a missing registry must not break the installed base
# ---------------------------------------------------------------------------


def test_missing_registry_degrades_to_local_default(env, monkeypatch):
    """No registry dir -> empty collision set -> every bare name routes local
    (clause 2 preserved). Enforcement degrades OPEN for bare names; explicit
    @peer forms become unknown_env (still refused — cannot vouch)."""
    monkeypatch.setattr(its, "ENV_REGISTRY_DIR", env["registry_dir"] / "absent")
    _write(env, "findings", _msg("msg-noreg-1", target="zeta"))
    resolved, refused, collision = its.resolve_addressing(its.load_triggers())

    assert collision == []
    assert [t["msg_id"] for t in resolved] == ["msg-noreg-1"]
    assert refused == []
