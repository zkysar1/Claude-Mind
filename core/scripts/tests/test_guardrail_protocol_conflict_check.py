#!/usr/bin/env python3
"""Behavioral tests for guardrail-protocol-conflict-check ().

Per guard-1451 these are BEHAVIORAL, not structural: each test CALLS the
predicate or the scanner with a refuse-case and an allow-case rather than
grepping the source for wiring. Every assertion below was mutation-proved
during authoring -- see test docstrings naming the mutation that reddens it.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "guardrail-protocol-conflict-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("gpcc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gpcc = _load()


# --------------------------------------------------------------------------
# constrained_signatures: the core predicate
# --------------------------------------------------------------------------

def test_refuse_case_prohibition_governs_command():
    """The motivating case (guard-272 shape) MUST be extracted.

    This is the positive control. It caught a real bug during authoring:
    PROHIBITION_RE lacked re.I, so sentence-initial "Never" never matched
    and the predicate silently extracted nothing.
    Mutation: drop re.I from PROHIBITION_RE -> this goes RED.
    """
    rule = ("Never run 'stranded-claim-sweep.py --apply' before confirming "
            "no second live instance is running.")
    assert ("stranded-claim-sweep.py", ("--apply",)) in gpcc.constrained_signatures(rule)


def test_allow_case_prescriptive_guardrail_is_not_a_constraint():
    """A guardrail TEACHING correct usage must NOT register as prohibiting it.

    This is the inversion that made the naive design unusable: guardrails
    overwhelmingly prescribe the right invocation (rb-6305).
    Mutation: drop the marker.start() ordering check -> RED.
    """
    rule = ("Pass board-post.sh's message via STDIN "
            "(echo \"msg\" | board-post.sh --channel <ch> --type <kind>).")
    assert gpcc.constrained_signatures(rule) == set()


def test_allow_case_command_before_prohibition_is_not_governed():
    """The command must come AFTER the prohibition marker to be governed.

    The rule must keep both clauses in ONE sentence. An earlier version used
    "Use retrieve.sh --category for lookups; never guess a category." -- the
    semicolon splits it into two sentences, so the ordering guard was never
    reached and the test passed for the wrong reason. Mutation-proving caught
    that: neutering the guard left the test GREEN. Verified: with this rule,
    replacing the guard with `if False` turns this RED.
    """
    rule = "Always call retrieve.sh --category first and never skip the consult."
    assert gpcc.constrained_signatures(rule) == set()


def test_allow_case_no_flag_is_prose_not_a_signature():
    """A script name followed by prose must not become a command signature.

    Without this, '_env.sh must add the ...' registers as a constrained
    invocation with args ('must','add','the').
    Mutation: remove the any(a.startswith('--')) check -> RED.
    """
    rule = "Do not assume _env.sh must add the credential automatically."
    assert gpcc.constrained_signatures(rule) == set()


def test_prohibition_is_sentence_scoped():
    """A prohibition in a DIFFERENT sentence must not govern the command.

    Mutation: replace SENTENCE_SPLIT_RE splitting with a whole-rule scan -> RED.
    """
    rule = ("Never delete a pipeline record. "
            "Use pipeline-move.sh --to archived for retirement.")
    assert gpcc.constrained_signatures(rule) == set()


def test_placeholder_tokens_are_normalised_away():
    """<key> carries no matching power and must not enter the signature."""
    rule = "Never use tree-update.sh --set <key> last_update_trigger for that."
    sigs = gpcc.constrained_signatures(rule)
    assert ("tree-update.sh", ("--set", "last_update_trigger")) in sigs


# --------------------------------------------------------------------------
# scan: end-to-end behavior against a synthetic corpus
# --------------------------------------------------------------------------

def _corpus(tmp_path, guardrails, skill_lines, skill_name="demo"):
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    with open(world / "guardrails.jsonl", "w", encoding="utf-8") as fh:
        for g in guardrails:
            fh.write(json.dumps(g) + "\n")
    sk = tmp_path / ".claude" / "skills" / skill_name
    sk.mkdir(parents=True, exist_ok=True)
    (sk / "SKILL.md").write_text("\n".join(skill_lines), encoding="utf-8")
    return world, tmp_path


def test_scan_reports_a_real_conflict(tmp_path):
    """Refuse-case end-to-end: prohibited invocation present in SKILL.md.

    Mutation: make line_matches return False unconditionally -> RED.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-x", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking for a second instance."}],
        ["Phase 1", "Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert len(hits) == 1
    assert hits[0]["line"] == 2
    assert hits[0]["guardrails"] == ["guard-x"]
    assert hits[0]["likely_compliance"] is False


def test_scan_allow_case_different_flag_is_not_a_conflict(tmp_path):
    """guard-531 shape: the FIELD argument discriminates, not the flag.

    Prohibiting `--set <key> last_updated` must not flag `--set <key>
    growth_state`. Mutation: drop the trailing-argument tokens from the
    signature (match on flag alone) -> RED.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-y", "status": "active",
          "rule": "Never use tree-update.sh --set <key> last_updated after an Edit."}],
        ["bash core/scripts/tree-update.sh --set <node> growth_state ready_to_split"],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert hits == []


def test_compliance_is_ranked_not_dropped(tmp_path):
    """Compliance narration must still be REPORTED, just ranked last.

    A filter that silently drops rows can hide a genuine violation that
    happens to cite its own guardrail. Mutation: change scan() to skip
    compliance rows instead of sorting them last -> RED.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-z", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking."}],
        [
            "Bash: `py -3 core/scripts/sweep.py --apply`",          # real
            "# no explicit `sweep.py --apply` call needed here.",   # compliance
        ],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert len(hits) == 2, "compliance row must be reported, not dropped"
    assert hits[0]["likely_compliance"] is False, "real conflict must sort first"
    assert hits[1]["likely_compliance"] is True


