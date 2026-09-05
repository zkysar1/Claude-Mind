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
    directed_pairs, retrieved_predicate, run, sample_clusters, sample_key)

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

    The key is a pure function of (goal, artifact path, cluster text), so the
    ONLY lever that changes which claims are examined is changing the artifact.
    """
    a = sample_key("g-357-44", "art.md", "Acme Corporation reported X in 2024.")
    b = sample_key("g-357-44", "art.md", "Acme Corporation reported X in 2024.")
    c = sample_key("g-357-44", "art.md", "Acme Corporation reported Y in 2024.")
    assert a == b
    assert a != c


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

def test_retrieved_predicate_is_None_when_the_manifest_is_empty(tmp_path):
    """guard-1760: a check that could not run must not report a pass."""
    assert retrieved_predicate("a-session-id-that-never-existed") is None


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
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ctx_scope", SCRIPTS / "context-reads.py")
    cr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cr)
    root = SCRIPTS.parent.parent

    # POSITIVE CONTROL — a class the manifest really does track.
    assert cr.is_in_scope(str(root / ".claude/skills/aspirations-verify/SKILL.md"))
    # The blind spot itself.
    assert not cr.is_in_scope(str(root / "agents/alpha/temp/note.md"))
    assert not cr.is_in_scope(str(root / ".claude/rules/read-before-edit.md"))


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
    """guard-1760 survives the union: nothing known means SKIP, never pass."""
    stub = tmp_path / "context-reads.py"
    stub.write_text(
        "def read_provenance(session_id=None):\n    return []\n"
        "def read_tracker(session_id=None):\n    return set()\n",
        encoding="utf-8")
    monkeypatch.setattr("q4_provenance_sample.SCRIPTS", tmp_path)
    assert retrieved_predicate("any-sid") is None
