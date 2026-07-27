"""test_capability_gate_table_token_noise.py — regression test for .

Markdown-table STRUCTURAL vocabulary + prose function words from
capability-routing.md row text ("row", "entry", "table", "because",
"verified", "evidence") leaked into capability-gate keyword extraction and
formed 2+-token overlaps that BYPASS the g-248-105 sole-token distinctiveness
rule (multi-token matches always survive by design). Two junk Layer-D
auto-Unblocks in one day (2026-07-16), different agents:
  - echo's defer of g-115-2269 ("registry entry must exist...") matched
    'row' against the PLAY-mode bridge row and 'entry' against the
    bounded-config-tune row -> auto-filed "Unblock: fire for g-115-2269",
    skipped as g-115-2329.
  - foxtrot's g-350-21 "Unblock: deploy for g-350-19" — same shape.
Live repro before the fix: 'because'+'row'+'play-mode' survived as a 3-token
match on text referencing no capability.

Fix (g-115-2336): add the six tokens to gates.capability._STOPWORDS. None is
ever a capability identifier — they appear in row text only as table
structure ("see Genuinely Human-Only row"), quoted prose ("because Character
only spawns..."), or verification notes ("Verified g-273-02 spike",
"empirical evidence"). Every colliding row retains its true discriminators
(roblox-studio.sh / start-session / play-mode / player; bounded-config /
tune compounds), so legitimate detection is preserved — asserted below per
guard-958 (adversarial single-surviving-keyword recall control adjacent to
the new stopwords). "verified" is a distinct token from the imperative
"verify" (_IMPERATIVE_VERBS), which stays matchable (rb-2996) — also
asserted below.

Subprocess + fixture shape mirrors test_capability_gate_fence_stopwords.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
GATE_PY = CORE_SCRIPTS / "capability-gate.py"

_NEW_STOPWORDS = {"row", "entry", "table", "because", "verified", "evidence"}

# Echo-shaped FP defer ( class): structural/table vocabulary and
# verification prose, but NO capability reference — no imperative verbs, no
# row-naming compounds. Pre-fix, 'because'+'row' co-occurred in the PLAY-mode
# row and 'entry'+'evidence' in the bounded-config-tune row, each forming a
# multi-token overlap that survived _find_matches. Post-fix all six tokens
# are stopworded and the residual tokens must not match anything.
_TABLE_DEFER = (
    "upstream doc incomplete: the registry entry must exist and the matching "
    "table row was never added; verified against the evidence ledger because "
    "the checker demands it"
)

# guard-958 adversarial recall control: the SOLE surviving keyword
# ('play-mode', structurally compound -> qualifies under ) sits
# immediately adjacent to three newly-stopworded tokens. If stopwording had
# collateral recall loss, THIS is where it would surface — a multi-keyword
# happy path cannot mask it.
_ADJACENT_RECALL_DEFER = "user must update the play-mode table row entry"

# rb-2996 inflection split: the imperative "verify" must survive extraction
# while the past-participle "verified" (narration, not a request) does not.
_VERIFY_SPLIT_DEFER = "verify the table entry against the row"


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


def test_table_tokens_not_extracted():
    """The six structural/prose tokens must not survive keyword extraction —
    they are the tokens that formed the junk multi-token overlaps."""
    _, d = _run_gate(_TABLE_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    leaked = _NEW_STOPWORDS & kws
    assert not leaked, (
        f"table tokens leaked into keyword extraction: {sorted(leaked)} "
        f"(all extracted: {sorted(kws)})"
    )


def test_table_defer_does_not_falsely_block():
    """An echo-shaped structural-vocabulary defer must not produce a spurious
    capability match — the exact FP chain that auto-filed g-115-2329."""
    _, d = _run_gate(_TABLE_DEFER)
    assert not d.get("would_block"), (
        f"table-vocabulary defer wrongly blocked; matches={d.get('matches')} "
        f"keywords={d.get('keywords_extracted')}"
    )
    match_kws = set()
    for m in d.get("matches") or []:
        match_kws.update(m.get("all_matched_keywords") or [])
    assert not (_NEW_STOPWORDS & match_kws), (
        f"a structural stopword produced a capability match: {sorted(match_kws)}"
    )


def test_recall_preserved_adjacent_to_stopword():
    """guard-958 adversarial recall control: stopwording table/row/entry must
    not suppress the ADJACENT genuine capability token. 'play-mode' is the
    sole surviving keyword (compound -> qualifies under g-248-105) and must
    still match the PLAY-mode bridge-start row and block user-routing."""
    _, d = _run_gate(_ADJACENT_RECALL_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    assert "play-mode" in kws, (
        f"recall token play-mode lost from extraction: {sorted(kws)}"
    )
    assert not (_NEW_STOPWORDS & kws), (
        f"new stopwords leaked alongside the recall token: {sorted(kws)}"
    )
    assert d.get("would_block"), (
        f"adjacent-token recall broken; keywords={sorted(kws)} "
        f"matches={d.get('matches')}"
    )
    matched_rows = " ".join(
        (m.get("row") or "") for m in (d.get("matches") or [])
    ).lower()
    assert "play-mode" in matched_rows, (
        f"play-mode no longer routes to its capability row; "
        f"matches={d.get('matches')}"
    )


def test_imperative_verify_survives_inflected_stopword():
    """rb-2996: stopwording the past-participle 'verified' must not touch the
    bare imperative 'verify' — a defer requesting verification is a genuine
    capability request and its verb must stay extractable."""
    _, d = _run_gate(_VERIFY_SPLIT_DEFER)
    kws = set(d.get("keywords_extracted") or [])
    assert "verify" in kws, (
        f"imperative 'verify' wrongly suppressed: {sorted(kws)}"
    )
    assert not ({"verified", "table", "entry", "row"} & kws), (
        f"inflected/structural tokens leaked: {sorted(kws)}"
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