def test_compliance_window_spans_wrapped_comment_lines(tmp_path):
    """The negation routinely wraps onto the previous comment line.

    Measured on the live corpus: a line-scoped window mislabelled 2 of 6
    actionable rows. Mutation: shrink the window back to the matched line
    only, or drop the comment-leader strip -> RED.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-w", "status": "active",
          "rule": "Never use tree-update.sh --set <key> last_updated after an Edit."}],
        [
            "   # The hook bumps it on every Edit of a node .md file — no",
            "   # explicit `tree-update.sh --set <k> last_updated` call needed.",
        ],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert len(hits) == 1
    assert hits[0]["likely_compliance"] is True


def test_retired_guardrail_is_ignored(tmp_path):
    """Only ACTIVE guardrails constrain. Mutation: drop the status check -> RED."""
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-r", "status": "retired",
          "rule": "Never run 'sweep.py --apply' before checking."}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert hits == []


def test_reconciled_annotation_suppresses_actionable(tmp_path):
    """An explicit `reconciled:` annotation marks a known-accepted pairing."""
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-q", "status": "active",
          "rule": ("Never run 'sweep.py --apply' before checking. "
                   "reconciled: the entry battery confirms single-instance first.")}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    hits = gpcc.scan(root, gpcc.build_index(gpcc.load_guardrails(world)))
    assert len(hits) == 1
    assert hits[0]["reconciled"], "reconciled reason must be carried on the row"


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------

def test_cli_exit_on_hits_is_opt_in(tmp_path):
    """Default exit is 0 (advisory); --exit-on-hits flips it to 1 on actionable."""
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-c", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking."}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    base = [sys.executable, str(SCRIPT), "--world", str(world), "--root", str(root)]
    assert subprocess.run(base + ["--output", "json"],
                          capture_output=True).returncode == 0
    r = subprocess.run(base + ["--output", "json", "--exit-on-hits"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert json.loads(r.stdout)["actionable"] == 1


def test_known_guardrails_are_reported_but_not_novel(tmp_path):
    """--known baselines a triaged conflict WITHOUT hiding it.

    This is what makes --exit-on-hits mean "a NEW guardrail contradicts a
    protocol line" rather than "the backlog is nonzero" (red forever, which
    trains readers to ignore it). The row must still appear in `rows` --
    baselining that DELETED the row would reproduce the silent-suppression
    failure this whole check exists to detect.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-k", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking."}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    base = [sys.executable, str(SCRIPT), "--world", str(world),
            "--root", str(root), "--output", "json", "--exit-on-hits"]

    r = subprocess.run(base, capture_output=True, text=True)
    assert r.returncode == 1, "un-baselined conflict must be novel -> exit 1"
    assert json.loads(r.stdout)["novel"] == 1

    r = subprocess.run(base + ["--known", "guard-k"], capture_output=True, text=True)
    assert r.returncode == 0, "baselined conflict must not be novel"
    d = json.loads(r.stdout)
    assert d["novel"] == 0
    assert d["actionable"] == 1, "still actionable -- baselining is not resolution"
    assert len(d["rows"]) == 1, "baselined row must STILL be reported, not dropped"


def test_partially_known_conflict_is_still_novel(tmp_path):
    """A row owned by a known AND an unknown guardrail is still novel.

    Subset semantics, not intersection: baselining guard-a must not silently
    absorb a new guard-b that landed on the same line.
    """
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-a", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking."},
         {"id": "guard-b", "status": "active",
          "rule": "Do not run 'sweep.py --apply' during a live fleet window."}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--world", str(world), "--root", str(root),
         "--output", "json", "--exit-on-hits", "--known", "guard-a"],
        capture_output=True, text=True)
    assert r.returncode == 1, "guard-b is not baselined -> row is still novel"
    assert json.loads(r.stdout)["novel"] == 1


def test_cli_json_shape(tmp_path):
    world, root = _corpus(
        tmp_path,
        [{"id": "guard-j", "status": "active",
          "rule": "Never run 'sweep.py --apply' before checking."}],
        ["Bash: `py -3 core/scripts/sweep.py --apply`"],
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--world", str(world),
         "--root", str(root), "--output", "json"],
        capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    for k in ("active_guardrails", "constrained_signatures", "hits", "actionable", "rows"):
        assert k in d, f"missing key {k}"
    assert d["rows"][0]["script"] == "sweep.py"


def test_missing_guardrails_file_is_not_a_crash(tmp_path):
    """Fail-open: an absent store yields an empty index, not a traceback."""
    assert gpcc.load_guardrails(tmp_path / "nope") == []
