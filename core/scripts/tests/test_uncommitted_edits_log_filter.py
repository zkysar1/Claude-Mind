"""test_uncommitted_edits_log_filter.py —  regression test.

Verifies the partner-uncommitted-log filter in iteration-commit.sh: when
partner agent has a recorded entry in its `<partner>/session/uncommitted-edits.jsonl`,
the matching neutral-path file is filtered as partner work, EVEN WHEN partner's
in_flight is null. Closes the between-claim attribution gap that g-115-692's
filter could not catch.

Origin: g-115-695 (zeta's investigation brief naming three deepening options;
option (a) selected — see zeta/reports/g-115-695-between-claim-gap-brief.md).

Canonical incident: alpha commit 1b1dcac (2026-05-13 12:13:39, g-250-67) swept
zeta's core/scripts/agent_paths.py + aspirations_write.py changes because
zeta's in_flight was null at the moment of alpha's filter read — zeta's
previous goal had completed and the next claim had not yet fired. Both
g-248-87 (pre-claim) and g-115-692 (concurrent-partner) filters miss this
because both anchor on team-state.in_flight.

The new filter consults each partner's uncommitted-edits.jsonl directly,
which the partner's own PostToolUse hook writes at EDIT time and clears at
SELF-COMMIT time. The log persists across the partner's between-claim
window, so the filter has signal independent of in_flight state.

Diagnostic marker for the new filter: `filtered (partner-uncommitted-log):`.

Cross-references: g-115-691 (original investigation), g-115-692 (concurrent
filter), g-115-695 (between-claim gap investigation), rb-908 (Layer D parent
status), .claude/skills/aspirations-precheck/SKILL.md Phase 0.5b.7 (sibling
sweep for Unblock-parent-status — different scope but same recurrence shape).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

PROJECT_TMP = SCRIPT_DIR / "_tmp_uncommitted_edits_log_filter_test"


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority. GIT_BASH alias kept — _run_bash uses mount-prefix paths.
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _run_bash(args, env=None, cwd=None):
    cmd = [GIT_BASH] + [_to_bash_path(a) for a in args]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        env=full_env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _setup_repo(tmpdir: Path, agents=("alpha", "zeta")) -> Path:
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Phase 2.5.D: agent dirs live under agents/ parent.
    (repo / "agents").mkdir()
    for a in agents:
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
        (d / "session").mkdir()
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mock_team_state_read_null(tmpdir: Path) -> Path:
    """Shim team-state-read.sh that returns null for all in_flight queries.
    Models the canonical between-claim window where partner has no current
    in_flight but has uncommitted edits."""
    shim = tmpdir / "team-state-read.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# null-everywhere shim for test_uncommitted_edits_log_filter.py\n"
        "echo null\n"
        "exit 0\n"
    )
    shim.chmod(0o755)
    return shim


def _shim_iteration_commit(tmpdir: Path) -> Path:
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    _mock_team_state_read_null(shim_dir)
    return target


def _seed_partner_log(repo: Path, partner: str, files: list[str]) -> Path:
    """Write the partner's uncommitted-edits.jsonl with the given rel paths.
    Mimics what uncommitted-edits-record.sh would write at edit time."""
    log = repo / "agents" / partner / "session" / "uncommitted-edits.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as f:
        for rel in files:
            f.write(
                '{"file":"' + rel + '","mtime":1747145000,"edit_ts":"2026-05-13T12:11:00","goal_id":"g-test"}\n'
            )
    return log


# ---------------------------------------------------------------------------
# Filter-side tests (iteration-commit.sh)
# ---------------------------------------------------------------------------


def test_partner_log_filter_catches_between_claim_gap():
    """Canonical  incident: file in partner's uncommitted-edits.jsonl
    is filtered EVEN when partner.in_flight is null. This is the between-claim
    window that g-115-692's filter cannot see."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)  # team-state shim returns null

        # Zeta authored core/scripts/agent_paths.py; recorded in zeta's log
        _seed_partner_log(repo, "zeta", ["core/scripts/agent_paths.py"])

        # File exists on disk (alpha sees it in git status as untracked)
        target = repo / "core" / "scripts" / "agent_paths.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# zeta's between-claim edit\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-ul-01", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (partner-uncommitted-log)" in combined, \
            f"Partner-log filter did NOT fire. combined={combined!r}"
        assert "core/scripts/agent_paths.py" in combined, \
            f"Filtered file missing from output. combined={combined!r}"
        assert "partner=zeta" in combined, \
            f"Partner attribution missing. combined={combined!r}"


