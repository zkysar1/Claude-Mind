"""The skills that tell a reader to build a pipeline-add payload must name all nine creation fields ().

The incident: the sq-009 step of aspirations-spark showed `echo '<record-json>' | pipeline-add.sh` and listed
only the ACTIVE-stage contract, so a payload built from the step in front of the reader was refused with
"Missing required fields". The same rejection recurred at least four times (rb-8127 plus three sessions)
while guard-851 named the nine fields and was retrievable: the passage that produces the error was never
edited (guard-1984).

The step text is a COPY of REQUIRED_FIELDS (guard-426), so this test diffs the copy against the source. The
source lives in two files (the CLI module and the daemon writer that actually enforces it), so both are read
and pinned equal: a drift between them would leave the skills diffed against the wrong set.

REQUIRED_FIELDS is read from source text with ast and never imported, so the check needs neither the daemon
nor a world.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CLI_SOURCE = ROOT / "core" / "scripts" / "pipeline.py"
DAEMON_SOURCE = ROOT / "mind_api" / "src" / "world" / "pipeline_write.py"
# The skills whose numbered step tells a reader to build the pipeline-add payload.
SKILLS = [
    ROOT / ".claude" / "skills" / "aspirations-spark" / "SKILL.md",
    ROOT / ".claude" / "skills" / "decompose" / "SKILL.md",
]
STEP_OPENER = "1. Create pipeline record:"


def required_fields(path):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_FIELDS" for t in node.targets
        ):
            return {e.value for e in node.value.elts}
    raise AssertionError(f"REQUIRED_FIELDS not found in {path}")


def step_text(path):
    """The opener's numbered step, up to the next top-level numbered step."""
    text = path.read_text(encoding="utf-8")
    assert text.count(STEP_OPENER) == 1, f"{path.parent.name}: expected exactly one {STEP_OPENER!r}"
    tail = text[text.index(STEP_OPENER):]
    following = re.search(r"^2\. ", tail, re.MULTILINE)
    assert following, f"{path.parent.name}: no step 2 after the opener"
    return tail[: following.start()]


def missing_fields(text, required):
    """Required names the step does not carry as a backticked token."""
    return required - set(re.findall(r"`([a-z_]+)`", text))


def test_daemon_and_cli_copies_agree():
    assert required_fields(CLI_SOURCE) == required_fields(DAEMON_SOURCE)


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_step_names_every_required_field(skill):
    assert missing_fields(step_text(skill), required_fields(CLI_SOURCE)) == set()


def test_check_can_fail():
    """Positive control (guard-2421): a step that drops a field, or carries none, is reported."""
    required = required_fields(CLI_SOURCE)
    spark = step_text(SKILLS[0])
    assert missing_fields(spark.replace("`position`", "position"), required) == {"position"}
    assert missing_fields("", required) == required
