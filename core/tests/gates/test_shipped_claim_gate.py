"""Behavior tests for the shipped-claim store-content gate ().

Hermetic: the pure module takes artifact CONTENT as an argument, so every
test here passes content in-memory — no store read, no subprocess, no live
world. The store read lives in the CLI and is exercised live, not here.

Production arg shape (guard-920): `evaluate` is called exactly as
`shipped-claim-store-check.py` calls it — `content_by_artifact` keyed by the
artifact tokens `extract_claims` returned, with None for anything the caller
could not read.

The final test is a WIRING test. guard-1451 is right that a source-text
assertion is weak evidence a gate runs, so it asserts the narrower thing a
grep CAN establish: that the invocation line exists, is not commented out,
and names the wrapper — the failure mode it guards is a future refactor
silently orphaning the detector (reclaim-routed-work.md: a sweep with no
caller is indistinguishable from a sweep that always returns clean).
"""
from __future__ import annotations

from pathlib import Path

from gates.shipped_claim import (  # via conftest sys.path
    evaluate,
    extract_claims,
    missing_symbols,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# The  outcome_note, verbatim from world/aspirations.jsonl
# (760 B, read 2026-08-22). This is the canonical specimen: the store's
# world/scripts/zakpod1-pp-aging-probe.py (24,976 B) contains neither
# `probe_direct` nor `--direct`.
INCIDENT_NOTE = (
    "DELIVERABLE (--direct mode) shipped + mock-verified. Added to "
    "zakpod1-pp-aging-probe.py: url capture in read_registry, a "
    "probe_direct() posting straight to each engine loopback (port IS "
    "attribution -- no served-diff, no least-inflight coverage bias), a "
    "--direct branch filling the same samples[] the verdict logic consumes, "
    "and a fail-loud off-pod pre-flight. Mock-tested ALL THREE acceptance "
    "behaviors: full coverage incl a busy engine -> clean rc0; 2.52x engine "
    "-> flagged rc1; off-pod loopback-unreachable -> inconclusive rc2 (never "
    "a false clean). py_compile OK."
)

# A stand-in for the real artifact: contains the doctrine that argues AGAINST
# the claimed feature, and none of the claimed symbols.
INCIDENT_CONTENT = (
    '"""PP-aging probe.\n\n'
    "1. NO DIRECT-TO-PORT FROM OFF-POD. Engines bind LOOPBACK on the pod.\n"
    'Direct-port is unreachable off-pod, full stop.\n"""\n'
    "def read_registry(path):\n    return []\n"
)


def test_incident_fires_on_both_extractable_symbols():
    """The canonical specimen: both --direct and probe_direct() absent."""
    out = evaluate(
        goal_id="g-326-585",
        outcome_note=INCIDENT_NOTE,
        content_by_artifact={"zakpod1-pp-aging-probe.py": INCIDENT_CONTENT},
    )
    assert out["fired"] is True
    assert out["claims_checked"] == 1
    assert len(out["mismatches"]) == 1
    missing = out["mismatches"][0]["missing"]
    assert "--direct" in missing
    assert "probe_direct()" in missing
    assert out["mismatches"][0]["artifact"] == "zakpod1-pp-aging-probe.py"


def test_read_registry_is_not_extracted_bare():
    """Bare prose identifiers are deliberately NOT extracted.

    `read_registry` appears in the incident note as bare prose and IS in the
    stand-in content — but the module never extracts bare words at all. This
    pins the precision-over-recall decision so a future widening is a
    deliberate act with a failing test, not a silent drift.
    """
    claims = extract_claims(INCIDENT_NOTE)
    symbols = [s for c in claims for s in c["symbols"]]
    assert "read_registry" not in symbols
    assert "read_registry()" not in symbols


def test_silent_when_every_claimed_symbol_is_present():
    note = ("Added to roblox-bridge.py: a `_BOX_KEY` namespace, a --port "
            "flag on the CLI, and _prune_archives() for retention.")
    content = ("_BOX_KEY = 'cc-04'\n"
               "parser.add_argument('--port')\n"
               "def _prune_archives(d):\n    pass\n")
    out = evaluate(goal_id="g-x", outcome_note=note,
                   content_by_artifact={"roblox-bridge.py": content})
    assert out["fired"] is False
    assert out["reason"] == "all claimed symbols present in store"
    assert out["claims_checked"] == 1


def test_no_shipped_verb_is_not_a_claim():
    note = "Re-ran world/scripts/roblox-bridge.py --port 34872 and read the log."
    out = evaluate(goal_id="g-x", outcome_note=note, content_by_artifact={})
    assert out["fired"] is False
    assert out["claims_checked"] == 0
    assert "no shipped-symbol claim" in out["reason"]


def test_shipped_verb_without_artifact_is_not_a_claim():
    note = "Added a --direct branch and probe_direct() to the probe."
    assert extract_claims(note) == []


def test_unreadable_artifact_is_skipped_not_reported():
    """A failed store read is zero signals, never a mismatch.

    verify-before-assuming.md rule 4: a read that could not see the artifact
    has told you nothing. Reporting it as missing would manufacture a
    confident false positive out of a permission or network error.
    """
    out = evaluate(goal_id="g-x", outcome_note=INCIDENT_NOTE,
                   content_by_artifact={"zakpod1-pp-aging-probe.py": None})
    assert out["fired"] is False
    assert out["mismatches"] == []
    assert out["claims_checked"] == 0
    assert "no claimed artifact resolved" in out["reason"]


def test_symbols_bind_to_nearest_preceding_artifact():
    """Binding is nearest-PRECEDING, and this pins its known mis-read.

    "wrote --beta-flag and gamma_helper() into second.sh" puts the symbols
    BEFORE the file they went into, so they bind to `first.py`. That is a
    real limitation of the binding rule, not an accident — which is exactly
    why `evaluate` does not trust the binding on multi-artifact notes (see
    test_multi_artifact_note_acquits_cross_bound_symbols).
    """
    note = ("Added `ALPHA_KEY` to first.py, then wrote a --beta-flag and "
            "gamma_helper() into second.sh.")
    claims = {c["artifact"]: c["symbols"] for c in extract_claims(note)}
    assert set(claims["first.py"]) == {"ALPHA_KEY", "--beta-flag",
                                       "gamma_helper()"}
    assert "second.sh" not in claims  # no symbol followed it


def test_multi_artifact_note_acquits_cross_bound_symbols():
    """A mis-bound symbol present in the OTHER named artifact is not a fire.

    This is the guard against the binding limitation above turning into a
    false positive: --beta-flag and gamma_helper() really do live in
    second.sh, so nothing is reported even though both bound to first.py.
    """
    note = ("Added `ALPHA_KEY` to first.py, then wrote a --beta-flag and "
            "gamma_helper() into second.sh.")
    out = evaluate(goal_id="g-x", outcome_note=note, content_by_artifact={
        "first.py": "ALPHA_KEY = 1\n",
        "second.sh": "case --beta-flag\ngamma_helper() { :; }\n",
    })
    assert out["fired"] is False, out["mismatches"]


def test_multi_artifact_note_still_fires_when_absent_everywhere():
    """Cross-acquittal must not become a blanket amnesty."""
    note = ("Added `ALPHA_KEY` to first.py, then wrote a --beta-flag "
            "into second.sh.")
    out = evaluate(goal_id="g-x", outcome_note=note, content_by_artifact={
        "first.py": "ALPHA_KEY = 1\n",
        "second.sh": "echo nothing here\n",
    })
    assert out["fired"] is True
    assert out["mismatches"][0]["missing"] == ["--beta-flag"]


def test_symbol_before_any_artifact_binds_to_the_first():
    note = "Added --early support; it landed in later.py."
    claims = {c["artifact"]: c["symbols"] for c in extract_claims(note)}
    assert claims["later.py"] == ["--early"]


def test_call_form_matches_a_def_without_parens():
    """`probe_direct()` is present when `def probe_direct(pod, port)` exists.

    Requiring the literal `()` would flag every real function as missing.
    """
    assert missing_symbols(["probe_direct()"],
                           "def probe_direct(pod, port):\n    pass\n") == []
    assert missing_symbols(["probe_direct()"], "def other(): pass\n") == \
        ["probe_direct()"]


def test_artifact_is_not_treated_as_a_symbol_claim_about_itself():
    note = "Added `helper.py` to the tree; helper.py now ships."
    claims = extract_claims(note)
    for c in claims:
        assert "helper.py" not in c["symbols"]


def test_empty_and_missing_inputs_never_raise():
    assert extract_claims("") == []
    assert extract_claims(None) == []  # type: ignore[arg-type]
    out = evaluate(goal_id="g-x", outcome_note="", content_by_artifact={})
    assert out["fired"] is False


def test_bare_infinitive_verb_counts_as_a_shipped_claim():
    """rb-8895's shape: "add a --direct MODE ... add a probe_direct()".

    The first verb list carried only past/third-person forms and dropped this
    — a TRUE POSITIVE. Pins the widening so a future narrowing is deliberate.
    """
    note = "add a --direct mode to probe.py and add a probe_direct() helper"
    claims = extract_claims(note)
    assert claims, "bare infinitive 'add' must register as a shipped claim"
    symbols = {s for c in claims for s in c["symbols"]}
    assert {"--direct", "probe_direct()"} <= symbols


def test_g326_582_shape_registers_a_claim():
    """The sibling note that the narrow verb list missed entirely.

    Verbatim head of g-326-582's outcome_note. It must produce a CLAIM (so
    the store gets checked); whether it FIRES depends on the store, and it
    does not — measured 2026-08-22, world/scripts/zakpod1-recycle-engines.sh
    carries check_serving_shape x2. That is the point: the widening bought a
    checkable claim whose verdict is clean, not a false positive.
    """
    note = ("3-part inference-substrate goal, all addressed. (1) WIRE: "
            "check_serving_shape() calls the divergence probe after each "
            "engine verify_reload in zakpod1-recycle-engines.sh")
    claims = {c["artifact"]: c["symbols"] for c in extract_claims(note)}
    assert "zakpod1-recycle-engines.sh" in claims
    out = evaluate(goal_id="g-326-582", outcome_note=note,
                   content_by_artifact={
                       "zakpod1-recycle-engines.sh":
                           "check_serving_shape() { :; }\nverify_reload\n"})
    assert out["fired"] is False


def test_a_claim_that_names_no_file_is_structurally_uncheckable():
    """rb-8895 carries 1,863 B describing an implementation and ZERO filenames.

    Measured: no `*.py` / `*.sh` token anywhere in it. No tool can check such
    an entry against the store — not this one, not a better one. The test
    pins the honest verdict (`claims_checked == 0`, not `fired`) so nobody
    later reads the silence as an all-clear.
    """
    note = ("add a --direct MODE to the existing probe rather than a new "
            "script -- capture url in read_registry, add a probe_direct() "
            "that POSTs straight to engine.url. Mock-verified.")
    assert extract_claims(note) == []
    out = evaluate(goal_id="rb-8895", outcome_note=note,
                   content_by_artifact={})
    assert out["fired"] is False
    assert out["claims_checked"] == 0


def test_detector_has_a_live_call_site_in_iteration_close():
    """Wiring: the invocation exists and is not commented out.

    A detector with no caller is indistinguishable from one that always
    returns clean. This asserts the specific line, with its leading `bash`,
    and that the line is not a comment — the two things a grep can actually
    establish.
    """
    src = (REPO_ROOT / "core" / "scripts" / "iteration-close.sh").read_text(
        encoding="utf-8")
    hits = [ln.strip() for ln in src.splitlines()
            if "shipped-claim-store-check.sh" in ln]
    invocations = [ln for ln in hits
                   if not ln.startswith("#") and ln.startswith("bash ")]
    assert invocations, (
        "no uncommented `bash ... shipped-claim-store-check.sh` invocation in "
        "iteration-close.sh — the detector has been orphaned")
