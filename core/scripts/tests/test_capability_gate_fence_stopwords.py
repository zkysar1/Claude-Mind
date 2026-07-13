"""test_capability_gate_fence_stopwords.py — regression test for 1.

Own-cloud write-fence defer_reasons ("own-cloud pipeline writes fenced; reads
work, writes fail; write_conflict") leaked the generic infra tokens
writes / reads / pipeline into capability-gate keyword extraction, where they
false-matched domain capability-routing rows:
  - "writes"   -> game-session RUN-mode row ("NPC memory writes")
  - "reads"    -> PLAY-mode bridge row ("reads Player presence")
  - "pipeline" -> behavioral-analysis row ("OHS scoring pipeline")
This refused a legitimate fence defer AND auto-filed a spurious Unblock
(g-001-317) on 2026-07-05 when g-001-02 was deferred on the own-cloud fence.

Fix (g-115-1791): add writes/reads/pipeline to gates.capability._STOPWORDS.
writes/reads are the plural-leak of the already-stopworded singular
write/read; pipeline is a generic infra noun peer of the already-stopworded
process/script/system. Each colliding row retains its true discriminating
tokens (npc/memory/session; play-mode/bridge/player; ohs/scoring/
analyze-npc-behavior), so legitimate detection is preserved — asserted below.

Subprocess + sys.path import shape matches test_capability_gate_narrative.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

# A minimal own-cloud write-fence defer_reason. After 1 + 0
# the only tokens that survive extraction are non-capability-matching (fenced /
# precondition_unmet / write_conflict / since) — writes/reads/pipeline AND
# own-cloud are stopworded. (0: own-cloud was added to _STOPWORDS once
# the forged skill probe-governed-store registered a trigger containing it,
# turning the previously-inert own-cloud token into a false capability match on
# a legitimately-fenced defer.)
_FENCE_DEFER = (
    "precondition_unmet: own-cloud pipeline writes fenced; "
    "reads work, writes fail; write_conflict since 11:47"
)

# A real behavioral-analysis capability named by its true identifiers (NOT via
# the generic "pipeline"): must still be recognized as agent-capable.
_REAL_DEFER = "blocked: needs OHS scoring / analyze-npc-behavior run"


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


def test_fence_tokens_not_extracted():
    """writes / reads / pipeline must not survive keyword extraction from a
    fence defer — they are the tokens that used to false-match domain rows."""
    _, d = _run_gate(_FENCE_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    leaked = {"writes", "reads", "pipeline"} & kws
    assert not leaked, (
        f"fence tokens leaked into keyword extraction: {sorted(leaked)} "
        f"(all extracted: {sorted(kws)})"
    )


def test_fence_defer_does_not_falsely_block():
    """A pure own-cloud fence defer must not produce a spurious capability
    match — this is the exact false-positive that auto-filed g-001-317."""
    _, d = _run_gate(_FENCE_DEFER)
    assert not d.get("would_block"), (
        f"fence defer wrongly blocked; matches={d.get('matches')} "
        f"keywords={d.get('keywords_extracted')}"
    )
    match_kws = {m.get("matched_keyword") for m in (d.get("matches") or [])}
    assert not ({"writes", "reads", "pipeline"} & match_kws), (
        f"a fence stopword produced a capability match: {sorted(match_kws)}"
    )


def test_detection_preserved_for_real_domain_capability():
    """Stopwording the generic terms did NOT break legitimate detection: the
    behavioral-analysis capability is still recognized via its real
    discriminating tokens (ohs / scoring / analyze-npc-behavior)."""
    _, d = _run_gate(_REAL_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    assert kws & {"ohs", "scoring", "analyze-npc-behavior"}, (
        f"real domain tokens lost from extraction: {sorted(kws)}"
    )
    # The gate must still route this agent-capable action away from the user.
    assert d.get("would_block"), (
        f"real domain capability no longer detected; keywords={sorted(kws)} "
        f"matches={d.get('matches')}"
    )


# 0 recall control (guard-958): stopwording own-cloud must NOT suppress
# an ADJACENT genuine probe-governed-store token. The single surviving keyword
# (backend-cat) is the SOLE matcher, sitting right beside the newly-stopworded
# own-cloud — so a multi-keyword happy path cannot mask a recall regression.
_GOVERNED_STORE_RECALL_DEFER = "blocked: own-cloud backend-cat step must run first"


def test_own_cloud_stopworded_no_longer_matches_forged_skill():
    """0: the pure own-cloud fence defer must not match the
    probe-governed-store forged skill. own-cloud is now stopworded, so it is
    absent from keyword extraction and produces no capability match — the exact
    false-positive that regressed test_fence_defer_does_not_falsely_block when
    probe-governed-store (zeta, 2026-07-11) registered a trigger containing it."""
    _, d = _run_gate(_FENCE_DEFER)
    assert "own-cloud" not in set(d.get("keywords_extracted") or []), (
        f"own-cloud leaked into extraction: "
        f"{sorted(d.get('keywords_extracted') or [])}"
    )
    match_skills = {m.get("skill") for m in (d.get("matches") or [])}
    assert "probe-governed-store" not in match_skills, (
        f"own-cloud fence defer still matches probe-governed-store: "
        f"{sorted(m for m in match_skills if m)}"
    )


def test_governed_store_recall_preserved_adjacent_to_stopword():
    """0 / guard-958 adversarial recall control: stopwording own-cloud
    did NOT break probe-governed-store detection. A genuine defer whose SOLE
    surviving keyword is backend-cat (own-cloud stopworded right beside it) must
    still route to the agent via that skill — proving no collateral recall loss
    and no multi-keyword masking."""
    _, d = _run_gate(_GOVERNED_STORE_RECALL_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    assert "own-cloud" not in kws, f"own-cloud not stopworded: {sorted(kws)}"
    assert "backend-cat" in kws, (
        f"recall token backend-cat lost from extraction: {sorted(kws)}"
    )
    assert d.get("would_block"), (
        f"probe-governed-store recall broken; keywords={sorted(kws)} "
        f"matches={d.get('matches')}"
    )
    match_skills = {m.get("skill") for m in (d.get("matches") or [])}
    assert "probe-governed-store" in match_skills, (
        f"backend-cat no longer routes to probe-governed-store: "
        f"{sorted(m for m in match_skills if m)}"
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
