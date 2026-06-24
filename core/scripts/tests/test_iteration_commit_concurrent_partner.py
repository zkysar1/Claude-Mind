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
    # 3: the own-log () and partner-log () snapshots
    # import _normalize_rel_path from $REPO/core/scripts/_cross_agent_attribution
    # _filter.py (which pulls _stdio). Copy both into the temp repo so the harness
    # exercises the REAL normalization instead of the import's fail-open identity
    # fallback. Without these, absolute-form log entries silently stayed
    # un-normalized in-test — the exact harness blind spot that let the own-log
    # normalization asymmetry ship unnoticed.
    for _mod in ("_cross_agent_attribution_filter.py", "_stdio.py"):
        (core_scripts / _mod).write_bytes((CORE_SCRIPTS / _mod).read_bytes())
    # Match production: __pycache__ is gitignored. Without this, the
    # py-normalization subprocess that compiles the copied helper modules above
    # leaves an untracked core/scripts/__pycache__/ that git-status surfaces as
    # a staged neutral-path artifact — which the 6 over-inclusion audit
    # then (correctly) flags, polluting the audit-silent assertion.
    (repo / ".gitignore").write_text("__pycache__/\n")
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
# 3 — own-log path-normalization symmetry (over-exclusion fix)
#
# The  own-log exemption snapshot did raw `print(fp)` with NO
# _normalize_rel_path, while the  partner-log snapshot DID normalize
# (0). uncommitted-edits.jsonl demonstrably holds BOTH relative AND
# absolute (legacy C:/...) `file` entries. So an ABSOLUTE own-log entry keyed
# committer_authored_paths by the absolute string, while the git-status
# candidate `$path` is repo-relative — the membership check missed, the
# exemption never fired, and the concurrent-partner filter dropped the
# committer's OWN file (over-exclusion; guard-608 / commit 7f1df61d). The fix
# applies the SAME _normalize_rel_path both consumers now share (rb-1405 SSOT).
# Marker: ` first-person-authorship exemption` must fire for an
# absolute-form own-log entry.
# ---------------------------------------------------------------------------


def test_g1151413_absolute_own_log_entry_still_exempts():
    """3: an own-log entry stored in ABSOLUTE (Windows-drive) form —
    the legacy shape proven to coexist in real uncommitted-edits.jsonl files
    (g-115-1180) — must STILL grant the g-115-828 first-person-authorship
    exemption. Pre-fix the own-log snapshot did not normalize, so the absolute
    key missed the repo-relative candidate and the committer's own file was
    wrongly dropped by the concurrent-partner filter. This is the genuine
    regression: it FAILS against the pre-fix script (file dropped) and PASSES
    post-fix (file retained via the normalized own-log exemption)."""
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

        # alpha's own deliverable at a neutral path, mtime inside zeta's
        # concurrent in_flight window (so the concurrent-partner filter would
        # drop it absent the exemption).
        authored = repo / "core" / "scripts" / "abs-own-log-1413.py"
        authored.parent.mkdir(parents=True, exist_ok=True)
        authored.write_text("# alpha's own deliverable; own-log entry is absolute-form\n")
        os.utime(authored, (edit_epoch, edit_epoch))

        # Seed alpha's own-log with the ABSOLUTE Windows-drive form. The script
        # resolves proot=$REPO in MSYS form (/c/.../repo); _normalize_rel_path
        # adds the cross-form (C:/.../repo) prefix, so this absolute entry
        # normalizes to the repo-relative candidate `core/scripts/abs-own-log-1413.py`.
        abs_form = str(repo).replace("\\", "/") + "/core/scripts/abs-own-log-1413.py"
        _seed_own_uncommitted_log(repo, "alpha", [abs_form])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1413-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout

        assert "g-115-828 first-person-authorship exemption" in combined, \
            f"Exemption did NOT fire for absolute-form own-log entry (own-log normalization missing?). combined={combined!r}"
        assert "retained (committer-authored): core/scripts/abs-own-log-1413.py" in combined, \
            f"Absolute-form own-log file not retained. combined={combined!r}"
        assert "filtered (concurrent-partner): core/scripts/abs-own-log-1413.py" not in combined, \
            f"Committer's own file wrongly dropped despite absolute-form own-log entry. combined={combined!r}"


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


