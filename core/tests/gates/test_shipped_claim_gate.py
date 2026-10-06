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


# --- sentence-scoped binding () ---------------------------------
#
# The notes below are SANITISED PRODUCTION SHAPES: the structure of firings
# measured over 887 live closing notes (the old binding fired on 61 of them, 0
# of which was a genuine shipped claim), with the names made generic.


def test_symbol_in_another_sentence_never_binds_to_the_artifact():
    """The three-subject note: each token belongs to a DIFFERENT sentence.

    The old rule bound every flag to the nearest preceding filename however
    far back it was, so a flag about the domain suite and one about the
    duplication gate were charged to the script named in the first sentence.
    """
    note = (
        "Added a stale-lock check to sync-scripts.sh. The domain suite prints "
        "no summary line: --collect-only ends on a per-file count, not a total.\n"
        "Filed with an AUDITED --override-duplication after the gate refused.\n"
        "Separately, deploy-tool.sh was re-read for g-000-1."
    )
    assert extract_claims(note) == []
    out = evaluate(goal_id="g-x", outcome_note=note,
                   content_by_artifact={"sync-scripts.sh": "echo lock\n"})
    assert out["fired"] is False


def test_artifact_named_as_transport_in_line_one_is_not_a_claim_target():
    """An artifact in the first line, a symbol paragraphs later."""
    note = (
        "Reached the store via world/scripts/transport.sh (probe returned ok rc=0).\n"
        "Added a join rule: FLAG lines follow their tree's PROC carrying the "
        "same `character`.\n"
        "A per-character breakdown follows."
    )
    assert extract_claims(note) == []


def test_same_sentence_claim_still_fires_after_the_narrowing():
    """The positive control for the two tests above: scoping must not blind it."""
    note = "Added a `character` join rule to world/scripts/joiner.sh."
    out = evaluate(goal_id="g-x", outcome_note=note,
                   content_by_artifact={"world/scripts/joiner.sh": "echo hi\n"})
    assert out["fired"] is True
    assert out["mismatches"][0]["missing"] == ["character"]


def test_passive_claim_fires_and_present_symbol_is_silent():
    note = ("Defect: a POST endpoint `actor-send` was added to "
            "world/scripts/bridge.py with no admission decision recorded.")
    absent = evaluate(goal_id="g-x", outcome_note=note,
                      content_by_artifact={"world/scripts/bridge.py": "x = 1\n"})
    present = evaluate(goal_id="g-x", outcome_note=note, content_by_artifact={
        "world/scripts/bridge.py": "route('actor-send')\n"})
    assert absent["fired"] is True
    assert present["fired"] is False


def test_flag_is_not_shipped_into_a_data_or_config_artifact():
    """Records and declarative config have no CLI surface to ship a flag into."""
    note = "Wrote the ledger row to sweep-seen.jsonl after the sweep ran with --max 80."
    assert extract_claims(note) == []
    code = "Wrote the retry row to sweep.py after the sweep ran with --max 80."
    assert [c["symbols"] for c in extract_claims(code)] == [["--max"]]


def test_flag_inside_a_backticked_command_line_is_a_transcript_not_a_claim():
    """`tool.sh --repo` and `git diff --no-renames` record what was RUN."""
    note = ("Added a guard check: `deploy-tool.sh --repo` printed CLEAR and "
            "`git diff --no-renames --diff-filter=A` listed 3 files.")
    assert extract_claims(note) == []
    # A span that STARTS with the flag is the symbol itself, so it is kept.
    own = "Added `--no-renames <mode>` to deploy-tool.sh."
    assert [c["symbols"] for c in extract_claims(own)] == [["--no-renames"]]


def test_a_filename_is_never_a_symbol_claim_about_another_file():
    note = ("Appended a line to world/scripts/reader.sh; "
            "`full-recommender.sh` printed no changes.")
    assert extract_claims(note) == []


def test_multi_line_list_claims_do_not_bind_known_limitation():
    """PINNED LIMITATION: a header line plus bullet children is not one claim.

    The narrowing costs this shape, and no corpus case needed it: widening it
    is a deliberate act that must be measured against the live ledger first.
    """
    note = "Added to probe.py:\n- a --direct flag\n- a probe_direct() helper"
    assert extract_claims(note) == []


def _load_store_check_cli():
    import importlib.util
    path = REPO_ROOT / "core" / "scripts" / "shipped-claim-store-check.py"
    spec = importlib.util.spec_from_file_location("shipped_claim_store_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_framework_path_is_never_resolved_by_basename_to_a_store_file():
    """`core/config/aspirations.yaml` read as a 47-byte store file of that name."""
    cli = _load_store_check_cli()
    for tok in ("core/config/aspirations.yaml", "agents/a/changelog.jsonl",
                "mind_api/src/world/pipeline_write.py"):
        cands = cli._resolve_virtual(tok)
        assert all(c.startswith(("world/", "meta/")) and tok in c for c in cands), cands
    assert cli._resolve_virtual("WORLD_PATH/scripts/x.sh") == ["world/scripts/x.sh"]
    assert cli._resolve_virtual("world/scripts/x.sh") == ["world/scripts/x.sh"]
    assert cli._resolve_virtual("scripts/x.sh") == ["world/scripts/x.sh",
                                                    "meta/scripts/x.sh"]
    assert cli._resolve_virtual("x.sh")[0] == "world/scripts/x.sh"


def _verdict_args(goal, **kw):
    import types
    return types.SimpleNamespace(goal=goal, verdict=kw.get("verdict", "false_positive"),
                                 reason=kw.get("reason", "the claim was never made"),
                                 basis=kw.get("basis"), row_ts=kw.get("row_ts"))


def test_verdict_row_marks_a_firing_without_editing_the_ledger(tmp_path, monkeypatch):
    """Append-only disposition: a later `kind: verdict` row, never an edit."""
    import json
    cli = _load_store_check_cli()
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(cli, "_ledger_path", lambda: ledger)
    firing = {"ts": "2026-08-24T10:09:37", "agent": "a", "goal_id": "g-fired",
              "mismatches": [{"artifact": "x.sh", "missing": ["--f"]}], "resolved": {}}
    ledger.write_text(json.dumps(firing) + "\n", encoding="utf-8")

    assert cli._record_verdict(_verdict_args("g-fired", basis="note read in-turn")) == 0
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == firing, "the original firing row must be untouched"
    v = rows[1]
    assert (v["kind"], v["goal_id"], v["verdict"]) == ("verdict", "g-fired", "false_positive")
    assert v["mismatches"] == [] and v["resolved"] == {}, "legacy readers must see an empty row"
    assert v["basis"] == "note read in-turn"
    # A verdict row is not a firing, so it cannot be adjudicated in turn.
    assert cli._firing_ts("g-fired") == ["2026-08-24T10:09:37"]


def test_verdict_refused_without_reason_firing_or_matching_row_ts(tmp_path, monkeypatch):
    import json
    cli = _load_store_check_cli()
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(cli, "_ledger_path", lambda: ledger)
    ledger.write_text(json.dumps({"ts": "t1", "goal_id": "g-fired", "mismatches": []}) + "\n",
                      encoding="utf-8")
    before = ledger.read_text(encoding="utf-8")
    assert cli._record_verdict(_verdict_args("g-fired", reason="  ")) == 2
    assert cli._record_verdict(_verdict_args("g-never-fired")) == 2
    assert cli._record_verdict(_verdict_args("g-fired", row_ts="t-other")) == 2
    assert ledger.read_text(encoding="utf-8") == before, "a refusal must append nothing"


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
