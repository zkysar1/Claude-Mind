"""Pin : the multi-unit claim advisory in aspirations-claim.sh.

The unit-level claim (unit-claim.sh) shipped wired into the WORKER path only,
as worker-loop Phase 2.95. The reducer also executes goals, through
aspirations-execute Phase 4, and had no unit-claim step — so a reducer and a
worker could build the same unit of a multi-unit goal concurrently, which is
measured to have happened once on g-326-422 (one full unit wasted).

The fix is placed in the shared claim WRAPPER rather than in a skill, and the
tests below pin the two properties that choice depends on:

  * BOTH orchestrators reach it (guard-4376 — a guard enforced at one entry
    point proves nothing about the other, which is how the gap arose);
  * the advisory goes to STDERR, because STDOUT carries the goal JSON that
    every caller of this wrapper parses.

The detection logic is not paraphrased here. Each behavioural test EXTRACTS the
python block the shell actually runs and executes it (guard-4323: validate
through the production code, never through an equivalent you write in the
probe).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
CLAIM_SH = SCRIPTS / "aspirations-claim.sh"
SRC = CLAIM_SH.read_text(encoding="utf-8")

_BLOCK_RE = re.compile(
    r'extracted="\$\(printf .%s. "\$response" \| \$\(rt_python_launcher\) -c "\n(.*?)\n" 2>/dev/null\)"',
    re.S,
)


def _shipped_block() -> str:
    m = _BLOCK_RE.search(SRC)
    assert m, (
        "the extraction block in _post_claim_effects has moved or changed shape; "
        "these tests run the SHIPPED block and cannot fall back to a copy."
    )
    return m.group(1)


def _run(payload: dict) -> list[str]:
    """Run the shipped extraction over one claim response; return its fields."""
    proc = subprocess.run(
        [sys.executable, "-c", _shipped_block()],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
    )
    return proc.stdout.rstrip("\n").split("\t")


def _goal(title: str = "t", description: str = "") -> dict:
    return {"goal": {"claimed_by": "alpha", "title": title, "description": description}}


class TestTheDetectionFiresOnAPerUnitProtocolAndNotOtherwise:

    @pytest.mark.parametrize("phrase", [
        "do them one at a time, smallest first",
        "one per pass, never two",
        "one unit per claim",
        "one PR each",
        "one PR per template",
        "this is a multi-unit goal",
        "work them one-by-one",
        "work them one by one",
    ])
    def test_fires_on_a_per_unit_instruction(self, phrase):
        fields = _run(_goal(description=phrase))
        assert fields[2], f"no marker for {phrase!r}"

    @pytest.mark.parametrize("text", [
        "list the last 30 emails and route each new alert",
        "evict aged terminal goals from the queue",
        "resolve the hypothesis and record the outcome",
    ])
    def test_silent_on_an_ordinary_goal(self, text):
        assert _run(_goal(description=text))[2] == ""

    def test_the_title_is_searched_too_not_only_the_description(self):
        assert _run(_goal(title="multi-unit soak across two boxes"))[2] == "multi-unit"

    def test_detection_is_case_insensitive(self):
        assert _run(_goal(description="ONE AT A TIME"))[2]


class TestTheFieldContractTheShellDependsOn:
    """The shell splits on tabs; a field-count change breaks title silently."""

    def test_success_emits_exactly_three_fields(self):
        assert len(_run(_goal(description="one at a time"))) == 3

    def test_the_error_path_also_emits_three_fields(self):
        """A malformed response must not collapse the field count.

        The pre-change code printed ONE tab on error. Adding a third field
        without widening that would leave `title` holding the marker on every
        parse failure — invisible, because the error path is the quiet one.
        """
        fields = _run({"not": "a goal response"})
        assert len(fields) == 3
        assert fields == ["", "", ""]

    def test_a_tabbed_title_cannot_forge_a_marker(self):
        """Titles are tab-stripped upstream, so field 3 stays authoritative."""
        fields = _run(_goal(title="a\tb\tone at a time"))
        assert len(fields) == 3
        assert fields[2] == "one at a time"  # from the title text, not the tabs
        assert "\t" not in fields[1]


class TestTheAdvisoryIsWiredWhereBothOrchestratorsReachIt:

    def test_the_advisory_names_the_unit_claim_command(self):
        assert "unit-claim.sh acquire" in SRC
        assert "g-306-323" in SRC

    def test_the_advisory_writes_to_stderr_not_stdout(self):
        """STDOUT carries the goal JSON every caller of this wrapper parses.

        A single printf without >&2 would corrupt that for every claim on a
        multi-unit goal — the failure would look like a JSON parse error far
        from here.
        """
        block = SRC.split("--- multi-unit claim advisory (g-306-323) ---", 1)[1]
        block = block.split("\n    fi\n", 1)[0]
        printfs = [ln for ln in block.splitlines() if "printf" in ln]
        assert printfs, "the advisory no longer prints anything"
        for ln in printfs:
            assert ln.rstrip().endswith(">&2"), f"advisory line not on stderr: {ln.strip()}"

    def test_post_claim_effects_runs_on_every_success_path(self):
        """Both the direct 200 and the autospawn-retry 200 must reach it.

        The wrapper has two `exit 0` success paths; an advisory on only one is
        the entry-point asymmetry (guard-4376) this placement exists to avoid.
        """
        assert SRC.count("_post_claim_effects \"$GOAL_ID\"") == 2

    def test_both_orchestrators_call_this_wrapper(self):
        """The premise of the placement: one insertion covers reducer + worker."""
        skills = SCRIPTS.parents[1] / ".claude" / "skills"
        for path in ("aspirations/SKILL.md", "worker-loop/SKILL.md"):
            text = (skills / path).read_text(encoding="utf-8", errors="replace")
            assert "aspirations-claim.sh" in text, path

    def test_the_wrapper_is_not_in_a_hot_path_budget_set(self):
        """The other half of the placement argument: zero hot-path bytes.

        Every budgeted set globs `.md` surfaces. If a set ever covers
        core/scripts/, this advisory starts costing ratcheted bytes and the
        rationale in the source comment needs re-deciding.
        """
        import yaml
        cfg = yaml.safe_load(
            (SCRIPTS.parents[1] / "core" / "config" / "hot-path-budget.yaml")
            .read_text(encoding="utf-8")
        )
        globs = [p for s in cfg["sets"] for p in s["paths"]]
        assert all("core/scripts" not in g for g in globs), globs
