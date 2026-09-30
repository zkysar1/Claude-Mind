#!/usr/bin/env python3
"""Q4 entity-fact provenance sampling + direction fidelity ().

The CLOSE-time half of the 2026-08-31 "double-check everything against sources"
directive. Sibling of test_ground_truth_citation_gate.py (g-357-45), which pins
the same incident at WRITE time — that gate fires on the diff that would create a
mangled artifact, this one fires on the artifact at close.

The three cases the goal's verification names are test_OUTCOME_1 /
test_OUTCOME_2 / test_OUTCOME_3.

TWO KINDS OF TEST BELOW, and the split is deliberate. The CONTROLS assert that
the checks stay QUIET where they should — a lint that fires on ordinary prose is
switched off within a day, and then the positives are worth nothing (guard-4166:
a fix whose effect is that something stops appearing needs a positive control
that does NOT flip). The LIMITATION tests pin what these heuristics deliberately
MISS. Writing a known miss down as prose lets it rot into a believed capability;
writing it as an assertion makes it falsifiable and makes the day someone widens
the heuristic a day a test turns red rather than a silent behaviour change
(guard-4374 — pin both buckets, not just the one you are reporting).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from q4_provenance_sample import (  # noqa: E402
    direction_contradictions, direction_fidelity, direction_findings,
    directed_pairs, expressible_predicate, retrieved_predicate, run,
    sample_clusters, sample_key)

VERDICT_CLI = SCRIPTS / "close-review-verdict.py"
SAMPLER_CLI = SCRIPTS / "q4-provenance-sample.py"

# The goal's named fixture. The real coach  claim used the alias
# "Dolphins" for Miami; alias resolution is deliberately out of scope (pinned by
# test_LIMITATION_entity_aliases_are_not_resolved), so the fixture states both
# sides with the same token and tests the mechanism the goal actually names:
# same citation, same entity set, REVERSED relation.
TRADE_SOURCE = ("Per https://example.invalid/trade-report, Denver sent a "
                "first-round pick, a third-round pick and a fourth-round pick "
                "to Miami in the 2024 trade.")
TRADE_CLAIM_BACKWARDS = ("Per https://example.invalid/trade-report, Miami sent "
                         "the first-round pick to Denver in the 2024 trade.")
TRADE_CLAIM_CORRECT = ("Per https://example.invalid/trade-report, Denver sent "
                       "the first-round pick to Miami in the 2024 trade.")


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# OUTCOME 1 — "Q4 wired into the verify phase with deterministic sampling"
# ---------------------------------------------------------------------------

def test_OUTCOME_1_sampling_is_deterministic_across_runs():
    """The same artifact yields the same sample every time."""
    text = "\n\n".join(
        f"Acme Corporation {i} reported revenue of {i}.5 billion in 2024."
        for i in range(20))
    first, total = sample_clusters(text, "g-357-44", "art.md", 5)
    second, total2 = sample_clusters(text, "g-357-44", "art.md", 5)
    assert total == total2 == 20
    assert [c.start_line for c in first] == [c.start_line for c in second]
    assert len(first) == 5


def test_OUTCOME_1_the_sample_is_not_rerollable_by_the_executor():
    """An executor cannot shop for a friendlier sample.

    The key is a pure function of its three strings, and sample_clusters feeds it
    (goal, artifact path, cluster ordinal), so the ONLY lever that changes which
    claims are examined is adding, removing or splitting a claim.
    """
    a = sample_key("g-357-44", "art.md", "Acme Corporation reported X in 2024.")
    b = sample_key("g-357-44", "art.md", "Acme Corporation reported X in 2024.")
    c = sample_key("g-357-44", "art.md", "Acme Corporation reported Y in 2024.")
    assert a == b
    assert a != c


def test_OUTCOME_1_a_citation_fix_does_not_reroll_the_sample():
    """: the remedy a finding asks for must not move the sample.

    A missing-citation finding asks for a source token INSIDE the cluster, which
    edits the cluster's text. Keyed on text, each fixed cluster drew a new rank
    and usually left the sample, so the confirming run examined clusters no run
    had shown, and an executor that fixed exactly what it was told re-ran 3-6
    times. Rewording a sampled claim must not move it either. The last assertion
    is the positive control: adding a claim still moves the sample, so holding
    still here is not a sampler that ignores its input.
    """
    lines = [f"Widget Industries {i} employs {i}00 people in 2024." for i in range(20)]
    first, total = sample_clusters("\n\n".join(lines), "g-357-44", "art.md", 5)
    assert total == 20 and len(first) == 5
    assert not any(c.source_tokens for c in first)
    for c in first:
        lines[(c.start_line - 1) // 2] += " Source: g-357-44."
    reworded = (first[0].start_line - 1) // 2
    lines[reworded] = lines[reworded].replace(" employs ", " has ")
    second, _ = sample_clusters("\n\n".join(lines), "g-357-44", "art.md", 5)
    assert [c.start_line for c in second] == [c.start_line for c in first]
    assert all(c.source_tokens for c in second), "the confirming run sees every fix"
    added = ["Widget Industries 99 has 9900 people in 2024."] + lines
    moved, _ = sample_clusters("\n\n".join(added), "g-357-44", "art.md", 5)
    assert ([c.fact_lines[0].text for c in moved]
            != [c.fact_lines[0].text for c in second]), "adding a claim moves the sample"


def test_OUTCOME_1_reports_total_coverage_not_just_the_sampled_count(tmp_path):
    """guard-3489: a clean verdict must carry the coverage it is clean over."""
    text = "\n\n".join(
        f"Widget Industries {i} employs {i}00 people in 2024." for i in range(12))
    art = _write(tmp_path, "art.md", text)
    result = run("g-357-44", [str(art)], n=3, session_id=None)
    assert result["clusters_total"] == 12
    assert result["sampled_count"] == 3
    assert result["sampled_count"] != result["clusters_total"]


def test_OUTCOME_1_an_unreadable_artifact_is_reported_not_silently_zero(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    result = run("g-357-44", [str(missing)], n=5, session_id=None)
    assert result["artifacts_missing"] == [str(missing)]
    assert result["artifacts_read"] == []
    assert result["verdict"] == "skipped"
    assert "no artifact could be read" in result["skip_reason"]


def test_OUTCOME_1_q4_is_WIRED_into_the_verify_skill():
    """rb-9476: a scoped fix can be present, correct and INERT.

    Every other assertion in this file passes against a check no phase invokes,
    so the wiring is itself a test — the same reasoning the sibling gate's
    registration test rests on.
    """
    skill = (SCRIPTS.parent.parent / ".claude" / "skills" /
             "aspirations-verify" / "SKILL.md").read_text(encoding="utf-8")
    assert "**Q4 ENTITY-FACT PROVENANCE**" in skill
    assert "q4-provenance-sample.sh" in skill
    assert "phase_progress.q4_passed" in skill
    # prior_checks must list the key, or a resumed verify re-runs Q4 forever.
    assert "`q4_passed`" in skill


# ---------------------------------------------------------------------------
# OUTCOME 2 — "reviewer source-fetch REJECTs the trade-direction fixture"
# ---------------------------------------------------------------------------

def test_OUTCOME_2_the_trade_direction_fixture_is_caught():
    contradictions = direction_contradictions(TRADE_CLAIM_BACKWARDS, TRADE_SOURCE)
    assert contradictions == [{"claim": ["miami", "denver"],
                               "source": ["denver", "miami"]}]
    fid = direction_fidelity(TRADE_SOURCE, TRADE_CLAIM_BACKWARDS)
    assert fid["passed"] is False
    assert "backwards" in direction_findings(fid)[0]


def test_OUTCOME_2_approve_is_REFUSED_end_to_end_by_the_verdict_cli(tmp_path):
    """The whole point of the outcome: the REJECT must come out of the real CLI.

    A unit-level assertion on direction_fidelity would have passed while the CLI
    still approved — that is exactly what happened during development, because
    the first directed_pairs split on newlines and the fixture's own prose wraps.
    """
    src = _write(tmp_path, "source.md", TRADE_SOURCE)
    art = _write(tmp_path, "artifact.md", TRADE_CLAIM_BACKWARDS)
    proc = subprocess.run(
        [sys.executable, str(VERDICT_CLI), "--goal", "g-fixture-trade",
         "--reviewer", "bravo", "--closer", "alpha",
         "--source-file", str(src), "--artifact-file", str(art), "--approve"],
        capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING to write APPROVE" in proc.stderr
    assert "direction-fidelity" in proc.stderr


def test_CONTROL_the_same_fixture_with_the_correct_direction_APPROVES(tmp_path):
    """guard-4166: the positive control must NOT flip."""
    src = _write(tmp_path, "source.md", TRADE_SOURCE)
    art = _write(tmp_path, "artifact.md", TRADE_CLAIM_CORRECT)
    proc = subprocess.run(
        [sys.executable, str(VERDICT_CLI), "--goal", "g-fixture-trade",
         "--reviewer", "bravo", "--closer", "alpha",
         "--source-file", str(src), "--artifact-file", str(art), "--approve"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout[:proc.stdout.rindex("}") + 1])
    assert payload["verdict"] == "APPROVE"
    assert payload["direction"]["passed"] is True


def test_CONTROL_citations_exist_is_BLIND_to_the_fixture():
    """WHY the new check had to exist, asserted rather than argued.

    If this test ever fails because `named_entities` widened, the direction
    check has stopped being the only thing standing between a reversed claim and
    an APPROVE — and whoever widened it should find that out here.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("crv", VERDICT_CLI)
    crv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spec and crv)
    fid = crv.source_fidelity(TRADE_SOURCE, TRADE_CLAIM_BACKWARDS)
    assert fid["passed"] is True
    assert fid["missing"] == [] and fid["invented"] == []


