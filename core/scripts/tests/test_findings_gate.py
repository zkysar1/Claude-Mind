"""findings-gate phantom-goal regression tests ().

Before this file existed, `pytest -k findings_gate` collected ZERO tests and
therefore exited green while asserting nothing at all.

Measured scope that motivated the fix (telemetry gate_id=findings-gate,
2026-07-03..2026-08-10, 38 days, all 6 agents): 51 invocations, 49 noop
(decision_path=scan-clean), 2 block. Both blocks produced a phantom HIGH
Unblock goal in the shared world queue, so the gate's precision on its firing
path was 0/2. The two firings took DIFFERENT decision paths, which is why a
negation guard alone is not sufficient:

  g-335-688  decision_path=signal-found          root_cause keyword matched
                                                 inside "Not caused by this goal."
  g-335-709  decision_path=investigation-override insight_text[:50] sliced across
                                                 a markdown heading + two newlines

The fixtures below are the LITERAL production text of both incidents, recovered
from the `Source:` block that make_child_goal embeds in each filed goal
(guard-920: replicate the production shape, not a contract-ideal paraphrase).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# findings-gate.py is hyphenated -> load by path
_spec = importlib.util.spec_from_file_location("findings_gate", SCRIPT_DIR / "findings-gate.py")
fg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg)


# --------------------------------------------------------------------------
# Literal production fixtures
# --------------------------------------------------------------------------

# : the closing paragraph of the real insight. The trigger sentence is
# "Not caused by this goal." — the fleet's standard exoneration idiom, recording
# that a downgrade pre-dated the goal rather than being a finding of it.
G335688_INSIGHT = (
    "A capability downgrade surfaced during propagation: ayoai-web-app and root "
    "moved EXPLOIT to CALIBRATE because the stored level was inconsistent with "
    "measured confidence 0.591. Not caused by this goal. How long the level had "
    "been stale, and whether other subtrees carry the same drift, is unmeasured."
)

# : the real insight opens with a markdown heading, so the old
# insight_text[:50] slice produced a title containing "#" and two newlines.
G335709_INSIGHT = (
    "# g-335-536 — findings\n"
    "\n"
    "Resolved hypothesis `2026-07-29_perception-verticles-lack-static-seam` CONFIRMED\n"
    "(confidence 0.45, surprise 6). Sample rule pre-committed to the execution diary\n"
    "before any code was read.\n"
)


# --------------------------------------------------------------------------
# Outcome 2 — a negated match no longer produces a goal
# --------------------------------------------------------------------------

def test_g335688_literal_insight_produces_no_signal():
    """The exact text that created  must now scan clean.

    Pre-fix this returned exactly one root_cause signal whose match was
    "caused by this goal", which became the title "Unblock: Fix caused by this
    goal".
    """
    assert fg.scan_signals(G335688_INSIGHT) == []


def test_g335688_literal_insight_creates_zero_goals_end_to_end(tmp_path):
    """Same fixture through the real CLI: findings_count=0 created=0.

    Exercised as a subprocess so argument parsing, the early-return branch and
    the machine-parseable summary line are all covered — not just scan_signals.
    The zero-signal path returns before load_dedup_titles, so this touches no
    aspiration state.
    """
    insight = tmp_path / "insight.md"
    insight.write_text(G335688_INSIGHT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "findings-gate.py"),
         "--goal", "g-test-688", "--insight-file", str(insight),
         "--aspiration", "asp-test", "--category", "framework-architecture",
         "--dry-run"],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "PYTEST_CURRENT_TEST": "findings-gate-regression"},
    )
    assert result.returncode == 0, result.stderr
    assert "findings_count=0 created=0" in result.stdout


@pytest.mark.parametrize("text", [
    "These failures are pre-existing and NOT caused by g-115-3611.",
    "The regression is not due to the index change.",
    "Verified: never caused by the sync path.",
    "The wedge is NOT caused by ensure_ascii=False per se.",
    "Surfaced by review, not caused by it.",
])
def test_genuine_negations_are_suppressed(text):
    """The exoneration idiom, in the phrasings actually observed in the corpus.

    All five are real shapes drawn from the 53 corpus matches inspected when
    sizing the disqualifier.
    """
    assert fg.scan_signals(text) == []


# --------------------------------------------------------------------------
# Outcome 3 — the fix must not simply mute the gate (RECALL)
# --------------------------------------------------------------------------
# guard-958: verifying recall needs an ADVERSARIAL positive control — a single
# surviving keyword that is the SOLE matcher AND sits adjacent to the new
# disqualifier. A multi-keyword happy path masks recall loss.

def test_recall_plain_genuine_finding_still_fires():
    signals = fg.scan_signals(
        "The loop wedged for six hours. Root cause is the lock never being "
        "released on the error path."
    )
    assert len(signals) == 1
    assert signals[0]["type"] == "root_cause"


def test_recall_adversarial_negation_binds_to_a_different_verb():
    """"Do not de-dupe ..." negates de-dupe, not the cause. Signal must survive.

    This is the shape that a character-window disqualifier gets wrong. Measured:
    a 30-char window suppressed 21 corpus matches beyond the immediate-token
    form, several of them genuine findings exactly like this one.
    """
    signals = fg.scan_signals(
        "Do not de-dupe the queue yet. The duplicate rows are caused by the "
        "missing unique index on goal_id."
    )
    assert len(signals) == 1, "negation bound to a different verb must not suppress"


def test_recall_adversarial_ordinary_determiner_no():
    """guard-1378: "no" has a strong ordinary sense. "no doubt ... caused by"
    is a genuine finding and must survive."""
    signals = fg.scan_signals(
        "There is no doubt the stall is caused by the missing jitter in the "
        "retry loop."
    )
    assert len(signals) == 1


def test_negation_in_a_previous_sentence_does_not_suppress():
    """The disqualifier is confined to the current sentence."""
    signals = fg.scan_signals(
        "The retry path was not touched. The stall is caused by the missing "
        "jitter."
    )
    assert len(signals) == 1


def test_resolution_suppression_still_works():
    """Pre-existing behaviour must be unchanged by the negation guard."""
    assert fg.scan_signals("Root cause was the stale lock, fixed by releasing it.") == []


# --------------------------------------------------------------------------
# The investigation-override path (the second, distinct defect)
# --------------------------------------------------------------------------

def test_first_prose_fragment_skips_markdown_heading():
    """: the heading and the paragraph breaks must not reach the title."""
    fragment = fg._first_prose_fragment(G335709_INSIGHT)
    assert fragment.startswith("Resolved hypothesis")
    assert "#" not in fragment
    assert "\n" not in fragment


def test_first_prose_fragment_keeps_bullets():
    """Bullets are prose-bearing: skipping them would silently discard the
    caller's deliberate --investigation-needs-action signal."""
    assert fg._first_prose_fragment("# H\n\n- the sweep never ran").startswith("- the sweep")


