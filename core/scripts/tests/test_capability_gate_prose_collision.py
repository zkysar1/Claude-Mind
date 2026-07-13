"""test_capability_gate_prose_collision.py — regression test for 2.

The capability gate matched extracted defer_reason keywords against the FULL
PROSE of capability-routing.md rows (via _entry_tokens over the whole row), not
the capability identifier — so common words appearing incidentally in a row's
prose false-matched. On 2026-07-09, deferring g-115-1848 (genuinely
fleet-blocked) with a reason containing "fresh probe 2026-07-09 shows all 6
partners idle ... not agent-provisionable" tripped FOUR spurious keywords:
  - "probe" -> SSH-to-EFS row prose ("synthetic ssh probe"). probe is an
               _IMPERATIVE_VERB but was a NOUN here (reported evidence).
  - "goal"  -> bounded-parameter-config row ("named in the goal").
  - "idle"  -> game-session row ("idle Player object").
  - "agent-provisionable" -> the "## Agent-Provisionable" section descriptor,
               repeated in row prose. Matching it INVERTED the negation
               ("NOT agent-provisionable" flagged AS matching one).
The gate refused a legitimate defer AND auto-filed spurious Unblock g-115-1881.
Same class as g-001-317 (fixed narrowly by g-115-1791 fence-stopwords) and the
g-115-1872 noun-as-verb guard — which only SUPPRESSED the Unblock for verbLESS
matches; a verb-noun used as a noun kept MATCHING and filing.

Fix (g-115-1882, rb-2993):
  - _STOPWORDS += goal, idle, provisionable, agent-provisionable (pure
    prose / category descriptors, never a capability identifier).
  - _keyword_is_invocation_signal: evidence-verb disqualifier — a verb-noun
    (probe/monitor/audit) followed within 2 words by a past-evidence verb
    (shows/found/revealed) is a reported observation, not a requested action.

Subprocess + sys.path import shape matches test_capability_gate_fence_stopwords.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

# The 8 fleet-blocked defer_reason (the exact incident shape). After
# the fix the surviving tokens (fleet / partner / cross_agent_surfacing / ...)
# match no capability row.
_FLEET_DEFER = (
    "Blocked on active partner fleet: validating cross_agent_surfacing "
    "requires 2+ partner agents observing cross-agent goal surfacing in live "
    "sessions; fresh probe 2026-07-09 shows all 6 partners idle 44-168h. An "
    "active partner fleet is not agent-provisionable. Re-surface when active."
)

# A genuine agent-provisionable action wrongly deferred — MUST still block.
_REAL_DEFER = (
    "Deferring: blocked on user to commit and push the build to main and "
    "deploy the service."
)

# "probe" as a genuine imperative ACTION (no following evidence verb) — the
# evidence-verb disqualifier must NOT strip it (no over-disqualification).
_PROBE_ACTION = "blocked: need to probe the service health endpoint before retry"

# 3: compound action requests where a genuine provisionable keyword
# (commit/push — both ARE capability-row tokens) is followed within 0-2 words by
# an evidence-verb's BARE IMPERATIVE form (Confirm/show). The 2
# disqualifier's bare-form alternations (shows?, confirm(?:s|ed)?) wrongly
# matched here and stripped the keyword — turning a real agent action into
# would_block=False (false-negative in a safety gate, confirmed by the
# 2 fresh-eyes review a6e3fd81). After tightening _EVIDENCE_VERB_AFTER
# to inflected report forms only, these MUST block again.
#
# NB: the SURVIVING keyword must itself be a capability-row token for the
# end-to-end block. The reviewer's original 'deploy...Confirm' example does NOT
# round-trip because 'deploy' is not a row token in this domain (only its
# co-token 'production' matched there); 'commit'/'push' ARE row tokens, so they
# exercise the full extract->match->block path. The inflected reported-evidence
# case (probe...shows) stays disqualified — covered by _FLEET_DEFER above.
_COMPOUND_COMMIT_CONFIRM = (
    "blocked on user to commit the hotfix. Confirm the regression is fixed."
)
_COMPOUND_PUSH_SHOW = (
    "Waiting for user to push and show the team the updated dashboard."
)

# 5: a boolean literal is a config-VALUE fragment, never a capability
# identifier. The tokenizer splits "cross_agent_surfacing.enabled=true" into
# "cross_agent_surfacing.enabled" + "true"; the bare "true" then false-matched
# "plugin_connected: true" in the RUN-mode game-session capability row ->
# would_block=True + spurious Unblock 4. After stopwording true/false
# this defer passes cleanly. (This is the real 8 fleet defer shape.)
_BOOLEAN_LITERAL_DEFER = (
    "Blocked on active partner fleet: validating cross_agent_surfacing.enabled="
    "true requires 2+ partner agents in concurrent live sessions; none active. "
    "Waking the fleet is not agent-provisionable."
)


def _run_gate(failure_reason: str,
              intended_participants: str = "user") -> tuple[int, dict]:
    """Invoke capability-gate.py via subprocess. Returns (exit_code, parsed)."""
    cmd = [
        sys.executable, str(GATE_PY),
        "--failure-reason", failure_reason,
        "--intended-participants", intended_participants,
        "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"_raw_stdout": proc.stdout, "_raw_stderr": proc.stderr}
    return proc.returncode, payload


def test_prose_collision_tokens_not_extracted():
    """goal / idle / provisionable / agent-provisionable stopworded; probe
    disqualified as reported evidence ("fresh probe ... shows")."""
    _, d = _run_gate(_FLEET_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    leaked = {"goal", "idle", "provisionable", "agent-provisionable", "probe"} & kws
    assert not leaked, (
        f"prose-collision tokens leaked into extraction: {sorted(leaked)} "
        f"(all extracted: {sorted(kws)})"
    )


def test_fleet_defer_does_not_falsely_block():
    """The exact 8 defer must not produce a spurious capability match —
    this is what refused the defer AND auto-filed spurious g-115-1881."""
    _, d = _run_gate(_FLEET_DEFER)
    assert not d.get("would_block"), (
        f"fleet-blocked defer wrongly blocked; matches={d.get('matches')} "
        f"keywords={d.get('keywords_extracted')}"
    )


def test_detection_preserved_for_genuine_provisionable_action():
    """The fix did NOT weaken the gate: a genuine commit / push / deploy defer
    is still routed away from the user (would_block)."""
    _, d = _run_gate(_REAL_DEFER)
    assert d.get("would_block"), (
        f"genuine agent-provisionable action no longer detected; "
        f"keywords={sorted(d.get('keywords_extracted') or [])} "
        f"matches={d.get('matches')}"
    )


def test_probe_as_action_still_extracted():
    """The evidence-verb disqualifier is scoped to reported-evidence usage:
    'probe' as an imperative ACTION (no following evidence verb) is preserved,
    so a genuine probe request is not silently dropped."""
    _, d = _run_gate(_PROBE_ACTION)
    kws = set(d.get("keywords_extracted") or [])
    assert "probe" in kws, (
        f"'probe' wrongly disqualified in an action context "
        f"(over-disqualification): {sorted(kws)}"
    )


def test_compound_imperative_commit_confirm_still_blocks():
    """Recall-weakening regression (3): 'commit the hotfix. Confirm the
    regression is fixed' — the bare imperative 'Confirm' must NOT strip 'commit'
    (the sole capability-row token here). Under the g-115-1882 bare-form regex
    'commit' was stripped and the defer wrongly passed (would_block=False); after
    the inflected-only tightening the genuine provisionable action blocks."""
    _, d = _run_gate(_COMPOUND_COMMIT_CONFIRM)
    assert d.get("would_block"), (
        f"compound-imperative 'commit...Confirm' lost detection "
        f"(bare-imperative false-negative); "
        f"keywords={sorted(d.get('keywords_extracted') or [])} "
        f"matches={d.get('matches')}"
    )


def test_compound_imperative_push_show_still_blocks():
    """Recall-weakening regression (3): 'push and show the team' — bare
    'show' must not strip 'push'. Genuine provisionable action still blocks."""
    _, d = _run_gate(_COMPOUND_PUSH_SHOW)
    assert d.get("would_block"), (
        f"compound-imperative 'push and show' lost detection "
        f"(bare-imperative false-negative); "
        f"keywords={sorted(d.get('keywords_extracted') or [])} "
        f"matches={d.get('matches')}"
    )


def test_boolean_literal_true_not_extracted():
    """Boolean literals are config values, not capability identifiers — 'true'/
    'false' must be stopworded out of extraction (g-115-1885)."""
    _, d = _run_gate(_BOOLEAN_LITERAL_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    leaked = {"true", "false"} & kws
    assert not leaked, (
        f"boolean literal leaked into extraction: {sorted(leaked)} "
        f"(all extracted: {sorted(kws)})"
    )


def test_boolean_literal_defer_does_not_falsely_block():
    """The '=true' fragment must not false-match 'plugin_connected: true' in the
    game-session row (g-115-1885): the real g-115-1848 fleet defer must pass
    cleanly, not produce a spurious Unblock as g-115-1884 did."""
    _, d = _run_gate(_BOOLEAN_LITERAL_DEFER)
    assert not d.get("would_block"), (
        f"boolean-literal defer wrongly blocked; matches={d.get('matches')} "
        f"keywords={sorted(d.get('keywords_extracted') or [])}"
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
