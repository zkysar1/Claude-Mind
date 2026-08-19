"""test_bash_inject_goal_id.py — regression for .

`world/scripts/product-pr-flow.sh` has read `MIND_GOAL_ID` forward-compatibly
since 2026-08-01, and NOTHING in the tree exported it, so its durable diary row
landed `goal_id: ""` on every entry. That diary is what `stranded-claim-sweep.py
--apply` reads, and the sweep decides PER GOAL — so a merged PR, the strongest
signal a product goal emits, was the one signal it could not attribute. On
2026-08-12 it released a LIVE claim on g-335-1131 mid-execution (bravo, cc-05).

`bash-agent-inject.py` is the widest chokepoint that knows the id: it already
exports MIND_AGENT and MIND_SID before every Bash call. It now also exports
MIND_GOAL_ID, sourced from iteration-checkpoint.json.

Five contracts, in the order they can silently regress:

  1. A FRESH checkpoint yields `export MIND_GOAL_ID=<id>;` in the prefix.
  2. A STALE checkpoint yields NOTHING. This is the load-bearing one: under
     own-cloud the agent-wide checkpoint is a synced mirror of the reducer's
     box, so a non-forked session can read one days old. Exporting a WRONG goal
     id is strictly worse than exporting none — it misattributes where "" merely
     abstains, which is the exact failure the variable exists to fix.
  3. An ABSENT checkpoint yields nothing and does not crash (fail-open).
  4. A goal_id carrying shell metacharacters is REFUSED — the value is
     interpolated unquoted into the export clause.
  5. When this Body forked a WM, the PER-SESSION checkpoint wins over the
     agent-wide one — the same predicate `_paths.body_state_path` uses.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "bash_agent_inject_goalid", CORE_SCRIPTS / "bash-agent-inject.py")
bai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bai)

AGENT = "testagent"
SID = "sid-goalid-0001"


def _stamp(age_seconds: float) -> str:
    """A naive `selected_at` stamp `age_seconds` in the past (the framework's
    `date +%Y-%m-%dT%H:%M:%S` shape — no zone suffix)."""
    return (datetime.now() - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _write_ckpt(directory: Path, goal_id: str, selected_at: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "iteration-checkpoint.json").write_text(
        json.dumps({
            "goal_id": goal_id,
            "aspiration_id": "asp-115",
            "source": "world",
            "phase": "selected",
            "selected_at": selected_at,
        }),
        encoding="utf-8",
    )


def _run_hook(monkeypatch, tmp_root: Path, command: str = "echo hi") -> str:
    """Drive main() against a tmp agent root; return the injected command."""
    monkeypatch.setattr(
        bai, "resolve_binding_with_diagnostics",
        lambda sid, root: (SimpleNamespace(agent=AGENT), None))
    monkeypatch.setattr(bai, "_agent_dir",
                        lambda root, name: tmp_root / name, raising=False)
    monkeypatch.setattr(bai, "_mark_binding_resolved",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_log_binding_miss_once",
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_last_resolved_agent",
                        lambda sid, root: AGENT, raising=False)

    payload = {"session_id": SID, "tool_name": "Bash",
               "tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        bai.main()
    except SystemExit:
        pass
    text = out.getvalue().strip()
    if not text:
        return ""
    return json.loads(text)["hookSpecificOutput"]["updatedInput"]["command"]


# --- 1. fresh checkpoint -> exported ---------------------------------------

def test_fresh_checkpoint_exports_goal_id(monkeypatch, tmp_path):
    _write_ckpt(tmp_path / AGENT / "session", "g-115-6003", _stamp(60))
    cmd = _run_hook(monkeypatch, tmp_path)
    assert "export MIND_GOAL_ID=g-115-6003; " in cmd, (
        "a fresh iteration-checkpoint must supply MIND_GOAL_ID -- this is the "
        "whole point of g-115-6003; without it product-pr-flow diary rows stay "
        f"goal_id=\"\". Got: {cmd!r}")


# --- 2. stale checkpoint -> NOT exported (the load-bearing gate) ------------

def test_stale_checkpoint_does_not_export(monkeypatch, tmp_path):
    # 5 days stale -- the exact shape measured on cc-08 2026-08-12, where the
    # agent-wide mirror named  selected 2026-08-07 while the live
    # per-session file named .
    _write_ckpt(tmp_path / AGENT / "session", "g-001-01", _stamp(5 * 86400))
    cmd = _run_hook(monkeypatch, tmp_path)
    assert "MIND_GOAL_ID" not in cmd, (
        "a STALE checkpoint must NOT supply a goal id. Under own-cloud the "
        "agent-wide checkpoint is a synced mirror of the reducer's box, so a "
        "non-forked session can read one days old. Misattributing work to the "
        "wrong goal is strictly worse than abstaining -- it is the failure this "
        f"variable exists to fix. Got: {cmd!r}")


def test_freshness_boundary_is_the_declared_constant(monkeypatch, tmp_path):
    """Just inside the window exports; just outside does not. Pins the gate to
    GOAL_ID_MAX_AGE_SEC rather than to whatever a future edit hardcodes."""
    ckdir = tmp_path / AGENT / "session"
    _write_ckpt(ckdir, "g-115-1", _stamp(bai.GOAL_ID_MAX_AGE_SEC - 120))
    assert "export MIND_GOAL_ID=g-115-1; " in _run_hook(monkeypatch, tmp_path)
    _write_ckpt(ckdir, "g-115-2", _stamp(bai.GOAL_ID_MAX_AGE_SEC + 120))
    assert "MIND_GOAL_ID" not in _run_hook(monkeypatch, tmp_path)


# --- 3. absent / malformed checkpoint -> fail-open --------------------------

def test_missing_checkpoint_is_silent(monkeypatch, tmp_path):
    (tmp_path / AGENT / "session").mkdir(parents=True, exist_ok=True)
    cmd = _run_hook(monkeypatch, tmp_path)
    assert cmd, "hook must still inject PATH/AGENT/SID with no checkpoint"
    assert "MIND_GOAL_ID" not in cmd
    assert f"export MIND_SID={SID};" in cmd, (
        "the goal-id block must never break the clauses that precede it")


def test_corrupt_checkpoint_is_silent(monkeypatch, tmp_path):
    ckdir = tmp_path / AGENT / "session"
    ckdir.mkdir(parents=True, exist_ok=True)
    (ckdir / "iteration-checkpoint.json").write_text("{not json", encoding="utf-8")
    cmd = _run_hook(monkeypatch, tmp_path)
    assert "MIND_GOAL_ID" not in cmd
    assert f"export MIND_SID={SID};" in cmd


def test_missing_selected_at_is_silent(monkeypatch, tmp_path):
    """No timestamp means the freshness gate cannot be evaluated -- abstain
    rather than export an id of unknown age."""
    ckdir = tmp_path / AGENT / "session"
    ckdir.mkdir(parents=True, exist_ok=True)
    (ckdir / "iteration-checkpoint.json").write_text(
        json.dumps({"goal_id": "g-115-6003"}), encoding="utf-8")
    cmd = _run_hook(monkeypatch, tmp_path)
    assert "MIND_GOAL_ID" not in cmd


# --- 4. shell-injection safety ---------------------------------------------

def test_goal_id_with_shell_metacharacters_is_refused(monkeypatch, tmp_path):
    """The value is interpolated UNQUOTED into the export clause, so a goal_id
    carrying shell syntax would execute. Every real id is [a-z0-9-]."""
    _write_ckpt(tmp_path / AGENT / "session", "g-1; rm -rf /tmp/x", _stamp(60))
    cmd = _run_hook(monkeypatch, tmp_path)
    assert "MIND_GOAL_ID" not in cmd, (
        f"a goal_id with shell metacharacters must be refused. Got: {cmd!r}")
    assert "rm -rf" not in cmd


# --- 5. per-session checkpoint wins for a forked Body ----------------------

def test_forked_body_prefers_per_session_checkpoint(monkeypatch, tmp_path):
    """Mirrors _paths.body_state_path: the per-session file wins when this Body
    forked a WM. Pins that the two resolvers cannot drift apart -- the agent-wide
    mirror is precisely the stale one on a worker box."""
    sess = tmp_path / AGENT / "sessions" / SID
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    _write_ckpt(sess, "g-115-6003", _stamp(60))
    # Agent-wide mirror is BOTH fresh and different -- so a pass here proves the
    # per-session file was preferred, not merely that the stale gate fired.
    _write_ckpt(tmp_path / AGENT / "session", "g-999-99", _stamp(60))

    cmd = _run_hook(monkeypatch, tmp_path)
    assert "export MIND_GOAL_ID=g-115-6003; " in cmd, (
        f"forked Body must read its own per-session checkpoint. Got: {cmd!r}")
    assert "g-999-99" not in cmd
    assert "export BODY_ROLE=worker; " in cmd, (
        "sanity: this fixture is the worker shape, so the body clause must fire "
        "too -- if it does not, the fork predicate never ran and contract 5 is "
        "vacuous")