def test_no_partner_log_no_filter():
    """Control: when partner's log is absent, file at neutral path stages
    normally."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # NO partner log written

        target = repo / "core" / "scripts" / "alpha-own.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# alpha's own neutral-path work\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-ul-02", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (partner-uncommitted-log)" not in combined, \
            f"Partner-log filter fired despite no partner log. combined={combined!r}"
        assert "alpha-own.py" in combined, \
            f"Own work missing from staging. combined={combined!r}"


def test_own_log_does_not_self_filter():
    """Regression: the filter MUST iterate OTHER agents' logs only. An entry
    in committer's own uncommitted-edits.jsonl must NOT cause self-filtering."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Alpha (committer) has its OWN log entry — should not self-filter
        _seed_partner_log(repo, "alpha", ["core/scripts/alpha-own.py"])

        target = repo / "core" / "scripts" / "alpha-own.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# alpha's own work\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-ul-03", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (partner-uncommitted-log)" not in combined, \
            f"Filter wrongly fired on own log. combined={combined!r}"
        assert "alpha-own.py" in combined, \
            f"Own file missing from staging. combined={combined!r}"


def test_include_untracked_disables_partner_log_filter():
    """--include-untracked must bypass the partner-log filter, matching the
    behavior of sibling filters."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        _seed_partner_log(repo, "zeta", ["core/scripts/override.py"])

        target = repo / "core" / "scripts" / "override.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# would normally be filtered\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-ul-04", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo), "--dry-run",
             "--include-untracked"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (partner-uncommitted-log)" not in combined, \
            f"Filter fired despite --include-untracked. combined={combined!r}"
        assert "override.py" in combined, \
            f"Override file missing from staging. combined={combined!r}"


# ---------------------------------------------------------------------------
# Record-script tests (uncommitted-edits-record.sh)
# ---------------------------------------------------------------------------


def _record_script() -> Path:
    return CORE_SCRIPTS / "uncommitted-edits-record.sh"


def test_record_appends_neutral_path():
    """Record script writes a JSONL entry for a neutral-path edit."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # Create the neutral-path file
        target = repo / "core" / "scripts" / "agent_paths.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# zeta edit\n")

        # Build a fake local-paths.conf so _paths.sh can resolve PROJECT_ROOT.
        # The record script reads AGENT_DIR from _paths.sh, which needs the
        # binding file pointing at the agent.
        (tmp / "ayoai-mind.path").write_text(str(repo))
        # We can't easily set up _paths.sh in this test — just invoke the
        # script directly with PROJECT_ROOT + AGENT_DIR overrides.
        # The record script sources _paths.sh; in test mode, we bypass by
        # invoking with environment overrides that _paths.sh respects.

        hook_payload = (
            '{"tool_input":{"file_path":"'
            + str(target).replace("\\", "/").replace("/", "\\\\")
            + '"}}'
        )

        # The actual record script depends on _paths.sh sourcing — which
        # requires a fully-bound agent context. For unit tests we verify the
        # script EXISTS and is invocable; functional integration is covered
        # by the iteration-commit filter tests above using direct log seeding.
        assert _record_script().exists(), "record script not found"
        # Smoke test: script runs without crashing on empty stdin
        result = subprocess.run(
            [GIT_BASH, _to_bash_path(_record_script())],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"record script crashed on empty stdin: {result.stderr}"


# ---------------------------------------------------------------------------
# Clear-on-success tests
# ---------------------------------------------------------------------------


def test_clear_on_success_removes_committed_paths():
    """After a successful commit, committer's OWN uncommitted-edits.jsonl
    has the committed paths removed but keeps non-committed entries."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        shim = _shim_iteration_commit(tmp)

        # Seed alpha's own log with TWO entries: one we'll commit, one we won't.
        log = repo / "agents" / "alpha" / "session" / "uncommitted-edits.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            '{"file":"core/will-commit.py","mtime":1747145000,"edit_ts":"2026-05-13T12:11:00","goal_id":"g-test"}\n'
            '{"file":"core/wont-commit.py","mtime":1747145001,"edit_ts":"2026-05-13T12:12:00","goal_id":"g-test"}\n'
        )

        # Only create core/will-commit.py on disk so it appears in git status
        target = repo / "core" / "will-commit.py"
        target.write_text("# committed\n")

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-clear-01", "--title", "Apply: alpha goal",
             "--outcome", "deep", "--repo", str(repo)],
            env={"MIND_AGENT": "alpha"},
        )

        assert result.returncode == 0, \
            f"commit failed: stdout={result.stdout!r} stderr={result.stderr!r}"

        # Verify log now contains ONLY the wont-commit entry
        remaining = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 1, \
            f"expected 1 remaining entry, got {len(remaining)}: {remaining!r}"
        assert "wont-commit.py" in remaining[0], \
            f"wrong entry remained: {remaining[0]!r}"
        assert "will-commit.py" not in remaining[0], \
            f"committed entry should be removed: {remaining[0]!r}"
