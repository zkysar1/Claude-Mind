"""test_iteration_commit_concurrent_partner.py —  regression test.

Verifies the concurrent-partner filter in iteration-commit.sh: when a partner
agent has a non-null `in_flight.claimed_at`, neutral-path files edited
at-or-after that claim (within 5s tolerance) are filtered as concurrent
partner work, even if they POST-date the committer's own claim.

Origin: g-115-691 (bravo's investigation). Alpha's commit 79ce711 (09:25:08)
swept zeta's edit to .claude/skills/analyze-npc-behavior/SKILL.md (zeta's
g-250-68 work, edited ~09:20 AFTER alpha's claim at ~09:00). The pre-claim
filter (g-248-87, test_iteration_commit_untracked_filter.py) only catches
partner WIP PREDATING the committer's claim. The concurrent-partner filter
(g-115-692, this test) closes the post-claim gap.

Test pattern mirrors the sibling test file but with a multi-agent
team-state-read.sh shim that returns per-agent claimed_at based on the
`--field agent_status.<agent>.in_flight.claimed_at` query.

Diagnostic marker for the new filter: `filtered (concurrent-partner):`.

Cross-references: g-115-691 (investigation), g-115-692 (this fix),
test_iteration_commit_untracked_filter.py (sibling — pre-claim filter).
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
ITERATION_COMMIT_SH = CORE_SCRIPTS / "iteration-commit.sh"

# Use project-local tempdir (sibling test rationale: OS tempdir under
# C:\Users\... is unreachable from Git-Bash mount).
PROJECT_TMP = SCRIPT_DIR / "_tmp_iteration_commit_concurrent_partner_test"


def _to_bash_path(p) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


# Resolve bash via shared helper (, 2026-05-16). See
# core/scripts/tests/_bash_helpers.py for the canonical resolution
# priority. GIT_BASH alias preserved for in-file readability where the
# Git-Bash-vs-WSL distinction is load-bearing for the test logic.
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
    """Initialize a minimal git repo with the named agent dirs (self.md sentinels)."""
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Phase 2.5.D: agent dirs live under agents/ parent (must match
    # iteration-commit.sh namespace-filter discovery glob).
    (repo / "agents").mkdir()
    for a in agents:
        d = repo / "agents" / a
        d.mkdir()
        (d / "self.md").write_text(f"# {a}\n")
    core_scripts = repo / "core" / "scripts"
    core_scripts.mkdir(parents=True)
    (core_scripts / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mock_team_state_read_multi(tmpdir: Path, agent_claimed_at: dict) -> Path:
    """Shim team-state-read.sh that returns per-agent claimed_at based on the
    --field "agent_status.<agent>.in_flight.claimed_at" query argument.

    `agent_claimed_at` is a dict like `{"alpha": "2026-05-13T09:00:00",
    "zeta": "2026-05-13T09:17:00"}`. Pass None as the value to emit "null".
    Unmatched agents fall through to "null".
    """
    shim = tmpdir / "team-state-read.sh"
    lines = [
        '#!/usr/bin/env bash',
        '# multi-agent shim for test_iteration_commit_concurrent_partner.py',
        'field=""',
        'while [[ $# -gt 0 ]]; do',
        '  case "$1" in',
        '    --field) field="${2:-}"; shift 2 ;;',
        '    *) shift ;;',
        '  esac',
        'done',
        'agent_name=""',
        'if [[ "$field" =~ ^agent_status\\.([^.]+)\\.in_flight\\.claimed_at$ ]]; then',
        '  agent_name="${BASH_REMATCH[1]}"',
        'fi',
        'case "$agent_name" in',
    ]
    for agent, iso in agent_claimed_at.items():
        if iso is None:
            lines.append(f'  {agent}) echo "null" ;;')
        else:
            lines.append(f'  {agent}) echo \'"{iso}"\' ;;')
    lines.append('  *) echo "null" ;;')
    lines.append('esac')
    lines.append('exit 0')
    shim.write_text("\n".join(lines) + "\n")
    shim.chmod(0o755)
    return shim


def _shim_iteration_commit_multi(tmpdir: Path, agent_claimed_at: dict) -> Path:
    """Copy iteration-commit.sh + multi-agent team-state-read.sh shim alongside."""
    shim_dir = tmpdir / "scripts"
    shim_dir.mkdir()
    target = shim_dir / "iteration-commit.sh"
    target.write_bytes(ITERATION_COMMIT_SH.read_bytes())
    target.chmod(0o755)
    _mock_team_state_read_multi(shim_dir, agent_claimed_at)
    return target


def _seed_tracked_file(repo: Path, rel_path: str, content: str) -> Path:
    """Create + commit a file so subsequent modifications appear as ' M'."""
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    subprocess.run(["git", "add", str(full)], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"seed {rel_path}"], cwd=repo, check=True)
    return full


def _iso_to_epoch(iso: str) -> int:
    return int(datetime.datetime.fromisoformat(iso).timestamp())


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_concurrent_partner_edit_at_neutral_path_is_filtered():
    """Canonical : file edited at neutral path DURING partner's
    in_flight goal must be filtered, even if mtime POST-dates committer's
    own claim. Mimics alpha vs zeta race on .claude/skills/."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # Alpha (committer) claimed at 09:00. Zeta claimed at 09:17.
        # File edited at 09:20 — POST-dates alpha's claim, POST-dates zeta's claim.
        # Pre-claim filter would NOT catch it (file > committer.claimed_at).
        # Concurrent filter MUST catch it (file >= zeta.claimed_at).
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })

        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        target = repo / "core" / "scripts" / "shared-partner-edit.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# zeta's concurrent edit during alpha's iteration\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" in combined, \
            f"Concurrent-partner filter did NOT fire. combined={combined!r}"
        assert "shared-partner-edit.py" in combined, \
            f"Filtered file missing from output. combined={combined!r}"
        assert "partner=zeta" in combined, \
            f"Partner attribution missing. combined={combined!r}"


