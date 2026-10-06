#!/usr/bin/env python3
"""Ground-truth citation gate () — the write-path half of the
2026-08-31 no-publish-from-memory directive.

Sibling of test_close_review_coach_fixture.py (g-357-42), which pins the same
incident at CLOSE time. This file pins it at WRITE time, one moment earlier: the
close-review gate can only catch a mangled artifact after it exists, while this
gate fires on the diff that would create it.

The three cases the goal's verification names verbatim are
test_OUTCOME_1 / test_OUTCOME_2 / test_OUTCOME_3; everything else is a control.

WHY THE CONTROLS OUTNUMBER THE POSITIVES. This gate's whole value is that it
STAYS QUIET on ordinary writes — a lint that fires on every knowledge edit gets
switched off within a day, and then the positives above are worth nothing. A
fix whose effect is that something stops appearing needs a positive control that
does NOT flip (guard-4166), so every silence below is asserted, not assumed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from ground_truth_citation import analyze, source_tokens  # noqa: E402

GATE = SCRIPTS / "ground-truth-citation-gate.py"
PROJECT_ROOT = SCRIPTS.parent.parent
IN_SCOPE = "world/knowledge/tree/system/pytest-throwaway-citation-fixture.md"

# A URL the session DID fetch, and one it did not. `retrieved` is the predicate
# the production entry builds from the  provenance manifest.
FETCHED = "https://example.invalid/report-2024"
UNFETCHED = "https://example.invalid/never-opened"


def _retrieved(kind, value):
    return FETCHED in str(value)


def _kinds(findings):
    return sorted(f.kind for f in findings)


# ---------------------------------------------------------------------------
# The three named verification outcomes
# ---------------------------------------------------------------------------

def test_OUTCOME_1_unmarked_entity_fact_diff_is_flagged():
    """"unmarked entity-fact diff flagged"."""
    text = ("## Findings\n\n"
            "Acme Corporation reported revenue of $4.2 billion in 2024.\n")
    findings = analyze(text, retrieved=_retrieved)
    assert _kinds(findings) == ["missing-citation"], findings
    assert "Acme Corporation" in findings[0].sample


def test_OUTCOME_2_unverified_tagged_passes():
    """"UNVERIFIED-tagged passes". The escape hatch is the whole reason the gate
    can be strict: a claim the author KNOWS is a prior stays writable, labelled."""
    text = ("## Findings\n\n"
            "Acme Corporation reported revenue of $4.2 billion in 2024. "
            "[UNVERIFIED -- model prior]\n")
    assert analyze(text, retrieved=_retrieved) == []


def test_OUTCOME_3_cited_but_not_fetched_url_is_decorative():
    """"cited-but-not-fetched URL flagged as decorative".

    The load-bearing case. A citation the session never opened is WORSE than no
    citation — it reads as verified to every downstream reader — so it is flagged
    at the same severity, not a lesser one."""
    text = (f"## Findings\n\nGlobex Industries employs 12,000 people as of 2025. "
            f"See {UNFETCHED} for detail.\n")
    findings = analyze(text, retrieved=_retrieved)
    assert _kinds(findings) == ["decorative-citation"], findings


# ---------------------------------------------------------------------------
# The coach  shape (second named outcome)
# ---------------------------------------------------------------------------

def test_coach_shape_publication_name_only_source_is_caught():
    """"coach  fixture (prior-substituted identities, publication-name-only
    sources) is caught".

    The coach incident's artifact named real-sounding entities and attributed them
    to a real-sounding publication, with nothing retrievable behind either. This is
    the case that decides the design: a bare publication name MUST NOT count as a
    source token. If it did, the gate would pass the exact write that motivated it."""
    text = ("## Catalogue\n\n"
            "The Quarterly Industrial Review found that Globex Industries "
            "acquired Initech Systems in 2023 for $800 million.\n")
    findings = analyze(text, retrieved=_retrieved)
    assert _kinds(findings) == ["missing-citation"], findings


def test_CONTROL_the_same_claim_with_a_fetched_url_is_clean():
    """Positive control for the case above: it is the missing PROVENANCE that
    flags, not the sentence shape. Without this, the coach assertion is equally
    satisfied by a gate that flags every line containing a capital letter."""
    text = ("## Catalogue\n\n"
            f"Globex Industries acquired Initech Systems in 2023 for $800 million "
            f"({FETCHED}).\n")
    assert analyze(text, retrieved=_retrieved) == []


# ---------------------------------------------------------------------------
# Cost control (third named outcome) and scope
# ---------------------------------------------------------------------------

def test_OUTCOME_tier0_and_nonfactual_writes_pass_untouched():
    """"tier-0/no-op writes (formatting, non-factual edits) pass untouched
    (cost control test)"."""
    for text in (
        "## Notes\n\n- reflowed the table\n- fixed a typo\n",
        "\n\n",
        "## Next steps\n\nWe should probably revisit this later.\n",
        "| col | col |\n| --- | --- |\n| a | b |\n",
    ):
        assert analyze(text, retrieved=_retrieved) == [], repr(text)


