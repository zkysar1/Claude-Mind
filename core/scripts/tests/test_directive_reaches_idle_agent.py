"""A directive reaches an ALL-BLOCKED agent ().

Measured 2026-09-03 on a single-agent deployment: a user scope directive posted with
--type directive sat unacknowledged and unmarked for four hours because
aspirations-select returned at its all_blocked branch BEFORE Phase 2.07 (the only
scan/ack site), and the all-blocked handler's B0 read only escalation and
review-request posts, so the generator never saw it. These tests pin the three
surfaces: the all_blocked branch acks before it returns, the handler carries active
directives from B0 through B1 into B2 as generation scope, and the convention
documents the form (and that target:<agent-name> matches nothing).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SELECT = REPO / ".claude/skills/aspirations-select/SKILL.md"
ALL_BLOCKED = REPO / ".claude/skills/aspirations-all-blocked/SKILL.md"
COORD = REPO / "core/config/conventions/coordination.md"

ACK_READ = "--type directive --since 24h --unread-only --mark-read --json"
HONOR_READ = "board-read.sh --channel coordination --type directive --since 24h --json"
ALL_BLOCKED_RETURN = 'RETURN (goal = None, selection_reason = "all_blocked", selection_context = parsed_output)'


def test_select_acks_and_hands_off_directives_before_the_all_blocked_return():
    text = SELECT.read_text(encoding="utf-8")
    ret = text.index(ALL_BLOCKED_RETURN)
    branch = text[text.index('"all_blocked": true'):ret]
    assert ACK_READ in branch, "the all_blocked branch must run the dedup ACK read before returning"
    assert 'acknowledged,{AGENT_NAME}' in branch, "the ack post shape must match Phase 2.07"
    assert "parsed_output.active_directives = " + HONOR_READ in branch, \
        "the active set must be handed to the all-blocked handler on selection_context"


def test_phase_2_07_still_carries_both_reads_with_different_scopes():
    text = SELECT.read_text(encoding="utf-8")
    p207 = text[text.index("### Phase 2.07"):]
    assert "all_directives = " + HONOR_READ in p207          # HONOR: no --unread-only, no --mark-read
    assert "new_directives = board-read.sh --channel coordination " + ACK_READ in p207  # ACK: dedup
    assert "g-115-2990" in p207


def test_all_blocked_handler_reads_directives_as_generation_scope():
    text = ALL_BLOCKED.read_text(encoding="utf-8")
    b0 = text[text.index("## Step B0"):text.index("## Step B1")]
    # The handler acks too (dedup read, same shape) — a digest may route all_blocked here
    # without paging aspirations-select, and the sender must still be answered.
    assert "new_directives = board-read.sh --channel coordination " + ACK_READ in b0
    assert 'acknowledged,{AGENT_NAME}' in b0
    assert "selection_context.active_directives or " + HONOR_READ in b0
    assert '"directive_type:veto" not in d.tags' in b0
    b1 = text[text.index("## Step B1"):text.index("## Step B2")]
    ctx = b1[b1.index("constraint_context = {"):]
    assert "directives: [{id: d.id, author: d.author, text: d.text, tags: d.tags} for d in active_directives]" in ctx
    b2 = text[text.index("## Step B2"):]
    after_invoke = b2[b2.index("invoke /create-aspiration from-self --plan with: constraint_context"):]
    assert "origin_signal user_directive:<msg-id>" in after_invoke
    assert "not supply evidence" in after_invoke   # scope never relaxes the supply gate


def test_coordination_convention_documents_the_generation_scope_form():
    text = COORD.read_text(encoding="utf-8")
    assert "generation_scope" in text
    assert "`target:<agent-name>` matches NOTHING" in text
    assert "all_blocked early-return branch runs the same ACK read" in text
    assert "### Generation-scope directives" in text
