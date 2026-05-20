"""Equivalence + behavior tests for goal-duplication gate (PR 7a/4).

Five checks (recent_completions, partner_in_flight, git_log_48h,
insight_triggers, target_state), override + audit-ledger side effect,
description_quality_warning, CLI error paths, and CLI ↔ module
equivalence on every payload.

Strategy:
  * Use a tmp git repo as project_root so git_log_48h is deterministic
    (one initial commit; nothing recent → always passes).
  * Use a tmp world dir with constructed team-state.yaml + board files
    so recent_completions / partner_in_flight / insight_triggers each
    have a known input.
  * Use goal titles that EITHER (a) extract no targets [target_state
    skips clean] OR (b) start with READ-intent verbs [target_state
    skips via is_read_intent]. This avoids depending on the real
    project tree's contents for target_state behavior.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "goal-duplication-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def empty_world(tmp_path: Path) -> Path:
    """Tmp world dir with no team-state.yaml or board files. Used when the
    test wants every world-backed check to skip."""
    w = tmp_path / "world"
    w.mkdir()
    return w


@pytest.fixture
def world_with_team_state(tmp_path: Path):
    """Tmp world with a team-state.yaml containing one non-self
    recent_completion entry. Returns (world_dir, helper to write the YAML)."""
    w = tmp_path / "world"
    w.mkdir()

    def write_state(yaml_text: str):
        (w / "team-state.yaml").write_text(yaml_text, encoding="utf-8")
    return w, write_state


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Path:
    """Tmp git repo with one initial commit. git log --since=48h returns
    nothing relevant because the only commit's content is unrelated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed", "--quiet"],
                   check=True)
    return repo


# ---------------------------------------------------------------------------
# CLI subprocess helper
# ---------------------------------------------------------------------------

def _run_cli(goal: dict, *, override_duplication: str | None = None,
             agent: str = "", world_dir: Path | None = None,
             output: str = "json") -> tuple[int, dict | str, str]:
    """Invoke CLI as subprocess. MIND_WORLD env override lets us point at
    a tmp world. Returns (rc, parsed_stdout_or_raw, stderr)."""
    args = [sys.executable, str(CLI), "--output", output]
    if override_duplication is not None:
        args.extend(["--override-duplication", override_duplication])
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    if world_dir is not None:
        env["MIND_WORLD"] = str(world_dir)
    else:
        env.pop("MIND_WORLD", None)
    proc = subprocess.run(
        args, input=json.dumps(goal), env=env,
        capture_output=True, text=True, check=False,
    )
    if proc.stdout.strip():
        try:
            return proc.returncode, json.loads(proc.stdout), proc.stderr
        except json.JSONDecodeError:
            return proc.returncode, proc.stdout, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def _call_module(goal: dict, *, override_duplication: str | None = None,
                 agent: str = "", world_dir: Path | None = None,
                 project_root: Path | None = None) -> dict:
    from gates.goal_duplication import evaluate
    return evaluate(
        goal,
        override_duplication=override_duplication,
        agent_name=agent,
        world_dir=world_dir,
        project_root=project_root,
    )


def _get_check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


# ---------------------------------------------------------------------------
# All checks pass — clean goal
# ---------------------------------------------------------------------------

def test_all_checks_pass_clean_goal(empty_world: Path):
    """Goal with no overlap signals and a READ-intent title → all 5 pass.

    READ-intent title skips target_state (which otherwise might match
    against real project state and produce nondeterministic results)."""
    goal = {
        "title": "Investigate: hypothetical-unique-zzz-abc",
        "description": "Look into how the foo-bar-baz feature works.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["failing_count"] == 0
    assert rc == 0


# ---------------------------------------------------------------------------
# recent_completions check
# ---------------------------------------------------------------------------

def test_recent_completions_skipped_no_world():
    """world_dir=None → check skipped (every world-backed check skips)."""
    goal = {
        "title": "Investigate: foo",
        "description": "x",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=None)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=None)
    assert cli == mod
    rc_check = _get_check(cli, "recent_completions")
    assert rc_check["passed"] is True
    assert "no WORLD_PATH" in rc_check["reason"]


