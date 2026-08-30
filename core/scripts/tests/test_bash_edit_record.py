"""test_bash_edit_record.py — rb-1761 Bash-blind gap closure (over-inclusion fix).

Verifies core/scripts/bash-edit-record.sh: the PostToolUse[Bash] companion to
uncommitted-edits-record.sh that records neutral-path framework files an agent
creates/modifies via the Bash tool into <agent>/session/uncommitted-edits.jsonl,
so iteration-commit.sh's partner-uncommitted-log filter (g-115-697) can drop
them from a partner's commit.

Root incident: alpha's brand-new core/scripts/knowledge-graph-build.py (+ test,
545 LOC) created via Bash were absent from alpha's uncommitted-edits.jsonl
(Write/Edit recorder is Bash-blind), so echo's iteration-commit d78979bd swept
them under echo's authorship with no fresh-eyes review (2026-06-20). Two prior
events.py sweeps the same week. This recorder closes the gap WITHOUT touching
iteration-commit.sh (purely additive) and WITHOUT the rb-1794 allowlist flip.

Design contract exercised here:
  - records a neutral file whose mtime is NEWER than the per-session cursor;
  - first run (no cursor) records NOTHING and seeds the cursor (no over-capture
    of pre-existing partner WIP);
  - skips a file OLDER than the cursor;
  - dedups against already-logged paths;
  - resolves the agent from the payload session_id binding when MIND_AGENT is
    absent (the production PostToolUse[Bash] case);
  - fails open on empty stdin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECORD_SH = CORE_SCRIPTS / "bash-edit-record.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_bash_edit_record_test"

# Strip the framework env namespace before spawning the record-script
# subprocess so a fresh shell sourcing _paths.sh resolves paths from the seeded
# temp repo, not from leaked parent env (rb-1324/rb-1565 non-hermetic class —
# mirrors test_uncommitted_edits_log_filter.py).
_FRAMEWORK_ENV_PREFIXES = (
    "MIND_", "WORLD_", "META_", "STORAGE_", "FILEOPS_", "RT_",
    "RUNTIME_", "AGENTS_", "MACHINE_", "OWNERSHIP_", "ENVIRONMENT_", "MIND_",
    "BODY_",  # : BODY_WM_PATH is the FIRST branch of wm_path()
)

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _hermetic_env(**overrides) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_FRAMEWORK_ENV_PREFIXES) and k != "PROJECT_ROOT"
    }
    env.update(overrides)
    return env


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _setup_repo(tmp: Path, agents=("alpha", "zeta")) -> Path:
    """Temp repo whose core/scripts holds the real recorder + _paths.sh so
    _paths.sh anchors PROJECT_ROOT to `repo` via BASH_SOURCE."""
    repo = tmp / "repo"
    repo.mkdir()
    (repo / "agents").mkdir()
    for a in agents:
        d = repo / "agents" / a
        (d / "session").mkdir(parents=True)
        (d / "self.md").write_text(f"# {a}\n")
        (d / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (repo / ".claude").mkdir()
    for fname in ("bash-edit-record.sh", "_paths.sh"):
        dst = core_scripts / fname
        dst.write_bytes((CORE_SCRIPTS / fname).read_bytes())
        dst.chmod(0o755)
    # Pre-seed .python-shim so _paths.sh skips the Windows Store python3 probe.
    # The shim MUST exec the real interpreter (sys.executable), NOT `py -3`: on a
    # host where `py` is itself an `exec python3` wrapper (e.g. this Linux box's
    # /usr/local/bin/py), a `python3->py -3` shim plus a `py -3->python3` wrapper
    # form an infinite mutual recursion once the shim dir is on PATH — the record
    # script hangs and the subprocess times out (). sys.executable is an
    # absolute path, so there is no PATH re-resolution and no loop; it is also the
    # correct working interpreter on Windows (bypasses the Store stub).
    shim_dir = core_scripts / ".python-shim"
    shim_dir.mkdir()
    for name in ("python3", "python"):
        s = shim_dir / name
        s.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        s.chmod(0o755)
    return repo


def _run(repo: Path, payload: str, **env_overrides):
    return subprocess.run(
        [GIT_BASH, _to_bash_path(repo / "core" / "scripts" / "bash-edit-record.sh")],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=_hermetic_env(**env_overrides),
    )


def _log(repo: Path, agent: str) -> Path:
    return repo / "agents" / agent / "session" / "uncommitted-edits.jsonl"


def _cursor(repo: Path, agent: str) -> Path:
    return repo / "agents" / agent / "session" / ".bash-edit-cursor"


def _seed_cursor(repo: Path, agent: str, epoch: int):
    _cursor(repo, agent).write_text(f"{epoch}\n")


def _make_file(repo: Path, rel: str, mtime: int, body: str = "# x\n") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    os.utime(p, (mtime, mtime))
    return p


# ---------------------------------------------------------------------------


def test_records_bash_created_neutral_file():
    """A neutral file with mtime newer than the cursor is recorded."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        base = int(time.time())
        _seed_cursor(repo, "alpha", base - 60)
        _make_file(repo, "core/scripts/new-tool.py", base - 5)

        r = _run(repo, '{"session_id":""}', MIND_AGENT="alpha")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        log = _log(repo, "alpha")
        assert log.exists(), "neutral Bash-created file was not logged"
        files = [json.loads(l)["file"] for l in log.read_text().splitlines() if l.strip()]
        assert "core/scripts/new-tool.py" in files, f"missing; log={files!r}"


