"""test_recurring_precondition_sweep_env_propagation.py — 2 regression.

recurring-precondition-sweep.py is invoked as a direct `py` call from
aspirations-precheck Phase 0.5c (no _paths.sh preamble). Its write path spawns
`aspirations.py update-goal` as a subprocess; that child resolves the own-cloud
governed-root map from MIND_WORLD/MIND_META (or the *_PATH fallbacks) in its
OWN env only (OwnCloudBackend._resolve_root_map reads env, never
WORLD_DIR/META_DIR). On env-only own-cloud hosts the inherited env lacks those
vars, so the write aborted BEFORE the lock ("cannot map a governed path to a
root") — advanced=0 skipped_on_error=1, silently, every iteration a goal
actually needed advancing. Latent on ALL env-only own-cloud hosts (the read
side works via _paths.py's .mind-data/local-paths.conf fallback, masking the
bug until a goal needs the write).

Fix: _advance_last_achieved_at augments the subprocess env with
MIND_WORLD/WORLD_PATH/MIND_META/META_PATH resolved from
_paths.WORLD_DIR/META_DIR (the read side already resolved them), letting an
already-set MIND_* win (guard-879 / guard-652) and skipping a None-able root
(guard-551).

These tests monkeypatch subprocess.run to capture the env kwarg — no real
write, hermetic (all four governed-root vars are stripped so the fix, not the
ambient environment, is under test).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
SWEEP_PY = CORE_SCRIPTS / "recurring-precondition-sweep.py"

sys.path.insert(0, str(CORE_SCRIPTS))

_GOVERNED_VARS = ("MIND_WORLD", "WORLD_PATH", "MIND_META", "META_PATH")


def _load_sweep_module():
    """Import the hyphenated sweep script under a clean module name."""
    spec = importlib.util.spec_from_file_location("_rps_sweep_under_test", SWEEP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture_run(monkeypatch, mod):
    """Patch mod.subprocess.run to record the env kwarg; return the capture dict."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeCompleted()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return captured


def test_governed_root_map_propagated_to_write_subprocess(monkeypatch):
    """os.environ lacks the governed-root vars → the write subprocess env still
    carries all four aliases resolved from _paths.WORLD_DIR/META_DIR."""
    mod = _load_sweep_module()
    captured = _capture_run(monkeypatch, mod)
    for k in _GOVERNED_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mod._paths, "WORLD_DIR", Path("/fake/world"))
    monkeypatch.setattr(mod._paths, "META_DIR", Path("/fake/meta"))

    ok = mod._advance_last_achieved_at("g-x", "world", "2026-07-08T00:00:00", dry_run=False)

    assert ok is True
    env = captured["env"]
    assert env is not None, "subprocess.run was not passed an explicit env"
    assert env["MIND_WORLD"] == "/fake/world"
    assert env["WORLD_PATH"] == "/fake/world"
    assert env["MIND_META"] == "/fake/meta"
    assert env["META_PATH"] == "/fake/meta"


def test_already_set_ayoai_world_wins(monkeypatch):
    """guard-879 / guard-652: an already-set MIND_* is not overwritten; the
    unset *_PATH alias is still filled from _paths."""
    mod = _load_sweep_module()
    captured = _capture_run(monkeypatch, mod)
    for k in _GOVERNED_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MIND_WORLD", "/preset/world")
    monkeypatch.setattr(mod._paths, "WORLD_DIR", Path("/fake/world"))
    monkeypatch.setattr(mod._paths, "META_DIR", None)  # None-able (guard-551)

    mod._advance_last_achieved_at("g-x", "world", "2026-07-08T00:00:00", dry_run=False)

    env = captured["env"]
    assert env["MIND_WORLD"] == "/preset/world"  # preset wins
    assert env["WORLD_PATH"] == "/fake/world"     # unset alias filled from _paths
    assert "MIND_META" not in env                # None META_DIR → not set
    assert "META_PATH" not in env


def test_empty_string_env_treated_as_unset(monkeypatch):
    """from_env's `or` falsy-check treats "" as unset (guard-879 idiom
    ${MIND_WORLD:-$WORLD_DIR}); an ambient EMPTY-STRING alias must be filled
    from _paths, not left empty (a plain setdefault would wrongly keep "")."""
    mod = _load_sweep_module()
    captured = _capture_run(monkeypatch, mod)
    monkeypatch.setenv("MIND_WORLD", "")  # ambient empty string, NOT unset
    monkeypatch.setenv("WORLD_PATH", "")
    monkeypatch.delenv("MIND_META", raising=False)
    monkeypatch.delenv("META_PATH", raising=False)
    monkeypatch.setattr(mod._paths, "WORLD_DIR", Path("/fake/world"))
    monkeypatch.setattr(mod._paths, "META_DIR", Path("/fake/meta"))

    mod._advance_last_achieved_at("g-x", "world", "2026-07-08T00:00:00", dry_run=False)

    env = captured["env"]
    assert env["MIND_WORLD"] == "/fake/world"  # empty filled, not left ""
    assert env["WORLD_PATH"] == "/fake/world"
    assert env["MIND_META"] == "/fake/meta"
    assert env["META_PATH"] == "/fake/meta"


def test_none_world_dir_skips_gracefully(monkeypatch):
    """guard-551: a None WORLD_DIR must not crash — the vars are simply absent
    (a genuinely unconfigured box, distinct from the env-propagation bug)."""
    mod = _load_sweep_module()
    captured = _capture_run(monkeypatch, mod)
    for k in _GOVERNED_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mod._paths, "WORLD_DIR", None)
    monkeypatch.setattr(mod._paths, "META_DIR", None)

    ok = mod._advance_last_achieved_at("g-x", "world", "2026-07-08T00:00:00", dry_run=False)

    assert ok is True  # no crash
    env = captured["env"]
    for k in _GOVERNED_VARS:
        assert k not in env


def test_dry_run_skips_subprocess(monkeypatch):
    """dry_run short-circuits before any subprocess spawn (env work never runs)."""
    mod = _load_sweep_module()
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return _FakeCompleted()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok = mod._advance_last_achieved_at("g-x", "world", "2026-07-08T00:00:00", dry_run=True)

    assert ok is True
    assert calls["n"] == 0