def test_CONTROL_code_fences_and_front_matter_are_skipped():
    """A fenced sample or a front-matter date is not a factual assertion about
    the world. Scanning them is how a lint earns its reputation for noise."""
    text = ("---\nlast_updated: 2026-09-03\nauthor: Alpha Agent\n---\n\n"
            "```\nAcme Corporation reported revenue of $4.2 billion in 2024.\n```\n")
    assert analyze(text, retrieved=_retrieved) == []


def test_CONTROL_each_of_the_four_source_token_kinds_satisfies_the_gate():
    """The directive names four in-session source tokens. Pinning all four keeps a
    future narrowing of the token set from silently flagging cited writes."""
    for token in (FETCHED, "tree-node system/daemon-only-architecture",
                  "msg-20260903-095816-alpha-5483", "g-357-45"):
        text = f"## Findings\n\nGlobex Industries employs 12,000 people as of 2025. [{token}]\n"
        assert analyze(text, retrieved=lambda k, v: True) == [], token


def test_CONTROL_unreadable_provenance_skips_the_decorative_check(  ):
    """guard-1760: a checker must not report what it DECLINED to look at as a pass.

    With `retrieved=None` the manifest was unreadable, so whether a citation was
    fetched is UNKNOWN. The gate must skip the decorative check — not silently
    treat every citation as verified, which is the failure that would make an
    unreadable manifest look like a clean bill of health."""
    text = f"## Findings\n\nGlobex Industries employs 12,000 people as of 2025. See {UNFETCHED}.\n"
    assert analyze(text, retrieved=None) == []
    # ...and the MISSING-citation half must still fire, or "unknown provenance"
    # would disable the whole gate rather than one check.
    bare = "## Findings\n\nGlobex Industries employs 12,000 people as of 2025.\n"
    assert _kinds(analyze(bare, retrieved=None)) == ["missing-citation"]


# ---------------------------------------------------------------------------
# The hook entry, through its real stdin contract
# ---------------------------------------------------------------------------

def _run_hook(payload, env_extra=None):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=120,
                          cwd=str(PROJECT_ROOT))


FACT = "Acme Corporation reported revenue of $4.2 billion in 2024.\n"