def test_the_verdict_record_reproduces_its_own_direction_veto(tmp_path):
    """guard-3743: a reader recomputes the verdict from the record alone."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("crv", VERDICT_CLI)
    crv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crv)
    fid = crv.source_fidelity(TRADE_SOURCE, TRADE_CLAIM_BACKWARDS)
    dirfid = direction_fidelity(TRADE_SOURCE, TRADE_CLAIM_BACKWARDS)
    payload = crv.build_verdict(goal_id="g", reviewer="r", fidelity=fid,
                                approve=True, checks=[], findings=[],
                                direction=dirfid)
    assert payload["verdict"] == "REJECT"
    assert payload["direction"]["contradictions"]
    assert payload["direction"]["source_pairs"] == [["denver", "miami"]]
    assert payload["direction"]["claim_pairs"] == [["miami", "denver"]]


def test_build_verdict_without_a_direction_block_keeps_the_old_behaviour():
    """`direction=None` must stay inert, or every pre-existing caller changes."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("crv", VERDICT_CLI)
    crv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(crv)
    fid = crv.source_fidelity("g-1 and g-2", "g-1 and g-2")
    payload = crv.build_verdict(goal_id="g", reviewer="r", fidelity=fid,
                                approve=True, checks=[], findings=[])
    assert payload["verdict"] == "APPROVE"
    assert payload["direction"] is None
    assert not any("direction-fidelity" in c for c in payload["checks"])


