"""test_iteration_commit_clear_inversion.py —  regression test.

Pins the ONE property of iteration-commit.sh's post-commit clear that was
never covered, and whose absence let a wrong root-cause be inferred:

    A post-hoc grep of agents/<agent>/session/uncommitted-edits.jsonl is an
    INVERTED diagnostic for "did the attribution filter drop my file?".

The clear (iteration-commit.sh, "Clear own uncommitted-edits.jsonl on
committed paths", g-115-697) is keyed on the COMMITTED set: it prunes rows
whose path was committed and deliberately preserves rows whose path was not
("Entries for files we haven't committed yet remain — partner
iteration-commits still need that signal"). So the log's post-commit state is
causally DOWNSTREAM of the outcome an investigator is trying to explain:

    RETAINED (exempted) -> committed   -> row PRUNED    -> grep matches 0
    DROPPED  (filtered) -> not staged  -> row PRESERVED -> grep matches 1

g-115-4232 read that inversion as evidence that the g-115-828 first-person
exemption "does not match its own message" — grepping foxtrot's log gave the
inverse of the observed outcome. It is not a predicate/message mismatch; it
is the clear working exactly as designed. The exemption predicate itself is
covered by test_iteration_commit_concurrent_partner.py
(test_g115828_committer_own_log_exempts_and_genuine_partner_still_drops and
its per-path / absolute-path siblings) — this file deliberately does NOT
duplicate that.

The clear is exercised as PRODUCTION BYTES: the heredoc is extracted from
iteration-commit.sh at test time rather than re-implemented, so the test
cannot drift from the code it pins (rb-5235 — probe the canonical code path,
not a synthetic equivalent).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent
ITERATION_COMMIT = SCRIPT_DIR / "iteration-commit.sh"
ITERATION_CLOSE = SCRIPT_DIR / "iteration-close.sh"

CLEAR_BLOCK_MARKER = 'COMMITTED="$committed_set"'


def _extract_clear_block():
    """Return the production clear heredoc from iteration-commit.sh verbatim.

    Anchored on the env-prefix line that opens the heredoc, terminated by the
    PYEOF sentinel. Fails loudly rather than silently returning a prefix — a
    truncated extraction would make every assertion below vacuous.
    """
    lines = ITERATION_COMMIT.read_text(encoding="utf-8").splitlines()
    start = None
    for i, line in enumerate(lines):
        if CLEAR_BLOCK_MARKER in line and "<<'PYEOF'" in line:
            start = i + 1
            break
    assert start is not None, (
        "could not locate the post-commit clear heredoc in iteration-commit.sh "
        f"(anchor {CLEAR_BLOCK_MARKER!r} + <<'PYEOF'). If the block was renamed "
        "or restructured, update this extraction — do NOT re-implement the clear."
    )
    end = None
    for j in range(start, len(lines)):
        if lines[j] == "PYEOF":
            end = j
            break
    assert end is not None, "clear heredoc has no closing PYEOF sentinel"
    body = "\n".join(lines[start:end])
    assert "os.replace(" in body, (
        "extracted clear block does not end in the atomic rename — extraction "
        "is truncated, so every assertion in this file would be vacuous"
    )
    return body


def _run_clear(tmp_path, rows, committed):
    """Write `rows` to a log, run the production clear with `committed`, and
    return the surviving lines."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "uncommitted-edits.jsonl"
    log.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    script = tmp_path / "clear_block.py"
    script.write_text(_extract_clear_block(), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "OWN_LOG": str(log),
            "COMMITTED": "\n".join(committed),
            "XAGENT_SCRIPTS": str(SCRIPT_DIR),
            "XAGENT_ROOT": str(PROJECT_ROOT),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"production clear block exited {proc.returncode}: {proc.stderr!r}"
    )
    return [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_clear_prunes_committed_and_preserves_uncommitted(tmp_path):
    """The clear's contract: committed -> pruned, uncommitted -> preserved."""
    rows = [
        {"file": "core/scripts/committed.py", "goal_id": "g-1"},
        {"file": "core/scripts/still-dirty.py", "goal_id": "g-1"},
    ]
    kept = _run_clear(tmp_path, rows, committed=["core/scripts/committed.py"])

    assert len(kept) == 1, f"expected exactly one surviving row, got {kept!r}"
    assert "still-dirty.py" in kept[0], (
        "the clear pruned the UNCOMMITTED row — that would destroy the "
        "between-claim signal partner iteration-commits depend on (g-115-697)"
    )
    assert "committed.py" not in kept[0], (
        "the clear failed to prune a committed row — a stale record then "
        "false-drops a partner's later legitimate edit at that path (rb-2186)"
    )


def test_post_hoc_grep_is_inverted_for_identically_recorded_files(tmp_path):
    """The canonical  reproduction.

    Three files are recorded IDENTICALLY in the committer's own log — same
    shape, same goal, all present before the commit. The only difference is
    the attribution filter's verdict: two were retained (and therefore
    committed), one was dropped (and therefore not staged).

    After the clear, a substring grep of the log reports the exact inverse of
    the outcome. This is what g-115-4232 measured and read as a
    predicate/message mismatch.
    """
    rows = [
        # DROPPED by the concurrent-partner filter -> never staged.
        {"file": "core/scripts/aspirations_write.py", "goal_id": "g-115-4232"},
        # RETAINED by the  exemption -> staged and committed.
        {"file": "core/scripts/execution-diary.py", "goal_id": "g-115-4232"},
        {"file": "core/scripts/unrelated-sibling.py", "goal_id": "g-115-4232"},
    ]
    kept = _run_clear(
        tmp_path,
        rows,
        committed=[
            "core/scripts/execution-diary.py",
            "core/scripts/unrelated-sibling.py",
        ],
    )
    blob = "\n".join(kept)

    # The DROPPED file greps to a hit...
    assert blob.count("aspirations_write") == 1, (
        "the dropped file should still be recorded post-clear; without this "
        "the inversion below is not the mechanism being pinned"
    )
    # ...while both RETAINED files grep to nothing.
    assert "execution-diary" not in blob
    assert "unrelated-sibling" not in blob

    # Stated as the property an investigator must not be fooled by: presence
    # in the post-commit log means NOT COMMITTED, which for a file the agent
    # authored means DROPPED. It says nothing about whether the exemption
    # predicate fired, because a fired exemption erases its own evidence.
    assert len(kept) == 1


def test_absence_from_post_commit_log_is_ambiguous(tmp_path):
    """Absence has two causes that the grep cannot distinguish.

    A path absent from the post-commit log was either (a) never recorded at
    all — no PostToolUse hook fired, so no exemption was possible — or (b)
    recorded, exempted, committed, and then pruned. These imply OPPOSITE
    diagnoses (a recording gap vs. the filter working correctly), and the
    grep returns the same zero for both. Pinned so the ambiguity is not
    rediscovered as a "finding" a third time.
    """
    never_recorded = _run_clear(
        tmp_path / "a",
        rows=[{"file": "core/scripts/other.py", "goal_id": "g-1"}],
        committed=["core/scripts/target.py"],
    )
    recorded_then_pruned = _run_clear(
        tmp_path / "b",
        rows=[
            {"file": "core/scripts/other.py", "goal_id": "g-1"},
            {"file": "core/scripts/target.py", "goal_id": "g-1"},
        ],
        committed=["core/scripts/target.py"],
    )

    assert "target.py" not in "\n".join(never_recorded)
    assert "target.py" not in "\n".join(recorded_then_pruned)
    assert never_recorded == recorded_then_pruned, (
        "the two causes must be byte-identical in the log — that IS the "
        "ambiguity being pinned"
    )


@pytest.mark.parametrize(
    "marker",
    ["filtered (concurrent-partner):", "filtered (partner-uncommitted-log):"],
)
def test_close_boundary_banner_pattern_matches_live_drop_markers(marker):
    """Coupling pin for the  close-boundary banner.

    iteration-close.sh greps iteration-commit.sh's merged output for the
    per-file drop markers. If iteration-commit renames a marker, the banner
    goes SILENT — the exact failure mode the banner exists to prevent, and
    one that no other test would catch. Pin both ends.
    """
    commit_src = ITERATION_COMMIT.read_text(encoding="utf-8")
    close_src = ITERATION_CLOSE.read_text(encoding="utf-8")

    assert marker in commit_src, (
        f"iteration-commit.sh no longer emits {marker!r} — update the "
        "close-boundary banner's grep in iteration-close.sh to match"
    )

    banner_pattern = re.search(
        r"grep -E '(filtered \\\(\([^']*\)\\\):)'", close_src
    )
    assert banner_pattern is not None, (
        "could not find the ATTRIBUTION DROP grep pattern in iteration-close.sh"
    )
    alternation = banner_pattern.group(1)
    # e.g. "filtered \((concurrent-partner|partner-uncommitted-log)\):"
    kind = marker[len("filtered (") : -len("):")]
    assert kind in alternation, (
        f"the close-boundary banner does not match {marker!r} — a drop of this "
        "kind would be silently invisible at the close boundary"
    )


def test_close_boundary_banner_records_to_execution_diary():
    """The banner must write to a channel that survives backgrounding.

    stdout alone is insufficient: the state-update phase call backgrounds past
    the 2-minute Bash timeout, and its output is then never read by the LLM —
    which is how the original drop became invisible in the first place.
    """
    close_src = ITERATION_CLOSE.read_text(encoding="utf-8")
    banner_idx = close_src.find("ATTRIBUTION DROP:")
    assert banner_idx != -1, "close-boundary ATTRIBUTION DROP banner is missing"

    tail = close_src[banner_idx : banner_idx + 2000]
    assert "execution-diary.sh" in tail and "append" in tail, (
        "the ATTRIBUTION DROP banner does not append to the execution diary — "
        "stdout alone does not survive a backgrounded close (g-115-4252)"
    )