def test_concurrent_partner_no_partner_in_flight_file_included():
    """Control: when partner has null in_flight, file at neutral path is
    staged normally (no concurrent filter fires)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": None,  # partner has no in_flight
        })

        # File mtime post-alpha's claim — should be staged
        edit_epoch = _iso_to_epoch(alpha_claim) + 1200
        target = repo / "core" / "scripts" / "alpha-own-work.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# alpha's own work, no partner in_flight\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" not in combined, \
            f"Concurrent filter fired despite null partner in_flight. combined={combined!r}"
        assert "alpha-own-work.py" in combined, \
            f"Own work file missing from staging. combined={combined!r}"


def test_concurrent_partner_own_agent_dir_not_filtered():
    """File under committer's own agent dir is NOT filtered by concurrent
    filter (own work in own namespace). Matches the pre-claim filter's
    own-dir exemption."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })

        # File under alpha/ (own dir) with mtime that would otherwise
        # match the concurrent-partner window
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        target = repo / "agents" / "alpha" / "experience" / "alpha-during-zeta-claim.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# alpha's own work — happens to be during zeta's iteration\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-03", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" not in combined, \
            f"Concurrent filter incorrectly fired on own-agent-dir path. combined={combined!r}"
        assert "alpha/experience" in combined, \
            f"Own agent-dir file missing from staging. combined={combined!r}"