def test_first_prose_fragment_returns_empty_when_no_prose_exists():
    """Fail-closed: no prose line means nothing can name the finding."""
    assert fg._first_prose_fragment("# heading\n\n=====\n\n###\n") == ""


# --------------------------------------------------------------------------
# Shared-surface invariant
# --------------------------------------------------------------------------

@pytest.mark.parametrize("signal_type", ["root_cause", "investigation_finding", "proposed_fix"])
def test_goal_title_never_contains_a_newline(signal_type):
    """Every signal path converges on make_child_goal, so the invariant holds
    there regardless of which producer supplied the fragment."""
    goal = fg.make_child_goal(
        {"type": signal_type, "match": "line one\n\nline two\ttabbed"},
        "g-src", "framework-architecture", "insight body",
    )
    assert "\n" not in goal["title"]
    assert "\r" not in goal["title"]
    assert goal["title"].endswith("line one line two tabbed")
    assert "\n" not in goal["description"].split("\n\n")[0]


def test_child_goal_still_carries_its_provenance_fields():
    """Dedup, audits and this test file all key on these — a fix that dropped
    them would be invisible until an audit came back empty."""
    goal = fg.make_child_goal(
        {"type": "root_cause", "match": "the stale lock"},
        "g-src", "framework-architecture", "insight body",
    )
    assert goal["discovered_by"] == "g-src"
    assert goal["discovery_type"] == "root_cause"
    assert goal["origin_signal"] == "unblock:g-src"
    assert goal["priority"] == "HIGH"
    assert "Discovered by: Step 8.5 Actionable Findings Gate" in goal["description"]
    json.dumps(goal)  # must stay serialisable for aspirations-add-goal.sh stdin
