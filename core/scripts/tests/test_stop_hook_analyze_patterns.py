#!/usr/bin/env python3
"""Regression pin for stop-hook-analyze.sh's pat_block / pat_allow ().

WHY THIS FILE EXISTS. `stop-hook-analyze.sh` is a safety-net analyzer: it reads
BLOCK/ALLOW rows from core/logs/stop-hook.log and warns when an agent's loop
stalls. Its failure mode is silent and one-directional — when pat_block stops
matching a writer's shape, ALLOW rows can still CLEAR a streak that BLOCK rows
can no longer BUILD, so the analyzer prints "no loop stalls detected", which is
byte-identical to its healthy-fleet output. The first instance of that ran two
days unnoticed (worker-net gate added 2026-08-04, consumer blind until 08-06).

That fix was proved by an ad-hoc synthetic probe and left no test behind, so
nothing would have caught the next recurrence. This file is that net.

THE PATTERNS ARE READ FROM THE LIVE SCRIPT, NOT COPIED HERE. A test carrying
its own copy of the regex pins the copy, not the code — it would stay green
through any edit to stop-hook-analyze.sh, which is precisely the regression
class this file exists to catch.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# guard-580: never build a ["bash", ...] argv by hand — a bare `bash` on Windows
# PATH resolves to WSL bash, which sees the repo under /mnt/c and cannot exec the
# script. BASH is the canonical resolution shared by every test in this tree.
from _bash_helpers import BASH  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ANALYZE_SH = PROJECT_ROOT / "core" / "scripts" / "stop-hook-analyze.sh"
STOP_HOOK_SH = PROJECT_ROOT / "core" / "scripts" / "stop-hook.sh"

TS = "2026-08-10T11:00:00"

# The pre- pattern, frozen as a literal. This is the DISCRIMINATOR:
# every widening case below must fail under OLD and pass under NEW. Without it
# the suite would pass against a reverted script (guard-385).
OLD_PAT_BLOCK = re.compile(
    r"^(?P<ts>\S+) BLOCK (?:gate=\S+ )?sid=(?P<sid>\S+) agent=(?P<agent>\S+)"
)


def _extract_pattern(name: str) -> re.Pattern:
    """Pull a compiled `name = re.compile(r"...")` out of the live shell script.

    Tolerates the assignment being wrapped across lines (black-style), which is
    how pat_block is written after g-115-5367.
    """
    src = ANALYZE_SH.read_text(encoding="utf-8", errors="replace")
    m = re.search(
        rf'^{re.escape(name)}\s*=\s*re\.compile\(\s*r"(?P<body>(?:[^"\\]|\\.)*)"\s*,?\s*\)',
        src,
        re.MULTILINE,
    )
    assert m, f"could not extract {name} from {ANALYZE_SH}"
    return re.compile(m.group("body"))


@pytest.fixture(scope="module")
def pat_block() -> re.Pattern:
    return _extract_pattern("pat_block")


@pytest.fixture(scope="module")
def pat_allow() -> re.Pattern:
    return _extract_pattern("pat_allow")


# (label, row, matched_by_OLD) -- every row must match NEW.
# The False rows are the widening; the True rows are the no-regression proof.
BLOCK_ROWS = [
    ("main path, 0 intervening fields", f"{TS} BLOCK sid=S1 agent=alpha", True),
    (
        "main path with trailing runner_token",
        f"{TS} BLOCK sid=S1 agent=alpha runner_token=T9",
        True,
    ),
    (
        "worker-net gate, 1 intervening field",
        f"{TS} BLOCK gate=worker-net sid=S1 agent=alpha",
        True,
    ),
    (
        "2 intervening fields",
        f"{TS} BLOCK gate=worker-net phase=4 sid=S1 agent=alpha",
        False,
    ),
    ("1 differently-named field", f"{TS} BLOCK reason=stall sid=S1 agent=alpha", False),
    ("3 intervening fields", f"{TS} BLOCK gate=w gen=2 phase=4 sid=S1 agent=alpha", False),
    ("field between sid and agent", f"{TS} BLOCK sid=S1 phase=4 agent=alpha", False),
]


@pytest.mark.parametrize(
    "label,row,_old", BLOCK_ROWS, ids=[r[0].replace(" ", "-") for r in BLOCK_ROWS]
)
def test_pat_block_matches_arbitrary_intervening_fields(pat_block, label, row, _old):
    """0, 1, and 2+ intervening key=value fields all parse (the goal's criterion)."""
    m = pat_block.match(row)
    assert m, f"pat_block failed to match {label}: {row!r}"
    assert m.group("sid") == "S1", f"wrong sid for {label}: {m.group('sid')!r}"
    assert m.group("agent") == "alpha", f"wrong agent for {label}: {m.group('agent')!r}"
    assert m.group("ts") == TS


@pytest.mark.parametrize(
    "label,row,old_matches", BLOCK_ROWS, ids=[r[0].replace(" ", "-") for r in BLOCK_ROWS]
)
def test_discrimination_old_pattern_fails_where_widening_was_needed(
    label, row, old_matches
):
    """The pin has teeth: the OLD pattern must MISS every widened shape.

    If pat_block is reverted, the test above goes red on exactly these rows.
    This test asserts that claim directly rather than assuming it (guard-385).
    """
    assert (OLD_PAT_BLOCK.match(row) is not None) is old_matches, (
        f"{label}: OLD pattern discrimination expectation violated -- "
        f"expected match={old_matches}"
    )


def test_widening_does_not_capture_sid_from_inside_a_field_value(pat_block):
    """rb-7110: anchor on the discriminating marker, not on a field siblings emit.

    pat_allow uses `.*?sid=`, which halts at the FIRST `sid=` substring even when
    that substring sits inside another field's VALUE. pat_block deliberately uses
    whole-field consumption instead, so it cannot mis-capture this way. Measured:
    the naive `.*?` mirror returns sid='x' on this row.
    """
    row = f"{TS} BLOCK reason=missing-sid=x sid=S1 agent=alpha"
    m = pat_block.match(row)
    assert m, "pat_block should still match a row with an embedded sid= substring"
    assert m.group("sid") == "S1", (
        f"pat_block captured sid={m.group('sid')!r} from inside a field value -- "
        "the whole-field tolerance regressed to a lazy `.*?`"
    )

    naive = re.compile(r"^(?P<ts>\S+) BLOCK .*?sid=(?P<sid>\S+).*? agent=(?P<agent>\S+)")
    assert naive.match(row).group("sid") == "x", (
        "positive control failed: the naive mirror is supposed to mis-capture here, "
        "so this test would not be discriminating"
    )


def test_pat_block_still_requires_the_block_marker(pat_block):
    """The literal BLOCK token after the timestamp is the discriminating anchor.

    Whole-field tolerance must not let an ALLOW row (or any other row type) be
    parsed as a BLOCK -- that would let the analyzer BUILD streaks from the very
    rows that are supposed to CLEAR them.
    """
    for row in (
        f"{TS} ALLOW sid=S1 agent=alpha",
        f"{TS} ALLOW gate=worker-net sid=S1 agent=alpha",
        f"{TS} NOTE BLOCK sid=S1 agent=alpha",  # BLOCK not adjacent to the timestamp
    ):
        assert pat_block.match(row) is None, f"pat_block wrongly matched: {row!r}"


def test_pat_allow_still_clears_only_matching_sid(pat_allow):
    """No-regression guard on the sibling pattern this change did NOT touch."""
    m = pat_allow.match(f"{TS} ALLOW reason=ok sid=S1 agent=alpha")
    assert m and m.group("sid") == "S1" and m.group("agent") == "alpha"


def test_every_live_block_writer_shape_is_matched(pat_block):
    """Anti-drift: parse stop-hook.sh's ACTUAL BLOCK emitters and match each one.

    The 2026-08-04 recurrence happened because a new writer shape appeared and
    nothing joined the writer side to the reader side. This test fails when a
    future writer emits a shape pat_block cannot parse -- without anyone having
    to remember to update a hardcoded list here.
    """
    src = STOP_HOOK_SH.read_text(encoding="utf-8", errors="replace")

    # Extract the PAYLOAD only, anchored on the emit statement itself. An
    # earlier version matched on the field shape (`BLOCK (?:[a-z_]+=\S+ )*sid=...`)
    # and had two fail-SILENT defects, both found by fresh-eyes review of this
    # very file -- the same defect class the test exists to catch, reproduced
    # inside the test:
    #   1. It silently SKIPPED any emitter whose field keys did not match
    #      `[a-z_]+` (a digit, uppercase, or hyphen in a key) or that placed a
    #      field between sid= and agent=. Measured: 4 of 5 plausible future
    #      shapes were skipped, and because `assert emitters` only checks
    #      NON-EMPTY, the two existing emitters kept the test green. A new
    #      writer shape would have gone undetected -- exactly the 2026-08-04
    #      recurrence this test is the net for.
    #   2. `agent=\S+` swallowed the closing quote, so the parsed shape carried
    #      trailing shell syntax (`agent=$HOOK_AGENT" >> `) and the capture came
    #      back as `V"`. The old `assert m.group("agent")` (truthiness) passed on
    #      that corrupted value.
    # Anchoring on `echo "$(date ...) <payload>" >> "$LOG"` makes the extractor
    # agnostic to the payload's internal shape, which is the whole point.
    emit_re = re.compile(r'echo "\$\(date [^"]*\) (?P<payload>BLOCK [^"]*)" >> "\$LOG"')
    emitters = [m.group("payload") for m in emit_re.finditer(src)]
    assert emitters, "found no BLOCK emitters in stop-hook.sh -- extractor drifted"

    # Cross-check the extractor against a coarser ground truth: ANY line that
    # echoes something containing " BLOCK " into $LOG must have been extracted.
    # Without this, a shape the payload regex cannot see is silently dropped and
    # the test still passes -- defect (1) above, in its general form.
    candidates = [
        ln
        for ln in src.splitlines()
        if " BLOCK " in ln and 'echo "' in ln and '>> "$LOG"' in ln
    ]
    assert len(emitters) == len(candidates), (
        f"extractor saw {len(emitters)} BLOCK emitter(s) but {len(candidates)} line(s) "
        f"echo a BLOCK row into $LOG -- a writer shape is invisible to this test. "
        f"Missed: {[c.strip()[:100] for c in candidates if not emit_re.search(c)]}"
    )

    for raw in emitters:
        # Substitute shell expansions with representative literals.
        row = f"{TS} " + re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "V", raw).strip()
        m = pat_block.match(row)
        assert m, f"pat_block cannot parse a LIVE stop-hook.sh BLOCK shape: {row!r}"
        # EXACT, not truthy: a truthiness check passed on the corrupted `V"`
        # capture that defect (2) produced.
        assert m.group("agent") == "V", (
            f"agent captured as {m.group('agent')!r} (expected 'V') from live shape "
            f"{row!r} -- the extractor is carrying shell syntax into the payload"
        )
        assert m.group("sid") == "V", (
            f"sid captured as {m.group('sid')!r} (expected 'V') from live shape {row!r}"
        )


def test_analyzer_runs_clean_on_a_synthetic_log(tmp_path):
    """End-to-end: the script still runs and reports no stalls on a benign log.

    Guards against a syntax error in the heredoc that unit-level regex tests
    cannot see. FRESHNESS_SEC=0 keeps the run deterministic.
    """
    log = tmp_path / "stop-hook.log"
    log.write_text(
        "\n".join(
            [
                f"{TS} BLOCK gate=worker-net phase=4 sid=S1 agent=nosuchagent",
                f"{TS} ALLOW sid=S1 agent=nosuchagent",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # guard-580 / guard-581: resolved BASH, and .as_posix() — bash silently
    # strips the backslashes of a str(WindowsPath).
    proc = subprocess.run(
        [BASH, ANALYZE_SH.as_posix()],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "LOG_PATH": str(log),
            "FRESHNESS_SEC": "0",
            "STORAGE_BACKEND": "local",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"analyzer exited {proc.returncode}: {proc.stderr[-800:]}"
    assert "no loop stalls detected" in proc.stdout, (
        f"unexpected analyzer output: {proc.stdout[-500:]} / {proc.stderr[-500:]}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
