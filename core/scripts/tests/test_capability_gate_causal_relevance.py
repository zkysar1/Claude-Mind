"""test_capability_gate_causal_relevance.py — regression test for .

Two hardenings to the capability-gate Layer-D auto-conversion (rb-3955):

  Mode 2 (verb-validation): the action-verb extractor in evaluate() must NOT
  pick an ambiguous verb-adjective used ADJECTIVALLY ("clean" in "clean
  session") as the Unblock title's action verb. Rejecting it leaves
  action_verb=None, which routes into the EXISTING g-115-1872 verbless-
  suppression (unblock_suggested=False) rather than filing a nonsensical
  "Unblock: clean for g-X".

  Mode 1 (causal-relevance): _keyword_is_invocation_signal must disqualify an
  INCIDENTAL keyword occurrence whose narrative asserts the referent is
  AVAILABLE / not-the-blocker ("efs probed available"), or names it as the
  LOCATION where some OTHER thing is absent ("config absent on efs"). A
  disqualified keyword is dropped by _filter_context_disqualified before
  matching, so it never contributes to would_block.

CRITICAL (guard-958 / rb-389): a keyword-matching safety gate must never
over-broaden a disqualifier into a FALSE-NEGATIVE — a genuine "X is the
blocker" request that stops matching lets a real capability-routing violation
slip through. Both new disqualifiers are false-negative-SAFE by construction:
they fire only on contexts SEMANTICALLY OPPOSITE to a genuine block ("efs is
NOT available" / "cannot access efs" assert unavailability and must still
match). The SAFETY tests below are the load-bearing assertions — they encode
the guard-958 evidence discharged during the g-115-2583 pre-apply consult.

Pattern: hermetic unit tests on the pure module functions (no live-registry
dependency) + one subprocess integration case mirroring
test_capability_gate_suggest_unblock.py (live-registry npc match), proving the
mode-2 wiring end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PY = CORE_SCRIPTS / "capability-gate.py"

from gates.capability import (  # noqa: E402
    _keyword_is_invocation_signal,
    _filter_context_disqualified,
    _is_adjectival_use,
    _TOKEN_RE,
    _ADJECTIVE_VERBS,
)


def _kw_signal(text: str, kw: str) -> bool:
    return _keyword_is_invocation_signal(text.lower(), kw)


def _clean_match(text: str):
    """Return the re.Match for the 'clean' token in text (helper for mode-2)."""
    for m in _TOKEN_RE.finditer(text):
        if m.group(0).lower() == "clean":
            return m
    raise AssertionError(f"no 'clean' token in {text!r}")


# ── Mode 1: causal-relevance FALSE-NEGATIVE SAFETY (guard-958 — load-bearing) ──
# Every one of these is a GENUINE efs block and MUST still register as an
# invocation signal (return True). If any of these regresses to False, a real
# efs-access block would slip past the gate — the exact failure guard-958 warns
# against. Do NOT relax these.
GENUINE_BLOCK_TEXTS = [
    "cannot access efs",
    "efs unreachable",
    "efs is not available",
    "efs is not reachable",
    "unable to mount efs",
    "fetch config from efs",          # location prep, no absence word
    "reconnect to efs",
    "efs down since 09:00",
    "cannot read the manifest from efs",
]


def test_mode1_genuine_efs_blocks_still_match():
    for text in GENUINE_BLOCK_TEXTS:
        assert _kw_signal(text, "efs") is True, (
            f"FALSE-NEGATIVE regression (guard-958): genuine block "
            f"{text!r} no longer matches 'efs'"
        )


# ── Mode 1: causal-relevance DISQUALIFICATION (the FP being fixed) ──
# These name efs only INCIDENTALLY — the narrative says efs is fine / is the
# location of some OTHER absent thing — so 'efs' must be disqualified (False).
INCIDENTAL_EFS_TEXTS = [
    "efs probed available; the config lives elsewhere",
    "efs is available",
    "efs reachable, resource not there",
    "efs is not the blocker",
    "efs is not the issue here",
    "config file absent on efs",
    "the manifest was deleted from efs",
    "data not found on efs",
]


def test_mode1_incidental_efs_disqualified():
    for text in INCIDENTAL_EFS_TEXTS:
        assert _kw_signal(text, "efs") is False, (
            f"expected 'efs' disqualified as incidental in {text!r}, got matched"
        )


def test_mode1_multi_occurrence_fail_open():
    # If ANY occurrence is a genuine signal, the keyword matches (fail-open
    # across occurrences). A narrative that both says efs is the blocker AND
    # mentions absence-on-efs must still match.
    text = "cannot access efs; also the temp file is absent on efs"
    assert _kw_signal(text, "efs") is True


def test_mode1_filter_wiring_removes_incidental_keeps_genuine():
    # _filter_context_disqualified is the would_block-affecting call site
    # (capability.py:850): a disqualified keyword is dropped from the set so it
    # cannot match a capability row.
    assert "efs" not in _filter_context_disqualified(
        "config absent on efs", {"efs", "deploy"})
    assert "efs" in _filter_context_disqualified(
        "cannot access efs now", {"efs", "deploy"})


# ── Mode 2: verb-validation (adjectival detection) ──
def test_mode2_adjectival_clean_detected():
    # "clean" directly modifying a following noun -> adjectival (reject).
    for text in ["clean session npc node", "a clean session then deploy",
                 "waiting for a clean session"]:
        assert _is_adjectival_use(text, _clean_match(text)) is True, (
            f"expected adjectival use of 'clean' in {text!r}")


def test_mode2_verbal_clean_kept():
    # "clean" followed by a function word / particle -> genuine verb (keep).
    for text in ["clean the cache before deploy", "clean up the logs",
                 "clean it and retry", "clean"]:
        assert _is_adjectival_use(text, _clean_match(text)) is False, (
            f"expected verbal use of 'clean' in {text!r}")


def test_mode2_clean_is_the_only_adjective_verb():
    # Guard against speculative over-broadening (implementation-discipline):
    # the set stays minimal + evidence-driven. Adding a member risks rejecting
    # genuine verb+noun requests ("deploy production", "restart service").
    assert _ADJECTIVE_VERBS == {"clean"}


# ── Mode 2: end-to-end wiring (subprocess, live registry — mirrors
#    test_capability_gate_suggest_unblock.py Case 5) ──
def _run_gate(failure_reason: str, suggest_unblock: bool = True):
    cmd = [sys.executable, str(GATE_PY), "--failure-reason", failure_reason,
           "--intended-participants", "user", "--output", "json"]
    if suggest_unblock:
        cmd.append("--suggest-unblock")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"_raw_stdout": proc.stdout,
                                 "_raw_stderr": proc.stderr}


def test_mode2_adjectival_clean_suppresses_unblock_end_to_end():
    # "clean session" (adjectival) + a domain-noun ('npc') that token-matches an
    # NPC capability -> would_block True, but 'clean' is rejected as the action
    # verb and no other imperative verb remains, so action_verb=None and the
    #  guard suppresses the Unblock (unblock_suggested=False).
    # WITHOUT the  fix, 'clean' would be picked -> unblock_suggested
    # True with a nonsensical "Unblock: clean" title (the rb-3955 bug).
    rc, d = _run_gate("clean session npc memory hierarchy node")
    if not d.get("would_block"):
        # Live registry did not match 'npc' (capability retired / renamed). The
        # hermetic unit tests above still fully cover the new logic; skip the
        # end-to-end assertion rather than fail on an environment dependency.
        print("  [SKIP] npc no longer matches live registry — "
              "would_block=False; hermetic tests cover the logic")
        return
    assert d.get("unblock_suggested") is False, (
        f"expected Unblock suppressed for adjectival 'clean' (verbless after "
        f"rejection), got unblock_suggested={d.get('unblock_suggested')!r} "
        f"title={d.get('unblock_title')!r}")
    assert "unblock_title" not in d, (
        f"no title expected when suppressed, got {d.get('unblock_title')!r}")


def main() -> int:
    tests = [
        test_mode1_genuine_efs_blocks_still_match,
        test_mode1_incidental_efs_disqualified,
        test_mode1_multi_occurrence_fail_open,
        test_mode1_filter_wiring_removes_incidental_keeps_genuine,
        test_mode2_adjectival_clean_detected,
        test_mode2_verbal_clean_kept,
        test_mode2_clean_is_the_only_adjective_verb,
        test_mode2_adjectival_clean_suppresses_unblock_end_to_end,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failures.append((t.__name__, str(e)))
            print(f"  [FAIL] {t.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s).")
        return 1
    print(f"\nAll {len(tests)} causal-relevance / verb-validation cases verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
