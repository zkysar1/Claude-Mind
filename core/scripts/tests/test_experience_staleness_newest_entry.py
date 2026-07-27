"""Regression tests for : experience-staleness-check must pick the
NEWEST entry by timestamp, not the LAST LINE by position.

The original loop was `for line in f: ... last_ts = ts` — last-write-wins over
file order — and a comment stated the premise outright: "JSONL append-only means
last line is newest." bravo's live experience.jsonl (429 entries) is NOT
timestamp-ordered: it ends with a 2026-07-10 entry while 2026-07-26 entries sit
earlier in the file. So the check reported 383.4h stale against an archive
written 15 minutes prior and false-fired the force_experience_archival sentinel
every iteration — a forcing gate demanding a filler experience record for a
fresh archive.

Same defect class as the tree node `checker-input-assumption-defects`: the
checker's input did not mean what the checker assumed. Note that a PRIOR fix to
this same function (g-115-1916, routing the read through the storage backend to
defeat a stale local mirror) corrected a different wrong-input problem here
without noticing the ordering assumption.

Tests drive the real shell entry point via the MIND_EXPERIENCE_FILE test seam
rather than a reimplementation, per
.claude/rules/probe-with-canonical-code-path.md.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from _bash_helpers import BASH  # : bare "bash" hits the System32 WSL launcher outside pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "experience-staleness-check.sh"

# Match the EXACT warning substring, never the bare word "stale": the script's own
# filename contains "stale", so any startup failure puts that word in stderr via the
# path and a bare-word assertion passes vacuously. This bit during development —
# a pre-fix probe copy died on a missing _paths.sh and its error message
# ("prefix-staleness.sh: ... No such file or directory") matched the bare word,
# reporting "warns=True" for a script that never executed a line of its logic.
WARN = "experience.jsonl stale for"


def _stamp(hours_ago):
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _write(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _run(exp_file):
    """Invoke the real script over exp_file. Returns (rc, stderr)."""
    env = dict(os.environ)
    env["MIND_EXPERIENCE_FILE"] = str(exp_file)
    env["STORAGE_BACKEND"] = "local"  # guard-955/rb-2983 — never touch the live store
    proc = subprocess.run(
        [BASH, str(SCRIPT)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stderr


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
def test_out_of_order_file_with_fresh_newest_entry_does_not_warn(tmp_path):
    """THE REGRESSION. Newest entry is in the MIDDLE; the last line is ancient.

    Pre-fix this reported the last line's age and fired. Post-fix the max
    timestamp governs, so a fresh archive reads fresh regardless of line order.
    """
    exp = tmp_path / "experience.jsonl"
    _write(exp, [
        {"id": "exp-old-a", "created": _stamp(400)},
        {"id": "exp-newest", "created": _stamp(0.3)},   # newest, NOT last
        {"id": "exp-old-b", "created": _stamp(383)},    # last line, ancient
    ])
    rc, err = _run(exp)
    assert rc == 0, err
    assert WARN not in err, (
        "a file whose NEWEST entry is 0.3h old must not warn, even though its "
        "LAST LINE is 383h old — that is the g-115-3211 defect\n" + err)


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
def test_genuinely_stale_archive_still_warns(tmp_path):
    """POSITIVE CONTROL. Without this, a checker that never fires would pass
    the test above vacuously (guard-1465)."""
    exp = tmp_path / "experience.jsonl"
    _write(exp, [
        {"id": "exp-old-a", "created": _stamp(400)},
        {"id": "exp-old-b", "created": _stamp(383)},
    ])
    rc, err = _run(exp)
    assert rc == 0, err
    assert WARN in err, (
        "every entry is >380h old — the gate MUST still fire; if it does not, "
        "the max-timestamp change disabled the check entirely\n" + err)


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
def test_unparseable_stamp_does_not_decide_freshness(tmp_path):
    """One malformed timestamp must be skipped, not swallow the whole file.

    Pre-fix, a garbage stamp on the LAST line set last_ts and then failed the
    single post-loop parse, exiting 0 silently — a genuinely stale archive went
    unreported. Post-fix the malformed entry is skipped and the real newest
    (here: ancient) entry governs, so the gate still fires. Measured on a
    runnable pre-fix copy: PRE warns=False, POST warns=True.
    """
    exp = tmp_path / "experience.jsonl"
    _write(exp, [
        {"id": "exp-old", "created": _stamp(400)},
        {"id": "exp-garbage", "created": "not-a-timestamp"},
    ])
    rc, err = _run(exp)
    assert rc == 0, err
    assert WARN in err, (
        "the sole parseable entry is 400h old; a malformed sibling stamp must "
        "not silence the check\n" + err)


@pytest.mark.skipif(not SCRIPT.exists(), reason="script not present")
def test_override_run_does_not_write_the_production_sentinel(tmp_path):
    """: the override path must not touch the live WM sentinel.

    The sentinel asserts "THIS AGENT's archive is stale" and gates precheck
    Phase 0-pre2. Under MIND_EXPERIENCE_FILE the input is a fixture, so that
    assertion is false by construction. The tests above isolated the checker's
    INPUT but not its WRITE: their `exp-old` / 400.0h payload landed in the
    production slot and false-fired the archival gate on a 0.7h-fresh archive
    one iteration later.

    Read-only on the live slot — snapshot, run, compare. The test never writes
    it; a difference IS the regression.
    """
    def read_slot():
        p = subprocess.run(
            [BASH, "core/scripts/wm-read.sh", "force_experience_archival", "--json"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        return p.stdout.strip()

    before = read_slot()
    exp = tmp_path / "experience.jsonl"
    _write(exp, [{"id": "exp-sentinel-probe", "created": _stamp(400)}])
    rc, err = _run(exp)
    after = read_slot()

    assert rc == 0, err
    assert WARN in err, "fixture is 400h old — the diagnostic warning must still print\n" + err
    assert "sentinel write SKIPPED" in err, (
        "the override run must announce that it suppressed the sentinel write\n" + err)
    assert after == before, (
        "the override run MUTATED the live force_experience_archival slot — this is the "
        "g-115-3217 regression.\nbefore: %s\nafter:  %s" % (before[:300], after[:300]))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