def test_concurrent_partner_include_untracked_disables_filter():
    """--include-untracked override disables BOTH the pre-claim AND the
    concurrent-partner filter."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })

        # File that would normally be filtered by concurrent filter
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        target = repo / "core" / "scripts" / "override-concurrent.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# would normally be filtered\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-04", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run",
             "--include-untracked"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" not in combined, \
            f"Filter fired despite --include-untracked. combined={combined!r}"
        assert "override-concurrent.py" in combined, \
            f"Override file missing from staging. combined={combined!r}"


def test_concurrent_partner_modified_tracked_file_filtered():
    """M-status (modified tracked file) concurrent-partner edit must be
    filtered too — extends the 38fb983 M-status coverage to concurrent
    partner edits."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })

        # Seed tracked file, then modify with mtime in zeta's window
        target = _seed_tracked_file(repo, "core/scripts/m-status-concurrent.py", "# v1\n")
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        target.write_text("# v2 — zeta's concurrent edit\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-05", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" in combined, \
            f"Concurrent filter did NOT catch M-status file. combined={combined!r}"
        assert "m-status-concurrent.py" in combined, \
            f"Filtered M file missing from output. combined={combined!r}"


def test_concurrent_partner_predate_partner_claim_not_filtered_by_concurrent():
    """File edited BEFORE partner's claim is NOT caught by concurrent filter
    (handled by pre-claim filter when applicable). This verifies the two
    filters partition correctly and concurrent doesn't over-match."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # Alpha claimed at 09:00, zeta claimed at 09:30
        # File edited at 09:15 — AFTER alpha's claim, BEFORE zeta's claim
        # Pre-claim filter: file_mtime > committer.claimed_at, so NO filter
        # Concurrent filter: file_mtime + 5 < zeta.claimed_at, so NO filter
        # Expected: file is STAGED (legitimate alpha own work)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:30:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })

        edit_epoch = _iso_to_epoch("2026-05-13T09:15:00")
        target = repo / "core" / "scripts" / "alpha-pre-zeta-claim.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# alpha's edit between alpha-claim and zeta-claim\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-06", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        # NEITHER filter should fire
        assert "filtered (concurrent-partner)" not in combined, \
            f"Concurrent filter incorrectly fired on pre-zeta-claim file. combined={combined!r}"
        assert "filtered (cross-agent-uncommitted)" not in combined, \
            f"Pre-claim filter incorrectly fired. combined={combined!r}"
        assert "alpha-pre-zeta-claim.py" in combined, \
            f"File missing from staging plan. combined={combined!r}"


def test_concurrent_partner_5s_tolerance_at_boundary():
    """File edited exactly at partner's claim minus 5s should still be
    filtered (5s clock-skew tolerance is INCLUSIVE on the partner side)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim_epoch = _iso_to_epoch("2026-05-13T09:17:00")
        zeta_claim_iso = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim_iso,
        })

        # File mtime exactly zeta.claimed_at - 5s — should match (tolerance)
        edit_epoch = zeta_claim_epoch - 5
        target = repo / "core" / "scripts" / "boundary-5s-tolerance.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# edge case: 5s before partner claim\n")
        os.utime(target, (edit_epoch, edit_epoch))

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-cp-07", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )

        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner)" in combined, \
            f"5s tolerance boundary case not filtered. combined={combined!r}"


# ---------------------------------------------------------------------------
#  — committer first-person-authorship exemption
#
# The  filter above keys ONLY on (partner in_flight + neutral-path
# mtime at/after partner.claimed_at) with ZERO authorship signal. It dropped
# alpha's OWN  deliverables twice (commits 8be45e1, 28a3b7a) while
# charlie held an unrelated OHS in_flight. Fix: exempt a neutral path from the
# concurrent-partner drop when the committer's OWN session/uncommitted-edits
# .jsonl (the per-edit SSOT the  partner block already parses)
# recorded it — first-person authorship overrides the mtime-coincident
# partner-claim signal. Genuine partner edits (absent from the committer's own
# log) still drop. Marker: ` first-person-authorship exemption`.
# ---------------------------------------------------------------------------


def _seed_own_uncommitted_log(repo: Path, agent: str, rel_paths) -> Path:
    """Write the committer's own session/uncommitted-edits.jsonl (the
    per-edit SSOT uncommitted-edits-record.sh produces). Repo-relative
    `file` field per the g-115-697 schema the snapshot block parses."""
    log = repo / "agents" / agent / "session" / "uncommitted-edits.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(f'{{"file": "{p}", "agent": "{agent}"}}\n' for p in rel_paths)
    )
    return log


def test_g115828_committer_own_log_exempts_and_genuine_partner_still_drops():
    """Canonical : committer=alpha edits core/scripts/X.py while
    partner=zeta in_flight on an unrelated goal. X.py is recorded in alpha's
    OWN uncommitted-edits.jsonl → must NOT be dropped. A sibling neutral file
    NOT in alpha's own log, edited in the same partner window, MUST still drop.
    One scenario pins both the exemption AND the per-path discrimination."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")

        # (a) alpha-authored: recorded in alpha's OWN uncommitted log
        authored = repo / "core" / "scripts" / "alpha-authored-828.py"
        authored.parent.mkdir(parents=True, exist_ok=True)
        authored.write_text("# alpha's own deep-iteration deliverable\n")
        os.utime(authored, (edit_epoch, edit_epoch))
        # (b) genuine partner edit: NOT in alpha's own log
        genuine = repo / "core" / "scripts" / "zeta-genuine-828.py"
        genuine.write_text("# genuinely zeta's concurrent edit\n")
        os.utime(genuine, (edit_epoch, edit_epoch))

        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/alpha-authored-828.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-828-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout

        # Exemption fired for the alpha-authored file
        assert "g-115-828 first-person-authorship exemption" in combined, \
            f"Exemption INFO missing. combined={combined!r}"
        assert "retained (committer-authored): core/scripts/alpha-authored-828.py" in combined, \
            f"alpha-authored file not retained. combined={combined!r}"
        # The genuine partner edit STILL drops
        assert "filtered (concurrent-partner): core/scripts/zeta-genuine-828.py" in combined, \
            f"Genuine partner edit was NOT dropped (over-exemption). combined={combined!r}"
        # alpha-authored must NOT appear in any concurrent-partner drop line
        assert "filtered (concurrent-partner): core/scripts/alpha-authored-828.py" not in combined, \
            f"alpha-authored file wrongly dropped. combined={combined!r}"


def test_g115828_no_own_log_failsafe_drops_as_before():
    """Fail-safe: with NO committer own uncommitted-edits.jsonl, the
    concurrent-partner filter behaves exactly as pre-g-115-828 (drop).
    Confirms the fix strictly narrows false-positives, never widens."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        target = repo / "core" / "scripts" / "no-own-log-828.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# no own log exists for alpha\n")
        os.utime(target, (edit_epoch, edit_epoch))
        # Deliberately do NOT seed alpha/session/uncommitted-edits.jsonl

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-828-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner): core/scripts/no-own-log-828.py" in combined, \
            f"Fail-safe broke — file not dropped without own log. combined={combined!r}"
        assert "g-115-828 first-person-authorship exemption" not in combined, \
            f"Exemption fired with no own log. combined={combined!r}"


