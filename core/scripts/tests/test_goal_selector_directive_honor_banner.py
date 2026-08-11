"""test_goal_selector_directive_honor_banner.py -- .

Pins emit_directive_honor_banner in goal-selector.py: the compaction-proof
bash-side companion to aspirations-select Phase 2.07's LLM-executed
DIRECTIVE-HONOR hard rule (guard-1310). It emits a LOUD stderr banner (and
returns the warning list) when an active, UNACKED directive DIRECTED AT the
running agent targets a goal PRESENT in the scored candidate list.

Origin: the 2026-07-20 miss (g-115-2797) -- a user directive targeting zeta
(g-315-390) was lane-skipped 5+ times over 8h with 0 acks / 0 read-receipts.
The Phase 2.07 LLM rule is skippable post-autocompact; goal-selector.py runs
every iteration ("goal-selector.sh MUST run every iteration"), so a stderr
banner here cannot be summarized away by compaction.

Contracts pinned:
  * FIRES: unacked directive (bare agent-name tag) targeting a scored goal.
  * SUPPRESSED by ack: agent has a reply_to the directive (plain ack OR a
    justified-deferral ack) -> no warning.
  * requires_action_by:<agent> tag also counts as "directed at agent".
  * NOT directed at this agent -> no warning.
  * Target absent from scored (blocked/precondition-gated) -> no warning.
  * Expired directive -> no warning.
  * Fail-open: missing board file / empty scored / empty agent -> returns [].

Daemon-safe (no daemon_integration marker): every test injects board_path=tmp
file, so the real coordination board is never touched and no daemon spawns.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("goal_selector_dhb", "goal-selector.py")


def _board(tmp_path, rows):
    p = tmp_path / "coordination.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _scored(*goal_ids):
    return [{"goal_id": g, "score": round(10.0 - i, 2)} for i, g in enumerate(goal_ids)]


DIRECTIVE = {
    "id": "msg-dir-1", "author": "alpha", "type": "directive",
    "channel": "coordination",
    "tags": ["directive", "target:g-315-390", "weight:+2.0", "zeta", "user-directive"],
    "text": "USER DIRECTIVE: zeta -- prioritize g-315-390.",
}


def test_fires_on_unacked_directive_targeting_scored_goal(tmp_path):
    bp = _board(tmp_path, [DIRECTIVE])
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390", "g-999-1"), "zeta", board_path=bp)
    assert len(warns) == 1
    assert warns[0]["directive_id"] == "msg-dir-1"
    assert warns[0]["goal_id"] == "g-315-390"
    assert warns[0]["rank"] == 1


def test_suppressed_by_plain_ack(tmp_path):
    ack = {"id": "msg-ack-1", "author": "zeta", "type": "status",
           "reply_to": "msg-dir-1", "tags": ["acknowledged", "zeta"], "text": "ack"}
    bp = _board(tmp_path, [DIRECTIVE, ack])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []


def test_suppressed_by_justified_deferral_ack(tmp_path):
    # A justified-deferral ack is still a reply_to the directive -> honored.
    jd = {"id": "msg-jd-1", "author": "zeta", "type": "status",
          "reply_to": "msg-dir-1", "tags": ["justified-deferral", "guard-1310"],
          "text": "justified-deferral: blocked on recordings"}
    bp = _board(tmp_path, [DIRECTIVE, jd])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []


def test_ack_by_other_agent_does_not_suppress(tmp_path):
    # bravo replying to the directive must NOT suppress zeta's banner.
    other = {"id": "msg-ack-2", "author": "bravo", "type": "status",
             "reply_to": "msg-dir-1", "tags": ["acknowledged", "bravo"], "text": "ack"}
    bp = _board(tmp_path, [DIRECTIVE, other])
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp)
    assert len(warns) == 1


def test_requires_action_by_tag_directs(tmp_path):
    d = {**DIRECTIVE,
         "tags": ["directive", "target:g-315-390", "weight:+2.0",
                  "requires_action_by:zeta"],
         "text": "USER DIRECTIVE: prioritize g-315-390."}
    bp = _board(tmp_path, [d])
    assert len(gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp)) == 1


def test_not_directed_at_this_agent(tmp_path):
    d = {"id": "msg-dir-2", "author": "alpha", "type": "directive",
         "tags": ["directive", "target:g-777-1", "weight:+2.0", "bravo"],
         "text": "USER DIRECTIVE: bravo -- prioritize g-777-1."}
    bp = _board(tmp_path, [d])
    assert gs.emit_directive_honor_banner(
        _scored("g-777-1"), "zeta", board_path=bp) == []


def test_target_not_in_scored(tmp_path):
    #  blocked -> absent from scored -> no banner (the executable-only gate).
    bp = _board(tmp_path, [DIRECTIVE])
    assert gs.emit_directive_honor_banner(
        _scored("g-999-1", "g-888-2"), "zeta", board_path=bp) == []


def test_expired_directive(tmp_path):
    d = {**DIRECTIVE, "tags": DIRECTIVE["tags"] + ["expires:2000-01-01T00:00:00"]}
    bp = _board(tmp_path, [d])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []


def test_multiple_targets_report_each_in_scored(tmp_path):
    d = {**DIRECTIVE,
         "tags": ["directive", "target:g-315-390", "target:g-315-391",
                  "weight:+2.0", "zeta"]}
    bp = _board(tmp_path, [d])
    # Only  is scored;  is gated (absent) -> exactly one warning.
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp)
    assert [w["goal_id"] for w in warns] == ["g-315-390"]
    # Both scored -> two warnings.
    warns2 = gs.emit_directive_honor_banner(
        _scored("g-315-390", "g-315-391"), "zeta", board_path=bp)
    assert sorted(w["goal_id"] for w in warns2) == ["g-315-390", "g-315-391"]


def test_fail_open_missing_board(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=missing) == []


def test_empty_scored_or_agent_noop(tmp_path):
    bp = _board(tmp_path, [DIRECTIVE])
    assert gs.emit_directive_honor_banner([], "zeta", board_path=bp) == []
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "", board_path=bp) == []


# ── : explicit routing tag beats a loose prose agent-mention ──

def test_explicit_routing_tag_beats_exclusionary_prose_mention(tmp_path):
    """A directive routed to alpha (requires_action_by:alpha) that names bravo
    in an exclusionary prose clause must NOT flag bravo — the live incident
    msg-20260721-211141-bravo-5456 shape. The routed target (alpha) IS flagged.
    """
    directive = {
        "id": "msg-excl-1", "author": "alpha", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "weight:+2.0",
                 "requires_action_by:alpha"],
        "text": "alpha please claim g-315-390; bravo cannot deploy it well.",
    }
    bp = _board(tmp_path, [directive])
    # bravo is named in prose but routed-away -> NO warning (the fix).
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "bravo", board_path=bp) == []
    # alpha, the explicitly-routed target, IS still flagged.
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390"), "alpha", board_path=bp)
    assert [w["goal_id"] for w in warns] == ["g-315-390"]


def test_self_authored_prose_mention_not_flagged(tmp_path):
    """A directive whose AUTHOR names themselves in prose but routes the work
    to another agent must not flag the author (same trap, g-115-2870)."""
    directive = {
        "id": "msg-self-1", "author": "bravo", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "weight:+2.0",
                 "requires_action_by:alpha"],
        "text": "bravo is handing g-315-390 to alpha; alpha owns it now.",
    }
    bp = _board(tmp_path, [directive])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "bravo", board_path=bp) == []


def test_prose_only_directive_still_directs_via_fallback(tmp_path):
    """When a directive carries NO explicit routing tag, the prose-mention
    fallback still fires — the fix only SUPPRESSES prose when a routing tag is
    present, preserving pre-fix behavior for un-tagged directives."""
    directive = {
        "id": "msg-prose-1", "author": "alpha", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "weight:+2.0"],
        "text": "foxtrot -- please prioritize g-315-390 next.",
    }
    bp = _board(tmp_path, [directive])
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390"), "foxtrot", board_path=bp)
    assert [w["goal_id"] for w in warns] == ["g-315-390"]


# ── : @env-QUALIFIED addressing ────────────────────────────────────
# Mirrors test_insight_trigger_sweep_addressing.py pins 3 + 4 for THIS consumer.
# The sweep REFUSES a bare collision-set name and tells the poster to qualify it;
# before these pins, the qualified form the sweep recommends compared unequal
# here and was dropped in SILENCE. Bare loud upstream + qualified silent
# downstream is a pincer, so qualifying posts to satisfy the sweep would have
# traded a visible failure for an invisible one.
#
# Per guard-2860 the carve-out pin is the LEAST valuable of these four: it
# cannot fail in the dangerous direction. The load-bearing ones are the
# exclusions — a peer deployment's same-named agent, and an agent whose name
# merely shares a prefix.

def _qualified_directive(target_tag):
    return {
        "id": "msg-qual-1", "author": "alpha", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "weight:+2.0", target_tag],
        "text": "USER DIRECTIVE: prioritize g-315-390.",
    }


def test_qualified_self_env_tag_directs(tmp_path, monkeypatch):
    """`requires_action_by:zeta@<self-env>` IS this agent — the defect."""
    monkeypatch.setattr(gs, "ENVIRONMENT_ID", "ayoai-mind")
    bp = _board(tmp_path, [_qualified_directive(
        "requires_action_by:zeta@ayoai-mind")])
    warns = gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp)
    assert [w["goal_id"] for w in warns] == ["g-315-390"]


def test_qualified_peer_env_tag_does_not_direct(tmp_path, monkeypatch):
    """`zeta@<peer-env>` is a PEER deployment's zeta, not ours. This is the
    pin that a `split("@")[0]` shortcut would break — it is why the predicate
    compares (agent, env) component-wise instead of matching a pattern."""
    monkeypatch.setattr(gs, "ENVIRONMENT_ID", "ayoai-mind")
    bp = _board(tmp_path, [_qualified_directive(
        "requires_action_by:zeta@zds-mind")])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []


def test_qualified_prefix_sibling_agent_does_not_direct(tmp_path, monkeypatch):
    """`zetax@<self-env>` is a different agent that merely shares a prefix."""
    monkeypatch.setattr(gs, "ENVIRONMENT_ID", "ayoai-mind")
    bp = _board(tmp_path, [_qualified_directive(
        "requires_action_by:zetax@ayoai-mind")])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []


def test_bare_qualified_tag_does_not_reopen_prose_fallback(tmp_path, monkeypatch):
    """A qualified tag with NO `requires_action_by:` prefix (`alpha@ayoai-mind`)
    must still count as an explicit routing tag, so the prose fallback stays
    suppressed. Otherwise has_routing_tag goes False and the g-115-2870
    false-flag reopens: the tag routes to alpha, the prose names zeta."""
    monkeypatch.setattr(gs, "ENVIRONMENT_ID", "ayoai-mind")
    directive = {
        "id": "msg-qual-2", "author": "alpha", "type": "directive",
        "channel": "coordination",
        "tags": ["directive", "target:g-315-390", "weight:+2.0",
                 "alpha@ayoai-mind"],
        "text": "alpha please claim g-315-390; zeta cannot do it.",
    }
    bp = _board(tmp_path, [directive])
    assert gs.emit_directive_honor_banner(
        _scored("g-315-390"), "zeta", board_path=bp) == []