def test_first_run_records_nothing_and_seeds_cursor():
    """No cursor => set cursor, record nothing (no over-capture of partner WIP)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        base = int(time.time())
        # A recent file exists but there is no cursor yet.
        _make_file(repo, "core/scripts/pre-existing.py", base - 5)

        r = _run(repo, '{"session_id":""}', MIND_AGENT="alpha")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        log = _log(repo, "alpha")
        assert not log.exists() or log.read_text().strip() == "", \
            f"first run wrongly recorded: {log.read_text() if log.exists() else ''!r}"
        assert _cursor(repo, "alpha").exists(), "cursor was not seeded on first run"


def test_skips_file_older_than_cursor():
    """A file last touched BEFORE the cursor is not (re)recorded."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        base = int(time.time())
        _seed_cursor(repo, "alpha", base - 30)
        _make_file(repo, "core/scripts/stale.py", base - 120)  # older than cursor

        r = _run(repo, '{"session_id":""}', MIND_AGENT="alpha")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        log = _log(repo, "alpha")
        assert not log.exists() or "stale.py" not in log.read_text(), \
            "file older than cursor was wrongly recorded"


def test_dedup_against_existing_log():
    """An already-logged path is not duplicated (e.g. Write recorder logged it)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        base = int(time.time())
        _seed_cursor(repo, "alpha", base - 60)
        log = _log(repo, "alpha")
        log.write_text(
            '{"file":"core/scripts/dup.py","mtime":%d,"edit_ts":"2026-06-20T00:00:00","goal_id":"g-x"}\n'
            % (base - 5)
        )
        _make_file(repo, "core/scripts/dup.py", base - 3)

        r = _run(repo, '{"session_id":""}', MIND_AGENT="alpha")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        entries = [json.loads(l)["file"] for l in log.read_text().splitlines() if l.strip()]
        assert entries.count("core/scripts/dup.py") == 1, f"duplicated; entries={entries!r}"


def test_resolves_agent_from_session_binding():
    """Production path: MIND_AGENT absent, agent resolved from session_id
    binding.yaml (PostToolUse[Bash] gets no MIND_AGENT in env)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        sid = "sess-abc-123"
        bdir = repo / "agents" / "zeta" / "sessions" / sid
        bdir.mkdir(parents=True)
        (bdir / "binding.yaml").write_text(f"agent: zeta\nmode: autonomous\n")
        base = int(time.time())
        _seed_cursor(repo, "zeta", base - 60)
        _make_file(repo, "core/scripts/zeta-tool.py", base - 5)

        # No MIND_AGENT override -> must resolve from the binding.
        r = _run(repo, json.dumps({"session_id": sid}))
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        log = _log(repo, "zeta")
        assert log.exists() and "zeta-tool.py" in log.read_text(), \
            f"binding-resolved record missing; log={log.read_text() if log.exists() else '(absent)'!r}"


def test_fail_open_empty_stdin():
    """Empty stdin must exit 0 (fail-open, no crash)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        r = _run(repo, "")
        assert r.returncode == 0, f"non-zero on empty stdin: {r.stderr!r}"


def test_agent_private_path_not_recorded():
    """A file under agents/<name>/ is NOT a neutral framework path — the scan
    roots are core/ + .claude/ only, so agent-private files never land here."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        repo = _setup_repo(Path(td))
        base = int(time.time())
        _seed_cursor(repo, "alpha", base - 60)
        _make_file(repo, "agents/alpha/scratch.py", base - 5)

        r = _run(repo, '{"session_id":""}', MIND_AGENT="alpha")
        assert r.returncode == 0, f"crashed: {r.stderr!r}"

        log = _log(repo, "alpha")
        assert not log.exists() or "scratch.py" not in log.read_text(), \
            "agent-private path was wrongly recorded"