def test_g115828_own_log_does_not_exempt_unrelated_path():
    """The exemption is PER-PATH: an own-log entry for file A does not
    exempt a different file B. Prevents 'any own-log entry → blanket pass'."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        zeta_claim = "2026-05-13T09:17:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": zeta_claim,
        })
        edit_epoch = _iso_to_epoch("2026-05-13T09:20:00")
        other = repo / "core" / "scripts" / "unrelated-B-828.py"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("# file B — NOT the one in the own log\n")
        os.utime(other, (edit_epoch, edit_epoch))
        # Own log records a DIFFERENT path (file A, which isn't even present)
        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/some-other-A-828.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-828-03", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout
        assert "filtered (concurrent-partner): core/scripts/unrelated-B-828.py" in combined, \
            f"Unrelated path wrongly exempted by mismatched own-log entry. combined={combined!r}"


# ---------------------------------------------------------------------------
#  /  — partner-uncommitted-log filter when partner has
# NO current in_flight (cross-claim attribution gap closure)
#
# Scenario: partner edited a neutral path DURING a prior in_flight goal, then
# cleared in_flight (routine close — no iteration-commit, no log clear). The
# committer (alpha) now closes deep with file_mtime AFTER alpha's own claim.
#   - Filter 1 (mtime predates committer.claimed_at): does NOT fire — edit
#     is AFTER alpha's claim.
#   - Filter 2 (concurrent-partner via partner.in_flight): does NOT fire —
#     partner cleared in_flight.
#   - Filter 3 (partner-uncommitted-log SSOT, ): MUST fire — the
#     partner's session/uncommitted-edits.jsonl still records the path
#     because the routine close did not invoke iteration-commit to clear
#     it. Closes the "between-claim attribution gap" that 
#     introduced but never had a pinned test for.
#
# Pins the contract  set out to verify: simulate two agents with
# overlapping claim windows, verify partner edits stay in partner workspace.
# Acceptance: this test passes against the current iteration-commit.sh
# without code changes (Filter 3 already handles the case correctly).
# ---------------------------------------------------------------------------


def _seed_partner_uncommitted_log(repo: Path, partner: str, rel_paths) -> Path:
    """Write a partner's session/uncommitted-edits.jsonl. Schema matches the
    committer-own variant — same `file` field consumed by Filter 3's snapshot
    block (g-115-697). The partner-vs-own distinction is purely the dir name
    (alpha/ vs zeta/) — both feed the same SSOT parser in iteration-commit.sh."""
    log = repo / "agents" / partner / "session" / "uncommitted-edits.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(f'{{"file": "{p}", "agent": "{partner}"}}\n' for p in rel_paths)
    )
    return log


def test_g115697_partner_uncommitted_log_filters_after_in_flight_cleared():
    """Cross-claim attribution gap ( acceptance test):
    partner=zeta edited core/scripts/X.py during a prior routine goal,
    cleared in_flight without committing, log still has the entry. alpha
    closes deep — Filter 3 MUST drop X.py via the partner-uncommitted-log
    SSOT, since Filter 2 (in_flight=null) and Filter 1 (mtime>committer
    claim) both pass-through."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # alpha claims at 09:00; zeta's in_flight is CLEARED (None).
        # The file mtime is AFTER alpha's claim so Filter 1 cannot fire.
        alpha_claim = "2026-05-13T09:00:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": None,  # in_flight cleared after zeta's routine close
        })

        # File modified at 09:15 — AFTER alpha's claim (09:00). Mimics zeta
        # editing during its own 09:05-09:13 in_flight window, then routine-
        # closing at 09:14 without iteration-commit.
        edit_epoch = _iso_to_epoch("2026-05-13T09:15:00")
        target = repo / "core" / "scripts" / "zeta-routine-edit-914.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# zeta's edit during a routine close — log not cleared\n")
        os.utime(target, (edit_epoch, edit_epoch))

        # Critical: zeta's log STILL records the edit (routine close skipped
        # iteration-commit, which is the only writer that clears the log).
        _seed_partner_uncommitted_log(
            repo, "zeta", ["core/scripts/zeta-routine-edit-914.py"],
        )

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-914-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout

        # Filter 3 must fire — the path is recorded in zeta's log even
        # though zeta has no current in_flight. The diagnostic marker
        # `filtered (partner-log)` is the canonical signature.
        assert "filtered (partner-uncommitted-log)" in combined, \
            f"Filter 3 did NOT fire. combined={combined!r}"
        assert "zeta-routine-edit-914.py" in combined, \
            f"Target file missing from filter output. combined={combined!r}"
        # Belt-and-suspenders: Filter 2 must NOT have fired (in_flight=null
        # means partner_claimed_at_epochs is empty). If Filter 2 fires here,
        # the shim's null-zeta path is broken AND we'd be testing Filter 2.
        assert "filtered (concurrent-partner): core/scripts/zeta-routine-edit-914.py" not in combined, \
            f"Filter 2 fired despite null partner in_flight — shim broken or filter logic mis-routed. combined={combined!r}"


