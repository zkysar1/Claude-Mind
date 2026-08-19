"""Pins for core/scripts/doc-pin-map.py ().

The script tells the context-window-diet family which documentation lines a
test suite pins. Its whole value is that a reader BELIEVES its verdict before
deleting prose, so the two ways it can lie are what these tests target:

  1. A collapsed scan printing zeros that read as "no pins" (guard-2421).
  2. A host-line MISCLASSIFICATION — in particular the table-row case, where
     saying STATEMENT ("a diet preserves this") about a row the diet
     relocates is the single most expensive wrong answer it can give.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "core" / "scripts" / "doc-pin-map.py"


def _load():
    spec = importlib.util.spec_from_file_location("doc_pin_map", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def m():
    return _load()


# ── host classification ─────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expect", [
    # STATEMENT — survives a prose diet
    ("Bash: bash core/scripts/foo.sh --flag", "STATEMENT"),
    ("   8. Bash: board-read.sh --channel reasoning", "STATEMENT"),
    ("ELIF encoding_deferred_by_coordination:", "STATEMENT"),
    ("user-invocable: false", "STATEMENT"),          # hyphenated yaml key
    ("0.5. Unreflected Hypothesis Sweep:", "STATEMENT"),
    ("c.5. CURATOR QUALITY GATE (AutoContext-inspired):", "STATEMENT"),
    ("## Phase 0.5g.7", "STATEMENT"),
    ("**Do not move this below the idempotent guard**", "STATEMENT"),
    ("- a bullet point item", "STATEMENT"),
    ("echo 'x' | wm-set.sh slot", "STATEMENT"),
    # TABLE — survives a PROSE diet, dies when the data moves to config
    ("| `goal-selection.md` | Mandatory goal-selector.sh |", "TABLE"),
    ("|  Team state | world/team-state.yaml |", "TABLE"),
    # NARRATIVE — a prose diet deletes it
    ("retries after 3 consecutive `_perform_recovery` failures, and then", "NARRATIVE"),
    ("(FIRST heartbeat — seeds `runner-heartbeat` mtime AND stamps team-state", "NARRATIVE"),
    ("", "NARRATIVE"),
])
def test_host_class(m, line, expect):
    assert m._host_class(line) == expect


def test_table_is_not_folded_into_statement(m):
    """The regression that motivated the TABLE verdict.

    A markdown table row began life classified STATEMENT, which told a reader
    "a prose diet preserves this" about CLAUDE.md's Convention-Index and
    Core-Systems rows — 13 of that file's 26 literal pins, and the rows the
    diet most wants to relocate to config. Collapsing the two verdicts again
    restores a clean bill of health on the highest-risk surface.
    """
    assert m._host_class("| `coordination.md` | Multi-agent coordination |") != "STATEMENT"


def test_best_host_wins_when_a_literal_appears_in_both(m):
    """A literal in BOTH a table row and a Bash line is reachable after the
    table moves, so it must report STATEMENT — the diet cannot orphan it."""
    assert m._host_class("| x | foo.sh | y |") == "TABLE"
    assert m._host_class("Bash: foo.sh") == "STATEMENT"


# ── polarity ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("check,negative", [
    ('Check: `x/SKILL.md` does NOT contain "sensory_buffer"', True),
    ("Check: `x/SKILL.md` MUST NOT contain `tree_maturity = mean`", True),
    ("Check: `x/SKILL.md` has ZERO `wm-set` lines", True),
    ('Check: `x/SKILL.md` table says "5-phase" (not "4-phase")', True),
    # WITHOUT: the miss that manufactured a phantom NARRATIVE on the one file
    # this goal's outcome list still showed as coupled.
    ("Check: `x/SKILL.md` Phase 0.5.1 invokes `from-self` WITHOUT `--plan`", True),
    ("Check: `x/SKILL.md` Step 3 calls `foo.sh`", False),
])
def test_negative_polarity_detection(m, check, negative):
    """An absent literal is a PASS for a negative check and a FAILURE for a
    positive one. Reporting both as ABSENT makes the count meaningless — 43
    of 199 absences on the live corpus are passes."""
    assert bool(m._NEG_RE.search(check)) is negative


# ── needle narrowing: the prose half PARAPHRASES, the Bash half GREPS ───────

def test_echo_payload_regex_strips_messages_not_commands(m):
    """`echo "PASS: ..."` payloads restate the prose claim, so leaving them in
    the Bash half re-admits the very paraphrase the narrowing drops."""
    s = 'grep -q "meter.sh start" f && echo "PASS: meter start+end wired"'
    stripped = m._ECHO_PAYLOAD_RE.sub(" ", s)
    assert "meter start+end" not in stripped, "echo payload survived"
    assert "meter.sh start" in stripped, "the real needle was destroyed"


def test_paraphrase_in_prose_half_is_not_reported_as_a_pin(m):
    """The regression, end to end.

    A Check line writes `meter start` in prose and greps
    `aspirations-precheck-budget-meter.sh start`. Only the second is a needle.
    Reporting the first searches the target for a string no check depends on;
    being short and generic it lands in a prose sentence and is reported
    NARRATIVE — a phantom "a diet would break this" whose real host is a
    `Bash:` line. All three of the last NARRATIVE pins in this goal's outcome
    list were this shape.
    """
    rows = m.scan_verify_learning()["rows"]
    phantom = [r for r in rows
               if r["target"].endswith("aspirations-precheck/SKILL.md")
               and r["literal"] in ("meter start", "meter end")]
    assert not phantom, (
        f"prose paraphrase reported as a pin: {[r['literal'] for r in phantom]}")


def test_narrowing_fires_without_emptying_the_enumeration(m):
    """guard-3970: a filter that CAN empty an enumeration must be shown both
    to fire and to leave the fallback population intact. A narrowing that
    silently dropped every pin would read as a clean corpus."""
    rows = m.scan_verify_learning()["rows"]
    sources = {r.get("needle_source") for r in rows}
    assert "bash-half" in sources, "narrowing never fired — filter is dead"
    assert "whole-line" in sources, "fallback never used — filter is too greedy"
    assert len(rows) >= m.MIN_CHECKS


# ── polarity outranks hosting ───────────────────────────────────────────────

def test_negative_check_with_a_host_is_never_diet_risk(m):
    """Deleting text can only make "X is absent" MORE true, so no diet breaks
    a negative check whatever line currently hosts the string. Calling it
    NARRATIVE tells a shrinker to preserve prose in order to protect a check
    that wants the prose gone."""
    rows = m.scan_verify_learning()["rows"]
    wrong = [r for r in rows
             if r["negative"] and r["verdict"] in ("NARRATIVE", "TABLE")]
    assert not wrong, (
        f"{len(wrong)} negative check(s) reported as diet-risk, e.g. "
        f"{wrong[0]['target']}:{wrong[0]['literal']}")
    assert any(r["verdict"] == "NEGATIVE" for r in rows), \
        "NEGATIVE bucket never produced — the branch is dead"


# ── trailing-annotation stripping ───────────────────────────────────────────

@pytest.mark.parametrize("line,expect", [
    # the  case: documenting a re-anchor must not create a phantom pin
    ("Check: `core/config/conventions/x.md` exists   # re-anchored off the CLAUDE.md index row",
     "Check: `core/config/conventions/x.md` exists"),
    # a `#` INSIDE backticks is a shell literal the needle depends on
    ("Check: `aspirations/SKILL.md` uses `grep -c '#' foo` somewhere",
     "Check: `aspirations/SKILL.md` uses `grep -c '#' foo` somewhere"),
    # no annotation -> unchanged
    ("Check: `x/SKILL.md` Step 3 calls `foo.sh`",
     "Check: `x/SKILL.md` Step 3 calls `foo.sh`"),
    # a '#' not preceded by whitespace is not an annotation marker
    ("Check: `x/SKILL.md` names issue#42", "Check: `x/SKILL.md` names issue#42"),
])
def test_strip_trailing_comment(m, line, expect):
    assert m._strip_trailing_comment(line) == expect


def test_annotation_does_not_create_a_phantom_target(m):
    """The regression: a check re-anchored AWAY from CLAUDE.md, whose comment
    still says 'CLAUDE.md', must not be reported as a CLAUDE.md pin."""
    line = "Check: `core/config/conventions/x.md` exists   # re-anchored off the CLAUDE.md index row"
    stripped = m._strip_trailing_comment(line)
    assert m._TARGET_RE.search(stripped) is None


# ── resolver: name what it cannot resolve, never drop it ────────────────────

def test_resolver_maps_all_three_target_shapes(m):
    assert m._resolve_target("CLAUDE.md") == REPO / "CLAUDE.md"
    assert m._resolve_target("`.claude/rules/self.md`") == \
        REPO / ".claude" / "rules" / "self.md"
    assert m._resolve_target("`aspirations-precheck/SKILL.md`") == \
        REPO / ".claude" / "skills" / "aspirations-precheck" / "SKILL.md"
    assert m._resolve_target("`.claude/skills/tree/SKILL.md`") == \
        REPO / ".claude" / "skills" / "tree" / "SKILL.md"


def test_resolver_returns_none_rather_than_guessing(m):
    """guard-3970: a fallback chain is an enumeration claim. An unrecognised
    shape must surface as unresolved-and-named, not be coerced to some path
    that happens to exist."""
    assert m._resolve_target("core/config/aspirations.yaml") is None
    assert m._resolve_target("world/conventions/board.md") is None


def test_unresolved_targets_are_reported_not_silently_skipped(m):
    out = m.scan_verify_learning()
    assert out["error"] is None
    for u in out["unresolved"]:
        assert "token" in u and "source_line" in u


# ── positive controls ───────────────────────────────────────────────────────

def test_live_scan_clears_its_own_floors(m):
    """The scan must find a non-trivial corpus. A zero here is the
    silent-failure signature, not a clean repo (guard-2421/guard-1641)."""
    out = m.scan_verify_learning()
    assert len(out["rows"]) >= m.MIN_CHECKS, (
        f"only {len(out['rows'])} pins parsed — below the {m.MIN_CHECKS} floor; "
        "the Check:/target parser has broken and every verdict below it is vacuous")
    targets = {r["target"] for r in out["rows"]}
    assert len(targets) >= m.MIN_TARGETS


def test_every_verdict_bucket_is_populated_on_the_live_corpus(m):
    """Each bucket exercised by real data — an always-empty bucket is a dead
    branch that would never be noticed."""
    rows = m.scan_verify_learning()["rows"]
    seen = {r["verdict"] for r in rows}
    for want in ("STATEMENT", "TABLE", "NARRATIVE", "JUDGEMENT"):
        assert want in seen, f"verdict {want} never produced by the live corpus"


def test_test_scan_finds_content_pins_and_separates_path_fixtures(m):
    rows = m.scan_tests()
    assert rows, "no test file found reading a live repo doc — scan collapsed"
    kinds = {r["verdict"] for r in rows}
    assert "CONTENT-PIN" in kinds


# ── CLI ─────────────────────────────────────────────────────────────────────

def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, cwd=str(REPO),
                          timeout=180)


def test_cli_summary_prints_its_positive_control():
    r = _run()
    assert r.returncode == 0, r.stderr
    assert "POSITIVE CONTROL" in r.stdout
    assert "pins parsed (floor" in r.stdout


def test_cli_json_is_parseable_and_carries_controls():
    r = _run("--json")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["controls"]["collapsed"] is False
    assert payload["controls"]["checks_parsed"] >= 200
    assert payload["verify_learning_pins"]


def test_cli_file_scope_narrows_the_result():
    """The sibling's actual call. A rule file with no pins must say so
    plainly, because that verdict is what unblocks an 81 KB shrink."""
    r = _run("--file", ".claude/rules/run-full-suite-after-deep-code.md")
    assert r.returncode == 0, r.stderr
    assert "no verify-learning pin reads this target" in r.stdout
    assert "TEST FILES reading this target: none" in r.stdout


def test_cli_file_scope_does_not_attribute_global_unresolved_tokens():
    """An unresolved token belongs to no file; printing the global list under
    a --file scope reads as 'your file has broken pins'."""
    r = _run("--file", ".claude/rules/run-full-suite-after-deep-code.md")
    assert "did not resolve to a file" not in r.stdout


def test_cli_file_scope_surfaces_claude_md_table_pins():
    """CLAUDE.md's residual coupling is table-row shaped, and the report must
    show the rows themselves.

    Deliberately does NOT name a specific row. The first version asserted
    "Convention Index" or "goal-selection.md" — both real at the time and both
    removed hours later by this goal's own re-anchor, so the test went red on
    the fix it was written to accompany. A pin that names the very rows the
    work exists to decouple is a pin against progress (guard-1802 shape:
    predicate narrower than the population it must cover).
    """
    r = _run("--file", "CLAUDE.md")
    assert r.returncode == 0, r.stderr
    assert "TABLE-HOSTED" in r.stdout
    host_rows = [ln for ln in r.stdout.splitlines()
                 if ln.strip().startswith("host:") and "|" in ln]
    assert host_rows, (
        "no table-row host lines printed — either CLAUDE.md's table coupling is "
        "fully gone (update this test and say so) or the TABLE verdict broke")
