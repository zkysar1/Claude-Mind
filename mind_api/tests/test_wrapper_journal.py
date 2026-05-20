"""End-to-end wrapper tests for journal-add.sh / journal-update.sh /
journal-merge.sh through a running daemon (H2 Wave 1).

Full shell-wrapper -> daemon -> file cycle. Each test calls the wrapper
via subprocess with env pointed at the test daemon (isolated tmp dirs —
never the live agent's journal). Mirrors test_wrapper_pipeline.py.
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


def _journal(project_root: Path) -> Path:
    return project_root / "agents" / "alpha" / "journal.jsonl"


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


def _rec(**kw) -> dict:
    base = {
        "session": 3,
        "date": "2026-05-15",
        "journal_file": "alpha/journal/2026/05/2026-05-15.md",
        "goals_completed": ["g-001-09"],
        "key_events": ["wave-1"],
        "tags": ["h2"],
    }
    base.update(kw)
    return base


# --- journal-add.sh --------------------------------------------------------

def test_wrapper_add_creates_record(running_daemon):
    project_root, _ = running_daemon
    live = _journal(project_root)
    before = len(_read_jsonl(live))

    r = _run_wrapper(project_root, "journal-add.sh", [],
                     stdin_data=json.dumps(_rec()))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    after = _read_jsonl(live)
    assert len(after) == before + 1
    assert any(x["session"] == 3 for x in after)
    # stdout is the record (indent=2, ensure_ascii=False) — parity with the
    # deleted journal.py cmd_add print.
    assert json.loads(r.stdout)["session"] == 3


def test_wrapper_add_auto_session(running_daemon):
    project_root, _ = running_daemon
    rec = _rec()
    del rec["session"]
    r = _run_wrapper(project_root, "journal-add.sh", [],
                     stdin_data=json.dumps(rec))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert json.loads(r.stdout)["session"] == 3  # conftest seeds 1,2


def test_wrapper_add_schema_flag_refused(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "journal-add.sh", ["--schema"])
    assert r.returncode == 1
    assert "no longer available" in r.stderr


def test_wrapper_add_duplicate_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "journal-add.sh", [],
                     stdin_data=json.dumps(_rec(session=1)))
    assert r.returncode != 0


# --- journal-update.sh -----------------------------------------------------

def test_wrapper_update_replaces_record(running_daemon):
    project_root, _ = running_daemon
    live = _journal(project_root)
    new = _rec(session=2, tags=["replaced"])
    r = _run_wrapper(project_root, "journal-update.sh", ["2"],
                     stdin_data=json.dumps(new))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rec = next(x for x in _read_jsonl(live) if x["session"] == 2)
    assert rec["tags"] == ["replaced"]


def test_wrapper_update_session_dash_form(running_daemon):
    project_root, _ = running_daemon
    live = _journal(project_root)
    new = _rec(session=2, tags=["dash"])
    r = _run_wrapper(project_root, "journal-update.sh", ["session-2"],
                     stdin_data=json.dumps(new))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rec = next(x for x in _read_jsonl(live) if x["session"] == 2)
    assert rec["tags"] == ["dash"]


def test_wrapper_update_missing_session_arg(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "journal-update.sh", [],
                     stdin_data=json.dumps(_rec(session=2)))
    assert r.returncode == 1
    assert "requires a session id" in r.stderr


# --- journal-merge.sh ------------------------------------------------------

def test_wrapper_merge_union_append_scalar(running_daemon):
    project_root, _ = running_daemon
    live = _journal(project_root)
    # conftest session 2: goals_completed=[,], key_events=[],
    # tags=[routine]
    patch = json.dumps({
        "goals_completed": ["g-001-03", "g-NEW"],
        "key_events": ["e1"],
        "tags": ["routine", "extra"],
        "hypotheses_resolved": 7,
    })
    r = _run_wrapper(project_root, "journal-merge.sh", ["2"],
                     stdin_data=patch)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    rec = next(x for x in _read_jsonl(live) if x["session"] == 2)
    assert rec["goals_completed"] == ["g-001-02", "g-001-03", "g-NEW"]
    assert rec["key_events"] == ["e1"]
    assert rec["tags"] == ["routine", "extra"]
    assert rec["hypotheses_resolved"] == 7


def test_wrapper_merge_not_found_nonzero(running_daemon):
    project_root, _ = running_daemon
    r = _run_wrapper(project_root, "journal-merge.sh", ["99"],
                     stdin_data=json.dumps({"tags": ["x"]}))
    assert r.returncode != 0