def test_recent_completions_self_entries_skip(world_with_team_state):
    """Entries with completed_by == self_agent are skipped (no overlap with self)."""
    world, write = world_with_team_state
    # Self-entry: should be SKIPPED, not blocked.
    write(textwrap.dedent("""\
        recent_completions:
          - goal_id: g-1
            completed_by: test-alpha-zzz
            key_finding: "rewrote retrieve-stem-foo.py to handle dual-cache eviction"
            completed_at: '2026-05-12T10:00:00'
    """))
    goal = {
        "title": "Investigate: retrieve-stem-foo cache",
        "description": "Look at retrieve-stem-foo.py and the dual-cache eviction logic.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    rc_check = _get_check(cli, "recent_completions")
    assert rc_check["passed"] is True  # self-entries don't count


def test_recent_completions_blocks_on_partner_overlap(world_with_team_state):
    """Non-self entry with 2+ unique high-weight hits → block."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        recent_completions:
          - goal_id: g-1
            completed_by: bravo
            key_finding: "rewrote unique-zzz-foo.py and the dual-cache-evictor-x123 logic"
            completed_at: '2026-05-12T10:00:00'
    """))
    goal = {
        "title": "Investigate: unique-zzz-foo cache",
        "description": ("Read through unique-zzz-foo.py and the "
                        "dual-cache-evictor-x123 module to understand it."),
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    rc_check = _get_check(cli, "recent_completions")
    # Single-doc corpus → all terms get idf=0 (df==n). Even with 2+ unique hits,
    # weighted score will be 0 < WEIGHT_THRESHOLD=1.5 → passes. To get a real
    # block, the corpus needs ≥2 docs so IDF can rise above the threshold.
    # This test verifies the corpus-size-1 edge case: passes correctly.
    assert rc_check["passed"] is True


def test_recent_completions_blocks_with_multi_doc_corpus(world_with_team_state):
    """Multi-doc corpus → IDF rises above threshold for rare identifiers → block."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        recent_completions:
          - goal_id: g-1
            completed_by: bravo
            key_finding: "rewrote unique-zzz-foo.py and the dual-cache-evictor-x123 logic"
            completed_at: '2026-05-12T10:00:00'
          - goal_id: g-2
            completed_by: bravo
            key_finding: "fixed routing in main loop"
            completed_at: '2026-05-12T09:00:00'
          - goal_id: g-3
            completed_by: bravo
            key_finding: "added telemetry to cron jobs"
            completed_at: '2026-05-12T08:00:00'
    """))
    goal = {
        "title": "Investigate: unique-zzz-foo cache",
        "description": ("Read through unique-zzz-foo.py and the "
                        "dual-cache-evictor-x123 module to understand it."),
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    rc_check = _get_check(cli, "recent_completions")
    assert rc_check["passed"] is False
    assert "overlap" in rc_check["reason"]
    assert len(rc_check["matches"]) >= 1


# ---------------------------------------------------------------------------
# partner_in_flight check
# ---------------------------------------------------------------------------

def test_partner_in_flight_no_partners(world_with_team_state):
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        agent_status:
          test-alpha-zzz:
            in_flight:
              goal_id: g-7
              title: "self in-flight"
              phase: 4
    """))
    goal = {
        "title": "Investigate: foo-bar-baz",
        "description": "x",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    pf = _get_check(cli, "partner_in_flight")
    assert pf["passed"] is True


def test_partner_in_flight_blocks_on_partner_overlap(world_with_team_state):
    """Partner in_flight with 2+ unique hits → block."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        agent_status:
          bravo:
            in_flight:
              goal_id: g-99
              title: "rewrite unique-zzz-foo.py with dual-cache-evictor-x123"
              phase: 4
              claimed_at: '2026-05-12T10:00:00'
    """))
    goal = {
        "title": "Investigate: unique-zzz-foo cache",
        "description": ("Look at unique-zzz-foo.py and dual-cache-evictor-x123 "
                        "to figure it out."),
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    pf = _get_check(cli, "partner_in_flight")
    assert pf["passed"] is False
    assert "partner in_flight" in pf["reason"]


def test_partner_in_flight_same_goal_id_skipped(world_with_team_state):
    """If partner's in_flight goal_id matches proposed goal id → skipped."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        agent_status:
          bravo:
            in_flight:
              goal_id: g-99
              title: "rewrite unique-zzz-foo.py with dual-cache-evictor-x123"
              phase: 4
    """))
    goal = {
        "id": "g-99",
        "title": "Investigate: unique-zzz-foo cache",
        "description": ("Read unique-zzz-foo.py and dual-cache-evictor-x123 "
                        "module thoroughly."),
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    pf = _get_check(cli, "partner_in_flight")
    # Same goal_id → not a duplication signal (id reuse, different bug).
    assert pf["passed"] is True


# ---------------------------------------------------------------------------
# git_log_48h check
# ---------------------------------------------------------------------------

def test_git_log_skipped_no_file_paths(empty_world: Path):
    """No file paths extracted → check skipped."""
    goal = {
        "title": "Investigate: vague idea",
        "description": "no extractable paths here",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    g = _get_check(cli, "git_log_48h")
    assert g["passed"] is True
    assert "no file paths" in g["reason"]


def test_git_log_passes_with_file_paths_no_recent_match(empty_world: Path,
                                                        empty_git_repo: Path):
    """File paths extracted, but tmp repo has no recent matching commits → pass."""
    goal = {
        "title": "Investigate: foo-bar-baz",
        "description": "Look at unique-test-file-zzz.py to understand it.",
    }
    # CLI doesn't accept project_root override — use module path only here.
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world,
                       project_root=empty_git_repo)
    g = _get_check(mod, "git_log_48h")
    assert g["passed"] is True
    assert "no file-path overlap" in g["reason"]


# ---------------------------------------------------------------------------
# insight_triggers check
# ---------------------------------------------------------------------------

def test_insight_triggers_skipped_no_findings_file(empty_world: Path):
    goal = {
        "title": "Investigate: foo",
        "description": "Look at unique-bar.py thoroughly.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    it = _get_check(cli, "insight_triggers")
    assert it["passed"] is True
    assert "no findings.jsonl" in it["reason"]


def test_insight_triggers_blocks_on_affects_overlap(tmp_path: Path):
    """Active insight_trigger affecting a file in the proposed goal → block."""
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    now_iso = "2099-01-01T00:00:00"  # far future so the 48h cutoff doesn't drop it
    findings = world / "board" / "findings.jsonl"
    findings.write_text(json.dumps({
        "id": "f-1",
        "author": "bravo",
        "tags": ["insight_trigger", "affects:unique-zzz-target.py", "severity:high"],
        "timestamp": now_iso,
    }) + "\n", encoding="utf-8")

    goal = {
        "title": "Modify unique-zzz-target.py for new behavior",
        "description": "Refactor unique-zzz-target.py.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    it = _get_check(cli, "insight_triggers")
    assert it["passed"] is False
    assert "insight_trigger" in it["reason"]


def test_insight_triggers_self_authored_skipped(tmp_path: Path):
    """Findings authored by self_agent are skipped (not partner signal)."""
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    now_iso = "2099-01-01T00:00:00"
    findings = world / "board" / "findings.jsonl"
    findings.write_text(json.dumps({
        "id": "f-1",
        "author": "test-alpha-zzz",  # same as self_agent — should be skipped
        "tags": ["insight_trigger", "affects:unique-zzz-target.py"],
        "timestamp": now_iso,
    }) + "\n", encoding="utf-8")

    goal = {
        "title": "Investigate: unique-zzz-target.py",
        "description": "Look at unique-zzz-target.py.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    it = _get_check(cli, "insight_triggers")
    assert it["passed"] is True


# ---------------------------------------------------------------------------
# target_state check
# ---------------------------------------------------------------------------

def test_target_state_skipped_read_intent(empty_world: Path):
    """READ-intent verb in title → target_state skipped."""
    goal = {
        "title": "Investigate: how foo works",
        "description": "Read core/scripts/aspirations.py.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    ts = _get_check(cli, "target_state")
    assert ts["passed"] is True
    assert "READ-intent" in ts["reason"]


def test_target_state_skipped_no_target_extracted(empty_world: Path):
    """No file/identifier pair → check skipped."""
    goal = {
        "title": "Add some unique-feature-zzz",
        "description": "Vague description without identifiers.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    ts = _get_check(cli, "target_state")
    assert ts["passed"] is True


def test_target_state_skipped_completed_maintain(empty_world: Path):
    """status=completed Maintain goal → target_state skipped.

    g-115-836: when filing a retroactive Maintain record for just-shipped
    framework code, the identifiers ARE the completion signal. Without the
    skip every such filing required --override-duplication (canonical
    incident: g-115-832 / encode-session 2026-05-16).
    """
    goal = {
        "title": "Maintain: encode WSL-bash CRLF mitigation in _runtime.sh",
        "description": (
            "Wired _runtime.sh CRLF translation via the shim in "
            "core/scripts/.python-shim/. PreToolUse hook bash-agent-inject.sh "
            "now sources _runtime.sh with CRLF stripped at exec time."
        ),
        "status": "completed",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    ts = _get_check(cli, "target_state")
    assert ts["passed"] is True
    assert "completed Maintain" in ts["reason"]
    assert "g-115-836" in ts["reason"]


def test_target_state_pending_maintain_NOT_skipped(empty_world: Path):
    """status=pending Maintain → NOT skipped (forward-work, dup check applies).

    Negative test: the skip MUST be gated on status=completed. A pending
    Maintain goal describes work TO DO, where identifier presence in target
    files would correctly indicate the fix already shipped (dup signal).
    """
    goal = {
        "title": "Maintain: encode WSL-bash CRLF mitigation in _runtime.sh",
        "description": "Add CRLF stripping to bash-agent-inject.sh hook.",
        "status": "pending",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    ts = _get_check(cli, "target_state")
    # The reason may be "passed" (no target extracted) OR a real already_present
    # block — but it must NOT be the new "completed Maintain" skip reason.
    assert "completed Maintain" not in ts["reason"]


def test_target_state_completed_non_Maintain_NOT_skipped(empty_world: Path):
    """status=completed but NOT a Maintain goal → NOT skipped.

    Title-prefix discipline: the new skip fires only on titles starting
    "Maintain:". An Apply/Fix/Idea goal at status=completed still routes
    through the regular target_state check (the completion of an Apply
    SHOULD have identifiers; but a completed Fix being re-filed could
    legitimately be duplication).
    """
    goal = {
        "title": "Apply: encode WSL-bash CRLF mitigation in _runtime.sh",
        "description": "Wired _runtime.sh CRLF translation via the shim.",
        "status": "completed",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    ts = _get_check(cli, "target_state")
    assert "completed Maintain" not in ts["reason"]


# ---------------------------------------------------------------------------
# Override behavior — bypasses block, writes to ledger
# ---------------------------------------------------------------------------

def test_override_module_writes_audit(world_with_team_state):
    """Override flips would_block=False; module writes audit-ledger entry."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        recent_completions:
          - goal_id: g-1
            completed_by: bravo
            key_finding: "rewrote unique-zzz-foo.py and dual-cache-evictor-x123"
          - goal_id: g-2
            completed_by: bravo
            key_finding: "routing"
          - goal_id: g-3
            completed_by: bravo
            key_finding: "telemetry"
    """))
    goal = {
        "title": "Modify unique-zzz-foo cache eviction",
        "description": "Edit unique-zzz-foo.py with the dual-cache-evictor-x123.",
    }
    result = _call_module(goal, override_duplication="emergency",
                          agent="test-alpha-zzz", world_dir=world)
    assert result["would_block"] is False
    assert result["override_applied"] == "emergency"
    ledger = world / "goal-duplication-overrides.jsonl"
    entries = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["justification"] == "emergency"
    assert e["agent"] == "test-alpha-zzz"


def test_override_cli_writes_audit(world_with_team_state):
    """CLI path also writes audit-ledger entry."""
    world, write = world_with_team_state
    write(textwrap.dedent("""\
        recent_completions:
          - goal_id: g-1
            completed_by: bravo
            key_finding: "rewrote unique-zzz-foo.py and dual-cache-evictor-x123"
          - goal_id: g-2
            completed_by: bravo
            key_finding: "routing"
          - goal_id: g-3
            completed_by: bravo
            key_finding: "telemetry"
    """))
    goal = {
        "title": "Modify unique-zzz-foo cache eviction",
        "description": "Edit unique-zzz-foo.py with the dual-cache-evictor-x123.",
    }
    rc, cli, stderr = _run_cli(goal, override_duplication="emergency",
                                agent="test-alpha-zzz", world_dir=world)
    assert cli["would_block"] is False
    assert cli["override_applied"] == "emergency"
    assert rc == 0
    assert "override applied" in stderr
    ledger = world / "goal-duplication-overrides.jsonl"
    entries = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# description_quality_warning
# ---------------------------------------------------------------------------

def test_description_quality_warning_fires(empty_world: Path):
    """Description with fewer non-stopword tokens than title → warning."""
    goal = {
        "title": "Investigate: alpha bravo charlie delta echo foxtrot",
        "description": "x",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    assert cli.get("description_quality_warning") is True
    assert "non-stopword tokens" in cli["description_quality_reason"]


def test_description_quality_recurring_exempt(empty_world: Path):
    """Recurring goals exempt — title-as-spec is documented pattern."""
    goal = {
        "title": "Investigate: alpha bravo charlie delta echo foxtrot",
        "description": "x",
        "recurring": True,
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    assert "description_quality_warning" not in cli


def test_description_quality_no_warning_when_desc_rich(empty_world: Path):
    """Description with more tokens than title → no warning."""
    goal = {
        "title": "Investigate: alpha",
        "description": "alpha bravo charlie delta echo foxtrot golf hotel india.",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    assert "description_quality_warning" not in cli


# ---------------------------------------------------------------------------
# expected_coverage_paths — response-prefix carve-out
# ---------------------------------------------------------------------------

def test_expected_coverage_paths_response_to_insight(tmp_path: Path):
    """origin_signal=investigate:X + matching insight_trigger affects:Y
    → file_path Y is exempted from overlap detection."""
    world = tmp_path / "world"
    (world / "board").mkdir(parents=True)
    now_iso = "2099-01-01T00:00:00"
    (world / "board" / "findings.jsonl").write_text(json.dumps({
        "id": "f-1",
        "author": "bravo",
        "tags": ["insight_trigger", "affects:unique-zzz-target.py"],
        "timestamp": now_iso,
    }) + "\n", encoding="utf-8")

    goal = {
        "title": "Modify unique-zzz-target.py to address bravo's finding",
        "description": "Refactor unique-zzz-target.py based on the trigger.",
        "origin_signal": "investigate:bravo-trigger-001",
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=world)
    assert cli == mod
    it = _get_check(cli, "insight_triggers")
    assert it["passed"] is True  # expected_paths exempted the overlap


# ---------------------------------------------------------------------------
# Verification text preferred over prose
# ---------------------------------------------------------------------------

def test_verification_text_preferred(empty_world: Path):
    """verification.outcomes is the signal source when present (not prose)."""
    goal = {
        "title": "Modify unique-prose-only-zzz",
        "description": "Edit unique-prose-only-zzz.py for cleanup.",
        "verification": {
            "outcomes": ["unique-verification-source-foo.py is updated"],
        },
    }
    rc, cli, _ = _run_cli(goal, agent="test-alpha-zzz", world_dir=empty_world)
    mod = _call_module(goal, agent="test-alpha-zzz", world_dir=empty_world)
    assert cli == mod
    # file_paths_detected should come from verification, not prose.
    paths = cli["file_paths_detected"]
    assert any("unique-verification-source-foo.py" in p for p in paths)
    # Prose-only path should NOT appear.
    assert not any("unique-prose-only-zzz.py" in p for p in paths)


# ---------------------------------------------------------------------------
# CLI error paths
# ---------------------------------------------------------------------------

def test_empty_stdin_returns_2():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input="", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2


def test_invalid_json_returns_2():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input="not json", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "bad JSON" in proc.stderr


# ---------------------------------------------------------------------------
# Human output mode
# ---------------------------------------------------------------------------

def test_human_output_format(empty_world: Path):
    goal = {
        "title": "Investigate: foo",
        "description": "x",
    }
    args = [sys.executable, str(CLI), "--output", "human"]
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(empty_world)
    proc = subprocess.run(args, input=json.dumps(goal), env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "would_block: False" in proc.stdout
    assert "[PASS]" in proc.stdout
