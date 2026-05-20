"""End-to-end wrapper tests for pattern-signatures-{add,update,update-field,
set-status}.sh and spark-questions-{add,update-field,retire}.sh through a
running daemon (H2 Wave 3).

Full shell-wrapper -> daemon -> file cycle. Each test calls the wrapper
via subprocess with env pointed at the test daemon (isolated tmp dirs --
never the live world/meta stores). Mirrors test_wrapper_rbguard.py.
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


def _patsig_path(project_root: Path) -> Path:
    return project_root / "world" / "pattern-signatures.jsonl"


def _spark_path(project_root: Path) -> Path:
    return project_root / "meta" / "spark-questions.jsonl"


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


# ---------------------------------------------------------------------------
# Minimal valid records
# ---------------------------------------------------------------------------

def _patsig_rec(**kw) -> dict:
    base = {
        "name": "wrapper test pattern",
        "description": "a wrapper test pattern signature",
        "conditions": ["cond-a"],
        "expected_outcome": "outcome-z",
    }
    base.update(kw)
    return base


def _spark_question_rec(**kw) -> dict:
    base = {
        "type": "question",
        "text": "Wrapper test question?",
        "category": "surprise",
    }
    base.update(kw)
    return base


def _spark_candidate_rec(**kw) -> dict:
    base = {
        "type": "candidate",
        "text": "Wrapper candidate question",
        "category": "learning",
    }
    base.update(kw)
    return base


# ===========================================================================
# pattern-signatures-add.sh
# ===========================================================================

def test_wrapper_patsig_add_creates_record(running_daemon):
    project_root, _ = running_daemon
    live = _patsig_path(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                     stdin_data=json.dumps(_patsig_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    out = json.loads(r.stdout)
    assert out["name"] == "wrapper test pattern"


def test_wrapper_patsig_add_auto_id(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                     stdin_data=json.dumps(_patsig_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    # next_id_for_prefix: seeds sig-001, sig-002 -> sig-3
    assert out["id"] == "sig-3"


def test_wrapper_patsig_add_schema_flag_refused(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "pattern-signatures-add.sh", ["--schema"])
    assert r.returncode == 1
    assert "no longer available" in r.stderr


def test_wrapper_patsig_add_duplicate_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                     stdin_data=json.dumps(_patsig_rec(id="sig-001")))
    assert r.returncode != 0


# ===========================================================================
# pattern-signatures-update.sh (replace)
# ===========================================================================

def test_wrapper_patsig_update_replaces_record(running_daemon):
    project_root, _ = running_daemon
    live = _patsig_path(project_root)
    # Add a valid record first.
    _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                 stdin_data=json.dumps(_patsig_rec(id="sig-100")))

    updated = _patsig_rec(id="sig-100", name="updated pattern",
                          description="updated description")
    r = _run_wrapper(project_root, "pattern-signatures-update.sh",
                     ["sig-100"], stdin_data=json.dumps(updated))
    assert r.returncode == 0, f"stderr: {r.stderr}"

    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "sig-100")
    assert on_disk["name"] == "updated pattern"


# ===========================================================================
# pattern-signatures-update-field.sh
# ===========================================================================

def test_wrapper_patsig_update_field(running_daemon):
    project_root, _ = running_daemon
    live = _patsig_path(project_root)
    _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                 stdin_data=json.dumps(_patsig_rec(id="sig-101")))
    r = _run_wrapper(project_root, "pattern-signatures-update-field.sh",
                     ["sig-101", "status", "retired"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "sig-101")
    assert on_disk["status"] == "retired"


def test_wrapper_patsig_update_field_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "pattern-signatures-update-field.sh",
                     ["sig-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


# ===========================================================================
# pattern-signatures-set-status.sh
# ===========================================================================

def test_wrapper_patsig_set_status(running_daemon):
    project_root, _ = running_daemon
    live = _patsig_path(project_root)
    _run_wrapper(project_root, "pattern-signatures-add.sh", [],
                 stdin_data=json.dumps(_patsig_rec(id="sig-102")))
    r = _run_wrapper(project_root, "pattern-signatures-set-status.sh",
                     ["sig-102", "contradicted"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "sig-102")
    assert on_disk["status"] == "contradicted"


def test_wrapper_patsig_set_status_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "pattern-signatures-set-status.sh",
                     ["sig-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


# ===========================================================================
# spark-questions-add.sh
# ===========================================================================

def test_wrapper_spark_add_question(running_daemon):
    project_root, _ = running_daemon
    live = _spark_path(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "spark-questions-add.sh", [],
                     stdin_data=json.dumps(_spark_question_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    out = json.loads(r.stdout)
    assert out["type"] == "question"


def test_wrapper_spark_add_candidate(running_daemon):
    project_root, _ = running_daemon
    live = _spark_path(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "spark-questions-add.sh", [],
                     stdin_data=json.dumps(_spark_candidate_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    out = json.loads(r.stdout)
    assert out["type"] == "candidate"


def test_wrapper_spark_add_auto_id_question(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-add.sh", [],
                     stdin_data=json.dumps(_spark_question_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    # Seeds: sq-001 -> next sq-002 (pad_width=3)
    assert out["id"] == "sq-002"


def test_wrapper_spark_add_auto_id_candidate(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-add.sh", [],
                     stdin_data=json.dumps(_spark_candidate_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    out = json.loads(r.stdout)
    # Seeds: sq-c01 -> next sq-c02 (pad_width=2, separator="")
    assert out["id"] == "sq-c02"


def test_wrapper_spark_add_schema_flag_refused(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-add.sh", ["--schema"])
    assert r.returncode == 1
    assert "no longer available" in r.stderr


def test_wrapper_spark_add_duplicate_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-add.sh", [],
                     stdin_data=json.dumps(_spark_question_rec(id="sq-001")))
    assert r.returncode != 0


# ===========================================================================
# spark-questions-update-field.sh
# ===========================================================================

def test_wrapper_spark_update_field(running_daemon):
    project_root, _ = running_daemon
    live = _spark_path(project_root)
    _run_wrapper(project_root, "spark-questions-add.sh", [],
                 stdin_data=json.dumps(_spark_question_rec(id="sq-100")))
    r = _run_wrapper(project_root, "spark-questions-update-field.sh",
                     ["sq-100", "status", "retired"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "sq-100")
    assert on_disk["status"] == "retired"


def test_wrapper_spark_update_field_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-update-field.sh",
                     ["sq-001"])
    assert r.returncode == 1
    assert "Usage" in r.stderr


# ===========================================================================
# spark-questions-retire.sh
# ===========================================================================

def test_wrapper_spark_retire(running_daemon):
    project_root, _ = running_daemon
    live = _spark_path(project_root)
    _run_wrapper(project_root, "spark-questions-add.sh", [],
                 stdin_data=json.dumps(_spark_question_rec(id="sq-101")))
    r = _run_wrapper(project_root, "spark-questions-retire.sh",
                     ["sq-101"])
    assert r.returncode == 0, f"stderr: {r.stderr}"
    on_disk = next(x for x in _read_jsonl(live) if x["id"] == "sq-101")
    assert on_disk["status"] == "retired"


def test_wrapper_spark_retire_missing_args(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "spark-questions-retire.sh", [])
    assert r.returncode == 1
    assert "Usage" in r.stderr
