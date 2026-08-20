"""End-to-end wrapper tests for reasoning-bank-{add,update-field,increment}.sh
and guardrails-{add,update-field,increment}.sh through a running daemon
(H2 Wave 2).

Full shell-wrapper -> daemon -> file cycle. Each test calls the wrapper
via subprocess with env pointed at the test daemon (isolated tmp dirs —
never the live world's stores). Mirrors test_wrapper_journal.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rb_path(project_root: Path) -> Path:
    return project_root / "world" / "reasoning-bank.jsonl"


def _guard_path(project_root: Path) -> Path:
    return project_root / "world" / "guardrails.jsonl"


def _run_wrapper(project_root: Path, script_name: str, args: list,
                 *, stdin_data: str = ""):
    script = REPO_ROOT / "core" / "scripts" / script_name
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    env["MIND_RUNTIME_DISABLE_SPAWN"] = "1"
    return subprocess.run(
        [_bash(), script.as_posix()] + args,
        capture_output=True, text=True, timeout=15,
        input=stdin_data or None, env=env,
    )


# Valid rb record (RB_VALID_TYPES: success, failure, user_provided).
def _rb_rec(**kw) -> dict:
    base = {
        "title": "Wrapper test RB",
        "type": "success",
        "category": "test-cat",
        "content": "A wrapper test rb entry.",
        "applies_to": "framework",
        "tags": ["wave2"],
    }
    base.update(kw)
    return base


def _guard_rec(**kw) -> dict:
    base = {
        "rule": "wrapper guard test",
        "category": "test-guard",
        "trigger_condition": "before deploy",
        "source": "wave-2-wrapper-test",
        "when_to_use": "always",
        "tags": ["wave2"],
    }
    base.update(kw)
    return base


# ===========================================================================
# reasoning-bank-add.sh
# ===========================================================================

def test_wrapper_rb_add_creates_record(running_daemon):
    project_root, _ = running_daemon
    live = _rb_path(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "reasoning-bank-add.sh", [],
                     stdin_data=json.dumps(_rb_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    out = json.loads(r.stdout)
    assert out["title"] == "Wrapper test RB"


def test_wrapper_rb_add_auto_id(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "reasoning-bank-add.sh", [],
                     stdin_data=json.dumps(_rb_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    # next_id_for_prefix uses unpadded numeric IDs: seeds 001..003 -> 4
    assert out["id"] == "rb-4"


def test_wrapper_rb_add_schema_flag_refused(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "reasoning-bank-add.sh", ["--schema"])
    assert r.returncode == 1
    assert "no longer available" in r.stderr


def test_wrapper_rb_add_duplicate_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "reasoning-bank-add.sh", [],
                     stdin_data=json.dumps(_rb_rec(id="rb-001")))
    assert r.returncode != 0


# ===========================================================================
# reasoning-bank-update-field.sh
# ===========================================================================

def test_wrapper_rb_update_field(running_daemon):
    # Seeds have legacy type=insight — add a valid record, then update it.
    project_root, _ = running_daemon
    live = _rb_path(project_root)
    _run_wrapper(project_root, "reasoning-bank-add.sh", [],
                 stdin_data=json.dumps(_rb_rec(id="rb-100")))
    r = _run_wrapper(project_root, "reasoning-bank-update-field.sh",
                     ["rb-100", "status", "retired"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "rb-100")
    assert on_disk["status"] == "retired"


def test_wrapper_rb_update_field_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "reasoning-bank-update-field.sh", ["rb-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


# ===========================================================================
# reasoning-bank-increment.sh
# ===========================================================================

def test_wrapper_rb_increment(running_daemon, monkeypatch):
    # LANE PIN (2026-08-20): this test asserts the LEGACY embedded-counter RMW.
    # Post  the session env of any flipped box carries
    # UTILIZATION_COUNTERS_SPOOLED=1, which leaks into the in-process test
    # daemon and routes the increment to the spool — freezing the embedded
    # counter and failing this test only on flipped boxes (env-dependence,
    # not portability). Pin the legacy lane explicitly; the spool lane has
    # its own tests (test_utilization_spool.py + *_spooled_surface twins).
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    project_root, _ = running_daemon
    live = _rb_path(project_root)
    # rb-002 has times_helpful=3
    r = _run_wrapper(project_root, "reasoning-bank-increment.sh",
                     ["rb-002", "utilization.times_helpful"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "rb-002")
    assert on_disk["utilization"]["times_helpful"] == 4


def test_wrapper_rb_increment_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "reasoning-bank-increment.sh", ["rb-002"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


def test_wrapper_rb_increment_spooled_surface(running_daemon, monkeypatch):
    """f40656f72 (): on the spool lane the wrapper must SAY so.

    The spool path returns a bare-id record BY DESIGN (it never reads the
    content store), so before the `spooled`/`where` surface existed a working
    spooled write printed byte-identically to a no-op — two agents re-read the
    content store, found the embedded counter frozen (also by design,
    g-358-05), and filed a false HIGH data-loss goal. This pins the reporting
    hop; the daemon-side spool behavior is pinned in test_utilization_spool.py.
    The running_daemon fixture is an in-process thread, so monkeypatching this
    process's env is what its spooled_enabled() reads.
    """
    project_root, _ = running_daemon
    monkeypatch.setenv("UTILIZATION_COUNTERS_SPOOLED", "1")
    live = _rb_path(project_root)
    before = next(x for x in _read_jsonl(live) if x["id"] == "rb-002")
    before_count = before["utilization"]["times_helpful"]
    r = _run_wrapper(project_root, "reasoning-bank-increment.sh",
                     ["rb-002", "utilization.times_helpful"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    assert out.get("spooled") is True, (
        "spool-lane increment did not surface `spooled` — the ambiguity that "
        f"produced the g-115-6850 false alarm is back. stdout: {r.stdout[:200]}")
    assert "SIDECAR" in (out.get("where") or "")
    # The content store must be untouched on this lane — that is the design
    # the reporting surface exists to explain.
    after = next(x for x in _read_jsonl(live) if x["id"] == "rb-002")
    assert after["utilization"]["times_helpful"] == before_count


# ===========================================================================
# guardrails-add.sh
# ===========================================================================

def test_wrapper_guard_add_creates_record(running_daemon):
    project_root, _ = running_daemon
    live = _guard_path(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "guardrails-add.sh", [],
                     stdin_data=json.dumps(_guard_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    out = json.loads(r.stdout)
    assert out["rule"] == "wrapper guard test"


def test_wrapper_guard_add_auto_id(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "guardrails-add.sh", [],
                     stdin_data=json.dumps(_guard_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    # next_id_for_prefix: seeds 001,002,099 -> max is 99 -> 100
    assert out["id"] == "guard-100"


def test_wrapper_guard_add_schema_flag_refused(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "guardrails-add.sh", ["--schema"])
    assert r.returncode == 1
    assert "no longer available" in r.stderr


def test_wrapper_guard_add_duplicate_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "guardrails-add.sh", [],
                     stdin_data=json.dumps(_guard_rec(id="guard-001")))
    assert r.returncode != 0


# ===========================================================================
# guardrails-update-field.sh
# ===========================================================================

def test_wrapper_guard_update_field(running_daemon):
    # Seeds lack trigger_condition/source — add a valid record, then update.
    project_root, _ = running_daemon
    live = _guard_path(project_root)
    _run_wrapper(project_root, "guardrails-add.sh", [],
                 stdin_data=json.dumps(_guard_rec(id="guard-300")))
    r = _run_wrapper(project_root, "guardrails-update-field.sh",
                     ["guard-300", "status", "retired"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "guard-300")
    assert on_disk["status"] == "retired"


def test_wrapper_guard_update_field_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "guardrails-update-field.sh",
                     ["guard-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


# ===========================================================================
# guardrails-increment.sh
# ===========================================================================

def test_wrapper_guard_increment(running_daemon, monkeypatch):
    # Seeds lack trigger_condition/source — add a valid record, then increment.
    # LANE PIN (2026-08-20): this test asserts the LEGACY embedded-counter RMW.
    # Post  the session env of any flipped box carries
    # UTILIZATION_COUNTERS_SPOOLED=1, which leaks into the in-process test
    # daemon and routes the increment to the spool — freezing the embedded
    # counter and failing this test only on flipped boxes (env-dependence,
    # not portability). Pin the legacy lane explicitly; the spool lane has
    # its own tests (test_utilization_spool.py + *_spooled_surface twins).
    monkeypatch.delenv("UTILIZATION_COUNTERS_SPOOLED", raising=False)
    project_root, _ = running_daemon
    live = _guard_path(project_root)
    _run_wrapper(project_root, "guardrails-add.sh", [],
                 stdin_data=json.dumps(_guard_rec(id="guard-400")))
    r = _run_wrapper(project_root, "guardrails-increment.sh",
                     ["guard-400", "utilization.times_helpful"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "guard-400")
    assert on_disk["utilization"]["times_helpful"] == 1


def test_wrapper_guard_increment_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "guardrails-increment.sh",
                     ["guard-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


def test_wrapper_guard_increment_spooled_surface(running_daemon, monkeypatch):
    """Guardrails twin of test_wrapper_rb_increment_spooled_surface — both
    wrappers carry the f40656f72 reporting surface; pin both or a partial
    revert passes."""
    project_root, _ = running_daemon
    _run_wrapper(project_root, "guardrails-add.sh", [],
                 stdin_data=json.dumps(_guard_rec(id="guard-401")))
    monkeypatch.setenv("UTILIZATION_COUNTERS_SPOOLED", "1")
    live = _guard_path(project_root)
    before = next(x for x in _read_jsonl(live) if x["id"] == "guard-401")
    before_count = before["utilization"]["times_helpful"]
    r = _run_wrapper(project_root, "guardrails-increment.sh",
                     ["guard-401", "utilization.times_helpful"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    assert out.get("spooled") is True, f"stdout: {r.stdout[:200]}"
    assert "SIDECAR" in (out.get("where") or "")
    after = next(x for x in _read_jsonl(live) if x["id"] == "guard-401")
    assert after["utilization"]["times_helpful"] == before_count