def test_hook_flags_a_write_to_the_knowledge_tree():
    r = _run_hook({"tool_name": "Write", "session_id": "pytest",
                   "tool_input": {"file_path": IN_SCOPE, "content": FACT}})
    assert r.returncode == 0                                  # advisory: never blocks
    payload = json.loads(r.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "missing-citation" in payload["hookSpecificOutput"]["additionalContext"]
    # BOTH channels, deliberately: stdout reaches the model, stderr the human
    # terminal, and neither reaches the other's reader (guard-1680).
    assert "ground-truth-citation-gate" in r.stderr


def test_hook_extracts_added_text_from_all_three_tool_shapes():
    """Write/Edit/MultiEdit carry the added content under three different keys.
    A gate wired to one of them is silent on the other two."""
    for payload in (
        {"tool_name": "Write", "tool_input": {"file_path": IN_SCOPE, "content": FACT}},
        {"tool_name": "Edit", "tool_input": {"file_path": IN_SCOPE, "new_string": FACT}},
        {"tool_name": "MultiEdit", "tool_input": {"file_path": IN_SCOPE,
         "edits": [{"new_string": "- typo\n"}, {"new_string": FACT}]}},
    ):
        r = _run_hook({**payload, "session_id": "pytest"})
        assert r.stdout.strip(), f"silent on {payload['tool_name']}"


def test_hook_is_silent_outside_scope():
    """Same text, a path the gate does not govern. This is the control that keeps
    the gate from becoming a global prose lint."""
    r = _run_hook({"tool_name": "Write", "session_id": "pytest",
                   "tool_input": {"file_path": "core/scripts/notes.md", "content": FACT}})
    assert r.stdout.strip() == "" and r.returncode == 0


def test_hook_honours_the_ground_truth_front_matter_optin():
    r = _run_hook({"tool_name": "Write", "session_id": "pytest",
                   "tool_input": {"file_path": "agents/alpha/temp/pytest-fixture.md",
                                  "content": "---\nground_truth: true\n---\n\n" + FACT}})
    assert "missing-citation" in r.stdout


def test_hook_escalates_to_deny_only_under_the_env_flag():
    """The goal's wording: "advisory escalatable to refuse". The escalation is an
    env flag, not a code edit, so a box can turn it on and off without a commit."""
    p = {"tool_name": "Write", "session_id": "pytest",
         "tool_input": {"file_path": IN_SCOPE, "content": FACT}}
    assert json.loads(_run_hook(p).stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    r = _run_hook(p, env_extra={"GROUND_TRUTH_CITATION_GATE": "refuse"})
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_fails_open_on_garbage_stdin():
    """Fail-open by contract: a malformed payload must never wedge a write."""
    for junk in ("", "not json", "[]", '{"tool_name": "Write"}'):
        r = subprocess.run([sys.executable, str(GATE)], input=junk, capture_output=True,
                           text=True, timeout=60, cwd=str(PROJECT_ROOT))
        assert r.returncode == 0 and r.stdout.strip() == "", junk


def test_the_gate_is_REGISTERED_in_settings_json():
    """rb-9476: a scoped fix can be correct-looking and INERT. Every assertion
    above passes against a gate that no hook ever invokes; only this one fails."""
    cfg = json.loads((PROJECT_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    for matcher in ("Write", "Edit", "MultiEdit"):
        blocks = [b for b in cfg["hooks"]["PreToolUse"] if b.get("matcher") == matcher]
        assert blocks, f"no PreToolUse block for {matcher}"
        cmds = " ".join(h["command"] for b in blocks for h in b["hooks"])
        assert "ground-truth-citation-gate.sh" in cmds, f"unregistered for {matcher}"


# ---------------------------------------------------------------------------
# Controls added after mutation testing showed the set above did not pin them.
# Both mutants SURVIVED the first pass: the assertions existed, but nothing
# failed when the behaviour was removed. Recording why they are here, because a
# control whose motivation is lost is the first thing a future reader deletes.
# ---------------------------------------------------------------------------

def test_CONTROL_entities_without_an_assertion_are_not_flagged():
    """A candidate line needs BOTH an entity signal and an assertion signal.

    "Better to under-flag than to spam" is the design decision this pins. Cross-
    reference lists, headings and see-alsos are dense with proper nouns and years
    and assert nothing about the world; flagging them would put a warning on most
    knowledge-tree edits, and a gate that cries wolf gets switched off — at which
    point every genuine finding above is worth nothing.

    Mutant that survived without this: `is_assertion` hard-coded to True."""
    for line in ("Related entities: Acme Corporation, Globex Industries, Initech Systems (2024).",
                 "## Acme Corporation Overview 2024",
                 "See also: Globex Industries, 2025 filings."):
        assert analyze(line, retrieved=_retrieved) == [], line


def _load_entry_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gtc_entry_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_CONTROL_entry_returns_None_when_the_provenance_manifest_is_unreadable(
        tmp_path):
    """guard-1760 at the ENTRY, not just in analyze().

    The unit control above proves `analyze` skips the decorative check when handed
    `retrieved=None`. It says nothing about whether the entry ever PRODUCES None —
    and a predicate that defaults to permissive would report every citation as
    fetched, turning an unreadable manifest into a clean bill of health.

    Both no-manifest branches are pinned, because they are reached differently:
    an absent/empty manifest, and a manifest whose read RAISES.

    Mutant that survived without this: `except Exception: return lambda: True`."""
    mod = _load_entry_module()

    # STUB the manifest empty rather than trusting the live filesystem to be
    # empty (). Passing a never-existed sid does NOT isolate this:
    # tracker_path falls back to the agent-wide session/context-reads.txt,
    # whose existence varies by box and by compaction cycle -- so the live-fs
    # form passed on one Body and failed on another, neither of them wrong.
    stub = tmp_path / "context-reads.py"
    stub.write_text(
        "def read_provenance(session_id=None):\n    return []\n"
        "def read_tracker(session_id=None):\n    return set()\n",
        encoding="utf-8")
    saved_scripts = mod.SCRIPTS
    try:
        mod.SCRIPTS = tmp_path
        assert mod._retrieved_predicate("pytest-no-such-session") is None
    finally:
        mod.SCRIPTS = saved_scripts

    class _Boom:
        class util:
            @staticmethod
            def spec_from_file_location(*a, **k):
                raise RuntimeError("manifest unreadable")

    saved = mod.importlib
    try:
        mod.importlib = _Boom
        assert mod._retrieved_predicate("pytest-any-session") is None
    finally:
        mod.importlib = saved


def test_CONTROL_the_world_tree_root_helper_actually_resolves():
    """The resolved-absolute-path arm of the scope check must not be INERT.

    `_world_tree_root` imports a name from `_paths` inside a bare `except
    Exception: return None`, so a WRONG NAME degrades to "no tree root" in
    total silence — the branch reads as live and does nothing. That is exactly
    what shipped in the first draft (`world_dir` instead of `WORLD_DIR`), and
    every other test in this file stayed green through it, because the
    substring arm of the scope check masked the loss (rb-9476).

    Asserting `is not None` is the assertion that catches it: a swallowed
    ImportError is the only way this returns None on a configured box."""
    mod = _load_entry_module()
    root = mod._world_tree_root()
    assert root is not None, "import name drifted; the branch is silently inert"
    assert root.name == "tree" and root.parent.name == "knowledge"


def test_scope_accepts_a_resolved_absolute_tree_path():
    """Hooks receive resolved absolute paths, not the `world/` virtual prefix."""
    mod = _load_entry_module()
    root = mod._world_tree_root()
    assert mod._in_scope(str(root / "system" / "pytest-fixture.md"), "")


# ── PARTIAL: "never opened" vs "opened, in part" ( class 1) ────────
#
# The predicate was BOOLEAN, so one message served two different situations and
# asserted the wrong one for the second: a file read with an offset/limit is
# recorded behind context-reads.PARTIAL_PREFIX and excluded from read_tracker()'s
# full set BY DESIGN, and the finding then told the reader the file had "NOT
# [been] retrieved this session" -- sending them to look for a read that had
# already happened. The VERDICT is deliberately unchanged (a ranged peek is
# still not evidence for the claim); only what the message says it is changes.
#
# Three tests, because the interesting property is that three inputs produce
# three DIFFERENT outputs. Two of them are controls: without the True case this
# says nothing about whether anything still passes, and without the False case
# nothing pins that the ORIGINAL wording survives for the case it was right
# about (guard-4166 -- a fix whose effect is that something stops appearing
# needs a control that does not flip).

_PARTIAL_TEXT = (
    "The sampler reads the manifest written by the PostToolUse hook.\n"
    "Measured 2026-09-05 on cc-08: core/scripts/context-reads.py line 101 "
    "defines PARTIAL_PREFIX and 42 entries were recorded.\n"
)


def test_a_partial_read_is_not_reported_as_never_retrieved():
    """The fix: the message must stop asserting something FALSE."""
    from ground_truth_citation import PARTIAL
    findings = analyze(_PARTIAL_TEXT, retrieved=lambda k, v: PARTIAL)
    assert len(findings) == 1, findings
    detail = findings[0].detail
    assert "ONLY IN PART" in detail, detail
    assert "NOT retrieved this session" not in detail, detail


def test_CONTROL_a_partial_read_still_FAILS():
    """The half that must NOT change, and the alarm-suppressing direction.

    PARTIAL is a truthy STRING, so a `any(verdicts)` truthiness test anywhere on
    this path would read it as a full retrieval and silently pass the cluster.
    That is the failure this gate exists to prevent, so it is asserted directly
    rather than inferred from the message text above."""
    from ground_truth_citation import PARTIAL
    findings = analyze(_PARTIAL_TEXT, retrieved=lambda k, v: PARTIAL)
    assert findings, "PARTIAL silently PASSED -- the alarm-suppressing direction"
    assert findings[0].kind == "decorative-citation", findings[0].kind


def test_CONTROL_never_retrieved_keeps_the_original_wording():
    """The case the original message was RIGHT about must be untouched."""
    findings = analyze(_PARTIAL_TEXT, retrieved=lambda k, v: False)
    assert len(findings) == 1, findings
    assert "NOT retrieved this session" in findings[0].detail, findings[0].detail
    assert "ONLY IN PART" not in findings[0].detail, findings[0].detail


def test_CONTROL_a_fully_retrieved_citation_still_passes():
    """Without this, the three tests above are consistent with a gate that
    flags everything."""
    assert analyze(_PARTIAL_TEXT, retrieved=lambda k, v: True) == []


def test_the_partial_remedy_asks_for_the_read_the_predicate_credits():
    """. The ranged-read message said "re-read the region that supports
    it", but read_tracker() credits FULL reads only, so that re-read (another
    ranged Read) came back with this same finding: on the worker fleet one close
    went unread, partial, then PASS. The text must ask for the read that clears
    it and must not invite one that cannot. The predicate half of that claim is
    pinned end to end in test_context_reads_partial.py."""
    from ground_truth_citation import PARTIAL
    detail = analyze(_PARTIAL_TEXT, retrieved=lambda k, v: PARTIAL)[0].detail
    assert "with no offset or limit" in detail, detail
    assert "re-read the region" not in detail, detail


def test_a_pass_count_ratio_is_not_a_source_token():
    """A slash-joined run of bare numbers is a ratio, not a citation.

    _NODE_KEY's segment class is ``[a-z0-9]+``, which admits all-digit segments,
    so "suites 6/6 green" was extracted as a node-key and then adjudicated. It
    names no file, no tree node and no URL, so it could only ever come back
    uncited. Measured over the live world store BEFORE the fix (g-115-9059,
    guard-3086): 3,033 goals / 1,584 outcome+progress notes yielded 11,507
    node-key tokens, of which 2,055 (17.86%, 939 distinct) were this shape.

    The exclusion must NOT rescue the claim. An entity-bearing fact line whose
    only slash token is a ratio is genuinely uncited, so it stays flagged -- as
    ``missing-citation``, the honest verdict, rather than as a decorative
    citation of "6/6". Before the fix this returned decorative-citation.
    """
    line = ("Acme Corporation reported revenue of $4.2 billion in 2024, "
            "suites 6/6 green.")
    # non-vacuity: the ratio is the ONLY slash token, and it is now dropped.
    assert source_tokens(line) == [], source_tokens(line)
    text = "## Findings\n\n" + line + "\n"
    # Same verdict either way -- nothing checkable is left to adjudicate.
    for r in (True, False):
        findings = analyze(text, retrieved=lambda k, v: r)
        assert _kinds(findings) == ["missing-citation"], (r, findings)


def test_CONTROL_a_ratio_does_not_suppress_a_real_node_key_beside_it():
    """The exclusion is per-TOKEN, not per-line.

    Tightening an extractor weakens a negative assertion (guard-1901): a dropped
    token can take a cluster from a blocking finding to "no checkable token, no
    finding", which is alarm suppression. This pins that a genuine node key
    sitting on the same line as a ratio still satisfies the gate, so the
    exclusion cannot widen into the neighbouring token.
    """
    line = ("Acme Corporation reported revenue of $4.2 billion in 2024 "
            "(6/6 green), per system/daemon-only-architecture.")
    # the neighbouring key survives extraction; the ratio does not.
    assert source_tokens(line) == [
        ("node-key", "system/daemon-only-architecture")], source_tokens(line)
    text = "## Findings\n\n" + line + "\n"
    assert analyze(text, retrieved=lambda k, v: True) == []
    # NON-VACUITY CONTROL: a bare "== []" also passes when NO cluster forms at
    # all -- the first draft of this test did exactly that and proved nothing.
    assert _kinds(analyze(text, retrieved=lambda k, v: False)) == [
        "decorative-citation"]


def test_CONTROL_digit_bearing_paths_are_not_mistaken_for_ratios():
    """Only an ENTIRELY numeric run is excluded.

    Real citations carry digits all the time -- an API path, a versioned key, a
    hyphenated slug with a number in it. If the exclusion keyed on "contains a
    digit" instead of "is all digits and slashes" it would delete genuine
    citations, which is the direction this gate must never fail in.

    UPDATED 2026-10-03 (g-115-8858): the token list used to include
    "runs/32023260302", an all-DIGIT run. That token now deliberately does NOT
    match: the new grammar requires a letter (and a kebab segment, for the
    slug form) because no file path, tree-node key or URL in this store
    consists only of digits and slashes -- the same proof the _RATIO_RUN note
    below uses for the ratio class, and the live-tree census measured zero
    all-digit multi-segment node keys. The pin was repointed at a
    letter-bearing token that still carries a full numeric segment
    ("runs/32023260302-a"), which exercises the same property (a numeric
    segment inside a genuine citation is not a ratio); the pure-digit form is
    asserted DROPPED below so a future widening cannot resurrect it.
    """
    for token in ("runs/32023260302-a", "core/scripts/q4-provenance-sample",
                  "system/asp-115-tail", "v1/watch-2026"):
        line = (f"Globex Industries reported revenue of $2.1 billion in "
                f"2025. [{token}]")
        assert source_tokens(line) == [("node-key", token)], (
            token, source_tokens(line))
        text = "## Findings\n\n" + line + "\n"
        assert analyze(text, retrieved=lambda k, v: True) == [], token
        # NON-VACUITY: flipping retrieved must CHANGE the verdict, proving a
        # real cluster was adjudicated rather than none forming at all.
        assert _kinds(analyze(text, retrieved=lambda k, v: False)) == [
            "decorative-citation"], token
    # The pure-digit form is a ratio-shaped non-citation, not a node key
    # ( synthetic fixture; the revenue line is a citation-free probe).
    line = ("Globex Industries reported revenue of $2.1 billion in 2025. "
            "[runs/32023260302]")
    assert source_tokens(line) == [], source_tokens(line)


def test_a_ratio_beside_a_goal_id_falls_back_to_the_corpus_wide_policy():
    """The measured consequence of the ratio exclusion, pinned deliberately.

    Diffing findings pre/post across all 1,587 live notes at retrieved=False
    (the worst case for suppression) found 18 notes that lose a blocking
    finding. Every one of the 32 affected clusters retained ONLY goal-id (x28)
    or board-msg+goal-id (x4) -- zero retained a url or node-key.

    ``checkable`` is url/node-key only, so ``if not checkable: continue``
    already declines to adjudicate goal-id-only clusters corpus-wide. The
    phantom ratio was pulling these clusters OUT of that pre-existing policy
    and into a verdict that could only ever fail. This pins the fall-back as
    intended behaviour so a future reader does not "fix" it back into a
    phantom -- and so that changing ``checkable`` breaks a test that explains
    why, rather than silently shifting 18 verdicts.
    """
    line = ("Acme Corporation reported revenue of $4.2 billion in 2024, "
            "suites 6/6 green, per g-115-9059.")
    # the goal-id survives; the ratio does not; nothing checkable remains.
    assert source_tokens(line) == [("goal-id", "g-115-9059")], source_tokens(line)
    text = "## Findings\n\n" + line + "\n"
    # NOT missing-citation: the cluster HAS a source token, so the
    # `not cl.source_tokens` branch does not fire either.
    for r in (True, False):
        assert analyze(text, retrieved=lambda k, v: r) == [], r


# ---------------------------------------------------------------------------
#  — the bare-fraction defect: the node-key grammar no longer parses
# prose slash-compounds, rate units and shell syntax as tree-node keys, and no
# longer truncates file-path citations at the first non-conforming segment.
# ---------------------------------------------------------------------------
#
# The goal's verification outcomes, verbatim:
#   1. _NODE_KEY no longer matches 48/57, 1155/1155, 389/391, pip/sol or
#      collided/perceived
#   2. a genuine tree node key still matches
#   3. a test carries both the positive and the negative cases
# Everything below is the pin for that. The grammar's reasoning (the rejected
# remedies, the census numbers, the one dropped pinned token) lives on _NODE_KEY
# in the module; the comments here point at the MEASUREMENTS, not the grammar.

# Every negative is a token MEASURED in the goal's recorded instance list
# (fractions, prose word-pairs, rate units, shell syntax, git refs) — not an
# invented one. If any of these starts matching again, an author's closure note
# containing it is one cluster away from a Q4 FAIL it could never remedy,
# because Q4 has no override by design.
_NEGATIVE_NODEKEY_TOKENS = (
    "48/57", "1155/1155", "389/391",            # fractions (outcome 1, verbatim)
    "pip/sol", "collided/perceived",            # prose pairs (outcome 1, verbatim)
    "124/125", "0/0/0", "20/40/80", "6/6",      # pass-counts (the _RATIO_RUN class)
    "191/h", "7/h",                             # rate units (measured 2026-09-25)
    "adopt/drop", "posts/h", "world/board",     # the 2026-09-06 counter-evidence
    "claim/complete", "ts/domain/listed",       #   (all four have a letter in
    "unmeasured/lanes", "true/true/true",       #   every segment, so a letter
    "owner/name", "membership/turnover",        #   rule cannot catch these)
    "records/files",                            # the sealed-JSON instance (echo)
    "dev/null",                                 # shell redirect (cc-09 instance)
    "origin/main", "refs/heads/main",           # git refs
    "stdout/stderr", "try/except", "and/or",    # prose compounds
    "48/57/2026",                               # a ratio wearing a date
)


def test_OUTCOME_1_g8858_bare_fractions_and_prose_pairs_are_not_node_keys():
    """Outcome 1, verbatim plus the full recorded instance list.

    Each token must be EXTRACTED by nothing: source_tokens returns no node-key
    for it (a url/goal-id alongside it is unaffected, but that is not the case
    here). The non-vacuity arm below proves the old grammar WOULD have matched
    these, so a future no-op "fix" (e.g. deleting the extractor entirely) fails
    the positive test instead of passing both.
    """
    from ground_truth_citation import _NODE_KEY
    for tok in _NEGATIVE_NODEKEY_TOKENS:
        toks = [v for k, v in source_tokens(tok) if k == "node-key"]
        assert toks == [], (tok, toks)
    # NON-VACUITY: the OLD grammar (pre-fix, pinned here as a reference pattern,
    # not imported from the module so the module cannot be edited to pass this)
    # matched every one of them.
    old = re.compile(
        r"\b(?:world/knowledge/tree/)?[a-z0-9]+(?:-[a-z0-9]+)*"
        r"(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+(?:\.md)?\b")
    for tok in _NEGATIVE_NODEKEY_TOKENS:
        assert old.search(tok), (tok, "old grammar must have matched; the "
                                    "negative pin would be vacuous")
        assert not _NODE_KEY.search(tok), (tok, "new grammar re-matches a "
                                               "recorded false positive")


def test_OUTCOME_2_g8858_genuine_tree_node_keys_still_match():
    """Outcome 2: a genuine tree node key still matches.

    The battery exercises the CITED SHAPES, not a live-key snapshot: the store-
    root form (guard-6054), the .md file form, the digit-bearing slug (the class
    the _RATIO_RUN control protects), and a bare live key
    (system/daemon-only-architecture -- verified in the live tree census
    2026-10-03, hostname zc-10). The store-root entries are SYNTHETIC shape
    fixtures (the pearl-bridge nodes were archived in the 2026-08-31 pearl
    split; ledger.jsonl is the external-world .jsonl class), so the assertion
    is about the grammar, not the tree's current contents. The file-path
    citations are the second half of the goal's scope (the truncation fix,
    appended 2026-09-29): the same run, terminated by a known extension, must
    arrive WHOLE, not truncated at the first non-conforming segment.
    [UNVERIFIED -- model prior: no in-session retrieval of the fixture tokens]
    """
    node_keys = (
        "system/daemon-only-architecture",
        "world/knowledge/tree/pearl-bridge",
        "world/knowledge/tree/perception/pearl-bridge-pearl",
        "system/asp-115-tail",                 # kebab + digit segment
        "world/knowledge/tree/system/pytest-throwaway-citation-fixture.md",
        "agents/alpha/self.md",                # file form, no kebab in last seg
        "claude/rules/read-before-edit",       # the largest demotable class
    )
    file_paths = (
        "core/scripts/wm-read.sh",             # the goal's own fixture (cc-05)
        "core/scripts/wm.py",                  # the relayed .py variant
        "ops/mind-sidecar/provision-env.sh",   # the  truncation
        "core/scripts/q4_provenance_sample.py",  # underscore segment, .py
        "core/scripts/ground_truth_citation.py",
        "src/main/java/AyoServer/BudgetMeterVerticle.java",  # uppercase segs
        "world/telemetry/ledger.jsonl",        # the external-world .jsonl class
        "core/githooks/commit-msg",            # no extension, kebab: node form
    )
    for tok in node_keys + file_paths:
        toks = [v for k, v in source_tokens(tok) if k == "node-key"]
        assert toks == [tok], (tok, toks)
    # The old grammar TRUNCATED the file forms: pin the regression direction so
    # a "simpler" grammar that reverts to the old segment class fails here.
    old = re.compile(
        r"\b(?:world/knowledge/tree/)?[a-z0-9]+(?:-[a-z0-9]+)*"
        r"(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+(?:\.md)?\b")
    for tok in ("core/scripts/wm-read.sh", "core/scripts/wm.py",
                "ops/mind-sidecar/provision-env.sh"):
        m = old.search(tok)
        assert m is None or m.group(0) != tok, (
            tok, "old grammar did not truncate this form; the positive pin "
                 "for the truncation fix would be vacuous")


def test_OUTCOME_3_g8858_a_cluster_with_only_a_prose_slash_pair_is_unadjudicated():
    """Outcome 3, the cluster-level consequence.

    The expensive shape the goal measured: a cluster whose ONLY node-key token
    was a prose slash-compound used to come back decorative-citation and could
    never be remedied (the manifest can never contain "pip/sol"). With the
    compound no longer extracted the phantom is gone, so the verdict can no
    LONGER be decorative: a fact line with no other source token at all gets
    the honest missing-citation (the same directional improvement the
    _RATIO_RUN note measured for pass-counts), and a line that DOES carry a
    non-checkable token (a goal-id) falls into the pre-existing corpus-wide
    `if not checkable: continue` policy -- no finding. The non-vacuity arm
    proves a GENUINE uncited node key on the same line shape still comes back
    decorative: the grammar narrowed the phantom class, it did not switch the
    check off.
    """
    text = ("## Findings\n\n"
            "Globex Industries employs 12,000 people as of 2025. The rollout "
            "split pip/sol across the two lanes.\n")
    # pre-fix this cluster was decorative-citation on the phantom "pip/sol";
    # now the honest verdict, in either retrieved state.
    for r in (True, False):
        assert _kinds(analyze(text, retrieved=lambda k, v: r)) == [
            "missing-citation"], r
    # with a non-checkable token present, the pre-existing policy applies:
    # the cluster has a source token but nothing checkable -> no finding.
    text_goalid = ("## Findings\n\n"
                   "Globex Industries employs 12,000 people as of 2025, per "
                   "g-115-9059. The rollout split pip/sol across the lanes.\n")
    assert analyze(text_goalid, retrieved=lambda k, v: False) == []
    # NON-VACUITY: the identical line with a genuine, never-fetched node key
    # still fails -- narrowing must not read as silencing.
    text2 = ("## Findings\n\n"
             "Globex Industries employs 12,000 people as of 2025. The rollout "
             "split per system/daemon-only-architecture.\n")
    assert _kinds(analyze(text2, retrieved=lambda k, v: False)) == [
        "decorative-citation"]


def test_g8858_a_rate_unit_in_a_sentence_is_not_a_citation():
    """The 2026-09-25 measured variant (): '$0.191/h' became the
    node-key '191/h' and came back decorative. The digits/letters unit is the shape a
    bare 'all-digit' exclusion cannot catch, so pin the prose sentence, not
    just the token. Pre-fix the cluster was decorative on '191/h'; now the
    honest missing-citation, in either retrieved state."""
    line = ("Acme reported the daemon drew $0.191/h over the quarter, "
            "$4.7/h at the measured peak.")
    assert source_tokens(line) == [], source_tokens(line)
    text = "## Findings\n\n" + line + "\n"
    for r in (True, False):
        assert _kinds(analyze(text, retrieved=lambda k, v: r)) == [
            "missing-citation"], r
