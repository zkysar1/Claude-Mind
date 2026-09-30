"""presence-tick.py ticks a WORKER Body's liveness after EVERY tool call — .

bash-agent-inject.py ticks a Body's carrier only before Bash calls (g-115-8200).
A slow worker Body can spend hours in Read/Edit/Grep with no Bash call, its
carrier ages past the sweep's window, and the stranded-claim sweep releases its
live claim mid-work (zc-05, 2026-09-29 04:42). The PostToolUse '*' hook now makes
the same decision through _shared_tick.maybe_tick, on the same per-SID stamp, so
the two hooks together tick a Body at most once per interval.

WHAT THESE TESTS PIN (through presence-tick main(), spawn captured):
  1. a Read/Edit/Grep stretch ticks a worker Body `--body-only` once per
     interval, tagged via=post-tool, and ticks again once the interval passes.
  2. the Bash hook and this hook share ONE window, in both orders.
  3. the reducer (SID == running-session-id) is never ticked from here: this
     process runs with STORAGE_BACKEND=local, which the full tick was not built
     for, and the reducer keeps its diary and Bash cadences.
  4. a session with no carrier never ticks.
  5. under pytest WITHOUT the opt-in the spawn is refused (g-115-5310).
  6. a failing tick never costs the presence record.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))


def _paths():
    """The _paths module main() will import NOW. Other tests in the same process
    reload it, so an object captured at import time can be stale, and patching it
    leaves main() on the LIVE paths: the first scoped run did exactly that and
    wrote 9 records into the box's real presence log."""
    return importlib.import_module("_paths")


def _tick():
    """The _shared_tick module both hooks will import NOW (same reason as _paths)."""
    return importlib.import_module("_shared_tick")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load("presence_tick_hb", "presence-tick.py")
bai = _load("bash_agent_inject_hb", "bash-agent-inject.py")

AGENT = "alpha"
WORKER = "sid-post-tool-worker-0001"
REDUCER = "sid-post-tool-reducer-0002"


def _root(tmp_path: Path, monkeypatch, *, carrier: bool = True, running: str = REDUCER) -> Path:
    """A staged project root with WORKER bound to AGENT, and _paths pointed at it."""
    root = tmp_path / "repo"
    adir = root / "agents" / AGENT
    sess = adir / "session"
    sess.mkdir(parents=True)
    (adir / "sessions" / WORKER).mkdir(parents=True)
    (adir / "sessions" / WORKER / "binding.yaml").write_text(f"agent: {AGENT}\n", encoding="utf-8")
    if carrier:
        (sess / f"body-heartbeat-{WORKER}.json").write_text(json.dumps({"sid": WORKER}), encoding="utf-8")
    (sess / "running-session-id").write_text(running, encoding="utf-8")
    (root / "world").mkdir()
    paths = _paths()
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setattr(paths, "WORLD_DIR", root / "world")
    monkeypatch.setattr(paths, "AGENT_DIR", adir)
    # The agent must come from the staged binding, never from the env fallback
    # that conftest populates with a live agent name.
    monkeypatch.delenv("MIND_AGENT", raising=False)
    return root


def _capture(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(_tick(), "spawn_detached", lambda *a, **k: calls.append((a, k)))
    # The sanctioned opt-in: the spawn is captured, nothing reaches a daemon.
    monkeypatch.setenv("MIND_DIARY_SHARED_TICK_TEST", "1")
    return calls


def _post_tool(monkeypatch, tool: str, sid: str = WORKER) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": tool, "session_id": sid})))
    return pt.main()


def _stamp(root: Path, sid: str = WORKER) -> Path:
    return root / "core" / "logs" / "heartbeat-hook" / sid


def _backdate(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def _presence_records(root: Path) -> int:
    p = root / "world" / "presence" / f"{AGENT}.jsonl"
    return len(p.read_text(encoding="utf-8").splitlines()) if p.exists() else 0


def test_a_read_edit_grep_stretch_ticks_a_worker_body_once_per_interval(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root = _root(tmp_path, monkeypatch)
    for tool in ("Read", "Edit", "Grep"):
        assert _post_tool(monkeypatch, tool) == 0
    assert len(calls) == 1, f"a no-Bash stretch must tick once per interval, got {calls!r}"
    args, kwargs = calls[0]
    assert args[1] == AGENT and args[2] == WORKER
    assert kwargs["body_only"] is True and kwargs["via"] == "post-tool"
    assert _stamp(root).exists(), "the per-SID stamp the Bash hook also reads was not written"
    _backdate(_stamp(root), _tick().SHARED_HEARTBEAT_INTERVAL_S + 5)
    assert _post_tool(monkeypatch, "Edit") == 0
    assert len(calls) == 2, "once the interval has passed, the next Edit must tick again"
    assert _presence_records(root) == 4, "the presence record must still be written on every call"


def test_the_bash_hook_and_the_post_tool_hook_share_one_window(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root = _root(tmp_path, monkeypatch)
    bai._maybe_tick_heartbeat(AGENT, WORKER, root)
    assert len(calls) == 1 and calls[0][1]["body_only"] is True
    _post_tool(monkeypatch, "Read")
    assert len(calls) == 1, "a Bash-hook tick must hold off the post-tool tick for the interval"
    _backdate(_stamp(root), _tick().SHARED_HEARTBEAT_INTERVAL_S + 5)
    _post_tool(monkeypatch, "Read")
    assert len(calls) == 2
    bai._maybe_tick_heartbeat(AGENT, WORKER, root)
    assert len(calls) == 2, "a post-tool tick must hold off the Bash-hook tick for the interval"


def test_the_reducer_is_left_to_its_own_cadences(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root = _root(tmp_path, monkeypatch, running=WORKER)
    _post_tool(monkeypatch, "Read")
    assert calls == [], "the post-tool hook must never run the reducer's full tick"
    assert not (root / "agents" / AGENT / "session" / "claim-renewal-last").exists()


def test_a_session_without_a_carrier_never_ticks(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root = _root(tmp_path, monkeypatch, carrier=False)
    _post_tool(monkeypatch, "Read")
    assert calls == []
    assert not (root / "agents" / AGENT / "session" / f"body-heartbeat-{WORKER}.json").exists(), (
        "the hook must never CREATE a carrier: /start and the tick own that")
    assert _presence_records(root) == 1


def test_pytest_without_opt_in_refuses_the_spawn(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_tick(), "spawn_detached", lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("MIND_DIARY_SHARED_TICK_TEST", raising=False)
    _root(tmp_path, monkeypatch)
    _post_tool(monkeypatch, "Read")
    assert calls == [], "under pytest without the opt-in the tick must be refused (g-115-5310)"


def test_a_failing_tick_never_costs_the_presence_record(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tick failed")

    monkeypatch.setattr(_tick(), "maybe_tick", boom)
    root = _root(tmp_path, monkeypatch)
    assert _post_tool(monkeypatch, "Edit") == 0
    assert _presence_records(root) == 1