def test_g1151620_partner_log_exempts_own_authored_double_recorded_path():
    """0 REVERSAL of the prior  default (this test replaces
    test_g115697_partner_log_does_not_exempt_own_authored_path): when a contested
    neutral path is recorded in BOTH the committer's OWN uncommitted-edits.jsonl
    AND a partner's log (the g-115-695 double-recording overlap), the committer's
    first-person authorship now OVERRIDES the partner-log drop — the file is
    RETAINED, not dropped.

    Prior behavior: Filter 3 (partner-log) and the g-115-828 own-log exemption
    were SEPARATE branches, so Filter 3 dropped a contested path regardless of
    own-log content when in_flight was null. That default silently ORPHANED the
    committer's own deep-close work whenever a stale partner-log entry happened
    to cover the same path. Root cause (investigated under g-115-1620):
    uncommitted-edits.jsonl is cleared ONLY by the editing agent's own
    iteration-commit self-commit, so a file a PARTNER commits first leaves a
    persistent stale entry in the original editor's log (alpha's live log held
    114 such framework-file entries). Within the 48h staleness window those
    entries false-drop another agent's legitimate edits. Observed incidents:
    g-303-33, g-115-1619 (zeta's evolution-complete.py / fresh-eyes-program /
    verify-learning dropped — a WARN not an ERROR, so the goal closed exit-0
    with its edits left uncommitted).

    Failure-mode rationale for the reversal: dropping leaves the committer's own
    work UNCOMMITTED/orphaned (no agent re-commits a file that isn't really
    theirs); retaining at worst mis-attributes a genuinely co-edited file to the
    committer's commit — but the work IS committed (the partner's worktree
    version is already merged in the shared index). Lost work >> mild
    mis-attribution, so the own-log entry — proof of CURRENT authorship interest —
    now wins under BOTH double-recording interpretations. g-115-1620 closes the
    asymmetry by giving the g-115-697 partner-log filter the same own-log check
    the g-115-828 concurrent-partner filter already had.

    A partner-ONLY log entry (no own-log presence) STILL drops — pinned by
    test_g115697_partner_uncommitted_log_filters_after_in_flight_cleared."""
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
        target = repo / "core" / "scripts" / "contested-1620.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# both alpha and zeta logs record this — own-log wins (g-115-1620)\n")
        os.utime(target, (edit_epoch, edit_epoch))

        # Both alpha's own log AND zeta's partner log record the same path.
        # 0: the own-log presence now exempts the contested path from
        # the partner-log drop (first-person authorship overrides).
        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/contested-1620.py"])
        _seed_partner_uncommitted_log(repo, "zeta", ["core/scripts/contested-1620.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1620-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout
        # The own-log proof overrides the partner-log drop: the path is RETAINED.
        assert "filtered (partner-uncommitted-log): core/scripts/contested-1620.py" not in combined, \
            f"Contested own-authored path was wrongly dropped (g-115-1620 regression). combined={combined!r}"
        assert "retained (committer-authored-log)" in combined, \
            f"g-115-1620 own-log exemption marker missing. combined={combined!r}"
        assert "contested-1620.py" in combined, \
            f"Retained contested file missing from staging. combined={combined!r}"


# ---------------------------------------------------------------------------
# 6 — over-inclusion AUDIT (detection-only)
#
# The attribution filters above are a DENYLIST: a neutral-path file is staged
# unless a partner signal fires. An ORPHAN — a partner's uncommitted shared-
# path edit whose every signal has lapsed (partner not in_flight, edit post-
# dates the committer claim, edit never recorded in any uncommitted-edits.jsonl)
# — is byte-indistinguishable from the committer's own UNLOGGED neutral edit.
# test_concurrent_partner_no_partner_in_flight_file_included pins that an own
# unlogged neutral file MUST stage, so an orphan cannot be auto-dropped without
# over-excluding legitimate own work. Staging-time PREVENTION is therefore
# impossible; the 6 audit makes the residual over-inclusion VISIBLE +
# post-hoc-correctable instead of silently swept under the committing goal_id
# (the  deep auto-override path). A flagged file is EITHER a recording
# gap (own edit missing from the own-log) OR a mis-attributed partner orphan —
# both actionable. Deny-vs-allow-list contract: 2.
#
# The audit gates on OUTCOME==deep (the only path that bulk-stages neutral
# files under one goal_id while bypassing the uncommitted-work-gate) and keys
# the per-file flag on absence from committer_authored_paths — the SAME own-log
# SSOT the  exemption uses, so attribution is consistent across the
# retain-side (Filter 2 exemption) and the detect-side (this audit).
# Marker: `AUDIT (6 over-inclusion)`.
# ---------------------------------------------------------------------------


def test_g1151426_over_inclusion_audit_flags_unattributed_not_attributed():
    """6 (both branches in one scenario): under a deep outcome with
    partner in_flight=null, two neutral-path files reach staging. One is
    recorded in alpha's OWN uncommitted-edits.jsonl (attributed) -> must NOT be
    flagged. The other is in NO own-log (orphan OR recording gap) -> MUST be
    flagged `unattributed (over-inclusion-risk)`. Pins the audit-fires branch
    AND the per-path own-log discrimination (guard-502: met AND not-met)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # partner in_flight=null -> Filter 2/3 cannot fire; both files post-date
        # alpha's claim -> Filter 1 cannot fire. Both stage and reach the audit.
        alpha_claim = "2026-05-13T09:00:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": None,
        })
        edit_epoch = _iso_to_epoch(alpha_claim) + 1200

        attributed = repo / "core" / "scripts" / "attributed-1426.py"
        attributed.parent.mkdir(parents=True, exist_ok=True)
        attributed.write_text("# alpha's own deliverable — recorded in own log\n")
        os.utime(attributed, (edit_epoch, edit_epoch))

        unattributed = repo / "core" / "scripts" / "unattributed-1426.py"
        unattributed.write_text("# neutral edit with NO own-log entry (orphan-or-gap)\n")
        os.utime(unattributed, (edit_epoch, edit_epoch))

        # Only the attributed file is recorded in alpha's own log.
        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/attributed-1426.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1426-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout

        # Audit fired (deep + >=1 unattributed neutral staged file)
        assert "AUDIT (g-115-1426 over-inclusion)" in combined, \
            f"Over-inclusion audit did NOT fire. combined={combined!r}"
        # Unattributed file flagged
        assert "unattributed (over-inclusion-risk): core/scripts/unattributed-1426.py" in combined, \
            f"Unattributed neutral file not flagged. combined={combined!r}"
        # Own-logged file NOT flagged (per-path attribution discrimination)
        assert "unattributed (over-inclusion-risk): core/scripts/attributed-1426.py" not in combined, \
            f"Own-logged file wrongly flagged as unattributed. combined={combined!r}"
        # Detection-only: both files still staged (audit changes nothing)
        assert "unattributed-1426.py" in combined and "attributed-1426.py" in combined, \
            f"Audit altered staging (must be detection-only). combined={combined!r}"


def test_g1151426_audit_silent_when_all_neutral_attributed():
    """Negative branch: under a deep outcome where EVERY staged neutral file is
    recorded in the committer's own log, the `unattributed` set is empty so NO
    `AUDIT (g-115-1426 over-inclusion)` line is emitted. Pins the audit-silent
    path (guard-502: the empty-set branch must not false-fire on clean work)."""
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        alpha_claim = "2026-05-13T09:00:00"
        shim = _shim_iteration_commit_multi(tmp, {
            "alpha": alpha_claim,
            "zeta": None,
        })
        edit_epoch = _iso_to_epoch(alpha_claim) + 1200
        target = repo / "core" / "scripts" / "all-attributed-1426.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fully-attributed neutral edit (recorded in own log)\n")
        os.utime(target, (edit_epoch, edit_epoch))

        # The ONLY neutral staged file is recorded in alpha's own log.
        _seed_own_uncommitted_log(repo, "alpha", ["core/scripts/all-attributed-1426.py"])

        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1426-02", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo), "--dry-run"],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout

        assert "AUDIT (g-115-1426 over-inclusion)" not in combined, \
            f"Audit false-fired with all neutral files attributed. combined={combined!r}"
        # Deep dry-run still produces the staging plan; the file is staged.
        assert "all-attributed-1426.py" in combined, \
            f"Attributed neutral file missing from staging plan. combined={combined!r}"


def test_g1151498_prestaged_foreign_not_swept_by_pathspec_commit():
    """8: a file PRE-STAGED in the shared index but EXCLUDED from this
    committer's staged_files[] (here via the namespace filter -- agents/zeta/)
    must NOT be swept into the committer's real commit. The cross-agent filters
    gate what iteration-commit `git add`s, but a bare whole-index `git commit`
    committed the ENTIRE index, sweeping a concurrent partner's pre-staged WIP
    regardless (observed g-115-1486 / g-115-1497: alpha's deliverable swept into
    zeta's commit despite the partner-log filter correctly excluding it). The
    `-- "${staged_files[@]}"` pathspec restricts the commit to the intended
    paths; the foreign pre-staged entry is excluded AND left staged for the
    partner's own commit.

    Genuine regression (FAILS pre-fix, PASSES post-fix): under the old whole-
    index `git commit -F -`, the pre-staged agents/zeta/ file lands in HEAD; the
    pathspec scope excludes it. Filter-agnostic -- the same protection holds
    whatever excluded the file from staged_files[] (namespace / partner-log /
    concurrent); namespace is used here for the cleanest deterministic setup.
    Unlike the sibling tests, this one runs a REAL commit (no --dry-run) so it
    inspects the resulting HEAD tree, not the staging plan.
    """
    PROJECT_TMP.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROJECT_TMP) as td:
        tmp = Path(td)
        repo = _setup_repo(tmp)
        # zeta has no in_flight -- isolate to the pre-staged sweep, not the
        # concurrent-partner filter.
        shim = _shim_iteration_commit_multi(
            tmp, {"alpha": "2026-05-13T09:00:00", "zeta": None})

        # alpha's own deliverable (own agent-dir -> always in staged_files[],
        # never namespace-filtered): seed tracked, then modify so git sees ' M'.
        alpha_file = _seed_tracked_file(
            repo, "agents/alpha/deliverable-1498.md", "base\n")
        alpha_file.write_text("base\nalpha deep change\n")

        # FOREIGN partner WIP, pre-staged into the shared index BEFORE alpha's
        # iteration-commit. Under agents/zeta/ -> the namespace filter excludes
        # it from alpha's staged_files[]; the pre-stage puts it in the index.
        foreign = repo / "agents" / "zeta" / "wip-prestaged-1498.md"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("# zeta WIP, pre-staged by zeta mid-commit\n")
        subprocess.run(["git", "add", str(foreign)], cwd=repo, check=True)

        # REAL commit (no --dry-run).
        result = _run_bash(
            [str(shim), "--goal-id", "g-test-1498-01", "--title", "Apply: test",
             "--outcome", "deep", "--repo", str(repo)],
            env={"MIND_AGENT": "alpha"},
        )
        combined = result.stderr + result.stdout
        assert result.returncode == 0, f"iteration-commit failed: {combined!r}"

        head_files = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.split()

        assert "agents/alpha/deliverable-1498.md" in head_files, \
            f"alpha's own deliverable was NOT committed. head_files={head_files!r} combined={combined!r}"
        assert "agents/zeta/wip-prestaged-1498.md" not in head_files, \
            f"FOREIGN pre-staged file was SWEPT into alpha's commit (pathspec scope failed). head_files={head_files!r}"

        # The foreign file must remain staged+uncommitted (left for zeta).
        still_staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert "agents/zeta/wip-prestaged-1498.md" in still_staged, \
            f"Foreign pre-staged file should remain staged for the partner. still_staged={still_staged!r}"


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
    test_g1151413_absolute_own_log_entry_still_exempts()
    test_g115697_partner_uncommitted_log_filters_after_in_flight_cleared()
    test_g1151620_partner_log_exempts_own_authored_double_recorded_path()
    test_g1151426_over_inclusion_audit_flags_unattributed_not_attributed()
    test_g1151426_audit_silent_when_all_neutral_attributed()
    test_g1151498_prestaged_foreign_not_swept_by_pathspec_commit()
    print("All 16 concurrent-partner tests passed (7 g-115-692 + 3 g-115-828 + 1 g-115-1413 + 1 g-115-697/914 + 1 g-115-1620 + 2 g-115-1426 + 1 g-115-1498).")
