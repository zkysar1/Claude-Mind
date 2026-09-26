"""test_owncloud_push_on_write.py —  regression.

Pins the PostToolUse own-cloud single-file push shim
(core/scripts/owncloud-push-on-write.sh): backend gate, governed-root filter,
fail-open behavior. The shim exists because the sweep's no-baseline classifier
ASSUMES a real-time push hook records baselines for locally-authored writes —
without it, LLM world/meta edits are silently reverted by the S3-authoritative
reconcile (the g-115-1807 -> g-115-1923 incident).

Hermetic: OWNCLOUD_PUSH_HOOK_DRYRUN=1 stops before any backend construction;
OWNCLOUD_PUSHHOOK-free env plus MIND_WORLD/MIND_META tmp overrides steer
_paths.sh's governed roots; OWNCLOUD_PUSH_HOOK_ENV_LOCAL points the backend
probe at a tmp .env.local. No S3, no creds, no daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

SHIM = CORE_SCRIPTS / "owncloud-push-on-write.sh"


def _run_shim(tmp_path, *, backend, file_path, world=None, meta=None,
              stdin_text=None, dryrun=True):
    """Invoke the shim as the hook harness would: JSON on stdin, env-steered."""
    env_local = tmp_path / "env.local"
    env_local.write_text(f"STORAGE_BACKEND={backend}\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("STORAGE_BACKEND", None)
    env["OWNCLOUD_PUSH_HOOK_ENV_LOCAL"] = str(env_local)
    if dryrun:
        env["OWNCLOUD_PUSH_HOOK_DRYRUN"] = "1"
    if world is not None:
        env["MIND_WORLD"] = str(world)
    if meta is not None:
        env["MIND_META"] = str(meta)
    payload = stdin_text if stdin_text is not None else json.dumps(
        {"tool_input": {"file_path": file_path}, "session_id": "test-sid"})
    return subprocess.run(
        [BASH, str(SHIM)], input=payload, capture_output=True, text=True,
        env=env, timeout=60, cwd=str(PROJECT_ROOT))


def test_local_backend_fast_exits(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    target = world / "conventions" / "x.md"
    r = _run_shim(tmp_path, backend="local", file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" not in r.stdout


def test_world_file_would_push(tmp_path):
    world = tmp_path / "world"
    (world / "scripts").mkdir(parents=True)
    target = world / "scripts" / "stale-jobs-scan.py"
    target.write_text("# x\n", encoding="utf-8")
    r = _run_shim(tmp_path, backend="own-cloud", file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" in r.stdout, (
        f"world-file edit must be push-eligible; stdout={r.stdout!r} "
        f"stderr={r.stderr!r}")
    assert "stale-jobs-scan.py" in r.stdout


def test_meta_file_would_push(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    meta = tmp_path / "meta"
    meta.mkdir()
    target = meta / "goal-selection-strategy.yaml"
    target.write_text("x: 1\n", encoding="utf-8")
    r = _run_shim(tmp_path, backend="own-cloud", file_path=str(target),
                  world=world, meta=meta)
    assert r.returncode == 0
    assert "would push" in r.stdout


def test_non_governed_repo_file_skipped(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    target = PROJECT_ROOT / "core" / "scripts" / "retrieve.py"
    r = _run_shim(tmp_path, backend="own-cloud", file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" not in r.stdout, (
        "git-synced repo files must never route to S3 push")


def test_windows_backslash_path_form_governed(tmp_path):
    """The Edit tool hands hooks OS-native paths; backslashes must still match
    the governed root."""
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    target = world / "board" / "general.jsonl"
    target.write_text("", encoding="utf-8")
    native = str(target).replace("/", "\\") if os.name == "nt" else str(target)
    r = _run_shim(tmp_path, backend="own-cloud", file_path=native, world=world)
    assert r.returncode == 0
    assert "would push" in r.stdout


def test_missing_file_path_silent(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    r = _run_shim(tmp_path, backend="own-cloud", file_path="", world=world,
                  stdin_text=json.dumps({"tool_input": {}}))
    assert r.returncode == 0
    assert "would push" not in r.stdout


def test_malformed_stdin_fail_open(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    r = _run_shim(tmp_path, backend="own-cloud", file_path="",
                  world=world, stdin_text="{not json")
    assert r.returncode == 0
    assert "would push" not in r.stdout


def test_missing_env_local_fast_exits(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    target = world / "x.md"
    env = dict(os.environ)
    env["OWNCLOUD_PUSH_HOOK_ENV_LOCAL"] = str(tmp_path / "does-not-exist")
    env["OWNCLOUD_PUSH_HOOK_DRYRUN"] = "1"
    env["MIND_WORLD"] = str(world)
    r = subprocess.run(
        [BASH, str(SHIM)],
        input=json.dumps({"tool_input": {"file_path": str(target)}}),
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(PROJECT_ROOT))
    assert r.returncode == 0
    assert "would push" not in r.stdout


# --- : environment-registry backend gate ----------------------------
# Env-config deployments set ONLY ENVIRONMENT_ID in .env.local; the daemon
# derives STORAGE_BACKEND from core/config/environments/<id>.yaml. The shim's
# old gate grepped .env.local alone, so it silently fast-exited on every such
# box (proven cc-02 2026-07-16) — every governed write waited for the ~120s
# sweep, breeding both-moved conflict freezes. These tests pin the fallback
# chain against the COMMITTED registry files (deterministic, no S3/creds).


def _run_shim_env_id(tmp_path, *, env_lines, file_path, world):
    """Like _run_shim but with arbitrary .env.local content (no STORAGE_BACKEND)."""
    env_local = tmp_path / "env.local"
    env_local.write_text(env_lines, encoding="utf-8")
    env = dict(os.environ)
    env.pop("STORAGE_BACKEND", None)
    env["OWNCLOUD_PUSH_HOOK_ENV_LOCAL"] = str(env_local)
    env["OWNCLOUD_PUSH_HOOK_DRYRUN"] = "1"
    env["MIND_WORLD"] = str(world)
    payload = json.dumps({"tool_input": {"file_path": file_path}})
    return subprocess.run(
        [BASH, str(SHIM)], input=payload, capture_output=True, text=True,
        env=env, timeout=60, cwd=str(PROJECT_ROOT))


def test_environment_id_owncloud_registry_gates_open(tmp_path):
    """ENVIRONMENT_ID → registry backend: own-cloud → push-eligible."""
    world = tmp_path / "world"
    world.mkdir()
    target = world / "x.md"
    target.write_text("x\n", encoding="utf-8")
    r = _run_shim_env_id(
        tmp_path, env_lines="ENVIRONMENT_ID=ayoai-mind\n",
        file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" in r.stdout, (
        f"env-config own-cloud deployment must be push-eligible; "
        f"stdout={r.stdout!r} stderr={r.stderr!r}")


def test_environment_id_local_registry_fast_exits(tmp_path):
    """ENVIRONMENT_ID → registry backend: local → fast-exit."""
    world = tmp_path / "world"
    world.mkdir()
    target = world / "x.md"
    target.write_text("x\n", encoding="utf-8")
    r = _run_shim_env_id(
        tmp_path, env_lines="ENVIRONMENT_ID=local\n",
        file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" not in r.stdout


def test_explicit_backend_wins_over_environment_id(tmp_path):
    """Legacy explicit STORAGE_BACKEND=local beats an own-cloud registry entry
    (setdefault precedence, mirroring _apply_environment_registry)."""
    world = tmp_path / "world"
    world.mkdir()
    target = world / "x.md"
    target.write_text("x\n", encoding="utf-8")
    r = _run_shim_env_id(
        tmp_path,
        env_lines="STORAGE_BACKEND=local\nENVIRONMENT_ID=ayoai-mind\n",
        file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" not in r.stdout


def test_unknown_environment_id_fast_exits(tmp_path):
    """ENVIRONMENT_ID with no matching registry file → no backend → fast-exit
    (fail-open, guard-141: never block the edit on a config gap)."""
    world = tmp_path / "world"
    world.mkdir()
    target = world / "x.md"
    target.write_text("x\n", encoding="utf-8")
    r = _run_shim_env_id(
        tmp_path, env_lines="ENVIRONMENT_ID=no-such-env-zzz\n",
        file_path=str(target), world=world)
    assert r.returncode == 0
    assert "would push" not in r.stdout


# --- : PostToolUse wiring invariant ----------------------------------
# Restores the coverage the  merge dropped when it retired
# test_sync_governed_write.py. Distinct from the shim-behavior tests above
# (which exercise the script at runtime); this reads .claude/settings.json and
# pins the WIRING. It pins PRESENCE, not POSITION:  measured that
# Claude Code starts every matching hook of one event in parallel, so a chain's
# order in settings.json sequences nothing.
# NB: the module-level PROJECT_ROOT above is actually core/ (CORE_SCRIPTS.parent),
# not the repo root — the shim tests only use it as a subprocess cwd, where the
# shim re-resolves the real root via _paths.sh. .claude/settings.json lives at
# the TRUE repo root, so derive it explicitly here.
REPO_ROOT = CORE_SCRIPTS.parent.parent
SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"
PUSH_HOOK_BASENAME = "owncloud-push-on-write.sh"
GOVERNED_WRITE_MATCHERS = ("Write", "Edit", "MultiEdit")


def _post_tool_use_groups():
    """matcher -> hooks[] map from .claude/settings.json PostToolUse."""
    settings = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    groups = settings.get("hooks", {}).get("PostToolUse", [])
    return {g.get("matcher"): g.get("hooks", []) for g in groups}


def test_push_on_write_wired_in_all_governed_write_chains():
    """owncloud-push-on-write.sh MUST be wired, exactly once, into each
    PostToolUse Write/Edit/MultiEdit chain. Without it a tool write under a
    governed root stays local-only and the sweep's no-baseline reconcile can
    pull stale S3 back over it (the g-115-1807 -> g-115-1923 incident this
    shim closes). A second registration adds a concurrent duplicate push and no
    protection.

    POSITION IS DELIBERATELY NOT ASSERTED (g-306-487). This test used to require
    the push hook to be LAST so it would push the final post-hook file state.
    It could never enforce that: Claude Code starts all of an event's matching
    hooks in parallel. On cc-04 with claude 2.1.280, 8 hooks started within
    12.5 ms. The last-listed hook read the file about 1 s before the
    2nd-listed writer wrote it, in 3 of 3 Edits. Across 367 production pushes,
    the push hook started within 5 ms of the chain's first hook. So
    tree-sync-check.sh (Layer A) can rewrite a tree node after the push has read
    it, whatever the order in settings.json. What limits that race lives in the
    sync layer, not in this file: the baseline stamped at push time lets the
    next sweep push the later local write. The exception, where _put writes the
    pushed bytes back over a Layer A write that lands mid-push, is recorded on
    g-306-487."""
    groups = _post_tool_use_groups()
    for matcher in GOVERNED_WRITE_MATCHERS:
        assert matcher in groups, (
            f"PostToolUse[{matcher}] chain missing from settings.json")
        hooks = groups[matcher]
        assert hooks, f"PostToolUse[{matcher}] has no hooks"
        occurrences = sum(
            1 for h in hooks if PUSH_HOOK_BASENAME in h.get("command", ""))
        assert occurrences == 1, (
            f"PostToolUse[{matcher}]: {PUSH_HOOK_BASENAME} must be wired exactly "
            f"once (found {occurrences})")