# ---------------------------------------------------------------------------
# OUTCOME 3 — "citations-exist-but-never-fetched flagged via provenance manifest"
# ---------------------------------------------------------------------------

def test_OUTCOME_3_a_cited_but_never_fetched_url_is_DECORATIVE(tmp_path, monkeypatch):
    art = _write(tmp_path, "art.md",
                 "Acme Corporation reported revenue of 4.2 billion in 2024,\n"
                 "per https://example.invalid/never-opened.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: "actually-fetched" in str(v)))
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    kinds = [f["kind"] for f in result["findings"]]
    assert kinds == ["decorative-citation"]
    assert result["verdict"] == "fail"


def test_CONTROL_a_citation_the_session_DID_fetch_is_clean(tmp_path, monkeypatch):
    art = _write(tmp_path, "art.md",
                 "Acme Corporation reported revenue of 4.2 billion in 2024,\n"
                 "per https://example.invalid/actually-fetched.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: "actually-fetched" in str(v)))
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    assert result["findings"] == []
    assert result["verdict"] == "pass"


def test_an_uncited_claim_is_missing_citation_not_decorative(tmp_path, monkeypatch):
    art = _write(tmp_path, "art.md",
                 "Widget Industries employs 12,000 people across its plants.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: True))
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    assert [f["kind"] for f in result["findings"]] == ["missing-citation"]


def test_CONTROL_an_UNVERIFIED_tagged_claim_passes_untouched(tmp_path, monkeypatch):
    art = _write(tmp_path, "art.md",
                 "Globex Holdings acquired 3 subsidiaries in 2023 "
                 "[UNVERIFIED -- model prior].\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: True))
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    assert result["findings"] == []
    assert result["verdict"] == "pass"


# ---------------------------------------------------------------------------
# The unreadable-manifest path: skipped is NOT pass
# ---------------------------------------------------------------------------

def test_an_unreadable_manifest_yields_SKIPPED_and_says_why(tmp_path, monkeypatch):
    art = _write(tmp_path, "art.md",
                 "Acme Corporation reported revenue of 4.2 billion in 2024,\n"
                 "per https://example.invalid/some-url.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate", lambda sid: None)
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    assert result["verdict"] == "skipped"
    assert result["verdict"] != "pass"
    assert "NOT a pass" in result["skip_reason"]
    assert result["sampled_count"] == 1


def test_the_cli_exit_code_is_the_answer(tmp_path):
    """0 = pass or skipped, 1 = fail. guard-1150: never wrap this in a pipe."""
    art = _write(tmp_path, "art.md",
                 "Widget Industries employs 12,000 people across its plants.\n")
    proc = subprocess.run(
        [sys.executable, str(SAMPLER_CLI), "--goal", "g-357-44",
         "--artifact", str(art), "--session-id", "no-such-session", "--json"],
        capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["verdict"] == "fail"


# ---------------------------------------------------------------------------
# REGRESSIONS — the two defects found by the end-to-end run during development
# ---------------------------------------------------------------------------

def test_REGRESSION_a_soft_wrapped_sentence_still_yields_a_directed_pair():
    """Prose wraps. The first directed_pairs split on "\\n" as if it were a
    sentence boundary, which tore the verb from its "to <B>" and returned an
    EMPTY pair set on the fixture — silently blind, and green at unit level
    because every smoke test used single-line strings."""
    wrapped = "Per https://example.invalid/x, Miami sent the first-round pick\nto Denver in 2024."
    assert directed_pairs(wrapped) == {("miami", "denver")}


def test_REGRESSION_a_citation_on_the_wrapped_line_is_seen(tmp_path, monkeypatch):
    """The sampler passed only fact LINES to analyze(), but source tokens are
    collected over the whole cluster — so a citation on the next line was
    discarded and the cluster read as uncited. Wrong in the ALARM direction."""
    art = _write(tmp_path, "art.md",
                 "Acme Corporation reported revenue of 4.2 billion in 2024, per\n"
                 "https://example.invalid/actually-fetched.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: "actually-fetched" in str(v)))
    result = run("g-357-44", [str(art)], n=5, session_id="x")
    assert result["findings"] == [], result["findings"]


def test_REGRESSION_a_paragraph_break_still_separates_two_claims():
    """The wrap fix must not go too far the other way: a blank line is a real
    boundary, so a verb in one paragraph may not bind a "to <B>" in the next."""
    two = "Miami sent the pick away.\n\nDenver traded a player to Chicago."
    assert ("miami", "chicago") not in directed_pairs(two)


# ---------------------------------------------------------------------------
# LIMITATIONS — pinned so a widening is a red test, not a silent change
# ---------------------------------------------------------------------------

def test_LIMITATION_entity_aliases_are_not_resolved():
    """The real coach claim said "Dolphins" where the source said "Miami".
    Resolving that needs an alias table this check deliberately does not have,
    so the reversed claim goes UNCAUGHT in its original wording. Stated as an
    assertion because a limitation written only in prose rots into a believed
    capability."""
    aliased = "Per https://example.invalid/trade-report, the Dolphins sent the pick to Denver."
    assert direction_contradictions(aliased, TRADE_SOURCE) == []


def test_LIMITATION_source_silence_is_not_a_contradiction():
    """Absence of evidence must never veto an approval — that would make the
    check fire on every claim whose source phrases the relation differently."""
    assert direction_contradictions(TRADE_CLAIM_BACKWARDS,
                                    "Some unrelated prose about the season.") == []
    assert direction_fidelity("Some unrelated prose.", TRADE_CLAIM_BACKWARDS)["passed"]


def test_LIMITATION_sold_and_bought_are_not_in_the_verb_family():
    """"A sold X to B" and "B bought X from A" are the SAME transfer written in
    opposite syntactic directions. Admitting those verbs would manufacture
    contradictions out of correct paraphrase, so they are deliberately absent."""
    assert directed_pairs("Acme sold the unit to Globex.") == set()
    assert direction_contradictions("Acme sold the unit to Globex.",
                                    "Globex sold the unit to Acme.") == []


def test_CONTROL_ordinary_framework_prose_yields_no_directed_pairs():
    """The quiet case. If this ever fires, the direction check has started
    flagging normal writing and will be switched off."""
    prose = ("The gate is advisory by default and never blocks. Read the verdict\n"
             "rather than the exit code, because a skip and a pass are not the\n"
             "same answer. See core/config/rationale/verify-check-unevaluatable.md.")
    assert directed_pairs(prose) == set()


def test_LIMITATION_the_SAMPLER_and_the_DIRECTION_check_have_different_reach(tmp_path):
    """The two halves of Q4 do not see the same sentences, and that asymmetry is
    load-bearing rather than accidental.

    `direction_fidelity` carries this module's own prose-entity notion, so it
    catches the reversed trade sentence (OUTCOME_2 above). The SAMPLER inherits
    `is_entity_bearing` from ground_truth_citation, which wants a proper-noun
    RUN / year / number+unit / currency AND an assertion verb — and the bare
    trade sentence satisfies neither, so it produces ZERO clusters.

    Measured while verifying g-357-44: `is_entity_bearing("Miami sent the
    first-round pick to Denver in exchange")` is False. The consequence a caller
    must not misread is the verdict: a pure-prose artifact comes back `skipped`,
    which is NOT `pass` — the exact distinction aspirations-verify's Q4 block
    tells the reader never to collapse. Pinned here so that if the cluster
    predicate is ever widened to ordinary prose, this goes red and the reach
    change is a decision rather than a surprise.
    """
    from ground_truth_citation import is_entity_bearing
    bare = "Miami sent the first-round pick to Denver in exchange"
    assert is_entity_bearing(bare) is False

    # ... yet the direction half DOES see it, which is why it exists.
    assert directed_pairs("Miami sent the first-round pick to Denver.") == {
        ("miami", "denver")}

    art = _write(tmp_path, "prose.md", bare + ".\n")
    result = run("g-357-44", [str(art)], n=5, session_id=None)
    assert result["clusters_total"] == 0
    assert result["verdict"] == "skipped"
    assert result["verdict"] != "pass"


def test_LIMITATION_citations_to_manifest_UNTRACKED_paths_always_read_decorative():
    """The alarm-direction blind spot, pinned because it is the one that gets a
    check switched off.

    `context-reads.is_in_scope` tracks only some path classes. A Read of anything
    outside them is never recorded, so a citation to such a path is reported
    `decorative-citation` no matter how genuinely the session fetched it.
    Discovered by running Q4 against its own goal's closure note, which cited a
    file under `agents/**` that had just been opened with the Read tool.

    NOTE the probe shape: `is_in_scope` answers False for EVERY relative path, so
    a scope census taken with relative paths reports a uniform, plausible, and
    completely wrong zero. The in-scope assertion below is the positive control
    that makes the out-of-scope ones mean something (guard-2421).
    """
    # Use the SHARED loader, never a local exec_module of context-reads.py.
    # That module arms `threading.Timer(10, lambda: os._exit(0))` at import
    # scope; uncancelled inside pytest it kills the interpreter ~10s later with
    # no traceback, no epilogue, no summary line and exit status 0. This call
    # site did exactly that and voided suite chunk 09 at 88% () —
    # the same defect the helper's docstring records against chunk 04 at 13%
    # (). load_context_reads() cancels the timer and asserts `_timer`
    # still exists, so a rename fails loudly instead of silently re-arming it.
    from _context_reads_helper import load_context_reads
    cr = load_context_reads()
    root = SCRIPTS.parent.parent

    # `.as_posix()`, NEVER `str()` — is_in_scope's parameter is literally named
    # `normalized`: it normalizes its PATTERNS (prefix.replace("\\", "/")) and
    # not its INPUT, so a Windows `str(Path)` arrives backslash-separated and
    # returns False for everything. This test used str() and was therefore
    # GREEN on Linux and RED on Windows (measured 2026-09-05, ) —
    # and the Windows red was the lucky half. The two negative assertions
    # below would have passed VACUOUSLY on Windows for the same reason the
    # positive control failed, which is the exact "uniform, plausible and
    # completely wrong" result this test's own docstring warns about. The
    # positive control is what caught it; that is what positive controls are for.
    # POSITIVE CONTROL — a class the manifest really does track.
    assert cr.is_in_scope((root / ".claude/skills/aspirations-verify/SKILL.md").as_posix())
    # The blind spot itself.
    assert not cr.is_in_scope((root / "agents/alpha/temp/note.md").as_posix())
    assert not cr.is_in_scope((root / ".claude/rules/read-before-edit.md").as_posix())


def test_REGRESSION_retrieved_predicate_consults_BOTH_halves_of_the_tracker(
        tmp_path, monkeypatch):
    """The third development defect, and the one that would have killed the check.

    context-reads keeps two corpora: `read_provenance()` yields the `#prov:`
    retrieval-QUERY lines, and `read_tracker()` yields the paths the session
    actually opened. The first cut of `retrieved_predicate` consulted provenance
    ALONE, so every FILE citation reported `decorative-citation` however genuinely
    the session had Read it — wrong in the ALARM direction, which is how a check
    gets switched off. Caught only by running Q4 against its own goal's closure
    note (g-357-44).

    Note WHY the original positive control missed it: that probe used a retrieval
    QUERY string, which lives in the half that was being read. A control drawn
    from the same half as the bug cannot see the bug.
    """
    stub = tmp_path / "context-reads.py"
    stub.write_text(
        "def read_provenance(session_id=None):\n"
        "    return [('retrieval', 'ts', 'some-query-string')]\n"
        "def read_tracker(session_id=None):\n"
        "    return {'/repo/.claude/skills/aspirations-verify/SKILL.md'}\n",
        encoding="utf-8")
    monkeypatch.setattr("q4_provenance_sample.SCRIPTS", tmp_path)

    pred = retrieved_predicate("any-sid")
    assert pred is not None
    # The FILE half — this is the assertion that fails on the reverted code.
    assert pred("node", "claude/skills/aspirations-verify") is True
    # The QUERY half must keep working; the fix is a union, not a swap.
    assert pred("node", "some-query-string") is True
    # And an unrelated citation still reads as unretrieved.
    assert pred("url", "https://example.invalid/never-opened") is False


def test_retrieved_predicate_still_returns_None_when_BOTH_halves_are_empty(
        tmp_path, monkeypatch):
    """guard-1760 survives the union: nothing known means SKIP, never pass.

    SOLE hermetic pin of the empty-manifest contract since 2026-09-05
    (g-115-9059). A sibling test asserted the same contract by passing a
    never-existed sid to the REAL loader, which is not a contract test at
    all: `context-reads.tracker_path` falls back to the agent-wide
    `session/context-reads.txt` for any sid without a forked body-WM, and
    that file's existence varies by BOX (present on a reducer-shaped box,
    absent on a worker) and by position in the COMPACTION cycle (the hooks
    reset it each cycle). It therefore passed on cc-07 and failed on cc-08
    with neither box measuring wrong. Stub both halves; never reach for the
    live filesystem to prove "empty".
    """
    stub = tmp_path / "context-reads.py"
    stub.write_text(
        "def read_provenance(session_id=None):\n    return []\n"
        "def read_tracker(session_id=None):\n    return set()\n",
        encoding="utf-8")
    monkeypatch.setattr("q4_provenance_sample.SCRIPTS", tmp_path)
    assert retrieved_predicate("any-sid") is None


# ---------------------------------------------------------------------------
# THE THIRD VERDICT () — a citation the manifest cannot express is a
# check that did not run, not a check that failed.
# ---------------------------------------------------------------------------

_UNRECORDABLE = ("Widget Industries employs 12,000 people across its plants, "
                 "per core/githooks/commit-msg.")


def test_an_unrecordable_citation_is_unadjudicable_NOT_decorative():
    """The defect: `decorative-citation` asserts the session never fetched the
    source. That assertion is only available when the manifest COULD have held the
    answer; where it structurally could not, nothing was measured."""
    from ground_truth_citation import analyze
    kinds = [f.kind for f in analyze(_UNRECORDABLE,
                                     retrieved=lambda k, v: False,
                                     expressible=lambda k, v: False)]
    assert kinds == ["unadjudicable-citation"], kinds


def test_CONTROL_an_EXPRESSIBLE_citation_still_reads_decorative():
    """The other half, and the one that keeps this from gutting the check: when
    the manifest COULD have recorded it and did not, the citation is decorative
    exactly as before. Without this control the test above is equally satisfied by
    a change that demotes every citation."""
    from ground_truth_citation import analyze
    kinds = [f.kind for f in analyze(_UNRECORDABLE,
                                     retrieved=lambda k, v: False,
                                     expressible=lambda k, v: True)]
    assert kinds == ["decorative-citation"], kinds


def test_CONTROL_omitting_the_predicate_preserves_the_PRE_CHANGE_behaviour():
    """`expressible` defaults to None and None must mean "assume expressible" —
    the fail-safe direction, since demoting on doubt suppresses alarms. Every
    existing caller that does not pass it is unaffected."""
    from ground_truth_citation import analyze
    kinds = [f.kind for f in analyze(_UNRECORDABLE, retrieved=lambda k, v: False)]
    assert kinds == ["decorative-citation"], kinds


def test_a_run_whose_citations_are_ALL_unadjudicable_is_SKIPPED_not_failed(
        tmp_path, monkeypatch):
    """THE UNBLOCK. skipped exits 0, so the close stops being refused — and it is
    still not a `pass`, because guard-1760 forbids reporting an unrun check as
    one."""
    art = _write(tmp_path, "art.md", _UNRECORDABLE + "\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: False))
    monkeypatch.setattr("q4_provenance_sample.expressible_predicate",
                        lambda sid: (lambda k, v: False))
    result = run("g-115-9059", [str(art)], n=5, session_id="x")
    assert result["verdict"] == "skipped", result
    assert result["verdict"] != "pass"
    assert result["unadjudicable_count"] == 1
    assert "NOT ADJUDICABLE" in result["skip_reason"]


def test_a_BLOCKING_finding_still_fails_beside_an_unadjudicable_one(
        tmp_path, monkeypatch):
    """The demotion is per-citation, never per-run: one genuine decorative
    citation must still fail the whole verdict even when another is unadjudicable.
    A run-level demotion would let a real finding ride out beside an excused one."""
    art = _write(tmp_path, "art.md",
                 _UNRECORDABLE + "\n\n"
                 "Globex Limited operates 37 refineries worldwide, per\n"
                 "https://example.invalid/never-fetched.\n")
    monkeypatch.setattr("q4_provenance_sample.retrieved_predicate",
                        lambda sid: (lambda k, v: False))
    # Only the URL is expressible -> it stays decorative; the path is demoted.
    monkeypatch.setattr("q4_provenance_sample.expressible_predicate",
                        lambda sid: (lambda k, v: k == "url"))
    result = run("g-115-9059", [str(art)], n=5, session_id="x")
    assert result["verdict"] == "fail", result
    assert result["unadjudicable_count"] == 1
    kinds = sorted(f["kind"] for f in result["findings"])
    assert kinds == ["decorative-citation", "unadjudicable-citation"], kinds


def test_expressible_predicate_demotes_ONLY_real_out_of_scope_files():
    """The predicate itself, against the live repo. Three arms, because the
    failure that matters is over-demotion: a bare tree-node key resolves to no
    file and is recordable via a `#prov: node` row, so demoting it would switch
    off a real alarm."""
    pred = expressible_predicate("any-sid")
    assert pred is not None
    # In advisory scope (core/scripts) -> expressible, check stays ON.
    assert pred("node-key", "core/scripts/q4-provenance-sample") is True
    # A real file OUTSIDE advisory scope -> the manifest can never hold it.
    assert pred("node-key", "core/githooks/commit-msg") is False
    # Dotted, and the largest demotable class: _NODE_KEY strips the leading dot.
    assert pred("node-key", "claude/rules/read-before-edit") is False
    # Resolves to no file at all (a tree key) -> NOT demoted.
    assert pred("node-key", "system/daemon-only-architecture") is True
    # Non-path kinds are recordable via PROVENANCE_KINDS -> never demoted.
    assert pred("url", "https://example.invalid/whatever") is True


def _stub_world(tmp_path, monkeypatch):
    """A tmp WORLD root + a stub context-reads exposing it, fully hermetic.

    Hermetic on purpose: the real `world/` is an EXTERNAL, gitignored path whose
    contents differ per box, so asserting against a live world file would pin the
    BOX rather than the contract — the exact defect that made two sibling tests
    pass on cc-07 and fail on cc-08 (g-115-9059 unit 2).
    """
    world = tmp_path / "w"
    (world / "telemetry").mkdir(parents=True)
    (world / "telemetry" / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    (world / "conventions").mkdir()
    (world / "conventions" / "cap.md").write_text("x\n", encoding="utf-8")
    stub = tmp_path / "context-reads.py"
    stub.write_text(
        "from pathlib import Path\n"
        "WORLD_DIR = Path(%r)\n"
        "def is_in_scope_advisory(p):\n"
        "    return '/conventions/' in str(p).replace(chr(92), '/')\n"
        % world.as_posix(),
        encoding="utf-8")
    monkeypatch.setattr("q4_provenance_sample.SCRIPTS", tmp_path)
    return world


def test_an_out_of_scope_WORLD_path_is_demoted_not_left_blocking(
        tmp_path, monkeypatch):
    """`world/` is EXTERNAL, so resolution that only tries the repo root finds
    nothing and falls through to the default-True fail-safe — reporting
    `decorative-citation` for a file the session genuinely read.

    Measured g-306-401 (cc-07): `world/telemetry/adjudication-lane-ledger.jsonl`
    was read IN FULL (15,804 B, 28 lines) and cited for a claim it directly
    supports, and Q4 still blocked the close. This is citation class (2) of the
    four this goal enumerates.
    """
    _stub_world(tmp_path, monkeypatch)
    pred = expressible_predicate("any-sid")
    assert pred is not None
    assert pred("node-key", "world/telemetry/ledger.jsonl") is False


def test_CONTROL_an_IN_scope_world_path_still_blocks(tmp_path, monkeypatch):
    """Resolving world paths must not demote them WHOLESALE. A world path the
    recorder DOES track stays adjudicable, so the decorative alarm still fires
    where it can legitimately fire. Without this arm the fix above is
    indistinguishable from 'demote anything starting with world/'."""
    _stub_world(tmp_path, monkeypatch)
    pred = expressible_predicate("any-sid")
    assert pred("node-key", "world/conventions/cap.md") is True


def test_CONTROL_a_world_path_that_resolves_to_NOTHING_still_blocks(
        tmp_path, monkeypatch):
    """The predicate's stated invariant: demote ONLY where the token POSITIVELY
    resolves to a real file the scope predicate excludes. A `world/`-prefixed
    token naming no file must keep the check ON — otherwise the prefix alone
    would become a way to switch off a real alarm (guard-1760)."""
    _stub_world(tmp_path, monkeypatch)
    pred = expressible_predicate("any-sid")
    assert pred("node-key", "world/telemetry/no-such-file.jsonl") is True


# ---------------------------------------------------------------------------
# ABSOLUTE-PATH CITATIONS () — the tokenizer drops the leading slash
# ---------------------------------------------------------------------------
#
# `_NODE_KEY` starts at a `\b`, so a citation written as
# `<repo>/core/githooks/commit-msg` (absolute paths are what the Read tool
# emits) is tokenized WITHOUT its leading slash:
# `opt/<repo>/core/githooks/commit-msg`. Pre-fix, `_candidates` tried only
# root/tok, root/.tok and the world forms — none exists — so the token fell
# through to the default-True fail-safe: a correctly cited, genuinely READ
# out-of-scope file reported `decorative-citation` (a blocking FAIL) when
# cited absolutely, and was demoted to `unadjudicable-citation` when cited
# repo-relative. Measured at HEAD 2026-09-29 ( description): the
# same file answered False relative / True absolute, and the end-to-end
# verify-preflight on  flipped from rc 3 (FAIL decorative on three
# clusters) to rc 0 (SKIPPED, 4 unadjudicable) the moment the same citations
# were rewritten repo-relative. The verdict depended on the author's path
# STYLE, not on what the session actually read.
#
# The fix restores the dropped slash in `_candidates`, guarded to tokens that
# start with THIS box's repo root (or the world root). The prefix is DERIVED
# from the live root in every assertion below — never hardcoded to one box's
# path — because the defect is shape-relative to wherever the repo lives.

def _repo_root_prefix() -> str:
    """`str(root).lstrip('/')` — the shape the tokenizer leaves behind after
    dropping the leading slash of an absolute path under the repo root."""
    return SCRIPTS.parent.parent.as_posix().lstrip("/")


def test_OUTCOME_1_an_absolute_path_citation_gets_the_same_verdict_as_relative():
    """Goal outcome 1, against the live repo: the same file, cited
    repo-relative and cited as the absolute path the Read tool emits, must
    receive the SAME Q4 verdict.

    The RELATIVE arm is the positive control (guard-2421): it proves this box
    can answer False at all, which makes the absolute arm's False meaningful
    instead of a uniform-True read. The ABSOLUTE arm is False against the
    pre-fix `_candidates` (the token resolves to nothing and the default-True
    fail-safe answers True) — this is the pin the goal names.
    """
    pred = expressible_predicate("any-sid")
    assert pred is not None
    prefix = _repo_root_prefix()
    # One real file OUTSIDE the recorder's advisory scope, both styles.
    assert pred("node-key", "core/githooks/commit-msg") is False
    # THE OUTCOME: identical verdict for the same file, path style removed
    # as a variable.
    assert pred("node-key", f"{prefix}/core/githooks/commit-msg") is False


def test_OUTCOME_1_an_in_scope_absolute_path_stays_ADJUDICABLE():
    """The no-suppression half of outcome 1: an in-scope file cited
    absolutely still resolves to an IN-scope hit and stays expressible — the
    restored slash must not demote a citation whose source the manifest
    genuinely tracks, or the decorative alarm would be suppressed (guard-1901)
    for the very path style the Read tool emits. Without this arm the fix is
    indistinguishable from 'demote anything that resolves to a path'."""
    pred = expressible_predicate("any-sid")
    assert pred is not None
    prefix = _repo_root_prefix()
    # Positive control first: the repo-relative form is expressible.
    assert pred("node-key", "core/scripts/retrieve") is True
    # The absolute form of the SAME in-scope file answers the same.
    assert pred("node-key", f"{prefix}/core/scripts/retrieve") is True


def test_OUTCOME_1_an_absolute_world_root_path_gets_the_same_verdict_as_relative():
    """Symmetric to the repo-root OUTCOME_1 pin, for the WORLD half of the
    fix. `world/` is an EXTERNAL path (not under the repo), so a world
    citation written as the absolute the Read tool emits arrives with ITS
    leading slash dropped too, and the world-root prefix guard is what
    restores it. The probe this pin codifies (measured 2026-09-30, live box):
    `world/audit-reports/README.md` answered False in BOTH the repo-relative
    and the absolute forms, and an absolute world path naming NO file stayed
    True — the two roots must be pinned, not just resolved.

    Skipped on an UNINITIALIZED box: `WORLD_DIR` is None then and `_candidates`
    skips the world forms entirely (the fix's own docstring), so there is no
    world file to cite and the pin would be testing nothing. The repo-root pin
    does not have this problem because the repo root always exists.
    """
    import pytest

    from _context_reads_helper import load_context_reads

    cr = load_context_reads()
    world = getattr(cr, "WORLD_DIR", None)
    if world is None:
        pytest.skip("WORLD_DIR unset (uninitialized box) — world forms are skipped")
    pred = expressible_predicate("any-sid")
    assert pred is not None
    world_files = sorted(
        p for p in Path(world).rglob("*.md")
        if p.is_file()
    )
    if not world_files:
        pytest.skip("no .md files under WORLD_DIR to cite")
    # Out-of-scope arm — the pin: the same file, repo-relative and absolute
    # (leading slash dropped), must draw the SAME verdict.
    wf = next(
        (p for p in world_files
         if not cr.is_in_scope_advisory(p.as_posix())),
        None,
    )
    if wf is not None:
        rel = "world/" + wf.relative_to(world).as_posix()
        abs_form = str(world).lstrip("/") + "/" + wf.relative_to(world).as_posix()
        # Positive control first (guard-2421): the relative form answers
        # False at all on this box.
        assert pred("node-key", rel) is False
        # THE OUTCOME: identical verdict for the same file, path style
        # removed as a variable — the world-root half of outcome 1.
        assert pred("node-key", abs_form) is False
    # In-scope arm — the no-suppression half: an in-scope world file cited
    # absolutely must stay ADJUDICABLE.
    wf_in = next(
        (p for p in world_files
         if cr.is_in_scope_advisory(p.as_posix())),
        None,
    )
    if wf_in is not None:
        rel_in = "world/" + wf_in.relative_to(world).as_posix()
        abs_in = str(world).lstrip("/") + "/" + wf_in.relative_to(world).as_posix()
        assert pred("node-key", rel_in) is True
        assert pred("node-key", abs_in) is True
    # Nothing arm: an absolute world-shaped token naming NO file keeps the
    # check ON — the root-prefix guard is not a way to switch off an alarm
    # (guard-1760), for the world root exactly as for the repo root.
    assert pred("node-key",
                str(world).lstrip("/") + "/does/not/exist-xyz") is True


def test_CONTROL_an_absolute_path_that_resolves_to_NOTHING_still_blocks():
    """The stated invariant survives the new candidate: demote ONLY where the
    token POSITIVELY resolves to a real file the scope predicate excludes. An
    absolute-shaped token naming NO file must keep the check ON — otherwise
    the root-prefix guard itself becomes a way to switch off a real alarm
    (guard-1760). The bare-node-key control g-115-9266 preserves is re-asserted
    here because the fix and that goal touch the same function."""
    pred = expressible_predicate("any-sid")
    assert pred is not None
    prefix = _repo_root_prefix()
    # Starts with the repo-root prefix, so the restored-slash candidate IS
    # tried — and names no file.
    assert pred("node-key", f"{prefix}/system/daemon-only-architecture") is True
    # Bare tree node key: resolves to nothing, stays adjudicable, unchanged.
    assert pred("node-key", "system/daemon-only-architecture") is True


def test_REGRESSION_analyze_reports_unadjudicable_for_both_citation_styles():
    """End-to-end at the `analyze` level, the way the goal measured it: one
    cluster citing the out-of-scope file repo-relative, a SIBLING cluster
    citing the SAME file as the absolute path the Read tool emits. Post-fix
    both clusters draw `unadjudicable-citation`; pre-fix the absolute one drew
    the blocking `decorative-citation` (its absolute token fell through the
    default-True fail-safe while its relative twin demoted) — the g-306-541
    specimen, where rewriting the same citations repo-relative flipped
    verify-preflight from rc 3 to rc 0.

    Two clusters, not one, because `analyze` reports ONE finding per cluster
    and demotes a cluster only when NO checkable citation in it is
    expressible: with both styles in one cluster the pre-fix code would still
    demote it (the relative token carries the cluster), and the pin would not
    fail on the reverted code. The two paragraphs are the g-306-541 shape:
    independent claims, independent citation styles, one per cluster.

    LIVE repo, like the other predicate tests in this file: the absolute
    prefix is shape-relative to wherever the repo lives, so it is DERIVED from
    the live root here too, and a stub would only pin the box's tmp_path
    shape — pytest's tmp_path parents carry uppercase/underscored test names,
    which the tokenizer cannot even cross, so a stubbed root would leave root
    fragments that resolve to nothing and default to True. That would turn
    this pin into a test of the fail-safe instead of the fix (guard-1866: a
    control that cannot reach the code under test has no resolving power)."""
    from ground_truth_citation import analyze, source_tokens
    prefix = _repo_root_prefix()
    text = ("Widget Industries employs 12,000 people across its plants, per "
            "core/githooks/commit-msg.\n\n"
            "Widget Industries operates 37 refineries worldwide, per "
            f"/{prefix}/core/githooks/commit-msg.\n")
    # What the tokenizer MUST see for this test to be the pin it claims to be:
    # both shapes present, the absolute one WITH the leading slash still
    # dropped (the defect's input shape, re-asserted so a tokenizer change
    # fails here loudly instead of silently unpinning the fix).
    toks = source_tokens(text)
    assert ("node-key", "core/githooks/commit-msg") in toks
    assert ("node-key", f"{prefix}/core/githooks/commit-msg") in toks
    kinds = sorted(f.kind for f in analyze(
        text, retrieved=lambda k, v: False,
        expressible=expressible_predicate("any-sid")))
    assert kinds == ["unadjudicable-citation", "unadjudicable-citation"], kinds
