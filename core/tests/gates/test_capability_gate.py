# domain-leak-exempt: test fixtures intentionally use literal incident
# strings (insufficient_session_data, roblox_studio_session_required) as
# regression anchors — same exemption as core/scripts/gates/capability.py.
"""Equivalence + behavior tests for capability gate (PR 7a/5).

Eight decision branches: noop / keyword_block / override / evidence_approval /
evidence_error / user_only_exemption / cure_block / session_requirement_block
+ user_keystroke_pass + narrative_framing detection. CLI ↔ module
equivalence + override audit-ledger side effect.

Strategy:
  * Use unique synthetic failure_reasons that don't match any real skill
    for non-keyword-block tests. Capability matching depends on the real
    .claude/skills directory which the CLI hardcodes — synthetic
    failure_reasons make every test deterministic against real-skills
    drift.
  * For keyword_block, use the stable real skill name
    "felt-sense-checkin" as the match anchor.
  * Where a fixture also needs an ACTION VERB, pick one from _STOPWORDS
    ("run", "start", "build"), never a matchable verb like "deploy".
    _extract_keywords drops stopwords from the failure reason, so a
    stopword verb can never be promoted to a matched keyword; a
    non-stopword verb becomes one the moment any skill registers a
    trigger containing it. That is drift this file cannot see coming —
    _entry_tokens tokenizes multi-word triggers into bare words, so a
    new forged skill can add a common verb to the keyword space without
    anyone touching the gate (g-115-6138).
  * Use a fake MIND_AGENT name (test-alpha-zzz) so the CLI doesn't
    inherit a real alpha/bravo/zeta local-paths.conf.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "capability-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# CLI subprocess helper
# ---------------------------------------------------------------------------

def _run_cli(failure_reason: str, *,
             intended_participants: str = "user",
             override_agent_match: str | None = None,
             evidence: str | None = None,
             suggest_unblock: bool = False,
             for_goal_id: str | None = None,
             agent: str = "test-alpha-zzz",
             output: str = "json") -> tuple[int, dict | str, str]:
    args = [sys.executable, str(CLI),
            "--failure-reason", failure_reason,
            "--intended-participants", intended_participants,
            "--output", output]
    if override_agent_match is not None:
        args.extend(["--override-agent-match", override_agent_match])
    if evidence is not None:
        args.extend(["--evidence", evidence])
    if suggest_unblock:
        args.append("--suggest-unblock")
    if for_goal_id is not None:
        args.extend(["--for-goal-id", for_goal_id])
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    proc = subprocess.run(args, env=env, capture_output=True, text=True,
                          check=False)
    if proc.stdout.strip():
        try:
            return proc.returncode, json.loads(proc.stdout), proc.stderr
        except json.JSONDecodeError:
            return proc.returncode, proc.stdout, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def _call_module(failure_reason: str, *,
                 intended_participants: str = "user",
                 override_agent_match: str | None = None,
                 evidence: str | None = None,
                 suggest_unblock: bool = False,
                 for_goal_id: str | None = None,
                 agent: str = "test-alpha-zzz",
                 world_dir: Path | None = None,
                 skills_dir: Path | None = None) -> dict:
    from gates.capability import evaluate
    return evaluate(
        failure_reason,
        intended_participants=intended_participants,
        override_agent_match=override_agent_match,
        evidence_raw=evidence,
        suggest_unblock=suggest_unblock,
        for_goal_id=for_goal_id,
        agent_name=agent,
        world_dir=world_dir,
        skills_dir=skills_dir,
    )


# ---------------------------------------------------------------------------
# No matches → noop
# ---------------------------------------------------------------------------

def test_noop_no_capability_match():
    """Failure reason with no skill-matching tokens → no matches, no block."""
    fr = "the unique-zzz-foobar-baz process exited with code 42"
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["match_count"] == 0
    assert cli["suggested_routing"] == "unknown"
    assert "No agent-provisionable capability matched" in cli["reason"]
    assert rc == 0


# ---------------------------------------------------------------------------
# keyword_block → would_block when intended_participants=user
# ---------------------------------------------------------------------------

def test_keyword_block_fires():
    """Failure reason mentioning a real skill name → match → block."""
    fr = "the felt-sense-checkin process needs to be invoked"
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["would_block"] is True
    assert cli["match_count"] >= 1
    assert any(m.get("skill") == "felt-sense-checkin" for m in cli["matches"])
    assert "felt-sense-checkin" in cli["reason"]
    assert rc == 1


def test_keyword_match_no_block_when_participants_agent():
    """Match exists but intended_participants=agent → no block."""
    fr = "the felt-sense-checkin process needs to be invoked"
    rc, cli, _ = _run_cli(fr, intended_participants="agent")
    mod = _call_module(fr, intended_participants="agent")
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["match_count"] >= 1
    assert rc == 0


# ---------------------------------------------------------------------------
# Override → bypass block; no audit log written for free-text override
# ---------------------------------------------------------------------------

def test_override_bypasses_block():
    fr = "the felt-sense-checkin process needs to be invoked"
    rc, cli, stderr = _run_cli(fr, override_agent_match="manual-override-test")
    mod = _call_module(fr, override_agent_match="manual-override-test")
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["approval_kind"] == "override-agent-match"
    assert cli["override_applied"] == "manual-override-test"
    assert "override applied" in stderr
    assert rc == 0


# ---------------------------------------------------------------------------
# Evidence approval — bypass block; logs to ledger
# ---------------------------------------------------------------------------

def test_evidence_approval_bypasses_block(tmp_path: Path):
    fr = "the felt-sense-checkin process needs to be invoked"
    evidence_json = json.dumps([
        {"type": "rb", "id": "rb-999", "claim": "prior incident covers this"},
    ])
    # Module path: explicit world_dir for the audit-ledger write.
    result = _call_module(fr, evidence=evidence_json, world_dir=tmp_path)
    assert result["would_block"] is False
    assert result["approval_kind"] == "evidence"
    assert result["evidence_applied"] == [
        {"type": "rb", "id": "rb-999", "claim": "prior incident covers this"},
    ]
    assert result["evidence_logged_to"] is not None
    ledger = tmp_path / "blocker-gate-overrides.jsonl"
    entries = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["action"] == "evidence-approval"
    assert e["evidence"] == [
        {"type": "rb", "id": "rb-999", "claim": "prior incident covers this"},
    ]


def test_evidence_not_logged_when_no_matches(tmp_path: Path):
    """Evidence with no matches → no ledger write (no block was averted)."""
    fr = "the unique-zzz-no-match-baz process broke"
    evidence_json = json.dumps([
        {"type": "rb", "id": "rb-999", "claim": "approval"},
    ])
    result = _call_module(fr, evidence=evidence_json, world_dir=tmp_path)
    assert result["match_count"] == 0
    assert result["evidence_logged_to"] is None
    ledger = tmp_path / "blocker-gate-overrides.jsonl"
    assert not ledger.exists()


# ---------------------------------------------------------------------------
# Evidence error → 3-key short-circuit shape, rc=1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_evidence,error_substr", [
    ("not json", "not valid JSON"),
    ("{}", "must be a JSON array"),
    ("[]", "evidence array is empty"),
    ('["string-not-object"]', "is not an object"),
    ('[{"type":"unknown","id":"x","claim":"y"}]', "not in allowed set"),
    ('[{"type":"rb","id":"","claim":"y"}]', "id is missing"),
    ('[{"type":"rb","id":"rb-1","claim":""}]', "claim is missing"),
])
def test_evidence_error_short_circuit(bad_evidence: str, error_substr: str):
    fr = "the felt-sense-checkin process"
    rc, cli, _ = _run_cli(fr, evidence=bad_evidence)
    mod = _call_module(fr, evidence=bad_evidence)
    assert cli == mod
    assert "evidence_error" in cli
    assert error_substr in cli["evidence_error"]
    assert cli["would_block"] is True
    assert rc == 1
    # Confirm the 3-key shape verbatim — no other fields leak through.
    assert set(cli.keys()) == {"would_block", "evidence_error", "reason"}


# ---------------------------------------------------------------------------
# User-only-precondition exemption (no cure)
# ---------------------------------------------------------------------------

def test_user_only_precondition_exempts_match():
    """failure_reason names a user-only precondition WITHOUT a registered
    cure → exemption holds, no block even with keyword match."""
    fr = ("the felt-sense-checkin needs invoking but "
          "roblox_studio_session_required is the actual blocker")
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["user_only_preconditions_detected"] is True
    assert "roblox_studio_session_required" in cli["user_only_precondition_substrings"]
    assert cli["cure_action"] is None
    assert cli["would_block"] is False  # exempted
    assert "exempted" in cli["reason"]
    assert rc == 0


# ---------------------------------------------------------------------------
# Cure-aware exemption — cure overrides the blanket exemption → block fires
# ---------------------------------------------------------------------------

def test_cure_block_overrides_exemption():
    """insufficient_session_data has a registered cure → block fires."""
    fr = ("blocker insufficient_session_data: newest game session "
          "has only 15 cells")
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["user_only_preconditions_detected"] is True
    assert "insufficient_session_data" in cli["user_only_precondition_substrings"]
    assert cli["cure_action"] is not None
    assert "RUN-mode" in cli["cure_action"]
    assert cli["cure_overrides_exemption"] is True
    assert cli["would_block"] is True
    assert "Cure registry overrides" in cli["reason"]
    assert rc == 1


# ---------------------------------------------------------------------------
# Session-requirement classification — agent_provisionable → block
# ---------------------------------------------------------------------------

def test_session_requirement_agent_provisionable_blocks():
    """needs RUN-mode session → agent-provisionable → block."""
    fr = "verification requires fresh RUN-mode game session with >=100 cells"
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["session_requirement_detected"] is True
    assert cli["session_requirement_classification"] == "agent_provisionable"
    assert cli["would_block"] is True
    assert "session-requirement" in cli["reason"].lower() or \
           "Matched session-requirement" in cli["reason"]
    assert rc == 1


def test_session_requirement_user_keystroke_passes():
    """needs F5-Play character-spawn session → user_keystroke_required → pass."""
    fr = ("verification requires fresh RUN-mode game session with "
          "F5-Play character spawn for player character")
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["session_requirement_detected"] is True
    assert cli["session_requirement_classification"] == "user_keystroke_required"
    # Even if keyword matches present, user_keystroke_required vetoes the block.
    assert cli["would_block"] is False
    assert rc == 0


# ---------------------------------------------------------------------------
# Narrative framing — detected but doesn't block on its own
# ---------------------------------------------------------------------------

def test_narrative_framing_detected_but_no_block():
    """Pure narrative ('user must approve X') without keyword match → no block."""
    fr = "the unique-zzz-no-match action where user must approve the change"
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["narrative_framing_detected"] is True
    assert "user must" in cli["narrative_patterns"]
    assert cli["would_block"] is False  # narrative alone is insufficient
    assert rc == 0


def test_narrative_plus_keyword_blocks():
    """Narrative + keyword match → block (existing path).

    Phrasing avoids "before <gerund>" trap (_BEFORE_GERUND_END regex),
    which would otherwise context-disqualify the keyword and prevent
    the block — that disqualification is correct behavior and tested
    separately.
    """
    fr = "user must approve. The felt-sense-checkin module needs invocation."
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert cli["narrative_framing_detected"] is True
    assert cli["would_block"] is True
    assert rc == 1


# ---------------------------------------------------------------------------
# Suggest-unblock payload
# ---------------------------------------------------------------------------

# Shared by the payload test and its invariant pin, so that changing the verb
# here is what the pin actually measures (g-115-6138). The first word is the
# ACTION VERB and must stay in _STOPWORDS — see the pin for why.
_KEYWORD_BLOCK_VERB = "run"
_KEYWORD_BLOCK_FR = f"{_KEYWORD_BLOCK_VERB} the felt-sense-checkin module"


def test_suggest_unblock_keyword_block_payload():
    """suggest_unblock + keyword_block → full payload with title/desc.

    The action verb MUST be a member of _STOPWORDS (here "run"). This test's
    whole purpose is rb-574 — the title is built from the ACTION VERB while
    matched_capability preserves the MATCHED KEYWORD separately — so it only
    has force while those two differ. A non-stopword verb can silently become
    a capability keyword the moment any skill registers a trigger containing
    it, at which point action_verb == matched_keyword and every assertion
    below still passes while proving nothing.

    That is not hypothetical: this fixture read "deploy the felt-sense-checkin
    module" until g-115-6138. Nothing in the gate changed — the forged skill
    efs-file-put (2026-08-11) registered the trigger "deploy file to EFS", and
    _entry_tokens tokenizes trigger phrases into bare words, so "deploy"
    entered the keyword space and outranked the anchor. Re-pinning the
    expectation to "deploy" would have turned this test green and vacuous.
    A stopword verb is structurally immune instead of merely un-collided:
    _extract_keywords drops stopwords from the failure reason, so no registry
    row can ever promote one to a matched keyword.
    """
    fr = _KEYWORD_BLOCK_FR
    verb = _KEYWORD_BLOCK_VERB
    rc, cli, _ = _run_cli(fr, suggest_unblock=True, for_goal_id="g-test-001")
    mod = _call_module(fr, suggest_unblock=True, for_goal_id="g-test-001")
    assert cli == mod
    assert cli["unblock_suggested"] is True
    # action_verb wins over matched_keyword.
    assert cli["unblock_title"] == f"Unblock: {verb} for g-test-001"
    assert "Capability gate matched" in cli["unblock_description"]
    assert cli["matched_capability"]["matched_keyword"] == "felt-sense-checkin"
    # The pin has force only while the two differ (rb-574). If a future edit
    # collapses them, fail HERE with the reason rather than passing silently.
    assert cli["matched_capability"]["matched_keyword"] != verb


def test_suggest_unblock_fixture_verb_is_stopword_immune():
    """g-115-6138 regression pin: the fixture verb above must be a stopword.

    Guards the invariant, not the incident. A verb outside _STOPWORDS is
    eligible to become a capability keyword as soon as any forged skill or
    SKILL.md registers a trigger containing it — the exact drift that broke
    the sibling test. Asserting the property directly means a future author
    who swaps the verb back to a matchable one fails on this line, which
    names the constraint, instead of on an assertion that looks like a
    stale expected string.
    """
    from gates.capability import _IMPERATIVE_VERBS, _STOPWORDS
    # Derived from the SHARED constant, never re-typed — a pin that hardcodes
    # its own copy of the fixture cannot notice the fixture changing, which is
    # the only edit it exists to catch.
    verb = _KEYWORD_BLOCK_VERB
    assert _KEYWORD_BLOCK_FR.split()[0] == verb
    # Matchable as an ACTION VERB (so the title is still built from it)...
    assert verb in _IMPERATIVE_VERBS
    # ...but never extractable as a CAPABILITY KEYWORD from a failure reason.
    assert verb in _STOPWORDS
    # The verb the incident used is the counter-example that motivated this.
    assert "deploy" in _IMPERATIVE_VERBS and "deploy" not in _STOPWORDS


def test_suggest_unblock_cure_block_payload():
    """suggest_unblock + cure_block → cure_action wins as title."""
    fr = "blocker insufficient_session_data"
    rc, cli, _ = _run_cli(fr, suggest_unblock=True, for_goal_id="g-test-002")
    mod = _call_module(fr, suggest_unblock=True, for_goal_id="g-test-002")
    assert cli == mod
    assert cli["unblock_suggested"] is True
    # Title uses cure_action verbatim (overrides action_verb scan).
    assert "RUN-mode" in cli["unblock_title"]
    assert "g-test-002" in cli["unblock_title"]


def test_suggest_unblock_no_block_returns_false():
    """suggest_unblock + would_block=False → only unblock_suggested=False."""
    fr = "the unique-zzz-no-match-baz process"
    rc, cli, _ = _run_cli(fr, suggest_unblock=True)
    mod = _call_module(fr, suggest_unblock=True)
    assert cli == mod
    assert cli["unblock_suggested"] is False
    assert "unblock_title" not in cli
    assert "unblock_description" not in cli


def test_suggest_unblock_unset_omits_payload():
    """Without --suggest-unblock, none of the unblock_* fields appear.

    Shares the blocking fixture above rather than duplicating the literal.
    This case is immune to the g-115-6138 drift on its own terms — it asserts
    field ABSENCE, which holds for any input — but it is only non-vacuous
    while the input WOULD have produced a payload with the flag on, so it
    wants the same known-blocking string, not an arbitrary one.
    """
    fr = _KEYWORD_BLOCK_FR
    rc, cli, _ = _run_cli(fr)
    mod = _call_module(fr)
    assert cli == mod
    assert "unblock_suggested" not in cli
    assert "unblock_title" not in cli


# ---------------------------------------------------------------------------
# CLI error paths
# ---------------------------------------------------------------------------

def test_missing_failure_reason_returns_2():
    """argparse exits 2 on missing --failure-reason."""
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "--failure-reason" in proc.stderr


# ---------------------------------------------------------------------------
# Telemetry decision field is consistent (smoke check via stderr)
# ---------------------------------------------------------------------------

def test_human_output_format():
    fr = "the unique-zzz-no-match-baz process"
    args = [sys.executable, str(CLI),
            "--failure-reason", fr, "--output", "human"]
    env = os.environ.copy()
    env["MIND_AGENT"] = "test-alpha-zzz"
    proc = subprocess.run(args, env=env, capture_output=True, text=True,
                          check=False)
    assert proc.returncode == 0
    assert "Keywords:" in proc.stdout
    assert "Sources scanned:" in proc.stdout
    assert "Would block: False" in proc.stdout
