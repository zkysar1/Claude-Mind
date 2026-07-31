"""Production-shape tests for tree-split-verify.py (gap-051 / ).

Every test invokes the script exactly as the /tree split-overcap procedure
does — a subprocess with real argv (guard-920: replicate the literal
production arg shape, never an imported-function ideal).

Fixture semantics mirror the measured g-115-4069 incident: content loss must
FAIL loudly with the lost line listed; decoration reformatting must PASS;
a zero denominator must be exit 2 (vacuous), never a pass (guard-1220 /
rb-4133 discriminating-power: pass and fail fixtures drive DISTINCT verdicts).
Assertions are per-fixture on exit code + specific content (guard-1793 — no
aggregate-only assertion to mutate around).
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tree-split-verify.py"

ORIGINAL = """---
topic: "Sample Node"
last_updated: '2026-07-30'
---

# Sample Node

## Section Alpha

Alpha body line one with a **bold date 2026-07-29** inside.
Alpha body line two.
Repeated marker line.
Repeated marker line.
Repeated marker line.

## Section Beta

| 2026-07-30 | beta row one | detail |
Beta narrative sentence that must survive the split.

## Section Gamma

Gamma finding: the zeta 2026-07-30 07:29 section is the canary content.
"""

CHILD_A_VERBATIM = """---
topic: "Sample Node — Alpha"
---

# Sample Node — Alpha

## Section Alpha

Alpha body line one with a **bold date 2026-07-29** inside.
Alpha body line two.
Repeated marker line.
Repeated marker line.
Repeated marker line.
"""

CHILD_B_VERBATIM = """---
topic: "Sample Node — Beta+Gamma"
---

# Sample Node — Beta+Gamma

## Section Beta

| 2026-07-30 | beta row one | detail |
Beta narrative sentence that must survive the split.

## Section Gamma

Gamma finding: the zeta 2026-07-30 07:29 section is the canary content.
"""

# EDGE: decoration reformatted (bold -> strikethrough, header depth changed,
# table pipes re-spaced) but all CONTENT present.
CHILD_B_DECORATED = CHILD_B_VERBATIM.replace(
    "## Section Beta", "### Section Beta"
).replace(
    "| 2026-07-30 | beta row one | detail |", "|  2026-07-30  |  beta row one  |  detail  |"
)
CHILD_A_DECORATED = CHILD_A_VERBATIM.replace(
    "**bold date 2026-07-29**", "~~bold date 2026-07-29~~"
)

# FAIL: Section Gamma (the canary — a full section in NO child) dropped.
CHILD_B_GAMMA_LOST = """---
topic: "Sample Node — Beta"
---

## Section Beta

| 2026-07-30 | beta row one | detail |
Beta narrative sentence that must survive the split.
"""

# FAIL-duplicate: repeated line carried only twice (multiset deficit of 1).
CHILD_A_DUP_LOST = CHILD_A_VERBATIM.replace(
    "Repeated marker line.\nRepeated marker line.\nRepeated marker line.",
    "Repeated marker line.\nRepeated marker line.",
)

VACUOUS_ORIGINAL = """---
topic: "Metadata Only"
last_updated: '2026-07-30'
---
"""


def run(original: str, outputs: list[str], tmp_path: Path):
    op = tmp_path / "original.md"
    op.write_text(original, encoding="utf-8")
    outs = []
    for i, text in enumerate(outputs):
        p = tmp_path / f"out-{i}.md"
        p.write_text(text, encoding="utf-8")
        outs.append(str(p))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--original", str(op), "--outputs", *outs],
        capture_output=True, text=True,
    )


def test_pass_verbatim_split(tmp_path):
    r = run(ORIGINAL, [CHILD_A_VERBATIM, CHILD_B_VERBATIM], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "missing=0" in r.stdout
    assert "lines=" in r.stdout and "lines=0" not in r.stdout  # denominator printed, non-zero


def test_fail_full_section_lost(tmp_path):
    r = run(ORIGINAL, [CHILD_A_VERBATIM, CHILD_B_GAMMA_LOST], tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    # the specific lost content is named, not just counted
    assert "canary content" in r.stdout.lower()
    assert "missing=0" not in r.stdout


def test_fail_duplicate_instance_deficit(tmp_path):
    r = run(ORIGINAL, [CHILD_A_DUP_LOST, CHILD_B_VERBATIM], tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "repeated marker line" in r.stdout.lower()
    assert "missing=1" in r.stdout


def test_edge_decoration_tolerant(tmp_path):
    r = run(ORIGINAL, [CHILD_A_DECORATED, CHILD_B_DECORATED], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "missing=0" in r.stdout


def test_vacuous_zero_denominator_is_not_a_pass(tmp_path):
    r = run(VACUOUS_ORIGINAL, [CHILD_A_VERBATIM], tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "lines=0" in r.stdout
    assert "VACUOUS" in r.stdout


def test_bom_original_does_not_produce_spurious_missing(tmp_path):
    """A Windows-editor-touched (UTF-8-BOM) original must not FALSE-FAIL.

    U+FEFF is not in str.strip()'s whitespace set, so with plain utf-8 the
    front-matter fence test (`lines[0].strip() == "---"`) never matches, the
    ORIGINAL's front-matter keys enter the comparable set, and children — which
    carry DIFFERENT front matter by design — can never cover them. Fail
    direction is a loud false FAIL, so the split gate stays fail-closed, but the
    verdict is wrong. Reverting to encoding="utf-8" reds THIS test alone.
    (foxtrot, msg-20260730-213543-foxtrot-5245; mechanism verified g-115-4151.)
    """
    op = tmp_path / "original.md"
    op.write_text(ORIGINAL, encoding="utf-8-sig")   # the only difference
    out = tmp_path / "out-0.md"
    out.write_text(CHILD_A_VERBATIM + CHILD_B_VERBATIM, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--original", str(op), "--outputs", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "missing=0" in r.stdout
    # the specific false-positive shape: front-matter keys reported as lost
    assert "topic:" not in r.stdout
    assert "last_updated" not in r.stdout


def test_missing_sample_survives_cp1252_stdout(tmp_path):
    """The verdict diagnostic must survive a non-UTF-8 locale on piped stdout.

    Tree-node content is em-dash/arrow-heavy; on native Windows with piped
    stdout Python uses the locale encoding (cp1252) and a non-cp1252 char in
    the missing-sample loop raises UnicodeEncodeError — killing the `verdict:`
    line and the lost-content sample exactly when content IS missing. Forced
    here via PYTHONIOENCODING so the case reproduces off-Windows. U+2192 is
    deliberate: the em-dash IS in cp1252 (0x97) and would not trip it.
    Removing the guarded reconfigure reds THIS test alone.
    (foxtrot, msg-20260730-213552-foxtrot-5246; verified g-115-4151.)
    """
    op = tmp_path / "original.md"
    op.write_text(ORIGINAL.replace("Gamma finding:", "Gamma finding → arrow:"),
                  encoding="utf-8")
    out = tmp_path / "out-0.md"
    out.write_text(CHILD_A_VERBATIM + CHILD_B_GAMMA_LOST, encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--original", str(op), "--outputs", str(out)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "verdict: MISSING_CONTENT" in r.stdout      # the line that was lost
    assert "canary content" in r.stdout.lower()        # the sample that was lost
    assert "UnicodeEncodeError" not in r.stderr


def test_usage_error_missing_output_file(tmp_path):
    op = tmp_path / "original.md"
    op.write_text(ORIGINAL, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--original", str(op),
         "--outputs", str(tmp_path / "does-not-exist.md")],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "not found" in r.stderr
