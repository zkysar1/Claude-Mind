"""test_defer_self_artifact.py — a defer that waits on the goal's OWN unlanded
artifact is refused at write time (g-353-109).

The canonical fixture is the g-373-12 defer as it survives: quoted, with bravo's
own ellipses, in the goal's progress_note (2026-09-15T11:09Z) and on the
decisions board (msg-20260915-111404-bravo-840). The unelided original is not
recoverable — every 2026-09-15 history snapshot of the store is dropped and no
board post carries it — so the regression is pinned to the words that were
recorded. The record half is the goal's own progress_note clause, verbatim from
its note archive.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "core" / "scripts"))

from gates.defer_self_artifact import (  # noqa: E402
    ADVISORY_PREFIX, REFUSAL_PREFIX, evaluate, own_artifact_tokens)

CANON_DEFER = ("precondition_unmet: PR #528 ... is open and unmerged ... Neither gate "
               "is agent-clearable right now: the merge is the named external signal.")
CANON_RECORD = {
    "progress_note": (
        "=== alpha WORKER Body, cc-09, SID fdfc7f97, 2026-09-15T06:0x-06:17Z — OUTCOME 2 "
        "BUILT AND\nTESTED. PR #528 open (zkysar1/Ayoai-Environment-Server, base dev, "
        "branch\ng-373-12-finding-through-vessel, commit c8c0a23). Implemented exactly "
        "the design bravo\ndecided at 05:2x; I re-derived nothing."),
}
ENDPOINT = ROOT / "mind_api" / "src" / "endpoints" / "aspirations_write.py"


def test_canonical_g_373_12_defer_is_refused_through_the_goals_own_record():
    r = evaluate("g-373-12", CANON_DEFER, goal_record=CANON_RECORD)
    assert r["refuse"] is True
    b = r["bindings"][0]
    assert b["ref"] == "PR #528" and b["token"] == "g-373-12-finding-through-vessel"
    # the message names the artifact and BOTH legal responses
    assert r["reason"].startswith(REFUSAL_PREFIX + " g-373-12")
    assert "PR #528" in r["reason"] and "g-373-12-finding-through-vessel" in r["reason"]
    assert "LAND IT" in r["reason"] and "STATE WHY YOU CANNOT" in r["reason"]
    assert "--force-defer" in r["reason"]


def test_canonical_passes_once_the_self_reference_is_removed():
    stripped = CANON_DEFER.replace("PR #528 ", "")
    assert evaluate("g-373-12", stripped, goal_record=CANON_RECORD)["refuse"] is False
    # and the PR alone proves nothing: without the goal's own binding it is external
    assert evaluate("g-373-12", CANON_DEFER, goal_record={"progress_note": "PR #528 is a "
                    "sibling's work."})["refuse"] is False
    assert evaluate("g-373-12", CANON_DEFER)["refuse"] is False  # no record: lane 2 skipped


def test_triggers_on_the_field_whatever_the_structured_prefix():
    # the canonical defer is itself precondition_unmet: — the live corpus is all-structured
    for prefix in ("precondition_unmet: ", "human_blocked: ", ""):
        text = prefix + "waiting for branch g-353-109-self-artifact, still open and unmerged."
        assert evaluate("g-353-109", text)["refuse"] is True, prefix


def test_endpoint_wires_it_on_the_field_before_the_narrative_branch():
    src = ENDPOINT.read_text(encoding="utf-8")
    i_gate = src.find("from gates.defer_self_artifact import evaluate as _self_eval\n")
    i_narr = src.find("if _is_narrative_defer(field, value):")
    assert 0 < i_gate < i_narr, "Layer 0b must run before, and independently of, the narrative branch"
    block = src[src.rfind("if field == \"defer_reason\"", 0, i_gate):i_narr]
    assert '"X-Mind-Force-Defer"' in block, "the one shared override, no private bypass"
    assert '"error": "defer_self_artifact"' in block
    assert "from gates.defer_self_artifact import evaluate as _self_eval2" in src, "advisory half wired"


def test_branch_path_and_commit_scope_tokens_are_artifacts():
    assert own_artifact_tokens("g-353-109", "on branch g-353-109-fix-gate") == ["g-353-109-fix-gate"]
    assert own_artifact_tokens("g-353-109", "fix/g-353-109-gate lands") == ["fix/g-353-109-gate"]
    assert own_artifact_tokens("g-353-109", "commit 'fix(g-353-109): refuse' ") == ["fix(g-353-109):"]
    assert evaluate("g-353-109", "commit 'fix(g-353-109): x' is not yet on main")["refuse"] is True


def test_markers_and_predicate_ids_are_not_artifacts():
    # the measured false positives: '<goal-id>-<slug>' is ALSO the marker/predicate convention
    for text in (
        "precondition_unmet: window not yet open; machine-evaluable as the after_time "
        "predicate g-370-63-dev-row-window in verification.preconditions",
        "precondition_unmet: owned by g-115-8930 (id in progress_note marker "
        "g-115-8891-discharged-dep-provenance-20260922), not yet merged there",
    ):
        gid = "g-370-63" if "g-370-63" in text else "g-115-8891"
        assert own_artifact_tokens(gid, text) == [], text
        assert evaluate(gid, text)["refuse"] is False


def test_a_landed_own_artifact_cited_as_provenance_is_allowed():
    record = {"progress_note": "Shipped: PR #523 \"fix(g-374-15): label\" from branch "
                               "fix/g-374-15-sidecar-agent-label. Commit dde1022."}
    text = ("precondition_unmet: outcomes are launch-gated. Code is SHIPPED: commit dde1022, "
            "PR #523 merged 4bf785a4, deploy run success; needs a real launched instance.")
    r = evaluate("g-374-15", text, goal_record=record)
    assert r["refuse"] is False and r["bindings"] == []


def test_bare_own_id_and_longer_ids_are_prose():
    assert own_artifact_tokens("g-353-109", "g-353-109 waits on the window (g-353-109).") == []
    assert own_artifact_tokens("g-353-109", "see g-353-109/g-353-110 and g-353-109/17/29") == []
    #  must never match inside 's branch
    assert evaluate("g-373-1", "branch g-373-12-foo is still open")["refuse"] is False


def test_grant_verb_advisory_never_refuses_and_a_probe_silences_it():
    r = evaluate("g-353-109", "precondition_unmet: the merge is the named external signal")
    assert r["refuse"] is False and r["advisories"]
    assert r["advisories"][0].startswith(ADVISORY_PREFIX)
    probed = evaluate("g-353-109", "precondition_unmet: the merge waits; probed "
                      "gh pr view -> rc=1, branch protection requires review")
    assert probed["advisories"] == [] and probed["refuse"] is False


def test_fail_open_on_bad_input():
    for gid, text, rec in ((None, CANON_DEFER, None), ("not-a-goal", CANON_DEFER, None),
                           ("g-373-12", "", None), ("g-373-12", CANON_DEFER, "garbage")):
        r = evaluate(gid, text, goal_record=rec)
        assert r["refuse"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
