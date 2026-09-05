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
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from ground_truth_citation import analyze  # noqa: E402

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


def test_CONTROL_entry_returns_None_when_the_provenance_manifest_is_unreadable():
    """guard-1760 at the ENTRY, not just in analyze().

    The unit control above proves `analyze` skips the decorative check when handed
    `retrieved=None`. It says nothing about whether the entry ever PRODUCES None —
    and a predicate that defaults to permissive would report every citation as
    fetched, turning an unreadable manifest into a clean bill of health.

    Both no-manifest branches are pinned, because they are reached differently:
    an absent/empty manifest, and a manifest whose read RAISES.

    Mutant that survived without this: `except Exception: return lambda: True`."""
    mod = _load_entry_module()
    assert mod._retrieved_predicate("pytest-no-such-session") is None

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
