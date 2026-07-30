"""Step 8.79a branch-landed advisory must read EVERY partition of stranded_all.

WHY THIS FILE EXISTS. The advisory shipped (5f8387097) reading only
`report['stranded']`. But completed-not-committed-sweep.py PARTITIONS its
stranded results by reason:

    stranded       = [e for e in stranded_all if e["reason"] == "stranded_open_pr"]
    stranded_no_pr = [e for e in stranded_all if e["reason"] == "stranded_no_pr"]

Those are DISJOINT sibling keys, so the consumer was blind to every branch
pushed WITHOUT a pull request — the harder case to notice, and the exact shape
of the six-day incident that motivated g-115-3838. The goal's own description
had warned about this blindness (it preferred branch enumeration over
PR-listing because the request-listing form "is blind to a branch pushed
without one"); the first cut avoided it at the DETECTION layer and then
reintroduced it one layer up at consumption.

Nothing caught it. The committed tests drove `classify_stranded`, which is
correct — the defect was entirely in which key the consumer read, a layer with
no coverage at all. The banner had been checked by hand against a canned report
that the author built, and so encoded the author's own wrong belief about the
schema.

WHAT IS TESTED HERE, and why it is structural rather than a canned fixture:
a fixture test proves the consumer handles the two partitions that exist TODAY.
The recurring risk is a THIRD partition added later to the producer, which
would silently drop out of the advisory the same way. So the coupling test
derives the partition set from the producer's source and asserts the consumer
references all of it. It fails on the original commit, passes now, and keeps
failing if the producer grows a new bucket.

guard-1802: diff the consumer's predicate against the producer's population and
measure what it EXCLUDES — a subset predicate and a genuinely clean queue emit
the identical all-clear.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
SWEEP = CORE_SCRIPTS / "completed-not-committed-sweep.py"
ITERATION_CLOSE = CORE_SCRIPTS / "iteration-close.sh"

# Anchors the Step 8.79a block. Kept narrow on purpose: matching the whole file
# would let a reference from ANY other block satisfy the assertion below.
_BLOCK_START = "Step 8.79a Branch-landed advisory"
_BLOCK_END = "Step 8.79 Compounding-knowledge metric emission"


def _advisory_block() -> str:
    """The literal Step 8.79a text from the production script.

    Read from disk rather than re-typed, so the test cannot drift from the code
    it guards (guard-920: replicate the production shape, not the ideal one)."""
    text = ITERATION_CLOSE.read_text(encoding="utf-8", errors="replace")
    start = text.index(_BLOCK_START)
    end = text.index(_BLOCK_END, start)
    return text[start:end]


def _advisory_code() -> str:
    """The advisory block with shell COMMENT lines stripped.

    Found by mutation-testing this file's own coupling assertion: with the
    comments left in, reverting the code to the buggy single-key form still
    PASSED, because the explanatory comment above it mentions `stranded_no_pr`
    in prose. A coupling test a comment can satisfy is not a coupling test —
    documenting a key and reading it are different acts, and the whole defect
    class here is a consumer that looks correct while reading a subset.

    Strips only whole-line `#` comments; the python -c snippets are inside
    double-quoted shell strings and survive intact."""
    return "\n".join(ln for ln in _advisory_block().splitlines()
                     if not ln.lstrip().startswith("#"))


def _producer_partition_keys() -> set[str]:
    """Report keys the producer partitions stranded_all into.

    Derived from the source, not hardcoded — the whole point is to notice when
    the producer grows a partition the consumer does not know about."""
    src = SWEEP.read_text(encoding="utf-8", errors="replace")
    # e.g.  stranded_no_pr = [e for e in stranded_all if e["reason"] == "..."]
    return set(re.findall(
        r"^\s*(\w+)\s*=\s*\[\s*e\s+for\s+e\s+in\s+stranded_all\b",
        src, re.MULTILINE))


def _report_key_for(var_name: str) -> str:
    """Map a producer local (stranded / stranded_no_pr) to its emitted JSON key.

    They happen to match today, but the emit block is the authority — a local
    renamed without renaming its key would otherwise silently pass."""
    src = SWEEP.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\s*"([^"]+)"\s*:\s*%s\s*,\s*$' % re.escape(var_name),
                  src, re.MULTILINE)
    return m.group(1) if m else var_name


def test_producer_really_partitions_into_more_than_one_list():
    """Positive control. If the producer ever stops partitioning, the coupling
    assertion below becomes vacuously true and would pass forever while
    guarding nothing (guard-1832 — subject absent from the target)."""
    keys = _producer_partition_keys()
    assert len(keys) >= 2, (
        f"expected stranded_all to be partitioned into 2+ lists, found {keys} — "
        "if the producer was intentionally collapsed to one list, this whole "
        "test file is obsolete and should be retired, not weakened")
    assert "stranded" in keys and "stranded_no_pr" in keys, keys


def test_advisory_reads_every_partition_the_producer_emits():
    """THE REGRESSION TEST. Fails against 5f8387097, which referenced only
    `stranded` and so never saw a branch pushed without a PR."""
    code = _advisory_code()          # comments stripped — see _advisory_code
    missing = [
        _report_key_for(v) for v in sorted(_producer_partition_keys())
        if _report_key_for(v) not in code
    ]
    assert not missing, (
        f"Step 8.79a advisory does not reference producer partition(s) {missing}. "
        "completed-not-committed-sweep.py splits stranded_all by reason into "
        "disjoint report keys; a consumer reading a strict subset reports the "
        "same all-clear as a genuinely clean queue (guard-1802).")


def test_count_expression_sums_both_partitions_rather_than_reading_one():
    """Behavioural half: run the REAL counting expression lifted out of the
    script against a report whose only stranded entry has NO pull request.

    Under the shipped code this returned 0 and the banner stayed silent."""
    block = _advisory_block()
    m = re.search(r'_stranded_n=\$\(echo "\$_landed_json" \| python3 -c "(.+?)"',
                  block, re.DOTALL)
    assert m, "could not locate the _stranded_n counting expression in Step 8.79a"
    expr = m.group(1)

    no_pr_only = {
        "stranded": [],
        "stranded_no_pr": [{"goal_id": "g-350-77", "reason": "stranded_no_pr"}],
    }
    proc = subprocess.run([sys.executable, "-c", expr],
                          input=json.dumps(no_pr_only),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert proc.stdout.strip() == "1", (
        f"counted {proc.stdout.strip()!r} for a report whose only stranded entry "
        "lacks a PR — the advisory is blind to branches pushed without one")


def test_count_expression_adds_the_two_partitions_instead_of_overwriting():
    """Guards the obvious wrong fix: switching which single key is read, or
    letting one lookup shadow the other. 2 + 1 must be 3, not 2 and not 1."""
    block = _advisory_block()
    expr = re.search(r'_stranded_n=\$\(echo "\$_landed_json" \| python3 -c "(.+?)"',
                     block, re.DOTALL).group(1)
    both = {
        "stranded": [{"goal_id": "a", "reason": "stranded_open_pr"},
                     {"goal_id": "b", "reason": "stranded_open_pr"}],
        "stranded_no_pr": [{"goal_id": "c", "reason": "stranded_no_pr"}],
    }
    proc = subprocess.run([sys.executable, "-c", expr],
                          input=json.dumps(both),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert proc.stdout.strip() == "3", proc.stdout.strip()


def test_count_expression_still_returns_zero_on_a_clean_report():
    """The all-clear must survive the fix — an advisory that fires on every
    close is one that gets ignored on the close that matters."""
    block = _advisory_block()
    expr = re.search(r'_stranded_n=\$\(echo "\$_landed_json" \| python3 -c "(.+?)"',
                     block, re.DOTALL).group(1)
    proc = subprocess.run(
        [sys.executable, "-c", expr],
        input=json.dumps({"stranded": [], "stranded_no_pr": [], "scanned": 1}),
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert proc.stdout.strip() == "0", proc.stdout.strip()


def test_printer_emits_a_line_for_a_no_pr_entry():
    """The printer carried a 'no open pull request found' branch that was
    UNREACHABLE on the shipped code, because every member of `stranded` has a PR
    by construction. A handler for a case its input cannot contain is the
    fingerprint that the author believed the key was the union — so assert the
    branch is now genuinely reachable."""
    block = _advisory_block()
    m = re.search(r"echo \"\$_landed_json\" \| python3 -c \"\n(.+?)\n\" 2>/dev/null",
                  block, re.DOTALL)
    assert m, "could not locate the Step 8.79a printer snippet"
    proc = subprocess.run(
        [sys.executable, "-c", m.group(1)],
        input=json.dumps({
            "stranded": [],
            "stranded_no_pr": [{"goal_id": "g-350-77", "reason": "stranded_no_pr"}],
        }),
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "g-350-77" in proc.stdout
    assert "no open pull request found" in proc.stdout, proc.stdout