def test_g115697_partner_log_does_not_exempt_own_authored_path():
    """Sanity: a path in zeta's log MUST drop, but if alpha ALSO records
    the same path in alpha's own log, the committer-own-log exemption is
    NOT a defense here — Filter 3 (partner-log) and the g-115-828 own-log
    exemption are SEPARATE branches. Filter 3 fires from partner_log
    membership regardless of own-log content. Pins that the partner-log
    drop is not silently overridden by own-log."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": None,
        })
        edit_epoch = _iso_to_epoch("2026-05-13T09:15:00")
        target = repo / "core" / "scripts" / "contested-914.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# both alpha and zeta touched this — partner-log wins\n")
        os.utime(target, (edit_epoch, edit_epoch))

        # Both alpha's own log AND zeta's partner log record the same path.
        # Filter 3 (partner-log) fires first and drops; the 
        # exemption only runs inside the Filter 2 branch (which doesn't
        # fire here because in_flight=null). So the contested path drops.
        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/contested-914.py"])
        _seed_partner_uncommitted_log(repo, "zeta", ["core/scripts/contested-914.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-914-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout
        # Filter 3 fires — partner-log membership wins over a same-path
        # entry in alpha's own log when in_flight is null. (When in_flight
        # is non-null AND the path is in own-log,  exemption
        # applies — but that branch is a different code path.)
        assert "filtered (partner-uncommitted-log)" in combined, \
            f"Filter 3 did NOT fire on contested path. combined={combined!r}"
        assert "contested-914.py" in combined, \
            f"Contested file missing from filter output. combined={combined!r}"


if __name__ == "__main__":
    test_concurrent_partner_edit_at_neutral_path_is_filtered()
    test_concurrent_partner_no_partner_in_flight_file_included()
    test_concurrent_partner_own_agent_dir_not_filtered()
    test_concurrent_partner_include_untracked_disables_filter()
    test_concurrent_partner_modified_tracked_file_filtered()
    test_concurrent_partner_predate_partner_claim_not_filtered_by_concurrent()
    test_concurrent_partner_5s_tolerance_at_boundary()
    test_g115828_committer_own_log_exempts_and_genuine_partner_still_drops()
    test_g115828_no_own_log_failsafe_drops_as_before()
    test_g115828_own_log_does_not_exempt_unrelated_path()
    test_g115697_partner_uncommitted_log_filters_after_in_flight_cleared()
    test_g115697_partner_log_does_not_exempt_own_authored_path()
    print("All 12 concurrent-partner tests passed (7 g-115-692 + 3 g-115-828 + 2 g-115-697/914).")
